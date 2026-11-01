# M2 step 7 — Acceptance, benchmark, docs, and the live-agent check

Status: **done 2026-10-31; M2 accepted** (§8 is the as-built). **v2, signed off 2026-10-28,**
after the `skeptic` review and the `verifier`'s check of its claims (§9). Victor decided all three questions, each as recommended:
- **S7-D1:** split the step into 7a (the exam and the benchmark) and 7b (the live checks, then
  the docs), as M1 did;
- **S7-D2:** what evidence criterion 9's foreign-arch line rests on;
- **S7-D3:** how Linux Claude Code gets into WSL for the live check (E2).

Everything else is fixed: one defensible answer each.

Contract: [`M2-native-warden.md`](M2-native-warden.md) §9 (the 17 criteria), §8 (the
`ARCHITECTURE.md` corrections), §10 step 7, and E2. Carried forward here from:
- step 2 §6: the per-distro table's arm64 row and the settings table;
- step 4 §6: the README note that sandboxes run outside the client's cgroup;
- step 5 §6: criterion 9's two remaining parts, the availability table's ptrace line, and the
  wheel-contents check;
- step 6 §6.

The shape follows M1's step 7 ([`M1-step7-acceptance.md`](../M1/M1-step7-acceptance.md)).

## 1. What this step does, in plain words

Steps 1–6 built native isolation and tested each piece on its own, mostly at the backend.
Step 7 is the **exam**:
- one test per criterion through the entry point an agent uses (`server.call_tool`);
- the cold-start benchmark the README will quote;
- one live session with Linux Claude Code in WSL;
- the docs pass: everything written before M2 that is now wrong.

When they all hold, M2 is accepted.

## 2. The 17 criteria: what exists, what step 7 adds

"Handler" means `server.call_tool` with `server.LIMITS` (a temp workspace), `server.SELECTOR`
(a fresh one) and `server.CONFIG_ERROR` (None) swapped in. Real selection, the run, the result's
rendering and the server's own `note_result` hook are then all in the path. "Backend" means a
test that builds `NativeBackend` directly.

| # | Criterion (contract §9) | Covered today | Step 7 adds |
|---|---|---|---|
| 1 | Hello in python, bash and node ⇒ `isolation: native` | backend, native-tier, all three; handler for python | handler, all three, exactly `native` |
| 2 | No host filesystem visibility | backend (the canary, `/proc/1`, `find /`, the mount table, the PIDs, no `/sys`) | the same through the handler |
| 3 | TCP, DNS, UDP and `AF_VSOCK` fail; only `lo` | backend (guest-view test; step 5's denials test) | one handler test with all five |
| 4 | Memory bomb contained; the server and the next run fine | backend | handler: the note rendered, then a normal run |
| 5 | Fork bomb stops at `pids.max`; the next run works | backend | handler, M1's controlled-then-classic order |
| 6 | `cpu.max` reads back; a busy loop near `cpu_pct` | backend | handler for the loop |
| 7 | `/tmp` cap; `EROFS` on the rootfs and `/sandbox` | backend | handler |
| 8 | An infinite loop and a self-stopped guest time out; nothing survives | backend | handler, after a warm-up call: `[timed out` rendered within timeout + 2 s grace + margin (as the backend test's bound, not the outer deadline's, which the handler enforces anyway); no token holder; no scope of this run's left |
| 9 | Denied syscalls named; the storm bounded with the helper's CPU inside the envelope; a foreign-arch syscall kills | backend; the envelope's static half in `test_the_limits_from_outside`; the bad-arch filter attribute (step 5) | handler for the denials and the storm; the envelope during the storm (§3.2); the foreign-arch line per S7-D2 |
| 10 | The guest's own view, incl. `Seccomp: 2` | backend guest-view test, without the `Seccomp:` line | handler, with the guest's own `Seccomp: 2` |
| 11 | Server hard-killed mid-run ⇒ nothing within 2 s | backend in a subprocess; the stdio stdin-close test | the real stdio server `SIGKILL`ed mid-run |
| 12 | 4 runs at once, 4 PID namespaces | backend | handler, the four namespace inodes distinct |
| 13 | Degradation by injection | selection-level, all five, in `test_native_select.py`; the rootfs-removed case calls `note_result` itself | **handler: a rootfs removed after selection** (§3.4); the rest mapped. "No delegation" is shown at selection level through the no-bus injection, and a controller left undelegated at backend level only |
| 14 | Cold-start benchmark | — | §3.1 |
| 15 | Both platforms green | the gate | mapped only |
| 16 | The live-agent check | — | §3.3 (7b) |
| 17 | Minimal-environment launch selects native | the stdio test (step 6) | mapped only |

**What "the host is unaffected" can honestly mean,** as in M1: the guest was stopped and the
cgroup says why, the server returned a normal result, the next run works, and nothing survived.

## 3. Decisions

### S7-D1 (Victor, 2026-10-28) — Two parts, as M1

- **7a — the exam and the benchmark:** `tests/test_native_acceptance.py`, the benchmark
  script. Its own gate, `critic` and `verifier`, and its own commit.
- **7b — the live checks, then the docs:**
  1. the live checks (§3.3) run first, so a bug they find gets a failing test and a fix with
     their own review;
  2. the docs pass (§4);
  3. the "accepted" lines last, as M1's separate acceptance commit did.

  Its gate and review, then its commit.

**Why:** 7a is code a reviewer reads line by line; 7b is mostly prose plus sessions only Victor
can run.

**The alternative:** one step and one commit.

### S7-D2 (Victor, 2026-10-28: (a); amends criterion 9) — What the foreign-arch line rests on

Criterion 9 says "A foreign-arch syscall kills the process (the step-5 plan picks the exact
test)". Step 5 built the protection and proved it at the filter level: the bad-arch action is
`KILL_PROCESS`, and only the host architecture is registered. It deferred a live test to here.

