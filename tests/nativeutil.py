"""Shared helpers for the native-marked tests (M2 steps 3 and 4): two skip rules, a scan for
the host processes that carry a token in their command line, and the user manager's run scopes.

The skip rules: these tests need Linux, working unprivileged user namespaces, and both pinned
trees that `vestibule-mcp setup` makes in the default rootfs directory. The cgroup tests also
need a systemd user manager that delegates cpu, memory and pids (S4-D10). Anywhere else they
skip with the reason. Nothing here downloads anything.
"""
import ctypes
import os
import subprocess
import sys

import pytest

from vestibule import rootfs
from vestibule.backends import native_cgroup
from vestibule.config import Limits

_USERNS_PROBE = ("import ctypes, sys; "
                 "sys.exit(ctypes.CDLL(None, use_errno=True).unshare(0x10000000) != 0)")


def native_skip_reason() -> str | None:
    """None if the native-marked tests can run here, else why not (with the fix)."""
    if sys.platform != "linux":
        return "native isolation is Linux-only"
    lim = Limits()
    if any(rootfs.find(lim.rootfs_path, ref) is None for ref in (lim.image_python, lim.image_node)):
        return f"no native rootfs in {lim.rootfs_path}; run `vestibule-mcp setup` first"
    probe = subprocess.run([sys.executable, "-c", _USERNS_PROBE], timeout=10,
                           stdin=subprocess.DEVNULL, capture_output=True)
    if probe.returncode != 0:
        return "unprivileged user namespaces don't work here"
    try:
        ctypes.CDLL("libseccomp.so.2")  # native requires it since M2 step 5 (N4, S5-D8)
    except OSError:
        return "libseccomp.so.2 is not installed (native requires it; `apt install libseccomp2`)"
    return None


SKIP_REASON = native_skip_reason()
native_required = pytest.mark.skipif(SKIP_REASON is not None, reason=SKIP_REASON or "")


def cgroups_skip_reason() -> str | None:
    """None if the cgroup tests can run here: a user bus (S4-D4's rule), and a user manager
    that delegates the guest's three controllers."""
    if SKIP_REASON is not None:
        return SKIP_REASON
    if native_cgroup.bus_env() is None:
        return "no systemd user bus here (the cgroup tests need a user manager)"
    uid = os.getuid()
    path = f"{native_cgroup.CGROUP_FS}/user.slice/user-{uid}.slice/user@{uid}.service"
    try:
        with open(f"{path}/cgroup.controllers") as f:
            missing = {"cpu", "memory", "pids"} - set(f.read().split())
    except OSError:
        return f"no user manager cgroup at {path}"
    if missing:
        return f"user@{uid}.service doesn't delegate {', '.join(sorted(missing))}"
    return None


CGROUPS_SKIP_REASON = cgroups_skip_reason()
cgroups_required = pytest.mark.skipif(CGROUPS_SKIP_REASON is not None,
                                      reason=CGROUPS_SKIP_REASON or "")


def scopes() -> set[str] | None:
    """The user manager's vestibule-*.scope units in any state, failed ones included; None
    without a user manager to ask."""
    env = native_cgroup.bus_env()
    if env is None:
        return None
    p = subprocess.run(["systemctl", "--user", "list-units", "--all", "--plain", "--no-legend",
                        "vestibule-*.scope"], env=env, capture_output=True, text=True, timeout=10,
                       stdin=subprocess.DEVNULL)
    if p.returncode != 0:
        return None
    return {line.split()[0] for line in p.stdout.splitlines() if line.strip()}


def holders(token: str) -> list[int]:
    """PIDs of the host's processes whose command line contains `token`."""
    found = []
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        try:
            with open(f"/proc/{name}/cmdline", "rb") as f:
                if token.encode() in f.read():
                    found.append(int(name))
        except OSError:
            pass
    return found
