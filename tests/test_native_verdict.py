"""The native backend's pure parts (M2 step 3, docs/plans/M2/M2-step3-warden-core.md §4.1):
the per-run verdict, the mount-table rule, exit codes and the guest's environment. Offline, on
every platform; the sandbox itself is tested in test_native.py (Linux).
"""
import asyncio

import pytest

from vestibule.backends import native
from vestibule.config import Limits

UID, GID = 1000, 1000
HOST_NS = {ns: 4026531000 + i for i, ns in enumerate(native._NAMESPACES)}
# The /proc masks and read-only paths that exist on the host, and whether each is a directory:
# the same kernel gives the sandbox's /proc the same ones. They match GOOD_MOUNTS below.
HOST_PROC = {"acpi": True, "keys": False, "sys": True}
DEVICES = ("null", "zero", "full", "random", "urandom", "tty")
# A sandbox's mount table as the kernel shows it (§3), with this host's extras: relatime
# everywhere, the host's own options on the device binds, two masks and one read-only path.
GOOD_MOUNTS = [
    ("/", "ext4", "ro,nosuid,nodev,relatime", "rw,discard"),
    ("/proc", "proc", "rw,nosuid,nodev,noexec,relatime", "rw"),
    ("/dev", "tmpfs", "rw,nosuid,noexec,relatime", "rw,size=64k,mode=755"),
    *[(f"/dev/{d}", "devtmpfs", "rw,nosuid,relatime", "rw,size=7954124k") for d in DEVICES],
    ("/dev/pts", "devpts", "rw,nosuid,noexec,relatime", "rw,mode=620,ptmxmode=666"),
    ("/dev/shm", "tmpfs", "rw,nosuid,nodev,relatime", "rw,size=65536k,uid=1000"),
    ("/tmp", "tmpfs", "rw,nosuid,nodev,relatime", "rw,size=65536k,uid=1000"),
    ("/workspace", "ext4", "rw,nosuid,nodev,relatime", "rw,discard"),
    ("/sandbox", "tmpfs", "ro,nosuid,nodev,relatime", "ro,mode=755"),
    ("/proc/acpi", "tmpfs", "ro,nosuid,nodev,noexec,relatime", "ro"),
    ("/proc/keys", "devtmpfs", "rw,nosuid,relatime", "rw,size=7954124k"),
    ("/proc/sys", "proc", "ro,nosuid,nodev,noexec,relatime", "rw"),
]


def _mountinfo(rows):
    return "".join(f"{i} 1 0:{i} / {point} {opts} shared:{i} - {fstype} src {supers}\n"
                   for i, (point, fstype, opts, supers) in enumerate(rows, 20))


def _ready(**changes):
    ready = {
        "checks": {name: True for name in native._HARD_CHECKS},
        "namespaces": {ns: inode + 100 for ns, inode in HOST_NS.items()},
        "uid_map": "      1000       1000          1\n",
        "gid_map": "      1000       1000          1\n",
        "setgroups": "deny\n",
        "mountinfo": _mountinfo(GOOD_MOUNTS),
    }
    ready.update(changes)
    return {"ready": ready}


def _verdict(lines, limits=Limits(), host_proc=HOST_PROC):
    return native._verdict(lines, uid=UID, gid=GID, host_ns=HOST_NS, host_proc=host_proc,
                           limits=limits)


def _with(point, opts=None, supers=None):
    """GOOD_MOUNTS with one row's options (or super options) replaced."""
    return [(p, t, opts if p == point and opts else o, supers if p == point and supers else s)
            for p, t, o, s in GOOD_MOUNTS]


# --- the verdict (S3-D8) ------------------------------------------------------------------

def test_a_setup_error_means_nothing_ran():
    isolation, detail = _verdict([{"error": "mounts", "message": "mount /x: EPERM"}])
    assert isolation == "none"
    assert detail == "native sandbox setup failed at mounts: mount /x: EPERM; nothing was executed"


def test_no_report_claims_nothing():
    isolation, detail = _verdict([])
    assert isolation == "none" and "no status report" in detail
    assert "nothing was executed" not in detail  # a guest may have run (S3-D8)


def test_an_error_after_ready_is_not_a_setup_failure():
    isolation, detail = _verdict([_ready(), {"error": "helper", "message": "x"}])
    assert "setup failed" not in detail


