"""M1 step 7a (plan §5.2): the real `vestibule-mcp` process over stdio, driven the way an
MCP client drives it — the venv's console script, raw JSON-RPC on stdin/stdout.

What only this level can prove:
- golden rule 1 over a real pipe: every stdout line is a JSON-RPC 2.0 message;
- shutdown: closing stdin (what a client does first) makes the server exit on its own
  inside the client's 2 s grace — the tests kill it at 2 s exactly as the MCP client
  SDK does (S7-D1) — and a run still in flight has its container removed BEFORE the
  process exits.

Containers are identified by the server's own `run <id>: spawning` stderr lines, never by
label, so a live Vestibule server elsewhere on this machine is never confused with this
one; cleanup removes only those exact names.
"""
import asyncio
import json
import os
import re
import sys
import time
from pathlib import Path

import pytest
from dockerutil import docker_required, ps_names, remove
from mcp.types import LATEST_PROTOCOL_VERSION

pytestmark = pytest.mark.docker

SERVER_EXE = Path(sys.executable).parent / (
    "vestibule-mcp.exe" if os.name == "nt" else "vestibule-mcp"
)
# Real clients close stdin, wait 2 s, then kill the process tree (S7-D1; the MCP
# Python SDK's PROCESS_TERMINATION_TIMEOUT). The tests behave the same way, so a
# server that only cleans up after 2 s fails here exactly as it would in real use.
# Measured 2026-10-11: shutdown takes ~0.2-0.4 s, with or without a run in flight.
CLIENT_GRACE_S = 2.0
_SPAWN = re.compile(r"run ([0-9a-f]{16}): spawning")
_ISOLATION_CONTAINER = re.compile(r"^isolation: container$", re.M)


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
            except asyncio.TimeoutError:
                pass
        return self.run_ids[seen]

    async def close_like_a_client(self) -> tuple[int | None, float]:
        """Shut down the way an MCP client does: close stdin, and if the server has not
        exited after CLIENT_GRACE_S, kill it. Returns (exit code, seconds to exit), or
        (None, elapsed) if it had to be killed."""
        assert self.proc.stdin is not None
        t0 = time.monotonic()
        self.proc.stdin.close()
        try:
            await asyncio.wait_for(self.proc.wait(), CLIENT_GRACE_S)
        except asyncio.TimeoutError:
            self.proc.kill()
            await self.proc.wait()
            return None, time.monotonic() - t0
        return self.proc.returncode, time.monotonic() - t0

    def stderr_tail(self, n: int = 25) -> str:
        return "\n".join(self.stderr_lines[-n:])

    async def kill(self) -> None:
        if self.proc.returncode is None:
            self.proc.kill()
            await self.proc.wait()
        for t in self._readers:
            t.cancel()
        await asyncio.gather(*self._readers, return_exceptions=True)


def _container_gone(name: str, within_s: float) -> bool:
    deadline = time.monotonic() + within_s
    while True:
        if ps_names(f"name=^{name}$") == "":
            return True
        if time.monotonic() > deadline:
            return False
        time.sleep(0.25)


@pytest.fixture()
async def server(tmp_path):
    srv = await StdioServer.start(tmp_path / "ws")
    try:
        yield srv
    finally:
        await srv.kill()
        for rid in srv.run_ids:  # exact names only — never label-wide
            remove(f"vestibule-{rid}")


@docker_required
async def test_stdio_happy_path_and_clean_exit(server):
    await server.initialize()
    tools = await server.request("tools/list", {})
    assert sorted(t["name"] for t in tools["tools"]) == ["read_workspace", "run_code"]

    for language, code, marker in (
        ("python", "print('hi-py')", "hi-py"),
        ("bash", "echo hi-bash", "hi-bash"),
        ("node", "console.log('hi-node')", "hi-node"),
    ):
        text = await server.call("run_code", {"language": language, "code": code})
        assert marker in text, text
        # Whole line: "isolation: container" is also a prefix of container-degraded.
        assert _ISOLATION_CONTAINER.search(text), text

    await server.call("run_code", {
        "language": "python", "code": "open('/workspace/e2e.txt', 'w').write('round-trip')",
    })
    assert "round-trip" in await server.call("read_workspace", {"path": "e2e.txt"})

    code, elapsed = await server.close_like_a_client()
    assert code == 0, f"exit={code} (None = killed at the 2 s grace) after {elapsed:.2f}s\n{server.stderr_tail()}"
    assert server.bad_stdout == []  # golden rule 1


@docker_required
async def test_stdio_shutdown_with_run_in_flight_removes_its_container(server):
    await server.initialize()
    await server.call("run_code", {"language": "python", "code": "print('warm')"})

    seen = len(server.run_ids)
    await server.send_request("tools/call", {"name": "run_code", "arguments": {
        "language": "python", "code": "import time; time.sleep(3600)", "timeout_seconds": 60,
    }})
    name = f"vestibule-{await server.wait_for_new_spawn(seen)}"
    deadline = time.monotonic() + 60
    while ps_names(f"name=^{name}$", all_states=False) != name:
        assert time.monotonic() < deadline, "the in-flight container never started"
        await asyncio.sleep(0.25)

    code, elapsed = await server.close_like_a_client()
    assert code == 0, f"exit={code} (None = killed at the 2 s grace) after {elapsed:.2f}s\n{server.stderr_tail()}"
    # Removed by the server before it exited — not by --rm at the guest's natural end
    # (the guest sleeps for an hour), so a short poll for daemon bookkeeping is all.
    assert _container_gone(name, within_s=2), f"{name} outlived the server"
    assert any("shutdown: removing 1 container(s)" in ln for ln in server.stderr_lines)
    assert server.bad_stdout == []


@docker_required
async def test_stdio_shutdown_during_first_probe(server):
    """The first call's selection probe is in flight when the client goes away: the
    SDK cancels the handler through its anyio scope mid-selection (S7-D1, skeptic
    finding 1). What this proves is the process side: a prompt, clean exit with the
    selection cut off mid-probe. It cannot prove container removal — the probe guest
    ends by itself within a second and --rm removes it either way; removal of an
    in-flight container is proven by the run-in-flight test above."""
    await server.initialize()
    await server.send_request("tools/call", {"name": "run_code", "arguments": {
        "language": "python", "code": "print('never mind')",
    }})
    probe = f"vestibule-{await server.wait_for_new_spawn(0)}"

    code, elapsed = await server.close_like_a_client()
    assert code == 0, f"exit={code} (None = killed at the 2 s grace) after {elapsed:.2f}s\n{server.stderr_tail()}"
    # The shutdown really did land mid-probe: selection never reached a verdict.
    assert not any("selection: container" in ln for ln in server.stderr_lines), (
        server.stderr_tail()
    )
    assert _container_gone(probe, within_s=10)  # sanity only (see docstring)
    assert server.bad_stdout == []
