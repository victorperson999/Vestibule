# M4 step 1 — run dirs left behind by a hard kill (F3)

Status: **v2, signed off 2026-11-08 with T1–T3 as written, and built the same day (§9).** v1 was reviewed by `skeptic` (14 findings, 0 high,
verdict "sound with amendments"). `verifier` checked the claims about existing code: 8 confirmed,
2 plausible, 0 refuted (§8). Every finding is taken below.

Contract: [`M4-adoption.md`](M4-adoption.md), F3 and acceptance criterion 8. It closes
`docs/BUILD_LOG.md`'s 2026-10-11 (S7-D1) row.

## 1. The problem

Each container run writes its script to a host dir mounted at `/sandbox` (M1 D9).
- **Windows:** `%TEMP%\vestibule-run-<random>`, directly (`container.py:275`).
- **POSIX:** `/tmp/vestibule-runs-<random>/vestibule-run-<random>`. Each backend makes one
  user-owned, non-sticky parent, so a run dir that Docker re-creates as root can still be
  deleted (`container.py:473–486`).

A normal run, a timeout and a cancel remove the dir, and an orderly shutdown normally does too.
(Windows can `os._exit` with cleanup still pending, `server.py:527–535`.) A hard kill
(`TerminateProcess`, SIGKILL, a Job Object closing) skips every path that would. The dir stays
for good, and on POSIX so does its parent. Each leftover is a small folder holding the code the
agent ran. The container itself ends on its own (S7-D5), and the container reaper handles
anything labeled.

BUILD_LOG records why a startup sweep was rejected: a dir's name couldn't say whether a live run
owned it. F3 fixes that by putting the deadline in the name, as S4-D3 did with the container's
label.

## 2. The fix

### 2.1 The names (two one-line changes)

- **Run dir:** `tempfile.mkdtemp(prefix=f"vestibule-run-{deadline_epoch}-", dir=...)`. It's the
  same `deadline_epoch` the container's label gets (`container.py:271`): spawn + timeout + 90 s,
  so the two can't drift apart.
- **POSIX parent:** `tempfile.mkdtemp(prefix=f"vestibule-runs-{os.getpid()}-")`.

`mkdtemp`'s random part uses only `[a-z0-9_]`, never a hyphen. So an old undated name like
`vestibule-run-abcd1234` can't be mistaken for a dated one.

### 2.2 The rules

