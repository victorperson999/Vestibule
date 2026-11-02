# M3 step 1 — usage numbers in every result

Status: **draft (v2), revised after the `skeptic` review.** `verifier` confirmed 12 of the 13
findings and rated one plausible (§8). **Next: Victor's sign-off, including the seven departures
from the contract's wording in §3.1.** Code comes after that.

Inputs:
- the M3 contract, [`M3-audit-usage.md`](M3-audit-usage.md) (signed off 2026-11-01): A1, A1.1
  and A4, and §6's step 1;
- the code at `c5fe2a2`;
- the facts measured on 2026-11-01: the contract's §2 and §7, and this plan's §8.

---

## 1. Scope

**In:**
- **Native reads its counters from outside the sandbox.** After the whole sandbox has exited,
  the helper reads the run's `guest/` cgroup:
  - cpu time;
  - peak memory;
  - peak tasks;
  - refused task creations;
  - cpu throttling.
- **Timed-out native runs get their counters too (A1.1).** After stopping a timed-out run, the
  server waits at most 1 s for the helper's last lines. A test proves the bound.
- **`RunResult` gains `pids_peak`, `limit_hits` and `not_measured`.** `cpu_ms` and
  `mem_peak_mb` already exist, and this step fills them for the first time.
- **The server measures `wall`** around `warden.run`. Every result that has a `RunResult` gets a
  `usage:` line. It also gets a `limits hit:` line when a limit was hit.
- **Every backend says what it didn't measure, and why:**
  - container: the numbers can't be read from outside a container (A1);
  - naive: there's no sandbox to measure;
  - native: one of four reasons:
    - the run had no cgroup;
    - the sandbox didn't report its counters;
    - the kernel lacks a counter;
    - a counter couldn't be read.

**Not in this step** (U6 and U7 in §3.1 ask your OK where the contract says otherwise):
- **`ran`, `run_id` and `image` on `RunResult`.** The audit writer uses them, so they come with
  it in step 2 (U6).
- **The acceptance tests for criteria 2–4,** except the parts this step's mechanism needs: the
  timeout bound and the "didn't report" note (criterion 4) are tested here. Step 5 adds the
  rest, including the guest that prints a fake report.
- **The README's usage section.** Step 5 writes it with the audit section. This step only
  fixes the doc lines its change makes false and adds one prerequisite (§6).
- **The out-of-memory note on a timeout or an overflow.** `_outcome` still returns before it in
  both cases (§3.2).
- **The two results with no `RunResult`:** the outer deadline and an unexpected error (M2
  follow-ups, F3). They keep their exact text, with no `usage:` line (U7).

---

## 2. What changes

### 2.1 The helper reads the counters (`native_helper.py`)

Today, after `os.waitpid(init)`, the helper reads `guest/memory.events` and sends
`{"oom": n, "oom_kill": m}` (S4-D8). That block moves into a function,
`_report_counters(status_fd, guest)`. The function sends two lines, **each in its own
`try`**:
- the out-of-memory line, unchanged;
- then a new line: `{"usage": {…}}`.

A missing `memory.events` therefore never costs the usage line (finding 5). The helper calls
the function when it has a cgroup, the same condition as today. It sends the usage line even
when the line is empty (`{}`). If a send fails because the server is gone, the helper
ignores it, as now.

**The usage line's fields:**

| Key | Source in `guest/` | Present when |
|---|---|---|
| `cpu_usec` | `cpu.stat` `usage_usec` | always (a core file in every cgroup v2 cgroup) |
| `nr_periods` | `cpu.stat` `nr_periods` | the `cpu` controller is enabled |
| `nr_throttled` | `cpu.stat` `nr_throttled` | the `cpu` controller is enabled |
| `mem_peak` | `memory.peak` (bytes) | Linux 5.19+ with the `memory` controller |
| `pids_peak` | `pids.peak` | Linux 6.1+ with the `pids` controller |
| `tasks_refused` | `pids.events` `max` | the `pids` controller is enabled |

