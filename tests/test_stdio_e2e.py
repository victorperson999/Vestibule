"""The real `vestibule-mcp` process over stdio, driven the way MCP clients drive it: the
venv's console script, raw JSON-RPC on stdin/stdout (M1 step 7a, plan §5.2; extended in
M2 step 1b for the POSIX shutdown hand-off, docs/plans/M2/M2-step1-linux-baseline.md §4.2).

What only this level can prove:
- golden rule 1 over a real pipe: every stdout line is a JSON-RPC 2.0 message;
- shutdown, with each client's real sequence (tests/stdioutil.py): the server exits in the
  client's FIRST phase, before any escalation, and a run in flight has its container
  removed.
  - Windows: by the server itself before it exits (S7-D1).
  - POSIX: by the detached cleaner the server hands it to (S1-D5). The cleaner's trace
    (VESTIBULE_CLEANUP_TRACE) proves it was the cleaner, and the guest ignores SIGTERM, so
    `docker run`'s signal proxy and `--rm` cannot remove it on the server's behalf.

Containers are identified by the server's own `run <id>: spawning` stderr lines, never by
label, so a live Vestibule server elsewhere on this machine is never confused with this
one; cleanup removes only those exact names.
"""
import asyncio
import re
import sys
import time
from pathlib import Path

import pytest
from dockerutil import docker_required, ps_names, remove
from stdioutil import (
    CLIENT_GRACE_S,
    SERVER_EXE,
    SIGINT,
    STDIN,
    StdioServer,
    cleaner_removed,
    read_trace,
)

pytestmark = pytest.mark.docker

POSIX = sys.platform != "win32"
_ISOLATION_CONTAINER = re.compile(r"^isolation: container$", re.MULTILINE)
# The cleaner's first pass runs within a second; its later sweeps at +5 s and +20 s.
_CLEANER_WINDOW_S = 30
# A guest only a forced removal can end: SIGTERM is ignored, and it sleeps for an hour.
_STUBBORN_GUEST = (
    "import signal, time\n"
    "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
    "print('ready', flush=True)\n"
    "time.sleep(3600)\n"
)


def _container_gone(name: str, within_s: float) -> bool:
    deadline = time.monotonic() + within_s
    while True:
        if ps_names(f"name=^{name}$") == "":
            return True
        if time.monotonic() > deadline:
            return False
        time.sleep(0.25)


async def _wait_running(name: str, within_s: float = 60) -> None:
    deadline = time.monotonic() + within_s
    while ps_names(f"name=^{name}$", all_states=False) != name:
        assert time.monotonic() < deadline, f"{name} never started"
        await asyncio.sleep(0.25)


@pytest.fixture()
def trace_file(tmp_path: Path) -> Path:
    return tmp_path / "cleanup-trace.jsonl"


@pytest.fixture()
async def server(tmp_path, trace_file):
    srv = await StdioServer.start(
        tmp_path / "ws", {"VESTIBULE_CLEANUP_TRACE": str(trace_file)}
    )
    try:
        yield srv
    finally:
        await srv.kill()
        for rid in srv.run_ids:  # exact names only — never label-wide
            remove(f"vestibule-{rid}")


async def _start_stubborn_run(server: StdioServer) -> str:
    """A warm-up call (selection), then a run that stays in flight; its container name."""
    await server.call("run_code", {"language": "python", "code": "print('warm')"})
    seen = len(server.run_ids)
    await server.send_request("tools/call", {"name": "run_code", "arguments": {
        "language": "python", "code": _STUBBORN_GUEST, "timeout_seconds": 60,
    }})
    name = f"vestibule-{await server.wait_for_new_spawn(seen)}"
    await _wait_running(name)
    return name


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

    code, elapsed, phase = await server.close_like_sdk()
    await server.finish_reading()
    assert (code, phase) == (0, STDIN), (
        f"exit={code} phase={phase} after {elapsed:.2f}s\n{server.stderr_tail()}"
    )
    if POSIX:  # the stdin wrapper, not the SDK, saw the EOF first
        assert any("vestibule shutdown (stdin closed):" in ln for ln in server.stderr_lines)
    assert server.bad_stdout == []  # golden rule 1


@docker_required
async def test_stdio_shutdown_with_run_in_flight_removes_its_container(server, trace_file):
    await server.initialize()
    name = await _start_stubborn_run(server)

    code, elapsed, phase = await server.close_like_sdk()
    await server.finish_reading()
    assert (code, phase) == (0, STDIN), (
        f"exit={code} phase={phase} after {elapsed:.2f}s\n{server.stderr_tail()}"
    )
    if POSIX:
        assert _container_gone(name, within_s=_CLEANER_WINDOW_S), f"{name} outlived the cleaner"
        assert cleaner_removed(read_trace(trace_file), name), read_trace(trace_file)
        # The trigger label proves the stdin wrapper caught EOF before the SDK did (a
        # bypassed wrapper would log "server stopped", after the SDK cancelled the run).
        assert any("shutdown (stdin closed): handed 1 container(s)" in ln
                   for ln in server.stderr_lines), server.stderr_tail()
    else:
        # Removed by the server before it exited, not by --rm at the guest's natural end
        # (the guest sleeps for an hour), so a short poll for daemon bookkeeping is all.
        assert _container_gone(name, within_s=2), f"{name} outlived the server"
        assert any("shutdown: removing 1 container(s)" in ln for ln in server.stderr_lines)
    assert server.bad_stdout == []


