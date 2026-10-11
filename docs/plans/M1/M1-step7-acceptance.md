# M1 step 7 plan — shutdown & lifetime hardening, acceptance suite, docs pass

Status: **signed off v2, 2026-10-11; 7a built 2026-10-11 (gate green, critic ×2 + verifier, §12). S7-D1…D5 all approved as recommended, along with the
7a/7b split + live check (Victor commits each part himself).** v1 was reviewed by
`skeptic` ("rework needed for S7-D1"; 2 high, 6 medium, 10 low). `verifier` checked the
11 code claims: **11 confirmed, 0 refuted**, with corrections on three. Every finding and
what happened to it is in §11.
Parent contract: [`M1-container-backend.md`](M1-container-backend.md) §9 (the 14 acceptance
criteria) and §10 step 7 (binding). Also owns the known issue carried over from step 6
(`docs/BUILD_LOG.md` "Known open issues"; step-6 plan §10).

Environment at planning (2026-10-11): Docker Desktop 28.5.1 (linux/amd64), containerd image
store, both pinned images present, cgroup v2 inside containers, GNU coreutils `timeout` in
both images (9.7 python / 9.1 node). Baseline gate green: ruff + mypy clean, **122 passed,
0 skipped, 38 s**. MCP SDK 1.28.1. The plan files moved from `docs/plans/` to
`docs/plans/M1/` (Victor, uncommitted, pure renames); this step fixes the references (§6).

---

## 1. What this step does, in plain words

Steps 1–6 built the container backend and tested each piece on its own. Step 7 is the
**exam**: one test per acceptance criterion, run through the same entry point an agent
uses, against real Docker. When it passes, along with one live check from a real agent
session, M1 is done.

Planning and review turned up real problems the exam would otherwise have to work around,
so step 7 has **two parts, built and committed separately**. Each part gets its own gate,
`critic` and `verifier` pass, so each diff stays reviewable.

- **7a — hardening (code).** These fix how a run ends when the server goes away, and make
  the result honest about limits.
  - Shutdown that actually cleans up (S7-D1).
  - Containers that end themselves even if the server is killed (S7-D5).
  - Limits verified rather than assumed (S7-D3).
  - A probe a guest can't sabotage (§4.1).
  - Two `run_code` description fixes (S7-D2, §4.2).
- **7b — exam + docs.** The acceptance suite (§2, §5) and the docs pass (§6, S7-D4).

## 2. The 14 criteria: what exists, what step 7 adds

"Handler" means `server._handle_run_code` / `_handle_read_workspace` with a fresh
`BackendSelector` and a temp workspace, so real selection, preflight, the run, and result
formatting are all in the path.

| # | Criterion (contract §9) | Covered today | Step 7 adds |
|---|---|---|---|
| 1 | python/bash/node hello ⇒ `isolation: container` | backend-level; python and node assert isolation (`test_container.py:115, 207, 211`), bash does not | Handler test for all three languages; plus the stdio end-to-end test (§5.2) |
| 2 | TCP **and** DNS fail | TCP only | TCP to an IP fails, `getaddrinfo` fails, UDP send fails, and the only network interface is `lo` |
| 3 | memory bomb contained | — | The guest forks a child that touch-allocates about 1 GB against the 256 MB cap, then waits for it. The child dies by SIGKILL, the parent survives and prints `oom_kill` ≥ 1 from `/sys/fs/cgroup/memory.events`, which proves the memory cgroup did the kill. A follow-up run works and the selection is kept |
| 4 | fork bomb contained | — | (a) Controlled: fork until refused, bounded at `pids_max + 64` attempts; the count must land at or just below `pids_max`. (b) Classic bash `:(){ :\|:& };:`, which runs only after (a) passes in the same test: the run returns, no container survives, and a follow-up run works |
| 5 | tmpfs cap + read-only rootfs | `test_rootfs_is_read_only`, which cannot tell read-only from permission denied: uid 1000 can't write `/usr/bin` either way (verifier) | Fill `/tmp` until `ENOSPC` (total ≤ `tmpfs_mb`). Creating a new file in the world-writable `/var/tmp` fails with **`EROFS`**; only `--read-only` can cause that. `/proc/self/mounts` shows `/` as `ro`. Creating in `/sandbox` fails with `EROFS` |
| 6 | infinite loop ⇒ result within `timeout_s + 20`, marked timed out, no surviving container | sleep-based, no elapsed check | Warm-up call first, so selection isn't timed. Then a busy loop through the handler: `[timed out` rendered, elapsed ≤ `timeout_s + 20`, no container left under the owner label (polled) |
| 7 | 4 concurrent runs, distinct containers | 4 succeed | Each run prints its hostname (the container ID), and all 4 must differ |
| 8 | stale labeled container reaped on first use | reaper called directly | Plant a past-deadline container, make one normal handler call, and poll until the container is gone |
| 9 | innocent code under `--read-only` | — | A package saved to `/workspace` is imported with **the documented idiom** (§4.2) and no `__pycache__` is written. Stdlib imports work, `$HOME` and `/tmp` are writable, and Node `require('/workspace/…')` works |
| 10 | jail unit suite + **guest-planted symlink** refused | unit suite (24 cases) | Guest plants three links: relative to a host canary outside the workspace, absolute, and `..`. `read_workspace` refuses each one, the listing labels them `symlink`, and the canary text never appears |
| 11 | degraded path ⇒ `container-degraded` + list | Docker-free fakes | Real injection: a bad `--memory` value makes Docker refuse the full profile, the soft-off retry passes, and a handler run reports `container-degraded (limits not applied: …)` |
| 12 | no runtime / no image ⇒ actionable `Blocked:` | Docker-free mocks | Real failures: `VESTIBULE_RUNTIME=podman` with no podman ⇒ install + restart message (skips if podman exists); an override to a never-pulled digest ⇒ `Blocked:` with that exact pull command. No result ever claims `native`. Daemon-down stays mocked |
| 13 | malformed args ⇒ `Blocked:` | `tests/test_validation.py` | Nothing new; mapped only |
| 14 | ruff + mypy clean; non-Docker tests run everywhere; Docker tests pass here | gate | Plus one full run **with Docker hidden from PATH**: every Docker test skips with a reason, everything else passes. Recorded as proven **on a Windows host** only. No CI exists, and the Linux-only paths (`getuid`, `O_NOFOLLOW`) first run when M2 work moves to Linux/WSL |