- **Raw values, each read on its own.** A file that's missing or doesn't parse is left out. The
  helper interprets nothing; the server converts the values and judges them.
- **`cpu.stat`'s `throttled_usec` isn't used.** The kernel adds it up across CPUs: a fork bomb
  read 8.7 s throttled in a 2.0 s run (finding 1). `nr_throttled` out of `nr_periods` can never
  exceed the run (U3).
- **The numbers are final.** The helper reads after `waitpid(init)`. A PID namespace's init is
  waitable only once every other process in the namespace has gone (`zap_pid_ns_processes`).
  The verifier confirmed this.
- **`guest/` counts the whole sandbox** (finding 11 corrected the reason):
  - **`memory.peak`, `pids.peak` and `cpu.stat`** are hierarchical, so they count `guest/sandbox`,
    where the helper puts init before init does anything (`native_helper.py:122–123`).
  - **`pids.events` is different.** The kernel counts its event in the forking task's own
    cgroup. In this layout that is `guest/`, because `guest/sandbox` has no pids controller of
    its own: only the scope's `subtree_control` enables any. The verifier's repro showed the
    event moving into `sandbox/` once controllers were enabled there.
  - The live fork-bomb test (§5.3) checks this in the real layout.

About 30 lines: `_report_counters`, a `_read_usage(guest)` reader, and the call site.

### 2.2 The wait on a timeout (`native.py`, A1.1)

Today, on a timeout, `_collect` does three things in a row:
1. `_stop(run)`: a SIGTERM to the helper, which SIGKILLs init;
2. it cancels the output collection;
3. it parses `run.status` at once, before the helper's lines arrive.

After the `try`/`finally`, and only when `timed_out`, it now waits for those lines:

```python
if timed_out and run.status_task is not None:  # A1.1: the helper's last lines, init gone
    with contextlib.suppress(asyncio.TimeoutError):
        await asyncio.wait_for(asyncio.shield(run.status_task), _COUNTERS_WAIT_S)
```

- **`_COUNTERS_WAIT_S = 1`.** At the default `cpu_pct` (75), the helper's lines arrive 12–101 ms
  after the stop (measured).
- **The wait comes after the `finally`.** By then the output collection has been cancelled, so
  stdout and stderr stop at the timeout, as today.
- **`shield`, as in the normal path.** The finisher still awaits the same task.
- **Worst case:** timeout + 2 + 1. That's inside the outer deadline (timeout + 30) and inside
  c08's bound (timeout + 2 + 3). §5.3 tests the bound.
- **A cancellation during the wait** propagates out of `_collect`, like any cancellation. The
  finisher stops the run and reaps it, as now.
- **The overflow path is unchanged.** It already waits up to `_STOP_WAIT_S` for the status, so
  its counters arrive already.
- **At a low `cpu_pct` the window can be missed** (finding 4):
  - **How:** throttled tasks can't run their exit path until the next period, so a fork bomb's
    sandbox drains slowly. Measured from the stop to the end of the helper's report:
    - `cpu_pct` 10: 0.44–0.47 s;
    - `cpu_pct` 5: 1.2–2.0 s;
    - `cpu_pct` 1–2: 1.6–2.0 s, and twice the 5 s SIGKILL fallback.
  - **Result:** the honest "didn't report its counters" note, never a wrong number.
  - **Why the wait stays at 1 s:** the contract signed off 1 s, and the default `cpu_pct` is 75.
    §4 has this as a row.

### 2.3 The report becomes numbers (`native.py`)

A new pure function. It runs on every platform, so it gets table tests:

```python
def _usage(lines, *, cgroup: str | None, release: str) -> dict[str, Any]:
    """M3 A1/A4: RunResult's usage fields from the helper's report."""
```

It returns `cpu_ms`, `mem_peak_mb`, `pids_peak`, `limit_hits` and `not_measured`, and `_collect`
passes them into `RunResult(**…)`. `cgroup` is `run.cgroup`, and `release` is
`os.uname().release`.

