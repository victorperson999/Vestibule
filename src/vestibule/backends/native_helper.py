"""The native warden helper (M2 step 3, docs/plans/M2/M2-step3-warden-core.md).

The server never imports this. For each run, `native.py` starts a fresh interpreter as
`vestibule-warden -S -P -c "exec(open(<fd>,'rb').read())"`, and this file is that program
(S3-D4). It is stdlib-only (plus the sibling `native_seccomp`), and everything it needs exists
before `pivot_root`: imports, ctypes types, the libc handle, and the compiled seccomp filter
(S3-D5, S5-D3). After `pivot_root` only os/posix calls, calls on those types,
socket.sethostname, fcntl.ioctl, resource's rlimit calls, json.dumps, and the seccomp
`load`/`notify_fd` (which create no new ctypes type) run.

Process shape (S3-D7):
- helper: dies with the server, waits for its release while the server places it in a cgroup
  (S4-D3), imports native_seccomp and compiles the filter (S5-D3), unshares user + PID
  namespaces, writes the identity maps, forks init and moves it into the guest's cgroup, relays
  init's status lines, answers the guest's seccomp notifications with EPERM (S5-D5), SIGKILLs
  init on SIGTERM, reports the guest cgroup's OOM counters and the denied syscalls, exits after
  init. Its exit therefore means the sandbox is empty.
- init (PID 1): its own session, the other namespaces, the jail, every privilege dropped, the
  seccomp filter loaded (its listener fd handed to the helper), its checks (S3-D8), then
  `ready`, the guest, a reap loop, and the guest's exit.
- guest: no extra fds, default signals, our own PATH search, execve; it inherits init's filter.

Input: the JSON run config on stdin, then the release on cfg["sync_fd"]. Output: JSON lines on
cfg["status_fd"] (`ready`, `error`, `exit`, the OOM counters, the denied syscalls). Nothing of
the helper's own reaches the guest's pipes: its stdout is /dev/null and its stderr is the log.
"""
from __future__ import annotations

import ctypes
import errno
import fcntl
import json
import os
import resource
import select
import signal
import socket
import struct
import sys
import time
from collections.abc import Callable
from typing import Any

if sys.platform != "linux":  # S3-D13: exec'd or imported on Linux only; mypy reads the guard
    raise ImportError("vestibule's native helper is Linux-only")

CLONE_NEWNS, CLONE_NEWCGROUP, CLONE_NEWUTS = 0x00020000, 0x02000000, 0x04000000
CLONE_NEWIPC, CLONE_NEWUSER = 0x08000000, 0x10000000
CLONE_NEWPID, CLONE_NEWNET = 0x20000000, 0x40000000
MS_RDONLY, MS_NOSUID, MS_NODEV, MS_NOEXEC = 1, 2, 4, 8
MS_REMOUNT, MS_BIND, MS_REC, MS_PRIVATE = 32, 4096, 16384, 1 << 18
MNT_DETACH = 2
PR_SET_PDEATHSIG, PR_GET_DUMPABLE, PR_SET_DUMPABLE, PR_CAPBSET_DROP = 1, 3, 4, 24
PR_SET_NO_NEW_PRIVS, PR_CAP_AMBIENT, PR_CAP_AMBIENT_CLEAR_ALL = 38, 47, 4
SIOCSIFFLAGS, IFF_UP = 0x8914, 0x1
SYS_PIVOT_ROOT = {"x86_64": 155, "aarch64": 41}
SYS_CLOSE_RANGE = 436  # the same on x86_64 and aarch64
DEVICES = ("null", "zero", "full", "random", "urandom", "tty")
DEV_LINKS = (("fd", "/proc/self/fd"), ("stdin", "/proc/self/fd/0"),
             ("stdout", "/proc/self/fd/1"), ("stderr", "/proc/self/fd/2"))
NAMESPACES = ("user", "pid", "mnt", "net", "uts", "ipc", "cgroup")

# The native_seccomp module, imported by _build_seccomp in the helper before it forks init, so
# both the helper (its notification loop) and init (load/notify_fd) inherit it (S5-D3).
_SECCOMP: Any = None


