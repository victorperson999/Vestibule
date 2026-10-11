"""Vestibule MCP server (stdio transport).

RULE: never write to stdout except via the MCP SDK. stdout is the JSON-RPC channel;
a stray print() corrupts the protocol. All logging goes to stderr.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import sys

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from vestibule import workspace
from vestibule.backends.base import RunRefusedError, RunResult, Warden
from vestibule.backends.select import BackendSelector
from vestibule.config import ALLOWED_LANGUAGES, Limits

logging.basicConfig(
    stream=sys.stderr,  # NOT stdout
    level=logging.INFO,
    format="%(asctime)s vestibule %(levelname)s %(message)s",
)
log = logging.getLogger("vestibule")

LIMITS = Limits.from_env()
server = Server("vestibule")
SELECTOR = BackendSelector()


async def get_warden() -> Warden:
    # M1 (contract §5): lazily probe & cache the real backend on the first tool
    # call. Naive is never auto-selected — it needs explicit VESTIBULE_BACKEND=naive.
    # Raises RunRefusedError with an actionable message when no honest isolation
    # is possible; the handler renders it as `Blocked:` content.
    return (await SELECTOR.get(LIMITS)).warden


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="run_code",
            description=(
                "Execute code in an isolated sandbox and return its output. "
                "The sandbox has NO network access. The directory /workspace is the "
                "persistent workspace: files written there survive the run, and code "
                "may create, modify, or delete files in it. Everything else is "
                "ephemeral and discarded after the run. It is resource-limited: "
                "code exceeding the memory/CPU/time limits is terminated. "
                "Use this to run and test code safely; do not expect internet access. "
                "Your code runs as /sandbox/main.<ext> with /workspace as the working "
                "directory, so to import your own modules saved in the workspace, add it "
                "to the path first (Python: sys.path.insert(0, '/workspace'); Node: "
                "require('/workspace/<file>')). Read files that runs produce with "
                "read_workspace and change them with run_code, never with other file "
                "tools: the workspace is written by sandboxed code and may contain "
                "symlinks that other tools would follow out of it, for reads and writes."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "language": {
                        "type": "string",
                        "enum": list(ALLOWED_LANGUAGES),
                        "description": "Interpreter to run the code with.",
                    },
                    "code": {"type": "string", "description": "Source code to execute."},
                    "timeout_seconds": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": LIMITS.max_timeout_s,
                        "default": LIMITS.default_timeout_s,
                        "description": (
                            f"Max wall-clock seconds before the run is killed "
                            f"(capped at {LIMITS.max_timeout_s})."
                        ),
                    },
                },
                "required": ["language", "code"],
            },
        ),
        Tool(
            name="read_workspace",
            description=(
                "Read a file or list a directory inside the persistent workspace — the "
                "same directory that run_code mounts at /workspace. Paths are relative "
                "to the workspace root; '.' (the default) lists the root. Paths outside "
                "the workspace are refused, and symlinks are refused rather than "
                "followed. Note: while guest code is running it can modify the "
                "workspace concurrently with this tool."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "default": ".",
                        "description": "Workspace-relative path of a file or directory.",
                    },
                },
                "required": [],
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    try:
        args = arguments or {}
        if name == "run_code":
            return await _handle_run_code(args)
        if name == "read_workspace":
            return await _handle_read_workspace(args)
        return [TextContent(type="text", text=f"Unknown tool: {name}")]
    except Exception as e:  # never let an exception kill the session
        log.exception("tool handler crashed")
        return [TextContent(type="text", text=f"Internal error: {e}")]


def _blocked(msg: str) -> list[TextContent]:
    return [TextContent(type="text", text=f"Blocked: {msg}")]


async def _handle_run_code(args: dict) -> list[TextContent]:
    # Validation is total (M1 finding 18): every argument is type-checked here, in
    # the clean server process, before any subprocess can exist. Out-of-type input
    # gets a legible Blocked message, never an exception.
    language = args.get("language")
    code = args.get("code")
    timeout_raw = args.get("timeout_seconds", LIMITS.default_timeout_s)

    if not isinstance(language, str) or language not in ALLOWED_LANGUAGES:
        return _blocked(f"language must be one of {ALLOWED_LANGUAGES}.")
    if not isinstance(code, str) or not code.strip():
        return _blocked("'code' must be a non-empty string.")
    if len(code.encode()) > LIMITS.max_code_bytes:
        return _blocked(f"code exceeds {LIMITS.max_code_bytes} bytes.")
    if isinstance(timeout_raw, bool) or not isinstance(timeout_raw, int):
        return _blocked(f"timeout_seconds must be an integer from 1 to {LIMITS.max_timeout_s}.")
    timeout_s = max(1, min(timeout_raw, LIMITS.max_timeout_s))

    log.info("run_code lang=%s bytes=%d timeout=%ds",
             language, len(code.encode()), timeout_s)

    try:
        # Selection runs before the outer deadline starts: a slow first probe
        # (cold Docker Desktop VM) must not eat this run's time budget.
        warden = await get_warden()
        # Outer deadline in case the warden itself hangs; +30 covers the backend's
        # bounded worst case (slot wait 5 + image preflight 5 + collect timeout+5
        # + CLI wait 5 = timeout + 20) with real margin (M1 §4, step-5 budget).
        # asyncio.timeout, not wait_for: on 3.11 wait_for runs the run in a separate
        # task, which can outlive this handler when the SDK cancels it through its
        # anyio scope — an untracked run that shutdown cleanup can't see (S7-D1).
        async with asyncio.timeout(timeout_s + 30):
            result = await warden.run(language, code, timeout_s, LIMITS)
    except RunRefusedError as e:
        # Refused before anything executed (concurrency limit S4-D1, missing image
        # or runtime, hard-tier probe failure) — a legible Blocked message the
        # model can adapt to, never an exception.
        log.info("run_code refused: %s", e)
        return _blocked(str(e))
    except TimeoutError:
        log.error("warden exceeded outer deadline")
        return [TextContent(type="text",
                            text="Execution failed: sandbox did not return in time.")]

    # Honesty hook: a container-tier run that reports no isolation means the
    # runtime died mid-session — drop the cached selection so the next call
    # re-probes and gets an actionable message.
    SELECTOR.note_result(warden, result)
    log.info("run_code done exit=%s timed_out=%s isolation=%s",
             result.exit_code, result.timed_out, result.isolation)
    return [TextContent(type="text", text=_format_result(result))]


async def _handle_read_workspace(args: dict) -> list[TextContent]:
    path = args.get("path", ".")
    if not isinstance(path, str):
        return _blocked("'path' must be a string.")

    ws = LIMITS.workspace_path
    try:
        ws.mkdir(parents=True, exist_ok=True)  # D1: created on first use
        # Filesystem work is blocking -> thread, never the event loop.
        text = await asyncio.to_thread(
            workspace.read_workspace_entry, ws, path, LIMITS.max_output_bytes
        )
    except workspace.WorkspacePathError as e:
        log.info("read_workspace refused path=%r: %s", path, e)
        return _blocked(str(e))
    except OSError as e:
        log.error("read_workspace failed path=%r: %s", path, e)
        return [TextContent(type="text", text=f"Error reading workspace: {e}")]
    return [TextContent(type="text", text=text)]


def _format_result(r: RunResult) -> str:
    parts: list[str] = []
    if r.timed_out:
        parts.append("[timed out and was terminated]")
    parts.append(f"exit_code: {r.exit_code}")
    if r.stdout:
        parts.append(f"stdout:\n{_truncate(r.stdout)}")
    if r.stderr:
        parts.append(f"stderr:\n{_truncate(r.stderr)}")
    usage = []
    if r.cpu_ms is not None:
        usage.append(f"cpu={r.cpu_ms}ms")
    if r.mem_peak_mb is not None:
        usage.append(f"mem_peak={r.mem_peak_mb}MB")
    if usage:
        parts.append("usage: " + ", ".join(usage))
    if r.denied_syscalls:
        parts.append("blocked syscalls: " + ", ".join(r.denied_syscalls))
    isolation = f"isolation: {r.isolation}"
    if r.isolation_detail:
        isolation += f" ({r.isolation_detail})"
    parts.append(isolation)
    return "\n".join(parts)


def _truncate(s: str, limit: int = LIMITS.max_output_bytes) -> str:
    return s if len(s) <= limit else s[:limit] + f"\n...[truncated {len(s) - limit} chars]"


async def _main() -> None:
    try:
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())
    finally:
        await _shutdown()


async def _shutdown() -> None:
    """Client gone (stdin EOF): clean up inside the client's ~2 s grace (S7-D1).

    The SDK has already cancelled in-flight handlers; their containers are removed
    here. If cleanup tasks are still pending, exit at once instead of returning into
    asyncio.run(), whose cancel-all can catch a task mid-subprocess-spawn and hang
    loop close forever on Windows (the step-5 hang; reproduced at shutdown in step 7).
    Nothing is written to stdout here — the client is no longer reading it.
    """
    try:
        clean = await SELECTOR.aclose()
    except Exception:
        log.exception("shutdown cleanup failed")
        clean = False
    # Belt and braces: return into asyncio.run() only when there is nothing at all
    # left for its cancel-all to cancel — whatever spawned it, tracked or not.
    leftovers = _other_live_tasks()
    if not clean or leftovers:
        log.warning("shutdown: exiting with cleanup still pending (%d other task(s))",
                    len(leftovers))
        for stream in (sys.stderr, sys.stdout):
            with contextlib.suppress(Exception):
                stream.flush()
        os._exit(0)


def _other_live_tasks() -> list[asyncio.Task]:
    me = asyncio.current_task()
    return [t for t in asyncio.all_tasks() if t is not me and not t.done()]


def run() -> None:
    """Console entry point (`vestibule-mcp`)."""
    asyncio.run(_main())


if __name__ == "__main__":
    run()
