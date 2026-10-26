"""M2 native backend, server side (docs/plans/M2/M2-step3-warden-core.md).

Each run gets one fresh sandbox, built by a fresh helper interpreter (`native_helper.py`; N1,
S3-D4). This module starts the helper, collects the guest's output with M1's caps, stops a run
that overruns (S3-D7), and turns the helper's status report into an honest verdict (S3-D8).

Not reachable from the server until M2 step 6: tests build it directly. There is no seccomp
filter until step 5, so every run reports `isolation: none`, and only the test-only
`_skip_seccomp_for_tests` lets the sandbox run at all (S3-D9).

The verdict, the mount-table rule, the exit codes and the guest's environment are pure
functions that run on every platform; the rest is Linux-only behind `sys.platform` checks
that mypy reads (S3-D13).
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import secrets
import signal
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from vestibule import rootfs
from vestibule.backends.base import RunRefusedError, RunResult, Warden
from vestibule.config import Limits

log = logging.getLogger("vestibule.native")

_HELPER = Path(__file__).with_name("native_helper.py")
_EXT = {"python": "py", "bash": "sh", "node": "js"}
_INTERPRETER = {"python": "python", "bash": "bash", "node": "node"}
# The guest's environment beyond the image's own Env: the container profile's (N9).
_PROFILE_ENV = {"HOME": "/tmp", "TMPDIR": "/tmp", "PYTHONDONTWRITEBYTECODE": "1",
                "NODE_OPTIONS": "", "HOSTNAME": "vestibule"}
# Docker's /proc masks and read-only paths, as Docker 28.5.1 applies them (S3-D2).
MASKED = ("acpi", "asound", "interrupts", "kcore", "keys", "latency_stats", "sched_debug",
          "scsi", "timer_list", "timer_stats")
READ_ONLY = ("bus", "fs", "irq", "sys", "sysrq-trigger")
_DEVICES = ("null", "zero", "full", "random", "urandom", "tty")
_SHM_KB = 64 * 1024
# S3-D10: the sandbox is stopped at timeout_s + this. Native starts in ~40 ms (node ~190 ms
# with a cold page cache); M1 needed 5 s for container start.
_STARTUP_GRACE_S = 2
_STOP_WAIT_S = 5  # after SIGTERM, the helper gets this long before SIGKILL (S3-D7)
_SEM_WAIT_S = 5  # max wait for a concurrency slot before a legible refusal (S4-D1)
_STATUS_CAP = 64 * 1024
_SELF_DEADLINE_SLACK_S = 30  # the helper's own backstop sits at the server's outer deadline
# The hard tier is the server's own list (S3-D8), never one the helper sends.
_INIT_CHECKS = ("mount_flags", "caps", "no_new_privs", "not_dumpable", "only_lo", "no_tty")
_HARD_CHECKS = _INIT_CHECKS + ("seccomp",)
_NAMESPACES = ("user", "pid", "mnt", "net", "uts", "ipc", "cgroup")
# Applied and checked from M2 step 4 on; until then no run has them.
_CGROUP_LIMITS = ("cpu", "memory", "pids")


@dataclass
class _Run:
    """One run's moving parts. The finisher owns whatever is left once the result is out."""

    run_id: str
    proc: asyncio.subprocess.Process | None = None
    stdout: asyncio.StreamReader | None = None
    stderr: asyncio.StreamReader | None = None
    status: bytearray = field(default_factory=bytearray)
    status_task: asyncio.Task[None] | None = None
    transports: list[asyncio.ReadTransport] = field(default_factory=list)


