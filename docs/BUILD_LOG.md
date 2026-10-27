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
| M2 step 2 | Rootfs: registry client, unpacker, `vestibule-mcp setup` | 2026-10-25 | `2af2c0c` `9197a55` + review fixes and Karpathy re-review `4c8afcf` | 358 Linux (0 skipped); 305 + 33 POSIX/Linux-only skips on Windows |
| M2 step 3 | Warden core: helper, jail, PID-1 hardening, kill path (not reachable from the server yet) | 2026-10-25 | `1ef1b46` | 428 Linux (0 skipped); 348 + 60 POSIX/Linux-only skips on Windows |
| M2 step 4 | cgroups v2: per-run scope, guest limits, OOM note, fork-bomb backstop (not reachable from the server yet) | 2026-10-26 | `8b1de5e` | 470 Linux (0 skipped); 367 + 83 POSIX/Linux-only skips on Windows |
| M2 step 5 | seccomp: libseccomp binding, Docker-derived allowlist, bounded denial reporting; first real `native` (not reachable from the server yet) | 2026-10-27 | pending | 480 Linux (0 skipped); 369 + 91 POSIX/Linux-only skips on Windows |

## Known open issues

| Found | Where | Issue | Owner |
|---|---|---|---|
| 2026-10-11 (7a review) | `container.py` run CLI | If the *client* kills the process tree in the ~100 ms between `docker run`'s create and start, the container stays **Created** (never started, no CPU or memory use) until a reaper pass (deadline + 60 s). `--rm` and S7-D5 only act on started containers. | Accepted residual; reaper backstops |
| 2026-10-11 (S7-D5) | container profile | The self-deadline `timeout` runs as the guest's uid, so a *deliberately* malicious guest can `SIGSTOP` it and live until a reaper finds it, still capped by memory, CPU and pids unless its selection was `container-degraded`, which has only the limits it reported. | Documented residual (M4 `SECURITY.md`) |
| 2026-10-11 (S7-D1) | server hard exit | After a hard kill (Job Object / TerminateProcess / SIGKILL), per-run `%TEMP%\vestibule-run-*` script dirs are not removed. A startup sweep was rejected because it can't tell whose live run a dir belongs to. | M3/M4 candidate |
| 2026-10-11 (S7-D2) | workspace | Guest-made symlinks are real host symlinks that host file tools follow, for reads and writes. `read_workspace` refuses them, and the tool description warns. A structural fix (seccomp `symlink` denial) was considered for M2 step 5 and **rejected** (N4): denying `symlink`/`symlinkat` breaks `python -m venv` and npm `.bin` links in the workspace. M1's measures stay. | Documented (README, contract D1); revisit in M4 `SECURITY.md` |
| 2026-10-15 (M2 step 1b) | shutdown, Windows | Claude Code on Windows closes stdin, waits 300 ms, then `taskkill /T /F`s the server's tree. With a run in flight, M1's in-process cleanup (S7-D1, 0.2–0.4 s) can overrun that window, and the tree kill takes the `docker rm -f` with it, so the container lives until its S7-D5 self-deadline (`timeout + 90` s). A detached cleaner can't help on Windows: the server sits in kill-on-close Job Objects (the console-script launcher's, the venv redirector's, the Python SDK client's). The structural fix is a container-side liveness lease (a watchdog that ends the guest when the server's heartbeat goes stale), which would also cover a server killed outright on any platform. | Later milestone (liveness lease); M2 step 7 re-measures with a real session |
| 2026-10-14 (M2 step 1) | dependencies | `pyproject.toml` pins `mcp<2`. mcp 2.x (2.3.0 on 2026-10-14) replaced the low-level `Server` API this server uses (`list_tools`, `Tool(inputSchema=)`), so an unpinned install could not start. Moving to 2.x needs its own plan: it touches the protocol layer and the shutdown rules. | Before M4's PyPI release |
| 2026-10-25 (M2 step 3 review) | server logging | The server logs to stderr synchronously, so a client that stops reading stderr fills the pipe and the server blocks at its next log call. This hits every backend, M1's included. The native helper's own self-deadline no longer depends on stderr (step 3's second `critic` round), but a server blocked this way stops nothing. | Open; a non-blocking log handler is a candidate alongside M3's audit log |
| 2026-10-26 (M2 step 4, S4-D1) | native limits | Sandboxes run in the user manager's `app.slice`, outside the client's own cgroup, so a `MemoryMax=` a user put on their agent doesn't count them. Each sandbox has its own limits instead. | Accepted by Victor; README note in step 7 |
| 2026-10-26 (M2 step 4) | native placement | A host where systemd accepts the scope job and then fails it, with the helper alive, makes every run wait the full 5 s placement bound before running without limits. Not seen on any tested host. | Step 6's probe reports placement time |
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

## M2 step 5 — seccomp: libseccomp binding, Docker-derived allowlist, bounded reporting (2026-10-27)

- **Commit:** pending.
- **Plan:** [`docs/plans/M2/M2-step5-seccomp.md`](plans/M2/M2-step5-seccomp.md) S5-D1 to S5-D10
  (v2, after the `skeptic` rework). Contract amendment in M2 N4 (Q1/Q2 resolved + the mechanism).
- **What:**
  - **`native_seccomp.py`** (new): our own `ctypes` binding over the system `libseccomp.so.2`
    (N4 — libseccomp compiles the BPF). `build(profile)` ports moby's default profile for a
    capability-less, native-arch guest: default-deny `EPERM`; `clone3 → ENOSYS`; `socket`/
    `socketpair` restricted to AF_UNIX/INET/INET6/NETLINK with `AF_VSOCK` notified; a named
    dangerous subset via `SCMP_ACT_NOTIFY`; `io_uring_*` in `DENY_SILENT` (excluded from the
    allowlist, never notified — S5-D2); `PTRACE_POLICY="allow"` keeps moby's block-1 ptrace
    allow (S5-D1); bad-arch → `KILL_PROCESS`. Plus `load`/`notify_fd`/`alloc`/`receive`/
    `respond_eperm`/`name` and (for §4.1) `attr_get`/`arch_exists`.
  - **`seccomp-profile.json`** (new): moby/profiles `6fe7deb` verbatim, sha256-pinned by a test
    (S5-D9), Apache-2.0 (attributed in the binding's docstring). `.gitattributes` keeps git from
    rewriting its bytes.
  - **`native_helper.py`:** `_build_seccomp` imports the binding and compiles the ctx in the
    **helper, before it forks init** (so no ctypes array/POINTER type is first materialized after
    `pivot_root` — a 3.14 trap; S5-D3). `_init` gained `seccomp_ctx`; it now **loads** the filter
    and hands the listener fd to the helper over the init↔helper socketpair, **before `_checks`**
    (so `_checks` reads `/proc/self/status` `Seccomp:` as a 7th, reported-not-raising check —
    S5-D6). `_supervise` was rewritten: its **first** socket read is `recv_fds` (fd ⇒ listener,
    no fd ⇒ relay the bytes — the S5-D7 framing fix), and it answers notifications with `EPERM`,
    de-duplicating names and capping at 256 before closing the listener. The helper sends a
    `{"denied":[…],"notify_stopped":…}` status line.
  - **`native.py`:** reads the vendored profile once (`_PROFILE`), passes `profile` + `pkg_dir`
    in the run config, and `_denied` parses the status line into `RunResult.denied_syscalls` + a
    "reporting stopped" note.
  - **Config/docs:** the `pyproject` `seccomp` extra removed (N4); two CLAUDE.md bullets and a
    GETTING_STARTED libseccomp note (S5-D10); the contract N4 amendment.
- **Tests:** `test_native.py` migrated off the bypass — the `backend` fixture loads the real
  filter, the `seccomp_aside` fixture is gone, and the step-3/4 `STEP3_LABEL`/`none` side-checks
  became native-tier assertions; the subprocess runners (locked mounts, tty, killed server) keep
  the bypass since they probe non-seccomp mechanisms. New: `test_native_seccomp.py` (the binding,
  `native`-marked, imported via a fixture so collection never hits its Linux-only guard),
  `test_seccomp_profile.py` (the pin, runs everywhere), the denial + storm runs, the
  default-vs-bypass test, a `seccomp: False` verdict row. `nativeutil.native_skip_reason` now
  also requires `libseccomp.so.2`.
- **What broke along the way (build-time, fixed before gate):**
  1. *`seccomp_arch_exists` → `seccomp_arch_exist`* — the real symbol has no trailing `s`; the
     typo made the whole module fail to import. Build-order check 1 caught it at once.
  2. *`_OPS` trimmed to `EQ` only* — the first build raised `KeyError: SCMP_CMP_LT`. moby's
     *applicable* entries use `MASKED_EQ` (the `clone` rule that denies `CLONE_NEW*`) and others,
     so trimming would have silently mis-built the namespace-blocking clone rule. Restored the
     full 7-comparator map. (This is exactly why the plan front-loads the build as check 1.)
  3. *the default-vs-bypass test asserted the old `-1`/"nothing ran"* — under the step-5 bypass
     the guest still *runs* (the step-3 stub that raised is gone); only the verdict is `none`.
     Fixed to assert exit 0 + the file written + `none`.
- **Where to start if it breaks:**
  - a run wrongly `none` with "…not confirmed: seccomp": init didn't reach `Seccomp: 2` — check
    `native_helper._init`'s seccomp stage order (load is before `_checks`) and that libseccomp is
    present (`nativeutil`).
  - a `blocked syscalls:` line that's wrong/missing: `native_helper._supervise`'s listener branch
    and `native._denied`; the dangerous subset is `native_seccomp.NOTIFY`.
  - an innocent node run reporting `io_uring_setup`: it drifted out of `DENY_SILENT` or into
    `NOTIFY` (S5-D2).
  - the suite hanging on a seccomp test: a notified syscall in a lone child with no serviced
    listener blocks — §4.1's child-load test must use only silent-deny calls.
- **Mechanism verified by hand on 3.14 and 3.11** before the tests (scratch harness, not
  committed): build → 347 rules, bad-arch KILL, host-arch only; default → `native`; bypass →
  `none`; mount/unshare/keyctl/vsock denied+named, io_uring silent, AF_INET + self-ptrace
  allowed; 60k-notification storm → `["mount"]` + "stopped" note in ~2.3 s.
- **Review:** `skeptic` on the plan → "rework needed" (3 findings: the S5-D7 listener framing on
  an early abort; a §4.1 test that would hang on a notified `mount`; the 3.14 ctypes-after-pivot
  trap) — all fixed in plan v2 before code. `critic` on the diff → no high/med; one low "dead
  `PTRACE_POLICY=="deny"` branch" nit, kept deliberately (S5-D1's one-line stricter-posture
  toggle). Verified via 5 mutants (all red). Gate green both platforms (Linux 480/0 skips ~207 s,
  Windows 369 + 91 skips).
- **Sizes** (code lines): `native_seccomp.py` 175 (est. ~140; ctypes boilerplate + test helpers,
  looked at), `native_helper.py` 382 (est. ~365, look-again ~380; the notification loop is
  coupled to `_supervise`'s poll, looked at), `native.py` 439 (est. ~445).

## M2 step 4 — cgroups v2: per-run scope, guest limits, OOM note, fork-bomb backstop (2026-10-26)

- **Commit:** `8b1de5e`.
- **Plan:** [`docs/plans/M2/M2-step4-cgroups.md`](plans/M2/M2-step4-cgroups.md) S4-D1 to S4-D10
  (v3, after one `skeptic` review and a Karpathy re-check). Contract amendments in M2 N3, N8,
  §3, §4 and §10.
- **What:**
  - **`native_cgroup.py`** (new) places each run before its helper may start:
    - a transient systemd scope `vestibule-<id>.scope` holding the paused helper, asked for
      with `busctl --user`. The bus is found as N3 says, under the MCP SDK's minimal
      environment too;
    - `supervisor/` for the helper and `guest/sandbox/` for init. The guest's limits sit on
      `guest/` (`memory.max` with `memory.swap.max` 0, `pids.max`, `cpu.max`), each read back;
    - the envelope and an outside deadline as the scope's own properties: `MemoryMax`,
      `TasksMax`, `CPUQuotaPerSecUSec`, and `RuntimeMaxSec` with `KillSignal=SIGKILL` and
      `CollectMode=inactive-or-failed`;
    - one 5 s bound over all of it. Any failure degrades the run to "limits not applied: …"
      and logs why.
  - **`native_helper.py`:**
    - the helper waits for its release, then moves init into `guest/sandbox/`. A failed move
      stops the run with `none`;
    - init reports its own cgroup in `ready`;
    - when `pids` isn't applied, on Linux 5.14+, an `RLIMIT_NPROC` backstop, clamped to the
      user's hard limit;
    - after init's exit, the helper sends `guest/`'s `oom` and `oom_kill` counters.
  - **`native.py`:**
    - the release pipe, with placement between the spawn and the guest's timeout clock;
    - the verdict counts a limit only where init reports it is and the limit read back, so
      `native` is reachable once step 5's filter exists;
    - the OOM note on stderr names the memory limit only when that limit fired.
  - **Every real run still reports `isolation: none`** until step 5. One test sets the seccomp
    check aside and gets `native`.
  - **Measured in WSL:** placement adds about 2 ms to the median cold start (the prototype).
    The Linux gate still takes 207 s.
- **Files:**
  - Source: `backends/native_cgroup.py` (new); `backends/native.py`;
    `backends/native_helper.py`.
  - Tests: `test_native.py` (+17 Linux tests, and a no-scope-left check on every test);
    `test_native_verdict.py` (+25, six of them Linux-only); `nativeutil.py` (the `cgroups`
    skip rule, the scope listing).
  - Docs: the contract amendments; `docs/GETTING_STARTED.md` (the cgroup tests'
    prerequisite).
- **Guarded by:**
  - `test_native.py::test_the_limits_from_outside`: every limit file and the envelope, read
    by the test; the helper alone in `supervisor/`; nothing on the server's stdout.
  - `::test_a_memory_bomb_is_killed_at_the_limit`, `::test_a_fork_bomb_stops_at_pids_max`,
    `::test_the_cpu_limit_holds`.
  - `::test_systemds_deadline_stops_a_run_nothing_else_stops`: systemd's deadline alone
    ends a self-stopped guest; the helper's exit is −9.
  - `::test_a_hanging_busctl_costs_at_most_the_placement_bound`,
    `::test_a_cancel_during_placement_leaves_nothing`, `::test_without_busctl_…`,
    `::test_without_cgroup_v2_…`, `::test_without_a_bus_…` (with the backstop).
  - `::test_placement_finds_the_bus_in_the_sdks_minimal_environment`.
  - `test_native_verdict.py::test_limits_count_only_where_init_reports_it_is`,
    `::test_the_oom_note`, `::test_the_nproc_backstop_needs_linux_5_14`, and the two
    `bus_env` tests.
- **Broke along the way:**
  - **The prototype, before the plan** (WSL `~/m2spike/step4/`): `busctl`'s stdout would have
    reached the MCP channel without `/dev/null`, and `busctl` missing from `PATH` raises at
    the spawn.
  - **`skeptic`** (11 findings, all accepted, the three medium ones re-measured first):
    - a scope that ends badly stays `failed` and leaves the user manager `degraded`;
    - init ignores SIGTERM, so systemd's deadline needs `KillSignal=SIGKILL`;
    - `busctl --timeout` doesn't cover a bus that never answers;
    - before 5.14, `RLIMIT_NPROC` counts the user's processes across the host.
  - **The Karpathy re-check** that Victor asked for: three gaps (a guessed size estimate, no
    test for `busctl`'s stdout, two behaviours no test could fail).
  - **`critic`** (8 findings; `verifier`: 7 confirmed, 1 plausible; all dealt with). Two real
    bugs, each with a test that failed first:
    - with no user bus and a hard process limit below 128, every run was refused at
      "privileges", because no process can raise its hard limit;
    - on a cgroup-v1 host, every run would have waited the full 5 s, with the log blaming the
      bound.

    It also found four test gaps: the readback compare, a controller that isn't delegated,
    `bus_env()`'s environment, and the size line.
  - **Mutation checks:** 28 broken versions, all red. A memory-pressure kill of the
    background run left one mutant (no placement bound) in `native_cgroup.py`. The file was
    restored, diffed against a backup, and the rest re-run in the foreground. A harness that
    edits the real source needs a backup and a check after any kill.
  - **Size:** `native_cgroup.py` is 136 code lines, against ~110 planned and a look-again
    line of ~130. It was looked at twice, and the reason is recorded in the plan's §8.
- **Where to start if it breaks:**
  - **"limits not applied":** the server log gives the reason: no cgroup v2, no user bus,
    `busctl` failed, placement over 5 s, the layout failed, or a limit that wasn't applied
    or read back differently.
  - **A unit left behind, or the user manager `degraded`:** `systemctl --user list-units
    --all 'vestibule-*'`, then `systemctl --user reset-failed`.
  - The prototype in `~/m2spike/step4/` reproduces each §1 fact.

---

## M2 step 3 — Warden core: helper, jail, PID-1 hardening, kill path (2026-10-25)

- **Commit:** `1ef1b46`.
- **Plan:** [`docs/plans/M2/M2-step3-warden-core.md`](plans/M2/M2-step3-warden-core.md)
  S3-D1 to S3-D13 (v3, after two `skeptic` reviews). Contract amendments in M2 N1, N5, N7,
  N8, N9, §3 and §4.2.
- **What:**
  - **`native_helper.py`** is the stdlib-only program each run executes. It is started as
    `vestibule-warden -S -P -c …` with only `PYTHONHOME` in its environment, its stdout on
    `/dev/null` and its stderr on the server's log.
    - **helper:** user and PID namespaces, the identity maps, the status relay, and the stop
      path. It exits only once the sandbox is empty.
    - **init (PID 1):**
      - `setsid` and the remaining namespaces;
      - a non-recursive read-only tree, Docker's `/proc` masks, and a tmpfs `/sandbox` holding
        the script;
      - `lo` up, `pivot_root`, every privilege dropped, not dumpable;
      - its own checks, then `ready` and the guest.
    - **guest:** default signals, its own PATH search, `execve`.
  - **`native.py`** launches the helper (config in a memfd), collects output with M1's caps,
    stops overruns (SIGTERM to the helper, which SIGKILLs init through its pidfd), and
    computes the verdict.
    - The verdict re-checks the reported namespaces, identity maps and mount table against
      the server's own values.
    - Every run reports `isolation: none` until step 5's seccomp filter. A test-only bypass
      lets the tests run the sandbox at all.
  - **Not reachable from the server** until step 6.
  - **Measured in WSL:**
    - cold start ~40 ms (python), node 60–240 ms;
    - the stop takes 3–5 ms;
    - the Linux gate takes 174 s, +31 s for the new tests on two Pythons.
- **Files:**
  - Source: `backends/native_helper.py` and `backends/native.py` (new); `backends/base.py`
    (the isolation-value comment).
  - Tests: `test_native.py` (Linux, `native` marker), `test_native_verdict.py` (every
    platform), `nativeutil.py` (new).
  - Config and docs: `pyproject.toml` (`native` marker); `docs/GETTING_STARTED.md` (the marker
    and its `setup` prerequisite).
- **Guarded by:**
  - `test_native.py::test_what_the_guest_sees`: ids, capabilities, environment, fds,
    `/proc/1`, network.
  - `::test_the_mount_table_is_the_planned_one`, `::test_proc_is_masked_like_docker`.
  - `::test_a_run_past_its_timeout_is_stopped_with_nothing_left`: busy loop, self-`SIGSTOP`,
    closed output. It also checks that the helper exited by itself.
  - `::test_a_killed_server_leaves_no_sandbox`.
  - `::test_the_guest_gets_no_controlling_terminal`: run under `script`.
  - `::test_a_locked_workspace_mount_keeps_its_flags` and
    `::test_a_workspace_with_a_mount_inside_is_refused`: both run under `unshare -r -m`.
  - `::test_the_helpers_self_deadline_stops_a_run_the_server_forgot` and
    `::test_a_stalled_server_stderr_cannot_hold_the_self_deadline`.
  - `test_native_verdict.py::test_the_mount_table_rule` and
    `::test_a_hard_control_not_confirmed_claims_nothing`.
- **Broke along the way:**
  - **The prototype, before the plan** (WSL `~/m2spike/step3/`), found eight problems in the
    spike's design:
    - the `-I` launch can't find its prefix;
    - lazy imports after `pivot_root` (`ctypes._layout`; `os.execvpe` → `warnings`);
    - uv-3.11's `os.closerange` takes 246 ms;
    - SIGPIPE ignored in the guest, and a guest's SIGINT reaching PID 1;
    - `lo` down;
    - `/proc/keys` readable;
    - locked `noexec`/`ro` workspace mounts failing setup.
  - **`skeptic`, twice** (6, then 13 findings, all accepted). The high one: the guest
    inherited the server's terminal and could open `/dev/tty`. Also: the helper's stderr was
    the guest's, a wait status can't be decoded on Windows, and recursive binds let a mount
    inside the workspace in without `nosuid,nodev`.
  - **`critic`, twice** (4, then 2 findings; all confirmed by `verifier` and fixed, each with a
    test that failed first):
    - the self-deadline never fired;
    - a weak stdout/stderr test;
    - a mount rule that let missing masks through;
    - an untested note;
    - a stderr write that could block the self-deadline;
    - the size line.
  - **The mutation harness itself:** tests that run the backend in a child process (under
    `script` or `unshare`) let mutants survive at first, because only the pytest process was
    patched. A `sitecustomize.py` fixed it. All 14 mutants now turn their tests red.
  - **Size:** `native.py` reached 383 code lines against a ~380 look-again line. One fold, and
    it was recorded (plan §3).
- **Where to start if it breaks:**
  - **A setup failure:** the run's `isolation_detail` names the stage (`error` line), or the
    control the server couldn't confirm.
  - **A hang:** the helper's `_supervise` loop, then `NativeBackend._finish`.
  - The prototype in `~/m2spike/step3/` reproduces each §1 fact.

---

## M2 step 2 — Rootfs: registry client, unpacker, `vestibule-mcp setup` (2026-10-25)

- **Commit:** `2af2c0c`, `9197a55` (the step); review fixes and the Karpathy re-review
  `4c8afcf`.
- **Plan:** [`docs/plans/M2/M2-step2-rootfs.md`](plans/M2/M2-step2-rootfs.md) S2-D1 to S2-D10,
  with an amendment for the review fixes. Contract amendments in M2 N2, §6 and §7; M1 step 6's
  re-pin procedure now runs `setup` on Linux.
- **What:**
  - **`vestibule-mcp setup`** fetches both pinned images from the registry without Docker:
    an anonymous token in an unredirected header, and every blob's size and sha256 checked
    before anything parses it. Each image is unpacked into
    `~/.vestibule/rootfs/<index-hex>-<arch>/`, with a marker beside it, written last after
    `os.sync()`.
  - **The unpacker:**
    - walks every parent from the root fd with `O_NOFOLLOW` and uses leaf-only calls;
    - stores symlinks verbatim, and allows hardlinks only to regular files in the tree;
    - strips write and set-id bits (directories keep owner rwx only);
    - refuses whiteouts, devices, FIFOs, a directory replaced by a non-directory, and a
      missing parent (S2-D2).
  - **Setup also:**
    - adds `workspace/`, `sandbox/` and a localhost `etc/hosts`;
    - touches only its own names in the directory;
    - refuses an overlap with the workspace, a relative directory, and an unexpandable `~`.
  - `vestibule-mcp <anything else>` warns on stderr and still serves.
  - **Measured in WSL:**
    - python: 43.3 MB downloaded, 119.4 MB unpacked, in 3.8–4.0 s;
    - node: 79.8 MB downloaded, 227.0 MB unpacked, in 5.0–5.1 s;
    - 357 MB on disk; a second run takes 0.5 s and says "already present".
- **Files:**
  - Source: `registry.py`, `unpack.py`, `rootfs.py` (new); `config.py` (`rootfs_dir`);
    `server.py` (`run()` dispatch; the invalid-config log line moved to `_main`).
  - Tests: `test_registry.py`, `test_unpack.py`, `test_rootfs.py` (new); `test_config.py`.
- **Guarded by:**
  - `test_rootfs.py::test_our_tree_matches_docker_export` (`docker` + `network`): every
    entry of both trees against `docker export` of the same digest. It checks paths, types,
    symlink targets, contents, hardlink pairs and the mode formula.
  - `test_unpack.py::test_refused_entries_fail_and_touch_nothing_outside`: the attack
    fixtures, with sentinel and `st_nlink` checks.
  - `test_registry.py::test_the_token_never_reaches_the_cdn`,
    `::test_a_token_realm_must_use_the_registrys_scheme`.
  - `test_rootfs.py::test_setup_removes_only_its_own_leftovers`,
    `::test_a_tree_from_format_1_is_replaced`.
- **Broke along the way:**
  - **The plan's `skeptic` review** (13 findings, all accepted) reshaped it before any code.
    The main ones: cleanup scoped to setup's own names (high), hardlink linkname rules, the
    mount points, setup reading its own settings, and both images tested.
  - **Mutation checks** turned the guarding tests red for four broken unpackers: no
    `O_NOFOLLOW`, a raw-path hardlink, an unscoped sweep, and one that keeps setuid bits.
    - The setuid check passed falsely at first. The mutant set the mode before the file
      object's last buffered write, and Linux clears set-id bits on a write.
    - A flush added to the real unpacker at the time was removed by the Karpathy re-review
      (below): its mask already strips set-id bits, so the order can't matter.
  - **The first oracle run** fixed its expected differences:
    - Docker's container additions: `.dockerenv`, `dev/console`, `dev/pts`, `dev/shm`,
      `etc/mtab`;
    - the three files Docker rewrites when it creates a container: `etc/hostname`,
      `etc/hosts`, `etc/resolv.conf`;
    - ours: `workspace`, `sandbox`.
  - **mypy on both platforms:** Windows mypy doesn't narrow on `assert sys.platform !=
    "win32"`, so the Linux-only helpers use `if … raise`. The Linux run caught a variable
    reused with two types, which Windows couldn't see.
  - **`critic`, round 1** (2 medium + 4 low; all confirmed by `verifier`, the mediums lowered
    to low):
    - world-writable directories in the tree (the sticky bit was stripped, but not the write
      bits);
    - a `file:` or `http:` token realm accepted;
    - a sweep error ending in a traceback;
    - the server's config advice printed by `setup`;
    - a relative rootfs directory accepted;
    - the mismatched-marker path untested.

    All fixed.
  - **`critic`, round 2** (on the fixes; 2 medium + 3 low, all fixed):
    - existing caches kept the world-writable dirs, so the marker format went to 2 and old
      trees are replaced; verified on the real WSL cache;
    - an unexpandable `~` raised;
    - the plan's §4.2 example;
    - the sweep test failing when run as root;
    - the realm-scheme wiring untested.
  - **Karpathy re-review** (after `skeptic` and `critic` gained a Karpathy check): 8 low
    findings, all confirmed by `verifier`.
    - **Two tests could pass on broken code:**
      - "verify, then parse" was tested only for the index, not for the config blob;
      - S2-D10's modes weren't asserted.

      Both are fixed, and each is proved red by a mutant.
    - **Removed, because nothing asked for them:**
      - the defenceless `flush()`;
      - the "MB downloaded / unpacked" summary line (setup now says `ready`; the figures
        above were measured with it);
      - `Image.ref`;
      - the one-caller `_kind`;
      - an untested 404 message.
    - **Kept, with reasons:** the repository-name check, and the separate `manifest()` and
      `json_blob()`.
    - **Size:** `registry.py` is still about 200 code lines against ~150 planned. Most of it
      is input checks at the registry boundary, and it is now accepted in the plan (§3
      amendment).
- **Where to start if it breaks:** the oracle test, then `unpack.py` `_apply`'s per-type
  branches.

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