class _CapHeader(ctypes.Structure):
    _fields_ = [("version", ctypes.c_uint32), ("pid", ctypes.c_int)]


class _CapData(ctypes.Structure):
    _fields_ = [("effective", ctypes.c_uint32), ("permitted", ctypes.c_uint32),
                ("inheritable", ctypes.c_uint32)]


_CAP_V3 = _CapHeader(0x20080522, 0)
_NO_CAPS = (_CapData * 2)()  # version 3 takes two 32-bit halves, all zero here
_LIBC = ctypes.CDLL(None, use_errno=True)
_LIBC.mount.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_ulong,
                        ctypes.c_char_p]
_LIBC.umount2.argtypes = [ctypes.c_char_p, ctypes.c_int]
_LIBC.unshare.argtypes = [ctypes.c_int]
_LIBC.prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong,
                        ctypes.c_ulong]
_LIBC.syscall.restype = ctypes.c_long


def main() -> None:
    cfg = json.loads(_read_all(0))
    try:
        _helper(cfg)
    except BaseException as e:  # noqa: BLE001 - reported on the status pipe, never raised
        try:
            _send(cfg["status_fd"], {"error": "helper", "message": str(e)})
        finally:
            os._exit(70)


def _helper(cfg: dict[str, Any]) -> None:
    _prctl(PR_SET_PDEATHSIG, signal.SIGKILL)
    if os.getppid() != cfg["server_pid"]:  # the server died before PDEATHSIG was armed
        os._exit(0)
    release = _read_all(cfg["sync_fd"])  # paused until the server has placed us (S4-D3)
    if not release:  # the server gave up on this run
        os._exit(0)
    cfg.update(json.loads(release))  # {"cgroup": guest/sandbox | None, "nproc": n | None}
    last_cap = int(_read("/proc/sys/kernel/cap_last_cap"))
    pivot_nr = SYS_PIVOT_ROOT[os.uname().machine]
    uid, gid = os.getuid(), os.getgid()
    seccomp_ctx = _build_seccomp(cfg)  # before the fork: all ctypes types materialize now (S5-D3)
    _check(_LIBC.unshare(CLONE_NEWUSER | CLONE_NEWPID), "unshare")
    _write("/proc/self/setgroups", "deny")
    _write("/proc/self/uid_map", f"{uid} {uid} 1")
    _write("/proc/self/gid_map", f"{gid} {gid} 1")
    ours, theirs = socket.socketpair()
    pid = os.fork()
    if pid == 0:
        ours.close()
        _init(cfg, theirs, last_cap, pivot_nr, seccomp_ctx)  # never returns
    theirs.close()
    if cfg["cgroup"]:  # before init does anything; a failure here stops the run (S4-D5)
        _write(f"{cfg['cgroup']}/cgroup.procs", str(pid))
    ours.sendall(b"g")
    os.close(cfg["stdout_fd"])  # so the guest's pipes reach EOF only when the sandbox is gone
    os.close(cfg["stderr_fd"])
    pidfd = os.pidfd_open(pid)

    def stop(*_: object) -> None:  # through the pidfd: a reaped init's PID can't be reused
        try:
            signal.pidfd_send_signal(pidfd, signal.SIGKILL)
        except OSError:
            pass

    signal.signal(signal.SIGTERM, stop)
    denied, notify_stopped = _supervise(cfg["status_fd"], ours, pidfd, stop,
                                        cfg["self_deadline_s"])
    os.waitpid(pid, 0)
    if cfg["cgroup"]:  # S4-D8: before our exit, after which systemd removes the scope
        try:
            events = dict(line.split() for line in
                          _read(os.path.dirname(cfg["cgroup"]) + "/memory.events").splitlines())
            _send(cfg["status_fd"], {"oom": int(events["oom"]),
                                     "oom_kill": int(events["oom_kill"])})
        except (OSError, KeyError, ValueError):  # no memory controller, or the server is gone
            pass
    _send(cfg["status_fd"], {"denied": denied, "notify_stopped": notify_stopped})  # S5-D5
    os._exit(0)


