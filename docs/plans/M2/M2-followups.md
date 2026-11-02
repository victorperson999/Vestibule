# M2 follow-ups — four small fixes before M3

Status: **v2, signed off by Victor 2026-11-01**, after the `skeptic` review; `verifier`
confirmed all seven findings (§6). **F-D1: option 1**, every isolating backend refuses a root
server (§3).

M2 is accepted (`f318e0c`). Its last review and gate left four small items open
(`docs/BUILD_LOG.md`, known issues and the 7a entry). This step fixes them before the M3
plan starts, so M3's gate runs don't fail at random and every result keeps golden rule 5.

## 1. Scope

| # | What | Kind |
|---|---|---|
| F1 | `test_a_hard_nproc_limit_below_pids_max_still_runs` depends on how many tasks your uid has | test only |
| F2 | Nothing refuses a server started as root: its guest is then host uid 0, under native **and** under the container, and the result still says `native` or `container` | `src/` |
| F3 | Two `run_code` results have no `isolation:` line: the outer deadline's, and the one for an unexpected error | `src/` |
| F4 | Two stale doc lines | docs |

**Not in scope:**
- the liveness lease for Windows shutdown, a later milestone;
- the SDK's answer to a cancelled call, which is upstream;
- the `mcp<2` move, before M4's PyPI release;
- the non-blocking stderr handler, workspace quotas and leftover `%TEMP%\vestibule-run-*` dirs.
  BUILD_LOG marks all three as M3 candidates, so the M3 plan takes them up or says why not.

## 2. The fixes

### F1 — the nproc test stops depending on your task count

**Problem.** The test runs the server-side code under `ulimit -u 100`. Since Linux 5.14,
`RLIMIT_NPROC` is checked at every user-namespace level up to the host's. The host level uses
the soft limit the helper had when it created its user namespace, and there every task of the
uid counts, threads included. On WSL2 that includes tasks in other distros, which `ps` can't
see. In 7b the kernel counted about 102, so every fork failed. After one Claude Code session
closed it counted about 76, and the test passed. (`verifier` measured both effects in WSL: one
process with 301 threads raised the count by 301, and a soft limit of 20 at `unshare` time
blocked a later fork after the limit was raised.)

**What the test is for** (its docstring, critic finding 1 of step 4): when the hard limit is
below `pids_max`, the backstop clamps to the hard limit instead of failing the run (S4-D3).
That needs a hard limit below `pids_max`. It doesn't need a *small* hard limit.

**Fix** (`tests/test_native.py` only; nothing in `src/`):
- **`n`.** If the current hard `RLIMIT_NPROC` is finite, `n` is that limit, uncapped. It is
  never higher than the current limit, so `ulimit` can't fail. If the limit is unlimited
  (`RLIM_INFINITY`, which is `-1` in Python on Linux, so `min()` alone would be wrong), `n` is
  `/proc/sys/kernel/threads-max`, the system-wide task cap that no uid can exceed.
- The test runs the runner under `ulimit -u n`, as now.
- The runner builds `Limits(workspace_dir=..., pids_max=n + 1)` from a new `argv`
  argument, so the hard limit is again below `pids_max` and the clamp is still what
  the test checks. With no bus, nothing else reads `pids_max`.
- The docstring gains one sentence: the limit is the uid's own hard limit (or the system's
  task cap), so the test fails only where the user's own shell couldn't fork either.

The expected result is unchanged: exit 0, `ran`, detail `ALL_LIMITS + " — no systemd user
bus"`.

**Verify:**
1. **Reproduce first.** In WSL, raise the uid's task count above 100 by starting about 40
   `sleep 600` processes. The current test goes red, and the new one is green under the same
   load. Then kill the sleepers.
2. **Mutant.** In `native_helper.py`, set `n = cfg["nproc"]` with no clamp. `setrlimit(n + 1)`
   above the hard limit then fails at init's `privileges` stage, so the run reports `none` and
   the new test goes red. Restore the original afterwards and diff it against a backup.
