# M3 contract — audit log + resource reporting

Status: **signed off 2026-11-01 (v2).** Revised after the `skeptic` review; `verifier`
confirmed all 13 findings (§7). Victor chose option 1 on each of A1–A3. The rest of the
decisions are fixed, with one defensible answer each.

Inputs:
- `docs/PLAN.md` §Milestone 3;
- the three BUILD_LOG known issues marked as M3 candidates;
- two earlier sign-offs that deferred work to "M3's audit record": M1's S6-D2 and S7-D2;
- the code as of `c5fe2a2`;
- facts measured on 2026-11-01 (the evidence notes in §2 and §7).

---

## 1. Scope

PLAN.md's M3 has three parts:
- a structured JSONL audit trail of every execution (request, code hash, exit, resource
  high-water marks, denied syscalls);
- `memory.peak` / `cpu.stat` surfaced in every result;
- a CLI that prints what agents have been doing.

Its acceptance: every run appends an audit record; the CLI prints a readable history; results
include real usage numbers.

**In:**
- **Usage numbers in every `run_code` result,** each from a source the sandboxed code can't
  alter (A1).
- **An audit log** of every `run_code` call that reaches Vestibule's handler: the ones that
  ran, the ones refused, and the ones cancelled.
  - It records which image each run used, which closes S6-D2's deferral ("per-run provenance
    belongs in M3's audit record").
  - Calls that fail the tool's input schema leave no record: the MCP SDK rejects them before
    the handler runs, and the model sees "Input validation error".
- **`vestibule-mcp audit`,** which prints the history.
- **Non-blocking stderr logging.** This is the 2026-10-25 known issue: a client that stops
  reading stderr blocks the server at its next log call. The audit adds a second writer, and
  both follow one rule: a stuck output never blocks the event loop.

**Out, with reasons:**
- **Workspace quotas** (an M3 candidate). Enforcing a disk cap needs filesystem quotas or a
  loop-mounted image, and both need root (golden rule 2). The README documents the missing
  cap.
