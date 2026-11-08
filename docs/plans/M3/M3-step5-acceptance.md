# M3 step 5 — Acceptance, the live checks, and the docs

Status: **v2, signed off 2026-11-06.** v1 was reviewed by `skeptic` (13 findings, verdict
"sound with amendments"), and `verifier` confirmed 11 and found 2 plausible, refuting none
(§10). Victor asked on 2026-11-06 for two parts, 5a and 5b (S5-D1). The two questions were
taken as recommended with the sign-off. Both belong to 5b:
- **S5-D2:** the Windows stderr check is (b), Linux only;
- **S5-D3:** golden rule 1 gets its one sentence, (a).

Everything else is fixed, with one defensible answer each.

Contract: [`M3-audit-usage.md`](M3-audit-usage.md) §5 (the 10 criteria), §6 step 5, and the
places it says "the README says so" (A2, A12, §1's retention line). Carried forward here from:
- step 1 §1 "Not in this step": criteria 2–4's acceptance tests, including the guest that
  prints a fake report; the README's usage section;
- step 2 §1: criterion 5's probe of the audit dir from inside native; the README's "Audit log"
  section; the live check;
- step 3 §1: the README section, PLAN.md's naming change (`vestibule --audit` becomes
  `vestibule-mcp audit`), the live check;
- step 4 §4: the live check records what Claude Code hands the server as stderr, on Linux and
  on Windows (S5-D2).

The shape follows M2's step 7 ([`M2-step7-acceptance.md`](../M2/M2-step7-acceptance.md)).

## 1. What this step does, in plain words

Steps 1–4 built the usage numbers, the audit log, `vestibule-mcp audit` and non-blocking
stderr. Most of their tests use stand-in wardens, a recording audit, or the backend called
directly. Step 5 is the **exam**:
- **5a:** tests through the entry point an agent uses (`server.call_tool`), with real backends
  and a real audit file, wherever that proves something the existing tests don't. The rest is
  mapped to the tests that already prove it. Then the cold-start benchmark is run again, since
  M3 put work on every call's path.
- **5b:** a live Claude Code session on Linux (native) and one on Windows (container), each
  with a call cancelled by Ctrl+C, then `vestibule-mcp audit`. Then the docs pass, and the "M3
  accepted" lines last.

No `src/` change is planned. A bug the exam or the live checks find gets a failing test first,
then a fix, reviewed with its part.

## 2. The 10 criteria: what exists, what 5a adds

"Handler" means `server.call_tool` with `server.LIMITS` (a temp workspace), `server.SELECTOR`
(a fresh one), `server.CONFIG_ERROR` (None) and `server.AUDIT` (a real `AuditLog` on a temp
dir) swapped in. Real selection, the run, the result's rendering, both records and the writer
thread are then all in the path. Today's handler tests (`test_audit.py` §5.3) use stand-in
wardens and the recording audit from `conftest.py`.

**The rule for "adds" versus "mapped"** (review findings 3 and 10): 5a adds a test where real
backends or a real run are what's unproven. A behavior already tested with the real component
(a real `AuditLog`, the real console script, the real helper) is mapped, not repeated.

| # | Criterion (contract §5) | Covered today | 5a adds |
|---|---|---|---|
| 1 | The right records per call | handler with stand-in wardens, every row of the table (`test_audit.py:400–418`); each backend's `ran`, `run_id`, `image` at backend level | handler with real backends: native `ran`, `timed_out`, `cancelled`, a full slot `refused`, a removed rootfs `not_run`, then `blocked`; container `ran` with the image id, and a missing image `refused`. Mapped: the outer deadline, an unexpected error, a native setup error (§3.2) |
| 2 | Native usage is real | backend: `print(1)`'s counters, a timed-out busy loop's throttling (`test_native.py:817–834`) | handler, each bullet, rendered and recorded: `print(1)`; the memory bomb's note and `mem_peak`; the classic fork bomb's `pids_peak` and refusals; the busy loop's throttling; a file reader under a small `mem_mb` |
| 3 | Numbers come only from outside | pure: `_usage` parsing; container and naive notes at backend level | handler: a native guest that prints a fake `usage:` line and the helper's own report lines, and writes them to every fd it might reach; the same guest in a container and in naive |
| 4 | Missing counters are named | backend, real helper: the bounded wait's note (`test_the_counters_wait_is_bounded`); pure: the old-kernel wording; helper: a garbled counter file | handler: native without a bus. The other two are mapped (§3.2) |
| 5 | The audit dir | real `AuditLog`: 0700/0600 (`test_audit.py:89–94`); stdio: a relative dir blocks every call; config: an overlap is a config error, the same `CONFIG_ERROR` path; unit: the container command never names the dir | native: the dir's absolute path is "no such file" from inside |
| 6 | A failed write | real `AuditLog`: an unwritable dir, a writer stuck past 2 s then a later record, a dir deleted, a file deleted during a write (`test_audit.py:97–194`); handler: the note's place (`:553–566`) and a real log (`:599–606`) | handler with a real `AuditLog` that can't write: the run happens and the result says so. Nothing else joins a real failing log to a rendered result |
| 7 | `vestibule-mcp audit` | every bullet, with the real writer's open file and the real console script over a cp1252 pipe (`test_audit_cli.py:149–530`) | mapped only (§3.2) |
| 8 | Stdout stays clean | the stdio tests, with the audit on (`stdioutil.py:72` sets the dir) | mapped only |
| 9 | Stderr never blocks | step 4's unit test and stdio e2e test | mapped only |
| 10 | The live check | — | 5b (§3.4) |