| Thing | Removed when | Otherwise |
|---|---|---|
| A run dir | Its name has a deadline, `now > deadline + 60` (the reaper's `_REAP_MARGIN_S`), and it isn't one of this backend's own runs | Kept. No deadline or a garbled one is never removed |
| A POSIX parent | Its name has a pid, that pid is gone, and it's empty (`os.rmdir` is the emptiness check) | Kept. A reused pid only keeps it, the safe direction |

The two name patterns are strict `fullmatch`es with ASCII digits only:
`vestibule-run-([0-9]{1,12})-[a-z0-9_]+` and `vestibule-runs-([0-9]{1,9})-[a-z0-9_]+`. Nine
digits can't overflow `os.kill`'s pid argument.

### 2.3 The sweep

A plain function in `container.py`, with no backend state:

```python
def _sweep_run_dirs(root: str, now: float, keep: set[str], uid: int | None) -> None:
    """F3: remove what a hard-killed server left under `root`."""
    with os.scandir(root) as entries:
        found = [e for e in entries if _RUN_PARENT.fullmatch(e.name) or _RUN_DIR.fullmatch(e.name)]
    for entry in found:
        if not _real_dir_owned_by(entry.path, uid):
            continue
        parent = _RUN_PARENT.fullmatch(entry.name)
        if parent is None:
            _remove_if_stale(entry.path, entry.name, now, keep)
            continue
        with contextlib.suppress(OSError):  # a parent we can't read never ends the pass
            with os.scandir(entry.path) as children:
                names = [c.name for c in children]
            for name in names:
                _remove_if_stale(os.path.join(entry.path, name), name, now, keep)
            if _pid_gone(int(parent[1])):
                os.rmdir(entry.path)  # refuses, and so keeps, anything not empty
```

- **One function for both layouts, with no `os.name` branch.** Windows has dated dirs at the
  root and POSIX has parents, so each platform simply never finds the other's shape. The tests
  run the layouts on both, with the pid rule stood in on Windows (§5).
- **`_real_dir_owned_by`** does an `os.lstat`, and returns `False` on `OSError` (the entry
  vanished). It then requires a real directory, no symlink or reparse point
  (`workspace._is_symlink_or_reparse`, reused), and, when `uid` isn't `None`, `st_uid == uid`.
  `/tmp` is shared, so another user could plant `vestibule-runs-1-x` as a link to somewhere
  else. The check runs before `os.scandir` would follow it.
- **A child of a parent gets no owner check on purpose.** A run dir Docker re-created is owned
  by root, and that's what the parent exists to let us delete (`container.py:476`). The parent
  is ours, mode 0700, so nobody else can put anything in it.
- **`_remove_if_stale`** checks `_run_dir_stale`, logs `sweep: removing stale run dir <name>` at
  INFO, and calls `shutil.rmtree(path, ignore_errors=True)`. `rmtree` refuses a symlink or
  junction at the top and doesn't descend into one. A dir it can't remove yet is retried by the
  next sweep.
- **`_pid_gone`** is `os.kill(pid, 0)`: `ProcessLookupError` means gone, and any other outcome
  means not gone.
  - **It returns `False` at once when `os.name != "posix"`.** On Windows `os.kill(pid, 0)` isn't
    a probe: signal 0 is `CTRL_C_EVENT`, so it would send a real Ctrl+C.

### 2.4 When it runs

As the first step of `_reap_orphans`, before its `ps`. So it runs at first use and after each
run, like the reaper (`container.py:245`, `:292`), and it also runs when the daemon is hung or
no labeled container exists, which is the usual state after a hard kill (`container.py:661`).

```python
async def _sweep_stale_run_dirs(self) -> None:
    if self._closing:  # shutdown: housekeeping can wait for next time
        return
    keep = {os.path.basename(r.tmpdir) for r in self._runs.values()}
    uid = getattr(os, "getuid", lambda: None)()
    try:
        await asyncio.to_thread(
            lambda: _sweep_run_dirs(tempfile.gettempdir(), time.time(), keep, uid)
        )
    except Exception:  # detached task; a failed sweep must not skip the reaper's pass
        log.exception("sweep: pass failed")
```

- **The root is `tempfile.gettempdir()`,** evaluated in the thread, because `mkdtemp` makes run
  dirs there.
- **`_schedule_reap` already limits it to one in flight, and `pending_tasks()` already lists
  `_reap_task`,** so `drain()`, `idle()` and `aclose()` see it with no new wiring.
- **Shutdown:** on POSIX the hand-off exits at once; a thread mid-`rmtree` dies, and a later
  sweep finishes the job. On Windows `aclose()` waits within its existing budget, then exits.

**This reads F3's "on its own" narrowly** (the contract's reason is only the reaper's early
return). The plan adds a dated amendment note to F3 saying the sweep is the reaper task's first
step, ahead of that return.

**Cost,** measured on this machine: one pass over the real `%TEMP%` (1,723 entries) takes about
2 ms, and over 100,000 entries about 55 ms, in a thread. So there's no time budget and no rate
limit.

## 3. Choices for sign-off

- **T1 — Undated dirs are skipped silently.** The reaper warns about an unlabeled container, but
  F3 keeps pre-M4 leftovers forever, so a warning here would repeat after every run for good.
- **T2 — Leftovers from before this step are not cleaned.** That's F3's rule: no deadline, never
  removed. BUILD_LOG records a one-time note that they can be deleted by hand. This PC has none
  in `%TEMP%` today.
- **T3 — A host running only native isolation never sweeps.** The sweep lives in the container
  backend. On Linux, once selection picks full native, no `ContainerBackend` is built, so a
  leftover from an earlier container run stays while native keeps being selected.

## 4. The alternative, weighed

`skeptic` proposed one fixed per-user folder, such as `~/.vestibule/runs`, with dated run dirs
inside and no per-backend parent.

- **What it would remove:** the shared-`/tmp` concerns (the owner and link checks), the pid
  rule and its Ctrl+C guard, the parent's lifecycle code, and §5's residual.