class NativeBackend(Warden):
    """Runs each snippet in a fresh native sandbox: namespaces and a pivot_root jail."""

    def __init__(self, *, _skip_seccomp_for_tests: bool = False) -> None:
        # S3-D9: no config value or selector path passes this. Without it, init stops at its
        # seccomp stage, so nothing runs until step 5 brings the filter.
        self._skip_seccomp = _skip_seccomp_for_tests
        self._sem: asyncio.Semaphore | None = None
        self._finishers: set[asyncio.Task[None]] = set()  # strong refs: loop holds tasks weakly

    async def run(self, language: str, code: str, timeout_s: int, limits: Limits) -> RunResult:
        if sys.platform != "linux":
            return _not_run("native isolation needs Linux")
        image = limits.image_node if language == "node" else limits.image_python
        found = rootfs.find(limits.rootfs_path, image)
        if found is None:
            return _not_run(f"no native rootfs for {image} in {limits.rootfs_path}; "
                            "run `vestibule-mcp setup`")
        sem = self._sem
        if sem is None:  # lazy: bound to the running loop; no await before assignment
            sem = self._sem = asyncio.Semaphore(limits.max_concurrent)
        try:
            async with asyncio.timeout(_SEM_WAIT_S):
                await sem.acquire()
        except TimeoutError:
            raise RunRefusedError(
                f"too many concurrent runs (max {limits.max_concurrent}); retry shortly"
            ) from None
        run = _Run(secrets.token_hex(8))
        try:
            try:
                await self._spawn(run, language, code, timeout_s, limits, found)
            except OSError as e:
                log.error("run %s: could not start the native helper: %s", run.run_id, e)
                return _not_run(f"could not start the native sandbox helper: {e}")
            return await self._collect(run, timeout_s, limits)
        finally:
            # Detached, as M1's finisher (P2): it stops whatever still runs, waits until the
            # helper is gone (the sandbox is then empty), and only then hands back the slot.
            task = asyncio.create_task(self._finish(run, sem))
            self._finishers.add(task)
            task.add_done_callback(self._finishers.discard)

    async def _spawn(self, run: _Run, language: str, code: str, timeout_s: int,
                     limits: Limits, found: rootfs.Rootfs) -> None:
        if sys.platform != "linux":  # never: run() returns first; the guard is for mypy
            raise RuntimeError("native isolation needs Linux")
        workspace = limits.workspace_path
        workspace.mkdir(parents=True, exist_ok=True)
        loop = asyncio.get_running_loop()
        loose: list[int] = []  # fds this function still has to close
        try:
            out_r, out_w = os.pipe()
            loose += [out_r, out_w]
            err_r, err_w = os.pipe()
            loose += [err_r, err_w]
            status_r, status_w = os.pipe()
            loose += [status_r, status_w]
            config = {
                "server_pid": os.getpid(),
                "status_fd": status_w, "stdout_fd": out_w, "stderr_fd": err_w,
                "rootfs": os.path.abspath(found.tree),  # the helper's cwd is /
                "workspace": os.path.abspath(workspace),
                "workspace_ro": limits.workspace_ro,
                "tmpfs_mb": limits.tmpfs_mb,
                "script": f"main.{_EXT[language]}",
                "code": code,
                "argv": [_INTERPRETER[language], f"/sandbox/main.{_EXT[language]}"],
                "env": _guest_env(found.env),
                "masked": list(MASKED), "read_only": list(READ_ONLY),
                "self_deadline_s": timeout_s + _SELF_DEADLINE_SLACK_S,
                "skip_seccomp": self._skip_seccomp,
            }
            # S3-D1/S3-D4: the config, code included, travels in an anonymous memfd on the
            # helper's stdin; nothing of the run is written to the host's disk.
            config_fd = os.memfd_create("vestibule-run", os.MFD_CLOEXEC)
            loose.append(config_fd)
            data = json.dumps(config).encode()
            while data:
                data = data[os.write(config_fd, data):]
            os.lseek(config_fd, 0, os.SEEK_SET)
            source = os.open(_HELPER, os.O_RDONLY | os.O_CLOEXEC)
            loose.append(source)
            log.info("run %s: starting a native sandbox (%s)", run.run_id, language)
            run.proc = await asyncio.create_subprocess_exec(
                # A neutral argv[0], and the code by fd: the guest's /proc/1/cmdline shows no
                # host path (criterion 10). PYTHONHOME tells the interpreter where it lives.
                "vestibule-warden", "-S", "-P", "-c", f"exec(open({source},'rb').read())",
                executable=os.path.realpath(sys.executable),
                stdin=config_fd,
                stdout=asyncio.subprocess.DEVNULL,  # golden rule 1
                stderr=None,  # the server's own stderr: the helper's diagnostics go to the log
                pass_fds=(source, status_w, out_w, err_w),
                env={"PYTHONHOME": f"{sys.base_prefix}:{sys.base_exec_prefix}"},
                cwd="/",
            )
            for fd in (config_fd, source, status_w, out_w, err_w):
                os.close(fd)
                loose.remove(fd)
            readers = []
            for fd in (out_r, err_r, status_r):
                pipe = os.fdopen(fd, "rb", buffering=0)
                loose.remove(fd)
                reader = asyncio.StreamReader()
                protocol = asyncio.StreamReaderProtocol(reader)
                transport, _ = await loop.connect_read_pipe(lambda: protocol, pipe)
                run.transports.append(transport)
                readers.append(reader)
            run.stdout, run.stderr, status = readers
            # Drained to EOF whatever happens to the result, so the helper's last status
            # write never blocks or fails (S3-D11).
            run.status_task = asyncio.create_task(_drain_status(status, run.status))
        finally:
            for fd in loose:
                os.close(fd)

    async def _collect(self, run: _Run, timeout_s: int, limits: Limits) -> RunResult:
        if sys.platform != "linux":  # never: run() returns first; the guard is for mypy
            raise RuntimeError("native isolation needs Linux")
        assert run.proc is not None and run.stdout is not None and run.stderr is not None
        cap = 2 * limits.max_output_bytes
        out_buf, err_buf = bytearray(), bytearray()
        overflow = asyncio.Event()
        done = asyncio.gather(
            _collect_stream(run.stdout, out_buf, cap, overflow),
            _collect_stream(run.stderr, err_buf, cap, overflow),
            run.proc.wait(),
        )
        stopper = asyncio.create_task(self._stop_on(overflow, run))
        timed_out = False
        try:
            # The helper exits only once init and every process in the sandbox are gone.
            await asyncio.wait_for(done, timeout=timeout_s + _STARTUP_GRACE_S)
            if run.status_task is not None:  # EOF follows the helper's exit at once
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(asyncio.shield(run.status_task), _STOP_WAIT_S)
        except asyncio.TimeoutError:
            timed_out = True
            log.warning("run %s: timeout after %ds; stopping the sandbox", run.run_id, timeout_s)
            _stop(run)
        except BaseException:  # cancelled request, or a read error: stop it, the finisher reaps
            _stop(run)
            raise
        finally:
            stopper.cancel()
            done.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await done

        lines = _parse_status(bytes(run.status))
        isolation, detail = _verdict(lines, uid=os.getuid(), gid=os.getgid(),
                                     host_ns=_host_namespaces(), host_proc=_host_proc(),
                                     limits=limits)
        exit_code, note = _outcome(lines, timed_out=timed_out,
                                   truncated=len(out_buf) >= cap or len(err_buf) >= cap)
        log.info("run %s: done exit=%s timed_out=%s isolation=%s",
                 run.run_id, exit_code, timed_out, isolation)
        return RunResult(stdout=out_buf.decode(errors="replace"),
                         stderr=err_buf.decode(errors="replace") + note,
                         exit_code=exit_code, timed_out=timed_out,
                         isolation=isolation, isolation_detail=detail)

    @staticmethod
    async def _stop_on(overflow: asyncio.Event, run: _Run) -> None:
        await overflow.wait()
        log.warning("run %s: output cap exceeded; stopping the sandbox", run.run_id)
        _stop(run)

    async def _finish(self, run: _Run, sem: asyncio.Semaphore) -> None:
        """Detached post-run task: the sandbox is gone when the helper is (S3-D7)."""
        try:
            if run.proc is not None:
                if run.proc.returncode is None:
                    _stop(run)
                    try:
                        await asyncio.wait_for(run.proc.wait(), _STOP_WAIT_S)
                    except asyncio.TimeoutError:
                        log.error("run %s: the helper did not stop; killing it", run.run_id)
                        run.proc.kill()  # PDEATHSIG then takes init and the namespace
                        await run.proc.wait()
                log.info("run %s: sandbox gone (helper exit %s)", run.run_id, run.proc.returncode)
            if run.status_task is not None:  # EOF now: the helper was its only writer
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(run.status_task, _STOP_WAIT_S)
        except Exception:  # detached task: never propagate
            log.exception("run %s: cleanup failed", run.run_id)
        finally:
            for transport in run.transports:
                transport.close()
            sem.release()

    async def drain(self, timeout: float | None = None) -> bool:
        """Wait for the detached finishers, never cancelling them; True when none is left.

        `asyncio.wait`, not `gather`, as in ContainerBackend.drain (S7-D1). It loops because a
        run that ends during the drain adds a finisher.
        """
        loop = asyncio.get_running_loop()
        deadline = None if timeout is None else loop.time() + timeout
        while True:
            tasks = [t for t in self._finishers if not t.done()]
            if not tasks:
                return True
            remaining = None if deadline is None else deadline - loop.time()
            if remaining is not None and remaining <= 0:
                return False
            await asyncio.wait(tasks, timeout=remaining)