- **(a) The filter-level proof, with the criterion's wording amended to say what it rests on.**
  - The 32-bit ABI is refused by the filter's architecture check; both halves of that
    configuration are pinned by step 5's two unit tests.
  - The x32 ABI shares the host's architecture token, so the arch check doesn't catch it.
    libseccomp's own handling of a native filter does, and the line says so instead of
    claiming a test.
- **(b) A live test,** which the `skeptic` recommends: a short guest that makes a foreign-arch
  call and must be killed, skipped with a reason where the kernel has no 32-bit support. It
  exercises the filter the helper actually loads, through the kernel. The `skeptic` also
  pointed out that step 5 asked for a search for a safe trigger, and v1 of this plan showed
  none.

**Decided: (a), as recommended.** Step 5's tests pin the configuration the kernel acts on, and the
amended line is honest about the one part that rests on libseccomp. (b) would add test-only
low-level code to the suite, the only code of its kind in the project.

### S7-D3 (Victor, 2026-10-28; implements E2) — Linux Claude Code in WSL

WSL's `claude` today is the Windows npm binary, and WSL has no Node.

**Decided (as recommended):** Claude Code's native installer, which needs no Node. Victor runs it and
logs in inside WSL, since login is interactive, and checks that `command -v claude` now
resolves to `~/.local/bin`, not `/mnt/c`.

The server is registered from the dev venv, which is the code being accepted, through
`sh -c 'exec ~/.venvs/vestibule/bin/vestibule-mcp 2>>~/vestibule-live.log'`:
- `exec` keeps the server's PID, so the client's signals reach it unchanged;
- its stderr lands in a file we control, whatever the client logs.

**The alternative:** Node in WSL, then the npm package.

### 3.1 (fixed; criterion 14) — The benchmark

