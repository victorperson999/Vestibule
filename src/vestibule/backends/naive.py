"""Milestone 0 backend: subprocess, NO isolation. Plumbing only — never ship as default.

Writes the code to a temp workspace file and runs the interpreter against it with a
wall-clock timeout. Cross-platform (bash may be unavailable on bare Windows -> clean error).
"""
from __future__ import annotations

import asyncio
import shutil
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


class NaiveBackend(Warden):
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
            )
        except FileNotFoundError as e:
            return RunResult(stdout="", stderr=str(e), exit_code=127,
                             timed_out=False, isolation="none")

        timed_out = False
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
        except asyncio.TimeoutError:
            timed_out = True
            proc.kill()
            out, err = await proc.communicate()

        return RunResult(
            stdout=out.decode(errors="replace"),
            stderr=err.decode(errors="replace"),
            exit_code=proc.returncode if proc.returncode is not None else -1,
            timed_out=timed_out,
            isolation="none",   # M0 is explicitly unsafe; be honest
        )
