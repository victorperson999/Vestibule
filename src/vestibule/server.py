"""Vestibule MCP server (stdio transport).

RULE: never write to stdout except via the MCP SDK. stdout is the JSON-RPC channel;
a stray print() corrupts the protocol. All logging goes to stderr.
"""
from __future__ import annotations

import asyncio
import logging
import sys

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from vestibule.backends.base import RunResult, Warden
from vestibule.backends.naive import NaiveBackend
from vestibule.config import ALLOWED_LANGUAGES, Limits

logging.basicConfig(
    stream=sys.stderr,  # NOT stdout
    level=logging.INFO,
    format="%(asctime)s vestibule %(levelname)s %(message)s",
)
log = logging.getLogger("vestibule")

LIMITS = Limits.from_env()
server = Server("vestibule")


def get_warden() -> Warden:
    # M0: always the unsafe naive backend.
    # M1+: capability-detect -> NativeWarden on Linux, else ContainerBackend.
    return NaiveBackend()


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="run_code",
            description=(
                "Execute code in an isolated sandbox and return its output. "
                "The sandbox has NO network access and an ephemeral filesystem "
                "(only the workspace directory persists). It is resource-limited: "
                "code exceeding the memory/CPU/time limits is terminated. "
                "Use this to run and test code safely; do not expect internet access."
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
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    try:
        if name == "run_code":
            return await _handle_run_code(arguments)
        return [TextContent(type="text", text=f"Unknown tool: {name}")]
    except Exception as e:  # never let an exception kill the session
        log.exception("tool handler crashed")
        return [TextContent(type="text", text=f"Internal error: {e}")]


async def _handle_run_code(args: dict) -> list[TextContent]:
    language = args.get("language")
    code = args.get("code", "")

    if language not in ALLOWED_LANGUAGES:
        return [TextContent(type="text",
                            text=f"Blocked: language must be one of {ALLOWED_LANGUAGES}.")]
    if not isinstance(code, str) or not code.strip():
        return [TextContent(type="text", text="Blocked: 'code' is empty.")]
    if len(code.encode()) > LIMITS.max_code_bytes:
        return [TextContent(type="text",
                            text=f"Blocked: code exceeds {LIMITS.max_code_bytes} bytes.")]

    timeout_s = int(args.get("timeout_seconds", LIMITS.default_timeout_s))
    timeout_s = max(1, min(timeout_s, LIMITS.max_timeout_s))

    log.info("run_code lang=%s bytes=%d timeout=%ds",
             language, len(code.encode()), timeout_s)

    warden = get_warden()
    try:
        result = await asyncio.wait_for(
            warden.run(language, code, timeout_s, LIMITS),
            timeout=timeout_s + 5,  # outer deadline in case the warden itself hangs
        )
    except asyncio.TimeoutError:
        log.error("warden exceeded outer deadline")
        return [TextContent(type="text",
                            text="Execution failed: sandbox did not return in time.")]

    log.info("run_code done exit=%s timed_out=%s isolation=%s",
             result.exit_code, result.timed_out, result.isolation)
    return [TextContent(type="text", text=_format_result(result))]


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
    parts.append(f"isolation: {r.isolation}")
    return "\n".join(parts)


def _truncate(s: str, limit: int = LIMITS.max_output_bytes) -> str:
    return s if len(s) <= limit else s[:limit] + f"\n...[truncated {len(s) - limit} chars]"


async def _main() -> None:
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


def run() -> None:
    """Console entry point (`vestibule-mcp`)."""
    asyncio.run(_main())


if __name__ == "__main__":
    run()
