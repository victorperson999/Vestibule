"""Vestibule MCP server (stdio transport).

RULE: never write to stdout except via the MCP SDK. stdout is the JSON-RPC channel;
a stray print() corrupts the protocol. All logging goes to stderr.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import secrets
import signal
import subprocess
import sys
import tempfile
import time
from io import TextIOWrapper
from typing import Any, cast

import anyio
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from vestibule import __version__, audit, cleanup, rootfs, workspace
from vestibule.backends.base import RunRefusedError, RunResult
from vestibule.backends.container import ContainerBackend
from vestibule.backends.naive import kill_guest
from vestibule.backends.select import BackendSelector, Selection
from vestibule.config import ALLOWED_LANGUAGES, ConfigError, Limits

logging.basicConfig(
    stream=sys.stderr,  # NOT stdout
    level=logging.INFO,
    format="%(asctime)s vestibule %(levelname)s %(message)s",
)
log = logging.getLogger("vestibule")


def _load_limits() -> tuple[Limits, str | None]:
    """The configured limits, or the defaults plus an error message if a setting is invalid.

    A bad config does not stop the server: the client would only show "failed to
    connect", with the reason in a log the model never sees. The server starts and every
    tool call returns the message as `Blocked:`, so the agent can tell the user what to
    fix (golden rules 4 and 6). The defaults only render the tool schema; nothing runs.
    """
    try:
        return Limits.from_env(), None
    except ConfigError as e:
        msg = (
            "Vestibule's configuration is invalid: " + "; ".join(e.problems) + ". Fix the "
            "setting in the MCP client's config for this server, then restart it. "
            "Nothing was run."
        )
        return Limits(), msg  # logged when the server starts (_main), not by `setup`


LIMITS, CONFIG_ERROR = _load_limits()
# Without version= the SDK reports its own package version as the server's.
server = Server("vestibule", version=__version__)
SELECTOR = BackendSelector()
# M3: no I/O here; the file appears on the first record (docs/plans/M3/M3-step2-audit-writer.md).
AUDIT = audit.AuditLog(LIMITS.audit_dir)


async def get_selection() -> Selection:
    # M1 (contract §5): lazily probe & cache the real backend on the first tool
    # call. Naive is never auto-selected — it needs explicit VESTIBULE_BACKEND=naive.
    # Raises RunRefusedError with an actionable message when no honest isolation
    # is possible; the handler renders it as `Blocked:` content. The whole Selection,
    # because the audit's start record keeps its verdict (M3 A6).
    return await SELECTOR.get(LIMITS)


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
        if CONFIG_ERROR is not None:
            # The defaults standing in for a bad config are not what the user asked for.
            return _blocked(CONFIG_ERROR)
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


# A run_code result with no RunResult behind it (M2 follow-ups F3): nothing confirmed what
# protected the run, worded like the native verdict for a sandbox that sent no report.
_NO_REPORT = ("isolation: none (the sandbox did not report back; no isolation is claimed for "
              "this run)")


async def _handle_run_code(args: dict) -> list[TextContent]:
    # Validation is total (M1 finding 18): every argument is type-checked here, in
    # the clean server process, before any subprocess can exist. Out-of-type input
    # gets a legible Blocked message, never an exception.
    language = args.get("language")
    code = args.get("code")
    timeout_raw = args.get("timeout_seconds", LIMITS.default_timeout_s)

    if not isinstance(language, str) or language not in ALLOWED_LANGUAGES:
        return await _refuse(f"language must be one of {ALLOWED_LANGUAGES}.", language)
    if not isinstance(code, str) or not code.strip():
        return await _refuse("'code' must be a non-empty string.", language)
    if len(code.encode()) > LIMITS.max_code_bytes:
        return await _refuse(f"code exceeds {LIMITS.max_code_bytes} bytes.", language)
    if isinstance(timeout_raw, bool) or not isinstance(timeout_raw, int):
        return await _refuse(
            f"timeout_seconds must be an integer from 1 to {LIMITS.max_timeout_s}.", language)
    timeout_s = max(1, min(timeout_raw, LIMITS.max_timeout_s))

    log.info("run_code lang=%s bytes=%d timeout=%ds",
             language, len(code.encode()), timeout_s)

    # Each way a call ends leaves its audit records (M3 A6): `blocked` before a backend is
    # chosen (W1), else `start` and `end`. A cancel queues its record and re-raises at once;
    # every other path waits once, after the run, at most 2 s (A7, W2).
    try:
        # Selection runs before the outer deadline starts: a slow first probe
        # (cold Docker Desktop VM) must not eat this run's time budget.
        selection = await get_selection()
    except asyncio.CancelledError:
        AUDIT.submit(audit.blocked_record(language, "cancelled", None))
        raise
    except RunRefusedError as e:  # no honest isolation possible: the message names the fix
        log.info("run_code refused: %s", e)
        return await _refuse(str(e), language)
    except Exception as e:  # golden rule 5 here too; call_tool's catch-all has no isolation line
        first = _failure(e)
        note = await AUDIT.confirm(AUDIT.submit(audit.blocked_record(language, "failed", first)))
        return [TextContent(type="text", text=first + _audit_line(note) + "\n" + _NO_REPORT)]

    audit_id = secrets.token_hex(8)
    backend, runtime = _backend(selection)
    image = None if backend == "naive" else (
        LIMITS.image_node if language == "node" else LIMITS.image_python)
    start = AUDIT.submit(audit.start_record(
        audit_id, language=language, code=code, timeout_s=timeout_s, backend=backend,
        runtime=runtime, verdict=selection.verdict, image=image, client=_client()))
    # M3 A1: wall time as the agent waits for it (the slot, a preflight, placement), never
    # the selection or the audit's wait.
    started = time.monotonic_ns()
    try:
        # Outer deadline in case the warden itself hangs; +30 covers the backend's
        # bounded worst case (slot wait 5 + image preflight 5 + collect timeout+5
        # + CLI wait 5 = timeout + 20) with real margin (M1 §4, step-5 budget).
        # asyncio.timeout, not wait_for: on 3.11 wait_for runs the run in a separate
        # task, which can outlive this handler when the SDK cancels it through its
        # anyio scope — an untracked run that shutdown cleanup can't see (S7-D1).
        async with asyncio.timeout(timeout_s + 30):
            result = await selection.warden.run(language, code, timeout_s, LIMITS)
        wall_ms = _ms_since(started)
    except asyncio.CancelledError:  # the SDK's cancel: queue the end, never wait (A6, A7)
        AUDIT.submit(audit.end_record(audit_id, "cancelled", _ms_since(started)))
        raise
    except RunRefusedError as e:
        # Refused before anything executed (concurrency limit S4-D1, missing image
        # or runtime, hard-tier probe failure) — a legible Blocked message the
        # model can adapt to, never an exception. After the start, so an end (A6).
        log.info("run_code refused: %s", e)
        note = await AUDIT.confirm(start, AUDIT.submit(
            audit.end_record(audit_id, "refused", _ms_since(started), str(e))))
        return _blocked(str(e) + _audit_line(note))
    except Exception as e:  # the outer deadline (TimeoutError), or an unexpected error
        first = _failure(e)
        note = await AUDIT.confirm(start, AUDIT.submit(
            audit.end_record(audit_id, "failed", _ms_since(started), first)))
        return [TextContent(type="text", text=first + _audit_line(note) + "\n" + _NO_REPORT)]

    # Honesty hook: a container-tier run that reports no isolation means the
    # runtime died mid-session — drop the cached selection so the next call
    # re-probes and gets an actionable message.
    SELECTOR.note_result(selection.warden, result)
    log.info("run_code done exit=%s timed_out=%s isolation=%s",
             result.exit_code, result.timed_out, result.isolation)
    note = await AUDIT.confirm(start, AUDIT.submit(audit.end_from_result(audit_id, result,
                                                                         wall_ms)))
    return [TextContent(type="text", text=_format_result(result, wall_ms, note))]


async def _refuse(msg: str, language: object) -> list[TextContent]:
    """`Blocked:` before a backend was chosen, with its `blocked` record (M3 A6)."""
    note = await AUDIT.confirm(AUDIT.submit(audit.blocked_record(language, "refused", msg)))
    return _blocked(msg + _audit_line(note))


def _failure(e: Exception) -> str:
    """The first line of a call that ended with no RunResult (M2 follow-ups F3), logged."""
    if isinstance(e, TimeoutError):
        log.error("warden exceeded outer deadline")
        return "Execution failed: sandbox did not return in time."
    log.exception("run_code crashed")
    return f"Internal error: {e}"


def _audit_line(note: str | None) -> str:
    """The `audit:` line, only when a record wasn't written or confirmed (A3)."""
    return "" if note is None else f"\naudit: {note}"


