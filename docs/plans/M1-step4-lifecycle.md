# M1 step 4 plan — run lifecycle: cancellation-resistant cleanup, orphan reaping, concurrency

Status: **signed off and implemented 2026-07-04** (S4-D1/S4-D2 approved with v1; S4-D3 approved with v2 after the Codex adversarial review; built the same day — see `docs/HISTORY.md`).
Parent contract: `docs/plans/M1-container-backend.md` §4 (binding, amended by S4-D1/S4-D3 below).
Review history: v1's age-based reaper was rejected by a Codex adversarial review (finding: a
reaper using *local* `max_timeout_s` can kill legitimate live runs of another Vestibule server
with a larger configured timeout). v2 adopts the review's deadline-label remedy and documents
why it also fixes two further failure modes v1 had (created-state race, Docker Desktop VM clock
drift) that the review's framing surfaced.

---

## 1. Scope

Complete the §4 run lifecycle that step 3 deliberately deferred:

- **Cancellation-resistant cleanup** (§4.5): container + temp dir are cleaned up even when the
  backend coroutine is cancelled (server outer deadline, MCP client cancellation).
- **Orphan reaping** (§4.6, redesigned — see S4-D3): stale labeled containers are removed
  without ever endangering live runs, ours or any other process's.
- **Concurrency semaphore** (D7) with a legible busy refusal (S4-D1).
- Honesty fix for the missing-runtime path (S4-D2).

Out of scope: capability probing / backend selection (step 5), digest pinning (step 6),
acceptance suite (step 7), stale *temp-dir* sweeping (cosmetic; dirs are `ignore_errors`-removed
and prefixed `vestibule-run-` in the system temp).

## 2. Step-level decisions

### S4-D1 (user-approved 2026-07-04) — Bounded semaphore wait, then legible refusal (amends §4.1)
Contract §4.1 said only "acquire semaphore". Literal queueing interacts badly with the server's
outer deadline (`timeout_s + 20`): a call queued behind four 60-second runs would burn its whole
deadline waiting and return the generic "sandbox did not return in time" — a message the model
cannot adapt to. Instead:

- `run()` waits up to **5 s** (`_SEM_WAIT_S`, module constant, not config — avoid config sprawl)
  for a slot.
- On expiry it raises **`RunRefusedError("too many concurrent runs (max N); retry shortly")`** —
  a new typed exception in `backends/base.py`. `server.py` catches it around `warden.run(...)`
  and returns the standard `Blocked: <msg>` content. Handlers still never raise (golden rule 4);
  the model reads the reason and retries.
- Rationale for an exception over a `RunResult`: nothing ran, so fabricating exit codes/isolation
  for a non-run would be dishonest; and step 5 needs the same pathway (hard-tier probe failure ⇒
  every call returns a Blocked explanation), so `RunRefusedError` is built once here.

### S4-D2 (user-approved 2026-07-04) — Honest reporting when the runtime binary is missing
Step 3's spawn-failure path returns `isolation: "container"` for a run that never executed
anything (`container.py` catch of `FileNotFoundError`/`OSError`). Fix: report
`isolation: "none"` with `isolation_detail: "runtime unavailable; nothing was executed"`.
(Step 5's probing later makes this path nearly unreachable; the fix costs two lines now and
keeps golden rule 5 unbroken in the interim.)

### S4-D3 (user-approved 2026-07-04) — Deadline-label reaping (replaces §4.6's age heuristic)

**Every container is stamped at spawn, by its owner, with its own expiry:**

```
--label vestibule.deadline=<unix epoch int>   # spawn_now + timeout_s + 90
--label vestibule.owner=<instance token>      # observability only, never a reap criterion
```

`+90` covers the owner's startup grace (+5), kill/rm budget (+10), and slack. The deadline is
computed *after* the semaphore slot is acquired, so queue time never eats into it.

**The reap rule — one rule, every state:** remove a `vestibule.run=1` container iff
`now > deadline + 60` (`_REAP_MARGIN_S`) and its name is not in this process's in-memory
active set. Containers with a missing or unparseable `vestibule.deadline` are **skipped with a
loud warning, never removed** (never reap on bad data; pre-step-4 dev leftovers get a one-time
manual `docker rm` note in HISTORY).

Why this is correct where v1's age heuristic was not:

1. **Cross-config safety (the Codex finding).** The deadline encodes the *run's own* timeout,
   set by the only party that knows it — its owner. A foreign server's 3600-second run carries a
   3600-second deadline; our reaper honors it regardless of our local `max_timeout_s`.
2. **No created-state race.** `docker run` = create → start; a container sits in `created` for
   ms-to-seconds (longer under Docker Desktop VM wakeup). v1 reaped `created` containers
   unconditionally — that can destroy a run *before it starts*. Under S4-D3 a fresh `created`
   container has a future deadline and is untouchable.