**The cases, checked in this order:**

| Situation | Numbers | `not_measured` |
|---|---|---|
| init's first line is an `error` (nothing executed, U7 in §3.2) | none | none |
| no `ready` and no `error` line (the sandbox sent no report) | none | `cpu, mem_peak and pids_peak not measured: the sandbox didn't report its counters` |
| init isn't in its cgroup: no cgroup, or init reported another one (`_placed`, below) | none | `cpu, mem_peak and pids_peak not measured: this run had no cgroup` |
| placed, but no `usage` line (a timeout past the wait, or a helper that died) | none | `cpu, mem_peak and pids_peak not measured: the sandbox didn't report its counters` |
| placed, and a full line | all three | none |
| `mem_peak` missing, and the kernel parses below 5.19 | the rest | `mem_peak not measured: Linux 5.19+ needed` |
| `pids_peak` missing, and the kernel parses below 6.1 | the rest | `pids_peak not measured: Linux 6.1+ needed` |
| a field missing otherwise (a controller not enabled, a read error, a release that doesn't parse) | the rest | `<field> not measured: not readable` |

- **One placement rule (finding 9).** The verdict's test, "init reported exactly the cgroup the
  server made" (`native.py:485`), moves into a small `_placed(ready, cgroup)`. Both `_verdict`
  and `_usage` call it, so the numbers are shown exactly when the verdict counts the limits.
- **The first line is the same line `_verdict` reads.** It's the first line with `ready` or
  `error`. The `usage` line is taken with `next()`, as `_oom_note` and `_denied` take theirs
  (finding 13). Only the helper sends one, after everything it relayed from init. The guest
  can't reach init's channel: `_close_from(3)`, and init isn't dumpable.
- **Units:**
  - `cpu_usec` becomes ms;
  - `mem_peak` becomes MiB (U1);
  - `pids_peak` stays as it is.

  Each is rounded up (§3.2), so a counter that moved never shows 0.
- **Validation:** a value counts only if it's an `int` (not a `bool`) and ≥ 0, as `_oom_note`
  checks its counters. Anything else counts as missing.
- **`limit_hits`** holds only nonzero entries:
  - `tasks_refused` (U4);
  - `cpu_throttled_periods` together with `cpu_periods`, its denominator. These come as a pair,
    and only when both values are valid and `nr_throttled` > 0 (U3).

  Memory is never in it (contract A4).
- **Several missing fields** are joined with `"; "`.
- **The kernel check.** It reuses the parse in `_nproc_backstop`:
  - a new `_kernel(release) -> tuple[int, int] | None` takes that parse;
  - `_nproc_backstop` becomes `(k := _kernel(release)) is not None and k >= (5, 14)`, and its
    tests don't change;
  - a version is named only when the release parses and is below it. A release that doesn't
    parse gives `not readable`.

`_denied`'s comment for the missing line says "(timed out, overflowed, or the helper died before
sending it)". It becomes "(the helper died, or a timed-out run's helper didn't report within the
wait)". It was already wrong about the overflow, and this step makes it wrong about the timeout.

About 60 lines in `native.py`.

### 2.4 `RunResult` (`base.py`)

```python
pids_peak: int | None = None
# nonzero only: "tasks_refused"; "cpu_throttled_periods" with "cpu_periods"
limit_hits: dict[str, int] = field(default_factory=dict)
# what this result's usage line lacks, and why; None when nothing is missing
not_measured: str | None = None
```

### 2.5 Container and naive

- **`container.py`:** the result of a run that started is the one `RunResult` after
  collection, exit 125 included. It gets `not_measured="cpu, mem_peak and pids_peak not
  measured: not readable from outside a container"`.
  - The runtime-missing result says "nothing was executed" and gets no note.
- **`naive.py`:** the result of a run that started gets `not_measured="cpu, mem_peak and
  pids_peak not measured: the naive backend has no sandbox to measure"`.
  - Its two "interpreter not found" results get no note.
