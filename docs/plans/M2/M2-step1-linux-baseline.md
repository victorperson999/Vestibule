# M2 step 1 — Linux baseline (1a) and POSIX shutdown hand-off (1b)

Status: **DRAFT v3 (2026-10-14)**, waiting on Victor's sign-off.
- **Reviews:** `skeptic` reviewed v1 ("rework needed") and v2 ("sound with amendments");
  `verifier` confirmed every claim that drove the rework. The record is in §7.
- **Victor decided 2026-10-14:**
  - S1-D1: a version range plus a constraints file.
  - S1-D5: a shutdown hand-off on POSIX, with Windows keeping M1's in-process cleanup. This
    revises the first S1-D5 after the v2 review proved a detached cleaner can't outlive a
    Windows session.

Contract: [`M2-native-warden.md`](M2-native-warden.md) §10 step 1. Two commits, like M1's
7a/7b:
- **1a, tooling:** pins, the two-platform gate, a Python 3.11 Linux venv, line-ending rules.
  No `src/` change.
- **1b, POSIX shutdown:** closes the BUILD_LOG `SIGTERM` issue for the clients that really
  end Linux servers.

## 1. What the investigation found (2026-10-14, before any code)

### 1.1 Linux is fine; fresh installs are not

- **Every platform:** `pip install -e ".[dev]"` resolves mcp **2.3.0**, ruff 0.17.0 and mypy
  2.4.0 today. With mcp 2.3.0 the server doesn't import (`Server` has no `list_tools`;
  `inputSchema` is now `input_schema`). ruff 0.17.0 adds 31 findings.
- **A range alone doesn't pin:** `mcp>=1.28.1,<2` resolves to 1.30.0. The shutdown-relevant
  SDK modules are identical in 1.28.1 and 1.30.0 (review check), so the published range adds
  no shutdown risk today.
- **The two venvs already differ:** pydantic 2.13.4 vs 2.14.1, starlette 1.3.1 vs 1.8.0, and
  about 20 more. The WSL venv still holds leftovers from the 2.3.0 install (`mcp-types`,
  `httpx2`, …), and the Windows venv has `py-spy`, a debugging tool that isn't a dependency.
- **With mcp, ruff and mypy matched,** Linux passes everything: ruff, mypy, **240/240 tests
  in 75 s**, Docker tests included.
- **Every pinned version has Linux x86_64 wheels for CPython 3.11 and 3.14.**

### 1.2 How real clients end a stdio server (verified)

| Client | Sequence | Time before a hard kill | Job Object? |
|---|---|---|---|
| **Claude Code, POSIX** (2.1.300) | `SIGINT` to the **PID only**, stdin left open → 100 ms → `SIGTERM` → 400 ms → `SIGKILL` | **100 ms** | none on POSIX |
| **Claude Code, Windows** | close stdin → 300 ms → `taskkill /PID /T /F` | **300 ms** | yes: the measured server chain is entirely `in_job` (the console-script launcher and venv redirector create kill-on-close jobs) |
| MCP Python SDK, POSIX (1.28.1) | close stdin → 2 s → `killpg` `SIGTERM` (the server gets its own session) → 2 s → `SIGKILL` | 2 s | none on POSIX |
| MCP Python SDK, Windows | close stdin → 2 s → `TerminateJobObject` | 2 s | yes: kill-on-close, **no breakaway**. The job closes when the SDK's `Process` is freed, killing **any** process the server spawned, every session (review experiment) |

**Consequence:** on POSIX a process started with `start_new_session=True` escapes every
kill above. On Windows no child of the server reliably outlives the session.

### 1.3 What today's server does

- **Claude Code on Linux:** the server **hangs on `SIGINT`** while stdin is open (alive at
  2.0 s, verified). The SDK's stdio task group waits on a worker thread blocked in
  `readline`. The `SIGKILL` at 500 ms then ends it without cleanup, so an in-flight container
  lives until its S7-D5 self-deadline (`timeout_s + 90` s).
- **Claude Code on Windows:** S7-D1's in-process cleanup (0.2–0.4 s with a run in flight) can
  overrun the 300 ms window, and the tree kill takes the `docker rm -f` with it. **Not fixed
  by this step** (S1-D5 says why); it stays a documented gap with S7-D5 as the backstop.
