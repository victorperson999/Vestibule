# Vestibule — Build log

A lookup index for backtracking and bug-fixing: for every finished build step, **when** it
landed, **which commits** carry it, **which plan** explains it, **what files** it touched,
**which tests guard it**, **what broke along the way** (and how it was fixed), and **where to
start** if it breaks again.

How this differs from the other records:
- [`docs/HISTORY.md`](HISTORY.md) is the plain-language story: decisions, sign-offs, why.
- `docs/plans/*.md` are the contracts: the full reasoning for each step.
- This file is the index you open when something regresses. Entries are newest first.

**Keeping it current** (part of loop step 9 in `.claude/WORKFLOW.md`): when a step lands,
add an entry. The commit hash only exists after Victor commits, so an entry starts as
`pending` and Claude fills in the hash from `git log` at the start of the next session.

**Using it on a regression:**
1. Find the step that owns the behavior (status table, then the entry's *Files*).
2. Run its guarding tests: `.venv/Scripts/python.exe -m pytest -q <file>::<test>`.
3. See what changed since: `git log --oneline <commit>..HEAD -- <file>`.
4. Read the plan's decision before changing the behavior; most rules here exist because
   a review found a concrete failure.

---

## Status at a glance

| Milestone / step | What | Landed | Commit(s) | Tests after |
|---|---|---|---|---|
| M0 | MCP server + naive backend (no isolation) | 2026-07-02 | `aabd935` | 4 |
| M1 step 1 | Config additions + `read_workspace` path jail | 2026-07-03 | `9f43762` | — |
| M1 step 2 | Total arg validation, `read_workspace` tool, `isolation_detail` | 2026-07-03 | `ec467f7` | 49 |
| M1 step 3 | `ContainerBackend` happy path | 2026-07-03 | `4f765e9` | 58 |
| M1 step 4 | Run lifecycle: cleanup, reaping, concurrency cap | 2026-07-04 | `b4019a7` (+ fix in `5ce882a`) | 78 |
| M1 step 5 | Backend selection + capability probing | 2026-07-05 | `522606f` `5ce882a` `d6a9df8` `7ebd28b` | 103 (104 after `99a7f99`) |
| M1 step 6 | Digest-pinned images + setup UX | 2026-10-10 | `a0827fd` | 122 |
| M1 step 7a | Shutdown & lifetime hardening, verified limits | 2026-10-11 | `938056f` | 175 |
| M1 step 7b | Acceptance suite (14 criteria) + docs | 2026-10-14 | `3c2dab9` | 197 |
| M1 accepted | Live-agent check + status docs (no code) | 2026-10-14 | `e7aaae5` | 197 |
| M1 follow-up | Config validation + server version | 2026-10-14 | `9a4f15e` | 240 |
| M2 step 0 | Spike: kernel facts for N1–N5 (no code) | 2026-10-14 | `4509d35` | 240 |
| M2 step 1a | Linux baseline: version pins, constraints, two-platform gate | 2026-10-15 | `d811021` (with 1b) | 240 (+201 / +3 on Linux 3.11) |
| M2 step 1b | POSIX shutdown hand-off (detached cleaner) | 2026-10-15 | `d811021` (with 1a) | 261 Linux (0 skipped); 252 + 9 POSIX-only skips on Windows |

## Known open issues

| Found | Where | Issue | Owner |
|---|---|---|---|
| 2026-10-11 (7a review) | `container.py` run CLI | If the *client* kills the process tree in the ~100 ms between `docker run`'s create and start, the container stays **Created** (never started, no CPU or memory use) until a reaper pass (deadline + 60 s). `--rm` and S7-D5 only act on started containers. | Accepted residual; reaper backstops |
| 2026-10-11 (S7-D5) | container profile | The self-deadline `timeout` runs as the guest's uid, so a *deliberately* malicious guest can `SIGSTOP` it and live until a reaper finds it, still capped by memory, CPU and pids unless its selection was `container-degraded`, which has only the limits it reported. | Documented residual (M4 `SECURITY.md`) |
| 2026-10-11 (S7-D1) | server hard exit | After a hard kill (Job Object / TerminateProcess / SIGKILL), per-run `%TEMP%\vestibule-run-*` script dirs are not removed. A startup sweep was rejected because it can't tell whose live run a dir belongs to. | M3/M4 candidate |
| 2026-10-11 (S7-D2) | workspace | Guest-made symlinks are real host symlinks that host file tools follow, for reads and writes. `read_workspace` refuses them, and the tool description warns. A structural fix (seccomp `symlink` denial) is noted for M2. | Documented (README, contract D1); M2 seccomp / M4 |
| 2026-10-15 (M2 step 1b) | shutdown, Windows | Claude Code on Windows closes stdin, waits 300 ms, then `taskkill /T /F`s the server's tree. With a run in flight, M1's in-process cleanup (S7-D1, 0.2–0.4 s) can overrun that window, and the tree kill takes the `docker rm -f` with it, so the container lives until its S7-D5 self-deadline (`timeout + 90` s). A detached cleaner can't help on Windows: the server sits in kill-on-close Job Objects (the console-script launcher's, the venv redirector's, the Python SDK client's). The structural fix is a container-side liveness lease (a watchdog that ends the guest when the server's heartbeat goes stale), which would also cover a server killed outright on any platform. | Later milestone (liveness lease); M2 step 7 re-measures with a real session |
| 2026-10-14 (M2 step 1) | dependencies | `pyproject.toml` pins `mcp<2`. mcp 2.x (2.3.0 on 2026-10-14) replaced the low-level `Server` API this server uses (`list_tools`, `Tool(inputSchema=)`), so an unpinned install could not start. Moving to 2.x needs its own plan: it touches the protocol layer and the shutdown rules. | Before M4's PyPI release |
| 2026-10-14 (7b docs) | workspace | The workspace has no size cap. The rootfs is read-only and `/tmp` is capped, but a run can fill the host disk through `/workspace`. By design for M1 (D1). Documented in the README's "Know the limits". | Documented; revisit with M3 quotas |

**Closed:**
- The 2026-10-11 `SIGTERM` issue (a POSIX client that signals without closing stdin first
  skipped cleanup) was closed in M2 step 1b, **Linux verified, macOS untested.** The real
  case was worse than recorded: Claude Code on Linux sends `SIGINT`, then `SIGTERM` 100 ms
  later, then `SIGKILL`, and the server *hung* on that `SIGINT`. Now every trigger runs the
  hand-off and the server exits within milliseconds (S1-D5).
- The 2026-10-10 issue (dropped and live backends never drained; cleanup cut off at
  shutdown) was fixed in step 7a (S7-D1). It was reproduced first as an indefinite
  shutdown hang.
- The 2026-10-14 config-validation issue (bad values silently replaced by the default, `0`
  accepted, `²` crashing startup) and the server-version issue (`1.28.1` reported) were
  fixed in the M1 follow-up below.

---

## M2 step 1b — POSIX shutdown hand-off (2026-10-15)

- **Commit:** `d811021` (one commit with 1a, made 2026-10-24).
- **Plan:** [`docs/plans/M2/M2-step1-linux-baseline.md`](plans/M2/M2-step1-linux-baseline.md)
  S1-D5 (revised by Victor after the second plan review); contract amendments in M1 §4 and
  M2 §4.
- **What:**
  - **One synchronous path on POSIX** for stdin EOF, `SIGINT`, `SIGTERM` and `SIGHUP`
    (`server._hand_off_and_exit`):
    1. `SELECTOR.cleanup_job()` snapshots names, owner tokens, temp dirs and live `run`
       CLIs;
    2. the CLIs are killed;
    3. `vestibule.cleanup` starts in its own session (`-I -B`, stdio `/dev/null`, cwd =
       temp dir);
    4. one stderr line, then `os._exit(0)`.
  - **Signals** go through `loop.add_signal_handler`; **EOF** is caught by a stdin wrapper
    before the SDK sees it.
  - **The cleaner:** `rm -f` by name, owner-label sweeps (now, +5 s, +20 s), and temp-dir
    removal after the first pass and at the end. Each call is bounded; on failure it leaves
    a log in the temp dir. `VESTIBULE_CLEANUP_TRACE` records its actions.
  - **Windows unchanged** (S7-D1).
- **Files:**
  - Source: `server.py` (`_main` split by platform, `_hand_off_and_exit`, `_spawn_cleaner`,
    `_HandOffOnEof`), `cleanup.py` (new), `backends/select.py` (`CleanupJob`,
    `cleanup_job`), `backends/container.py` (`_RunRecord`, `_runs`, `owner`,
    `cleanup_snapshot`, `_forget_run`).
  - Tests: `stdioutil.py` (new: the harness moved out of `test_stdio_e2e.py`, with
    phase-aware closers for each client's real sequence), `test_signals.py`,
    `test_cleanup.py`, `test_shutdown.py` (new), `test_stdio_e2e.py`, `test_config.py`.
- **Guarded by:**
  - `test_stdio_e2e.py::test_stdio_claude_code_sequence_hands_off_a_run_in_flight` and
    `::test_stdio_real_sdk_client_hands_off_a_run_in_flight`: a `SIGTERM`-ignoring guest; the
    server exits in the first phase; the cleaner's trace proves it removed the container.
  - `test_signals.py`: each trigger on an idle server; Docker-free, POSIX.
  - `test_shutdown.py`, `test_cleanup.py`: units.
- **Broke along the way:**
  - The plan went through two `skeptic` rounds. The first found that the target client
    (Claude Code on Linux) sends `SIGINT` with stdin open, which hung the old server.
  - The second showed a detached cleaner dies with Windows Job Objects, so Windows keeps
    S7-D1.
  - During the build, `test_config.py` still called the harness's old
    `close_like_a_client`; the Linux gate caught it.
  - **Critic:** 2 medium and 3 low issues, all confirmed by `verifier` (reproduced against
    the real daemon) and fixed:
    - **The cleaner as `-m vestibule.cleanup` under `-I` failed silently** when the package
      was importable only through `PYTHONPATH` or a `--user` install: the container stayed
      up, and no log was written. Fix: it now runs by file path (it is stdlib-only); the
      test runs it with the base interpreter, which can't import the package.
    - **Naive guests outlived the server on EOF.** They are now on the kill list.
    - **The trace's exit code could not prove removal** (`rm -f` of a missing container
      exits 0). The trace now records stdout, the check requires the name there, and the
      hand-off e2e tests check the trigger label.
    - **An unexpected exception after the spawn leaked the run record,** so every later
      shutdown started a cleaner. It now reaps the CLI, and the snapshot retires records
      whose CLI has exited.
    - **A CLI still inside `create_subprocess_exec` isn't killed.** Documented gap (the
      owner sweeps still remove its container).
  - **Critic, round 2** (on the fixes): 1 medium and 3 low issues, all confirmed and fixed:
    - a *cancelled* naive guest was dropped from the kill list while still running. Its
      run now kills it, and each guest runs in its own process group, so its children
      die too (a bash guest's `sleep`);
    - two of the new code paths had no test that drives them for real. Real naive-run tests
      and a lifecycle test for the unexpected-error reap now do;
    - the happy-path and first-probe e2e tests now check the trigger label as well.
  - **Linux gate finding (M1 gap, first seen here):** in
    `test_run_cancelled_at_spawn_leaves_no_container`, two run dirs survived. A late
    container re-creates its deleted bind source as **root**, and in the sticky `/tmp` the
    user can't delete that. On POSIX, run dirs now live in a private, user-owned
    `vestibule-runs-*` parent, which is removed once its last run retires.
    - Leftovers from runs before this fix (root-owned `/tmp/vestibule-run-*`) need
      `sudo rm -rf` or a reboot.

---

## M2 step 1a — Linux baseline (2026-10-15)

- **Commit:** `d811021` (one commit with 1b, made 2026-10-24).
- **Plan:** [`docs/plans/M2/M2-step1-linux-baseline.md`](plans/M2/M2-step1-linux-baseline.md)
  (S1-D1 to S1-D4, plus the 1a build amendment).
- **What:**
  - `pyproject.toml` now says `mcp>=1.28.1,<2` and gives the dev tools ranges.
    `requirements/constraints.txt` pins every package, transitive ones included, for every
    dev venv.
  - `scripts/check_env.py` checks that a venv matches the constraints exactly, runs the
    expected Python, and is bound to this checkout. Both gates run it first.
  - `scripts/gate-linux.sh` runs the Linux gate: two venvs (system Python and a uv-managed
    3.11), time-bounded pytest, any unallowed skip fails, no caches in the repo.
  - `gate.md` runs Windows, then Linux.
  - `.gitattributes` keeps `*.sh` LF and `tests/fixtures/**` byte-exact.
  - mypy now checks against 3.11 on both platforms.
- **Files:** `pyproject.toml`, `requirements/constraints.txt`, `scripts/check_env.py`,
  `scripts/gate-linux.sh`, `.gitattributes`, `.claude/agents/gate.md`, `.claude/WORKFLOW.md`,
  `docs/GETTING_STARTED.md`. No `src/` change.
- **Guarded by:** the gates themselves. Windows: `check_env` + ruff + mypy + 240 tests.
  Linux: `check_env` on both venvs, ruff, mypy, 240 tests on 3.14, then 201 Docker-free
  and 3 stdio end-to-end tests on 3.11.
- **Broke along the way:**
  - A fresh install resolved mcp 2.3.0, whose server API change stops Vestibule from
    starting. The two venvs had also drifted (pydantic, starlette and ~20 more). Both are
    fixed by the pins and the constraints file.
  - **Critic, round 1:** 3 medium and 8 low issues, all confirmed by `verifier` and fixed:
    - no time bound on the Linux gate (the tool's 120 s limit would kill it mid-Docker-test);
    - `--setup` could build on the wrong Python;
    - a gate run from a second clone would test the first clone's code;
    - plus quoting, docs and procedure fixes.
  - **Critic, round 2:** 2 medium and 2 low issues, all confirmed and fixed:
    - the per-run bound could never beat the tool's 600 s limit, and a killed `wsl.exe`
      left `timeout`+pytest running (it leads its own process group). Fix: one 540 s gate
      budget, plus an EXIT trap that kills the group;
    - forced colour (or `PYTEST_ADDOPTS=--no-summary`) hid skips from the check;
    - a differently-cased `/mnt/c` path failed the checkout check;
    - uv's errors were hidden.

---

## M2 step 0 — spike (2026-10-14)

- **Commit:** `4509d35`.
- **Plan:** [`docs/plans/M2/M2-native-warden.md`](plans/M2/M2-native-warden.md) §10 step 0;
  report [`docs/plans/M2/M2-step0-spike.md`](plans/M2/M2-step0-spike.md).
- **What:** no repo code. Throwaway helper/harness/libseccomp-binding scripts proved the
  contract's kernel facts on the WSL host. The contract gained amendments A1–A6.
- **Files:** docs only: the spike report, contract amendments, HISTORY.
- **Where the evidence is:** WSL `~/m2spike/scripts/` (`warden.py`, `sc.py`, `server.py`,
  `run.sh`) and `~/m2spike/logs/`. The rootfs trees used are in `~/m2spike/rootfs-*`.
  Re-run with `wsl -d Ubuntu -- bash ~/m2spike/scripts/run.sh <smoke|cg scope|bench 30|…>`.
- **Broke along the way:**
  - The helper shared init's mount namespace, so `pivot_root` moved the helper into the
    sandbox root too (A1).
  - An import after `pivot_root` failed. Init reported it and never ran the guest, which is
    the abort behaviour N5 requires (A5).
  - moby's profile moved to the `moby/profiles` repo.

---

## M1 follow-up — config validation + server version (2026-10-14)

- **Commit:** `9a4f15e`.
- **Plan:** [`docs/plans/M1/M1-followup-config-version.md`](plans/M1/M1-followup-config-version.md)
  (approved by Victor 2026-10-14; review record in its §Review).
- **What:**
  - `Limits.from_env` validates every setting and raises `ConfigError` naming each bad one.
    Numbers: ASCII digits, ≥ 1, at most 18 significant digits. On/off: `1/true/yes/on` or
    `0/false/no/off`. Blank = unset = default (unchanged). Strings unchanged.
  - The server starts on a bad config and refuses every tool call with `Blocked:
    Vestibule's configuration is invalid: …`, also logged to stderr at startup
    (`server._load_limits`, `CONFIG_ERROR`). The defaults only render the tool schema.
  - `Server("vestibule", version=__version__)`: the server reports `0.0.1`, not the SDK's
    `1.28.1`.
- **Files:**
  - Source: `config.py` (`ConfigError`, `_shown`, `_MAX_DIGITS`, `from_env`), `server.py`
    (`_load_limits`, `CONFIG_ERROR`, the `call_tool` guard, `version=`).
  - Tests: `tests/test_config.py` (new), `tests/test_stdio_e2e.py` (`StdioServer.start`
    takes `settings`, `initialize` returns the result), `tests/test_acceptance.py` and
    `tests/test_validation.py` (pin `CONFIG_ERROR` to `None`).
  - Docs: `README.md` (Configuration paragraph), the plan, `docs/HISTORY.md`, this file.
- **Guarded by:** all of `tests/test_config.py`, including
  `test_stdio_bad_config_starts_and_blocks`. That test runs the real console script with
  `VESTIBULE_MEM_MB=²` and `VESTIBULE_MAX_CONCURRENT=0` and needs no Docker. It checks that
  the server starts, `initialize` reports `0.0.1`, both tools are `Blocked:` naming both
  settings, nothing spawns or gets created, stdout stays JSON-RPC, and the exit code is 0.
- **Red first** (old code): `VESTIBULE_MEM_MB=²` crashed `import vestibule.server` with
  `ValueError`; `512m` silently became 256; `MAX_CONCURRENT=0` was accepted;
  `WORKSPACE_RO=ture` read as off; the reported version was `1.28.1`.
- **Bugs found in review:** 6 low findings over two `critic` rounds, all confirmed by
  `verifier` except one refuted half, and all fixed. The notable one: the first guard
  against a 5000-digit value relied on Python's adjustable digit limit; it's now an explicit
  18-digit cap.

---

## M1 accepted — live-agent check (2026-10-14)

- **Commit:** `e7aaae5`.
- **Plan:** [`docs/plans/M1/M1-step7-acceptance.md`](plans/M1/M1-step7-acceptance.md) §8 step 9.
- **What:** no code. A restarted Claude Code session ran the plan's live checks against the
  registered server, all `isolation: container`. Shutdown was measured from outside: a
  detached watcher logged each server process's exit time and code, compared against Claude
  Code's own MCP log. Then the status docs were switched to **M1 accepted**.
- **Files:** `README.md`, `docs/PLAN.md`, `docs/HISTORY.md`, this file,
  `docs/plans/M1/M1-container-backend.md`, `docs/plans/M1/M1-step7-acceptance.md`.
- **Found:** the server reports the SDK's version (`1.28.1`), now a known issue above. Claude
  Code closes stdin before anything else, which narrows the SIGTERM known issue to other
  clients and to Linux/macOS.
- **Where to start if shutdown regresses:** `tests/test_stdio_e2e.py` (real stdio, Docker
  needed). Claude Code keeps a per-session MCP log at
  `%LOCALAPPDATA%\claude-cli-nodejs\Cache\<project>\mcp-logs-vestibule\*.jsonl`; its last
  line, "Closing MCP server stdin", marks when Claude Code let go of the server.

---

## M1 step 7b — Acceptance suite + docs pass (2026-10-14)

- **Commit:** `3c2dab9`.
- **Plan:** [`docs/plans/M1/M1-step7-acceptance.md`](plans/M1/M1-step7-acceptance.md) v2
  §2 (criteria), §5.1 (test layout), §6 (docs pass), S7-D2 (README half), S7-D4 (dev guide).
  New decision: `HOME=/tmp` (contract §3 amendment, approved 2026-10-14).
- **What:**
  - `tests/test_acceptance.py`: one test per contract §9 criterion (`test_c01`…`test_c12`),
    plus `test_hard_and_soft_controls_apply_inside_the_guest`. Each test drives
    `server.call_tool` with a fresh `BackendSelector` and a temp workspace, and checks every
    result against the honest isolation values. The module docstring maps each criterion to
    its tests.
  - Profile fix: `HOME=/tmp` (was `/tmp/home`, which never existed).
  - `tests/test_docs.py`: every relative Markdown link target (badges and titled links
    included) and every `docs/….md` path in README, CLAUDE.md (up to its changelog), `docs/`,
    `.claude/`, `src/` and `tests/` must exist, with exact case. Exempt: `docs/reviews/`,
    `docs/archive/`, `.claude/worktrees/`. A plan may name a file its step will create
    through the `_NOT_YET_BUILT` allowlist.
  - Docs: README Configuration table + "Know the limits" + status; new
    `docs/GETTING_STARTED.md`; M0 walkthrough archived; PLAN M1 status; contract status, D1
    amendment (S7-D2), §3 HOME amendment, §9 acceptance record; plan-path references fixed
    everywhere live.
  - Test fix: `test_run_cancelled_at_spawn_leaves_no_container` drains before polling,
    and now also checks for leftover temp dirs.
  - Leak fix: `ContainerBackend._reap_cli` removes the run's temp dir once more after the
    `docker run` CLI has exited (it already receives every CLI that can outlive the result).
- **Files:**
  - Source: `backends/container.py` (HOME; the CLI reap's final temp-dir removal); docstring paths in `config.py`, `workspace.py`,
    `backends/container.py`, `backends/select.py`.
  - Tests: `tests/test_acceptance.py` (new), `tests/test_docs.py` (new), `tests/test_container.py`,
    `tests/test_lifecycle.py`.
  - Docs: `README.md`, `docs/GETTING_STARTED.md` (rewritten),
    `docs/archive/M0-scaffold-walkthrough.md` (new), `docs/PLAN.md`, `docs/HISTORY.md`, this
    file, the M1 plans in `docs/plans/M1/`, `.claude/WORKFLOW.md`, `.claude/WORKFLOWDIAGRAM.md`.
- **Guarded by:** all of `tests/test_acceptance.py` (criterion → test map in its docstring and in
  contract §9); `tests/test_docs.py`;
  `test_lifecycle.py::test_profile_environment_is_exactly_four_variables` (`HOME=/tmp`, Docker-free);
  `test_lifecycle.py::test_cli_reap_removes_a_bind_source_recreated_after_cleanup` (Docker-free,
  deterministic; mutation-checked: fails with the final removal disabled);
  `test_container.py::test_run_cancelled_at_spawn_leaves_no_container` (live race, now also
  checking temp dirs).
- **Red first:** criterion 9 failed on the unchanged profile (`FileNotFoundError:
  '/tmp/home/.vestibule-c09'`) and passed after the `HOME` fix. The other 13 passed on
  their first run.
- **Bugs found and fixed:**
  1. *`$HOME` never existed in the guest* (criterion 9): `/tmp` is a fresh empty tmpfs, so
     `/tmp/home` was missing, and `~/x` writes, `cd ~` and `git config --global` failed ⇒
     `HOME=/tmp`.
  2. *Flaky 7a test* (first baseline run, 174/175): `test_run_cancelled_at_spawn_leaves_no_container`
     passed its poll on a `docker ps` taken before the late containers existed. Its final
     check then caught one in its 0.74 s life. Docker's event log showed every container
     killed 34–67 ms after start. The product was fine; the test now drains (every
     `docker run` CLI exited) before polling.
  3. *Empty temp dir left per lost cancel-at-spawn race* (gate, pre-existing since step 4):
     when a cancelled run's container starts only after cleanup removed its temp dir, Docker
     Desktop re-creates the missing bind source (checked: `-v` and `--mount` alike), the
     guest exits 2 with no script, `--rm` removes the container, and the empty
     `%TEMP%\vestibule-run-*` dir stays. Found as the gate's "3 before, 4 after" count,
     traced through Docker's event log (exit 2, no kill). The CLI reap now removes the dir
     again after the CLI exits. Live check: the race occurred 4 times in 6 test runs, with 0
     dirs left. The 4 empty leftovers from earlier runs were deleted.
- **Criterion 14:** gate green; with Docker removed from `PATH`, 159 passed and 38 skipped
  ("docker CLI not found"). Windows host only; the Linux run comes with M2.
- **If it breaks, start here:**
  - **An acceptance test fails:** the test name says which criterion. Treat it as a finding
    and diagnose before touching the assertion (plan §8).
  - **A memory or fork bomb test fails after a Docker update:** compare the guest's cgroup
    readback (`test_hard_and_soft_controls_apply_inside_the_guest`) with `Limits()`. A runtime
    that stopped applying a limit shows up there first.
  - **`test_docs.py` fails:** a doc or docstring names a path that moved. Fix the
    reference, not the test; dated records belong in `docs/reviews/` or `docs/archive/`.
  - **`$HOME` problems in guests:** the profile sets `HOME=/tmp` (`_build_command`), and
    custom images must not depend on a different home.

## M1 step 7a — Shutdown & lifetime hardening, verified limits (2026-10-11)

- **Commit:** `938056f` (its subject line says "M7" by typo; it is M1 step 7a).
- **Plan:** [`docs/plans/M1/M1-step7-acceptance.md`](plans/M1/M1-step7-acceptance.md) v2:
  S7-D1 (shutdown + one concurrency budget), S7-D5 (in-container self-deadline), S7-D3
  (cgroup readback), S7-D2 (description sentence), §4.1 (random probe name), §4.2 (import
  idiom). §11 has the skeptic dispositions; §12 has critic rounds 1–2.
- **What:**
  - `BackendSelector` tracks every backend it builds, with one shared semaphore.
    `note_result(warden, result)` checks identity.
  - `aclose()` removes all active containers in one batched `rm -f` (1.5 s), then waits
    without cancelling (0.5 s; in-flight runs count).
  - `server._shutdown` runs it on stdin EOF and calls `os._exit(0)` if anything at all is
    still alive.
  - Waits use `asyncio.wait`/`asyncio.timeout`, never `gather`/`wait_for` around cleanup or
    the run. Cancelled CLI calls kill their process.
  - Guest command: `timeout -s KILL <timeout+90> <interpreter> …`.
  - The probe reads back `memory.max`, `memory.swap.max`, `pids.max`, `cpu.max` and the
    `/tmp` size. A mismatch, or no visible cgroup, gives `container-degraded`; cgroup v1
    falls back to a broadened warning scan.
  - The probe file is `.vestibule-probe-<hex>`.
  - New refusals: a workspace-permission refusal, and an image refusal naming `timeout`.
- **Files:**
  - Source: `backends/base.py`, `backends/container.py`, `backends/select.py`,
    `server.py`.
  - New tests: `tests/dockerutil.py` (the shared Docker skip rule, moved out of
    `test_container.py`) and `tests/test_stdio_e2e.py`.
  - Changed tests: `tests/test_select.py`, `tests/test_lifecycle.py`,
    `tests/test_container.py`.
  - Docs: `README.md` (override contract needs `timeout`), contract §3/§4/§5 amendments,
    plan, HISTORY, this file.
  - The plans moved to `docs/plans/M1/` in the same commit (Victor's renames).
- **Guarded by:**
  - `tests/test_stdio_e2e.py`: all 3 tests use the real server process, closing stdin
    and killing at 2 s like a client.
  - `test_select.py`:
    - `test_anyio_cancel_during_candidate_drain_spares_its_cleanup` (fails on the old
      `gather` drain; mutation-checked)
    - `test_cancelled_handler_leaves_no_run_behind` (fails on the old `wait_for`;
      mutation-checked)
    - `test_anyio_cancel_mid_probe_leaves_candidate_for_aclose`
    - `test_aclose_*`, `test_server_shutdown_*`,
      `test_note_result_ignores_a_dropped_backends_late_result`
    - `test_reselection_shares_one_slot_budget`
    - `test_limits_note` (14 cases), `test_runtime_warning_formats`
    - `test_unverified_limit_makes_the_selection_degraded`,
      `test_unusable_workspace_gets_its_own_refusal`
    - `test_probe_uses_a_fresh_random_file_name`
  - `test_lifecycle.py`:
    - `test_guest_runs_under_its_own_deadline`, `test_unverified_limits_are_reported_degraded`
    - the drain/idle tests
    - `test_cancelled_run_hands_its_cli_to_a_tracked_reap`,
      `test_cancelled_cli_call_kills_its_process`
  - `test_container.py` (Docker):
    - `test_real_selection_reports_container` (now also checks the limits were verified)
    - `test_planted_probe_name_cannot_break_selection`
    - `test_container_ends_itself_without_a_server`
    - `test_run_cancelled_at_spawn_leaves_no_container`
- **Red first:** on unchanged code, `test_stdio_shutdown_with_run_in_flight_removes_its_container`
  failed: the server was still alive 30 s after stdin closed. A `py-spy dump` showed the
  main thread in `asyncio.runners._cancel_all_tasks` (`server.py:230`), and the
  container was still running. Same mechanism as the step-5 hang.
- **Bugs found and fixed (all before commit):**
  1. *v1 drain cancelled its own cleanup* (skeptic): `gather` passed the SDK's anyio
     cancellation into the finisher ⇒ `asyncio.wait`.
  2. *3.11 `wait_for` inner task* (critic r1, verifier repro): a cancelled request's run
     could outlive its handler, untracked ⇒ `asyncio.timeout`; `aclose` counts in-flight
     runs; hard exit if any task remains.
  3. *Fixed probe filename lockout* (skeptic, live repro) ⇒ random name.
  4. *Unimportable workspace modules* (skeptic, live repro) ⇒ documented idiom.
  5. *Overclaimed `container` with unverified limits* ⇒ readback (S7-D3), including the
     no-cgroupfs case (critic r2).
  6. *Test-hygiene leak:* `test_result_not_delayed_by_cleanup` (step 4) leaked two
     `%TEMP%` dirs per suite run ⇒ the stub now cleans up; 49 stale dirs deleted.
- **If it breaks, start here:**
  - **The server hangs on exit, or a container survives it:** run
    `pytest tests/test_stdio_e2e.py -v`, then `py-spy dump --pid <python child of the
    launcher>`. Look for `_cancel_all_tasks`. Check `_shutdown` / `_other_live_tasks`
    and `BackendSelector.aclose`.
  - **Selection reports `container-degraded (limits not verified: …)` unexpectedly:** the
    detail quotes the observed cgroup values. Compare them with `Limits()`; see
    `select._limits_note`.
  - **Every run fails with exit 127 and a tini "exec … failed" error:** the image lacks
    coreutils `timeout` (override contract).
  - **`Blocked: the sandbox could not use the workspace …`:** fix that folder's
    permissions, then retry.

## M1 step 6 — Digest-pinned images + setup UX (2026-10-10)

- **Commit:** `a0827fd`.
- **Plan:** [`docs/plans/M1/M1-step6-pinning.md`](plans/M1/M1-step6-pinning.md) v2 — S6-D1
  (tagless, fully qualified index digests), S6-D2 (unpinned override ⇒ stderr warning),
  S6-D3 (no doctor CLI in M1), S6-D4 (proof under both image stores). §9 = skeptic
  dispositions, §10 = critic dispositions (two rounds).
- **What:** `config.py` ships
  `docker.io/library/python@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1`
  (python:3.12-slim → 3.12.15) and
  `docker.io/library/node@sha256:c3de60bf2f9dd0ac6370e6117950ff62d6e339527e7472301c9c78a017978392`
  (node:22-slim → v22.23.3); captured 2026-10-10 on Docker 28.5.1, pull `Digest:` ==
  `RepoDigests`, OCI index with amd64 + arm64/v8; proven (empty store → pull by ref →
  inspect + `run --pull never` + `pytest -m docker`) in containerd **and** classic mode.
  README one-time setup block; setup refusals say retry vs. restart; runtime-specific
  start hints; hedged exit-125 detail.
- **Files:** `config.py`, `backends/select.py`, `backends/container.py`, `README.md`,
  `pyproject.toml` (docker marker text), `tests/test_pins.py` (new), `tests/test_select.py`,
  `tests/test_lifecycle.py`, `tests/test_container.py`, plus docs (plan, contract §8,
  HISTORY, this file).
- **Guarded by:** `tests/test_pins.py` (per-constant ref format; README pull lines ==
  config); `tests/test_container.py::test_pinned_images_pass_preflight_and_hold_expected_versions`
  (Docker); in `tests/test_select.py` the `test_unpinned_override_warns_for_both_refs`,
  `test_pinned_defaults_do_not_warn`, `test_naive_skips_pin_check`,
  `test_config_refusals_say_restart_not_retry`,
  `test_runtime_not_installed_says_install_and_restart_agent`,
  `test_daemon_down_says_start_then_retry`, `test_daemon_down_hint_is_runtime_specific`,
  `test_cached_restart_refusal_never_suggests_waiting`,
  `test_cached_refusal_says_when_it_rechecks`; in `tests/test_lifecycle.py` the
  `test_preflight_missing_image_refused` (verbatim ref + hedge),
  `test_preflight_hedge_names_the_configured_runtime`, `test_exit_125_reports_no_isolation`
  (both-cause wording). The Docker-test skip path (`_docker_skip_reason`) was exercised
  live on an empty classic store: 19 skipped with the pull commands.
- **Bugs found and fixed (all before commit):**
  1. *Tag next to a digest is unchecked* (skeptic): `python:3.11-slim@<3.12 digest>`
     resolves to 3.12, so the planned `name:tag@digest` form carried a label nothing
     verified ⇒ tagless refs + interpreter-version test.
  2. *Docker tests failed instead of skipping* on a daemon-up/images-missing machine,
     and `_plant` could pull from the network (skeptic) ⇒ image-aware skip +
     `--pull never`.
  3. *"Retry" sent the model in circles* (critic): config errors and fresh installs need
     a restart; cached refusals hid fixes for 30 s; the exit-125 "retry" would re-run
     code that already ran ⇒ retry-vs-restart wording, `_RestartRequired`, hedged
     exit-125 detail.
  4. *Wrong product named* (critic ×2): Podman users were told to start Docker Desktop
     ⇒ shared `START_HINTS`.
  5. *README registered the server for the clone folder only* (critic) ⇒
     `--scope user`.
- **If it breaks, start here:**
  - `Blocked: image … is not usable` after a Docker update, store switch, or prune →
    run the README pull commands again (they are the shipped refs). Preflight:
    `ContainerBackend._preflight_image`.
  - `test_readme_pull_command_matches_config` fails → someone re-pinned `config.py` and
    forgot the README (or the reverse).
  - The interpreter-version Docker test fails → a re-pin pasted a digest from the wrong
    tag.
  - Re-pinning for upstream fixes → the plan's "Re-pin cost": capture + both checks +
    proof gate in one store mode + full suite.

## M1 step 5 — Backend selection + capability probing (2026-07-05)

- **Commits:** `522606f` (plan draft), `5ce882a` (plan final + step-4 budget fix),
  `d6a9df8` (selection build), `7ebd28b` (CRLF + task-leak fixes).
- **Plan:** [`docs/plans/M1/M1-step5-selection.md`](plans/M1/M1-step5-selection.md) — S5-D1
  (30 s failure cooldown), S5-D2 (all-soft-off degraded retry), S5-D3 (per-run image
  preflight + `--pull never`).
- **What:** on the first tool call, `BackendSelector` picks a runtime (`auto` prefers
  Docker), test-drives the full locked-down profile as a real bash run, and commits to
  `container`, `container-degraded`, or a `Blocked:` refusal with the fix. Naive is only
  reachable through `VESTIBULE_BACKEND=naive`.
- **Files:** `backends/select.py` (new), `backends/container.py`, `server.py`,
  `tests/test_select.py` (new), `tests/test_lifecycle.py`, `tests/test_container.py`.
- **Guarded by:** all of `tests/test_select.py`; in `tests/test_lifecycle.py` the
  `test_preflight_*`, `test_build_command_soft_flags_and_pull_never`,
  `test_exit_125_reports_no_isolation`, `test_degraded_backend_reports_detail`; in
  `tests/test_container.py` the step-5 block, especially
  `test_failed_selection_drains_and_leaves_no_tasks`.
- **Bugs found and fixed:**
  1. *CRLF script corruption* (latent since step 3): on a Windows host, text-mode writes
     turned `\n` into `\r\n` and bash in the Linux guest choked (`then\r`). Fix:
     `newline="\n"` on the script write in `ContainerBackend._execute`.
  2. *Failed-selection task leak*: rejected probe backends left detached cleanup tasks
     alive; event-loop shutdown cancelled one mid-`create_subprocess_exec` and deadlocked
     (Windows proactor). The suite hung 42 min in teardown; found with py-spy stack dumps
     plus a task-dump watchdog. Fix: `ContainerBackend.drain()` on every rejected
     candidate.
  3. *Exit 125 claimed isolation for a non-run*: a dead daemon makes `docker run` exit
     125 without starting anything. Fix: report `isolation: none`, and the server's
     `note_result` hook drops the cached selection so the next call re-checks.
- **If it breaks, start here:** a pytest run that hangs at teardown means a leaked
  detached task — check `drain()` is awaited on every rejected `ContainerBackend`. A
  multi-line bash guest failing only on Windows hosts means CRLF — check the script
  write. A wrong `isolation:` value: `BackendSelector._probe` acceptance rules and the
  exit-125 branch in `ContainerBackend._execute`.

## M1 step 4 — Run lifecycle (2026-07-04)

- **Commits:** `b4019a7`; follow-up budget fix in `5ce882a` (2026-07-05).
- **Plan:** [`docs/plans/M1/M1-step4-lifecycle.md`](plans/M1/M1-step4-lifecycle.md) — S4-D1
  (5 s bounded wait, then `Blocked:`), S4-D2 (missing runtime reports `isolation: none`),
  S4-D3 (deadline-label reaping).
- **What:** cleanup (container kill → `rm -f` → temp dir) runs as a detached finisher
  task that survives request cancellation and holds the concurrency slot until the
  container is really gone; orphan reaping removes only containers whose own
  `vestibule.deadline` label is past (+60 s), never on missing or garbled labels;
  `max_concurrent` semaphore.
- **Files:** `backends/base.py` (`RunRefusedError`), `backends/container.py`,
  `server.py`, `tests/test_lifecycle.py` (new), `tests/test_container.py`.
- **Guarded by:** `tests/test_lifecycle.py` — `test_semaphore_caps_concurrency`,
  `test_busy_refusal_after_bounded_wait`, `test_reap_decision` (7-case table),
  `test_cancelled_run_releases_slot_and_active_name`,
  `test_result_not_delayed_by_cleanup`, `test_build_command_stamps_deadline_and_owner`,
  `test_timed_out_result_returns_before_any_kill`,
  `test_cli_wedge_after_guest_finished_is_not_a_timeout`; Docker-marked
  `test_cancellation_cleans_up_container_and_tmpdir`, the three `test_reaper_*`,
  `test_four_simultaneous_runs_succeed`.
- **Bugs found and fixed:**
  1. *v1 design rejected before code*: age-based reaping judged other servers'
     containers by this server's timeout and could kill their live runs. Replaced by
     owner-stamped deadline labels (which also avoided Docker Desktop clock drift).
  2. *Codex P2*: shield-awaited cleanup could push a timed-out result past the server's
     outer deadline on a slow daemon, so the honest result was lost. Fix: full
     detachment + slot handoff.
  3. *Same failure, one path over* (`5ce882a`): the timeout path serialized kill + rm +
     wait (15 s) after `timeout + 5`. Fix: detached kill; the result returns at
     `timeout + 5`. A CLI that wedges after the guest finished is reported as "exit code
     unknown", never as a fake timeout.
- **If it breaks, start here:** a result replaced by the generic outer-deadline message
  means something on the request path awaits cleanup — every bound must fit inside
  `timeout + 30` (`server.py`). Containers piling up: `ContainerBackend._reap_orphans` /
  `_reap_decision`. Note: containers created before step 4 have no deadline label and
  are never reaped; remove them by hand.

## M1 step 3 — `ContainerBackend` happy path (2026-07-03)

- **Commit:** `4f765e9`.
- **Plan:** [`docs/plans/M1/M1-container-backend.md`](plans/M1/M1-container-backend.md) §3
  (exact container profile).
- **What:** each run gets a fresh, named, labeled container: `--network none`,
  `--cap-drop ALL`, `no-new-privileges`, `--read-only`, non-root `--user`, memory = swap,
  CPU, pids, capped tmpfs `/tmp`, no env inheritance. Code arrives via a read-only
  `/sandbox` mount (never argv or stdin); output is collected streaming with a 2× cap
  that kills a flooding container; timeouts kill the container, not just the CLI.
- **Files:** `backends/container.py` (new), `tests/test_container.py` (new).
- **Guarded by:** `tests/test_container.py` step-3 block — `test_python_hello`,
  `test_bash_hello`, `test_node_hello`, `test_nonzero_exit_and_stderr`,
  `test_workspace_write_persists_to_host`, `test_network_is_gone`,
  `test_rootfs_is_read_only`, `test_timeout_kills_container`,
  `test_output_flood_is_capped_and_killed`.
- **Bugs:** the CRLF script bug was introduced here and found in step 5.
- **If it breaks, start here:** `ContainerBackend._build_command` (the profile) and
  `_execute` (collection, caps, timeout).

## M1 step 2 — Server hardening + `read_workspace` wiring (2026-07-03)

- **Commit:** `ec467f7`.
- **Plan:** M1 contract §7 (server changes) and §6 (`read_workspace`).
- **What:** every `run_code` argument type-checked before anything spawns (malformed ⇒
  `Blocked:`, never a crash); `read_workspace` tool exposed (filesystem work off the
  event loop); `RunResult.isolation_detail` + the `container-degraded` vocabulary;
  outer deadline `timeout + 20` (raised to `+30` in step 5).
- **Files:** `server.py`, `backends/base.py` (this commit also carried
  `tests/test_workspace.py`).
- **Guarded by:** all of `tests/test_validation.py`.
- **If it breaks, start here:** `server._handle_run_code` (validation order) and
  `server.call_tool` (handlers must return content, never raise).

## M1 step 1 — Config + `read_workspace` path jail (2026-07-03)

- **Commit:** `9f43762` (this commit also carried `tests/test_validation.py`; the jail
  suite landed in `ec467f7`).
- **Plan:** M1 contract §8 (config) and §6 (`read_workspace` jail); decision D1
  (writable dedicated workspace).
- **What:** env-overridable M1 settings in `config.py`; `workspace.py` rejects traversal,
  absolute paths, drive letters / NTFS streams (any `:`), UNC/device prefixes, NUL,
  reserved device names and trailing dots/spaces *before* touching the filesystem, then
  walks the path refusing symlinks/reparse points.
- **Files:** `config.py`, `workspace.py` (new).
- **Guarded by:** all of `tests/test_workspace.py` (24 cases incl. live symlink attacks;
  `test_jail_rejects` is the parametrized lexical table).
- **If it breaks, start here:** `workspace.split_relpath` (lexical rules) then
  `resolve_in_workspace` (component walk + containment).

## M0 — Skeleton that runs, unsafely (2026-07-02)

- **Commit:** `aabd935`; acceptance recorded in `c70bca6`.
- **Plan:** [`docs/PLAN.md`](PLAN.md) Milestone 0; scaffold walkthrough in
  `docs/archive/M0-scaffold-walkthrough.md` (it was `docs/GETTING_STARTED.md` until 7b).
- **What:** MCP server over stdio with `run_code`; `Warden` ABC + `RunResult`;
  `NaiveBackend` (plain subprocess, honestly `isolation: none`); stderr-only logging.
- **Files:** `server.py`, `config.py`, `backends/base.py`, `backends/naive.py`,
  `tests/test_smoke.py`.
- **Guarded by:** `tests/test_smoke.py` — incl. `test_guest_stdin_is_closed_not_inherited`
  and `test_spawn_failure_reports_isolation_none` (added 2026-07-06, `99a7f99`).
- **Bugs found and fixed:**
  1. *Guest stdin inherited the server's stdin*: leaks the JSON-RPC channel to untrusted
     code and hangs the child on Windows. Fix: `stdin=DEVNULL` — now a rule for every
     backend and every runtime CLI call.
  2. *2026-07-06*: a stray working-tree edit made the spawn-failure path report
     `isolation="false"`; caught by review before commit and pinned by a regression test.
- **If it breaks, start here:** a dead MCP session is almost always something writing to
  stdout (golden rule 1) — look for `print()` in anything the server runs.