- **Native's `_not_run` results** get no note either. Those are: not Linux, no rootfs, or the
  helper didn't start. Their detail already says "nothing was executed" (§3.2).

### 2.6 The server (`server.py`)

- **`wall`:** `_handle_run_code` takes `time.monotonic()` just before `warden.run` and computes
  `wall_ms` after it, rounded up.
  - Selection isn't counted.
  - The slot wait, a container's preflight and native's placement are counted, because the
    agent waits for them too.
- **`_format_result(r, wall_ms)`** replaces today's usage block. That block is never reached:
  no backend fills `cpu_ms` or `mem_peak_mb`.
- **`isolation:` stays the last line,** which tests and the acceptance suites rely on.

```
usage: wall=<n>ms[, cpu=<n>ms][, mem_peak=<n>MiB][, pids_peak=<n>][ (the whole sandbox, including its setup and file cache)][; <not_measured>]
limits hit: [pids (<n> new processes or threads refused)][, cpu (throttled in <n> of <m> periods of 100 ms)]
```

- **The parenthetical** follows the kernel numbers whenever there are any (U5). What it says,
  measured:
  - init's own setup is 12–32 ms of a 24–47 ms `cpu=` for `print(1)` (finding 2);
  - the same `print(1)` reads 14 MiB after the page cache is cleared and 6 MiB right after
    another run (finding 12).
- **`limits hit:`** appears only when `limit_hits` isn't empty.

### 2.7 What results look like (the numbers are illustrative)

A native run of `print(1)`:
```
exit_code: 0
stdout:
1
usage: wall=84ms, cpu=31ms, mem_peak=11MiB, pids_peak=2 (the whole sandbox, including its setup and file cache)
isolation: native
```

A native fork bomb that times out:
```
[timed out and was terminated]
exit_code: -1
usage: wall=3012ms, cpu=2250ms, mem_peak=70MiB, pids_peak=128 (the whole sandbox, including its setup and file cache)
limits hit: pids (4113 new processes or threads refused), cpu (throttled in 18 of 20 periods of 100 ms)
isolation: native
```

A container run:
```
exit_code: 0
stdout:
1
usage: wall=412ms; cpu, mem_peak and pids_peak not measured: not readable from outside a container
isolation: container
```

Native on a 5.15 kernel:
```
usage: wall=90ms, cpu=33ms (the whole sandbox, including its setup and file cache); mem_peak not measured: Linux 5.19+ needed; pids_peak not measured: Linux 6.1+ needed
```

---

## 3. Choices this plan makes

### 3.1 Departures from the contract's wording — these need Victor's OK

| # | The contract says | This plan does | Why |
|---|---|---|---|
| U1 | `mem_peak=54MB` | `mem_peak=54MiB` | The number is `memory.peak >> 20`. The out-of-memory note beside it says `at its 256 MiB limit`, and a memory bomb reads exactly `256MiB` (16 of 16 measured). |
| U2 | `cpu and memory not measured: …` | `cpu, mem_peak and pids_peak not measured: …` | The note sits right after those fields, the per-field form (`mem_peak not measured: Linux 5.19+ needed`) uses the same names, and "cpu and memory" leaves `pids_peak` unexplained. |
| U3 | `cpu throttled 210ms` | `cpu (throttled in 18 of 20 periods of 100 ms)` | `throttled_usec` is summed across CPUs: 8719 ms in a 2009 ms run, 4760 ms for four busy loops in 1505 ms. Next to `wall=` it reads as false. Periods can't exceed the run. |
| U4 | `37 forks refused (pids)` | `pids (37 new processes or threads refused)` | `pids.events` `max` counts threads too: 64 threads under `pids.max` 32 read `max 33` for a program that never forked. |
| U5 | `(includes file cache)` after `mem_peak` | one parenthetical after all three: `(the whole sandbox, including its setup and file cache)` | All three numbers include init. Its setup is about half of `cpu=` for a short run, and its task is in `pids_peak`. The contract's rule: "the model never reads the README". |
| U6 | step 1 adds the six `RunResult` fields | three now; `ran`, `run_id` and `image` in step 2 | Only the audit reads them; in step 1 they'd be fields no code uses. |
| U7 | A1: every result gets a `usage:` line | not the two results with no `RunResult` (the outer deadline, an unexpected error) | They already say "the sandbox did not report back", and `wall` there is just the deadline. Step 2's `end` record keeps `wall_ms` for them anyway. Their exact text stays (`tests/test_select.py:475`). |