3. The gate.

**Edge case:** the uid already has more tasks than its own hard limit. The user's own shell
can't fork then either; nothing the test can fix.

### F2 — a server started as root runs no code

**Problem.** A root server passes its own uid to both isolating backends:
- **Native.** The helper writes the identity map from its own uid
  (`native_helper.py:110–115`), so the map is `0 0 1` and the guest is host uid 0. The verdict
  accepts it (`native.py:475`), so the result says `isolation: native`, although contract N6
  defines the guest as "not root".
- **Container.** `_container_user()` (`container.py:98–105`) returns `getuid():getgid()`, so the
  run gets `--user 0:0`. Nothing checks the uid (`isolation_status()`, the probe), so the result
  says `isolation: container`. M1's signed-off D3 puts "non-root `--user`" in the hard tier,
  where anything missing means `Blocked:` (`docs/plans/M1/M1-container-backend.md:47`), and
  `README.md:49` promises a non-root user.

`verifier` reproduced both. This is one server-wide problem.

**Fix (the recommended F-D1 option, §3).**

1. **One check in `BackendSelector._select`** (`select.py:367`), after the naive branch and the
   unknown-backend check and before any backend is built:

   ```python
   if sys.platform != "win32" and 0 in (os.getuid(), os.geteuid()):
       raise _RestartRequired(<the message below>)
   ```

   - **Message**, roughly: "the server runs as root, so the code it runs would be root too;
     Vestibule never needs root. Start it as your own user (on a WSL distro with only root,
     create one with `adduser`), then restart your agent."
   - **Covers `auto`, `native` and `container`**, on Linux and macOS. On Windows there is no
     uid to check, and the container always runs as `1000:1000` inside Docker Desktop's VM
     (`_container_user()`).
   - **Real uid as well as effective uid.** Native's identity map and the container's
     `--user` both copy the *real* uid. The effective one is what makes the process privileged.
   - **Final, with no 30-second note.** The check runs before either backend is tried. So the
     refusal is `_RestartRequired` on its own, and its cached copy never adds "re-checked every
     30 s" (`select.py:239–246`).
   - **The guard.** mypy on Windows accepts `sys.platform != "win32" and …os.geteuid()…` and
     rejects a bare `os.geteuid()` (checked 2026-11-01).
   - **The explicit naive backend still runs** (`VESTIBULE_BACKEND=naive`). It's a dev-only
     opt-in that already reports `isolation: none` and claims nothing, so a root server
     breaks no promise there.
2. **Native's per-run verdict refuses a root guest too.** In `_verdict` (`native.py:475`) the
   identity-map condition also requires `uid != 0`. Contract N5 checks the hard tier per run,
   not only at selection, and N6 defines the identity map as "not root". A `NativeBackend` built
   outside the selector then reports `none` with "hard controls not confirmed: identity map".
   The only such backends today are in tests, but the verdict is where the contract puts the
   rule. The container has no per-run verdict for its hard tier, which is the command line
   itself, so the selector check is its only guard.

**Verify:**
- **`tests/test_select.py`, Docker-free; skipped on Windows, which has no uid.**
  - Parametrized over `backend` in `auto`, `native` and `container`, and over the patched
    function, `os.getuid` or `os.geteuid` returning 0.
  - `select_mod._version_check` and `select_mod.NativeBackend` are replaced by stand-ins that
    fail the test if touched.
  - `get()` raises `_RestartRequired` with this message's own words ("runs as root"), not
    test-name words (the step-6 lesson).
  - A second `get()` raises the same message, without "re-checked" (finding 5).
- **The same file: naive as root.** With root patched, `backend="naive"` is still selected,
  `none`. This pins where the check sits.
- **`tests/test_native_verdict.py`: the verdict.** `uid=0, gid=0` with maps `0 0 1` and the
  file's good status document gives `("none", "hard controls not confirmed: identity map")`.
  It's a pure function, so it runs on every platform.