def _stop(run: _Run) -> None:
    """SIGTERM to the helper, whose handler SIGKILLs init (S3-D7). Idempotent."""
    if run.proc is not None and run.proc.returncode is None:
        with contextlib.suppress(ProcessLookupError):
            run.proc.send_signal(signal.SIGTERM)


async def _collect_stream(stream: asyncio.StreamReader, buf: bytearray, cap: int,
                          overflow: asyncio.Event) -> None:
    """Read to EOF, keeping at most `cap` bytes; signal overflow, keep draining (as M1)."""
    while chunk := await stream.read(65536):
        take = cap - len(buf)
        if take > 0:
            buf.extend(chunk[:take])
        if len(buf) >= cap:
            overflow.set()


async def _drain_status(stream: asyncio.StreamReader, buf: bytearray) -> None:
    """Read the status pipe to EOF, keeping one byte past the cap so an overrun shows."""
    while chunk := await stream.read(65536):
        room = _STATUS_CAP + 1 - len(buf)
        if room > 0:
            buf.extend(chunk[:room])


def _host_namespaces() -> dict[str, int]:
    return {ns: os.stat(f"/proc/self/ns/{ns}").st_ino for ns in _NAMESPACES}


def _host_proc() -> dict[str, bool]:
    """Which masks and read-only paths this kernel has, and whether each is a directory. The
    sandbox's fresh /proc comes from the same kernel, so these must all be there (S3-D8)."""
    return {name: os.path.isdir(f"/proc/{name}") for name in MASKED + READ_ONLY
            if os.path.exists(f"/proc/{name}")}