### 3.2 Fixed choices

- **Every number is rounded up** to its unit. 300 µs of cpu reads `cpu=1ms`, never `cpu=0ms`,
  which would look measured-and-idle.
- **Timed-out runs now name their blocked syscalls too.** The helper's `denied` line arrives
  within the wait, as the counters do. Today a timed-out run drops it.
- **No out-of-memory note on a timeout or an overflow, as today.** `_outcome`'s early returns
  stay. The S4-D8 amendment (§6) gives the true reason: the note isn't computed on those paths.
  It isn't that the helper's line is missing.
- **No numbers and no "not measured" note when nothing ran.** That covers native's `_not_run`
  and any result whose first status line is an `error`: a setup failure in init, or a helper
  failure. Their detail already says "nothing was executed" (finding 9).
- **A kernel release that doesn't parse names no version.** The note says `not readable`, since
  Vestibule doesn't know the version. `_nproc_backstop` still treats such a release as "no
  backstop", the safe direction there. Real release strings always parse.

---

## 4. Edge cases

| Case | What happens |
|---|---|
| A timed-out run whose helper is slow to exit | At most 1 s more, then "didn't report its counters". The finisher handles the helper as today. §5.3 tests the bound. |
| A timed-out fork bomb at a low `cpu_pct` (≤ 5) | Usually misses the 1 s window (§2.2): the honest note, never a wrong number. At 1–2 % the finisher's existing 5 s SIGKILL fallback can fire. That's today's code, noted in BUILD_LOG at done, not changed here. |
| A run whose output overflows | The counters arrive, through the existing 5 s status wait. |
| init or the helper fails during setup | `isolation: none`, "nothing was executed"; no numbers and no note (§3.2). |
| `cpu` controller absent | No `nr_throttled`, and with no cpu limit nothing is throttled. The verdict already names `cpu`. `cpu=` still shows, because `usage_usec` is a core counter. |
| `pids` controller absent | `pids_peak` is named as not readable, and the verdict names `pids`. No refused-task count. With no cgroup at all, on 5.14 and later the RLIMIT_NPROC backstop still refuses forks (S4-D9), uncounted. That run shows no numbers at all, only the no-cgroup note. |
| `memory` controller absent | `mem_peak` is named as not readable; the usage line still comes (§2.1's separate `try`). |
| A memory bomb | `memory.peak` stops at `memory.max`: 256 MiB (16 of 16 measured). |
| The same code, run twice | `mem_peak` can differ 2–5× with the page cache (python 14 vs 6 MiB, node 55 vs 11 MiB). The parenthetical says file cache is included; the ARCHITECTURE line and step 5's README say why. |
| A program that starts threads under the pids limit | Counted as refused "new processes or threads" (U4). |
| A guest that prints `{"usage": …}` on stdout or stderr | Never parsed. Only the status pipe is read, and the guest can't reach it. |
| Several runs at once | Each has its own scope, `guest/` and status pipe. |
| Windows, macOS | Native never runs there. The pure functions' tests run everywhere. |

---

## 5. Tests

Each new or extended test must be shown to fail on a mutant (§5.5).

### 5.1 Pure, on every platform (`tests/test_native_verdict.py`)

**`_usage`**, as a table. Each case has a `ready` line with a matching cgroup unless it says
otherwise:
- **A full line:** the converted values, `limit_hits == {}`, no note. The values show the
  rounding:
  - `cpu_usec` 1 → 1 ms;
  - `mem_peak` 1 byte → 1 MiB;
  - `mem_peak` exactly 256 MiB → 256.
- **No usable report:**
  - the first line is an `error` → no numbers, no note;
  - no `ready` or `error` line → the didn't-report note;
  - `cgroup` None, or `ready` naming another cgroup → the no-cgroup note, even with a `usage`
    line present;
  - placed, no `usage` line → the didn't-report note.
- **A missing field:**
  - `mem_peak` missing on `5.15.0` → `Linux 5.19+ needed`;
  - `mem_peak` missing on `6.6.87.2-microsoft-standard-WSL2` → `not readable`;
  - `mem_peak` missing on a release that doesn't parse → `not readable`;
  - `pids_peak` missing on `6.0.9` → `Linux 6.1+ needed`;
  - both missing on 5.15 → both clauses, joined.
- **A bad value:** `True`, `-1`, `"5"` or `1.5` → treated as missing.
- **Limit hits:**
  - nonzero `tasks_refused` → in `limit_hits`;
  - `nr_throttled` 9 with `nr_periods` 20 → the pair;
  - zero counts → nothing;
  - `nr_throttled` with no `nr_periods` → no cpu entry.

**`_kernel`:** a WSL release, `5.19.0`, `6.0.9`, and one that doesn't parse (→ None).

**`_placed`:** the existing `_verdict` tests are unchanged and must still pass. They cover the
same cgroup condition.

### 5.2 The helper's reporting (`tests/test_native.py`, Linux, no sandbox needed)

`_report_counters` gets a temp directory and a pipe:
- **Every file present** → the out-of-memory line, then a full `usage` line.
- **`memory.events` and `memory.peak` missing** → no out-of-memory line, and a `usage` line
  without `mem_peak`. This is finding 5's case.
- **`cpu.stat` garbled** → the `usage` line without the cpu fields, with the others present.

### 5.3 Native, live (`tests/test_native.py`, `native` and `cgroups` markers)

The `pids_peak` and `mem_peak` assertions apply only when `_kernel(platform.release())` is at
or above 6.1 and 5.19. Below those, they assert the matching note instead (finding 7).
- **New: `test_a_run_reports_its_usage_from_outside`.** `print(1)` → `cpu_ms` > 0,
  `mem_peak_mb` > 0, `pids_peak` ≥ 2 (init and the guest), no `tasks_refused`, no note.
  - It doesn't assert `limit_hits == {}`: a cold run can be throttled.
- **New: `test_a_timed_out_run_still_reports_its_counters` (A1.1).** A busy loop with a 1 s
  timeout → `timed_out`, `cpu_ms` > 0, `limit_hits["cpu_throttled_periods"]` > 0 (`cpu_pct`
  is 75), no note.
- **New: `test_the_counters_wait_is_bounded`.** This is finding 3 and contract criterion 4.
  - A stand-in skips `_stop`'s first call, so the helper keeps a busy guest running past the
    timeout. The finisher's later call stops it for real.
  - With a 1 s timeout, `run()` returns between 1 + 2 + 1 − 0.2 and 1 + 2 + 1 + 1 s, and gives
    the didn't-report note.
  - Then the test drains, and nothing is left.
- **`test_a_fork_bomb_stops_at_pids_max`** also asserts `pids_peak == pids_max` and
  `limit_hits["tasks_refused"]` ≥ 1. That's the `pids.events` check in the real layout (§2.1).
- **`test_a_memory_bomb_is_killed_at_the_limit`** also asserts `mem_peak_mb == mem_mb`. Its
  exact `stderr` assertions don't change, because nothing new goes into stderr.
- **`test_the_cpu_limit_holds`** also asserts `cpu_throttled_periods` > 0 and no more than
  `cpu_periods`.
- **`test_without_a_bus_the_run_degrades_and_the_backstop_holds`** also asserts no numbers and
  the no-cgroup note.
- **Through the real server on Linux** (`tests/test_native_select.py`,
  `test_auto_selects_native_and_a_run_says_so`): the text also has a line starting
  `usage: wall=` with `cpu=` and the parenthetical. It requires `mem_peak=`/`pids_peak=` only
  on kernels that have them.

### 5.4 Server rendering and the other backends

- **`_format_result`** (`tests/test_select.py`, beside the existing rendering tests). With
  `wall_ms=412`, the exact §2.6 lines, `isolation:` last, for each of these:
  - a full result;
  - a note;
  - limit hits;
  - no kernel numbers (the container shape: no parenthetical).
- **`wall` through `_handle_run_code`.** `server.time` is swapped for a fake clock, so the test
  doesn't depend on Windows' 15.6 ms monotonic clock (finding 6). It asserts an exact
  `usage: wall=…ms`.
- **Container** (Docker-free, `tests/test_lifecycle.py`):
  `test_degraded_backend_reports_detail` also asserts the container note.
- **Naive** (`tests/test_smoke.py`): `test_python_hello` also asserts the naive note.

### 5.5 Mutants (each must turn a test red)

| Mutant | Expected red |
|---|---|
| the A1.1 wait removed | `test_a_timed_out_run_still_reports_its_counters` |
| `_COUNTERS_WAIT_S` replaced by `_STOP_WAIT_S` (5 s), or an unbounded `await` | `test_the_counters_wait_is_bounded` |
| the usage send moved inside the out-of-memory line's `try` | §5.2's missing-`memory.events` case |
| the helper's `usage` send removed | `test_a_run_reports_its_usage_from_outside`, the stdio check |
| `_usage` ignoring `_placed` (only `cgroup is not None`) | the "`ready` naming another cgroup" case |
| `_usage` showing numbers after an `error` line | the error-first case |
| rounding down instead of up | the rounding cases |
| the kernel check inverted, or an unparseable release naming a version | the 5.15, 6.0.9, WSL and unparseable cases |
| `throttled_usec` rendered instead of the periods | the `limit_hits` and rendering cases |
| `pids.events` read from `guest/sandbox` instead of `guest/` | the fork-bomb assertion |
| `wall` dropped from the line | the rendering tests |

### 5.6 The gate

1. Windows: the pure, rendering and backend tests run; the native ones skip.
2. Linux, Python 3.14 with Docker.
3. Linux, Python 3.11 without Docker.

ruff and mypy must be clean on both platforms.

---

## 6. Docs (this step)

- **`docs/plans/M2/M2-step4-cgroups.md`, S4-D8:** an amendment blockquote saying:
  - on a timeout, the server now waits up to 1 s for the helper's lines (M3 A1.1), so a
    timed-out run gets its counters and its blocked syscalls;
  - the out-of-memory note still isn't added on a timeout or an overflow, because `_outcome`
    returns before it;
  - "those return before the helper's line exists" was never true for the overflow, and is no
    longer true for the timeout.
- **`docs/ARCHITECTURE.md`:**
  - **Line 126,** step 18 of the lifecycle diagram: it adds the usage counters.
  - **Line 219:** "Peak usage … waits for M3" becomes what the helper reads, when, and from
    where. It also says that the numbers include init and the page cache, so the same code
    can read differently twice.
- **`README.md`, the native prerequisites:** one bullet. `mem_peak` needs Linux 5.19 and
  `pids_peak` needs 6.1, and on an older kernel the result names what wasn't measured (contract
  A4).