def _ms_since(started: int) -> int:
    return -(-(time.monotonic_ns() - started) // 1_000_000)  # integer ns, rounded up to ms


def _backend(selection: Selection) -> tuple[str, str | None]:
    """The start record's backend in VESTIBULE_BACKEND's words, and a container's runtime.
    Selection gives the verdict `none` only to an explicit naive backend."""
    if isinstance(selection.warden, ContainerBackend):
        return "container", selection.warden.runtime
    return ("naive" if selection.verdict == "none" else "native"), None


def _client() -> dict[str, str] | None:
    """The MCP client's name and version, when the SDK has them (A6)."""
    try:
        params = server.request_context.session.client_params
    except LookupError:  # no request: the handler was called directly (tests)
        return None
    if params is None:  # before initialization
        return None
    return {"name": params.clientInfo.name[:200], "version": params.clientInfo.version[:200]}


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


# After the kernel's numbers (M3 U5): they count init and the page cache too, so the same code can
# read differently twice. The model never reads the README.
_WHOLE_SANDBOX = " (the whole sandbox, including its setup and file cache)"


def _format_result(r: RunResult, wall_ms: int, audit_note: str | None = None) -> str:
    parts: list[str] = []
    if r.timed_out:
        parts.append("[timed out and was terminated]")
    parts.append(f"exit_code: {r.exit_code}")
    if r.stdout:
        parts.append(f"stdout:\n{_truncate(r.stdout)}")
    if r.stderr:
        parts.append(f"stderr:\n{_truncate(r.stderr)}")
    usage = f"usage: wall={wall_ms}ms"
    counted = []  # the kernel's numbers (M3 A1, U1, U5)
    if r.cpu_ms is not None:
        counted.append(f"cpu={r.cpu_ms}ms")
    if r.mem_peak_mb is not None:
        counted.append(f"mem_peak={r.mem_peak_mb}MiB")
    if r.pids_peak is not None:
        counted.append(f"pids_peak={r.pids_peak}")
    if counted:
        usage += ", " + ", ".join(counted) + _WHOLE_SANDBOX
    if r.not_measured:
        usage += f"; {r.not_measured}"
    parts.append(usage)
    hits = r.limit_hits
    shown = []  # U4 and U3; native's cpu.max period is 100 ms (native_cgroup)
    if "tasks_refused" in hits:
        shown.append(f"pids ({hits['tasks_refused']} new processes or threads refused)")
    if "cpu_throttled_periods" in hits:
        shown.append(f"cpu (throttled in {hits['cpu_throttled_periods']} of "
                     f"{hits['cpu_periods']} periods of 100 ms)")
    if shown:
        parts.append("limits hit: " + ", ".join(shown))
    if r.denied_syscalls:
        parts.append("blocked syscalls: " + ", ".join(r.denied_syscalls))
    if audit_note is not None:  # above isolation, which stays the last line (M3 A3)
        parts.append(f"audit: {audit_note}")
    isolation = f"isolation: {r.isolation}"
    if r.isolation_detail:
        isolation += f" ({r.isolation_detail})"
    parts.append(isolation)
    return "\n".join(parts)


def _truncate(s: str, limit: int = LIMITS.max_output_bytes) -> str:
    return s if len(s) <= limit else s[:limit] + f"\n...[truncated {len(s) - limit} chars]"


async def _main() -> None:
    if CONFIG_ERROR:
        log.error("%s", CONFIG_ERROR)
    if sys.platform == "win32":
        try:
            async with stdio_server() as (read, write):
                await server.run(read, write, server.create_initialization_options())
        finally:
            await _shutdown()
        return

    # POSIX (M2 step 1b, S1-D5): every way a client ends the server runs the same
    # synchronous hand-off. Signal handlers run on the loop thread (never between
    # bytecodes), and EOF is caught inside the stdin wrapper, BEFORE the SDK sees it and
    # cancels in-flight handlers (whose finishers would otherwise race the hand-off).
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        loop.add_signal_handler(sig, _hand_off_and_exit, sig.name)
    stdin = _HandOffOnEof(
        anyio.wrap_file(TextIOWrapper(sys.stdin.buffer, encoding="utf-8", errors="replace"))
    )
    try:
        async with stdio_server(stdin=cast("anyio.AsyncFile[str]", stdin)) as (read, write):
            await server.run(read, write, server.create_initialization_options())
    except Exception:
        log.exception("server stopped with an error")
    finally:
        _hand_off_and_exit("server stopped")


# --- POSIX shutdown: hand off, then exit (M2 step 1b, S1-D5) ----------------------------
#
# Real clients give a stdio server 100 ms (Claude Code on Linux: SIGINT, then SIGTERM, then
# SIGKILL, to the PID, with stdin still open) up to 2 s (the MCP Python SDK: close stdin,
# then killpg). Cleaning up in-process cannot fit the short end, and today's SDK stdio task
# group would hang on SIGINT anyway (its stdin thread never returns). So the server
# snapshots what it owns, kills its `docker run` CLIs, starts `vestibule.cleanup` in its own
# session (out of reach of both kinds of kill), and exits within milliseconds.

_handed_off = False


def _hand_off_and_exit(trigger: str) -> None:
    """Snapshot, kill CLIs, start the cleaner, exit 0. Synchronous: no await anywhere, so
    two triggers cannot interleave and no run or selection resumes after the snapshot.
    Every step is guarded; the `finally` exits whatever happens. Never touches stdout: the
    SDK's writer thread may be mid-message, and the client is ending the session anyway."""
    global _handed_off
    if _handed_off:
        return
    _handed_off = True
    note = "nothing to clean up"
    try:
        job = SELECTOR.cleanup_job()
        for guest in job.guests:  # naive (dev-only): the guest and its children
            kill_guest(guest)
        for proc in job.cli_procs:  # no new create requests reach the daemon
            with contextlib.suppress(Exception):
                proc.kill()
        if not job.empty:
            _spawn_cleaner(job.payload())
            note = (f"handed {len(job.names)} container(s) and {len(job.tmpdirs)} temp "
                    "dir(s) to the cleaner")
    except BaseException as e:  # noqa: BLE001 - exit regardless; S7-D5 backstops containers
        note = f"hand-off failed: {e!r}"
    finally:
        with contextlib.suppress(Exception):
            # os.write, not logging: one atomic line, no handler locks.
            line = f"vestibule shutdown ({trigger}): {note}\n"
            os.write(2, line.encode(errors="replace"))
        os._exit(0)


def _spawn_cleaner(payload: dict[str, object]) -> None:
    subprocess.Popen(
        # By file path, not `-m vestibule.cleanup`: -I drops PYTHONPATH and the user site,
        # so the package may not be importable there, and the module is stdlib-only anyway.
        # -B: isolated mode also drops PYTHONPYCACHEPREFIX, so write no bytecode at all.
        [sys.executable, "-I", "-B", cleanup.__file__, json.dumps(payload)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,  # golden rule 1: nothing may reach the MCP channel
        stderr=subprocess.DEVNULL,
        cwd=tempfile.gettempdir(),  # never hold the client's working folder
        start_new_session=True,  # out of reach of a PID kill and of the SDK's killpg
        close_fds=True,
    )


class _HandOffOnEof:
    """The stdin `stdio_server` iterates: on EOF, hand off and exit BEFORE the SDK sees the
    EOF (S1-D5). The SDK only ever does `async for line in stdin`."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def __aiter__(self) -> "_HandOffOnEof":
        return self

    async def __anext__(self) -> str:
        line: str = await self._inner.readline()
        if not line:
            _hand_off_and_exit("stdin closed")
            raise StopAsyncIteration  # reached only when os._exit is stubbed (tests)
        return line


# --- Windows shutdown: M1's in-process cleanup (S7-D1) -----------------------------------
#
# On Windows the server sits in kill-on-close Job Objects (the console-script launcher's,
# the venv redirector's, the Python SDK client's), so a detached child would die with the
# session. The in-process cleanup fits the Python SDK's 2 s. Claude Code's 300 ms window is
# a documented gap (docs/BUILD_LOG.md): S7-D5's self-deadline ends the container.


async def _shutdown() -> None:
    """Client gone (stdin EOF): clean up inside the client's ~2 s grace (S7-D1). Windows.

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
    """Console entry point: `vestibule-mcp` serves MCP on stdio; `vestibule-mcp setup`
    fetches the native rootfs (docs/plans/M2/M2-step2-rootfs.md S2-D1)."""
    args = sys.argv[1:]
    if args == ["setup"]:
        sys.exit(rootfs.setup_main())
    if args:
        # Not an error: refusing to start would leave the client saying only "failed to
        # connect" (the M1 follow-up's rule). A human at a terminal sees this line.
        log.warning('ignoring unexpected arguments %s; the only subcommand is "setup"', args)
    asyncio.run(_main())


if __name__ == "__main__":
    run()