def test_everything_this_step_can_confirm_is_still_degraded():
    """No run has cgroup limits before step 4, so `native` is out of reach (S3-D8)."""
    assert _verdict([_ready()]) == ("native-degraded", "limits not applied: cpu, memory, pids")


@pytest.mark.parametrize("ready, named", [
    (_ready(checks={n: True for n in native._INIT_CHECKS}), "seccomp"),  # step 3's own runs
    (_ready(checks={**{n: True for n in native._HARD_CHECKS}, "caps": False}), "caps"),
    # a key the server doesn't know can't stand in for a missing one
    (_ready(checks={**{n: True for n in native._INIT_CHECKS}, "seccomp_ok": True}), "seccomp"),
    (_ready(namespaces={**{ns: i + 100 for ns, i in HOST_NS.items()}, "net": HOST_NS["net"]}),
     "namespaces"),
    (_ready(namespaces={ns: i + 100 for ns, i in HOST_NS.items() if ns != "pid"}), "namespaces"),
    (_ready(uid_map="0 1000 1"), "identity map"),
    (_ready(setgroups="allow"), "identity map"),
    (_ready(mountinfo=None), "mount table (unreadable)"),
])
def test_a_hard_control_not_confirmed_claims_nothing(ready, named):
    isolation, detail = _verdict([ready])
    assert isolation == "none"
    assert detail.startswith("hard controls not confirmed: ") and named in detail


@pytest.mark.parametrize("rows, problem", [
    (GOOD_MOUNTS + [("/mnt", "ext4", "rw,nosuid,nodev", "rw")], "unexpected mount at /mnt"),
    ([r for r in GOOD_MOUNTS if r[0] != "/tmp"], "/tmp is missing"),
    (GOOD_MOUNTS[:10] + [GOOD_MOUNTS[11], GOOD_MOUNTS[10]] + GOOD_MOUNTS[12:],  # /tmp first
     "/dev/shm is missing"),
    (_with("/", opts="rw,nosuid,nodev,relatime"), "/ lacks ro"),
    (_with("/sandbox", opts="ro,nosuid,relatime"), "/sandbox lacks nodev"),
    (_with("/proc/sys", opts="rw,nosuid,nodev,noexec"), "/proc/sys lacks ro"),
    ([(p, "tmpfs" if p == "/" else t, o, s) for p, t, o, s in GOOD_MOUNTS], None),  # any fstype
    ([(p, "ext4" if p == "/proc" else t, o, s) for p, t, o, s in GOOD_MOUNTS],
     "/proc is ext4, not proc"),
    # a mask or read-only path this kernel has must be there (second critic finding 3)
    ([r for r in GOOD_MOUNTS if r[0] != "/proc/keys"], "/proc/keys is missing"),
    ([r for r in GOOD_MOUNTS if r[0] != "/proc/sys"], "/proc/sys is missing"),
    ([r for r in GOOD_MOUNTS if not r[0].startswith("/proc/")], "/proc/acpi is missing"),
    (_with("/proc/acpi", opts="rw,nosuid,nodev,noexec,relatime"), "/proc/acpi lacks ro"),
    ([(p, "proc" if p == "/proc/keys" else t, o, s) for p, t, o, s in GOOD_MOUNTS],
     "/proc/keys is proc, not devtmpfs"),  # a file mask is a bind of the sandbox's /dev/null
])
def test_the_mount_table_rule(rows, problem):
    isolation, detail = _verdict([_ready(mountinfo=_mountinfo(rows))])
    if problem is None:
        assert isolation == "native-degraded"
    else:
        assert isolation == "none" and f"mount table ({problem})" in detail


@pytest.mark.parametrize("rows", [
    _with("/workspace", opts="rw,nosuid,nodev,noatime"),  # a noatime host mount (/mnt/c)
    _with("/workspace", opts="rw,nosuid,nodev,noexec,relatime"),  # a carried noexec (S3-D6)
    _with("/workspace", opts="ro,nosuid,nodev,relatime"),  # a carried ro
])
def test_allowed_differences_still_confirm_the_table(rows):
    assert _verdict([_ready(mountinfo=_mountinfo(rows))])[0] == "native-degraded"


