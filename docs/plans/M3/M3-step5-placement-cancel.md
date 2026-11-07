# M3 step 5 fix — a cancel during native placement leaves a scope

Status: **v2, signed off 2026-11-06.** v1 was reviewed by `skeptic` (7 findings, 1 high,
verdict "sound with amendments"). `verifier` confirmed 5, confirmed 2 in part and found 1
plausible, refuting none. It ran the two experiments that settled the cause (§6). Victor asked
on 2026-11-06 for this fix to come before 5b.

It closes the known issue that 5a's review found
([`M3-step5-acceptance.md`](M3-step5-acceptance.md) §9, finding 6; `docs/BUILD_LOG.md`'s
2026-11-06 native-placement row). The bug is older than M3: it comes from M2 step 4's placement
([`M2-step4-cgroups.md`](../M2/M2-step4-cgroups.md), S4-D3, S4-D6). This plan amends S4-D6
(§5).

## 1. The problem, measured

Between spawning the paused helper and releasing it, `NativeBackend._release` awaits
`native_cgroup.place()`. That asks systemd for the run's scope (`busctl` StartTransientUnit),
waits until the helper is in it, then builds the layout in a thread (about 15 ms in all).
StartTransientUnit returns once systemd has queued the job; systemd attaches the helper when the
job runs, a moment later.

**The cause** (verifier, experiment 2): systemd accepts the pid of a **zombie**, a process that
has exited but isn't reaped yet. cgroup v2 then moves nothing.
- The scope is born empty: `active`, `running`, `TasksCurrent=0`, `populated 0`.
- It never sees itself become empty, so it stays until its `RuntimeMaxSec` (the timeout +
  30 s), or until it is stopped.
- Reaping the zombie later doesn't change that.
- A pid already reaped is refused outright ("No such process").
- A live process that exits after the attach is removed at once (the control).

**How a run gets there:**
- **A cancel** (Ctrl+C in Claude Code) while `place()` is in flight. `place()` kills `busctl`,
  but its request is already out. `_release`'s `finally` closes the helper's release pipe, and
  the helper exits at once on that EOF (`native_helper.py:105-107`). If systemd's job attaches
  it before the event loop reaps it, the scope is empty for good. `run.cgroup` was never set, so
  nothing on our side knows the scope exists.
- **`place()`'s "the helper was gone before placement" return** has the same exposure: a helper
  that died on its own during placement can be attached as a zombie.
  - The verifier ran an exit-at-once stand-in helper 460 times and left 0 scopes, so here the
    window is narrow.
  - BUILD_LOG's open 2026-11-04 flake (a scope left by
    `test_a_helper_that_reports_nothing_claims_nothing`) may be this; that is unproven.

**The spike** (2026-11-06; scratch, not committed). Native runs were cancelled at a random 0–20
ms after the "starting a native sandbox" line:
- **today's code:** 2 of 40 left that empty, active scope, both cancelled late in the window
  (12.4 and 16.1 ms). The verifier had seen 2 of 20;
- **with a stop by name in the finisher, monkeypatched in:** 0 of 60, then 0 of 100. That
  matches "about 1 in 20 prevented". The 23 units stopped in those 100 runs also include scopes
  systemd would have removed on its own, so they aren't a count of leaks prevented (finding 8).

**What a leftover costs:** no process and no memory. It's a unit in `systemctl --user` for up to
timeout + 30 s, which the leftover-scope checks in the tests and the live check count as "left
behind".

## 2. The fix

1. **`native_cgroup.stop_unit(run_id)`** (new, async). It asks systemd to stop
   `vestibule-<run_id>.scope`: `busctl --user call … StopUnit ss <unit> replace`.
   - **The spawn:** the same `bus_env()`, with stdin and stdout both `DEVNULL`. stdin is the
     server's MCP channel, and stdout is golden rule 1 (finding 5).
   - **One bound** of `_PLACE_WAIT_S` (5 s), with `busctl` killed past it, as in `place()`.
   - **Without a bus** it returns at once.
   - **Its outcomes:**
     - **the unit stopped:** INFO, "stopped its scope by name". Not every stop is a leak
       prevented (§1), so the line doesn't claim one;
     - **"not loaded"** (there was none): nothing logged;
     - **any other failure, or the bound passing:** a warning;
     - it never raises.
2. **`_unit(run_id)`** names the scope, for `place()` and `stop_unit` alike, so the two can't
   drift apart.