def _build_seccomp(cfg: dict[str, Any]) -> int | None:
    """Import the binding and compile the filter, in the helper before it forks init (S5-D3), so
    every ctypes type is materialized while the stdlib is still reachable. Returns the ctx init
    will load, or None in the test-only bypass."""
    if cfg["skip_seccomp"]:
        return None
    global _SECCOMP
    sys.path.insert(0, cfg["pkg_dir"])
    sys.dont_write_bytecode = True  # don't write __pycache__ into a read-only install
    import native_seccomp  # type: ignore[import-not-found]  # loaded via the pkg_dir path insert
    _SECCOMP = native_seccomp
    ctx, _info = native_seccomp.build(cfg["profile"])
    return ctx


def _supervise(status_fd: int, sock: socket.socket, pidfd: int, stop: Callable[[], None],
               self_deadline_s: float) -> tuple[list[str], bool]:
    """Relay init's lines until init has exited; stop it at the self-deadline (N8); answer the
    guest's seccomp notifications with EPERM, recording names (S5-D5). Returns the deduplicated
    denied-syscall names and whether reporting was stopped at the cap.

    The *first* read of the init socket is `recv_fds` (S5-D7): if an fd rides along it is the
    notification listener (and the one-byte "L" tag is dropped); if not, those bytes are the
    first status chunk. So the filter path, the bypass, and an early init abort all frame right.
    """
    deadline: float | None = time.monotonic() + self_deadline_s
    poller = select.poll()
    poller.register(pidfd, select.POLLIN)
    poller.register(sock.fileno(), select.POLLIN)
    pending, sock_open, first = b"", True, True
    listener: int | None = None
    req = resp = None
    denied: list[str] = []
    seen: set[str] = set()
    count, stopped = 0, False
    while True:
        wait_ms = None
        if deadline is not None:
            left = deadline - time.monotonic()
            if left <= 0:  # no log line: the server's stderr may be stalled, and init must die
                stop()
                deadline = None
                continue
            wait_ms = left * 1000
        events = poller.poll(wait_ms)
        if not events:  # the deadline's wake-up: back to its check, never into a blocking recv
            continue
        if any(fd == pidfd for fd, _ in events):
            break
        if listener is not None and any(fd == listener for fd, _ in events):
            nr = _SECCOMP.receive(listener, req)
            if nr is not None:
                name = _SECCOMP.name(nr)
                if name not in seen:
                    seen.add(name)
                    denied.append(name)
                _SECCOMP.respond_eperm(listener, req, resp)
                count += 1
                if count >= _SECCOMP.NOTIFY_CAP:  # the kernel then fails the rest with ENOSYS
                    poller.unregister(listener)
                    os.close(listener)
                    listener, stopped = None, True
            continue
        if first:
            first = False
            msg, fds, _, _ = socket.recv_fds(sock, 65536, 1)
            if fds:  # the seccomp listener (S5-D7); the "L" tag carried no status bytes
                listener = fds[0]
                poller.register(listener, select.POLLIN)
                req, resp = _SECCOMP.alloc()
                continue
            chunk = msg  # no fd: these bytes are the first status chunk (ready, or an abort error)
        else:
            chunk = sock.recv(65536)
        if not chunk:
            poller.unregister(sock.fileno())
            sock_open = False
        pending = _relay_lines(status_fd, pending + chunk)
    while sock_open and (chunk := sock.recv(65536)):  # init's last lines: EOF now it's gone
        pending = _relay_lines(status_fd, pending + chunk)
    return denied, stopped


def _relay_lines(fd: int, data: bytes) -> bytes:
    """Write the complete lines in `data` to the status pipe; return the rest. A closed pipe
    is ignored: the server keeps it open until the helper exits (S3-D11)."""
    *lines, rest = data.split(b"\n")
    try:
        for line in lines:
            _write_all(fd, line + b"\n")
    except BrokenPipeError:
        pass
    return rest


