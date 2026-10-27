"""The seccomp binding + filter builder (M2 step 5, docs/plans/M2/M2-step5-seccomp.md).

Our own small ctypes binding over the system's `libseccomp.so.2` (N4): libseccomp compiles the
BPF, so we never hand-assemble it. The allowlist is *derived* from moby's default seccomp
profile, vendored verbatim beside this file as `seccomp-profile.json`.

    seccomp-profile.json is moby/profiles@6fe7deb1b9fb7c0397a4593480d7d22b9ee8caef
    (github.com/moby/profiles), Apache-2.0. Vestibule itself is MIT. The allowlist Vestibule
    loads is derived from that file for a capability-less, amd64/aarch64 guest.

Like `native_helper.py`, this module is Linux-only and is imported only inside the warden
helper process, before it forks init (S5-D3): `build()` materialises every ctypes type while
the stdlib is still reachable, so init's post-`pivot_root` `load`/`notify_fd` create none. The
server (`native.py`) never imports this; it only reads the JSON with plain `json`.
"""
from __future__ import annotations

import ctypes
import os
import sys
from typing import Any

if sys.platform != "linux":  # imported on Linux only; the guard lets Windows mypy read it
    raise ImportError("vestibule's seccomp binding is Linux-only")

# --- the named dangerous subset: denied AND reported by name via user notification (N4) ------
# io_uring_* are NOT here (S5-D2): they are denied silently. ptrace/process_vm_* are governed by
# PTRACE_POLICY below (S5-D1).
NOTIFY = (
    "mount", "umount2", "pivot_root", "open_tree", "move_mount", "fsopen", "fsconfig",
    "fsmount", "fspick", "mount_setattr", "unshare", "setns", "bpf", "keyctl", "add_key",
    "request_key", "perf_event_open", "userfaultfd", "init_module", "finit_module",
    "delete_module", "kexec_load", "kexec_file_load", "reboot", "swapon", "swapoff",
    "open_by_handle_at",
)
# Denied silently (S5-D2): excluded from the ported allowlist, never notified, so they fall
# through to the profile's default EPERM. The exclusion is the mechanism, not an override rule.
DENY_SILENT = ("io_uring_setup", "io_uring_enter", "io_uring_register")
# Socket families allowed for socket() and socketpair() (N4); every other family (incl.
# AF_VSOCK=40, not isolated by the network namespace) falls through to default-deny.
FAMILIES = (1, 2, 10, 16)  # AF_UNIX, AF_INET, AF_INET6, AF_NETLINK
AF_VSOCK = 40
_SOCKET_RESTRICTED = ("socket", "socketpair")
# S5-D1 (Victor 2026-10-26): follow Docker. ptrace/process_vm_* are allowed on kernels >= 4.8
# exactly as moby's profile block 1 does, so they are kept in the allowlist, not notified.
PTRACE_POLICY = "allow"  # "allow" | "deny"
_PTRACE = ("ptrace", "process_vm_readv", "process_vm_writev")

NOTIFY_CAP = 256  # after this many notifications the helper closes the listener (N4, spike)

ACT_ALLOW = 0x7FFF0000
ACT_NOTIFY = 0x7FC00000
ACT_KILL_PROCESS = 0x80000000


def ACT_ERRNO(e: int) -> int:
    return 0x00050000 | (e & 0xFFFF)


_EPERM = 1
FLTATR_ACT_BADARCH = 2  # SCMP_FLTATR_ACT_BADARCH
ARCH_X86, ARCH_X32 = 0x40000003, 0x4000003E  # for the single-arch check (§4.1)
# libseccomp's enum scmp_compare. The profile's applicable entries use several: MASKED_EQ on
# clone's flags (the rule that denies CLONE_NEW*), and EQ on personality and the family rules.
_OPS = {"SCMP_CMP_NE": 1, "SCMP_CMP_LT": 2, "SCMP_CMP_LE": 3, "SCMP_CMP_EQ": 4,
        "SCMP_CMP_GE": 5, "SCMP_CMP_GT": 6, "SCMP_CMP_MASKED_EQ": 7}

_lib = ctypes.CDLL("libseccomp.so.2", use_errno=True)
_libc = ctypes.CDLL(None)
_libc.free.argtypes = [ctypes.c_void_p]


class ArgCmp(ctypes.Structure):
    _fields_ = [("arg", ctypes.c_uint), ("op", ctypes.c_int),
                ("datum_a", ctypes.c_uint64), ("datum_b", ctypes.c_uint64)]