- **`docs/GETTING_STARTED.md`, Troubleshooting:** one bullet on `… not measured: …` and what
  each reason means, including a low `VESTIBULE_CPU_PCT` (§2.2).
- **`docs/HISTORY.md`:** a bullet at sign-off and one at done.
- **`docs/BUILD_LOG.md`:** an entry at done, with the low-cpu SIGKILL fallback as a noted item.

---

## 7. Files and size

| File | Change | Lines (about) |
|---|---|---|
| `src/vestibule/backends/native_helper.py` | `_report_counters`, `_read_usage`, the call | 30 |
| `src/vestibule/backends/native.py` | the wait, `_usage`, `_placed`, `_kernel`, wiring, the `_denied` comment | 60 |
| `src/vestibule/backends/base.py` | three fields | 5 |
| `src/vestibule/server.py` | `wall`, `_format_result` | 30 |
| `src/vestibule/backends/container.py`, `naive.py` | one note each | 6 |
| tests (6 files) | §5 | 230 |
| docs (6 files) | §6 | 35 |

That's about 130 lines of source, as the contract estimated.

---

## 8. Review (`skeptic`, then `verifier`, 2026-11-01)

`skeptic`'s verdict was "sound with amendments", with 13 findings. `verifier` ran its own repros
in WSL (kernel 6.6.87.2, 12 CPUs). It confirmed 12 and rated 1 plausible, refuting none. Every
finding is taken.