- **Per-run workspace change reports** (S7-D2's deferral). M1 step 7 rejected per-run link
  reporting because the attacker it targets can evade it, it's noisy with legitimate links,
  and it costs a filesystem walk per run (`M1-step7-acceptance.md:244–248`). An audit field
  would carry all three costs, and the walk would run over an unbounded workspace. Declined.
- **Leftover `%TEMP%\vestibule-run-*` dirs after a hard kill** (an M3 candidate). That's
  lifecycle, not observability. It fits M4's polish, with deadline-stamped dir names a sweep
  can judge safely, as S4-D3 does for containers.
- **Usage numbers measured inside a container.** See A1. That includes the daemon's own `oom`
  event, which the guest can't forge but which would cost a `docker events` process per run.
- **Retention.** PLAN.md doesn't ask for it, and the review showed that a safe pruner
  needs liveness checks the plan doesn't have. 100 MB holds about 20,000 runs at A2's sizes.
  The README says where the files are and that old ones can be deleted by hand.
- **Making the audit tamper-proof.** See A12.
- **An audit of `read_workspace`.** It runs no code.
- **A remote or shared audit sink.** Vestibule sends nothing off the machine.

---

## 2. Decisions

### A1 (PROVISIONAL) — Usage numbers only from sources outside the sandbox

Every result gets a `usage:` line, and every number on it comes from somewhere the sandboxed
code can't write.

| Backend / state | Numbers | Source |
|---|---|---|
| every backend | `wall` (ms) | the server's monotonic clock around `warden.run` |
| `native`, and `native-degraded` with a cgroup | `cpu` (ms), `mem_peak` (MB, "includes file cache"), `pids_peak`; and when nonzero, pids and cpu limit hits (A4) | the run's `guest/` cgroup, read by the helper after init has exited and before the scope goes away. That's where it already reads `memory.events` for the out-of-memory note (`native_helper.py:139–146`). **Timed-out runs included:** see A1.1 |
| `native-degraded` without a cgroup | `wall`, plus a note on what wasn't measured | there's no cgroup to read |
| `container`, `container-degraded` | `wall`, plus a note | see below |
| `none` (naive) | `wall` | |

**A1.1 — the counters on a timeout.** Today `_collect` stops a timed-out run and parses its
status straight away (`native.py:251–264`). The helper's post-run line arrives after
that, so S4-D8 says it is used "not on a timeout". M3 amends S4-D8:
- **The wait.** After `_stop`, `_collect` waits for the status pipe to close, which happens
  when the helper exits. The wait is bounded at 1 s (a new `_COUNTERS_WAIT_S`).
- **Typical case.** init is SIGKILLed and the helper exits within milliseconds, so the fork
  bomb and the busy loop, which are the runs the counters exist for, get them.
- **Worst case.** The run costs timeout + 2 + 1, inside the outer deadline (timeout + 30) and
  inside c08's bound (timeout + 2 + 3).
- **No line in time.** The result says `cpu and memory not measured: the sandbox didn't report
  its counters`.
- **S4-D8's overflow wording is also wrong.** The helper's line does exist on an overflow: only
  `_outcome`'s early return drops the note (`native.py:399–400`). The amendment corrects it.

**Why the container gets no kernel numbers in M3.** The container can read its own cgroup
files: `memory.peak`, `cpu.stat` and `pids.peak` are all present under Docker Desktop
(measured). But those numbers can only reach the server through the container's own
stdout/stderr, which the guest shares. Even after a wrapper kills every other process
(`kill -KILL -1`), the guest can print a fake report and then SIGKILL the wrapper, since they
share a uid. The server then sees a well-formed report and exit 137, which is also what a real
out-of-memory kill looks like. A per-run secret the guest can't read would need `docker run -i`
to carry it in on stdin, and M1's profile forbids `-i` (contract §3).

**Options:**
- **(1) Recommended.** Kernel numbers only where Vestibule reads them from outside (native).
  Container results report `wall` and say why the rest is missing: `cpu and memory not
  measured: not readable from outside a container`.
  - This **amends PLAN.md's M3 acceptance** ("memory.peak / cpu.stat surfaced in every
    result") for containers. Every result still gets real numbers, at least `wall`.
- **(2)** Containers also report numbers measured inside the container, labeled
  `usage (reported by the sandbox, unverified): …`.
  - The cost: a shell wrapper in front of `timeout` (an amendment to M1's §3 profile),
    stripping the report line from stderr, and numbers a deliberately hostile guest can fake,
    put into the model's context.
  - Most users run the container backend (Windows and macOS), so this is the option that
    gives them numbers.

**Evidence** (WSL2, kernel 6.6.87.2, 2026-11-01):
- `memory.peak`, `pids.peak`, `cpu.stat` (with `nr_throttled` and `throttled_usec`),
  `memory.events` and `pids.events` exist in a user-manager scope. A transient scope that
  allocated 50 MB read `memory.peak=56184832` and `pids.peak=3`.
- The same files are present inside a Docker Desktop container, read-only to every uid.
- YAMA `ptrace_scope` is 1 on the host and in the container, so the guest can't attach to
  the wrapper. Killing it is enough.

### A2 (PROVISIONAL) — What a record keeps of the code and the output

**Options:**
- **(1) Recommended.**
  - **The code** is kept capped at 4 KiB, with `code_truncated`, plus its SHA-256 and byte
    count. A hash alone can't tell you what an agent tried, and "what have my agents been
    doing" is M3's question.
  - **Output** is kept only as sizes (`stdout_bytes`, `stderr_bytes`), never its content. It's
    unbounded, and it may hold what the code read from the workspace.
- **(2) Hash, size and language only,** as PLAN.md's "code hash". It keeps the least, and
  `audit` can only show "python, 312 bytes, sha256 …".
- **(3) The full code, up to `max_code_bytes` (256 KiB).** Records then grow large, and a
  pasted secret is kept whole.

The code is already in the agent's own transcript.

**Who can read the records:**
- **Linux and macOS:** the records are 0600 in a 0700 dir.
- **Windows:** Python's mode bits don't set file permissions; the files inherit their
  folder's ACL.
  - **Python 3.11 and 3.12 before 3.12.4:** `mkdir(0o700)` doesn't restrict the folder either.
  - **The default location under `%USERPROFILE%`:** its inherited ACL already limits it to the
    user, SYSTEM and Administrators.
  - **A custom `VESTIBULE_AUDIT_DIR` such as `D:\audit`:** it may inherit "Authenticated
    Users: Modify".

  The README says so.

### A3 (PROVISIONAL) — A failed audit write doesn't stop the run, and the result says so

- **(1) Recommended.** The run goes ahead, and the result says what happened to its record:
  - **The write failed** (disk full, permissions): `audit: not recorded (<reason>)`.
  - **The writer didn't confirm within 2 s** (A7): `audit: not confirmed (the disk didn't
    answer within 2 s)`. The write may still land.

  The server logs a warning either way. The audit is observability, not isolation: refusing
  every run over a full disk would turn a logging fault into an outage, and golden rule 6 says
  to degrade loudly, not to stop.
- **(2) Fail closed.** If the start record (A6) can't be confirmed, the call is `Blocked:` and
  nothing runs. This is stricter, and right for someone who treats the log as a compliance
  record. It isn't what M3 is for.

### Fixed decisions

**A4 — The `usage:` line.**
- **Format.** `usage: wall=412ms, cpu=380ms, mem_peak=54MB (includes file cache),
  pids_peak=3`, with fields only when measured (A1). When the counters are nonzero, a second
  line follows: `limits hit: 37 forks refused (pids), cpu throttled 210ms`. These come from
  `pids.events` `max` and `cpu.stat` `throttled_usec`.
- **Memory isn't in `limits hit`.** `memory.events` `max` counts reclaim at the limit, not
  failure. A program that only read 298 MB of files under a 48 MiB limit showed `max 1480`,
  `oom 0`, and `memory.peak` at exactly the limit, then exited normally (measured). S4-D8's
  out-of-memory note, from `oom_kill`, already states the one memory fact that means
  something failed.
- **What `mem_peak` counts.** Everything charged to the sandbox's cgroup: the sandbox's own
  init, the guest's processes, `/tmp` contents (a tmpfs) and file cache. The line itself says
  "(includes file cache)", because the model never reads the README.
- **Missing kernel files are named, never invented.** `memory.peak` needs Linux 5.19 and
  `pids.peak` needs 6.1 (checked in the kernel's history), but native's floor is 5.9. On an
  older kernel the result says e.g. `mem_peak not measured: Linux 5.19+ needed`, and the
  README's prerequisites say so.
- **`RunResult` gains:**
  - `pids_peak`;
  - `limit_hits`;
  - `not_measured`: the note;
  - `ran`: False when the backend knows nothing executed (A6);
  - `run_id`: the backend's own id (A6);
  - `image`: what ran (A6).

  `cpu_ms` and `mem_peak_mb` exist already and are unused today. `wall` is measured by the
  server and isn't a `RunResult` field.

**A5 — One audit file per server process, written by one thread.**
- **Where.** `~/.vestibule/audit/` (`VESTIBULE_AUDIT_DIR`, a new setting), created with mode
  0700. Files are named `<UTC start time>-<pid>.jsonl` and created with mode 0600 (A2 on
  Windows).
- **Format.** One JSON object per line, UTF-8, `\n` endings on every platform.
- **One writer thread** (A7) holds the process's one file descriptor. No two processes write
  the same file, and within a process only that thread writes. So there's no lock and no
  interleaving.
  - Windows' C runtime emulates `O_APPEND` as a seek and then a write, and loses records when
    each write opens its own descriptor: 453 of 6000 in a two-thread repro. With one shared
    descriptor, 6000 of 6000 survived.
- **A deleted file.**
  - **POSIX:** before each write the thread checks that the file still has a link
    (`fstat().st_nlink`); if not, it reopens. Appends to an unlinked file vanish without an
    error.
  - **Windows:** a held-open file can't be deleted.
- **A torn record.** If a write fails partway (a short write on a full disk), the next record
  starts on a new line. The same applies when a file is reopened and doesn't end in `\n`. A
  repro showed a short write glueing the next record onto the damaged one.

**A6 — Three record kinds, one audit id per call, and the outcomes that actually happen.**
- **`blocked`.** Refused before a backend was chosen: arguments the schema allows but the
  handler refuses (empty code, code over `max_code_bytes`), or selection refused. It keeps
  the reason.
- **`start`.** Written after selection, just before `warden.run`. It keeps:
  - the language, the timeout and the code (A2);
  - the backend and its verdict;
  - the configured image ref;
  - the server's pid and version;
  - the MCP client's name and version, if the low-level SDK exposes them in the handler (step
    2 checks; the fields are left out if not).
- **`end`.** Written for every `start`. It keeps:
  - `outcome`;
  - `exit_code`, `timed_out` and `wall_ms`;
  - `isolation` and its detail;
  - usage, limit hits and the not-measured note;
  - `denied_syscalls`;
  - the output sizes;
  - the backend's own run id (`backend_run`), which ties the record to the container
    `vestibule-<id>`, the scope `vestibule-<id>.scope` and the `run <id>:` log lines;
  - the image that ran: the container's image id from the preflight's `image inspect`
    (already run and cached), or the native rootfs digest.

**`outcome` values:**

| Outcome | When |
|---|---|
| `ran`, `timed_out` | the backend ran the code |
| `not_run` | a `RunResult` with `ran` False: native `_not_run`, a native setup error, the container runtime missing. Container exit 125 stays `ran`, since the code's own wording says the guest may have run |
| `refused` | `RunRefusedError` from inside `warden.run`: no free slot, the image missing. These come after `start`, so they're an `end`, not a `blocked` |
| `failed` | no `RunResult`: the outer deadline, an unexpected error (the M2 follow-ups F3 paths) |
| `cancelled` | the client cancelled the call (Ctrl+C in Claude Code). The handler hands an `end` to the writer without waiting (A7), then re-raises `CancelledError` |

A `start` with no `end` then means the server process ended mid-run, and `audit` shows it
that way.

**Not recorded:**
- **An invalid config** (`CONFIG_ERROR`, checked in `call_tool`). The server can't trust any
  setting then, the audit dir included. The `Blocked:` message already names the fix.
- **Schema-invalid calls** (§1).

**A7 — Writes never block the event loop, and never share the default thread pool.**
- **One daemon writer thread** per process, fed by a bounded queue (1000 records), like A11.
  - The handler enqueues a record with its acknowledgement future and waits up to 2 s for
    it. The thread completes the future through `loop.call_soon_threadsafe`.
  - On a timeout, A3's "not confirmed" applies. On an error, A3's "not recorded" applies.
  - A full queue, which means a disk that has been stuck for a while, also gives "not recorded".
- **Why not `asyncio.to_thread`.** Stuck writes would hold workers of the default pool. That
  pool also runs native placement's `_layout`, `read_workspace` and the container finisher's
  `rmtree`, so a hung audit disk would turn runs `native-degraded` and hold slots.
- **Shutdown.** A daemon thread isn't in the default executor, so `asyncio.run`'s
  executor shutdown and interpreter exit don't wait for it. An `end` lost at shutdown leaves
  a `start` alone, which is the honest record.
- **Budget.** The `start` wait comes before the outer deadline, and the `end` wait comes after
  it. So at most 4 s is added to a call, and only when the disk is failing.

**A8 — The sandbox can never reach the audit.**
- **Validation.** `VESTIBULE_AUDIT_DIR` must be absolute or start with `~`, as for the rootfs
  in `setup` (`rootfs.py:111–114`). If it overlaps the workspace or the rootfs, that's a
  **config error**, so every call is `Blocked:` (the M1 follow-up's mechanism). This differs
  from the rootfs's own overlap check, which is a selection-time refusal (`select.py:481`).
  The audit's check is static, so it can run at startup.
- **No mounts.** No backend mounts the audit dir:
  - **native:** the per-run mount-table verdict already refuses any unexpected mount
    (`native.py:547–568`);
  - **container:** its command shape is fixed, and a unit test asserts that no `-v` names the
    audit dir;
  - **the acceptance suite** also probes the dir's absolute path from inside native (Linux
    paths only).

**A9 — No retention in M3** (§1, Out).

**A10 — `vestibule-mcp audit [N]`.**
- **What it prints.** The last N calls (default 20), oldest first, across every
  `*-<pid>.jsonl` file in the dir, merged by `ts`. Each call gets one line:
  - the time and language;
  - the outcome and `isolation`;
  - `exit_code`;
  - `wall`, `cpu` and `mem_peak`;
  - the code's first line, cut to fit.

  A `blocked` or `refused` call gets its reason instead.
- **First, the dir it read.** If that's empty, it hints that `VESTIBULE_AUDIT_DIR` must
  match the MCP config's value.
- **Sanitized output.** Every field from a record goes through one sanitizer before
  printing. It escapes C0 and C1 controls, DEL and the bidi controls (U+202A–202E,
  U+2066–2069) as `\x..`/`\u....`, and takes "first line" after splitting on every line
  break `str.splitlines` knows, so a `\r` can't overwrite the line.
  - Without it, injected code could print ANSI sequences or OSC 52 to the terminal. That
    would hide its payload, or plant a command on the clipboard.
- **Encoding.** stdout is reconfigured as UTF-8 with `errors="replace"`. Piped stdout on
  Windows is cp1252, which raised `UnicodeEncodeError` on U+202E (measured).
- **Damaged lines.** Any unparseable line, anywhere in a file, is skipped and counted.
- **Output.** It goes to stdout, since `audit` is a terminal command and never the MCP server,
  and golden rule 1 is about the stdio server. Errors go to stderr.
- **No flags in M3.** For more, read the JSONL files themselves.
- **Naming.** PLAN.md's `vestibule --audit` becomes `vestibule-mcp audit`, to match `setup`.

**A11 — Non-blocking stderr.**
- **The handler.** The server's handler becomes a `QueueHandler` (queue bounded at 1000)
  whose `enqueue` is overridden to drop and count on a full queue. The stock `emit` would
  call `handleError` on `queue.Full`, which writes a traceback to the stuck `sys.stderr`.
  `logging.raiseExceptions` stays at its default.
- **The listener.** A `QueueListener` thread writes the records to its own `os.dup(2)`, never
  to `sys.stderr`. It holds no lock that other code also takes.
  - Once a write succeeds after drops, it logs the drop count.
- **Warnings.** `logging.captureWarnings(True)` routes Python warnings into the queue.
- **The POSIX hand-off line** (`os.write(2, …)` at `server.py:344`) checks writability first,
  with a zero-timeout `select`, and is skipped on a full pipe. Without that check,
  `os._exit` would wait for the client's SIGKILL.
- **Residuals, stated:**
  - the thread and unraisable-exception hooks still write `sys.stderr` (both rare, crash
    paths);
  - the native helper writes to the inherited stderr only on a crash.

**A12 — What the audit is not.** It's a record of what agents ran, kept where the sandboxed
code can't reach it. It isn't tamper-evident. The user, and any tool running as the user
(including an agent's own shell tool outside Vestibule), can edit it. Hash chaining wouldn't
change that, because the same user can recompute the chain. The README says so.

---

## 3. Record schema (`v: 1`)

```json
{"v":1,"kind":"start","id":"<16 hex>","ts":"2026-11-01T21:07:03.512Z","pid":4242,
 "version":"0.0.1","language":"python","timeout_s":10,"backend":"native","verdict":"native",
 "image":"docker.io/library/python@sha256:…","code_sha256":"…","code_bytes":9,
 "code":"print(1)\n","code_truncated":false}
{"v":1,"kind":"end","id":"<same>","ts":"…","outcome":"ran","backend_run":"0123456789abcdef",
 "image_ran":"sha256:…","exit_code":0,"timed_out":false,"wall_ms":84,"isolation":"native",
 "isolation_detail":null,"usage":{"cpu_ms":31,"mem_peak_mb":11,"pids_peak":2},
 "limit_hits":{},"not_measured":null,"denied_syscalls":[],"stdout_bytes":2,"stderr_bytes":0}
{"v":1,"kind":"blocked","id":"…","ts":"…","pid":4242,"version":"0.0.1","language":"python",
 "reason":"code exceeds 262144 bytes."}
```

- **`language`** is always one of the enum values. The SDK has validated it.
- **`code`** is cut at 4 KiB on a UTF-8 character boundary (A2).
- **`reason`** comes from Vestibule's own messages, capped at 1 KiB.

---

## 4. Time budgets and failure modes

- **Native's counters:** read by the helper after `waitpid(init)`.
  - **Normal path:** no change.
  - **Timeout:** at most 1 s more (A1.1).
  - **A read that fails:** that field goes into `not_measured`.
- **Audit acknowledgements:** at most 2 s each, outside the outer deadline (A7).
- **Concurrent runs** (up to `max_concurrent`): records go through the one writer in enqueue
  order, and the `id` ties `start` to `end`.
- **The audit dir removed under a running server:** the writer reopens on its next record,
  recreating the dir (0700). If that fails, A3 applies.
- **A clock step** (NTP) can put records out of order. `audit` sorts by `ts`. `wall_ms` comes
  from the monotonic clock.

---

## 5. Acceptance criteria

1. **The right records per call,** through the handler. Each case gives exactly these:

   | Case | Records |
   |---|---|
   | empty code, oversize code, selection refused | `blocked` |
   | normal run | `start` + `end(ran)` |
   | timeout | `start` + `end(timed_out)` |
   | no free slot, missing image | `start` + `end(refused)` |
   | missing rootfs after selection, a native setup error | `start` + `end(not_run)` |
   | outer deadline, unexpected error | `start` + `end(failed)` |
   | cancellation (through an anyio task group, as the SDK cancels) | `start` + `end(cancelled)` |
2. **Native usage is real.**
   - A memory bomb gets S4-D8's out-of-memory note and a `mem_peak` reading.
   - The classic fork bomb, which times out, reports `pids_peak` equal to `pids_max` and a
     nonzero `forks refused`.
   - A busy loop that times out reports cpu throttling.
   - `print(1)` reports `cpu` and `mem_peak` greater than 0.
   - A file-reading program under a small `mem_mb` shows no memory limit hit.
3. **Numbers come only from outside.**
   - A guest that prints a fake `usage:` line, or the helper's report JSON, leaves the
     result's numbers unchanged.
   - Container and naive results carry `wall` and the not-measured note, never kernel
     numbers.
4. **Missing counters are named.**
   - `native-degraded` without a bus reports `wall` and says what wasn't measured.
   - A timed-out run whose helper never reports says so.
   - A kernel without `memory.peak` (simulated) names it.
5. **The audit dir.**
   - It's 0700, with files 0600 (POSIX; Windows is documented, not tested).
   - A relative `VESTIBULE_AUDIT_DIR`, or one overlapping the workspace or the rootfs, makes
     every call `Blocked:` as a config error.
   - The container command never names the dir.
   - Native's absolute-path probe gets "no such file".
6. **A failed write.** The run still happens.
   - An unwritable dir gives `audit: not recorded (…)`.
   - A writer blocked past 2 s gives `audit: not confirmed (…)`, and a later record still lands
     once the writer is unblocked.
   - A file deleted under the writer is reopened on POSIX, and no record is lost silently.
7. **`vestibule-mcp audit`** prints:
   - the dir it read;
   - two processes' files merged in time order;
   - an unparseable line in the middle of a file, skipped and counted;
   - a `start` with no `end`;
   - a record whose code holds `\x1b[8m`, `\r` and U+202E, printed escaped;
   - all of it without errors when piped on Windows.
8. **Stdout stays clean.** The stdio server's stdout carries only JSON-RPC, with the audit on
   (the stdio e2e test, extended).
9. **Stderr never blocks the server.**
   - **Unit test:** with a listener blocked and the queue full, `emit` returns at once and
     nothing reaches `sys.stderr`.
   - **stdio e2e:** a client that never reads stderr. The run count is sized from the pipe's
     measured capacity (`F_GETPIPE_SZ`) to at least twice its worth of log lines. All runs
     complete, and the test is shown to hang on `c5fe2a2` first.
10. **The live check.** A live Claude Code session on Linux (native) and on Windows (container)
    runs a few calls, including one cancelled with Ctrl+C. `vestibule-mcp audit` then shows
    them, and each result has its `usage:` line.

---

## 6. Implementation order (each step ≈ one commit; each its own plan, `skeptic`-reviewed)

| Step | What | Rough size |
|---|---|---|
| 1 | Usage numbers: the helper's counter reads, the timeout wait (A1.1, amending S4-D8), the `RunResult` fields, the `usage:`/`limits hit:` lines, `wall` and the not-measured notes for every backend (A1, A4) | src ~130, tests ~150 |
| 2 | Audit writer: `audit.py` (the writer thread, the queue, acknowledgements, reopen and newline repair), `VESTIBULE_AUDIT_DIR` with its validation, the records and outcomes wired into `_handle_run_code` including cancellation (A2, A3, A5–A8) | src ~250, tests ~300 |
| 3 | `vestibule-mcp audit` with the sanitizer (A10) | src ~110, tests ~140 |
| 4 | Non-blocking stderr (A11) | src ~60, tests ~100 |
| 5 | Acceptance suite, docs (README "Audit log" section, ARCHITECTURE, PLAN status and the amended M3 acceptance), live checks | tests ~300 |

Every step runs the full loop: plan, `skeptic`, sign-off, build, `gate`, `critic`, `verifier`,
docs.

---

## 7. Review (`skeptic`, then `verifier`, 2026-11-01)

`skeptic`'s verdict was "sound with amendments", with 13 findings. `verifier` confirmed all 13
(two parts refuted, one partly plausible, one depends on the Python version) and adjusted five
severities. Every finding is taken.

| # | Sev. (verifier) | Finding | In v2 |
|---|---|---|---|
| 1 | high | Timed-out native runs would never carry counters: `_collect` parses before the helper's line exists (S4-D8 "not on a timeout") | A1.1: a 1 s bounded wait after `_stop`, amending S4-D8 (its overflow wording corrected too) |
| 2 | high → med | `audit` would print model-supplied control sequences to the user's terminal; piped Windows stdout (cp1252) raises on non-ASCII. Refuted part: a bad `language` can't reach a record, because the SDK validates the schema first | A10's sanitizer and UTF-8 stdout; `language` is an enum value by construction; §1 states that schema-invalid calls leave no record |
| 3 | med | Refusals happen inside `warden.run` (after `start`); results where nothing ran would be `ran`; cancellation wrote no `end` | A6's outcomes `refused`, `not_run`, `cancelled`; `RunResult.ran`; criterion 1 covers each |
| 4 | med | `memory.events` `max` counts reclaim: a file reader showed `max 1480`, `oom 0`, `mem_peak` at the limit | Memory left out of `limits hit`; "(includes file cache)" on the line; criterion 2 uses the out-of-memory note |
| 5 | med | `to_thread` writes share the default pool with placement, `read_workspace` and `rmtree`; Windows CRT appends lose records with a descriptor per write; stuck workers delay exit | A7: one daemon writer thread and one descriptor (A5), with acknowledgements |
| 6 | med → low | S6-D2 (which image ran) and S7-D2 (per-run link reports) deferred work to M3, and the plan was silent | Image ref in `start`, image id in `end`; S7-D2 declined with M1 step 7's reasons (§1) |
| 7 | med | A stock `QueueHandler` writes a traceback to the stuck stderr on a full queue; the e2e test may not reach the overflow path; writers that bypass logging | A11: `enqueue` overridden, the listener on its own fd, `captureWarnings`, a writability check on the hand-off line; criterion 9's unit test plus a pipe-sized e2e test shown to hang first |
| 8 | med → low | Retention could delete an idle live server's file, and its scope and timing were unspecified | Retention dropped from M3 (§1, A9) |
| 9 | low | The absolute-path probe can't see a mount elsewhere; Windows ignores mode bits (on 3.11, and on 3.12 before 3.12.4) | A8 relies on the mount-table verdict and a command-shape test; A2 states the Windows ACL facts |
| 10 | low | `VESTIBULE_AUDIT_DIR` validation was unspecified; `audit` can't see an MCP-config-only dir; an invalid config's record would land in the default dir | A8 requires absolute or `~`, and treats an overlap as a config error; A10 prints the dir it read, with a hint; A6 doesn't record an invalid config |
| 11 | low | `memory.peak` needs 5.19 and `pids.peak` needs 6.1, below native's 5.9 floor | A4 names the missing field in the result; README prerequisites |
| 12 | low | The audit id couldn't be tied to the backend's run id | `backend_run` in `end` (A6) |
| 13 | low | A short write glues the next record onto a damaged line mid-file | A5's newline repair; A10 skips any unparseable line |