- **What it would cost:**
  - it changes F3, which Victor signed, and moves every container run's script on every platform
    after the spike validated today's layout;
  - it needs a new `VESTIBULE_RUNS_DIR`-style setting, so the tests stay off the real folder,
    and a new entry in `config.py`'s overlap check, because a workspace that overlaps it would
    let one guest reach other runs' scripts;
  - a root-squashed NFS home with a custom workspace works today, and would stop working;
  - `_schedule_reap` has no `limits`, so the root needs plumbing, and the lifecycle tests that
    stub it change.
- **Recommendation:** keep F3 as signed. The extra code is about 25 lines in one function, and
  the change to today's behaviour is two name prefixes. Victor can overrule this at sign-off.

## 5. Edge cases

| Case | What happens |
|---|---|
| A live run in another server | Its deadline is in the future, so it's kept |
| An overdue run on this backend (a clock jump, a suspended laptop) | `keep` protects it |
| An overdue run on another backend or server | Removed. S4-D3's rule is the same (past the deadline, even with a live owner). Its accepted risk didn't name a suspended laptop, so this is a small extension. The guest already has the script open, and the self-deadline ends it |
| Two servers sweep at once | `rmtree(ignore_errors=True)` and `_real_dir_owned_by` tolerate the race |
| The clock goes backward | Dirs live longer, the safe direction |
| A hard-killed run whose container starts late and re-creates the dir, parent still there | Empty, same dated name, and swept after its deadline |
| **Residual:** the same, but a sweep removed the empty dead-pid parent first | Docker re-creates both levels as root in sticky `/tmp`, and the user can't delete them. Two empty folders until reboot. It takes all of: a create still pending when the finisher removed the dir (`verifier` measured this as impossible past about 15 ms on a healthy daemon, so a loaded or waking one), a SIGKILL to the server alone, and another server's sweep inside that window. Stated, not fixed |
| A pre-M4 parent (`vestibule-runs-<random>`) | Doesn't match the pattern and is never entered |
| A run dir that can't be removed yet (a Docker Desktop handle) | The next sweep retries |
| `TMPDIR`/`TEMP` differs between servers | Each sweeps its own root, where its own dirs are |
| A dead-NFS `/tmp` | The thread hangs, as `_cleanup`'s `rmtree` already would |

## 6. Tests (red first)

**`tests/test_run_dir_sweep.py`, new, Docker-free.** Pure rules take `now` as an argument. Tests
3–5 patch `_pid_gone`, so they run on both platforms and can never send a real signal.

1. `_run_dir_stale`, a table: past the margin; exactly at the margin (kept); one second past;
   future; named in `keep`; an old undated name; garbled names (letters in the digits, empty
   digits, non-ASCII digits, a trailing newline, 13 digits); a parent's name.
2. **Flat layout:** a mix under a temp root; only the stale dir goes.
3. **POSIX layout:** a stale child goes, and its now-empty dead-pid parent goes in the same pass.
4. A dead-pid parent stays while it holds a live child, an undated child, or a file.
5. A live-pid parent stays, even empty, and its stale child still goes.
6. An undated parent is never entered: a stale-named child inside survives.
7. Another owner's dir or parent is skipped (`uid` = the entry's `st_uid` + 1).
8. A link named like a parent isn't followed, and a stale-named child in the outside folder
   survives. It's a symlink on POSIX and a junction (`mklink /J`) on Windows, as a branch inside
   one test, so neither platform skips.
9. A file named like a run dir stays.
10. POSIX only: a parent we can't read (`chmod 000`) doesn't end the pass; a later entry is still
    swept.
11. `_pid_gone`, with a stand-in `os` in the module: POSIX with `ProcessLookupError`,
    `PermissionError`, success; and `nt`, where `kill` must never be called.
12. POSIX only: the real `_pid_gone`: its own pid isn't gone, and a child it has reaped is.
13. An entry that vanishes after the listing doesn't end the pass.