- **Python SDK on either platform:** fits today (2 s).

## 2. Decisions

### S1-D1 (Victor, 2026-10-14) — A range in `pyproject.toml`, plus a constraints file

- **`pyproject.toml`** states what users get:
  - `mcp>=1.28.1,<2`;
  - dev tools within compatible ranges: `ruff>=0.15.20,<0.16`, `mypy>=2.1,<2.2`,
    `pytest>=9.1,<10`, `pytest-asyncio>=1.4,<2`.
- **`requirements/constraints.txt`** pins every package exactly, transitive ones included.
  - **Generated** in a fresh throwaway venv: `pip install -e ".[dev]"` with the Windows
    venv's current versions as bootstrap constraints, then `pip freeze --exclude-editable`.
    Leftovers and `py-spy` never get in.
  - The Windows-only `pywin32` and `colorama` lines stay: pip ignores constraints for
    packages it isn't installing.
  - A header says how to regenerate it. Regenerating is a deliberate commit.
- **`scripts/check_env.py`** (stdlib, both platforms), run first by both gates:
  - **every installed distribution** must appear in the constraints at **exactly** that
    version;
  - except the project itself, `pip`, and a short, commented allowlist of local debugging
    tools (`py-spy`);
  - a constrained package that isn't installed is fine (that's how the Windows-only lines
    work on Linux).
- **The MCP SDK 2.x migration** becomes a BUILD_LOG known issue, due before M4's PyPI release.

### S1-D2 (fixed) — Python 3.11 on Linux from step 1

- **Why:** native code only ever runs on Linux, and M1's one real shutdown race was
  3.11-specific.
- **The venv:** 1a builds `~/.venvs/vestibule311` from a `uv python install 3.11`
  interpreter. That's a standalone build with no sudo; `uv` lives in a tools venv
  (`pip install uv`).
- **The Linux gate runs:**
  - the full suite on 3.14;
  - on 3.11, `pytest -m "not docker"` **plus `tests/test_stdio_e2e.py`**, so the shutdown
    end-to-end tests run on 3.11 too.
- **mypy:** `[tool.mypy] python_version = "3.11"` makes it flag stdlib APIs newer than 3.11
  on both platforms.

### S1-D3 (fixed) — The two-platform gate

- **New `scripts/gate-linux.sh`** (LF):
  - **`--setup`** recreates both Linux venvs from scratch (`--clear`), so stale packages
    can't survive. pip never uninstalls, so a "refresh" would keep the leftovers.
  - **Every run:** `check_env.py` on both venvs; `ruff check . --no-cache`; `mypy src` with
    an outside `--cache-dir`; the pytest runs with `-p no:cacheprovider` and an outside
    `PYTHONPYCACHEPREFIX`.
  - **Fails if any `docker`-marked test skipped on the 3.14 run** (1a), and, from 1b on, if
    `tests/test_signals.py` skipped. Missing Docker integration can't produce a quiet green.
- **`.claude/agents/gate.md`:** `check_env.py` and the Windows gate, then
  `MSYS_NO_PATHCONV=1 wsl.exe -d Ubuntu -- bash /mnt/c/…/scripts/gate-linux.sh`. Run
  **sequentially** (one Docker Desktop daemon); each platform reported separately.

### S1-D4 (fixed) — Line endings

`.gitattributes`: `*.sh text eol=lf`, and `tests/fixtures/** -text`. Step 2 asserts sha256
values on its fixtures, and `core.autocrlf=true` would otherwise change their bytes on the
Windows checkout.

### S1-D5 (Victor, 2026-10-14, revised) — POSIX: hand off, then exit fast. Windows: M1's path