def test_a_mask_only_the_kernel_lacks_may_be_absent():
    rows = [r for r in GOOD_MOUNTS if r[0] != "/proc/acpi"]
    without_acpi = {k: v for k, v in HOST_PROC.items() if k != "acpi"}
    assert _verdict([_ready(mountinfo=_mountinfo(rows))], host_proc=without_acpi)[0] \
        == "native-degraded"
    isolation, detail = _verdict([_ready()], host_proc=without_acpi)  # the sandbox has it
    assert isolation == "none" and "unexpected mount at /proc/acpi" in detail


def test_the_workspace_must_be_read_only_in_ro_mode():
    isolation, detail = _verdict([_ready()], Limits(workspace_ro=True))
    assert isolation == "none" and "/workspace lacks ro" in detail


def test_a_wrong_tmpfs_size_is_a_missing_soft_limit():
    rows = _with("/tmp", supers="rw,size=1024k,uid=1000")
    isolation, detail = _verdict([_ready(mountinfo=_mountinfo(rows))])
    assert isolation == "native-degraded" and detail.endswith("tmpfs-size")
    assert _verdict([_ready(mountinfo=_mountinfo(GOOD_MOUNTS))], Limits(tmpfs_mb=4))[1] \
        .endswith("tmpfs-size")


# --- the status pipe, exit codes, the environment -----------------------------------------

def test_status_lines_parse_with_a_cap():
    good = b'{"ready": {}}\nnot json\n[1]\n{"exit": {"code": 0}}\n{"cut'
    assert native._parse_status(good) == [{"ready": {}}, {"exit": {"code": 0}}]
    assert native._parse_status(b'{"exit": {"code": 0}}\n' + b" " * native._STATUS_CAP) == []


CUT = "\n[output exceeded collection cap; the sandbox was terminated]"
UNKNOWN = "\n[the sandbox did not report an exit status]"


@pytest.mark.parametrize("lines, timed_out, truncated, outcome", [
    ([_ready(), {"exit": {"code": 0}}], False, False, (0, "")),
    ([_ready(), {"exit": {"code": 3}}], False, False, (3, "")),
    ([_ready(), {"exit": {"signal": 9}}], False, False, (137, "")),  # Docker's 128+S
    ([_ready(), {"exit": {"code": "0"}}], False, False, (-1, UNKNOWN)),  # not a number
    ([_ready()], True, False, (-1, "")),  # M1's timeout code
    ([_ready()], False, True, (137, CUT)),  # M1's CLI code after its overflow kill
    ([_ready()], False, False, (-1, UNKNOWN)),  # the guest started; its end is unknown
    ([], False, False, (-1, "")),
    ([{"error": "mounts", "message": "x"}], False, False, (-1, "")),
])
def test_the_outcome_and_its_note(lines, timed_out, truncated, outcome):
    assert native._outcome(lines, timed_out=timed_out, truncated=truncated) == outcome


def test_the_guest_environment_is_the_images_plus_the_profile(monkeypatch):
    monkeypatch.setenv("VESTIBULE_CANARY_ENV", "leaked")
    env = native._guest_env(["PATH=/usr/local/bin:/usr/bin", "LANG=C.UTF-8", "HOME=/root", "x"])
    assert env == {"PATH": "/usr/local/bin:/usr/bin", "LANG": "C.UTF-8", "HOME": "/tmp",
                   "TMPDIR": "/tmp", "PYTHONDONTWRITEBYTECODE": "1", "NODE_OPTIONS": "",
                   "HOSTNAME": "vestibule"}


async def test_without_a_rootfs_nothing_is_spawned(tmp_path, monkeypatch):
    async def no_spawn(*args, **kwargs):
        raise AssertionError("nothing may be spawned")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", no_spawn)
    lim = Limits(workspace_dir=str(tmp_path / "ws"), rootfs_dir=str(tmp_path / "empty"))
    r = await native.NativeBackend(_skip_seccomp_for_tests=True).run("python", "1", 5, lim)
    assert r.isolation == "none" and r.exit_code == -1 and r.stdout == ""
    assert r.isolation_detail.endswith("; nothing was executed")
    assert "vestibule-mcp setup" in r.isolation_detail or "needs Linux" in r.isolation_detail