**Wiring, a real `_reap_orphans` with a fake runtime and `tempfile.tempdir` patched** (a pure
test can't catch a dropped hand-off):

14. A run's `/sandbox` source has the same deadline as its `vestibule.deadline` label.
15. POSIX only: the parent's name carries `os.getpid()`.
16. The sweep runs at first use and after the run, with the fake `ps` listing no containers. The
    fake run is held until the first pass is done, so the one-in-flight guard can't merge them.
17. It runs off the loop's thread.
18. No sweep starts after `begin_close()`.
19. A sweep that raises is logged, and the same pass still calls `ps`.
20. `keep` holds this backend's live run names.

**Real Docker:**

21. `test_a_hard_killed_servers_run_dir_is_swept_by_the_next_server` (`tests/test_stdio_e2e.py`).
    Server A, with `TMPDIR`/`TEMP`/`TMP` on a test-owned folder and `VESTIBULE_BACKEND=container`,
    starts a long run and is killed hard. Asserted: exactly one dated run dir is left, and on
    POSIX its parent has A's pid. The test removes the container and renames the dir so its
    deadline is an hour old. Server B starts on the same folder with
    `VESTIBULE_IMAGE_PYTHON` set to a missing image, so its call answers `Blocked:` and it never
    creates a container. Asserted: `Blocked:`, the dir is gone, and on POSIX so is the parent.
22. POSIX only: `test_a_root_owned_run_dir_in_our_parent_is_swept` (`tests/test_container.py`).
    `docker run -v parent:/p … mkdir /p/vestibule-run-1-abc` makes a root-owned child, and the
    sweep with the real uid removes it.

**Mutants** (each red on its named test, after a green control):

| Mutant | Caught by |
|---|---|
| `>` becomes `>=` | 1 |
| The margin dropped | 1 |
| The pattern loosened to accept undated names | 1, 2 |
| `keep` ignored | 1, 20 |
| Owner check dropped | 7 |
| POSIX: `os.lstat` becomes `os.stat` | 8 |
| Windows: the reparse check dropped | 8 |
| `os.rmdir` becomes `rmtree` | 4 |
| Parent removed before its children | 3 |
| `_pid_gone` always `True` | 12 |
| Windows guard dropped | 11 |
| `suppress(OSError)` dropped | 10 |
| `_real_dir_owned_by` lets `OSError` out | 13 |
| The sweep moved after the reaper's early return | 16, 21 |
| The scan called on the loop | 17 |
| The `_closing` check dropped | 18 |
| The `except` dropped | 19 |
| The name's deadline differs from the label's | 14 |
| An owner check added to children | 22 |

## 7. Changes

| File | Change |
|---|---|
| `src/vestibule/backends/container.py` | The two prefixes; `_RUN_DIR`, `_RUN_PARENT`, `_run_dir_stale`, `_pid_gone`, `_real_dir_owned_by`, `_remove_if_stale`, `_sweep_run_dirs`; `_sweep_stale_run_dirs`; one line at the top of `_reap_orphans`; the module docstring's step note |
| `tests/test_run_dir_sweep.py` | New: tests 1–20 |
| `tests/test_stdio_e2e.py`, `tests/test_container.py` | Tests 21 and 22 |
| `docs/plans/M4/M4-adoption.md` | A dated amendment note on F3 (§2.4) |
| `docs/BUILD_LOG.md` | The S7-D1 row closed, with T2's note and §5's residual; the entry; the step 0 amendments' hash (`f5b3659`) |
| `docs/HISTORY.md` | Sign-off and done bullets |
| this plan | The as-built |

No README or ARCHITECTURE change: neither mentions run dirs. For step 4's `SECURITY.md`: a guest
can read the `/sandbox` source path in `/proc/self/mountinfo`, as it already can for host paths,
and the new names add the deadline and, on POSIX, the server's pid. It's information only.

**Size:** src about 80 lines, tests about 400. (The contract guessed 80 and 120. The tests are
higher because of the fake-runtime harness and test 21.)

**Build order, each with its check:**
1. Tests 14, 15, 16 and 21 → red on today's code, for the stated reasons (undated names, no
   sweep).
2. The names → 14 and 15 green, and `test_shutdown.py` and `test_container.py` still green.
3. The pure sweep → 1–13 green on Windows and WSL.
4. The wiring → 16–20, and 21 and 22 with real Docker.
5. The mutants → each red on its test.
6. The gate, `critic`, `verifier`, the docs. Then Victor commits.