def _init(cfg: dict[str, Any], sock: socket.socket, last_cap: int, pivot_nr: int,
          seccomp_ctx: int | None) -> None:
    out = sock.fileno()
    stage = "start"
    try:
        _prctl(PR_SET_PDEATHSIG, signal.SIGKILL)
        if sock.recv(1) != b"g":  # the helper died before our death signal was armed
            os._exit(0)
        cgroup = _read("/proc/self/cgroup")  # the host's view, before NEWCGROUP (S4-D7)
        signal.signal(signal.SIGINT, signal.SIG_DFL)  # PID 1 then ignores a guest's SIGINT
        os.setsid()  # no controlling terminal for init or the guest (S3-D7)
        stage = "namespaces"
        _check(_LIBC.unshare(CLONE_NEWNS | CLONE_NEWNET | CLONE_NEWUTS | CLONE_NEWIPC
                             | CLONE_NEWCGROUP), "unshare")
        stage = "mounts"
        root = cfg["rootfs"]
        _build_root(cfg, root)
        stage = "loopback"
        lo = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        fcntl.ioctl(lo.fileno(), SIOCSIFFLAGS, struct.pack("16sh22x", b"lo", IFF_UP))
        lo.close()
        stage = "pivot_root"
        os.chdir(root)
        _check(_LIBC.syscall(pivot_nr, b".", b"."), "pivot_root")
        _check(_LIBC.umount2(b".", MNT_DETACH), "detach the old root")
        os.chdir("/")
        socket.sethostname("vestibule")
        stage = "fds"
        os.dup2(os.open("/dev/null", os.O_RDONLY), 0)
        os.dup2(cfg["stdout_fd"], 1)  # 1 and 2 first: nothing still needed lives there
        os.dup2(cfg["stderr_fd"], 2)
        os.dup2(out, 3)
        out = 3
        _close_from(4)
        stage = "privileges"
        for cap in range(last_cap + 1):
            _prctl(PR_CAPBSET_DROP, cap)
        _prctl(PR_CAP_AMBIENT, PR_CAP_AMBIENT_CLEAR_ALL)
        _check(_LIBC.capset(ctypes.byref(_CAP_V3), ctypes.byref(_NO_CAPS)), "capset")
        _prctl(PR_SET_DUMPABLE, 0)
        _prctl(PR_SET_NO_NEW_PRIVS, 1)
        if cfg["nproc"]:  # the fork-bomb backstop when there's no pids limit (S4-D9)
            hard = resource.getrlimit(resource.RLIMIT_NPROC)[1]  # no process can raise it
            n = cfg["nproc"] if hard == resource.RLIM_INFINITY else min(cfg["nproc"], hard)
            resource.setrlimit(resource.RLIMIT_NPROC, (n, n))
        stage = "seccomp"  # load before the checks, so _checks reads Seccomp: 2 (S5-D6)
        if seccomp_ctx is not None:
            chan = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM, fileno=os.dup(out))
            try:
                _SECCOMP.load(seccomp_ctx)  # init is filtered too; the guest, forked below,
                lfd = _SECCOMP.notify_fd(seccomp_ctx)  # inherits the filter (N4)
                socket.send_fds(chan, [b"L"], [lfd])  # hand the listener to the helper (N7)
                os.close(lfd)  # init keeps no copy of the listener
            finally:
                chan.close()  # closes the dup of fd 3, not fd 3 itself
        stage = "checks"
        checks = _checks(cfg)
        _send(out, {"ready": {
            "checks": checks,
            "namespaces": {ns: os.stat(f"/proc/self/ns/{ns}").st_ino for ns in NAMESPACES},
            "uid_map": _read("/proc/self/uid_map"), "gid_map": _read("/proc/self/gid_map"),
            "setgroups": _read("/proc/self/setgroups"),
            "mountinfo": _read("/proc/self/mountinfo"),
            "cgroup": cgroup,
        }})
        stage = "guest"
        guest = os.fork()
        if guest == 0:
            _guest(cfg)  # never returns
    except BaseException as e:  # noqa: BLE001 - before the guest exists: report, never exec
        try:
            _send(out, {"error": stage, "message": str(e)})
        finally:
            os._exit(70)
    try:
        while True:  # reap orphans until the guest itself exits
            pid, status = os.waitpid(-1, 0)
            if pid == guest:
                break
        code = os.waitstatus_to_exitcode(status)
        _send(out, {"exit": {"signal": -code} if code < 0 else {"code": code}})
    finally:
        os._exit(0)