class SeccompData(ctypes.Structure):
    _fields_ = [("nr", ctypes.c_int), ("arch", ctypes.c_uint32),
                ("ip", ctypes.c_uint64), ("args", ctypes.c_uint64 * 6)]


class Notif(ctypes.Structure):
    _fields_ = [("id", ctypes.c_uint64), ("pid", ctypes.c_uint32),
                ("flags", ctypes.c_uint32), ("data", SeccompData)]


class NotifResp(ctypes.Structure):
    _fields_ = [("id", ctypes.c_uint64), ("val", ctypes.c_int64),
                ("error", ctypes.c_int32), ("flags", ctypes.c_uint32)]


_lib.seccomp_init.restype = ctypes.c_void_p
_lib.seccomp_init.argtypes = [ctypes.c_uint32]
_lib.seccomp_release.argtypes = [ctypes.c_void_p]
_lib.seccomp_attr_set.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_uint32]
_lib.seccomp_attr_get.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                  ctypes.POINTER(ctypes.c_uint32)]
_lib.seccomp_arch_exist.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
_lib.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
_lib.seccomp_syscall_resolve_name.restype = ctypes.c_int
_lib.seccomp_syscall_resolve_num_arch.restype = ctypes.c_void_p  # malloc'd char*
_lib.seccomp_syscall_resolve_num_arch.argtypes = [ctypes.c_uint32, ctypes.c_int]
_lib.seccomp_rule_add_array.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int,
                                        ctypes.c_uint, ctypes.POINTER(ArgCmp)]
_lib.seccomp_load.argtypes = [ctypes.c_void_p]
_lib.seccomp_notify_fd.argtypes = [ctypes.c_void_p]
_lib.seccomp_notify_fd.restype = ctypes.c_int
_lib.seccomp_notify_alloc.argtypes = [ctypes.POINTER(ctypes.POINTER(Notif)),
                                      ctypes.POINTER(ctypes.POINTER(NotifResp))]
_lib.seccomp_notify_receive.argtypes = [ctypes.c_int, ctypes.POINTER(Notif)]
_lib.seccomp_notify_respond.argtypes = [ctypes.c_int, ctypes.POINTER(NotifResp)]

_SCMP_ERROR = -1  # __NR_SCMP_ERROR: resolve_name returns this (or other negatives) when unknown


def _kernel() -> tuple[int, int]:
    major, minor = (int(p) for p in os.uname().release.split("-")[0].split(".")[:2])
    return major, minor


def _applies(entry: dict[str, Any], kern: tuple[int, int]) -> bool:
    """Whether a moby entry applies to a capability-less, native-arch guest."""
    inc, exc = entry.get("includes", {}), entry.get("excludes", {})
    if inc.get("caps"):  # the guest has no capabilities, so cap-gated entries are dead
        return False
    arches = inc.get("arches")
    if arches and "amd64" not in arches and "arm64" not in arches:
        return False
    if {"amd64", "arm64"} & set(exc.get("arches", [])):
        return False
    mk = inc.get("minKernel")
    return not mk or kern >= tuple(int(x) for x in mk.split(".")[:2])


