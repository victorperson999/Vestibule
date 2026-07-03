# Vestibule — Getting Started (Milestone 0)

This gets you from empty folder to **a live agent executing `print("hi")` through Vestibule**. All Milestone-0 starter code is inline below — create each file with the given path and contents. When you run Claude Code, point it at this file and it can create the scaffold for you.

> **Platform note.** M0 (naive subprocess backend) and M1 (container backend) develop fine on **Windows/macOS/Linux**. The native warden (M2) is **Linux-only** — on Windows you'll develop it inside **WSL2**. Nothing below requires WSL yet.

> **SDK note.** The `mcp` Python SDK evolves. The code below targets the low-level `Server` API. If an import or signature has drifted, fix it against the current SDK docs and note the change in the commit — the structure stays the same.

---

## Prerequisites

- **Python ≥ 3.11** (`python --version`)
- **pipx** for the eventual install (`pip install --user pipx`), or just use a venv during dev
- **Node.js** (optional) — only if you want to test the `node` language path in M0
- **Docker or Podman** (for M1, later)
- An MCP-speaking agent to test against: **Claude Code** (recommended, you already use it), Claude Desktop, or Cursor

---

## Step 1 — Scaffold the repo

```bash
mkdir vestibule && cd vestibule
python -m venv .venv
# Windows:  .venv\Scripts\activate
# macOS/Linux:  source .venv/bin/activate

mkdir -p src/vestibule/backends tests docs
```

Drop the four docs (`README.md`, `CLAUDE.md`, `docs/PLAN.md`, `docs/ARCHITECTURE.md`, `docs/GETTING_STARTED.md`) into place, then create the files below.

---

## Step 2 — Create the Milestone-0 files

### `pyproject.toml`
```toml
[project]
name = "vestibule-mcp"
version = "0.0.1"
description = "A local, kernel-isolated code-execution sandbox for AI agents, exposed as an MCP server."
readme = "README.md"
requires-python = ">=3.11"
license = { text = "MIT" }
authors = [{ name = "Victor" }]
keywords = ["mcp", "sandbox", "ai-agents", "code-execution", "security", "isolation"]
dependencies = [
    "mcp>=1.2.0",
]

[project.optional-dependencies]
seccomp = ["pyseccomp>=0.1.2"]                       # optional defense-in-depth (Linux, M2)
dev = ["pytest>=8.0", "pytest-asyncio>=0.23", "ruff>=0.6", "mypy>=1.11"]

[project.scripts]
vestibule-mcp = "vestibule.server:run"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/vestibule"]

[tool.ruff]
line-length = 100
target-version = "py311"

[tool.pytest.ini_options]
asyncio_mode = "auto"
```

### `src/vestibule/__init__.py`
```python
"""Vestibule: a local, kernel-isolated code-execution sandbox for AI agents (MCP server)."""
__version__ = "0.0.1"
```

### `src/vestibule/config.py`
```python
"""Configuration: resource limits and allowed languages. Overridable via env later."""
from __future__ import annotations

import os
from dataclasses import dataclass

ALLOWED_LANGUAGES: tuple[str, ...] = ("python", "bash", "node")


@dataclass(frozen=True)
class Limits:
    max_timeout_s: int = 60           # server clamps requests to this ceiling
    default_timeout_s: int = 10
    max_code_bytes: int = 256 * 1024  # reject giant payloads before the warden
    max_output_bytes: int = 20_000    # truncate guest output to protect model context
    mem_mb: int = 256                 # (used by container/native backends)
    pids_max: int = 128
    cpu_pct: int = 75

    @classmethod
    def from_env(cls) -> "Limits":
        def _i(name: str, default: int) -> int:
            v = os.environ.get(name)
            return int(v) if v and v.isdigit() else default
        return cls(
            max_timeout_s=_i("VESTIBULE_MAX_TIMEOUT_S", 60),
            default_timeout_s=_i("VESTIBULE_DEFAULT_TIMEOUT_S", 10),
            max_code_bytes=_i("VESTIBULE_MAX_CODE_BYTES", 256 * 1024),
            max_output_bytes=_i("VESTIBULE_MAX_OUTPUT_BYTES", 20_000),
            mem_mb=_i("VESTIBULE_MEM_MB", 256),
            pids_max=_i("VESTIBULE_PIDS_MAX", 128),
            cpu_pct=_i("VESTIBULE_CPU_PCT", 75),
        )
```

### `src/vestibule/backends/__init__.py`
```python
```
*(empty file)*