## 8. Review record

| # | Severity (after `verifier`) | Finding | v2 |
|---|---|---|---|
| 1 | med | The pid rule can't run on Windows as tests 3–5 said; a real `_pid_gone` there would send Ctrl+C | §6: tests 3–5 patch it; test 12 is the POSIX real one; test 7 takes the uid from `lstat` |
| 2 | low | The link-check mutant can't go red on a symlink; only a junction reaches it | §6: test 8 uses a junction on Windows; the POSIX mutant is `lstat` → `stat` |
| 3 | low (race plausible, missing image confirmed) | Test 21 doesn't control "no containers present" | §6: server B has a missing image, so it answers `Blocked:` and makes no container |
| 4 | med | A simpler layout wasn't weighed | §4: weighed and recommended against; Victor's call |
| 5 | low | Sizes too low | §7 restated |
| 6 | low | Test 14 races the one-in-flight guard | §6 test 16 holds the fake run |
| 7 | low | A vanishing entry could end the pass | §2.3 and test 13 |
| 8 | low (plausible) | An empty parent removed, then re-created as root | §5: a stated residual, with the measured window |
| 9 | low | The child-without-owner-check choice had no test | §6 test 22 |
| 10 | low | The sweep root and test isolation unnamed | §2.4 and §6 |
| 11 | low | `keep` is per backend, not per server | §5 reworded |
| 12 | low | T3 understated | §3 reworded |
| 13 | low | Run it at the top of `_reap_orphans`; the count and gone-check exist only for a log line | §2.3 and §2.4: taken. The `_closing` check and the sweep's own `except` are kept, and F3 gets a note |
| 14 | info | The new name shows the pid to a guest | §7's line for step 4 |

`verifier` also corrected three statements, fixed in §1 and §5: "an orderly shutdown" removes the
dir only normally; the startup-sweep rejection is recorded in BUILD_LOG, not in M1 S7-D1's plan;
and S4-D3 accepted past-deadline removal but didn't name a suspended laptop.

## 9. As built (2026-11-08)

**Built as §2 and §6 say, with these changes from the diff review.** `critic` found no defect in
the code. It found 5 issues in the tests and the size, and `verifier` confirmed all 5:
- **`keep` had no test through the parent branch,** the only one POSIX runs use. New:
  `test_keep_protects_an_overdue_run_inside_a_parent`.
- **`uid` was recorded but never asserted,** so the owner check could be unwired with the suite
  green. Test 20 now asserts it.
- **Test 9 proved only the outcome,** because `rmtree` refuses a file anyway. It now also asserts
  no false "removing" log line.
- **`rglob` can raise on Python 3.11** if a dir vanishes mid-scan, so test 21's polling uses
  `os.walk`. `verifier` measured the chance per poll as 1 in 1,000 or less.
- **`raising=False`** came out of the tests' patches, now that the names exist.

These fixes touched only the findings' lines, all in tests, so `critic` didn't run again.

**Red first:** on the old code, tests 14 and 15 failed on the undated names, 16 because no sweep
was made, and 21 with "never happened: one dated run dir".

**Mutants:** 25 of 25 red on their named tests after a green control, on Windows or in WSL as each
one needs. That's §6's 19, with the moved sweep run against tests 16 and 21 separately, plus 5
more: the parent's pid dropped, `keep` not snapshotted, `keep` ignored in the parent branch, `uid`
not passed, and the directory check dropped. Before the fixes, the last three survived the
whole suite.

**Gate** (the final tree, before this docs pass; `tests/test_docs.py` re-run after it):
- check_env, ruff and mypy clean on both platforms.
- Linux (367 s of 540): 3.14 full suite 863 passed, 0 skipped; 3.11 Docker-free 816; stdio 6.
- Windows: 681 passed, plus 162 POSIX/Linux-only skips.

Test 22 really ran on Linux: Docker made the child as root, so its skip condition was false.

**Size:** src +98 lines against about 80, tests about 528 against about 400.
The src is over because of the comments on the safety rules. The tests are over because of the
fake runtime, the parametrized tables, and the review's added cases.
