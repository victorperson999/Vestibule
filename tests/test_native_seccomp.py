"""The seccomp binding (M2 step 5, docs/plans/M2/M2-step5-seccomp.md §4.1). Linux only, and
needs libseccomp; the `native` marker skips elsewhere. The binding is imported through a
fixture, never at module top, so collection never triggers its Linux-only import guard.

These build and probe a filter in-process; they do not need the rootfs or user namespaces, but
the `native` marker (which now also requires libseccomp, S5-D8) is the simplest honest gate.
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest
from nativeutil import native_required

pytestmark = [pytest.mark.native, native_required]

PROFILE_PATH = Path(__file__).resolve().parents[1] / "src" / "vestibule" / "backends" \
    / "seccomp-profile.json"


@pytest.fixture()
def sc():
    from vestibule.backends import native_seccomp
    return native_seccomp


@pytest.fixture()
def profile():
    return json.loads(PROFILE_PATH.read_text("utf-8"))


@pytest.fixture()
def ctx(sc, profile):
    c, info = sc.build(profile)
    yield c, info
    sc.release(c)


def test_build_has_a_sane_rule_count_and_only_32_bit_names_are_unknown(ctx):
    _c, info = ctx
    assert 330 <= info["rules"] <= 360, info["rules"]  # 347 on this host's libseccomp
    unknown = set(info["unknown"])
    assert {"mmap2", "socketcall", "stat64", "waitpid"} <= unknown  # 32-bit/legacy names
    # names we rely on must resolve on the 64-bit host, so they are NOT unknown
    assert not ({"mount", "openat", "clone", "socket", "execve"} & unknown)


def test_bad_arch_kills_the_process(sc, ctx):
    c, _info = ctx
    assert sc.attr_get(c, sc.FLTATR_ACT_BADARCH) == sc.ACT_KILL_PROCESS


def test_only_the_host_arch_is_in_the_filter(sc, ctx):
    c, _info = ctx
    assert sc.arch_exists(c, 0)  # SCMP_ARCH_NATIVE
    assert not sc.arch_exists(c, sc.ARCH_X86) and not sc.arch_exists(c, sc.ARCH_X32)


def test_name_resolves_and_falls_back(sc):
    assert sc.name(1) and not sc.name(1).startswith("#")  # a real syscall on the native arch
    assert sc.name(999999) == "#999999"


def test_ptrace_policy_is_allow_and_io_uring_is_never_notified(sc):
    assert sc.PTRACE_POLICY == "allow"  # S5-D1 (Victor 2026-10-26)
    assert not any(n.startswith("io_uring") for n in sc.NOTIFY)  # S5-D2: silent, never named
    assert set(sc.DENY_SILENT) == {"io_uring_setup", "io_uring_enter", "io_uring_register"}
    assert "ptrace" not in sc.NOTIFY  # allowed, so not in the reported subset


_CHILD = """
import ctypes, json, sys
sys.path.insert(0, {pkg!r})
import native_seccomp as sc
ctx, _ = sc.build(json.load(open({path!r})))
sc.load(ctx)
libc = ctypes.CDLL(None, use_errno=True)
def errno_of(rc):
    return 0 if rc == 0 else ctypes.get_errno()
# After load: io_uring_setup (425) is a silent EPERM; clone3 (435) is ENOSYS (glibc then falls
# back to clone). Neither is notified, so a lone child with no listener serviced won't block.
sys.exit(0 if errno_of(libc.syscall(425, 1, 0)) == 1
         and errno_of(libc.syscall(435, 0, 0)) == 38 else 1)
"""


def test_a_loaded_filter_denies_silently_without_a_listener(sc, tmp_path):
    """Loading the filter in a throwaway child and hitting a *silent-deny* syscall gives EPERM
    (and clone3 gives ENOSYS); a notified syscall would instead block in-kernel, so this test
    deliberately uses only silent-deny calls (§4.1)."""
    child = tmp_path / "child.py"
    child.write_text(_CHILD.format(pkg=str(PROFILE_PATH.parent), path=str(PROFILE_PATH)))
    p = subprocess.run([sys.executable, "-I", str(child)], timeout=30,
                       stdin=subprocess.DEVNULL, capture_output=True, text=True)
    assert p.returncode == 0, p.stderr