- **Mutants.**
  - Delete the selector check: a stand-in is touched and every parametrized case goes red,
    whatever the host has.
  - Delete `uid != 0`: the verdict test goes red.
- **The gate**, both platforms; mypy on Windows checks the guard.

**Edge cases:**
- **A server that is uid 0 only inside a user namespace** (`unshare -r`, a rootless container).
  It's refused too, and the message is true in that namespace. Neither backend works well
  there anyway: native gets a hidden `/proc` (`select.py:657`), and a container can't bind the
  server's paths unless the daemon shares them. This is accepted.
- **Primary gid 0 with a non-root uid.** Unusual, and not the known issue. Out of scope.

### F3 — every `run_code` result after validation has an `isolation:` line

**Problem.** Two results of `_handle_run_code` have none. Both were checked by `verifier`:
- **The outer deadline** (`timeout_s + 30`, `server.py:201–204`) returns only `Execution
  failed: sandbox did not return in time.` It was noted in 7a's BUILD_LOG entry.
- **Any other exception** from selection or from `warden.run` reaches `call_tool`'s catch-all
  (`server.py:153–155`) and returns `Internal error: {e}`. Native's `_collect` re-raises
  whatever it hits after the guest ran (`native.py:255–257`).

Golden rule 5 says every `run_code` result includes an `isolation:` field. (A `Blocked:`
message is a refusal before anything ran, as now.)

**Fix.** Both paths add the same second line, kept as one module constant:

```
isolation: none (the sandbox did not report back; no isolation is claimed for this run)
```

- The deadline text stays `Execution failed: sandbox did not return in time.`
- A new `except Exception as e:` after the two existing ones in `_handle_run_code` logs with
  `log.exception` as `call_tool` does and returns `Internal error: {e}` plus the line.
  `CancelledError` is a `BaseException`, so a client cancel still passes through untouched.
- **Why `none`.** Nothing confirmed what protected the run. The detail reuses the native
  verdict's own words for a sandbox that sent no report (`native.py:460`). Under-claiming is the
  safe direction (golden rule 5).
- **What doesn't change.** Neither path calls `SELECTOR.note_result`, so the cached selection
  isn't dropped. That would be a behavior change nothing asks for. `call_tool`'s catch-all
  stays for `read_workspace` and anything outside `_handle_run_code`.

**Verify** (`tests/test_select.py`, next to the existing `fake_get_warden` handler tests at
lines 379–406):
- **A fake warden whose `run` raises `TimeoutError`.** It goes through the same `except
  TimeoutError` branch the real deadline uses (`asyncio.timeout` re-raises it unchanged), and
  the text ends with the new line. The real deadline would cost at least 31 s per test.
- **A fake warden whose `run` raises `OSError`.** The text is `Internal error: …` plus the new
  line.
- **Mutants.** Drop the line from either path, and that test is red.

### F4 — two stale doc lines

- **`docs/GETTING_STARTED.md` "Run the checks".** One bullet after `tests/test_acceptance.py`'s:
  `tests/test_native_acceptance.py` is the M2 exam, criteria 1–13 through the real tool
  handler with real native sandboxes. It runs on Linux and needs what the native-marked tests
  need.
- **The status line of `docs/plans/M2/M2-step5-seccomp.md`.** It still reads "draft (v2) …
  pending Victor's build sign-off". It becomes the built state, with its date and commit
  (`06bd899`) as HISTORY records them.

## 3. Decision for Victor

**F-D1 — what a server started as root gets.** v1 recommended refusing native only, with
`auto` falling back to the container. The review showed that only moves the false claim to the
container (§6, finding 1), so that option is gone.

- **(1) Every isolating backend refuses. Recommended.**
  - **It keeps two signed-off promises:** native's "not root" (N6, golden rule 2) and the
    container's non-root `--user` (M1 D3, `README.md:49`).
  - **It's one check, made before anything is spawned**, plus the one verdict condition.
  - **The cost:**
    - a WSL distro with only root needs `adduser`;
    - an image that runs Vestibule as root needs a `USER` line.
    - Dev containers are mostly unaffected. Many run as a non-root user by default, and one
      that drives the host's Docker can't use the container backend anyway, because its paths
      aren't the host's.