Two more checks the suite exists for:
- **Every control is really applied inside the guest.** The hard tier is what D3 refuses
  to run without, and today only the command line is tested:
  - uid ≠ 0
  - `CapEff`, `CapPrm`, `CapBnd` all 0
  - `NoNewPrivs: 1`
  - `Seccomp: 2`
  - environment keys limited to the four the profile sets, plus the image's own
  - no `docker.sock`
  - `/` mounted read-only

  The soft tier is checked by cgroup readback: `memory.max` = 256 MiB, `memory.swap.max`
  = 0, `pids.max` = 128, `cpu.max` = `75000 100000`, and `/tmp` = 64 MiB, all from
  `Limits()`. All five readback values were checked by hand on 2026-10-11.
- **Golden rule 1 over a real pipe:** every stdout line of the real server process parses
  as JSON-RPC (§5.2).

**What "host unaffected" can honestly mean.** On Docker Desktop the guest runs in a Linux
VM, so a test cannot measure Windows host memory or PIDs meaningfully. The suite asserts
what is observable:
- the guest was stopped, and the cgroup says why
- the server returned a normal result
- the next run works
- no container survived

The docs claim exactly that.

## 3. Decisions to sign off

### S7-D1 (reworked) — Shutdown: kill our containers first, never let a wait cancel cleanup

**The problem.** It is pre-existing since steps 4–5, verifier-confirmed in step 6, and
sharpened by `skeptic`:
1. **Dropped backends.** `note_result` drops the cached selection without awaiting the old
   backend's detached cleanup tasks.
2. **Live backend at exit.** `server._main` never drains it before `asyncio.run`
   mass-cancels leftovers. A cleanup task cancelled mid-`create_subprocess_exec` can
   deadlock loop close on Windows; that was the 42-minute step-5 hang.
3. **v1's drain would cancel the cleanup it waits for.** On stdin EOF the SDK cancels
   in-flight handlers through an anyio cancel scope (`lowlevel/server.py:691-696`), and
   anyio re-cancels at every await. A drain built on `gather` passes that cancellation into
   the cleanup task. Example: the first call's probe is running when the user quits; the
   finisher is cancelled mid-spawn, which is exactly the hang mechanism.
4. **Real clients don't wait long.** The installed MCP **Python** client closes stdin,
   waits **2 s**, then kills the whole process tree (`mcp/client/stdio/__init__.py:48,
   195-209`). On Windows it does this through a Job Object when pywin32 is importable, so
   our `docker` CLI children die too; otherwise it uses a plain terminate. v1's 20 s drain
   budget assumed a client that waits. Claude Code is a different (Node) client and its
   window is unmeasured; the live check (§8) records it.
5. **Concurrency count.** A replacement backend's fresh semaphore stops counting the
   dropped one's containers. That breaks the step-4 invariant "`max_concurrent` bounds
   existing containers".

**Design:**
1. **Track every backend.** The selector registers each `ContainerBackend` it constructs
   (probe candidates, degraded, live, dropped) the moment it creates it. Idle,
   non-live backends are pruned: no run in flight, no pending task. The in-flight counter
   is raised at `run()` entry, before the preflight await.
2. **Waits never cancel what they wait on.** `drain()` and `aclose()` use
   `asyncio.wait(tasks, timeout=…)`, which leaves the awaited tasks alone when the waiter
   is cancelled. Never `gather` or `wait_for` on cleanup tasks. `drain()` loops until no
   detached task is pending, because a run that ends mid-drain adds a new finisher.
   `verifier` showed this matters **mid-session too**:
   - The same anyio cancellation arrives when a client cancels a single request
     (`notifications/cancelled`), not only at stdin EOF.
   - A finisher cancelled before its first step never runs its `finally`, so the slot
     release and the kill/rm are skipped.
   - With point 6's shared budget, that would leak a slot for the life of the process.

   So this rule is a precondition for point 6, and it is tested both ways.
3. **Cancellation in `_select`.** It re-raises immediately; the candidate is already
   tracked, so `aclose()` reaches it. An ordinary probe failure still drains the rejected
   candidate, with a bound and without cancelling.
4. **`aclose()` fits inside the client's 2 s.**
   - (a) One batched `<runtime> rm -f` covers every active container name of every tracked
     backend, bounded at 1.5 s. Containers go first because they are the expensive leak.
   - (b) A non-cancelling wait of up to 0.5 s for the remaining detached tasks.
   - (c) It returns whether anything is still pending.