3. **`native_cgroup.HELPER_GONE`,** the constant `place()` returns when the helper was gone
   before placement (today's text, unchanged).
4. **`_Run.stray_unit`** (new, `False`): a unit systemd may have made that nothing else will
   stop. In `_release`:
   - `run.stray_unit = True` just before `await native_cgroup.place(...)`;
   - after it returns, `run.stray_unit = (placed == native_cgroup.HELPER_GONE)`.

   So it stays set for a cancel, or an error raised out of `place()`, and for the helper-gone
   return. Every other return clears it:
   - a `Placement`: the helper is alive in its scope, which systemd removes once it exits;
   - no cgroup v2, no bus, `busctl` not found, `busctl` exiting non-zero: no unit was made;
   - the layout failing: the helper is alive in its scope;
   - the 5 s bound. Before `busctl` answers, the bus is hung, so a stop would only cost another
     5 s. The helper is alive, so a scope systemd makes later holds it and is removed when it
     exits. The bound can also pass after `busctl` answered, while `_in_scope` waits for a slow
     start job. A stop would then be cheap, and a scope is left only if that job attaches the
     helper after it exits but before it is reaped. Covering it would need `place()` to say
     which phase timed out, so it's stated as not covered (§3; the review's finding 1).
5. **`_finish`,** the detached finisher: after the helper is gone and the status pipe drained,
   `if run.stray_unit: await native_cgroup.stop_unit(run.run_id)`. This comes before the slot is
   handed back, inside the same `try`, so a failure is logged and the slot is still released.

**Why the finisher, after the helper:**
- The finisher is detached and `drain()` waits for it, so the stop survives the request's
  cancel. A stop sent from the request would be cancelled with it.
- Once the helper is reaped, the scope can't gain it anymore, so the stop is final.
- **The order is safe** (the skeptic's check). The placement request went out before `busctl`
  was killed, and the stop's `busctl` connects only after the helper is reaped. Both travel
  over the user bus to systemd's one connection, in that order, so the stop can't overtake the
  start.

**Cost:**
- A run that leaves `stray_unit` set pays one `busctl` call in the finisher, about 10 ms. At
  worst, with a hung bus, it pays 5 s more before the slot is handed back.
- Every other run pays nothing.

**This amends S4-D6:** its "the finisher does nothing new" and its §5 edge case, which says a
scope made during a cancelled placement empties and is removed. Both get an amendment note
(§5).

## 3. Edge cases

| Case | What happens |
|---|---|
| The cancel lands before `busctl` sent its request | No unit; the stop answers "not loaded", nothing is logged |
| The unit's start job is still pending when the stop arrives | `replace` turns it into a stop job; the unit ends inactive and is collected (`CollectMode=inactive-or-failed`) |
| The cancel lands after `place()` returned a `Placement` | The helper is alive in its scope; the finisher's SIGTERM ends it, and systemd removes the scope (the control in §1) |
| A second cancel (anyio repeats it) | The kill of `busctl` is already sent, and the finisher is created synchronously in `run()`'s `finally`, so nothing is lost |
| **Any POSIX shutdown during placement** (SIGINT, SIGTERM, stdin EOF, a hard kill) | `_hand_off_and_exit` ends in `os._exit`, and no native finisher runs, so the scope may be left until `RuntimeMaxSec`, as today. A residual, stated (finding 7) |
| A hung user bus | The stop gives up at 5 s with a warning; the slot is handed back then |
| The 5 s placement bound passes | Not covered, as §2.4 says. A scope can be left only when systemd's start job runs more than 5 s late and attaches the helper between its exit and its reap |
| No bus at all | `place()` returns at once and clears `stray_unit`; `stop_unit` would return at once anyway |

## 4. Tests (red first)

**The stand-in for the race** (finding 1): `native_cgroup._start_unit` is wrapped so that it
hands systemd the pid of a zombie the test owns, in place of the helper's. The zombie is
`subprocess.Popen(["true"])`, not waited on, checked as state `Z` in `/proc` before the run.
systemd then makes exactly the empty, active scope of §1, with no cgroup write from the test.
On WSL a write would fail anyway: pytest sits in the root-owned `/init.scope`. The zombie is
reaped in a `finally`.

1. **`test_a_cancel_during_placement_stops_the_scope_systemd_made`** (`test_native.py`,
   `@cgroups_required`). The helper never shows up in the scope, so `_in_scope` keeps waiting:
   placement is in flight. The test waits until the unit is listed, cancels the run, and
   drains.
   - **Asserted:** the run's unit is gone within 2 s.
   - **On today's code** it is still listed, active, with 0 tasks (red), until its
     `RuntimeMaxSec`.
   - The test stops it in its `finally`, so a red run leaves nothing.
2. **`test_a_helper_gone_before_placement_stops_its_scope`** (`@cgroups_required`). The same
   zombie start, with `_in_scope` patched to return None, so `place()` returns `HELPER_GONE`
   and the run goes on degraded.
   - **Asserted:** the result names "the helper was gone before placement", and after the drain
     the unit is gone.
   - **Red on today's code.**
3. **`test_a_placed_run_never_stops_its_scope_by_name`** (`@cgroups_required`). A normal
   `print(1)` with `stop_unit` replaced by a recorder; after the drain the recorder is empty.
4. **`test_stopping_a_unit_that_isnt_there_is_quiet`** (`@cgroups_required`):
   `stop_unit("0" * 16)` returns within the bound, and logs no warning.
5. **`test_a_cancel_during_placement_leaves_nothing`** (existing; finding 3). The fake `busctl`
   still hangs on every call, so the finisher's stop hangs too. Each call records its pid in
   its own file, by method name.
   - **Asserted, as today:** the drain completes, the placement `busctl` is gone, and no helper
     is left. M2 step 4's mutant "busctl killed only on the bound" stays caught.
   - **Asserted, new:** the stop's `busctl` is gone too, the drain took at least 5 s (the stop's
     bound), and the give-up warning was logged.