**On POSIX** (Linux; macOS takes the same path but isn't tested), any trigger runs one
synchronous path: stdin EOF, `SIGINT`, `SIGTERM` or `SIGHUP`.

**How the triggers reach it:**
- Signals use `loop.add_signal_handler`, installed first thing in `_main`, so they run on the
  loop thread, never between bytecodes.
- **EOF is caught inside a stdin wrapper** passed to `stdio_server(stdin=…)`, so the path runs
  *before* the SDK sees EOF. That matters: if the SDK sees EOF first, it cancels in-flight
  handlers, and their finishers spawn their own `docker kill`/`rm` (`container.py:526-534`).
  Those would race the hand-off, and the tests couldn't tell who removed what.

**The path:** wrapped in `try: … finally: os._exit(0)`, with no `await` anywhere. Nothing can
interleave, and no request, run or selection resumes after step 1.
1. **Snapshot:** `SELECTOR.cleanup_job()` collects, across every tracked backend (dropped and
   candidate ones included):
   - each runtime as an argv prefix, with its owner tokens and live container names;
   - every per-run temp dir;
   - the tracked `docker run` CLI processes.

   This needs new bookkeeping: run name → (temp dir, CLI process). Native runs (steps 3+)
   contribute only temp dirs: the PDEATHSIG chain ends their processes, and systemd removes
   their scopes.
2. **Kill the tracked `docker run` CLIs.** It's instant, and it narrows late container
   creation to requests already at the daemon.
3. **If there are names or temp dirs, start the cleaner:**
   `[sys.executable, "-I", "-B", "-m", "vestibule.cleanup", <job JSON>]`, with:
   - `start_new_session=True`;
   - `stdin`, `stdout` and `stderr` all `DEVNULL` (golden rule 1);
   - `cwd=tempfile.gettempdir()`.

   `-B` because `-I` drops `PYTHONPYCACHEPREFIX`: no `.pyc` lands in the repo. An idle
   shutdown starts nothing.
4. **One log line** via `os.write(2, …)`, then `os._exit(0)`. stdout is never touched: a
   message the SDK's writer thread is writing at that instant can be cut short. That's the
   accepted residual; the client has already ended the session.

- **Target:** exit within ~50 ms of the trigger, inside Claude Code's 100 ms.
- **The cleaner** (`src/vestibule/cleanup.py`, stdlib only, never raises, total bound ~30 s):
  1. `rm -f <names>`, then an owner-label sweep (`ps -aq --filter label=vestibule.owner=<t>`
     → `rm -f`);
  2. **remove the temp dirs right away,** so a container created late can't find its script;
  3. owner sweeps again at +5 s and +20 s;
  4. remove the temp dirs once more (Docker Desktop can re-create bind sources; M1 7b
     finding).
  - Every CLI call goes through `Popen` + `communicate(timeout=5)` + kill + a bounded
    wait. `subprocess.run(timeout=)` can hang on Windows-style grandchildren.
  - It writes nothing on success. On a failure it leaves `vestibule-cleanup-<pid>.log` in the
    temp dir.
  - **`VESTIBULE_CLEANUP_TRACE=<file>`** (diagnostic) makes it record each action. Tests use
    this to prove *the cleaner*, not `--rm` or a finisher, removed the container.
- **On Windows: no change.** M1's S7-D1 in-process shutdown stays: on stdin EOF,
  `SELECTOR.aclose()`, one batched `rm -f` within 1.5 s and a non-cancelling wait of ≤ 0.5 s.
  It fits the Python SDK's 2 s. Claude Code's 300 ms window stays a **documented gap**,
  backstopped by S7-D5 (BUILD_LOG).
- **Structural fix for later (BUILD_LOG known issue):** a container-side liveness lease. A
  watchdog in each container ends the guest when the server's heartbeat goes stale, on every
  platform, for any kind of server death.
- **Exit codes:** 0 for EOF and the handled signals. A signal that arrives before the handlers
  are installed (during imports) takes its default action.

## 3. Changes

### 1a — tooling (no `src/` change)

| File | Change |
|---|---|
| `pyproject.toml` | S1-D1 ranges; `[tool.mypy] python_version = "3.11"` |
| `requirements/constraints.txt` | new (S1-D1) |
| `scripts/check_env.py` | new (S1-D1) |
| `scripts/gate-linux.sh` | new (S1-D2, S1-D3) |
| `.gitattributes` | S1-D4 |
| `.claude/agents/gate.md` | two platforms, sequential, `check_env.py` first |
| `docs/GETTING_STARTED.md` | dev install with `-c requirements/constraints.txt`; a "Linux / WSL development" subsection |
| `docs/BUILD_LOG.md`, `docs/HISTORY.md` | the 1a entry; **known issue: MCP SDK 2.x migration** |

### 1b — POSIX shutdown

| File | Change |
|---|---|
| `src/vestibule/server.py` | the stdin EOF wrapper and signal handlers (POSIX); the synchronous path; Windows keeps `_shutdown` |
| `src/vestibule/cleanup.py` | new: the cleaner |
| `src/vestibule/backends/select.py` | `cleanup_job()` (alongside `aclose()`, which Windows keeps using) |
| `src/vestibule/backends/container.py` | name → (temp dir, CLI process) bookkeeping |
| `tests/test_signals.py` | new: POSIX, **no Docker**: idle server, each trigger ⇒ prompt exit 0, clean stdout, no cleaner started |
| `tests/test_cleanup.py` | new: the cleaner against a fake runtime `[sys.executable, fake_runtime.py]` (all platforms) |
| `tests/test_shutdown.py` | new: unit tests of the synchronous path (all platforms, monkeypatched) |
| `tests/test_stdio_e2e.py` | phase-aware client emulation; the POSIX client sequences (§4.2) |
| `docs/plans/M2/M2-native-warden.md` | **amendment:** §4 step 5, §7's `select.py` bullet and §10 step 6 now point to S1-D5. Native runs need no shutdown work beyond temp dirs. |
| `docs/plans/M1/M1-container-backend.md` | §4 amendment: on POSIX, S7-D1 is replaced by S1-D5 |
| `docs/BUILD_LOG.md`, `docs/HISTORY.md` | the `SIGTERM` issue **closed: Linux verified, macOS untested**; new known issues (the Windows Claude Code 300 ms gap, the liveness lease); the 1b entry |

## 4. Tests

### 4.1 Unit (all platforms)

- `cleanup_job()` collects names, owners, temp dirs and CLI handles from every tracked
  backend.
- **The synchronous path**, with `Popen`, `os._exit` and the CLI kills monkeypatched:
  - kills the tracked CLIs;
  - starts the cleaner only when there is work, with `start_new_session`, `-I -B`, `DEVNULL`
    and the temp-dir cwd;
  - calls `os._exit(0)` exactly once, **even if a step raises**, and never touches stdout;
  - a second trigger, or a trigger after EOF, does nothing.
- **The cleaner, against the fake runtime**, with the sweep delays shortened through the job:
  - the order: names, sweep, temp dirs, the later sweeps, temp dirs again;
  - a hanging fake call is killed at its bound;
  - a failure leaves the log and never raises;
  - the trace file records each action.

### 4.2 End to end with Docker (POSIX; Linux 3.14 and 3.11)

The guest **ignores `SIGTERM`**, so `docker run`'s signal proxy and `--rm` can't remove it.
Each test checks:
- (a) the server exited in the client's **first phase**, which the harness records;
- (b) the container is gone within 30 s, by exact name;
- (c) **the cleaner's trace shows it removed that name**;
- (d) every stdout line is valid JSON-RPC.

Sequences:
- **Claude Code POSIX:** `SIGINT` to the PID with stdin open → 100 ms → `SIGTERM` → 400 ms →
  `SIGKILL`.
- **The real MCP Python SDK client** (`mcp.client.stdio.stdio_client`): close stdin, and the
  SDK's own escalation.

On Windows the existing shutdown tests keep their current assertions, with phase-aware
closing.

### 4.3 End to end without Docker (`tests/test_signals.py`, POSIX)

An idle server receiving `SIGINT`, `SIGTERM`, `SIGHUP` or EOF, with stdin open where it
applies: exits 0 inside the first phase, with clean stdout, and starts no cleaner (no trace
file). `gate-linux.sh` fails if these skipped.

### 4.4 Done when

- **Gates:** both platform gates are green, including the 3.11 Linux run. `check_env` passes
  on all three venvs, and a fresh `gate-linux.sh --setup` reproduces the pins and passes.
- **Review:** `critic` and `verifier` have no open CONFIRMED high/med findings for each of
  1a and 1b.
- **Docs:** updated, including the M2 contract amendment.

## 5. Edge cases

- **A trigger during imports:** the default action, because nothing is tracked yet.
- **Repeated or mixed triggers:** the first one wins; the path ends in `os._exit`.
- **Cleaner spawn fails, or any step raises:** the `finally` still exits 0. The CLIs were
  already killed, and S7-D5 is the backstop for containers.
- **Several runtimes tracked:** one job, grouped by runtime argv.
- **A selection probe in flight:** its candidate backend is tracked (S7-D1), so its name and
  CLI are in the job.
- **Naive backend** (dev only): nothing to hand off; its child processes are left to the OS,
  as today.

## 6. Carried forward

- The MCP SDK 2.x migration (BUILD_LOG, before M4).
- The container liveness lease (BUILD_LOG; it would close the Windows Claude Code gap).
- **Step 3:** the A4 warden helper is a real module under `src/`, so ruff and mypy check it.
- **Step 7's live check:** confirm that Claude Code inside WSL takes the POSIX branch.

## 7. Review record

**Round 1** (`skeptic` on v1, "rework needed"; claims confirmed by `verifier`):

| # | Sev. | Finding | Disposition |
|---|---|---|---|
| 1 | high | Designed for the Python SDK, not Claude Code (`SIGINT`, 100 ms); today's server hangs on `SIGINT` | S1-D5 |
| 2 | med | Windows Claude Code allows 300 ms, not 2 s | Documented gap; the liveness lease is the later fix (Victor, S1-D5 revision) |
| 3 | med | Cleanup next to a live dispatcher | Synchronous path; EOF caught before the SDK cancels handlers |
| 4 | med | Only one trigger order | Synchronous; both orders unit-tested |
| 5 | med | Tests can't fail | §4.2: `SIGTERM`-ignoring guest, phase check, cleaner trace |
| 6 | med | The range doesn't pin; transitive drift | S1-D1 constraints + `check_env` |
| 7 | med | 3.11 Linux deferred | S1-D2 |
| 8 | low | Signal tests gated on Docker | §4.3 Docker-free module; skip guards |
| 9 | low | `.ruff_cache` in the repo | `--no-cache` |
| 10 | low | No budget margin | Moot on POSIX; Windows unchanged |
| 11 | low | "Closed" should say Linux only | BUILD_LOG wording |
| 12 | low | sha256 fixtures CRLF-converted | `tests/fixtures/** -text` |

**Round 2** (`skeptic` on v2, "sound with amendments"):

| # | Sev. | Finding | Disposition |
|---|---|---|---|
| 1 | high | On Windows the cleaner dies with the SDK's kill-on-close job on every session, and the breakaway attempt reports nothing (experiment) | Main session measured that Claude Code's Windows chain is in jobs too. **Victor revised S1-D5:** POSIX hand-off, Windows keeps S7-D1 |
| 2 | med | `DETACHED_PROCESS` shows a console window | Moot (no Windows cleaner); the cleaner starts only when there is work |
| 3 | med | EOF reaches the SDK first: cancelled runs' finishers remove containers, so tests can pass on a broken cleaner | EOF stdin wrapper; `VESTIBULE_CLEANUP_TRACE` proof in every test |
| 4 | med | The skip guard watched the wrong tests; 1a/1b order | Guards on `docker`-marked tests (1a) and `test_signals.py` (1b) |
| 5 | med | The M2 contract still describes the old shutdown | Contract amendment in 1b |
| 6 | low-med | Temp dirs removed last; orphan CLIs left alive | Kill CLIs in the synchronous path; remove temp dirs after pass 1 and at the end |
| 7 | low-med | `check_env`'s direction unspecified; venv leftovers; `py-spy` | Defined (installed ⊆ constraints, exact versions, allowlist); `--setup --clear`; generated from a fresh venv |
| 8 | low | `-I` drops `PYTHONPYCACHEPREFIX` | `-I -B` |
| 9 | low | `pending_tasks` is in use (`idle`, `drain`) | Kept; nothing removed (Windows keeps `aclose`) |
| 10 | low | Handler robustness, exit-code claim | `add_signal_handler`, `try/finally os._exit(0)`, `os.write(2, …)`; §5 exit codes corrected |
| 11 | low | Windows timing and bounded calls in tests | Windows path unchanged; the cleaner uses `Popen` + `communicate(timeout)`; the fake runtime is a Python argv |
| 12 | low | The cleaner's cwd locks the project folder | `cwd=tempfile.gettempdir()` |
| 13 | low | The shutdown e2e never runs on 3.11 | The 3.11 run includes `test_stdio_e2e.py` |