5. **`server._main`** runs `aclose()` in a `finally` after the stdio context exits, which
   happens on stdin EOF. If tasks are still pending, it flushes stderr and calls
   **`os._exit(0)`** instead of returning into `asyncio.run`'s cancel-all. So a task is
   never cancelled mid-spawn at shutdown, and the deadlock is impossible by construction.
   No stdout is written at that point, and the client is already gone.
6. **One concurrency budget per process.** The semaphore is created once, lazily, on the
   first `get()`, and passed to every backend the selector builds. Backends built directly
   (tests) keep their own.
7. **`note_result(warden, result)`.** It acts only when `warden` is the current selection,
   so a late `isolation: none` from a dropped backend can't evict a healthy new one.
8. **A cancelled run detaches its CLI reap**, so `drain()` tracks the `docker run` process
   too.

**Not covered here:** a hard kill (Job Object / TerminateProcess / SIGKILL) skips all of
this. That is what S7-D5 is for.

**Rejected:**
- *Drain the dropped backend inside the next `get()`.* Up to about 15 s extra latency right
  after a runtime failure, and it misses runs still in flight.
- *Keep per-backend semaphores.* It breaks a documented invariant to save about 10 lines.
- *Let the bounded drain return and accept `asyncio.run`'s cancel-all.* That is the
  original hang.

**Accepted side effect:** while a dropped backend still holds every slot, re-selection's
probe can be refused with "too many concurrent runs" (cached 30 s with the cooldown note).
It is accurate (those containers exist) and needs `max_concurrent` runs in flight while
the runtime is failing.

### S7-D5 (new) — Containers end themselves at their deadline (contract §3 amendment)

**The problem** (pre-existing; skeptic + the 2 s finding). Nothing inside a container
bounds its lifetime. The timeout lives in the server, and `--rm` only removes a container
once it has exited. If the server is killed:
- the guest keeps running, at 0.75 CPU and 256 MB
- the next server's reaper removes it only at deadline + 60 s, and only once that server is
  used
- if Vestibule is never used again, a `while True:` guest runs **forever**

Resource exhaustion is one of the three risks Vestibule promises to make structurally
impossible.

**Proposal:** wrap the guest command in coreutils `timeout`, set to the same deadline the
owner stamps on the label:
```
timeout -s KILL <timeout_s + 90> <interpreter> /sandbox/main.<ext>
```
- Under `--init`, `timeout` kills its whole process group, then exits. tini sees its
  child exit, so the container stops, and the daemon's `--rm` removes it. No server is
  needed. Checked 2026-10-11 in both pinned images: rc 137 after exactly the requested
  time.
- In normal operation it never fires, because the server kills timed-out runs at
  `timeout_s + 5`.
- Its exit codes overlap ours harmlessly: 125 now also means "`timeout` itself failed",
  which is still a non-run reported as `isolation: none`; 126/127 already meant a missing
  interpreter.
- The reaper stays as the backstop.
- The override-image contract (README) adds coreutils `timeout`.

**Residual, documented:** the watchdog runs as the guest's uid, so a *deliberately*
malicious guest can `SIGSTOP` it. Such a container lives until a reaper finds it, still
capped by memory, CPU and pids. Buggy code (the realistic runaway) can't do this.

**Rejected:**
- *`timeout` as PID 1 instead of tini.* The kernel shields a namespace's init from
  SIGSTOP/SIGKILL sent from inside, so the guest could not stop it. But `timeout` doesn't
  reap zombies, which loses finding 7's `--init`.
- *Podman's `--timeout`.* Podman only.
- *A server-side watchdog.* It dies with the server.

### S7-D2 (amended) — Guest-planted symlinks are real host symlinks: warn the model *and* the user

**Verified on this machine, 2026-10-11** (throwaway container, pinned image, scratch dir):
a guest `os.symlink()` in `/workspace` creates a real Windows reparse point on the host.
- Relative target (`../canary.txt`): a native NTFS symlink (tag `0xA000000C`). Host
  `open()` and `cmd /c type` **follow it out of the workspace** and print the canary.
- Absolute target: a WSL-style link (`0xA000001D`) that Win32 can't open.
- `..`: a directory symlink.
- `read_workspace` refused all three and listed them as `symlink`.
- Inside the guest nothing escapes, because links resolve in the container's own mount
  namespace. On Linux hosts, symlinks in a bind mount are always real host symlinks.

**The risk is a confused deputy, in both directions:**
- **Read:** a guest writes `report.txt -> ../../.ssh/id_ed25519`. An honest agent reads it
  with its own file tool, and the key lands in the model's context.
- **Write (worse):** an agent "edits" `workspace/setup.sh`, which actually links to
  `~/.bashrc`, and that gives host persistence.

The guest typically isn't the model's own code; think of a cloned repo's test suite or a
fetched script. The honest model and the user are the defenders.

**Options:**
- **(A) recommended:** say it where the readers are.
  - The `run_code` description gains one sentence: read files the code produced with
    `read_workspace`. The workspace is written by sandboxed code and may contain symlinks
    that other file tools would follow out of it, for reads and writes.
  - The README tells the **user**: treat the workspace like an untrusted download, never
    point `VESTIBULE_WORKSPACE` at a directory other tools use, and don't give the agent's
    own file tools blanket access to it.
  - The contract's D1 gets an amendment note: guest code's blast radius is still the
    workspace, but host tools that follow links reach past it. HISTORY's D1 bullet stays
    as written; a new bullet records the amendment.
  - Recorded for M4's `SECURITY.md`.