def build(profile: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """Compile the allowlist from a moby profile dict. Returns (ctx, info). Runs in the helper
    before the init fork, so every ctypes array type is materialised pre-`pivot_root` (S5-D3)."""
    kern = _kernel()
    ctx = _lib.seccomp_init(ACT_ERRNO(profile.get("defaultErrnoRet", _EPERM)))
    if not ctx:
        raise OSError("seccomp_init failed")
    _attr_set(ctx, FLTATR_ACT_BADARCH, ACT_KILL_PROCESS)  # a foreign-arch syscall kills (N4)
    notify = set(NOTIFY)
    if PTRACE_POLICY == "deny":  # S5-D1: otherwise ptrace/process_vm_* stay allowed (moby)
        notify |= set(_PTRACE)
    # allow entries for these names are dropped; the stricter/notify rules below replace them
    skip = set(DENY_SILENT) | set(_SOCKET_RESTRICTED) | notify
    info: dict[str, Any] = {"rules": 0, "unknown": []}
    for entry in profile["syscalls"]:
        if not _applies(entry, kern):
            continue
        action = _action(entry)
        if action is None:
            continue
        args = entry.get("args") or []
        arr = (ArgCmp * max(len(args), 1))(
            *[ArgCmp(a["index"], _OPS[a["op"]], a["value"], a.get("valueTwo", 0)) for a in args])
        for name in entry["names"]:
            if action == ACT_ALLOW and name in skip:  # replaced by the stricter rules below
                continue
            _add(ctx, action, name, args, arr, info)
    for name in _SOCKET_RESTRICTED:  # socket/socketpair only for the allowed families (N4)
        for fam in FAMILIES:
            fam_arg = (ArgCmp * 1)(ArgCmp(0, _OPS["SCMP_CMP_EQ"], fam, 0))
            _add(ctx, ACT_ALLOW, name, [1], fam_arg, info)
    vsock = (ArgCmp * 1)(ArgCmp(0, _OPS["SCMP_CMP_EQ"], AF_VSOCK, 0))  # named, not silent
    _add(ctx, ACT_NOTIFY, "socket", [1], vsock, info)
    for name in sorted(notify):  # the named dangerous subset
        _add(ctx, ACT_NOTIFY, name, [], None, info)
    return ctx, info


def _action(entry: dict[str, Any]) -> int | None:
    if entry["action"] == "SCMP_ACT_ALLOW":
        return ACT_ALLOW
    if entry["action"] == "SCMP_ACT_ERRNO":
        return ACT_ERRNO(entry.get("errnoRet", _EPERM))
    return None  # a profile action we don't port (e.g. LOG/TRACE): leave to default-deny


def _add(ctx: int, action: int, name: str, args: list[Any],
         arr: "ctypes.Array[ArgCmp] | None", info: dict[str, Any]) -> None:
    nr = _lib.seccomp_syscall_resolve_name(name.encode())
    if nr <= _SCMP_ERROR:  # a 32-bit-only name on a 64-bit host: skip it, default-deny covers it
        info["unknown"].append(name)
        return
    rc = _lib.seccomp_rule_add_array(ctx, action, nr, len(args), arr)
    if rc != 0:
        raise OSError(-rc, f"seccomp_rule_add {name}: {os.strerror(-rc)}")
    info["rules"] += 1


def load(ctx: int) -> None:
    rc = _lib.seccomp_load(ctx)
    if rc != 0:
        raise OSError(-rc, f"seccomp_load: {os.strerror(-rc)}")


def notify_fd(ctx: int) -> int:
    fd = _lib.seccomp_notify_fd(ctx)
    if fd < 0:
        raise OSError(-fd, f"seccomp_notify_fd: {os.strerror(-fd)}")
    return fd


def alloc() -> tuple[Any, Any]:
    req = ctypes.POINTER(Notif)()
    resp = ctypes.POINTER(NotifResp)()
    if _lib.seccomp_notify_alloc(ctypes.byref(req), ctypes.byref(resp)) != 0:
        raise OSError("seccomp_notify_alloc failed")
    return req, resp


def receive(fd: int, req: Any) -> int | None:
    """Receive one notification; return the syscall number, or None if it's gone (the guest
    died or the id went stale)."""
    ctypes.memset(req, 0, ctypes.sizeof(Notif))
    if _lib.seccomp_notify_receive(fd, req) != 0:
        return None
    return int(req.contents.data.nr)


def respond_eperm(fd: int, req: Any, resp: Any) -> None:
    """Answer the pending notification with EPERM (the call is denied, like default-deny)."""
    ctypes.memset(resp, 0, ctypes.sizeof(NotifResp))
    resp.contents.id = req.contents.id
    resp.contents.error = -_EPERM
    _lib.seccomp_notify_respond(fd, resp)


def name(nr: int) -> str:
    p = _lib.seccomp_syscall_resolve_num_arch(0, nr)  # 0 = SCMP_ARCH_NATIVE
    if not p:
        return f"#{nr}"
    try:
        return ctypes.string_at(p).decode()
    finally:
        _libc.free(p)


def _attr_set(ctx: int, attr: int, value: int) -> None:
    if _lib.seccomp_attr_set(ctx, attr, value) != 0:
        raise OSError(f"seccomp_attr_set({attr}) failed")


def attr_get(ctx: int, attr: int) -> int:  # for the §4.1 bad-arch attribute test
    out = ctypes.c_uint32()
    if _lib.seccomp_attr_get(ctx, attr, ctypes.byref(out)) != 0:
        raise OSError(f"seccomp_attr_get({attr}) failed")
    return int(out.value)


def arch_exists(ctx: int, arch: int) -> bool:  # for the §4.1 single-arch test
    return _lib.seccomp_arch_exist(ctx, arch) == 0


def release(ctx: int) -> None:
    _lib.seccomp_release(ctx)