3. **Immune to Docker Desktop VM clock drift.** After host sleep, the WSL2/VM clock that stamps
   `Created`/`StartedAt` can lag the host clock by minutes, making any daemon-timestamp age
   computation overshoot — a seconds-old run can look minutes old. S4-D3 consults **no daemon
   timestamps**: deadline (owner's host clock) is compared to now (reaper's host clock), and in
   every normal deployment those are the same clock.
4. **No liveness oracle needed.** Ownership-only scoping (Codex's first suggestion) can't
   distinguish a crashed owner from a live one without liveness checks that break on PID reuse
   and remote daemons. A deadline is self-describing: past it, removal is correct *even if the
   owner is alive* — the owner promised the container dead by then, so a survivor means the
   owner's own kill failed, and reaping is exactly the desired backstop.

Rejected alternatives: per-workspace/ownership reap scoping (leaves a crashed *other* install's
garbage forever — safe deadlines make global reaping both safe and complete);
`vestibule.timeout=<s>` label + `StartedAt` age (reintroduces daemon-clock dependence, per 3);
host-PID liveness labels (PID reuse; remote daemons).

Residual risk, documented: two *different hosts* sharing one remote Docker daemon with clocks
skewed by more than `_REAP_MARGIN_S` (60 s) could reap each other's runs prematurely. Accepted:
Vestibule targets local daemons; NTP-synced hosts are well inside the margin. Noted for
SECURITY.md/README in step 7.

## 3. Design

### 3.1 `run()` restructure — detached-task cleanup

> **Amended post-implementation (Codex P2 review finding, 2026-07-04):** the sketch below
> originally shield-*awaited* the cleanup task before returning. Codex showed that on the
> guest-timeout path with a slow daemon this serialization could push the backend past the
> server's `timeout_s + 20` outer deadline, replacing the honest timed-out `RunResult` with the
> generic failure message. As built, the cleanup task is **not awaited at all**: the result
> returns immediately, and the finisher task owns the semaphore slot — releasing it only when
> the container is actually gone, so `max_concurrent` still bounds *existing* containers, not
> just accepted requests. Regression test: `test_result_not_delayed_by_cleanup`.
>
> **Amended again (Codex adversarial follow-up, 2026-07-05):** P2 detached the *post-run*
> cleanup, but the guest-timeout branch inside `_execute()` still awaited `_force_remove` plus
> a bounded `proc.wait()` before returning — on a wedged daemon that is kill 5 s + rm 5 s +
> wait 5 s on top of `timeout_s + 5` of collection: the full outer budget, reproducing the P2
> failure mode one path over. Fixed: the timeout branch now detaches entirely — the honest
> timed-out result returns at `timeout_s + 5` (its `exit_code` is `-1`, since the CLI's exit
> is no longer awaited); the finisher performs the container kill; a detached `_reap_cli` task
> collects the CLI process. A CLI that wedges *after* the guest finished is now reported as a
> runtime failure ("exit code unknown"), never mislabeled a guest timeout. Regression tests:
> `test_timed_out_result_returns_before_any_kill`,
> `test_cli_wedge_after_guest_finished_is_not_a_timeout`.

`run()` becomes orchestration; today's spawn/collect/inner-timeout body moves to `_execute()`.

```
async def run(...):
    self._schedule_reap()                          # detached; §3.3 — never blocks the run
    acquired = await wait_for(sem.acquire(), _SEM_WAIT_S)    # S4-D1; timeout -> RunRefusedError
    try:
        deadline = int(time.time()) + timeout_s + 90
        tmpdir = mkdtemp; write script; name = vestibule-<runid>; self._active.add(name)
        try:
            return await self._execute(...)        # step-3 logic incl. inner timeout kill
        finally:
            cleanup = asyncio.create_task(self._cleanup(name, tmpdir))   # independent task
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.shield(cleanup)
            self._active.discard(name)             # unconditionally — reaper is the backstop
            self._schedule_reap()                  # post-run best-effort pass
    finally:
        sem.release()
```

- **The cleanup is its own task**, so the parent's cancellation cannot cancel it. `shield`
  protects against the first cancel; a *second* cancel breaks our `await` but the cleanup task
  keeps running detached. The original `CancelledError` propagates after the `finally`, so the
  server is never lied to about the call's fate.
- `_cleanup(name, tmpdir)` = `kill` (5 s bound) → `rm -f` (5 s bound, "No such container"
  tolerated — races both `--rm` and the inner-timeout kill; all paths idempotent) → `rmtree`
  via `asyncio.to_thread`.
- **Budget** (§4.5): sem wait ≤ 5, collect `timeout_s + 5`, kill 5, rm 5 ⇒ ≤ `timeout_s + 15`
  after the sem wait; the server's `timeout_s + 20` outer deadline holds. Reaping is fully
  detached and costs the run path nothing.
- Event-loop shutdown before a detached cleanup finishes is accepted: reaping exists precisely
  to mop up after hard exits.
- Semaphore created lazily on first `run()` from `limits.max_concurrent` (single-threaded event
  loop, no await between check and set ⇒ no race). Constructor gains only the owner token.

### 3.2 Honest failure paths

Spawn failure (`FileNotFoundError`/`OSError`) per S4-D2. Everything else in `_execute()` is
step-3 code, unchanged.

### 3.3 Orphan reaping — implementation

`_schedule_reap()`: if a reap task is already in flight, do nothing; else `create_task` a
detached `_reap_orphans()`. Called on every `run()` entry (so the *first* call reaps
post-crash leftovers — §5's "at backend init", made async so it never delays a run or the MCP
handshake) and after each run.

`_reap_orphans()`, every CLI call bounded 5 s, failures log-only (stderr):

1. `ps -aq --filter label=vestibule.run=1` → candidate IDs (all states — the rule is uniform).
2. One batched `inspect --format '{{.Name}}\t{{index .Config.Labels "vestibule.deadline"}}'`
   over all candidates.
3. Partition: name in `self._active` → skip; deadline missing/unparseable → skip + warn;
   `now > deadline + _REAP_MARGIN_S` → reap.
4. One batched `rm -f <id...>` for the reap set (single bounded call; errors per-container are
   tolerated — races with `--rm`, owners' cleanup, and other reapers are all benign).

The keep/remove decision is a pure function (`_reap_decision(name, deadline_raw, now, active)`)
so it unit-tests exhaustively without Docker.

### 3.4 Flags/knobs added

None. `_SEM_WAIT_S = 5`, `_REAP_MARGIN_S = 60`, deadline slack `+90`, and the existing
`_CLEANUP_STEP_S = 5` are module constants; `max_concurrent` already exists in `config.py`.

## 4. Files to change

| File | Change |
|---|---|
| `src/vestibule/backends/base.py` | `RunRefusedError` (S4-D1; reused by step 5) |
| `src/vestibule/backends/container.py` | `run()` restructure, `_cleanup`, `_reap_orphans` + `_reap_decision`, semaphore, deadline/owner labels in `_build_command`, S4-D2 fix |
| `src/vestibule/server.py` | catch `RunRefusedError` in `_handle_run_code` ⇒ `Blocked:` content (~4 lines) |
| `tests/test_container.py` (± a new module) | tests below |
| `docs/plans/M1-container-backend.md` | §3 label list + §4.6 annotated as amended by this doc |

## 5. Edge cases handled

- Double cancellation during cleanup → detached task survives (§3.1).
- `--rm` auto-removal vs our `rm -f` vs inner-timeout kill vs another process's reaper → all
  tolerate "No such container".
- Wedged daemon → every kill/rm/ps/inspect call individually bounded at 5 s; reaper retries on
  later runs.
- Foreign server with a larger/smaller timeout config → safe by construction (S4-D3.1).
- Container in `created`/`restarting`/`paused` state → same uniform deadline rule; no
  state-specific races (S4-D3.2).
- Docker Desktop VM clock drift after host sleep → no daemon timestamps consulted (S4-D3.3).
- Exited container whose owner CLI is still draining output → future deadline ⇒ skipped until
  long after the drain completes.
- Bad/missing deadline label → never reaped, warned loudly.
- Cancelled run can't leak a semaphore slot (`finally: sem.release()`) and can't strand its name
  in the active set (`discard` unconditional; the reaper backstops the container itself).
- Multi-host remote daemon with clock skew > 60 s → documented residual risk (S4-D3, accepted).
- Windows `rmtree` on a still-locked mount dir → already `ignore_errors=True`; stale temp dirs
  are cosmetic.

## 6. Test plan

Non-Docker (run everywhere):
1. Semaphore cap: monkeypatch `_execute` with a controllable coroutine; N concurrent `run()`s;
   assert in-flight never exceeds `max_concurrent`.
2. Busy refusal: hold all slots; next call raises `RunRefusedError` within ~`_SEM_WAIT_S`;
   server handler renders it as `Blocked:` content (session survives).
3. `_reap_decision` table test: past/future/missing/garbage deadlines × active/inactive names.
4. Cancelled run releases its semaphore slot and discards its active-set entry.
5. `_build_command` emits `vestibule.deadline` (sane epoch) + `vestibule.owner` labels.

Docker-marked (this machine):
6. Cancellation mid-run ⇒ no surviving container, temp dir removed (the headline §4.5 behavior).
7. Planted labeled container with `vestibule.deadline=1` (epoch 1970), *running* `sleep 300`
   ⇒ removed by the first-run reap — proves state-independence, no margin monkeypatching needed
   (acceptance criterion 8, landed early and strengthened).
8. Planted *running* labeled container with a far-future deadline ⇒ spared (then removed by the
   test itself).
9. Planted labeled container with **no** deadline label ⇒ spared + warning logged.
10. 4 simultaneous hellos all succeed with distinct containers (acceptance criterion 7).

Gate: all 58 existing tests stay green; `ruff check` + `mypy` clean.

## 7. Definition of done

Code + tests land as one commit (`feat: M1 step 4 — …`); `docs/HISTORY.md` gains the step-4
bullet plus S4-D1/S4-D2/S4-D3 sign-off notes and the one-time manual-cleanup note for
pre-step-4 dev containers; CLAUDE.md changelog on session log request.