- (B) Scan the workspace after each run and report links. Rejected:
  - The attacker it targets can evade it, by resetting link mtimes (`lutimes`) or by
    flooding the workspace so the bounded scan never reaches the link.
  - It is noisy with legitimate links such as a venv.
  - It costs a filesystem walk on every run.
  - M3's audit record is the right home for per-run workspace changes.
- (C) Delete guest-created links. Rejected: it breaks `python -m venv` and races with
  concurrent runs.
- (D) A Docker seccomp profile that denies `symlink`/`symlinkat`. This **is** a structural
  fix; v1 wrongly said none existed. Rejected for M1:
  - `--security-opt seccomp=` *replaces* Docker's default profile, so we would maintain a
    fork of it, and Podman's, per runtime version.
  - It breaks venv creation without `--copies`.
  - It belongs with M2's own seccomp allowlist. Noted there as a candidate.

### S7-D3 (changed) — Verify the soft limits by readback, not by trusting the runtime

**The gap.** `select.py:_probe` logs runtime `WARNING` lines and still reports `container`.
- Docker warns and runs anyway when it silently drops a limit (for example "…Memory
  limited without swap").
- Rootless Podman on cgroup v1 drops all limits. It logs in logrus format
  (`level=warning msg=…` / `WARN[0000] …`), which the current `startswith("WARNING")`
  check doesn't even match.

Either way, every run then claims the full profile. That breaks golden rule 5 and D3
("`container` only when the full profile applied").

**Options:**
- **(B) recommended, changed from v1's (A):** the probe is already a bash guest, so it
  also prints the container's own limits.
  - It reads `memory.max`, `memory.swap.max`, `pids.max` and `cpu.max` from the cgroup-v2
    files, plus the `/tmp` size from `stat -f`.
  - The selector compares each value with what it requested. Any soft control that
    doesn't match makes the verdict `container-degraded`, with detail
    `limits not verified: <names> (<observed values>)`.
  - Flags stay on the command line; the runtime accepted them.
  - No runtime-specific text parsing. A host whose cgroup view hides the container's own
    limits underclaims, which is the safe direction.
  - **cgroup v1 fallback** (no `cgroup.controllers`): readback is unavailable, so the
    warning scan is the signal. It is broadened to `WARNING…`, `WARN[` and `level=warning`
    (tested with Docker's and Podman's real lines), and any match makes the verdict
    degraded with the quoted line.
- (A) v1's choice: any warning ⇒ degraded. It depends on log formats it already got wrong
  once, and can't see a limit dropped silently.
- (C) Defer. That leaves a known overclaim in the step whose job is to verify claims.

### S7-D4 — `docs/GETTING_STARTED.md`: archive the M0 walkthrough, write a current dev guide

Unchanged from v1 (skeptic: sound). Today it is "Getting Started (Milestone 0)", a
500-line scaffold walkthrough:
- its M0-era code listings no longer match `src/`
- it registers at local scope with an active venv
- it ends with "Hand off to Claude Code for Milestone 1"
- the README still links to it

**Options:**
- **(A) recommended:** move the walkthrough verbatim to
  `docs/archive/M0-scaffold-walkthrough.md`, with a one-line "historical, M0-era" banner.
  Write a short new `GETTING_STARTED.md` covering:
  - dev install (`pip install -e ".[dev]"`)
  - registration (`--scope user` vs local), plus a hand-edited JSON config with `env` for
    other MCP clients
  - running the gate, and how the Docker tests skip
  - troubleshooting: `Blocked:` messages, retry vs restart, image-store switch
- (B) Banner + fix only the registration line.
- (C) Delete it (git keeps it).

## 4. Other 7a changes (bug fixes; covered by the plan sign-off, no separate decision)

### 4.1 The probe file name is random per probe
- **Today:** the probe always writes `/workspace/.vestibule-probe`.
- **Failure:** one guest running `os.mkdir('/workspace/.vestibule-probe')` makes every later
  selection fail both probes. That includes future server processes, because the workspace
  persists. The refusal leads with the runtime ("cannot enforce the isolation profile"),
  with the real cause only in its parenthetical ("Is a directory"). The agent loses its
  sandbox until someone deletes the directory by hand.
  - `verifier` reproduced this in RW mode.
  - RO mode is unaffected, because a failed write is what its probe expects.
  - Any unwritable entry at that name (a mode-000 file, a dangling link) should break it
    the same way.
- **Fix:** the name becomes `.vestibule-probe-<random hex>`, passed into the script.
- **Test:** plant a directory at the old name; a fresh selector must still pass.

### 4.2 `run_code` description: how to import workspace modules
Scripts run as `/sandbox/main.<ext>`, so Python's `sys.path[0]` is `/sandbox`, and `import
helper` of a file saved in `/workspace` fails. That is exactly the multi-module case
criterion 9 exists for.
- **Fix:** the description states the idiom (`sys.path.insert(0, "/workspace")` in Python;
  absolute `require('/workspace/x.js')` in Node), and criterion 9's test uses exactly that.
- **Rejected:** `-e PYTHONPATH=/workspace`. It puts a guest-writable directory ahead of the
  stdlib in *every* run, so one run could plant `json.py` and silently change all later
  runs. With the idiom, only runs that opt in take that on.
- This and the S7-D2 sentence are the only `run_code` description edits. The description
  is a prompt, so both stay one clause each.

## 5. Test design

### 5.1 Layout
- `tests/dockerutil.py` (new): the Docker skip logic and helpers move out of
  `test_container.py`, so both Docker test files share one skip rule. Behavior is
  unchanged: tests skip with the exact pull commands and never pull.
- `tests/test_acceptance.py` (new, Docker-marked, 7b):
  - criteria 1–12 plus the in-guest controls test, one test per criterion, named
    `test_cNN_…`
  - the docstring carries the criterion → test map, including the existing tests it
    relies on
  - a `tool` fixture swaps in `server.LIMITS` (temp workspace) and `server.SELECTOR`
    (fresh), and calls `aclose()` at teardown
  - every result is checked against the allowed isolation values, so `native` can never
    appear
- `tests/test_stdio_e2e.py` (new, Docker-marked, 7a): §5.2.
- `tests/test_select.py` / `test_lifecycle.py` (7a, Docker-free):
  - S7-D1 cases, with cancellation delivered **through an anyio task group**, the way the
    SDK delivers it, not plain `task.cancel()`. Covered both at shutdown and as a
    mid-session per-request cancel during a failed selection: the finisher must complete
    and the shared slot count must return to `max_concurrent`.
  - tracked and pruned backends; shared slots across re-selection (same semaphore object)
  - `note_result` identity
  - batched `rm -f` at `aclose`
  - the `os._exit` branch is chosen when tasks are still pending (it is mocked)
  - S7-D3: matching readback ⇒ `container`; mismatch ⇒ degraded with names; v1 + Docker's
    or Podman's warning line ⇒ degraded; v1 + no warning ⇒ `container`
  - S7-D5: the command starts with the `timeout` wrapper at `timeout_s + 90`
  - §4.1: random probe name
- `tests/test_docs.py` (new, Docker-free, 7b): relative Markdown links **and** any
  `docs/…\.md` path written in `README.md`, `docs/**/*.md`, `.claude/**/*.md` or
  `src/**/*.py` must point at existing files. This is a path pattern, backticked or not;
  `verifier` found the `src/` mentions are plain text. Dated records are allowlisted: the
  `CLAUDE.md` changelog and `docs/reviews/`.

### 5.2 The stdio end-to-end test
It spawns the console script `vestibule-mcp` from the venv, the same binary Claude Code is
registered with.
- **Environment:** copied, **minus every `VESTIBULE_*` variable**, with
  `VESTIBULE_WORKSPACE` set to a temp dir.
- **stderr:** drained concurrently. The test identifies its containers from the server's own
  `run <id>: spawning` log lines, so it never confuses them with Victor's live server's
  runs. Cleanup removes only those exact names.
- **Protocol:** raw JSON-RPC (`initialize`, `notifications/initialized`, `tools/list`,
  `tools/call`). That way the test controls stdin closure and sees every byte.

1. **Happy path:**
   - `tools/list` shows exactly `run_code` and `read_workspace`
   - python, bash and node report `isolation: container`
   - a produced file reads back through `read_workspace`
   - every stdout line parses as JSON-RPC 2.0
   - closing stdin makes the process **exit on its own within 3 s with code 0** (measured
     time recorded)
2. **Shutdown with a run in flight:**
   - start a `sleep 3600` run and wait until its container is running
   - close stdin
   - the process exits within 3 s, and **that container is already gone** right after
     exit (at most 2 s of polling for daemon bookkeeping, far shorter than any natural end)
3. **Shutdown during the first call's probe:**
   - close stdin as soon as the probe's `spawning` line appears
   - same assertions: prompt exit, no leftover probe container, no hang

Each test holds a hard timeout and kills the server if exceeded, so a regression fails the
test rather than hanging the suite.

**Red first.** Tests 2 and 3 run once against the *unchanged* code, and the outcome (hang,
leftover container, exit time) goes into BUILD_LOG as evidence. The fix doesn't depend on
reproducing it, because the mechanism is already established.

### 5.3 Safety of the destructive tests
- The fork bomb's classic phase runs only after the controlled phase (same test) has shown
  the pids cap biting. With a cap of 128, CPU at 0.75 and the S7-D5 watchdog, it is
  bounded.
- The memory bomb is bounded by `--memory` = `--memory-swap`.
- Every test that can leave a container polls by owner label or exact name and removes in
  `finally`.
- No test pulls an image, changes the image store, stops Docker, or touches a container it
  didn't create.

## 6. Docs pass (7b)

| File | Change |
|---|---|
| `README.md` | Status callout + M1 row (**accepted** only after the live check, §8). **Configuration** table: all 15 `VESTIBULE_*` variables from `config.py`. **Know the limits** bullets: the workspace is writable and **not size-capped** (contract §3 bounds only the rootfs and `/tmp`); the S7-D2 user warning; the S7-D5 residual; Podman is experimental (D4); on Docker Desktop the guest runs in a Linux VM, on Linux it shares the host kernel, and `SECURITY.md` comes in M4. The override contract adds `timeout`. Documentation table: new plan paths, `HISTORY.md`, `BUILD_LOG.md`. |
| `docs/GETTING_STARTED.md` | Per S7-D4. |
| `docs/archive/M0-scaffold-walkthrough.md` | New (S7-D4 A): the old walkthrough verbatim + banner. |
| `docs/PLAN.md` | M1 marked done; contract path updated. |
| `docs/plans/M1/M1-container-backend.md` | Status → accepted. §3 gains the S7-D5 wrapper. D1 gains the S7-D2 note. §9 gains an acceptance record (criterion → test). |
| Plan-move references | Every live pointer to `docs/plans/M1-*.md` → `docs/plans/M1/M1-*.md`: README, PLAN, BUILD_LOG links, HISTORY, the plans' cross-references, and `src/` docstrings (`config.py`, `workspace.py`, `container.py`, `select.py`). `.claude/WORKFLOW.md`, `WORKFLOWDIAGRAM.md` and the agent files say `docs/plans/<milestone>/<step>.md`. Dated records stay verbatim. A HISTORY bullet records the move. `test_docs.py` enforces the rest. |
| `docs/HISTORY.md` | Plan-move bullet, S7 sign-offs, 7a done, 7b done, the D1 amendment, M1 accepted. |
| `docs/BUILD_LOG.md` | 7a and 7b entries. Known issues: the drain issue closed; new rows for guest symlinks (documented; M2 seccomp / M4), the uncapped workspace (by design), leftover `vestibule-run-*` temp dirs after a hard kill, and the S7-D5 SIGSTOP residual. |

## 7. Code changes (7a)

| File | Change |
|---|---|
| `src/vestibule/backends/base.py` | `Warden.drain()`: async no-op default. |
| `src/vestibule/backends/container.py` | `timeout` wrapper (S7-D5); optional shared `slots`; in-flight counter + `idle()` + `active_names()`; non-cancelling, looping `drain()`; CLI reap on cancellation; `unverified` / `degraded_note` ⇒ `container-degraded` + detail (S7-D3). |
| `src/vestibule/backends/select.py` | Backend registry + pruning; semaphore created once; `note_result(warden, result)`; non-cancelling candidate handling; `aclose()` (batched `rm -f` 1.5 s + wait 0.5 s, returns pending); readback probe + v1 warning fallback (S7-D3); random probe name (§4.1). |
| `src/vestibule/server.py` | `_main` `finally` ⇒ `aclose()`, then `os._exit(0)` if tasks are pending; `note_result` call passes the warden; `run_code` description: the S7-D2 and §4.2 clauses. |

No new config knobs, dependencies, or tools. The run path changes only by the wrapper and
the semaphore source.

## 8. Build order

**7a**
1. Move the Docker helpers to `tests/dockerutil.py`; gate stays at 122.
2. Write `test_stdio_e2e.py`. Run tests 2–3 **red-first** on unchanged code; record the
   result.
3. S7-D1 + its Docker-free tests ⇒ the stdio tests go green.
4. S7-D5, S7-D3, §4.1, §4.2, S7-D2 sentence, each with its tests.
5. Gate → `critic` → `verifier`, looping per WORKFLOW. HISTORY + BUILD_LOG. Staging list,
   which includes Victor's plan renames (`git add -A docs/plans`). **Victor commits 7a.**

**7b**

6. `test_acceptance.py`, criterion by criterion. A failing criterion is a finding, not a
   test to loosen: stop, diagnose, and bring it back if it touches a decision.
7. `test_docs.py` + the docs pass (§6).
8. Gate, plus the Docker-hidden-from-PATH run → `critic` → `verifier`. The suite is the
   evidence for M1, so it gets reviewed like code. **Victor commits 7b.**

**Live check**

9. Victor restarts Claude Code; the install is editable, so the restart loads the new code.
   In the new session Claude calls the registered server:
   - python / bash / node hello
   - a network attempt
   - a memory bomb
   - `read_workspace` on a produced file
   - `read_workspace` on a guest-planted link that points *inside* the workspace, deleted
     by a follow-up run so no hazard is left in the real workspace

   Also recorded: whether ending that Claude Code session leaves the server exiting
   cleanly with no container behind (Claude Code's real shutdown window, S7-D1 point 4).
   Results go into HISTORY. Then README / PLAN / HISTORY say **M1 accepted**, in a small
   docs-only commit.

## 9. What can go wrong

- **Timing flakiness on a cold Docker Desktop.** Warm-up calls, generous per-test
  timeouts, and polling for conditions rather than sleeping.
- **The batched `rm -f` needs more than 1.5 s on a slow daemon.** The container survives
  until S7-D5's self-deadline. The hard exit still guarantees no hang.
- **`os._exit` skips interpreter finalization.** That is acceptable: the server holds no
  state worth flushing besides stderr, which is flushed first. It only fires when tasks
  are still pending at exit.
- **A guest exit code collides with the wrapper's (124–127, 137).** 125 is already
  hedged (step 6), and 137 already meant "killed". Nothing new is claimed.
- **Readback shows a different but stricter value**, for example a runtime rounding memory.
  The comparison is exact for the four cgroup files, as verified here, and for `/tmp` it
  allows a size ≤ the request. A mismatch underclaims, which is safe and diagnosable.
- **Shared slots refuse a probe** while a dropped backend is busy. Legible and accurate
  (S7-D1).
- **Suite time:** about +70 s over today's 38 s.

## 10. Definition of done

- **7a:**
  - the stdio tests are green, with red-first evidence recorded
  - the Docker-free tests are green
  - gate is green; `critic` and `verifier` have run with no CONFIRMED high or medium open
  - HISTORY and BUILD_LOG written; staging list delivered
- **7b:**
  - every criterion has a passing test or a mapped existing one, recorded in the
    `test_acceptance.py` docstring and the contract §9 record
  - the in-guest controls test is green
  - gate is green, including the Docker-hidden run
  - `critic` / `verifier` clean
  - docs per §6, HISTORY and BUILD_LOG written, staging list delivered
- **M1 accepted:** the live check passes, then the docs-only follow-up.
- Suggested messages: `fix: M1 step 7a — shutdown & lifetime hardening, verified limits`
  and `feat: M1 step 7b — acceptance suite + docs pass`.

## 11. Skeptic findings (2026-10-11) — dispositions

`skeptic` reviewed v1: "rework needed for S7-D1; everything else sound with amendments".
`verifier` checked the code claims with repros (four throwaway containers built from the
pinned image, all removed) and the decisive cancellation case through the real
`BackendSelector` + `ContainerBackend.drain`. Verifier verdicts and severities are in
brackets; "—" marks a design argument with no code claim to check.

| # | Sev | Finding (short) | Verifier | Disposition |
|---|---|---|---|---|
| 1 | high | v1's drain uses `gather`, so the SDK's anyio cancellation cancels the very finisher it waits for (shutdown mid-probe ⇒ the step-5 hang mechanism) | CONFIRMED [med]. Corrections: also hits mid-session per-request cancels; a cancelled-before-start finisher skips its slot release | **Accepted:** S7-D1 points 1–3 (tracked backends, non-cancelling waits, re-raise on cancel); anyio-delivered tests at shutdown and mid-session; stdio test 3 |
| 2 | high | Real clients give ~2 s, then kill the tree; v1's 20 s bound and "returns instead of hanging" don't hold | SDK facts CONFIRMED [info]; "killed CLI ⇒ container keeps running" CONFIRMED live [high] | **Accepted:** S7-D1 points 4–5 (batched `rm -f` first, 1.5 + 0.5 s, `os._exit` if tasks remain); **new S7-D5** in-container self-deadline; live check records Claude Code's real window |
| 3 | med | Workspace modules aren't importable (`sys.path[0]` = `/sandbox`) | CONFIRMED live [med] | **Accepted:** §4.2 documented idiom; `PYTHONPATH=/workspace` rejected (stdlib shadowing across runs) |
| 4 | med | S7-D2 under-scoped: "no structural fix" is false (seccomp); writes missing; description is weak; D1 wording now false | — | **Mostly accepted:** seccomp corrected, now option D, rejected for M1 with reasons; write direction added; README user warning; D1 amendment note. **Rejected:** the post-run link scan, which is evadable by the attacker it targets (mtime reset, scan-budget flooding) and noisy with venvs; it belongs in M3's audit record |
| 5 | med | Fixed probe filename lets one guest disable selection permanently | CONFIRMED live, RW mode only [med] | **Accepted:** §4.1 random name + regression test |
| 6 | med | S7-D3's `startswith("WARNING")` misses Podman's logrus lines | CONFIRMED [low] | **Accepted, and the decision changed:** S7-D3 is now cgroup readback (B), with a broadened warning scan as the cgroup-v1 fallback |
| 7 | med | No test checks hard-tier controls inside the guest | CONFIRMED [med]. Correction: the existing rootfs test can't tell read-only from permission denied | **Accepted:** in-guest controls test (§2); criterion 5 now uses `/var/tmp` (`EROFS` only via `--read-only`) + `/proc/self/mounts` |
| 8 | med | Stdio test 2 can false-green (`--rm` alone), races the live server's containers, risks label-wide cleanup, inherits `VESTIBULE_*` | — | **Accepted:** containers identified by the server's own `run <id>: spawning` lines; exact-name cleanup only; `VESTIBULE_*` stripped; ≤ 2 s poll right after exit |
| 9 | low | `note_result` can't tell which backend produced a result | CONFIRMED [low] | **Accepted:** `note_result(warden, result)` |
| 10 | low | Where the shared semaphore is created; when the in-flight count rises | — | **Accepted:** created once on the first `get()` (a test asserts the same object across re-selection); counter raised before preflight |
| 11 | low | Cancelled runs leave the `docker run` CLI untracked | CONFIRMED [low] | **Accepted:** S7-D1 point 8 |
| 12 | low | Exit 137 isn't proof of an OOM kill | — | **Accepted:** child-process bomb + `memory.events` `oom_kill` |
| 13 | low | Criterion 4a is unbounded if the cap is missing; criterion 5 should create new files | — | **Accepted:** `pids_max + 64` bound; criterion 5 per finding 7 |
| 14 | low | "Run everywhere" overclaims (Windows only, no CI) | — | **Accepted:** recorded as "Windows host"; the Linux run comes with M2 |
| 15 | low | Link-only doc checker misses most stale paths | CONFIRMED [low]. Correction: `src/` mentions aren't backticked | **Accepted:** path-pattern scan, formatting-independent |
| 16 | low | Criterion 6's elapsed time includes selection | — | **Accepted:** warm-up call first |
| 17 | low | Live check leaves a hazardous link in the real workspace | — | **Accepted:** link points inside the workspace and is deleted by a follow-up run |
| 18 | low | Factual: node isolation is asserted; spawn the console script | CONFIRMED [info] | **Accepted:** fixed in v2 |

Process note: `skeptic` wrote two simulation scripts into the session scratchpad. That is
outside the repo, but it goes against its read-only rule. The permission
classifier blocked `verifier` from running them, so it wrote its own repro against the real
code instead. The scripts were deleted afterwards.

## 12. Critic findings on the 7a diff — dispositions

**Round 1** (2026-10-11): `critic` found 2 medium and 5 low. `verifier` confirmed 6 and
marked 1 plausible, with a Docker-free repro for finding 1. All 7 are fixed. The gate went
from 168 to 173 tests. Verifier severities are in brackets.

| # | Sev | Finding (short) | Verifier | Fix |
|---|---|---|---|---|
| 1 | med | On Python 3.11, the server's `asyncio.wait_for` ran the run in an inner task. That task could outlive a handler the SDK cancelled through its anyio scope, so the run was untracked and still in flight at shutdown. `aclose()` ignored in-flight runs and could report "clean", and 3.11's `wait_for` can also swallow a cancel as a slot frees. This reopens the cancel-all hang | CONFIRMED by repro [med] | **Run stays in the handler task:** `asyncio.timeout` replaces `wait_for` around `warden.run`, the slot acquire, and the CLI calls. **Cleanup accounting:** `aclose()` waits on in-flight runs and is never "clean" while one remains; `_shutdown` hard-exits if *any* other task is still alive. **Stray processes:** a cancelled CLI call kills its process. **Test:** a new anyio-delivered regression test fails on the old `wait_for` code (mutation-checked) |
| 2 | med | The e2e happy path matched the substring `isolation: container`, which `container-degraded` also contains | CONFIRMED [low] | Whole-line regex |
| 3 | low | The probe-shutdown e2e "container gone" check can't fail: the probe guest ends by itself | CONFIRMED [low] | The test now asserts what it can prove (prompt exit, selection cut off mid-probe) and says so. Removal of an in-flight container is asserted in the run-in-flight test, now including the server's own `shutdown: removing 1 container(s)` line |
| 4 | low | Rejected-candidate drain was unbounded, contrary to the plan | CONFIRMED [low] | `drain(10 s)`. Any leftovers stay tracked for `aclose()` |
| 5 | low | The anyio drain test never checked that the shared slot comes back | CONFIRMED [low] | The fake cleanup now owns and releases a slot, like `_finish_run`. The test asserts the budget is whole again |
| 6 | low | An image without `timeout` (now required by S7-D5) got a refusal naming `bash`, and the 200-character cut hid runc's "timeout" | CONFIRMED [low] | The refusal names bash and coreutils `timeout`; the error excerpt is raised to 400 characters |
| 7 | low | On a Linux host the guest runs as your uid and can `chmod` the workspace root, after which every later selection fails and the runtime is blamed (the same lockout class as §4.1) | PLAUSIBLE [med] (no Linux host to run it) | A probe failure that mentions `/workspace` gets its own refusal, naming the host folder and the permissions fix. It is retryable, not restart-only |

**Round 2** (2026-10-11): `critic` re-reviewed the whole diff after the round-1 fixes and
found no high issues, 1 medium and 5 low. `verifier` (live Docker trials for #1 and #6)
confirmed 6 and **refuted #1 as stated**, downgrading it to low.

| # | Sev | Finding (short) | Verifier | Disposition |
|---|---|---|---|---|
| 1 | med | A run cancelled while `docker run` is still creating its container: the finisher's `kill` and `rm -f` both miss, then the CLI starts the container, which runs until its S7-D5 deadline | **REFUTED as stated** [low]. The race is real (both missed in 7 of 12 live tries), but 0 of 12 containers survived: `_cleanup` removes the script dir right after, so the late guest finds no script and `--rm` removes it | That guard was accidental, so it is now **deliberate and documented** in `_cleanup`. New Docker regression test: three runs cancelled at spawn, none may remain. Residuals go to BUILD_LOG: a client tree-kill between create and start leaves a *Created* (never started) container until the reaper; the slot is released a few hundred ms before such a container is gone |
| 2 | low | `test_probe_leaves_no_trace` checked the old fixed probe name, so it could never fail | CONFIRMED [low] | Asserts that no `.vestibule-probe*` entry remains |
| 3 | low | No visible cgroupfs was treated as cgroup v1, so plain `container` was reported with nothing verified | CONFIRMED [low] | The probe prints `cgroup=none` unless v1 controllers are visible, which gives `limits not verified: the container cannot see its cgroup` |
| 4 | low | The S7-D2 sentence covered reads only; writes through a link are the worse case | CONFIRMED [low] | Description: "read … with read_workspace and change them with run_code, never with other file tools … for reads and writes" |
| 5 | low | The e2e shutdown budget (3 s) was looser than real clients (kill at 2 s) | CONFIRMED [low] | The tests shut down exactly like the SDK client: close stdin, kill at 2.0 s. Measured shutdown is about 0.2–0.4 s, with or without a run in flight |
| 6 | low | A node override without `timeout` gives exit-127 non-runs reported as `container` | CONFIRMED with corrections [low]: tini's error names the missing command, and the container did start under the full profile | **No change.** No protection is overclaimed, the stderr names the cause, and the README states the override contract. Same behavior as a node image missing `node` before S7-D5 |
| 7 | low | Linux/macOS: a client that sends SIGTERM without closing stdin skips `aclose()` | CONFIRMED [low]. The only client installed here closes stdin first; Claude Code's behavior is unknown | **Deferred to M2** (Linux, where it can be tested), and recorded in BUILD_LOG known issues. Meanwhile S7-D5 bounds the container's lifetime |

**Found while verifying cleanup (not a review finding):** the step-4 test
`test_result_not_delayed_by_cleanup` stubbed `_cleanup` without removing the run's temp
dir, so every full suite run leaked two `%TEMP%\vestibule-run-*` dirs (49 had built up).
The stub now cleans up. A per-file leak count is now 0, and the leftovers, all test
artifacts, were deleted.

No third `critic` round: the round-2 fixes stay inside the flagged sites (a comment, the
description sentence, the readback's cgroup branch) plus tests.

**7a end state:** gate green, **175 passed, 0 skipped** (122 at baseline), with no
leftover containers or temp dirs after a full run.