### `src/vestibule/backends/base.py`
```python
"""Warden interface + result type. Server depends on this abstraction, not on any impl."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from vestibule.config import Limits


@dataclass
class RunResult:
    stdout: str
    stderr: str
    exit_code: int
    timed_out: bool
    cpu_ms: int | None = None
    mem_peak_mb: int | None = None
    denied_syscalls: list[str] = field(default_factory=list)
    isolation: str = "none"           # "none" | "container" | "namespaces-only" | "native"


class Warden(ABC):
    """Runs code in *some* level of isolation and reports honestly what it applied."""

    @abstractmethod
    async def run(self, language: str, code: str, timeout_s: int, limits: Limits) -> RunResult:
        ...
```

### `src/vestibule/backends/naive.py`
```python
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
            proc = await asyncio.create_subprocess_exec(
                *cmd, cwd=str(workdir),
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
```

### `src/vestibule/server.py`
```python
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
```

### `tests/test_smoke.py`
```python
"""Smoke test for the M0 naive backend. Runs without an agent."""
import pytest

from vestibule.backends.naive import NaiveBackend
from vestibule.config import Limits


@pytest.mark.asyncio
async def test_python_hello():
    result = await NaiveBackend().run("python", "print('hi')", 10, Limits())
    assert result.exit_code == 0
    assert "hi" in result.stdout
    assert result.isolation == "none"


@pytest.mark.asyncio
async def test_timeout_is_enforced():
    code = "import time; time.sleep(30)"
    result = await NaiveBackend().run("python", code, 1, Limits())
    assert result.timed_out is True


@pytest.mark.asyncio
async def test_unknown_language_is_clean():
    result = await NaiveBackend().run("ruby", "puts 1", 5, Limits())
    assert result.exit_code == 127
```

### `.gitignore`
```
.venv/
__pycache__/
*.pyc
dist/
build/
*.egg-info/
.pytest_cache/
.mypy_cache/
.ruff_cache/
```

---

## Step 3 — Install and test locally (no agent yet)

```bash
pip install -e ".[dev]"

# unit smoke tests
pytest -q

# lint + types
ruff check .
mypy src

# sanity: does the server start and speak MCP on stdio? (Ctrl-C to exit)
vestibule-mcp
```

If `pytest` is green, the backend works. Next, wire it to a live agent.

---

## Step 4 — Register with Claude Code

Add Vestibule as an MCP server. Easiest is the CLI:

```bash
# from the repo root, with the venv active
claude mcp add vestibule -- vestibule-mcp
```

Or edit the MCP config directly (adjust the path to your venv's script):

```jsonc
{
  "mcpServers": {
    "vestibule": {
      "command": "vestibule-mcp"
      // If not on PATH, use the absolute path to the entry point, e.g.:
      // Windows: "command": "C:\\path\\to\\vestibule\\.venv\\Scripts\\vestibule-mcp.exe"
      // macOS/Linux: "command": "/path/to/vestibule/.venv/bin/vestibule-mcp"
    }
  }
}
```

Restart Claude Code so it picks up the server. Verify the tool is discovered (Claude Code lists MCP tools on startup / via its tools command).

---

## Step 5 — First run (the M0 goal)

Ask the agent, in a Claude Code session:

> Use the `run_code` tool to run this Python: `print("hi from vestibule")`

You should see the tool invoked and the output returned with `isolation: none`. **That's Milestone 0 complete** — the plumbing works end to end.

Try a couple more to feel the shape:
- A timeout: ask it to run `import time; time.sleep(30)` with `timeout_seconds: 2` → expect `[timed out and was terminated]`.
- A crash: run `1/0` → expect a traceback in `stderr` and a non-zero `exit_code`.

---

## Step 6 — Hand off to Claude Code for Milestone 1

Open Claude Code in this repo (it will read `CLAUDE.md` automatically) and give it this prompt:

> Read `CLAUDE.md`, `docs/PLAN.md`, and `docs/ARCHITECTURE.md`. Milestone 0 is done (naive backend + working `run_code`). Implement **Milestone 1**: a `ContainerBackend` (Docker/Podman) with `--network none`, memory/cpu/pids limits, read-only rootfs, non-root user, and a bind-mounted workspace; plus capability detection in `get_warden()` (native on Linux later, container elsewhere now), and the `read_workspace` tool with strict path-jailing. Report `isolation: "container"` in results. Follow the golden rules in `CLAUDE.md` — especially: no stdout writes, errors returned as content, honest isolation reporting, and it must run on Windows/macOS with Docker. Add tests. Show me a plan before you start editing.

From there, work milestone by milestone. Keep `CLAUDE.md` authoritative — if a rule needs to change, change it there first.