def _build_root(cfg: dict[str, Any], root: str) -> None:
    """The jail, every mount under the tree (§3's table, in order)."""
    _mount(None, "/", None, MS_REC | MS_PRIVATE)
    _bind(root, root, MS_RDONLY)  # read-only at once: every later write goes to a tmpfs
    _mount("proc", f"{root}/proc", "proc", MS_NOSUID | MS_NODEV | MS_NOEXEC)
    _mount("tmpfs", f"{root}/dev", "tmpfs", MS_NOSUID | MS_NOEXEC, "mode=755,size=64k")
    for name in DEVICES:
        open(f"{root}/dev/{name}", "x").close()
        _mount(f"/dev/{name}", f"{root}/dev/{name}", None, MS_BIND)
    os.mkdir(f"{root}/dev/pts")
    _mount("devpts", f"{root}/dev/pts", "devpts", MS_NOSUID | MS_NOEXEC,
           "newinstance,ptmxmode=0666,mode=0620")
    os.symlink("pts/ptmx", f"{root}/dev/ptmx")
    os.mkdir(f"{root}/dev/shm")
    _mount("tmpfs", f"{root}/dev/shm", "tmpfs", MS_NOSUID | MS_NODEV, "mode=1777,size=64m")
    for name, target in DEV_LINKS:
        os.symlink(target, f"{root}/dev/{name}")
    _mount("tmpfs", f"{root}/tmp", "tmpfs", MS_NOSUID | MS_NODEV,
           f"mode=1777,size={cfg['tmpfs_mb']}m")
    try:
        _bind(cfg["workspace"], f"{root}/workspace", MS_RDONLY if cfg["workspace_ro"] else 0)
    except OSError as e:
        if e.errno != errno.EINVAL:
            raise
        raise OSError(e.errno, "the workspace has another filesystem mounted inside it, "
                               "which a native sandbox can't use") from None
    _mount("tmpfs", f"{root}/sandbox", "tmpfs", MS_NOSUID | MS_NODEV, "mode=755")
    with open(f"{root}/sandbox/{cfg['script']}", "wb") as f:
        f.write(cfg["code"].encode())
    _mount(None, f"{root}/sandbox", None, MS_REMOUNT | MS_RDONLY | MS_NOSUID | MS_NODEV)
    for name in cfg["masked"]:  # Docker's masks (S3-D2); a path this kernel lacks is skipped
        path = f"{root}/proc/{name}"
        if os.path.isdir(path):
            _mount("tmpfs", path, "tmpfs", MS_RDONLY | MS_NOSUID | MS_NODEV | MS_NOEXEC)
        elif os.path.exists(path):
            _mount(f"{root}/dev/null", path, None, MS_BIND)
    for name in cfg["read_only"]:
        path = f"{root}/proc/{name}"
        if os.path.exists(path):
            _bind(path, path, MS_RDONLY | MS_NOEXEC)


def _bind(src: str, target: str, flags: int) -> None:
    """A non-recursive bind, remounted nosuid,nodev plus `flags`, keeping the source mount's
    ro and noexec: a user namespace can't drop those (S3-D6)."""
    held = os.statvfs(src).f_flag
    _mount(src, target, None, MS_BIND)
    if held & os.ST_RDONLY:
        flags |= MS_RDONLY
    if held & os.ST_NOEXEC:
        flags |= MS_NOEXEC
    _mount(None, target, None, MS_REMOUNT | MS_BIND | MS_NOSUID | MS_NODEV | flags)