- **(2) Run as root and say so.** Both backends report `-degraded`, with "the server runs as
  root, so this code ran as root".
  - It keeps root-only setups working.
  - It needs more:
    - a uid-0 condition in both verdicts;
    - an amendment to M1's D3, whose hard tier lists the non-root `--user`;
    - a README change.
  - Every root run would carry the label.

## 4. Files

| File | Change | Size |
|---|---|---|
| `src/vestibule/backends/select.py` | F2: the root check in `_select`; `import os` | ~8 lines |
| `src/vestibule/backends/native.py` | F2: `uid != 0` in `_verdict`'s identity-map condition | 1 line |
| `src/vestibule/server.py` | F3: the shared line, the deadline text, `except Exception` | ~8 lines |
| `tests/test_native.py` | F1: the limit from the hard limit or `threads-max`; `pids_max` passed to the runner | ~10 lines changed |
| `tests/test_select.py` | F2: the parametrized refusal test and the naive test; F3: two handler tests | ~60 lines |
| `tests/test_native_verdict.py` | F2: one verdict test | ~6 lines |
| `docs/GETTING_STARTED.md`, `docs/plans/M2/M2-step5-seccomp.md` | F4 | 2 spots |
| README, ARCHITECTURE, BUILD_LOG, HISTORY | §5 | after the build |

## 5. Order of work and checks

1. F1. Reproduce under load, change the test, see it green under the same load, then the
   mutant.
2. F2. The tests first; they're red without the change. Then the check and the verdict
   condition, then both mutants.
3. F3. The two tests first (red), then the line on both paths.
4. F4.
5. Self-check the diff against `.claude/KARPATHY_EXAMPLES.md`.
6. `gate`: Windows, then Linux.
7. `critic`, then `verifier`. This step touches `src/`.
8. Docs:
   - **README.**
     - Line 120's "unless you start the server as root, which Vestibule never needs" becomes
       "never root: a server started as root refuses to run code".
     - One sentence in the setup section: run Vestibule as your own user, since started as
       root it refuses to run code.
     - `README.md:49`'s non-root promise stays as it is; it's now enforced.
   - **`ARCHITECTURE.md:177`.** The same qualification, reworded the same way.
   - **BUILD_LOG.**
     - The root-server known issue moves to Closed, widened to the container side.
     - The nproc-test known issue moves to Closed.
     - The 7a entry's "noticed" line points to this step.
     - A new status row and entry.
   - **HISTORY.** The F-D1 sign-off bullet and the done bullet.
9. The staging list for Victor. It includes the 7b hash fills already made in BUILD_LOG and
   HISTORY (`f318e0c`).

## 6. Review (`skeptic`, then `verifier`, 2026-11-01)

`skeptic`'s verdict was "sound with amendments", with F-D1 to be re-posed. `verifier` confirmed
all seven findings, with no refutations (6 confirmed; 4b plausible), and adjusted three
severities. Every finding is taken.

| # | Sev. (verifier) | Finding | Done in v2 |
|---|---|---|---|
| 1 | high → med | v1's option, native-only refusal, moved the false claim to the container: a root server gets `--user 0:0` and `isolation: container`, against M1 D3 and `README.md:49` | F-D1 re-posed (§3); the check is now server-wide (F2) |
| 2 | med | v1's `auto` test couldn't show the property: patching `geteuid` leaves the container's `getuid` real, and patching `getuid` fails the probe for another reason (`/sandbox` is 0700). On a host without delegation, it stays green with the check deleted | Docker-free stand-in tests that fail if any backend is touched |
| 3 | med → low | The 4096 cap only raised the threshold: `RLIMIT_NPROC` counts threads, so a busy desktop can pass 4096 | `n` is the uncapped hard limit, or `threads-max` if unlimited (F1) |
| 4 | low | (a) Docker Desktop on this host has no AppArmor/SELinux, so "the container adds an LSM profile" depends on the host. (b) A docker-outside-of-docker dev container can't use the container backend anyway | (a) the claim is dropped; (b) §3 states the real cost of option 1 |
| 5 | low | Under `auto`, a native-only `_RestartRequired` next to a daemon-down container got the 30-second note | The check now comes before both backends; a test asserts there's no note |
| 6 | low | An unexpected exception from `warden.run` returned `Internal error: …` with no `isolation:` line | F3 covers that path too |
| 7 | low | `_verdict` accepted a `0 0 1` map as `native`, although N5 checks the hard tier per run and N6 says "not root" | `uid != 0` in the verdict (F2.2) |

