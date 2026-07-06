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

## Workflow Rules

- **Plan before code**: For every coding task, present a written plan (files to change, approach, edge cases) and wait for explicit user approval before writing any code.
- **Show your changes**: After writing code, summarize every file that was modified and what changed in each.
- **Log this session**: When the user says "log this session", append a new dated entry to the top of the `## Changelog` section below, summarizing what was implemented in the session as bullet points per file changed (matching the existing entry format). This builds a running reference of prior work for future sessions.

## Changelog

### 2026-07-05 — M1 step 5 built: capability probing & honest backend selection; step-4 timeout-budget fix; two build-time bugs found and fixed (CRLF scripts, failed-selection task leak)
- `docs/plans/M1-step5-selection.md`: new — step-5 plan (deliberately short/plain per user request). Codex adversarial review (2 high findings) + a final pre-build logic check reshaped it: S5-D1 (failed selections cached only 30 s, then re-checked — "start Docker Desktop, then retry" works without an MCP session restart; success caches for process lifetime), S5-D2 (degraded retry drops all four soft limits at once; per-limit add-back rejected for unbounded first-call latency), S5-D3 **revised** (per-run image preflight + `--pull never` moved INTO step 5 — Codex showed `docker run`'s default auto-pull let a first node run start a multi-minute network pull inside a tool call, violating D2). The logic check caught a third gap: "daemon dies after a good probe" was NOT covered by S4-D2 (binary-missing only) — a dead daemon makes `docker run` exit 125 with the container never started, which would have claimed `isolation: container` for a non-run. All three decisions signed off 2026-07-05.
- `src/vestibule/backends/container.py`: (a) **Codex budget fix** — the guest-timeout path serially awaited kill (5 s) + rm (5 s) + CLI wait (5 s) after `timeout_s + 5` of collection = exactly the server's outer deadline on a wedged daemon, swallowing the honest timed-out result (same failure mode as P2, one path over). Now the timed-out result returns immediately (`exit_code: -1`), the detached finisher kills the container, and a detached `_reap_cli` collects the CLI; a CLI that wedges *after* the guest finished reports "exit code unknown", never a fake timeout. (b) **Step 5** — `soft_disabled` constructor param (omits soft flags; results report `container-degraded` + exact list), `--pull never` in the §3 profile, per-run `_preflight_image` (cached `image inspect`; missing image ⇒ `RunRefusedError` with the exact pull command, no `docker run` ever spawned), `_cli_status` refactor (exit codes for preflight; `_cli` delegates), exit-125 honesty rule (`isolation: none`, "nothing was executed"), overflow-killer kill now finisher-tracked/shielded. (c) **CRLF fix** — guest scripts are written `newline="\n"`: text mode turned `\n` into `\r\n` on Windows and CRLF breaks bash keywords in the Linux guest (`then\r` ≠ `then`); latent since step 3, exposed by the RO probe (first multi-line `if/then` bash guest). (d) **`drain()`** — bounded wait for a backend's detached finisher/reaper tasks; rejected probe candidates must be drained or event-loop shutdown mass-cancels a task mid-`create_subprocess_exec`, orphaning the spawn waiter (CPython proactor wart) and deadlocking loop close.
- `src/vestibule/backends/select.py`: new — `BackendSelector` (contract §5): lazy, lock-guarded, cached on first tool call; naive only via explicit `VESTIBULE_BACKEND=naive`; runtime resolution per D4 (`auto` prefers Docker; unknown values are legible refusals); the probe is a **real run through the normal run path** (bash workspace round-trip in `python:3.12-slim`; RO mode demands reads work AND writes fail); full profile → `container`, soft-off retry → `container-degraded`, else Blocked with the runtime's own error; probe stderr scanned for runtime WARNING lines; `note_result` drops the cached selection when a container-tier run reports `isolation: none`; rejected candidates drained.
- `src/vestibule/server.py`: `get_warden()` async ⇒ cached selection (M0 naive stub finally gone); selection runs before the outer deadline starts; outer deadline `timeout+20` → `timeout+30` (funds the preflight's bounded worst case); `SELECTOR.note_result` honesty hook after each run.
- `tests/test_lifecycle.py`: +8 — 2 budget regressions (`test_timed_out_result_returns_before_any_kill` with a wedged-daemon fake, CLI-wedge-after-EOF honesty) + 6 step-5 (preflight refusal/caching/unresponsive, soft-flag/`--pull never` command shape, exit-125 honesty, degraded detail); `quiet_backend` fixture also neutralizes preflight; `FakeCliProc` gained `exit()`.
- `tests/test_select.py`: new — 15 Docker-free selection tests (naive override, unknown backend/runtime, auto/forced runtime resolution, cooldown caching with re-check, degraded fallback order, both-probes-fail refusal, concurrent first calls share one probe, note_result invalidation rules, probe verdict criteria, server rendering of refusal + degraded detail).
- `tests/test_container.py`: timeout test now polls for the detached kill; +4 Docker-marked: real selection ⇒ `container` verdict + honest post-probe run, probe leaves no trace (no container, no workspace probe file), RO-workspace selection, and the failed-selection-drain regression (zero pending tasks after a refused selection).
- `docs/plans/M1-step4-lifecycle.md` + `docs/plans/M1-container-backend.md`: budget-fix amendment blockquotes; contract §3 gains `--pull never`, §5 the step-5 amendment blockquote, §7 the `timeout+30` note.
- `docs/HISTORY.md`: step-4 follow-up bullet, S5-D1/D2/D3 sign-offs, step-5 done bullet (incl. both build-time bugs), step-6 scope shrunk (preflight moved into step 5).
- (process) Codex adversarial review of the plan ran before sign-off (both high findings were real and fixed same-day). The task-leak hang was diagnosed live: the suite hung 42 min in pytest teardown → py-spy stack dumps showed asyncio `_cancel_all_tasks` stuck → a temporary conftest watchdog dumped still-alive tasks 10 s post-cancel and named the leaked finisher/reaper tasks (debug files deleted after the fix). Session end state: **103 tests pass in ~57 s with clean exit**, `ruff check` + `mypy` clean. User committed 3× mid-session (`522606f`, `5ce882a`, `d6a9df8`); still **uncommitted**: `container.py`, `select.py`, `test_container.py`, step-5 plan, HISTORY (the CRLF + drain fixes) — plus this CLAUDE.md entry. Note: the registered vestibule MCP server has been running since 2026-07-04 on pre-step-5 code; restart it to pick up real selection. Next: step 6 (digest pinning + setup-UX polish).

### 2026-07-04 — M1 step 4 built: run lifecycle (cancellation-safe cleanup, deadline-label reaping, concurrency cap)
- `docs/plans/M1-step4-lifecycle.md`: new — step-4 plan. v1 (age-based reaping) was rejected by a Codex adversarial review (high finding: a reaper judging foreign containers by *local* `max_timeout_s` can kill another server's legitimate longer run); v2 adopted **S4-D3 deadline-label reaping** — owner stamps `vestibule.deadline=<epoch>` (spawn + own timeout + 90s) at spawn; any reaper removes only past-deadline (+60s margin) labeled containers, any state, never on missing/garbled labels — which also fixed two v1 bugs the review surfaced (created-state race; Docker Desktop VM clock drift, since no daemon timestamps are consulted). Also S4-D1 (5s bounded semaphore wait ⇒ `RunRefusedError` ⇒ `Blocked:` message, not a queue into the outer deadline) and S4-D2 (missing runtime ⇒ `isolation: none`, never `container`). All three signed off 2026-07-04; §3.1 carries a post-implementation amendment (Codex P2, below).
- `src/vestibule/backends/base.py`: `RunRefusedError` — typed "refused before anything ran" exception; step 5 will reuse it for hard-tier probe failures.
- `src/vestibule/backends/container.py`: step-4 lifecycle. `run()` split into orchestration + `_execute()` (step-3 body); lazy `asyncio.Semaphore(max_concurrent)` with bounded wait; `vestibule.deadline`/`vestibule.owner` labels; cleanup (`kill` → `rm -f` → rmtree, each 5s-bounded via new `_cli` helper) runs as a **detached finisher task** that survives request cancellation, never delays the result (Codex P2 fix), and owns the semaphore slot until the container is really gone — so `max_concurrent` bounds *existing* containers; finisher refs held in a set against task GC. Reaper: detached deduplicated pass on first use + after each run — one `ps -aq --filter label`, one batched id-anchored `inspect`, one batched `rm -f`; keep/remove is the pure `_reap_decision` (in-flight names protected; never reap on bad data). S4-D2 honesty fix on the spawn-failure path.
- `src/vestibule/server.py`: catches `RunRefusedError` around `warden.run` ⇒ `Blocked:` content (handlers still never raise).
- `tests/test_lifecycle.py`: new — 13 Docker-free tests: semaphore cap, busy refusal + server rendering, 7-case `_reap_decision` table, cancellation releases slot/active name, label emission, and the Codex-P2 regression (result returns while cleanup blocked; slot held until it finishes).
- `tests/test_container.py`: existing 9 tests moved onto an async `backend` fixture that drains detached finisher/reap tasks (timeout no-survivor check now owner-label-scoped); +5 Docker-marked lifecycle tests: cancel mid-run leaves no container/tmpdir, epoch-1970 deadline reaped *while running*, future deadline spared, unlabeled spared + warned, 4 simultaneous runs distinct + successful.
- `docs/plans/M1-container-backend.md`: §3 profile shows the two new labels; §4 amendment blockquote (S4-D1, S4-D3, P2).
- `docs/HISTORY.md`: S4-D1/D2/D3 sign-off bullets + step-4 done bullet + P2 post-review note; includes the one-time note that pre-step-4 containers lack deadline labels (manual `docker rm` for old dev leftovers).
- (process) Both Codex passes ran this session: adversarial review of the plan (v1 → v2 redesign) and standard review of the diff (P2: shield-awaited cleanup could push a timed-out run past the server's `timeout_s + 20` outer deadline on a slow daemon, swallowing the honest result — fixed by full detachment + slot handoff). Session end state: 76 tests pass, `ruff check` + `mypy` clean; **uncommitted** (user commits himself; `docs/PLAN.md` also holds unrelated user formatter churn with two mangled italics — keep it out of the step-4 commit). Next: step 5 (capability probing, tiered backend selection, honest reporting).

### 2026-07-03 — README value-prop rework; M1 D1–D4 signed off; M1 steps 1–3 built
- `README.md`: new lead ("Run untrusted AI-agent code safely — on your own machine, for free, with nothing sent to a cloud") + new `## Why this exists` section (problem → cloud alternatives → local/free/no-cloud bullets), placed before "What it is"; old threat-model paragraph absorbed into it.
- `docs/HISTORY.md`: new — plain-language running build log (user-requested process rule: append one bullet per decision sign-off and per completed build step, under each milestone). M0, D1–D4, and steps 1–3 logged.
- `docs/plans/M1-container-backend.md`: status draft → **signed off**; D1–D4 all approved by the user 2026-07-03 (D1 writable workspace, D2 pinned official images/no auto-pull, D3 hard/soft degradation tiers, D4 Docker-first runtime-agnostic).
- `src/vestibule/config.py`: M1 step 1 — new env-overridable `Limits` fields (workspace dir/RO, runtime, backend, image refs [digests pinned in step 6], max_concurrent, tmpfs_mb) + `workspace_path` property + `_s`/`_b` env getters.
- `src/vestibule/workspace.py`: new (step 1) — `read_workspace` path jail: lexical rejection (`..`, absolute, `:`/ADS/drive letters, UNC/device prefixes, NUL, reserved device names, trailing dots/spaces) before any filesystem access, then a component walk refusing symlinks/reparse points, `commonpath` containment, `O_NOFOLLOW` opens; dir listing (200-entry cap) + file read (byte-capped) + `Not found` as content.
- `src/vestibule/backends/base.py`: step 2 — `RunResult.isolation_detail` added; isolation enum comment gains `container-degraded`.
- `src/vestibule/server.py`: step 2 — validation made total (type-checked `language`/`code`/`timeout_seconds`, bool excluded, `Blocked:` never exceptions); `read_workspace` tool wired (jail-backed, `asyncio.to_thread`); outer deadline `timeout_s + 20`; `run_code` description states the workspace is writable; `_format_result` renders `isolation_detail`; `call_tool` tolerates `None` arguments.
- `src/vestibule/backends/container.py`: new (step 3) — `ContainerBackend` happy path: full §3 profile (`--network none`, `--cap-drop ALL`, no-new-privileges, `--read-only`, non-root user, mem=swap, cpus, pids-limit, capped tmpfs, no env inheritance, `--rm --init`, labels), D9 read-only `/sandbox` script mount, D10 DEVNULL stdin, streaming collection capped at 2× display limit with early container kill, timeout kills the container (then `rm -f`), never just the CLI. Full §4 lifecycle (shielded cleanup, orphan reaping, semaphore) deferred to step 4.
- `tests/test_workspace.py`: new — 24-case jail suite incl. live symlink attacks (symlinks work on this host).
- `tests/test_validation.py`: new — malformed-arg suite (criterion 13) + `read_workspace` handler tests via monkeypatched `LIMITS`.
- `tests/test_container.py`: new — 9 Docker-marked tests, all ran against the real daemon (3-language hello, workspace persistence, network gone, rootfs read-only, timeout leaves no survivor, output flood capped); auto-skip without a daemon.
- `pyproject.toml`: registered the `docker` pytest marker.
- (env) Docker daemon verified 28.5.1; `python:3.12-slim` + `node:22-slim` pulled locally. Session end state: 58 tests pass, `ruff check` + `mypy` clean (note: `ruff format` was never adopted repo-wide; the gate is `ruff check`). Next: step 4 (timeout/cleanup lifecycle, orphan reaping, semaphore).

### 2026-07-02 — M0 accepted; M1 adversarially reviewed and planned
- (no code) M0 live-agent acceptance passed: fresh session ran `print("hi from vestibule")` via `run_code` over real MCP stdio — `exit_code: 0`, `isolation: none`. M0 formally done.
- `docs/reviews/M1-codex-adversarial-review.md`: new — Codex adversarial review of the M1 design (20 ranked findings; verdict: sound with amendments, no structural rework).
- `docs/plans/M1-container-backend.md`: new — full M1 contract (exact container profile, run lifecycle, capability probing, `read_workspace` jail, 14 acceptance criteria, 7-step implementation order). Decisions D1–D4 marked provisional pending user sign-off.
- `docs/PLAN.md`: Milestone 1 section now points to the M1 contract; its acceptance criteria supersede the original list.
- `CLAUDE.md`: added Workflow Rules and this Changelog section.
