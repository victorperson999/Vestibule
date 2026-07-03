# CLAUDE.md — Operating rules for building Vestibule

This file is read automatically by Claude Code. It defines the invariants, conventions, and boundaries for this project. **Read `docs/ARCHITECTURE.md` and `docs/PLAN.md` before writing code.**

---

## What this project is (one paragraph)

Vestibule is a local, kernel-isolated code-execution sandbox for AI agents, shipped as an MCP server (`vestibule-mcp` on PyPI). An agent calls a `run_code` tool; the code runs inside an isolated sandbox (Linux user/mount/pid/net namespaces + cgroups v2 + optional seccomp + a `pivot_root` filesystem jail) with no network by default, resource limits, and a full audit log. The MCP **server** decides *whether/what*; the **warden** decides *how* to isolate and run. Both are Python.

---

## Golden rules (invariants — never violate these)

1. **NEVER write to stdout except through the MCP SDK.** The stdio transport uses stdout as the JSON-RPC channel; any stray `print()` corrupts the protocol and breaks the session. All logging goes to **stderr** or a file. This applies to the server *and* anything it spawns.
2. **Always run unprivileged.** No feature may require `sudo` or real root. Isolation is achieved via a user namespace (`CLONE_NEWUSER`) that maps the unprivileged UID to root *inside* the namespace. If a capability needs real root, it doesn't ship.
3. **Untrusted code NEVER runs in the server process.** All execution happens in a `fork`'d/`subprocess` child that has been isolated. The long-lived server process stays clean.
4. **Tool handlers return errors as content; they do not raise.** An unhandled exception can kill the MCP session. Every handler path returns readable `TextContent` — including "Blocked: …" messages — because the *model reads them and adapts*.
5. **Report isolation honestly, every time.** Every `run_code` result includes an `isolation:` field stating what actually protected the run (`native`, `container`, `namespaces-only`, `none`). Never claim protection that wasn't applied. Never oversell: this is namespace isolation sharing the host kernel, **not** a hardened VM. Say so in `SECURITY.md`.
6. **It must RUN everywhere, even where best-in-class isolation is unavailable.** Native isolation is Linux-only; on macOS/Windows fall back to the container backend. A user who can't install/run it is a lost user. Degrade loudly and clearly, never fail silently.
7. **Validate before executing.** Clamp `timeout_seconds` to the max, cap code size, enforce the `language` enum — in the clean server process, before any untrusted code is spawned.
8. **Truncate guest output.** Bound stdout/stderr returned to the agent so a chatty program can't flood the model's context window.

---

## Tech stack & conventions

- **Python ≥ 3.11.** Type hints everywhere. `from __future__ import annotations` where useful.
- **MCP:** the official `mcp` SDK. Low-level `Server` API (not FastMCP) — we want explicit control over tool schemas because *tool descriptions are prompts read by the model*. (The SDK evolves; if an import/signature has drifted, fix it against current docs and note the change.)
- **Isolation:** `ctypes` calling libc/syscalls directly. cgroups v2 is just filesystem writes (no ctypes). seccomp uses the optional `pyseccomp` binding — never hand-assemble BPF.
- **Async:** the MCP server is `async`. The container backend uses `asyncio.create_subprocess_exec`. The native-fork warden does blocking syscalls → run it in an executor (`run_in_executor`), never directly in the event loop.
- **Layout:** `src/` layout. Import package `vestibule`; distribution `vestibule-mcp`.
- **Tooling:** `ruff` (lint+format), `mypy` (types), `pytest` + `pytest-asyncio` (tests). Line length 100.
- **Commits:** small, milestone-scoped. Conventional-commits style (`feat:`, `fix:`, `docs:`) is nice-to-have.

## Repo structure (target)

```
vestibule/
├── README.md
├── CLAUDE.md
├── SECURITY.md              # threat model — write honestly (M4)
├── pyproject.toml
├── docs/
│   ├── PLAN.md
│   ├── ARCHITECTURE.md
│   └── GETTING_STARTED.md
├── src/vestibule/
│   ├── __init__.py
│   ├── server.py            # MCP server: protocol, schemas, validation, dispatch
│   ├── config.py            # Limits, ALLOWED_LANGUAGES, env loading
│   └── backends/
│       ├── __init__.py
│       ├── base.py          # Warden ABC + RunResult dataclass
│       ├── naive.py         # M0: subprocess, NO isolation (plumbing only)
│       ├── container.py     # M1: Docker/Podman backend (cross-platform floor)
│       └── native.py        # M2: ctypes namespaces + cgroups + seccomp (Linux)
└── tests/
    └── test_smoke.py
```

## How to work

- **Milestone-driven.** Ship M0 (working, unsafe plumbing) before touching isolation. Get a live agent calling `run_code` first; a running feedback loop makes everything after concrete. Order matters — see `docs/PLAN.md`.
- **The container backend (M1) ships before the native warden (M2).** M1 is what makes Vestibule usable on Windows/macOS on day one. The native warden is the *differentiator*, but building it first would leave 80% of users unable to run the tool.
- **Definition of done for a change:** it runs, it has at least a smoke test, `ruff` and `mypy` are clean, and any new user-facing behavior is reflected in the README/docs.
- **When you hit a design fork not covered here**, prefer the option that (a) keeps the server process clean, (b) degrades gracefully cross-platform, and (c) is explainable in an interview from first principles. If still unclear, ask.

## What NOT to do

- **Don't add heavy frameworks** (LangChain, agent frameworks, ORMs). Vestibule is infrastructure; keep the dependency tree tiny so it's auditable and installs fast. Every dependency is a trust and adoption cost.
- **Don't expand the tool surface.** Two tools (`run_code`, `read_workspace`). More tools/params = more the model can misuse.
- **Don't hand-roll seccomp BPF.** Use `pyseccomp`, keep it optional.
- **Don't require Docker on Linux** — native isolation is the point there. Docker is the *fallback*, not the default, on Linux.
- **Don't let output or errors escape to stdout.** (Restating rule #1 because it's the #1 way to break an MCP server.)
- **Don't oversell security.** No "unescapable", no "unbreakable". Honest threat modeling is a trust-builder and a differentiator.

## Security posture (summary; full version → `SECURITY.md`)

Vestibule provides strong isolation comparable in *mechanism* to rootless containers: no host filesystem visibility (only a bind-mounted workspace + tmpfs), no network egress (empty network namespace), resource caps (cgroups), and a syscall allowlist (seccomp). It is **not** a VM boundary — it shares the host kernel, so a kernel privilege-escalation exploit could in principle escape. State this plainly. The goal is to make the *common, realistic* agent risks (prompt-injected exfiltration, destructive commands, resource exhaustion) structurally impossible, not to defend against a nation-state kernel 0-day.