def _checks(cfg: dict[str, Any]) -> dict[str, bool]:
    """Init's own checks (S3-D8): every one must pass before the guest exists."""
    sealed = os.ST_RDONLY | os.ST_NOSUID | os.ST_NODEV
    plain = os.ST_NOSUID | os.ST_NODEV
    want = {"/": sealed, "/sandbox": sealed, "/workspace": sealed if cfg["workspace_ro"] else plain,
            "/tmp": plain, "/dev/shm": plain}
    status = {k: v.strip() for k, _, v in
              (line.partition(":") for line in _read("/proc/self/status").splitlines())}
    interfaces = [line.split(":")[0].strip() for line in _read("/proc/net/dev").splitlines()[2:]]
    checks = {
        "mount_flags": all(os.statvfs(p).f_flag & w == w for p, w in want.items()),
        "caps": all(int(status[k], 16) == 0
                    for k in ("CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb")),
        "no_new_privs": status["NoNewPrivs"] == "1",
        "not_dumpable": _LIBC.prctl(PR_GET_DUMPABLE, 0, 0, 0, 0) == 0,
        "only_lo": interfaces == ["lo"],
        "no_tty": _read("/proc/self/stat").rsplit(")", 1)[1].split()[4] == "0",
    }
    failed = [name for name, ok in checks.items() if not ok]
    if failed:
        raise RuntimeError("checks failed: " + ", ".join(failed))
    # seccomp is reported, not run-blocking: the bypass still runs the guest, and the server's
    # verdict (not init) decides none-vs-native from this kernel-reported mode (S5-D6).
    checks["seccomp"] = status.get("Seccomp") == "2"  # 2 = SECCOMP_MODE_FILTER
    return checks


def _guest(cfg: dict[str, Any]) -> None:
    try:
        _close_from(3)
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)  # Python's SIG_IGN survives execve
        signal.signal(signal.SIGXFSZ, signal.SIG_DFL)
        os.chdir("/workspace")
        name, env, failure = cfg["argv"][0], cfg["env"], None
        for directory in env.get("PATH", "").split(":"):  # os.execvpe would import (S3-D5)
            try:
                os.execve(f"{directory}/{name}", cfg["argv"], env)
            except OSError as e:
                if failure is None or failure.errno in (errno.ENOENT, errno.ENOTDIR):
                    failure = e
        reason = failure.strerror if failure else "no PATH"
        os.write(2, f"vestibule: cannot start {name}: {reason}\n".encode())
    except BaseException as e:  # noqa: BLE001 - never back into init's code
        os.write(2, f"vestibule: cannot start the program: {e}\n".encode())
    os._exit(127)


def _mount(src: str | None, target: str, fstype: str | None, flags: int,
           data: str | None = None) -> None:
    _check(_LIBC.mount(src and os.fsencode(src), os.fsencode(target),
                       fstype and fstype.encode(), flags, data and data.encode()),
           f"mount {target}")


def _prctl(option: int, arg: int) -> None:
    _check(_LIBC.prctl(option, arg, 0, 0, 0), f"prctl({option})")


def _close_from(first: int) -> None:  # os.closerange loops a million times on some builds
    _check(_LIBC.syscall(SYS_CLOSE_RANGE, ctypes.c_uint(first), ctypes.c_uint(0xFFFFFFFF),
                         ctypes.c_uint(0)), "close_range")


def _check(rc: int, what: str) -> None:
    if rc != 0:
        e = ctypes.get_errno()
        raise OSError(e, f"{what}: {os.strerror(e)}")


def _send(fd: int, message: dict[str, Any]) -> None:
    _write_all(fd, json.dumps(message).encode() + b"\n")


def _write_all(fd: int, data: bytes) -> None:
    while data:
        data = data[os.write(fd, data):]


def _read(path: str) -> str:
    with open(path) as f:
        return f.read()


def _write(path: str, text: str) -> None:
    with open(path, "w") as f:
        f.write(text)


def _read_all(fd: int) -> bytes:
    chunks = []
    while chunk := os.read(fd, 1 << 20):
        chunks.append(chunk)
    return b"".join(chunks)


if __name__ == "__main__":
    main()
