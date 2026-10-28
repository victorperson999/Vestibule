"""M2 native backend: each run's systemd scope and the guest's cgroup limits (step 4,
docs/plans/M2/M2-step4-cgroups.md).

Between spawning the paused helper and releasing it, the server asks the user's systemd
manager for a transient scope holding the helper (S4-D3), builds the layout under it and writes
the guest's limits, reading each one back (S4-D5). The scope's own properties carry the
helper's envelope and an outside deadline (S4-D6). Any failure here degrades the run and never
blocks it: limits are the soft tier (N5).
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import stat
import sys
from dataclasses import dataclass

from vestibule.config import Limits

log = logging.getLogger("vestibule.native")

CGROUP_FS = "/sys/fs/cgroup"
_PLACE_WAIT_S = 5  # the whole placement, from busctl's spawn to the last readback (S4-D3)
# The helper's allowance on top of the guest's limits, as the scope's envelope (N3, step 0).
_ENVELOPE_MEM_MB = 64
_ENVELOPE_TASKS = 16
_ENVELOPE_CPU_PCT = 25


@dataclass(frozen=True)
class Placement:
    """A placed run: init's cgroup as /proc/<pid>/cgroup names it, and the guest's limits that
    read back exactly (S4-D5)."""

    sandbox: str
    applied: frozenset[str]


def bus_env() -> dict[str, str] | None:
    """The environment busctl gets, built from scratch, never the server's own (S4-D4); None if
    there is no user bus to find."""
    if sys.platform != "linux":
        return None
    env = {k: os.environ[k] for k in ("DBUS_SESSION_BUS_ADDRESS", "XDG_RUNTIME_DIR")
           if os.environ.get(k)}
    if not env:  # the MCP SDK's minimal environment: derive it, if the socket is ours
        runtime = f"/run/user/{os.getuid()}"
        try:
            st = os.lstat(f"{runtime}/bus")
        except OSError:
            return None
        if not stat.S_ISSOCK(st.st_mode) or st.st_uid != os.getuid():
            return None
        env["XDG_RUNTIME_DIR"] = runtime
    env["PATH"] = os.environ.get("PATH") or os.defpath
    return env


async def place(run_id: str, pid: int, runtime_max_s: int, limits: Limits) -> Placement | str:
    """Put the paused helper `pid` in its own scope, then build the layout and the guest's
    limits. If any of it fails or outlasts _PLACE_WAIT_S: the reason, short, for the run's
    detail (M2 step 6, S6-D7); the long form goes to the log."""
    if not os.path.exists(f"{CGROUP_FS}/cgroup.controllers"):  # cgroup v1, or v2 elsewhere
        log.warning("run %s: no cgroup v2 at %s; limits not applied", run_id, CGROUP_FS)
        return "no cgroup v2"
    env = bus_env()
    if env is None:
        log.warning("run %s: no user bus; limits not applied", run_id)
        return "no systemd user bus"
    unit = f"vestibule-{run_id}.scope"
    busctl = None
    try:
        # One bound around all of it: busctl's own --timeout doesn't cover connecting (§1).
        async with asyncio.timeout(_PLACE_WAIT_S):
            busctl = await asyncio.create_subprocess_exec(
                *_start_unit(unit, pid, runtime_max_s, limits), env=env,
                stdin=asyncio.subprocess.DEVNULL,  # the server's stdin is the MCP channel
                stdout=asyncio.subprocess.DEVNULL,  # golden rule 1: busctl prints the job there
                stderr=asyncio.subprocess.PIPE)
            _, err = await busctl.communicate()
            if busctl.returncode != 0:
                text = err.decode(errors="replace").strip()
                log.warning("run %s: busctl failed (%s); limits not applied", run_id, text)
                # an inherited XDG_RUNTIME_DIR with no bus behind it (WSLg sets one) fails here
                return ("no systemd user bus" if "Failed to connect" in text
                        else "systemd refused the scope")
            scope = await _in_scope(pid, unit)
            if scope is None:
                log.warning("run %s: the helper was gone before placement", run_id)
                return "the helper was gone before placement"
            # A thread: moving a process between cgroups takes a kernel-wide lock (S4-D5).
            return await asyncio.to_thread(_layout, run_id, scope, pid, limits)
    except TimeoutError:
        log.warning("run %s: placement took over %d s; limits not applied", run_id,
                    _PLACE_WAIT_S)
        return f"systemd did not place the run within {_PLACE_WAIT_S} s"
    except OSError as e:  # busctl missing from PATH, among others
        log.warning("run %s: placement failed (%s); limits not applied", run_id, e)
        return "busctl not found" if isinstance(e, FileNotFoundError) else "placement failed"
    finally:
        if busctl is not None and busctl.returncode is None:  # past the bound, or cancelled
            with contextlib.suppress(ProcessLookupError):
                busctl.kill()
            await busctl.wait()


def _start_unit(unit: str, pid: int, runtime_max_s: int, limits: Limits) -> list[str]:
    """busctl's StartTransientUnit call, with S4-D6's properties."""
    props = [
        ("PIDs", "au", "1", str(pid)),
        ("Delegate", "b", "true"),
        ("CollectMode", "s", "inactive-or-failed"),  # a scope that ends badly is removed too
        ("RuntimeMaxUSec", "t", str(runtime_max_s * 1_000_000)),
        ("KillSignal", "i", "9"),  # SIGKILL: init ignores SIGTERM
        ("MemoryMax", "t", str((limits.mem_mb + _ENVELOPE_MEM_MB) << 20)),
        ("TasksMax", "t", str(limits.pids_max + _ENVELOPE_TASKS)),
        ("CPUQuotaPerSecUSec", "t", str((limits.cpu_pct + _ENVELOPE_CPU_PCT) * 10_000)),
    ]
    return ["busctl", "--user", "call", "org.freedesktop.systemd1", "/org/freedesktop/systemd1",
            "org.freedesktop.systemd1.Manager", "StartTransientUnit", "ssa(sv)a(sa(sv))",
            unit, "fail", str(len(props)), *(part for prop in props for part in prop), "0"]


async def _in_scope(pid: int, unit: str) -> str | None:
    """The helper's cgroup once systemd has moved it into `unit`: StartTransientUnit returns
    when the job is queued, not done. None if the helper is gone."""
    while True:
        try:
            with open(f"/proc/{pid}/cgroup") as f:
                cgroup = next((line[3:] for line in f.read().splitlines()
                               if line.startswith("0::")), "")
        except OSError:
            return None
        if cgroup.endswith(f"/{unit}"):
            return cgroup
        await asyncio.sleep(0.001)


def _layout(run_id: str, scope: str, pid: int, limits: Limits) -> Placement | str:
    """S4-D5: supervisor/ with the helper, guest/sandbox/ for init, then each limit on guest/,
    one controller at a time, counted only if every file reads back what was written."""
    root = CGROUP_FS + scope
    try:
        os.mkdir(f"{root}/supervisor")
        _write(f"{root}/supervisor/cgroup.procs", str(pid))  # the scope must hold no process
        os.makedirs(f"{root}/guest/sandbox")
    except OSError as e:
        log.warning("run %s: cgroup layout failed (%s); limits not applied", run_id, e)
        return "cgroup layout failed"
    wanted = {  # M1's values and meanings
        "cpu": {"cpu.max": f"{limits.cpu_pct * 1000} 100000"},  # what Docker writes for --cpus
        "memory": {"memory.max": str(limits.mem_mb << 20), "memory.swap.max": "0"},
        "pids": {"pids.max": str(limits.pids_max)},
    }
    applied = set()
    for name, files in wanted.items():
        try:
            _write(f"{root}/cgroup.subtree_control", f"+{name}")
            for file, value in files.items():
                _write(f"{root}/guest/{file}", value)
            if all(_read(f"{root}/guest/{file}") == value for file, value in files.items()):
                applied.add(name)
            else:
                log.warning("run %s: the %s limit read back differently; not applied",
                            run_id, name)
        except OSError as e:
            log.warning("run %s: the %s limit not applied (%s)", run_id, name, e)
    return Placement(f"{scope}/guest/sandbox", frozenset(applied))


def _write(path: str, text: str) -> None:
    fd = os.open(path, os.O_WRONLY)  # no O_CREAT: a missing cgroup file is a failure
    try:
        os.write(fd, text.encode())
    finally:
        os.close(fd)


def _read(path: str) -> str:
    with open(path) as f:
        return f.read().strip()