## 3. Decisions

### S5-D1 (Victor, 2026-11-06) — Two parts, as M2

- **5a: the exam and the benchmark.** `tests/test_audit_acceptance.py`, and a run of
  `scripts/bench_coldstart.py`. Its own gate, `critic` and `verifier`, and its own commit.
- **5b: the live checks, then the docs.**
  1. The live checks (§3.4) run first, so a bug they find gets a failing test and a fix with
     their own review.
  2. The docs pass (§4).
  3. The "accepted" lines last.

  Then its gate and review, and its commit.

**Why:** 5a is code a reviewer reads line by line; 5b is mostly prose, plus sessions only
Victor can run.

### S5-D2 (Victor, 2026-11-06: (b); changes step 4's §4) — How the Windows stderr check is done

Step 4's plan says step 5 records what kind of fd Claude Code hands the server as stderr, on
Linux and on Windows. On Linux it's one line in the live session's launch command (§3.4). On
Windows, the launch can't be wrapped the same way without changing the process tree whose
shutdown the live check watches.

- **(b) Recommended (the review's finding 13). Linux only.** Windows is recorded as "not
  determined". Step 4's design doesn't depend on the answer: `os.write` and `select` work on
  both kinds.
- **(a) A separate probe session of about a minute.** Its MCP config runs a small script (in
  my scratchpad) that writes its own stderr handle's type to a file, then exits:
  `GetFileType`, and `GetNamedPipeInfo` to tell a pipe from a socket. Claude Code reports that
  server as failed, and Victor closes the session.

### S5-D3 (Victor, 2026-11-06: (a); golden rule wording) — Golden rule 1 and the `audit` command

CLAUDE.md's golden rule 1 says "NEVER write to stdout except through the MCP SDK", and "What
NOT to do" restates it. `vestibule-mcp audit` prints its table to stdout (`audit_cli.py:89–112`),
which contract A10 allows: it's a terminal command and never the MCP server. Only the contract
says so (review finding 6).

- **(a) Recommended.** Add one sentence to rule 1: "The terminal commands are not the server:
  `vestibule-mcp audit` prints its table to stdout (contract A10), and `setup` writes only to
  stderr." "What NOT to do" then says "the server's stdout". This changes a golden rule's
  wording, not what it requires, so it needs your OK, as M2 7b's rule-2 rewording did.
- **(b) Leave CLAUDE.md as it is.** The carve-out stays in the contract only.

### 3.1 (fixed) — The suite's shape

- **`tests/test_audit_acceptance.py`,** new, `test_cN_…`. Its docstring maps each criterion to
  its tests, the existing ones relied on included, as the M2 suite does.
- **Markers per test, not per file,** since the suite spans backends (finding 11):
  - native tests carry `@pytest.mark.native` with `native_required` and `cgroups_required`;
  - container tests carry `@pytest.mark.docker` with `docker_required`;
  - the rest are unmarked, so they also run on Windows.

  The 3.11 pass deselects `docker` by marker, so the marker is what keeps it Docker-free. The
  Linux gate runs everything with 0 skips.
- **Its own harness,** not the M2 suite's `Tool`: this one swaps in a real `AuditLog` and
  takes any backend. The M2 file is untouched.
  - `Tool.run()` checks every result against the allowed isolation values, and the native
    tests then assert exactly `native`, except criterion 4's, which expects `native-degraded`.
  - `Tool.records()` reads every `*.jsonl` file in the temp audit dir. A result returns after
    both records are confirmed, so they're in the file by then. The cancelled call's `end` is
    queued without a wait (A7), so that test polls for it, up to 5 s.
  - **Teardown** drains every warden used. For native, it also runs M2's own-scope check: the
    scopes named in this test's "starting a native sandbox" log lines must be gone within 2 s.
  - **A test that patches `bus_env`** (criterion 4) does it inside `monkeypatch.context()`, so
    the patch is gone before the teardown's scope check, which needs the bus (finding 2).
- **Kernel gating, as step 1's tests have it** (finding 9). Native's floor is Linux 5.9, but
  `memory.peak` needs 5.19 and `pids.peak` 6.1. The suite computes the same two flags from
  `native._kernel(platform.release())`. On an older kernel a test asserts the "Linux X+ needed"
  note in place of the number.
- **Destructive guests are bounded** as in M2. The memory bomb is bounded by `memory.max`. The
  fork bomb runs controlled first: a bounded fork loop must be refused at `pids.max` with
  exactly `native`. Only then does the classic bomb run, with a 3 s timeout (finding 9; this
  file sorts before the M2 suite, so it is the gate's first classic bomb).

### 3.2 (fixed) — What stays mapped, and why

- **Criterion 1's outer deadline and unexpected error.** No real backend reaches them without
  injecting a fault, which is what the stand-in wardens in `test_audit.py:400–418` already do
  through `call_tool`. The audit's handling of them doesn't depend on the backend.
- **Criterion 1's native setup error** (finding 7). It is a different path from the removed
  rootfs: `ran=_ran(lines)` after the helper reports an error, not `_not_run`. The real helper
  is covered at backend level (`test_native.py:500–515`, "setup failed at mounts", `ran is
  False`), and its outcome through the handler by the table at `test_audit.py:407`.
- **Criterion 4's helper that doesn't report after a timeout** (finding 8). This is already
  proven with the real helper at backend level: `test_the_counters_wait_is_bounded` patches
  `native._stop`, so the timeout's stop never happens. Through the handler, the only additions
  would be the rendering (`test_select.py::test_the_usage_lines`) and the record's copy of
  `not_measured` (`test_audit.py::test_the_end_record_fields`), both tested.
- **Criterion 4's old kernel.** A real run reads a real cgroup, so this needs a kernel older
  than the host's. The wording is covered by `test_a_missing_counter_is_named_with_its_reason`
  and by the helper-level counter-file tests.
- **Criterion 5,** except native's probe. The modes are tested on a real `AuditLog`. A relative
  dir blocks every call through a real stdio server
  (`test_config.py::test_stdio_relative_audit_dir_blocks_every_call`). An overlap is a config
  error, and every config error blocks through the same `CONFIG_ERROR` check. The container
  command is covered by `test_lifecycle.py::test_the_container_command_never_names_the_audit_dir`.
- **Criterion 6,** except the one composition test. Each failure is tested on a real
  `AuditLog`, and the handler's handling of the note in `test_audit.py:553–566`.
- **Criterion 7** (finding 3). Step 3's review dropped a stdio e2e test as low-value
  (M3-step3-audit-command.md review #12). Every bullet is tested in `test_audit_cli.py`, with
  the real writer's file still open (`:505`) and the real console script over a forced cp1252
  pipe (`:530`). The v1 test's mechanics were also fragile:
  - on Windows, the launcher's pid isn't the server's;
  - B's run could end before B was killed;
  - the cp1252 pipe holds only on some hosts.
- **Criterion 8,** the stdio tests with the audit on; **criterion 9,** step 4's two tests.

### 3.3 (fixed) — The benchmark, again

M3 added work to every call: two audit records and their confirmation, and the counters read.
`scripts/bench_coldstart.py` already points its audit at a temp dir, so the audit is on. 5a
runs it once on WSL, and BUILD_LOG records the numbers. If either median moved more than 15 %
from the quoted ones (native 82 ms, container 392 ms), 5b's docs pass puts the new numbers in
the README's table and in PLAN.md's two places (lines 78 and 130), with the commit they were
measured on.

### 3.4 (fixed; criterion 10, 5b) — The live checks

**The prompt, for both sessions** (finding 1):
- **One call at a time:** each call is made alone, and the next only after its result.
- **Every call sets `timeout_seconds: 60`,** except the one meant to time out. Victor presses
  Ctrl+C within about 20 s of the cancel call showing as running. In M2 7b two runs finished
  on their own before an interrupt.
- **The cancel goes last.** After a cancel, Claude Code 2.1.316 marked the server's connection
  dropped (BUILD_LOG's known issue), and whether it reconnects for a later call wasn't tested.
- **Judging the audit:** `vestibule-mcp audit` shows the newest 20 calls across every file in
  the folder, so the check reads only the session's own rows, the newest ones. Older records,
  if any, show above them.
- **A cancelled call shown as `no end`** fails the check. The `end` is queued at the cancel,
  and the server keeps running until the session ends seconds later, so the writer has had
  time. The exception is a log showing that the server exited within milliseconds of the
  cancel. That one is recorded as A7's honest record (a `start` alone) and looked into.

**Linux Claude Code, native** (`~/vestibule-live/start.sh`, as in M2: only Vestibule's tools,
started from a neutral dir). The launch command gains one record before its `exec`:
`readlink /proc/$$/fd/2 >~/vestibule-live-fd2.txt`. That is the stderr Claude Code hands the
command. The server itself then writes to `~/vestibule-live.log`, as in M2. The calls, in
order:
1. python `print(1)`;
2. a bash busy loop with `timeout_seconds` 2;
3. the python memory bomb;
4. node hello;
5. python `time.sleep(60)`, cancelled with Ctrl+C. Then Victor ends the session.

**Passes when,** judged from the raw tool results and then the shell:
- results 1–4 each have a `usage:` line with `cpu`, `mem_peak` and `pids_peak`. Result 2 adds
  `limits hit: cpu (throttled …)`, and result 3 has the out-of-memory note;
- `vestibule-mcp audit`'s newest five rows are the five calls in order: ran, timed_out, ran,
  ran, cancelled. Its first line names `~/.vestibule/audit`. The folder is 0700 and the file
  0600;
- `~/vestibule-live.log`'s last line names the shutdown trigger, and no `vestibule-warden`
  process or `vestibule-*.scope` is left.

**Recorded, not judged:** whatever `~/vestibule-live-fd2.txt` holds (finding 13), and the
Claude Code version.

**Windows Claude Code, container.** A neutral dir, `%USERPROFILE%\vestibule-live\`, with a
`start.ps1` and an `mcp.json` like the Linux pair, pointing at the repo venv's
`vestibule-mcp.exe`. The calls: python `print(1)`, node hello, a busy loop with
`timeout_seconds` 2, then the cancelled `time.sleep(60)`.

**Passes when:**
- the three results each have `usage: wall=…; cpu, mem_peak and pids_peak not measured: not
  readable from outside a container`;
- `vestibule-mcp audit`, run in PowerShell both plain and piped (`| Out-String`), shows the
  four calls as its newest rows, with the right outcomes and no error;
- no `vestibule-*` container is left.

The stderr check on Windows is S5-D2.

## 4. The docs pass (5b, after the live checks)

| File | Change |
|---|---|
| `README.md` | **Status:** the M3 row accepted, and one line in the status block. **A new section, "Usage numbers and the audit log":** what the `usage:` and `limits hit:` lines hold per backend, and why a container result has only `wall` (A1); `mem_peak` counts the whole sandbox, its setup and the page cache, so the same code can read 2–5× differently (step 1); where the records are and what one keeps (A2: the code up to 4 KiB with its SHA-256, the output's sizes only); who can read them (0600 in 0700; Windows' inherited permissions); that a failed write never stops a run (A3); that they're not tamper-proof (A12); no retention: old files are deleted by hand, and 100 MB holds about 20,000 runs (§1). The existing `audit` bullet and the config row point to it. **Line 108:** "one record per `run_code` call" becomes a `start` and an `end` per call, or one `blocked` (finding 6). **The speed table** if §3.3 says so. |
| `docs/PLAN.md` | M3: a status line like M2's (accepted, steps 1–5, the suite named, the contract superseding the list); `vestibule --audit` becomes `vestibule-mcp audit`; the acceptance's "every result" amended for containers (A1). Lines 78 and 130 if §3.3 says so. |
| `docs/ARCHITECTURE.md` | Server responsibility 6: a JSONL file, not "stderr/file". Line 92's "the one blocking step left in the server" names the two writer threads too (finding 6). Anything else the grep below finds. |
| `CLAUDE.md` | The repo-structure block gains `audit.py`, `audit_cli.py` and `stderr_log.py`. The Async bullet's "the one blocking step left in the server" gets the same fix as ARCHITECTURE's. Golden rule 1 per S5-D3. |
| `docs/GETTING_STARTED.md` | "Run the checks" names the M3 suite beside M1's and M2's (finding 6). Otherwise only what the live checks show is missing: steps 2–4 wrote its troubleshooting entries. |
| `docs/plans/M3/M3-audit-usage.md` | Status: accepted (last). §5: an acceptance record, criterion → test. §6: step 5's row says 5a and 5b. |
| `docs/HISTORY.md`, `docs/BUILD_LOG.md` | 5a and 5b entries; M3 accepted. **Known issues:** the workspace-size row ("revisit with M3 quotas") says the contract declined quotas because they need root; the `%TEMP%` row ("M3/M4 candidate") says M4, per the contract. |

**5b's check, besides `tests/test_docs.py`:** a grep of the live docs (README, CLAUDE.md
outside its changelog, `docs/` outside its dated records and plans) returns nothing for:
- `vestibule --audit`, `--audit`, `QueueHandler`, `QueueListener`, "waits for M3";
- "one record per", "one blocking step".

**Not changed:** GETTING_STARTED's overlap-warning line (200–201). Its reason ("records may be
forged") differs from the command's ("may not be its own"), but both are true (finding 6, the
verifier's note).

**Not in this step:** `SECURITY.md` is M4's.

## 5. Changes

| Part | File | Change |
|---|---|---|
| 5a | `tests/test_audit_acceptance.py` | new: §6.1's tests, §3.1 |
| 5a | `docs/BUILD_LOG.md`, `docs/HISTORY.md` | 5a's entry, with the benchmark's numbers |
| 5b | the files in §4 | the docs pass |
| 5b | `~/vestibule-live/mcp.json` (WSL), `%USERPROFILE%\vestibule-live\` (new) | the live sessions' launch, outside the repo |
| both | this plan | the as-built |

**Size:** the suite about 450 lines. That's a harness of about 70, 14 tests, and the guests.
M2's suite was 499 lines for 13, and steps 2–4's tests ran 1.35–2.2× over their estimates
(finding 12). Look again past 600.

**Build order, each step with its check:**
1. **5a:** measure the Linux gate's time first, as the baseline.
2. The harness and criterion 1 → it passes on WSL 3.14 and 3.11, and the container test on
   Windows too.
3. Criteria 2–6 → they pass; the scope check stays clean.
4. The mutants (§6.2) → each turns its named test red after a green control.
5. The benchmark (§3.3) → the numbers go into BUILD_LOG.
6. Both platform gates green, 0 skips on Linux, inside the 540 s budget. **Expected cost:**
   about +35 s on 3.14 and +30 s on 3.11, since native tests run in both passes. If the gate
   passes 450 s, the slow tests get looked at.
7. 5a's `critic` and `verifier`; the as-built; Victor commits 5a.
8. **5b:** the live checks (§3.4) → recorded.
9. The docs pass (§4) → `tests/test_docs.py` and the grep pass.
10. 5b's `critic` and `verifier`, then the "accepted" lines. Victor commits 5b, and M3 is
    accepted.

## 6. Tests

### 6.1 What each acceptance test asserts

Every result is also checked against the allowed isolation values, and every native test
against its own scopes. Counter assertions are gated on the kernel (§3.1).

**Criterion 1, real backends:**
1. `test_c1_native_records_a_run_a_timeout_and_a_cancel`
   - `print(1)`: the records are `start` + `end(ran)`. The `start` has backend and verdict
     `native` and the configured image ref. The `end`'s `backend_run` is the run id from the
     log line, and its `image_ran` is the rootfs's manifest digest.
   - A busy loop with timeout 1: `end(timed_out)`.
   - `time.sleep(60)`, cancelled through an anyio task group once the log says the sandbox
     started: `start` + `end(cancelled)`, with nothing returned and nothing left.
2. `test_c1_native_a_full_slot_is_refused` (`max_concurrent` 1): while a 7 s run holds the slot,
   a second call is `Blocked:` within the 5 s wait plus a margin. Its records are `start` +
   `end(refused)` with the message, and the first run ends `ran`.
3. `test_c1_native_a_removed_rootfs_then_blocked`: as M2's c13, the second call is
   `end(not_run)` with `isolation: none`, and the third a `blocked(refused)` whose reason names
   `setup`. Empty code is `blocked(refused)` too.
4. `test_c1_container_records_the_image_that_ran` (Docker): `end(ran)` with `image_ran` equal to
   `docker image inspect`'s id; then node with a missing image ref is `start` +
   `end(refused)`, and its reason holds the pull command.

**Criterion 2, native:**
5. `test_c2_print_1_reports_cpu_and_mem_peak`: the rendered `cpu`, `mem_peak` and `pids_peak`
   are all > 0, and equal the `end` record's.
6. `test_c2_a_memory_bomb_reports_its_peak`: the OOM note is shown, and `mem_peak` lies within
   10 % below `mem_mb`, in the result and in the record.
7. `test_c2_the_classic_fork_bomb_reports_pids_peak_and_refusals`:
   - **controlled first:** a bounded fork loop is refused at `pids.max`, with exactly `native`;
   - **then the classic bomb,** timeout 3: `pids_peak` equals `pids_max`, and `limits hit:
     pids (N new processes or threads refused)` has N > 0, as the record's `tasks_refused` does.
8. `test_c2_a_busy_loop_that_times_out_reports_throttling` (timeout 2): `limits hit: cpu
   (throttled in N of M periods of 100 ms)` with 0 < N ≤ M, as in the record.
9. `test_c2_reading_files_under_a_small_limit_is_no_memory_hit` (`mem_mb` 48).
   - **The guest** reads every file under the python tree's `/usr` (108 MiB measured, finding
     5) and prints the bytes it read. It drops each file from the page cache first
     (`posix_fadvise` `DONTNEED`), so its reads are charged to the sandbox.
   - **Why not a workspace file:** the host's `/tmp` is a tmpfs, whose pages can't be reclaimed,
     so the workspace can't hold the files.
   - **Asserted:**
     - exit 0, more than twice the limit read, and **no OOM note**: this is the assertion that
       can fail;
     - `mem_peak` at least 90 % of the limit, so the limit was in play;
     - no `limits hit:` line names memory. This one is structural: nothing renders memory
       there today. It guards against adding it.

**Criterion 3:**
10. `test_c3_a_native_guest_cant_change_its_numbers`. The guest prints, on stdout and stderr:
    - a fake `usage:` line (cpu 987654321 ms, mem_peak 999999 MiB, pids_peak 31337);
    - a fake `limits hit:` line;
    - the helper's own report lines (`{"ready": …}`, `{"usage": …}`);
    - and it writes those lines to fds 3–63.

    Asserted:
    - every write fails with `EBADF`;
    - the result's last `usage:` line, its own, has numbers inside the limits, and equal to the
      record's;
    - no fake number appears outside the guest's stdout and stderr sections.
11. `test_c3_a_container_result_has_only_wall` (Docker) and
    `test_c3_a_naive_result_has_only_wall`: the same guest. The result's own line is exactly
    `usage: wall=Nms; cpu, mem_peak and pids_peak not measured: …` with that backend's reason,
    and the record's usage numbers are all null.

**Criterion 4:**
12. `test_c4_native_without_a_bus_names_what_it_didnt_measure`: with `bus_env` returning None
    (inside `monkeypatch.context()`, §3.1), the result shows `isolation: native-degraded (limits
    not applied: cpu, memory, pids — no systemd user bus)`, and `usage: wall=Nms; cpu, mem_peak
    and pids_peak not measured: this run had no cgroup`, which the record's `not_measured`
    matches.

**Criterion 5:**
13. `test_c5_native_cant_see_the_audit_dir`: after one run, the guest's `stat`, `listdir` and
    `open` on the dir's and the file's absolute host paths each give `ENOENT`. The dir is under
    the home directory, not `tmp_path`: the jail's own `/tmp` would hide a `/tmp` path anyway.
    It's removed at teardown.

**Criterion 6, the naive backend** (the audit's path doesn't depend on the backend):
14. `test_c6_a_log_that_cant_write_still_runs`: a real `AuditLog` whose dir's parent is a
    regular file. The output is there, and `audit: not recorded (…)` sits just above
    `isolation:`.

### 6.2 Mutation checks (during the build; not committed)

Each must turn its named test red, after a green control:
- the cancel branch's `end` not queued → 1;
- native's `ran=False` on a removed rootfs dropped → 3;
- the container's `image` left off its `RunResult` → 4;
- the helper reads `memory.current` in place of `memory.peak` → 6. The verifier measured it
  surviving test 9: the file cache stays charged after init exits (finding 5);
- the helper reads `oom_kill` from `memory.events` `max`, so reclaim looks like an OOM kill →
  9;
- `_usage` ignores `tasks_refused` → 7;
- `_usage` ignores `nr_throttled` → 8;
- `_guest`'s `_close_from(3)` deleted (`native_helper.py:442`), so the guest inherits the
  status socket → 10 (finding 4);
- the container's not-measured note dropped → 11;
- the no-cgroup note dropped → 12;
- `AuditLog.confirm` returns no note after a failed write → 14.

No mutant for criterion 5's native probe: no code mounts the dir, and a mutant that did would
also have to change the verdict's mount rule. No mutant may hang: each one's test must end on
its own bound, since the suite has no per-test timeout (finding 10).

## 7. Edge cases and risks

| Case | What happens |
|---|---|
| A loaded host slows the native tests | The bounds come from the contract (timeout + 2 + 1, the 5 s slot wait) plus margins, as in M2's suite. Nothing asserts a lower bound on time. |
| The memory-bomb peak | `memory.peak` reaches `memory.max` before the kernel kills; it can't exceed it. "Within 10 % below" allows for the page rounding. |
| `posix_fadvise` under seccomp | The verifier's reader ran in a plain scope, not the sandbox. Docker's profile allows `fadvise64`. If the build shows it blocked, test 9 is rethought before the build goes on. |
| A kernel older than 6.1 | Tests 5–9 assert the "Linux X+ needed" note in place of the missing number (§3.1). |
| A stale audit dir from an earlier test | Every test gets its own temp dir, and test 13's home dir has a random name. |
| The live session's cancel drops the connection | It's the last call. `vestibule-mcp audit` is run from a shell afterwards, not through the session. |
| Live records land in the real `~/.vestibule/audit` | That is the point of criterion 10. Older rows may show above the session's; only the newest are judged (§3.4). |

## 8. Carried forward

- **M4:** `SECURITY.md`: A12's "not tamper-proof", the audit dir's Windows permissions, the
  residual stderr writers (A11 and step 4).
- **Later:** the `mcp<2` pin; the Windows shutdown row's liveness lease.

## 9. As built

### 5a — the exam and the benchmark (2026-11-06)

**The suite,** `tests/test_audit_acceptance.py`: 15 tests, §6.1's 14, with test 11 written as
two tests, one per backend. 575 lines against the ~450 estimated (565 at the gate; the review
added 10). The criterion map in its docstring is about 45 of them, and the guests about 100.
All 15 passed on the first run, on WSL 3.14 (about 28 s) and 3.11 (13 in about 23 s; the 2
Docker tests are deselected there).
On Windows the 2 Docker tests and the 2 unmarked ones pass, and the 11 native ones skip.

**Built as planned, with these details:**
- **The decorators.** `_native` and `_docker` wrap a test in the marker and its skip rule
  together (§3.1).
- **Test 2's hold** is a guest that waits for `/workspace/go`, not a 7 s sleep, so the slot is
  held exactly until the refusal is in.
- **The cancel waits** for the guest to write `/workspace/started`, so it lands while the
  guest runs, as a Ctrl+C on a call shown running does. The build first waited for the
  "starting a native sandbox" log line. The review showed a cancel can then land during
  placement (below).
- **Teardown tracks every warden a call ran on,** as M2's harness does. A `none` result drops
  the selection, so the current selection alone can miss one.
- **Records are matched by id in test 2,** since the holder's `end` lands after the refused
  call's records.
- **Teardown's scope check** reads `scopes()` directly. It runs for every test that made a
  native tool, and it asserts that at least one native run was logged, so a reworded log line
  can't turn it off.
- **`Limits.audit_dir`** names the test's temp dir too, so the settings match the audit the
  server writes.

**Mutants** (§6.2): 11 of 11 turned their named test red after a green control. The harness
requires the named test to be among the failures. Each failure was read, and each was the
property the mutant broke:

| Mutant | What failed |
|---|---|
| 1 | the cancelled call's `end` never landed |
| 2 | `not_run` became `ran` |
| 3 | `image_ran` was None |
| 4 | the peak, outside 90–100 % of the limit |
| 5 | a false OOM note on the reader |
| 6, 7 | no `limits hit:` line |
| 8 | the fake numbers rendered as the result's own |
| 9, 10 | the usage line without its note |
| 11 | no `audit:` line |

None hung. Files were restored byte for byte and hash-checked. The harness is scratch
(`mutants_s5.py`), not committed.

**The benchmark** (§3.3), on WSL, 50 timed calls per backend:

| Code | native median / p95 | container median / p95 |
|---|---|---|
| README (M2 7a, 2026-10-31) | 82 / 107 ms | 392 / 413 ms |
| this tree, first run | 117.7 / 141.1 ms | 895.4 / 1002.3 ms |
| `c5fe2a2` (pre-M3), same session | 123.7 / 153.5 ms | 887.0 / 960.2 ms |
| this tree, right after | 127.4 / 142.8 ms | 908.5 / 967.8 ms |

- **M3's own cost is within the noise:** the pre-M3 code, exported with `git archive` and run
  right before the working tree, measures the same.
- **The host got slower** since 2026-10-31. WSL has been up 3½ days, and an unrelated postgres
  container has run for 6 days. Both medians are past §3.3's 15 %, so 5b's docs pass updates
  the README's table and PLAN.md, saying the pre-M3 code measures the same today.

**Gate:** green on both platforms.
- Linux, 434 s of its 540 s budget, under §5's 450 s:
  - 3.14 full suite: 814 passed, 0 skipped, 269.7 s;
  - 3.11 Docker-free: 769, 140.8 s;
  - stdio: 5.
- Windows: 641 passed, plus 153 POSIX/Linux-only skips, the 11 native tests included.

The baseline gate time of build step 1 wasn't measured separately. The suite's own share is
measured directly instead: about 28 s on 3.14 and 23 s on 3.11, under §5's +35 s and +30 s.

**Review.** `critic` found 9 issues, 1 medium and 8 low, verdict "sound". `verifier` confirmed
5, rated 1 plausible and 2 partly confirmed, and refuted 1. It lowered the medium to low, so no
high or medium finding was left. Its own measurements found one more thing (finding 6, below).

| # | Finding | Verdict | Done |
|---|---|---|---|
| 1 | The as-built, the BUILD_LOG entry and the benchmark were missing; §3's headings still said "open" | partly confirmed, low: the as-built and the benchmark had been written meanwhile | Headings fixed; BUILD_LOG and HISTORY entries written |
| 2 | Teardown drained only the current selection | confirmed, low | Every warden a call ran on is drained |
| 3 | `if own:` let the scope check pass with no native run logged | confirmed, low | Asserted for every test with a native tool |
| 4 | The classic bomb's stderr could hit the collection cap | refuted: 6 runs read 23,872–24,000 bytes against 40,000. Bash's fork retry backs off, so a slower host prints less | None |
| 5 | `Limits.audit_dir` left at its default | partly confirmed, nit: nothing on the run path reads it, so c5 was right; only c1's setup message named the default | Set to the test's dir |
| 6 | The cancel landed before the helper existed | refuted as stated: 24 of 30 landed in `_collect` with a live scope. But 6 landed during placement, and 2 of those left an **empty scope** active until its `RuntimeMaxSec` (about 90 s) | The cancel waits for the guest to run. The empty scope is a known issue (below) |
| 7 | `records()` could parse a half-written last line | plausible, nit | Only whole lines are parsed |
| 8 | A redundant drain inside the bus patch | confirmed, nit | Removed |
| 9 | Each naive run leaves its temp dir | confirmed, nit: naive's own behavior | A known issue (below) |

**Found, not fixed: a cancel during placement can leave an empty scope** (finding 6). `place()`
may already have asked systemd for the scope when the cancel lands. `busctl` is killed, but the
backend never learns the unit's name, so nothing stops it. systemd removes the unit at its
`RuntimeMaxSec` (timeout + 30 s). It holds no process: it was empty in both runs (`populated
0`, no tasks). This is older than M3 (M2 step 4's placement). §1's rule says a bug the exam finds
gets a failing test and a fix. This fix changes `native_cgroup` in a way the plan doesn't
design, so it waits for Victor's decision, recorded in BUILD_LOG's known issues.

**After the fixes:** the file passed on 3.14 (15) and 3.11 (13), test 1 passed 10 of 10 with
its new trigger, and no scope was left. All 11 mutants were red again, and the Windows run
passed (4, plus 11 skips). The fixes touched only the new test file, and only at the findings'
lines, so the full gate wasn't run again.

## 10. Review record

`skeptic` reviewed v1: 13 findings, 6 medium and 7 low, verdict "sound with amendments".
`verifier` confirmed 11 and found 2 plausible, refuting none. It lowered three from medium to
low and kept the rest; one WSL measurement backs finding 5. All 13 are taken into v2.

| # | Severity (after `verifier`) | Finding | v2 |
|---|---|---|---|
| 1 | med | The live cancel call had the 10 s default timeout; parallel calls; a lost `end`; old rows | §3.4: 60 s timeouts, one call at a time, Ctrl+C within ~20 s, the newest rows judged, `no end` fails unless the log shows an exit right after the cancel |
| 2 | low (was med) | A `bus_env` patch is still active at the teardown's scope check | §3.1: `monkeypatch.context()` in test 12 |
| 3 | med | v1's end-to-end `audit` test repeated what step 3's review dropped, and was fragile on Windows | Criterion 7 mapped (§3.2); the rule for "adds" versus "mapped" (§2) |
| 4 | low (was med) | Test 10's mutant was a strawman; the real attack is the guest reaching the status socket | §6.2: delete `_close_from(3)` |
| 5 | med | Test 9's assertions couldn't fail, and its mutant survives (measured) | §6.1 test 9: the OOM note is the real assertion, the guest reports its bytes; §6.2: the `oom_kill`-from-`max` mutant |
| 6 | low (was med) | The docs pass missed lines M3 made false | §4: README:108, ARCHITECTURE:92, CLAUDE.md's Async bullet, GETTING_STARTED's "Run the checks", PLAN.md's numbers; golden rule 1 is S5-D3. GETTING_STARTED:200–201 kept: both reasons are true |
| 7 | low | Criterion 1's native setup error was neither tested nor mapped | Mapped (§3.2) |
| 8 | low | §3.2's reason for mapping criterion 4's wait was wrong | Reason restated; still mapped (§3.2) |
| 9 | low | The classic bomb ran without M2's controlled-first guard; no kernel gating | §3.1 and test 7: controlled first, exactly `native`; kernel flags |
| 10 | low (plausible) | Tests 13, 16, 17, 18 and most of 14 repeated real-`AuditLog` tests; one mutant would hang | Dropped, except one composition test (14); §6.2's no-hang rule |
| 11 | low | Per-test skipifs don't keep the 3.11 pass Docker-free | §3.1: the `docker` and `native` markers per test |
| 12 | low (plausible) | ~400 lines was likely low | §5: about 450 for 14 tests; look again past 600 |
| 13 | low | S5-D2's probe session for an unneeded fact; the fd record was a pass condition | S5-D2 now recommends (b); the record isn't judged (§3.4) |