def _not_run(why: str) -> RunResult:
    return RunResult(stdout="", stderr="", exit_code=-1, timed_out=False, isolation="none",
                     isolation_detail=f"{why}; nothing was executed")


# --- pure functions: every platform ------------------------------------------------------

def _guest_env(image_env: list[str]) -> dict[str, str]:
    """The image config's Env, then the profile's five, which win on a clash (N9)."""
    env = {key: value for key, sep, value in (e.partition("=") for e in image_env) if sep}
    env.update(_PROFILE_ENV)
    return env


def _parse_status(data: bytes) -> list[dict[str, Any]]:
    """The status lines, in order. Past the cap, or without a complete line, nothing counts."""
    if len(data) > _STATUS_CAP:
        return []
    lines = []
    for raw in data.split(b"\n")[:-1]:
        try:
            value = json.loads(raw)
        except ValueError:
            continue
        if isinstance(value, dict):
            lines.append(value)
    return lines


def _outcome(lines: list[dict[str, Any]], *, timed_out: bool,
             truncated: bool) -> tuple[int, str]:
    """The exit code and any note for stderr, with M1's conventions (S3-D10). A guest killed by
    signal S reports 128+S, as Docker does."""
    if timed_out:
        return -1, ""
    if truncated:  # as M1's CLI reports after its kill
        return 137, "\n[output exceeded collection cap; the sandbox was terminated]"
    for line in lines:
        end = line.get("exit")
        if isinstance(end, dict):
            if type(end.get("code")) is int:
                return int(end["code"]), ""
            if type(end.get("signal")) is int:
                return 128 + int(end["signal"]), ""
    if any("ready" in line for line in lines):  # the guest started; its end is unknown
        return -1, "\n[the sandbox did not report an exit status]"
    return -1, ""


def _verdict(lines: list[dict[str, Any]], *, uid: int, gid: int, host_ns: dict[str, int],
             host_proc: dict[str, bool], limits: Limits) -> tuple[str, str | None]:
    """What this run may claim (S3-D8): `native` only when every control is confirmed."""
    first = next((line for line in lines if "ready" in line or "error" in line), None)
    if first is None:
        return "none", "the sandbox sent no status report; no isolation is claimed for this run"
    if "error" in first:  # sent only before the guest exists
        return "none", (f"native sandbox setup failed at {first.get('error')}: "
                        f"{first.get('message')}; nothing was executed")
    ready = first["ready"] if isinstance(first["ready"], dict) else {}
    checks = ready.get("checks")
    if not isinstance(checks, dict):
        checks = {}
    unconfirmed = [name for name in _HARD_CHECKS if checks.get(name) is not True]
    inodes = ready.get("namespaces")
    if not isinstance(inodes, dict):
        inodes = {}
    if not all(type(inodes.get(ns)) is int and inodes.get(ns) != host_ns.get(ns)
               for ns in _NAMESPACES):
        unconfirmed.append("namespaces")
    if not (str(ready.get("uid_map")).split() == [str(uid), str(uid), "1"]
            and str(ready.get("gid_map")).split() == [str(gid), str(gid), "1"]
            and str(ready.get("setgroups")).strip() == "deny"):
        unconfirmed.append("identity map")
    mounts = _mount_table(ready.get("mountinfo"))
    problem = "unreadable" if mounts is None else _mount_problem(mounts, limits, host_proc)
    if problem:
        unconfirmed.append(f"mount table ({problem})")
    if unconfirmed:
        return "none", "hard controls not confirmed: " + ", ".join(unconfirmed)
    not_applied = list(_CGROUP_LIMITS)
    if mounts is not None and not _tmpfs_sizes_ok(mounts, limits):
        not_applied.append("tmpfs-size")
    return "native-degraded", "limits not applied: " + ", ".join(sorted(not_applied))