@docker_required
async def test_stdio_shutdown_during_first_probe(server, trace_file):
    """The first call's selection probe is in flight when the client goes away (S7-D1,
    skeptic finding 1). What this proves is the process side: a prompt, clean exit with the
    selection cut off mid-probe. It cannot prove container removal: the probe guest ends by
    itself within a second and --rm removes it either way. Removal of an in-flight container
    is proven by the run-in-flight tests."""
    await server.initialize()
    await server.send_request("tools/call", {"name": "run_code", "arguments": {
        "language": "python", "code": "print('never mind')",
    }})
    probe = f"vestibule-{await server.wait_for_new_spawn(0)}"

    code, elapsed, phase = await server.close_like_sdk()
    await server.finish_reading()
    assert (code, phase) == (0, STDIN), (
        f"exit={code} phase={phase} after {elapsed:.2f}s\n{server.stderr_tail()}"
    )
    if POSIX:  # the stdin wrapper caught EOF before the SDK could cancel the probe
        assert any("vestibule shutdown (stdin closed):" in ln for ln in server.stderr_lines)
    # The shutdown really did land mid-probe: selection never reached a verdict.
    assert not any("selection: container" in ln for ln in server.stderr_lines), (
        server.stderr_tail()
    )
    assert _container_gone(probe, within_s=10 if not POSIX else _CLEANER_WINDOW_S)
    assert server.bad_stdout == []


@docker_required
@pytest.mark.skipif(not POSIX, reason="POSIX shutdown hand-off (Windows keeps S7-D1; its "
                    "300 ms Claude Code window is a documented gap in docs/BUILD_LOG.md)")
async def test_stdio_claude_code_sequence_hands_off_a_run_in_flight(server, trace_file):
    """Claude Code on POSIX: SIGINT to the PID with stdin open, then SIGTERM after 100 ms,
    then SIGKILL after 400 ms. Today's server hung on that SIGINT (M2 step 1)."""
    await server.initialize()
    name = await _start_stubborn_run(server)

    code, elapsed, phase = await server.close_like_claude_code()
    await server.finish_reading()
    assert (code, phase) == (0, SIGINT), (
        f"exit={code} phase={phase} after {elapsed:.3f}s\n{server.stderr_tail()}"
    )
    assert _container_gone(name, within_s=_CLEANER_WINDOW_S), f"{name} outlived the cleaner"
    assert cleaner_removed(read_trace(trace_file), name), read_trace(trace_file)
    assert any("shutdown (SIGINT): handed 1 container(s)" in ln for ln in server.stderr_lines)
    assert server.bad_stdout == []


@docker_required
@pytest.mark.skipif(not POSIX, reason="POSIX shutdown hand-off (the Windows path is S7-D1)")
async def test_stdio_real_sdk_client_hands_off_a_run_in_flight(tmp_path, trace_file):
    """The real MCP Python SDK client, with its own shutdown sequence and its default
    environment for servers (HOME LOGNAME PATH SHELL TERM USER, plus what we pass)."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    errlog_path = tmp_path / "server-stderr.log"
    params = StdioServerParameters(command=str(SERVER_EXE), env={
        "VESTIBULE_WORKSPACE": str(tmp_path / "ws"),
        "VESTIBULE_CLEANUP_TRACE": str(trace_file),
    })

    def spawned() -> list[str]:
        text = errlog_path.read_text(encoding="utf-8", errors="replace")
        return re.findall(r"run ([0-9a-f]{16}): spawning", text)

    name = None
    try:
        async with asyncio.timeout(180):
            with errlog_path.open("w", encoding="utf-8") as errlog:
                async with stdio_client(params, errlog=errlog) as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        await session.call_tool(
                            "run_code", {"language": "python", "code": "print('warm')"}
                        )
                        seen = len(spawned())
                        pending = asyncio.create_task(session.call_tool("run_code", {
                            "language": "python", "code": _STUBBORN_GUEST,
                            "timeout_seconds": 60,
                        }))
                        while len(spawned()) <= seen:
                            await asyncio.sleep(0.1)
                        name = f"vestibule-{spawned()[seen]}"
                        await _wait_running(name)
                        t0 = time.monotonic()
                    # Leaving both contexts is the SDK's own shutdown: close stdin, wait up
                    # to 2 s, then escalate.
                elapsed = time.monotonic() - t0
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        stderr = errlog_path.read_text(encoding="utf-8", errors="replace")
        assert elapsed < CLIENT_GRACE_S, f"needed escalation ({elapsed:.2f}s)\n{stderr[-2000:]}"
        assert "shutdown (stdin closed): handed 1 container(s)" in stderr, stderr[-2000:]
        assert _container_gone(name, within_s=_CLEANER_WINDOW_S), f"{name} outlived the cleaner"
        assert cleaner_removed(read_trace(trace_file), name), read_trace(trace_file)
    finally:
        if name is not None:
            remove(name)