`scripts/bench_coldstart.py`, a dev script (not part of the gate):
1. builds a wheel of the working tree and checks it holds `native_helper.py`,
   `native_seccomp.py` and `seccomp-profile.json` (step 5's carried check);
2. installs it into a fresh venv on ext4, `~/.venvs/vestibule-bench`, with
   `-c requirements/constraints.txt`;
3. for each backend, **forced** (`VESTIBULE_BACKEND=native`, then `container`):
   - one warm-up call, so selection is cached;
   - then 50 `server.call_tool("run_code", python print(1))` calls, each timed end to end;
   - every timed call must render exactly `isolation: native`, or `isolation: container`, or
     the script stops. A degraded run, or a re-selection after one, never mixes into a row;
   - between calls, untimed, it waits for the warden's detached work (`drain()`). A container
     run's reaper passes and its `kill`/`rm -f` are ~130–145 ms CLI calls here, and would
     otherwise overlap the next timed run;
4. prints, per backend, the median and p95, with the host, the commit hash and the Docker
   version.

**What the README quotes:** the two medians and p95s, with their host and the caveat that the
container path here goes through Docker Desktop's VM, a hop a Docker Engine user on Linux
doesn't pay. **No headline ratio.** PLAN.md's "10–50×" and its résumé line's "~Y×" become the
measured numbers with the same caveat (spike B1).

**Its check:** it runs to the end on WSL with both backends; the output goes into BUILD_LOG.

### 3.2 (fixed; criterion 9's second part) — The helper's CPU stays inside the envelope

The static half is already proven: `test_the_limits_from_outside` asserts the helper is
`supervisor/`'s process and the scope's `cpu.max` is the envelope. During a storm through the
handler, step 7 adds, read from outside:
- the helper's own `/proc/<pid>/cgroup` is `<scope>/supervisor`;
- the scope's `cpu.max` is the envelope (`cpu_pct` + 25 % of one CPU).

**No CPU-usage assertion.** The storm hits its 256-notification cap in milliseconds, so "usage
grew" would pass on the helper's ordinary work. The evidence is where the helper is charged,
and the envelope's value.

### 3.3 (fixed; criterion 16, 7b) — The live checks

**Linux Claude Code** (the criterion):
1. I create an absolute-path canary (`$HOME/.vestibule-canary-<random>/id_ed25519`), and remove
   it afterwards.
2. Victor starts Claude Code from a **neutral directory** (not the repo, whose CLAUDE.md would
   tell the agent to plan first), in a session where only Vestibule's tools are allowed. Then
   he pastes a prepared prompt with the exact snippets. Each step must go through
   `mcp__vestibule__run_code` or `read_workspace`:
   - hellos in python, bash and node;
   - a TCP connection attempt;
   - a memory bomb;
   - reading the canary's absolute path;
   - listing the workspace.
3. **Passes when,** judged from the raw tool results:
   - every result says `isolation: native`;
   - the connection and the canary read fail;
   - the bomb carries the OOM note;
   - the workspace lists.
4. **Shutdown:** the session is ended normally. `~/vestibule-live.log`'s last line
   ("vestibule shutdown (<trigger>)") names the trigger.
   - **Expected: SIGINT.** `stdioutil.py` records Claude Code 2.1.300 on POSIX as SIGINT,
     100 ms, SIGTERM, 400 ms, SIGKILL.
   - The Claude Code version is recorded.
   - Then no `vestibule-warden` process and no `vestibule-*.scope` may be left.
   - A missing line means a SIGKILL before the handler ran, and that is recorded as such.

**Windows Claude Code** (BUILD_LOG's Windows-shutdown row promised a re-measure here): Victor
ends a Windows session with a long container run in flight. Within the S7-D5 deadline we check
whether that container survived, and record the result. The row itself stays open for its
later fix (a liveness lease).

### 3.4 (fixed) — The acceptance suite's shape

- **`tests/test_native_acceptance.py`**, `native`-marked; the limit tests also
  `@cgroups_required`. One test per criterion, `test_cNN_…`. The docstring maps each
  criterion to its test, the existing ones relied on included.
- **The `tool` fixture,** as M1's: it swaps in `server.LIMITS`, `server.SELECTOR` and
  `server.CONFIG_ERROR`, and drains the selected warden at teardown.
  - **The backend:** `auto` for c01, under `@cgroups_required`, which proves N5's choice;
    forced `native` everywhere else, so a host with Docker but no user manager doesn't turn
    native tests into container results.
- **c13, a rootfs removed after selection, through the handler** (finding 1): the first
  `call_tool` selects native. `rootfs.find` is then patched.
  - The next call must render `isolation: none … nothing was executed`; the server's own
    `note_result` hook dropped the selection.
  - The call after that must be `Blocked:` with the `setup` line.
  - Its mutant deletes the hook from `server.py`.
- **Every result is checked against the allowed isolation values.**
- **The scope check** compares only this test's own scopes, named from its runs' "starting a
  native sandbox" log lines, so a live Vestibule server elsewhere on the host can't fail it.
- **Destructive tests are bounded** as in steps 4–5: the memory bomb by `memory.max`, the fork
  bomb by `pids.max` (controlled first), the storm by the 256 cap.

## 4. The docs pass (7b, after the live checks)

| File | Change |
|---|---|
| `README.md` | **Status:** M2 accepted (last). **One-time setup:** Docker is needed off Linux; on Linux, `vestibule-mcp setup` and the prerequisites below (CLAUDE.md: don't require Docker on Linux). **Configuration:** a `VESTIBULE_ROOTFS_DIR` row. **A "Native isolation on Linux" section:** what native is; what `auto` picks where; the benchmark numbers (§3.1); denied syscalls reported by name; debuggers work (S5-D1). **A per-distro table with a prerequisites column:** kernel ≥ 5.9 (`close_range`; pidfd needs 5.3), cgroup v2, a user manager delegating `cpu`, `memory` and `pids`, `busctl`, libseccomp, unrestricted user namespaces. Each row's verdict follows from it, marked measured or expected. WSL2 Ubuntu 26.04 with systemd is measured, noted as "Microsoft's kernel, no AppArmor". Stock Ubuntu ≥ 23.10 is expected to restrict, so "the container if Docker or Podman works, otherwise `Blocked:`". Debian, Fedora and Arch are expected, with their versions' prerequisites. arm64 is **untested** (step 2 §6). M4's VM run is named. **Know the limits:** the hostile-run bullet scoped to containers; the native residuals: with `VESTIBULE_BACKEND=native` and no user manager, runs have no memory cap and can trigger the host's OOM killer (N5's own reason); sandboxes run outside the client's cgroup (S4-D1); the mount table shows the host username; supplementary groups still count (N6). |
| `CLAUDE.md` | Line 9: seccomp required for native. Golden rule 5: `native`, `native-degraded`, `container`, `container-degraded`, `none`. The Async bullet: a helper process, not a fork in an executor (N1). The repo-structure block: the `native_*` modules. |
| `docs/ARCHITECTURE.md` | The contract's §8 corrections, plus the as-built ones §8 predates (finding 5): **(1)** the helper unshares user + PID, init the rest, incl. `NEWCGROUP` (spike A1; the overview, the lifecycle and the code sample); **(2)** `lo` is up, and the network namespace alone does not isolate `AF_VSOCK`, which the filter denies (N4); **(3)** the stop kills PID 1 through a pidfd (S3-D7, S4-D2); **(4)** `memory.peak` waits for M3 (S4-D8); **(5)** systemd removes the scope; there is no temp rootfs (S3-D1); **(6)** the overview's "executor" label. |
| `docs/PLAN.md` | Line 35 (seccomp: required for native, our binding); 76 (`pyseccomp`); 77 (the executor); 80 (the `~/.ssh` probe, which can't fail); 108 (Windows: the container; the WSL bridge is M4's, E2); 78 and the résumé line (measured numbers); M2 done; the contract named as superseding the M2 list. |
| `docs/GETTING_STARTED.md` | The Linux section exists. Its edits: line 132's "native becomes reachable in M2 step 6"; a troubleshooting entry for `native-degraded` (the reason in the detail, and what fixes it); the leftover-scope check (`systemctl --user list-units 'vestibule-*'`). |
| `docs/plans/M2/M2-native-warden.md` | Status → accepted (last). §9: criterion 9's first bullet names `mount`, not `ptrace` (S5-D1); its foreign-arch line per S7-D2; an acceptance record (criterion → test). |
| `docs/HISTORY.md`, `docs/BUILD_LOG.md` | 7a and 7b entries; M2 accepted. **Known issues:** the S4-D1 row closed by its README note; the placement-time row closed (step 6's probe logs it); the Linux shutdown question answered (§3.3); the Windows row's re-measure recorded. |

**7b's check, besides `tests/test_docs.py`:** a grep of the live docs (README, CLAUDE.md
outside its changelog, `docs/` outside its dated records) for `pyseccomp`, `namespaces-only`,
`run_in_executor` and `10–50` returns nothing.

**Not in this step:** `SECURITY.md` is M4's (CLAUDE.md); what belongs there is carried in §7.

## 5. Changes

| Part | File | Change |
|---|---|---|
| 7a | `tests/test_native_acceptance.py` | new: §2's handler tests, §3.2, §3.4 |
| 7a | `scripts/bench_coldstart.py` | new: §3.1 |
| 7b | the files in §4 | the docs pass |
| both | this plan | §8's as-built |

No `src/` change is planned. A bug the exam or the live checks find gets a failing test first,
then a fix, reviewed with its part.

**Size:** the acceptance file about 450–500 lines (M1's is 448 code lines for 14 tests with
simpler guests, and this one adds the stdio `SIGKILL`, the storm-time scope reads, the classic
bomb and the canary); the benchmark about 120. Look again past 550 and 170.

**Build order, each step with its check:**
1. **7a:** the `tool` fixture and criteria 1–3 → they pass on WSL 3.14 and 3.11.
2. Criteria 4–8, 12, 13 → they pass; the scope check stays clean.
3. Criteria 9–11, the envelope (§3.2) → they pass, and each mutant in §6.2 turns its test red.
4. The benchmark → it runs end to end on WSL with both backends; the numbers go into BUILD_LOG.
5. Both platform gates green, 0 skips on Linux, inside the 540 s budget.
   - **Expected cost:** the gate runs native tests in both its 3.14 and its 3.11 pass, so
     about +45 s twice: about 340 s against 248 s after step 6. Past 370 s, the slow tests get
     looked at.
6. 7a's `critic` and `verifier`; the as-built; Victor commits 7a.
7. **7b:** Victor installs Linux Claude Code (S7-D3); the live checks (§3.3) → recorded.
8. The docs pass (§4) → `tests/test_docs.py` and the retired-terms grep pass.
9. 7b's `critic` and `verifier`; then the "accepted" lines; Victor commits 7b; M2 accepted.

## 6. Tests

### 6.1 What each acceptance test asserts

§2's "step 7 adds" column, through the handler. Every test also checks the allowed isolation
values and its own scopes.

### 6.2 Mutation checks (during the build; not committed)

Each must turn its guarding test red:
- the server's `note_result` hook deleted → c13;
- init skips loading the filter **and** the verdict's seccomp check is removed → c10 (only the
  guest's own `Seccomp:` line can catch it);
- the envelope's `CPUQuotaPerSecUSec` dropped from the unit → §3.2;
- the helper placed outside the scope's `supervisor/` → §3.2;
- the overflow or timeout stop removed → c08;
- the `pids` limit not written → c05;
- the OOM note dropped → c04;
- `AF_VSOCK` allowed → c03.

## 7. Carried forward

- **M4:**
  - `SECURITY.md`: the allowlist's origin, the `AF_VSOCK` note, the guest-symlink residual, the
    host username in the mount table, the supplementary groups, and the x32 point of S7-D2;
  - the AppArmor message on a stock Ubuntu 24.04+ VM, and the per-distro table's "expected"
    rows confirmed;
  - **the release checklist gains a wheel smoke test:** install the wheel, then
    `import vestibule.server`. `native.py` reads the vendored profile at import on every
    platform, so a wheel without it can't start (verified, V13). 7a's benchmark checks the
    wheel once; nothing re-checks it until then.
- **Later:** the Windows shutdown row's fix (a liveness lease); the `mcp<2` pin.

## 8. As built

### 7a — the exam and the benchmark (2026-10-31)

- **Files:** `tests/test_native_acceptance.py`, 499 lines (estimate 450–500), and
  `scripts/bench_coldstart.py`, 150 lines (estimate ~120, look again past 170). No `src/`
  change.
- **Where it differs from §3–§6:**
  - **§6.2's c08 line is corrected** (`critic` finding 1). Criterion 8 covers timeouts only:
    - **The overflow stop** is guarded by the backend test
      `test_native.py::test_an_output_flood_is_cut_and_the_sandbox_stopped`, which its mutant
      turns red. No c08 guest prints.
    - **The timeout stop:** deleting only the `_stop` in `_collect`'s timeout branch changes
      nothing anyone can see, because the detached finisher stops the helper right after. So
      the timeout mutant removes the wait bound itself (`wait_for(…, timeout=None)`), leaving
      only the helper's self-deadline, and c08 goes red.
  - **§3.1: each container sample includes one reaper pass.** Draining between calls keeps a
    run's `kill` and `rm -f` out of the next timed call. But every container run starts its own
    reaper pass (one `docker ps`) beside its `docker run` (`container.py`'s `_run`). That is what
    a call costs in use, and the script's docstring says so; the README caveat (7b) will too.
  - **§3.2: the helper is found by its parent,** as this process's own `vestibule-warden`
    child, not through `supervisor/cgroup.procs`, so where it is charged can be checked.
  - **The whole module is `@cgroups_required`,** not only the limit tests: every test expects
    exactly `native`.
  - **The scope check fails when `systemctl --user` can't answer,** instead of reading as
    clean, and when it saw no run of its own test.
- **What the build found in the test draft** (written 2026-10-28, first run 2026-10-31):
  1. *The teardown scope check checked nothing.* At fixture teardown `caplog.messages` holds
     only the teardown phase's records, so this test's scopes were always the empty set. It
     reads `caplog.get_records("call")` now. A guard that fails on an empty set found it.
  2. *c09's helper check was circular:* it read `supervisor/cgroup.procs`, then asserted that
     PID sits in `supervisor`.
  3. *c09 flaked once in four runs:* a second `holders(token)` call caught the guest mid-`exec`,
     when its command line reads empty. The PID from the wait loop is kept; 20 of 20 after.
     `test_native.py:677` has the same pattern (step 4's, left as it is).
- **Mutation checks** (§6.2), in WSL, sources backed up and byte-compared after, a no-mutant
  control green first. All 10 turned their test red at the intended assertion:

  | Mutant | Red at |
  |---|---|
  | the server's `note_result` hook deleted | c13: the third call isn't `Blocked:` |
  | init loads no filter, and the verdict's `Seccomp: 2` check is removed | c10: the guest sees `Seccomp: 0` |
  | the envelope's `CPUQuotaPerSecUSec` dropped | c09: the scope's `cpu.max` is `max 100000` |
  | the helper leaves the scope once init is placed | c09: the helper's cgroup isn't `supervisor/` |
  | the timeout's wait bound removed | c08: the server's outer deadline, not `[timed out` |
  | the overflow stop removed | the backend flood test |
  | `pids.max` not written | c05 (a): `native-degraded`, the RLIMIT backstop |
  | the OOM note dropped | c04 |
  | a process left in the scope after the run | the fixture's scope check |
  | `AF_VSOCK` allowed | c03 |

  Three first attempts modelled the wrong thing and were redone: the timeout-branch `_stop`
  above; a helper moved out before init existed (systemd collected the empty scope, so
  selection failed instead); and a leftover forked after `unshare(CLONE_NEWPID)`, which sat
  inside the PID namespace, where init waits for it.
- **The benchmark (criterion 14),** WSL2 Ubuntu 26.04.1, kernel 6.6.87.2, 12 CPUs, Python
  3.14.4, Docker Desktop 28.5.1, the wheel built from `3cf3024`, 50 timed calls each:

  | Backend | Median | p95 |
  |---|---|---|
  | native | 81.9 ms | 106.9 ms |
  | container | 392.3 ms | 413.0 ms |

  Two earlier runs that day: native 84.3 / 114.1 and 79.0 / 121.1 ms; container 384.4 / 430.0
  and 387.1 / 439.9 ms. Step 0's spike measured native with a `busctl` scope at 75.4 / 121.6
  ms and a bare `docker run` at 386.9 / 410.0 ms, so the server adds little to either path.
- **Review:** `critic`, 1 medium and 8 low; `verifier`, 8 confirmed, 1 refuted.

  | # | Sev. | Finding | Verdict | Disposition |
  |---|---|---|---|---|
  | 1 | med | c08 can't catch the overflow-stop or timeout-stop mutant | confirmed | §6.2 corrected (above); no code |
  | 2 | low | c12 never shows the four runs overlapped | refuted | a backend that runs one sandbox at a time fails c12 5 of 5 (freed namespace inodes are reused) |
  | 3 | low | `scopes() or set()` reads a failed `systemctl` as clean | confirmed | `_live_scopes()` asserts |
  | 4 | low | a c09 failure leaves the storm waiting and adds a false teardown error | confirmed | `try`/`finally` ends the storm |
  | 5 | low | c05's bomb runs don't check their own isolation | confirmed | both assert `native`; the `pids.max` mutant now fails at (a) |
  | 6 | low | the benchmark's docstring says the reaper is drained out | confirmed | docstring corrected; §3.1 note above |
  | 7 | low | the "uncommitted changes" mark misses `pyproject.toml`, the constraints and untracked `src/` files | confirmed | covers all three |
  | 8 | low | `Tool.read` has no caller | confirmed | deleted |
  | 9 | low | `make_tool` keeps a list though each test makes one tool | confirmed | kept: it mirrors M1's fixture (`tests/test_acceptance.py`), and one tool would need a `nonlocal` |

### 7b — the live checks and the docs pass (2026-10-31)

- **Linux Claude Code (§3.3, criterion 16): passed.**
  - **Setup:** Claude Code 2.1.316 from the native installer (`~/.local/bin/claude`), started
    from a neutral `~/vestibule-live` with `--strict-mcp-config --mcp-config mcp.json --tools ""
    --permission-mode dontAsk --allowedTools mcp__vestibule__run_code
    mcp__vestibule__read_workspace`. So only Vestibule's two tools existed, and anything else
    would have been denied without a prompt. `mcp.json` registers S7-D3's `sh -c 'exec
    ~/.venvs/vestibule/bin/vestibule-mcp 2>>~/vestibule-live.log'` (code at `6ada758`). The
    canary was `$HOME/.vestibule-canary-<12 hex>/id_ed25519`, removed afterwards.
  - **Results,** judged from the session's JSONL transcript (each tool result appears twice in
    it; the server log shows one probe and six runs, so each call ran once):

    | Call | Result |
    |---|---|
    | python, bash, node hellos | exit 0, `isolation: native` each; the bash one wrote `hello.txt` |
    | `socket.create_connection(("1.1.1.1", 443), 3)` | `OSError: [Errno 101] Network is unreachable`, `native` |
    | a list growing by 10 MB chunks | exit 137, `[out of memory: the kernel killed a process in the sandbox at its 256 MiB limit]`, `native` |
    | `cat` of the canary | `No such file or directory`, `native` |
    | `read_workspace` `.` | `file 16 hello.txt` |

  - **Shutdown:** `/exit`; the log's last line was `vestibule shutdown (SIGINT): nothing to
    clean up`, as `stdioutil.py` records for 2.1.300. The server's PID was gone, and there was
    no `vestibule-warden`, cleaner or `vestibule-*.scope`. The selection probe took 105 ms.
  - **Differences from §3.3:** the bash hello writes the file the listing then shows; the
    memory bomb runs in the guest itself (exit 137), not in a child as c04's does; "only
    Vestibule's tools" is enforced by the launch flags rather than asked for in the prompt.
- **Windows Claude Code (the BUILD_LOG row's re-measure):** Claude Code 2.1.316, a neutral
  folder, the repo's registered server, one `run_code` of `time.sleep(55)` with a 60 s timeout.
  A `docker ps` poll on the owner label logged every container change from outside.
  - The first two runs finished on their own before an interrupt (when to press it wasn't
    clear). Those runs only show the watcher works.
  - **Ctrl+C about 22 s in:** Claude Code cancels the call first; the container was last seen up
    at 21:15:53.76 and gone by the next poll at 21:15:54.51 (no timestamp exists for the key
    press itself), and no `%TEMP%\vestibule-run-*` dir was left. `/exit` two minutes later
    closed the server's stdin, and the three server processes exited.
  - **The terminal window closed about 12 s into a run:** Claude Code closed stdin with the call
    in flight ("Connection closed"); the container was up 14 ms later and gone 1.43 s after the
    close, against a self-deadline more than two minutes away. No server process, container or
    `%TEMP%\vestibule-run-*` dir was left.
  - **What it settles:** Ctrl+C can't end a session with a run in flight, since the cancel
    comes first. A closed window can, and there the in-process cleanup won the race this time.
    One sample, so the row stays open for the liveness lease.
  - **Found:** after the cancel, Claude Code logged "Received a response for an unknown message
    ID" and "STDIO connection dropped": the MCP SDK (1.28.1, `RequestResponder.cancel`) answers
    a cancelled request, and Claude Code doesn't expect it. A new BUILD_LOG known issue,
    upstream.
  - The watcher's first version tracked the selection probe's container and stopped 20 s after
    it went. It was restarted within 20 s with the end condition fixed; the run's container
    was up on both sides of the gap.
- **The docs pass (§4),** with these differences:
  - **`ARCHITECTURE.md` Part 2 is rewritten, not patched.** The old stage list (every namespace
    unshared before the fork, the cgroup joined from inside, `0 <uid> 1`, an executor,
    optional `pyseccomp`, a temporary rootfs) was wrong at nearly every stage, and six line
    fixes would have left a sequence that never existed. It now follows the build: placement
    from outside, the helper, init, the jail, the privileges, seccomp, the stop. Part 1 changed
    only where it said the server never touches cgroups, and the executor paragraph.
  - **The per-distro table's versions rest on one fact checked during the pass:** systemd
    delegates `cpu` to user managers by default since v252 (systemd commit `b8df7f86`, "user:
    delegate cpu controller, assign weights to user slices", July 2022; v252 was released that
    October). Debian 12 ships 252 and Fedora 38 ships 253. This host's `user@.service` (systemd
    259) carries `Delegate=pids memory cpu` from the vendor unit.
  - **README line 7:** `6ada758` carried the badge link wrapped as `](<https://…>)`, which failed
    `test_docs` on HEAD. It is a plain link again.
  - **Hashes:** 7a's `6ada758`, and HISTORY's step-3 bullet, which still said "commit pending",
    gets `1ef1b46`.
  - **Not done, noticed:** `GETTING_STARTED.md`'s "Run the checks" names M1's exam file but not
    `tests/test_native_acceptance.py` (§4 didn't list it).
- **Review:** `critic`, 4 medium and 9 low; `verifier` confirmed all 13 and put 12 at low and one
  at medium (#13, reproduced in WSL, as was #5). All are wording; no code changed.

  | # | Finding | Disposition |
  |---|---|---|
  | 1 | `ARCHITECTURE.md` said a failed `Seccomp: 2` check aborts before `execve`; init only reports it, and the server judges it (and its own comparisons) after the run | init's abort list and the server's after-the-run checks are now separate |
  | 2 | `CLAUDE.md` golden rule 2 says the user namespace maps the uid to root inside; the build uses an identity map (N6) | a golden rule, so put to Victor: reworded with his approval (2026-10-31); the rule itself is unchanged |
  | 3 | README: "can still learn two things" reads as a complete list | examples now, uid/gid, kernel version and `/proc`'s CPU and memory info included |
  | 4 | `PLAN.md`'s M2 bullets beside the edited ones still gave the old unshare order and "a minimal rootfs" | updated to the build |
  | 5 | README: "any other call fails with `EPERM`"; `clone3` gets `ENOSYS` (Docker's rule), as do named calls past the 256 cap | both stated |
  | 6 | the `RLIMIT_NPROC` backstop holds only from Linux 5.14; the README allows 5.9 | `ARCHITECTURE.md` gives the condition; the forced-native residual says a fork bomb is uncapped before 5.14 |
  | 7 | "the strong, non-escapable version of chroot", carried over from the old text, against the file's own "never claim unescapable" | reworded |
  | 8 | "gone within 0.75 s" after Ctrl+C is the gap between two polls; no key-press timestamp exists | reworded as the poll gap |
  | 9 | "no `vestibule-run-*` dir either way" was recorded only for the window close | the Ctrl+C check was made at the time (21:16, none); the record above now says so |
  | 10 | `GETTING_STARTED.md`: "until `setup` has run, a call names the exact command" holds only when no container wins | qualified |
  | 11 | acceptance row 13: "each in N5's order" for the AppArmor and libseccomp cases, which run with native forced | reworded; `test_select.py::test_auto_on_linux_follows_n5s_order` cited for the order |
  | 12 | the record said every result must be exactly `native`; c13 expects `none`, then `Blocked:` | reworded. The test module's docstring says the same and is left as it is (no `tests/` change in 7b) |
  | 13 | "never root": nothing refuses a server started as root, and then the guest is host uid 0 (no capabilities, `isolation: native`) | the README and `ARCHITECTURE.md` qualify it; a BUILD_LOG known issue for the refusal |
- **Gate:** the first Linux run failed one test,
  `test_native.py::test_a_hard_nproc_limit_below_pids_max_still_runs`. It sets `ulimit -u 100`,
  and the kernel counted about 102 tasks for uid 1000 (`ps` in the distro: 36; the rest belong
  to other WSL distros sharing the kernel), with the login-time WSL Claude Code session still
  open. With it closed, the count was about 76 and everything passed: Linux (274 s) 553 with 0
  skipped on 3.14, 510 Docker-free on 3.11, stdio 5 of 5; Windows 412 + 121 skips; ruff, mypy
  and check_env green on both. A BUILD_LOG known issue for the test.
- **The retired-terms check (§4)** finds none of `pyseccomp`, `namespaces-only`,
  `run_in_executor` or `10–50` in the live docs. `tests/test_docs.py` passes.

## 9. Review record

`skeptic` reviewed v1 on 2026-10-27. Verdict: **"sound with amendments"** (8 medium, 13 low).
The `verifier` checked thirteen of its claims about the code and docs: **13 confirmed**. Every
finding was accepted and applied in v2. S7-D2's finding went to Victor as a decision.

| # | Sev. | Finding | Disposition |
|---|---|---|---|
| 1 | med | Criterion 13's re-probe never goes through the handler; deleting the server's `note_result` hook leaves the suite green (V1) | c13 through the handler, with that mutant (§3.4); the delegation evidence stated (§2) |
| 2 | med | The benchmark: container cleanup overlaps the next timed run (V2); no per-call isolation check; the ratio includes Docker Desktop's VM | forced backends, a per-call isolation check, untimed drains, constraints, the commit hash; medians, no ratio (§3.1) |
| 3 | med | The envelope check can't fail; its mutant doesn't model an escape; the static half exists (V10) | storm-time cgroup reads, real escape mutants, no usage assertion (§3.2) |
| 4 | med | The shutdown evidence assumes the client logs server stderr; the expected trigger is SIGINT (V3) | stderr to our own file via `sh -c exec`; SIGINT expected; the version; no leftovers (S7-D3, §3.3) |
| 5 | med | `ARCHITECTURE.md` has false claims beyond §8's seven (V11) | six more corrections (§4) |
| 6 | med | The per-distro table lacks the real prerequisites (V4); WSL is native only for its kernel; arm64 dropped | a prerequisites column; the WSL note; arm64 untested; the restricted rows' wording (§4) |
| 7 | med | S7-D2's cost overstated; no search for a trigger shown; the x32 ABI isn't caught by the arch check | both options to Victor; the x32 point in (a)'s wording and M4's carry list (S7-D2) |
| 8 | med | PLAN.md under-scoped (V7) | each line listed; the retired-terms grep (§4) |
| 9 | low-med | The gate runs native tests twice (V5) | ~340 s, look again at 370 s (§5) |
| 10 | low-med | The live prompt: built-in tools and the repo's CLAUDE.md could spoil it | a neutral directory, only Vestibule's tools, exact snippets, raw results, the `claude` path (§3.3) |
| 11 | low | README: no `VESTIBULE_ROOTFS_DIR` row, Docker required on Linux, a container-only bullet, the native no-limits residual (V8) | all four (§4) |
| 12 | low | Criterion 9's first bullet still says `ptrace` (V12) | amended (§4) |
| 13 | low | The seccomp mutant can't turn anything red | skip the load **and** the verdict's check (§6.2) |
| 14 | low | Criterion 8's bound can't fail; no warm-up | the backend test's bound, after a warm-up (§2) |
| 15 | low | The fixture's backend value unstated | `auto` for c01, forced `native` elsewhere (§3.4) |
| 16 | low | The Windows re-measure dropped silently (V9) | re-measured in 7b (§3.3) |
| 17 | low | GETTING_STARTED already has a Linux section; the real stale lines unlisted (V6) | the real edits (§4) |
| 18 | low | 7b writes "accepted" before the live check; its check can't fail on a claim | live checks first, "accepted" last, the grep (S7-D1, §4) |
| 19 | low | The wheel check runs once and nothing re-checks it; a missing profile breaks start everywhere (V13) | carried to M4's release checklist as a smoke test (§7) |
| 20 | low | ~350 lines looks low | 450–500, look again past 550 (§5) |
| 21 | low | The scope check would see a live server's scopes | only this test's own runs (§3.4) |