_Mount = tuple[str, str, frozenset[str], str]  # mount point, fstype, options, super options


def _mount_table(mountinfo: object) -> list[_Mount] | None:
    """/proc/self/mountinfo as rows, or None if it can't be read."""
    if not isinstance(mountinfo, str):
        return None
    rows = []
    for line in mountinfo.splitlines():
        fields = line.split()
        if "-" not in fields[6:]:
            return None
        sep = fields.index("-", 6)
        if len(fields) < sep + 4:
            return None
        rows.append((fields[4], fields[sep + 1], frozenset(fields[5].split(",")),
                     fields[sep + 3]))
    return rows


_AS_DEV_NULL = "<the fstype of the sandbox's /dev/null>"  # a file mask is a bind of it


def _expected_mounts(limits: Limits,
                     host_proc: dict[str, bool]) -> list[tuple[str, str | None, frozenset[str]]]:
    """§3's table: (mount point, fstype or None for the host's, required options). A /proc mask
    or read-only path is expected exactly when this kernel has it (S3-D8)."""
    sealed = frozenset({"ro", "nosuid", "nodev"})
    plain = frozenset({"nosuid", "nodev"})
    rows: list[tuple[str, str | None, frozenset[str]]] = [
        ("/", None, sealed),
        ("/proc", "proc", plain | {"noexec"}),
        ("/dev", "tmpfs", frozenset({"nosuid", "noexec"})),
    ]
    rows += [(f"/dev/{name}", None, frozenset()) for name in _DEVICES]
    rows += [
        ("/dev/pts", "devpts", frozenset({"nosuid", "noexec"})),
        ("/dev/shm", "tmpfs", plain),
        ("/tmp", "tmpfs", plain),
        ("/workspace", None, sealed if limits.workspace_ro else plain),
        ("/sandbox", "tmpfs", sealed),
    ]
    rows += [(f"/proc/{name}", "tmpfs", frozenset({"ro"})) if host_proc[name]
             else (f"/proc/{name}", _AS_DEV_NULL, frozenset())
             for name in MASKED if name in host_proc]
    rows += [(f"/proc/{name}", "proc", sealed | {"noexec"})
             for name in READ_ONLY if name in host_proc]
    return rows


def _mount_problem(mounts: list[_Mount], limits: Limits,
                   host_proc: dict[str, bool]) -> str | None:
    """None if the mounts are §3's, in order, each with at least its required options. Extra
    options are fine: atime flags, and a source mount's carried ro or noexec (S3-D6)."""
    expected = _expected_mounts(limits, host_proc)
    dev_null = next((fstype for point, fstype, _, _ in mounts if point == "/dev/null"), None)
    for i, (point, fstype, required) in enumerate(expected):
        if i >= len(mounts):
            return f"{point} is missing"
        got_point, got_type, options, _ = mounts[i]
        if got_point != point:
            if got_point in {p for p, _, _ in expected[i:]}:  # out of order, or one skipped
                return f"{point} is missing"
            return f"unexpected mount at {got_point}"
        want = dev_null if fstype == _AS_DEV_NULL else fstype
        if want is not None and got_type != want:
            return f"{point} is {got_type}, not {want}"
        if not required <= options:
            return f"{point} lacks {','.join(sorted(required - options))}"
    if len(mounts) > len(expected):
        return f"unexpected mount at {mounts[len(expected)][0]}"
    return None


def _tmpfs_sizes_ok(mounts: list[_Mount], limits: Limits) -> bool:
    """The soft control `tmpfs-size`, from the kernel's own super options."""
    want = {"/tmp": f"size={limits.tmpfs_mb * 1024}k", "/dev/shm": f"size={_SHM_KB}k"}
    have = {point: set(supers.split(",")) for point, _, _, supers in mounts}
    return all(option in have.get(point, set()) for point, option in want.items())