**Mutants** (each must turn its test red, after a green control):
- the finisher's `stop_unit` call deleted → 1 and 2;
- `stray_unit` cleared in a `finally` around `place()`, so a cancel clears it too → 1;
- `stray_unit` never cleared after `place()` returns → 3;
- `stop_unit` naming a different unit than `place()` → 1 and 2;
- `stop_unit` without its bound → 5 (the drain's 10 s runs out);
- `stop_unit`'s warning dropped → 5.

**The spike is evidence, not a test.** It goes into the as-built as two counts: leftovers
before and after, and stops made. A statistical race test would flake.

## 5. Changes

| File | Change |
|---|---|
| `src/vestibule/backends/native_cgroup.py` | `_unit`, `HELPER_GONE`, `stop_unit`; `place()` uses `_unit` and `HELPER_GONE` |
| `src/vestibule/backends/native.py` | `_Run.stray_unit`; `_release` sets it and clears it; `_finish` stops the unit |
| `tests/test_native.py` | tests 1–4; test 5's fake `busctl` and its new assertions |
| `docs/plans/M2/M2-step4-cgroups.md` | amendment notes on S4-D6's cleanup line and its §5 edge case |
| `docs/ARCHITECTURE.md` | line 164 ("Nothing is left to tear down") and line 227 (systemd removes the scope): the finisher stops a scope that a cancel or a vanished helper may have left |
| `docs/BUILD_LOG.md`, `docs/HISTORY.md` | the 2026-11-06 row closed, with the shutdown residual; the 2026-11-04 flake row gets "possibly this; still watching"; the entry and bullet |
| this plan | the as-built |

**Size:** src about 40 lines, tests about 90.

**Build order:**
1. Tests 1 and 2 → red on today's code, for the stated reason (the unit still listed, empty).
2. The fix → tests 1–5 green; `test_native.py` and the 5a suite green on WSL 3.14 and 3.11.
3. The spike against the built code → 0 leftovers of 100, and its stop count.
4. The mutants → each red on its test.
5. The gate, `critic`, `verifier`, the docs. Then Victor commits, and 5b starts.

## 6. Review record

`skeptic` reviewed v1: 7 findings, 1 high, 2 medium and 4 low, verdict "sound with
amendments". `verifier` confirmed 5, confirmed 2 in part, and found the flake link in finding 2
plausible. It ran the experiments behind findings 1 and 2.

| # | Severity (after `verifier`) | Finding | v2 |
|---|---|---|---|
| 1 | high | Test 1's sleeper can't be written into the scope's `cgroup.procs` from pytest: the common ancestor is root-owned `/init.scope`. Experiment: `EACCES`. It would be red on both codes for the wrong reason | §4: a zombie pid handed to systemd, which makes the real empty scope with no write |
| 2 | med | The cause is a zombie attach, not a missed watch; `place()`'s helper-gone return leaks the same way. Experiment: a zombie's scope stays active, empty, even after the reap; a live process's is removed | §1 rewritten; §2.4 keeps `stray_unit` for `HELPER_GONE`; test 2. The 2026-11-04 flake link is unproven (0 of 460) |
| 3 | med (partly) | The existing cancel test's pidfile would name the stop's `busctl`, so M2 step 4's mutant would survive; the stop's 5 s bound had no test | §4.5: one pidfile per method; the bound and its warning asserted there |
| 4 | low | One mutant duplicated another; "never cleared" and the warning path weren't covered | §4: the `finally` slip, test 3, and the warning mutant |
| 5 | low | stdin wasn't specified for the stop's `busctl` | §2.1: stdin and stdout `DEVNULL` |
| 6 | low | S4-D6 amended without saying so; ARCHITECTURE:164 and :227 become untrue | §2 and §5 |
| 7 | low | Every POSIX shutdown, not just a hard kill, skips the native finisher | §3's row |
| 8 | low | The sleeper had no cleanup; a successful stop isn't a leak prevented | The tests stop their units in a `finally`; §2.1's log line; §1 counts stops apart from leftovers |

## 7. As built (2026-11-06)

**Built as §2 and §4 say, with these changes from the review:**
- **Where the stop sits** (critic round 1, finding 5). §2.5 put the stop inside the finisher's
  `try`, but an exception from an earlier step would then skip it. It now sits in the
  `finally`, in its own `try` whose `finally` releases the slot. So it runs after a failed step
  or a cancel too, and the slot is released exactly once. A non-`OSError` raised by `stop_unit`
  (it has none it can realistically raise) would surface as the task's exception instead of
  through `log.exception`.
- **Two more tests and two more assertions** (critic round 1, finding 2; round 2, findings 2
  and 3):
  - `test_a_hanging_busctl_costs_at_most_the_placement_bound` now asserts that the finisher
    sends no stop after a placement that returned: the drain is quick. Without it, clearing
    `stray_unit` only on a `Placement` passed every test;
  - `test_a_stop_that_fails_is_logged_and_replaces_any_start_job` checks the stop's argv ends
    in `replace`, and that a refusal other than "not loaded" is a warning.
- **Test hygiene** (round 1, findings 3 and 4): `_gone` asserts that `systemctl` answered, and
  test 1 cancels its run in the `finally` if its wait failed.
- **§2.4's reason for the 5 s bound was wrong in one sub-case** (round 1, finding 1), and is
  corrected there and in §3: the bound can pass after `busctl` answered. That sub-case stays not
  covered.

**Size:** src 52 net lines against about 40, tests about 140 against about 90. `stop_unit`
repeats `place()`'s spawn, bound and kill (§2.1 asked for that bound "as in `place()`"). The
test helpers `_zombie`, `_zombie_scopes`, `_stop_by_hand` and `_gone` are each used twice.

**Red first:** on the old code, test 1 failed with "vestibule-….scope was left behind", test 2
the same, and tests 3–5 failed because `stop_unit` didn't exist.

**The spike against the built code** (no monkeypatch): 0 of 100 cancels left a scope, and the
finisher logged "stopped its scope by name" 24 times, with no warning. Before the fix, 2 of 40
left one. Some of the 24 stops hit scopes systemd would have removed on its own (§1), so they
aren't 24 leaks prevented.

**Mutants:** 9 of 9 red on their named tests after a green control. The six of §4, plus:
- 7, `stray_unit` cleared only on a `Placement` → the hanging-busctl test;
- 8, the failure warning dropped, and 9, `replace` → `fail` → the new failure test.

**Gate:** green after round 1's fixes.
- Linux, 459 s of 540: 3.14 full suite 818 passed, 0 skipped; 3.11 Docker-free 773; stdio 5.
- Windows: 641 passed, plus 157 POSIX/Linux-only skips.

Round 2 changed only `test_native.py`. It then passed with the 5a suite on 3.14 and 3.11 (68
each).

**Review rounds on the diff:** `critic` ran twice, finding 5 low issues each time, and
`verifier` confirmed them (some in part), refuting none.
- **Round 1:** the bound's reason; an untested clear; `_gone`; test 1's cleanup; the stop's
  place.
- **Round 2:**
  - a dead assertion;
  - the untested failure warning;
  - `replace` untested;
  - this plan's §2.5 out of date (this section);
  - the size.

  All fixed, or accepted for the size.