`verifier`'s correction: the plan's v1 called the container's function `_user()`; its name is
`_container_user()`.

## 7. As built (2026-11-01)

The first gate run failed on both platforms. Each failure came from a case the plan missed,
and each fix makes the change smaller.

- **The guard is `getattr(os, "getuid", None)`, not `sys.platform != "win32"`.** 26
  `test_select.py` tests failed on Windows. To act as Linux, they replace `select.py`'s own
  `sys` with a stand-in whose platform is `"linux"` (`_on`, `test_select.py:907`). The guard
  then let `os.getuid()` run on Windows, which has no such function. The `getattr` form is the
  idiom `_container_user()` already uses for the same reason. It doesn't depend on
  `sys.platform`, and mypy accepts it on Windows. Same behavior on every real platform.
- **F2.2, `uid != 0` in native's verdict, is dropped.** Two Linux tests failed:
  `test_a_locked_workspace_mount_keeps_its_flags[noexec, read-only]`.
  - **Why it fails.** The tests run `NativeBackend` as uid 0 inside an `unshare -r` user
    namespace, which maps that uid 0 to the user's own uid 1000 on the host. That's how
    they get mounts that arrive locked. The guest there isn't host root, but `uid != 0`
    can't tell uid 0 in a nested namespace from host root, so the verdict said `none`.
  - **Why drop it, not refine it.** A precise check (uid 0 *and* the initial user namespace)
    would add code for callers that exist only in tests. The selector's check already
    covers every path the server takes.
  - **What else changes.** §6's finding 7 is now handled by the selector check alone, and
    its verdict test is removed with it.
  - **Edge case corrected.** §2 says native rarely builds where the server is uid 0 inside a
    user namespace. In fact it builds fine under `unshare -r`, as these tests show. The selector
    still refuses that case (the message is true there). A server run that way is rare, so this
    stays accepted.

**Checks done:**
- **F1.** The old test went red with 40 extra sleeping tasks (`[Errno 11]` starting the
  helper), and the new one stayed green under the same load. The no-clamp mutant failed
  "at privileges: not allowed to raise maximum limit".
- **F2, F3.** All 9 new tests were red before the `src/` change, each for its intended reason:
  - the stand-ins were touched;
  - the deadline text had no `isolation:` line;
  - the `OSError` escaped.
- **Mutants.** All red: the selector check deleted (6 red), the deadline line dropped, and the
  `except Exception` path dropped. Every file was restored byte-identical.

**Review:** `critic` found one low issue, and `verifier` confirmed it. The root check reads the
real uid, so the Docker-free tests in `test_select.py` depended on who runs pytest:
- **As real root in WSL,** 55 of them failed on the root refusal. All of them passed as root
  on HEAD.
- **Fix:** an autouse fixture replaces `select.py`'s own `os` with a stand-in holding an
  ordinary uid, as `_on` does for `sys`. Nothing global is patched: a patched `os.getuid` would
  also reach pytest's own check that its temp dir belongs to the current user. Afterwards, as
  real root, the file passed 115 of 115.
- **What stays root-dependent.** The tests that select a real backend now fail as root, by
  design (`test_native_select.py`, the acceptance suites, the Docker selection tests in
  `test_container.py`, `test_stdio_e2e.py`). GETTING_STARTED's "Run the checks" now says to run
  them as your own user.
