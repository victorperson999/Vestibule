"""A real `vestibule-mcp` process over stdio, plus the shutdown sequences real clients use.

Shared by tests/test_stdio_e2e.py (Docker) and tests/test_signals.py (no Docker). Moved here
from test_stdio_e2e.py in M2 step 1b; the closers now report WHICH phase of a client's
sequence ended the server, so a test can require that it exited before any escalation.

The client sequences (docs/plans/M2/M2-step1-linux-baseline.md §1.2, verified 2026-10-14):
- MCP Python SDK (1.28.1): close stdin, wait 2 s, then SIGTERM (POSIX; the SDK uses killpg
  on a new session) or a job/tree kill (Windows), then SIGKILL 2 s later.
- Claude Code on POSIX (2.1.300): SIGINT to the PID with stdin left open, 100 ms, SIGTERM,
  400 ms, SIGKILL.
- Claude Code on Windows: close stdin, 300 ms, `taskkill /PID <pid> /T /F`.

These emulations signal the server's PID, never a process group: the server shares this
test process's group, and the hand-off has to happen in the first phase anyway.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

from mcp.types import LATEST_PROTOCOL_VERSION

SERVER_EXE = Path(sys.executable).parent / (
    "vestibule-mcp.exe" if os.name == "nt" else "vestibule-mcp"
)
# The MCP Python SDK's PROCESS_TERMINATION_TIMEOUT (S7-D1).
CLIENT_GRACE_S = 2.0
_SPAWN = re.compile(r"run ([0-9a-f]{16}): spawning")

# Phases a closer reports: the step of the client's sequence that ended the server.
STDIN = "stdin"      # it exited on its own after stdin closed
SIGINT = "SIGINT"    # it exited after Claude Code's first signal
SIGTERM = "SIGTERM"  # it needed the second step
KILLED = "killed"    # the client had to kill it


class StdioServer:
    """A `vestibule-mcp` subprocess plus a minimal JSON-RPC client."""

    def __init__(self, proc: asyncio.subprocess.Process) -> None:
        self.proc = proc
        self.stderr_lines: list[str] = []
        self.bad_stdout: list[str] = []  # anything on stdout that is not JSON-RPC 2.0
        self.run_ids: list[str] = []  # in spawn order (the probe is a run too)
        self._spawned = asyncio.Event()
        self._pending: dict[int, asyncio.Future[dict]] = {}
        self._next_id = 1
        self._readers = [
            asyncio.create_task(self._read_stdout()),
            asyncio.create_task(self._read_stderr()),
        ]

    @classmethod
    async def start(cls, workspace: Path, settings: dict[str, str] | None = None) -> "StdioServer":
        # The dev shell's own VESTIBULE_* settings must not leak into the server under test.
        env = {k: v for k, v in os.environ.items() if not k.startswith("VESTIBULE_")}
        env["VESTIBULE_WORKSPACE"] = str(workspace)
        env.update(settings or {})
        proc = await asyncio.create_subprocess_exec(
            str(SERVER_EXE),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        return cls(proc)

    async def _read_stdout(self) -> None:
        assert self.proc.stdout is not None
        async for raw in self.proc.stdout:
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                self.bad_stdout.append(line)
                continue
            if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
                self.bad_stdout.append(line)
                continue
            fut = self._pending.pop(msg.get("id"), None) if "id" in msg else None
            if fut is not None and not fut.done():
                fut.set_result(msg)

    async def _read_stderr(self) -> None:
        # Drained continuously: a full stderr pipe would otherwise block the server.
        assert self.proc.stderr is not None
        async for raw in self.proc.stderr:
            line = raw.decode("utf-8", errors="replace").rstrip()
            self.stderr_lines.append(line)
            m = _SPAWN.search(line)
            if m:
                self.run_ids.append(m.group(1))
                self._spawned.set()

    async def _send(self, msg: dict) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write((json.dumps(msg) + "\n").encode())
        await self.proc.stdin.drain()

    async def request(self, method: str, params: dict, timeout: float = 90) -> dict:
        fut = await self.send_request(method, params)
        msg = await asyncio.wait_for(fut, timeout)
        assert "error" not in msg, msg
        return msg["result"]

    async def send_request(self, method: str, params: dict) -> "asyncio.Future[dict]":
        rid, self._next_id = self._next_id, self._next_id + 1
        fut: asyncio.Future[dict] = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        await self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        return fut

    async def initialize(self) -> dict:
        result = await self.request("initialize", {
            "protocolVersion": LATEST_PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "vestibule-e2e", "version": "0"},
        })
        await self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return result

    async def call(self, tool: str, arguments: dict, timeout: float = 90) -> str:
        result = await self.request(
            "tools/call", {"name": tool, "arguments": arguments}, timeout
        )
        return "\n".join(c.get("text", "") for c in result["content"])

    async def wait_for_new_spawn(self, seen: int, timeout: float = 60) -> str:
        """The id of the first run spawned after `seen` spawns."""
        deadline = time.monotonic() + timeout
        while len(self.run_ids) <= seen:
            self._spawned.clear()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError("no new run was spawned:\n" + self.stderr_tail())
            try:
                await asyncio.wait_for(self._spawned.wait(), remaining)
            except TimeoutError:
                pass
        return self.run_ids[seen]

    async def _exited_within(self, seconds: float) -> bool:
        try:
            await asyncio.wait_for(self.proc.wait(), seconds)
        except TimeoutError:
            return False
        return True

    async def close_like_sdk(self) -> tuple[int | None, float, str]:
        """The MCP Python SDK's sequence. Returns (exit code, seconds, ending phase)."""
        assert self.proc.stdin is not None
        t0 = time.monotonic()
        self.proc.stdin.close()
        if await self._exited_within(CLIENT_GRACE_S):
            return self.proc.returncode, time.monotonic() - t0, STDIN
        if sys.platform != "win32":
            self.proc.send_signal(signal.SIGTERM)
            if await self._exited_within(CLIENT_GRACE_S):
                return self.proc.returncode, time.monotonic() - t0, SIGTERM
        self.proc.kill()
        await self.proc.wait()
        return self.proc.returncode, time.monotonic() - t0, KILLED

    async def close_like_claude_code(self) -> tuple[int | None, float, str]:
        """Claude Code's sequence for this platform. Returns (exit code, seconds, phase)."""
        t0 = time.monotonic()
        if sys.platform == "win32":
            assert self.proc.stdin is not None
            self.proc.stdin.close()
            if await self._exited_within(0.3):
                return self.proc.returncode, time.monotonic() - t0, STDIN
            subprocess.run(["taskkill", "/PID", str(self.proc.pid), "/T", "/F"],
                           capture_output=True, check=False)
            await self.proc.wait()
            return self.proc.returncode, time.monotonic() - t0, KILLED
        self.proc.send_signal(signal.SIGINT)
        if await self._exited_within(0.1):
            return self.proc.returncode, time.monotonic() - t0, SIGINT
        self.proc.send_signal(signal.SIGTERM)
        if await self._exited_within(0.4):
            return self.proc.returncode, time.monotonic() - t0, SIGTERM
        self.proc.kill()
        await self.proc.wait()
        return self.proc.returncode, time.monotonic() - t0, KILLED

    async def finish_reading(self, timeout: float = 5) -> None:
        """After exit: wait until stdout/stderr hit EOF, so the last lines are in."""
        await asyncio.wait(self._readers, timeout=timeout)

    def stderr_tail(self, n: int = 25) -> str:
        return "\n".join(self.stderr_lines[-n:])

    async def kill(self) -> None:
        if self.proc.returncode is None:
            self.proc.kill()
            await self.proc.wait()
        for t in self._readers:
            t.cancel()
        await asyncio.gather(*self._readers, return_exceptions=True)


def read_trace(path: Path) -> list[dict]:
    """The cleaner's VESTIBULE_CLEANUP_TRACE records (empty if it never ran)."""
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def cleaner_removed(trace: list[dict], name: str) -> bool:
    """True if one of the cleaner's `rm -f` calls actually removed `name`.

    The exit code alone proves nothing: `docker rm -f` exits 0 for a container that is
    already gone. It prints each name it removed on stdout, so require it there."""
    return any(
        r.get("rc") == 0 and "rm" in r.get("call", [])
        and name in r.get("out", "").split()
        for r in trace
    )