| # | Sev. (verifier) | Finding | In v2 |
|---|---|---|---|
| 1 | med | `throttled_usec` is summed across CPUs: 8719 ms throttled in a 2009 ms fork bomb, 4760 ms in 1505 ms for four loops (`unthrottle_cfs_rq` adds per CPU) | U3: periods (`nr_throttled` of `nr_periods`), which can't exceed the run |
| 2 | low | init's setup is 12–32 ms of a 24–47 ms `cpu=` for `print(1)`, not "a few ms" | U5: one parenthetical names setup and file cache for all three numbers |
| 3 | low-med | No test catches a wrong bound on the 1 s wait | `test_the_counters_wait_is_bounded`, with its mutant; criterion 4's note moves here |
| 4 | low | At `cpu_pct` ≤ 5 a fork bomb drains in 1.2–2.0 s, past the window (at 1–2 %, twice the 5 s SIGKILL fallback) | Stated in §2.2 and §4: the note is honest. The 1 s stays (signed off). The fallback is noted in BUILD_LOG |
| 5 | low (plausible) | A usage send inside the out-of-memory `try` would vanish without a memory controller, and no test covers it | `_report_counters` with a separate `try` per line; §5.2's helper tests; a mutant |
| 6 | low-med | `wall ≥ 50` is flaky on Windows 3.11 (GetTickCount64): 6 of 300 read 47 | A fake clock and an exact assertion |
| 7 | low | The tests assume kernel ≥ 6.1 while native supports 5.9; `limit_hits == {}` for `print(1)` could break on throttling | Kernel-gated assertions; only `tasks_refused` is asserted absent |
| 8 | low | Two departures (`ran`/`run_id`/`image`, and no usage line on F3's results) weren't flagged for sign-off | U6 and U7 in §3.1 |
| 9 | low | The numbers' placement test differed from the verdict's; a "nothing was executed" result would show numbers or the note, against U7 | One `_placed` for both; error-first results show nothing |
| 10 | low | The RLIMIT_NPROC backstop refuses forks uncounted; `pids.events` counts threads too (`max 33` for 64 threads) | §4 states the backstop gap; U4's label |
| 11 | low | `ARCHITECTURE.md:126` also goes stale; §2.1's reason holds only for `pids.events` | Both docs lines in §6; §2.1 rewritten with the verifier's repro |
| 12 | low | `mem_peak` differs 2–5× with the page cache (python 14 vs 6 MiB, node 55 vs 11 MiB) | U5's parenthetical; the ARCHITECTURE line; step 5's README |
| 13 | nit | "The last line wins" disagreed with `_oom_note` and `_denied` | `next()`, as they do |

**Confirmed along the way:**
- `memory.peak` equalled `memory.max` exactly in 16 of 16 memory bombs.
- `pids.peak` equalled `pids.max` (32 of 32 runs, and 128 of 128 for the bash bomb).
- `guest/sandbox` has no `pids.*` or `memory.peak` files.
- init is reaped only after every process in its namespace is gone.
- The timeout path's worst case fits inside timeout + 30.
