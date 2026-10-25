"""Milestone 0 backend: subprocess, NO isolation. Plumbing only — never ship as default.

Writes the code to a temp workspace file and runs the interpreter against it with a
wall-clock timeout. Cross-platform (bash may be unavailable on bare Windows -> clean error).
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import signal
import sys
import tempfile
from pathlib import Path

from vestibule.backends.base import RunResult, Warden
from vestibule.config import Limits

_EXT = {"python": ".py", "bash": ".sh", "node": ".js"}


def _command(language: str, script: Path) -> list[str] | None:
    if language == "python":
        return [sys.executable, str(script)]
    if language == "node":
        node = shutil.which("node")
        return [node, str(script)] if node else None
    if language == "bash":
        bash = shutil.which("bash")
        return [bash, str(script)] if bash else None
    return None


def kill_guest(proc: asyncio.subprocess.Process) -> None:
    """Kill a guest and, on POSIX, everything in its process group. Never raises."""
    if sys.platform != "win32":
        try:
            os.killpg(proc.pid, signal.SIGKILL)  # pgid == pid: started in a new session
            return
        except OSError:
            pass  # already gone, or not (yet) a group leader: fall back to the process
    with contextlib.suppress(ProcessLookupError, OSError):
        proc.kill()


class NaiveBackend(Warden):
    def __init__(self) -> None:
        # Guest processes still running, so the POSIX shutdown hand-off can kill them
        # (M2 step 1b): it ends with os._exit, which skips the interpreter teardown that
        # used to kill them.
        self._procs: set[asyncio.subprocess.Process] = set()

    def live_procs(self) -> list[asyncio.subprocess.Process]:
        return [p for p in self._procs if p.returncode is None]

    async def run(self, language: str, code: str, timeout_s: int, limits: Limits) -> RunResult:
        workdir = Path(tempfile.mkdtemp(prefix="vestibule-"))
        script = workdir / f"main{_EXT.get(language, '.txt')}"
        script.write_text(code, encoding="utf-8")

        cmd = _command(language, script)
        if cmd is None:
            return RunResult(
                stdout="", stderr=f"Interpreter for '{language}' not found on PATH.",
                exit_code=127, timed_out=False, isolation="none",
            )

        try:
            # stdin MUST be explicit: the server's own stdin is the MCP JSON-RPC pipe.
            # Letting the guest inherit it both leaks the protocol channel to untrusted
            # code and hangs the child on Windows (overlapped pipe as a std handle).
            proc = await asyncio.create_subprocess_exec(
                *cmd, cwd=str(workdir),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                # POSIX: its own process group, so a kill reaches the guest's children
                # too (a bash guest's `sleep`, say), never this server's group.
                start_new_session=os.name == "posix",
            )
        except FileNotFoundError as e:
            return RunResult(stdout="", stderr=str(e), exit_code=127,
                             timed_out=False, isolation="none")

        timed_out = False
        self._procs.add(proc)
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
        except asyncio.TimeoutError:
            timed_out = True
            kill_guest(proc)
            out, err = await proc.communicate()
        except BaseException:
            # Cancelled (or failed) mid-run: never leave the guest running. The interpreter's
            # normal teardown used to kill it, but POSIX now exits with os._exit (M2 step 1b).
            kill_guest(proc)
            raise
        finally:
            self._procs.discard(proc)

        return RunResult(
            stdout=out.decode(errors="replace"),
            stderr=err.decode(errors="replace"),
            exit_code=proc.returncode if proc.returncode is not None else -1,
            timed_out=timed_out,
            isolation="none",   # M0 is explicitly unsafe; be honest
        )
