"""POSIX shutdown triggers on an idle server, without Docker (M2 step 1b, plan §4.3).

Each way a client ends a stdio server: SIGINT (Claude Code's first step), SIGTERM, SIGHUP,
and stdin EOF (the MCP Python SDK's first step). Before step 1b the server hung on SIGINT
with stdin open. Each trigger must end it with exit code 0 inside the client's FIRST phase,
keep stdout pure JSON-RPC, and start no cleaner (an idle server owns nothing).

Docker-free on purpose: these must run, and pass, on any Linux. The Linux gate fails if
they are skipped there.
"""
import asyncio
import signal
import sys
from pathlib import Path

import pytest
from stdioutil import SIGINT, STDIN, StdioServer, read_trace

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX shutdown path (Windows keeps S7-D1)"
)


@pytest.fixture()
async def idle_server(tmp_path: Path):
    trace = tmp_path / "cleanup-trace.jsonl"
    srv = await StdioServer.start(tmp_path / "ws", {"VESTIBULE_CLEANUP_TRACE": str(trace)})
    try:
        await srv.initialize()
        yield srv, trace
    finally:
        await srv.kill()


def _shutdown_line(srv: StdioServer) -> str:
    return next((ln for ln in srv.stderr_lines if "vestibule shutdown (" in ln), "")


async def test_sigint_like_claude_code(idle_server):
    srv, trace = idle_server
    code, elapsed, phase = await srv.close_like_claude_code()
    await srv.finish_reading()
    assert (code, phase) == (0, SIGINT), (
        f"exit={code} phase={phase} after {elapsed:.3f}s\n{srv.stderr_tail()}"
    )
    assert "shutdown (SIGINT): nothing to clean up" in _shutdown_line(srv), srv.stderr_tail()
    assert srv.bad_stdout == []
    assert read_trace(trace) == []  # no cleaner for an idle server


@pytest.mark.parametrize("sig", ["SIGTERM", "SIGHUP"])
async def test_other_signals_with_stdin_open(idle_server, sig):
    srv, trace = idle_server
    srv.proc.send_signal(getattr(signal, sig))
    # Claude Code would escalate after 100-400 ms; the server must be gone well before.
    code = await asyncio.wait_for(srv.proc.wait(), 0.4)
    await srv.finish_reading()
    assert code == 0, f"exit={code}\n{srv.stderr_tail()}"
    assert f"shutdown ({sig}): nothing to clean up" in _shutdown_line(srv), srv.stderr_tail()
    assert srv.bad_stdout == []
    assert read_trace(trace) == []


async def test_stdin_eof_like_the_sdk(idle_server):
    srv, trace = idle_server
    code, elapsed, phase = await srv.close_like_sdk()
    await srv.finish_reading()
    assert (code, phase) == (0, STDIN), (
        f"exit={code} phase={phase} after {elapsed:.2f}s\n{srv.stderr_tail()}"
    )
    assert elapsed < 1.0, f"{elapsed:.2f}s"
    assert "shutdown (stdin closed): nothing to clean up" in _shutdown_line(srv), srv.stderr_tail()
    assert srv.bad_stdout == []
    assert read_trace(trace) == []
