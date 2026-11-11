# M4 step 5 — the first runs, the release, and "M4 accepted"

Status: **v2, after the `skeptic` review (§13). Signed off by Victor 2026-11-10, with option (1)
on each of S5-D1 to S5-D5. The sign-off starts 5a. 5b, the upload, waits for his separate go.**

Contract: [`M4-adoption.md`](M4-adoption.md) P6, §3 to §5, and step 5 of §6. The checklist is
`docs/RELEASE.md`. This is the step where something is published, and **an upload to PyPI can't
be taken back.** No `src/` change is planned. A fix that the tests force is its own small loop
(§7).

## 1. What is left to show

Of the contract's eleven criteria, four are met and recorded, and seven are this step's.

| # | Criterion | State | Shown here by |
|---|---|---|---|
| 1 | The release: 0.1.0 on PyPI from a clean export, the sdist's files, smoke from the index | open | §2 steps 3 and 8 to 10 |
| 2 | The first run within 2 minutes on each machine, on the candidate and confirmed from PyPI | open | §3, and §2 step 10 |
| 3 | `setup` downloads only what the machine uses; its readiness line matches the first call, in Claude Code and in one GUI client | open | §3.5's evidence, §5 |
| 4 | Stock Ubuntu without Docker is `Blocked:` with the reason and the fix | open (seen on older code in steps 0 and 2) | §3, run U1 |
| 5 | The README's configs were used in live sessions | met in step 4a | — |
| 6 | `SECURITY.md` | met in step 4a | — |
| 7 | The demo GIF | met in step 4b, except "renders on the PyPI page" | §2 steps 8 and 10 |
| 8 | F3's sweep | met in step 1 | — |
| 9 | Uninstall leaves nothing but what the README names, and a linked folder survives | open | §4 |
| 10 | macOS: the manual job passes and is recorded | met on `b8565cf`; run again on the candidate commit | §2 step 4 |
| 11 | The gate on the re-frozen set, and the doc tests | open for the candidate commit | §2 step 2 |

**Criterion 3's "no images where native is full" can't be shown on Debian,** which has no
Docker: nothing could be pulled there whatever `setup` decided. Step 2's unit tests hold that
half. The VMs show the other half: no native trees on stock Ubuntu.

## 2. The order, in two parts

`docs/RELEASE.md` puts the release commit on `main` before the first-run tests. Those tests are
the step most likely to change a file, and each change would go round through `main` again
while its README names a package that doesn't exist yet. **So the candidate is built from the
branch, the tests run on it, and the two files that were tested are the two files uploaded**
(S5-D1). Nothing is built a second time.

**The rule that ties the upload to the tests:**
- **The candidate commit `C`** is on `pcbranch`. `release_smoke.py build` makes the sdist and
  the wheel from it once. They are copied to `m4spike\release\0.1.0\` with their sha256, and
  that folder is the only place the VMs, `smoke` and the upload take them from. (`build` empties
  `dist\` each time it runs, so `dist\` isn't where they are kept.)
- **What may change after `C` without redoing the tests:** files the package doesn't ship.
  That is everything under `docs/`, `tests/`, `scripts/`, `.claude/`, `.github/` and
  `requirements/`. A re-freeze of `requirements/constraints.txt` alone is followed by both
  gates and `smoke`.
- **What may not:** the shipped paths, which are `src/vestibule/`, `README.md`, `LICENSE`,
  `pyproject.toml` and `.gitignore` (the build tool adds that one to the sdist). A change to
  one of them makes a new candidate, and §7 says what is run again.
- **Before the upload,** `main` must hold the same shipped paths as `C`: checked in a fresh
  clone of GitHub's `main` in a scratch folder, so nothing in Victor's checkout moves. If they
  differ, the step stops.

> **Amendment 2026-11-11 (step 5c, [`M4-step5c-pypi-page.md`](M4-step5c-pypi-page.md), signed
> by Victor):** from 0.1.1 on, `PYPI.md` is a shipped path too. It is the package's
> description on PyPI, cut from `README.md` by `scripts/pypi_readme.py`.

**5a, nothing published and nothing that can't be undone:**

1. **The candidate commit `C`:** the follow-ups step committed; the README's M4 row (S5-D4) and
   its "ask your agent" sentence (S5-D3). Check: the working tree is clean and the doc tests
   pass.
2. **RELEASE steps 1 and 4:** uv current, and both gates green on `C`. The images were re-pinned
   in step 3 and aren't due. Check: the `gate` agent's report.
3. **Build and smoke** (RELEASE 6 and 7): `build` on `C`, the copy with its hashes, then `smoke`
   on Linux and Windows for Python 3.11 to 3.15. If `smoke` reports drift in the dependency
   set, RELEASE step 2 follows, then the gates and `smoke` again. Check: `smoke` exits 0; the
   uv and Python versions are recorded.
4. **Two checks that don't need `main`,** both Victor's, both on `C`:
   - the secrets scan of the history (RELEASE 5). A secret in a public history is revoked
     first, before anything else in this plan;
   - the macOS job, dispatched on `pcbranch`, as run #1 was (it ran on `b8565cf`, a branch
     commit). If GitHub doesn't offer the branch, the job waits for step 8 and runs before the
     upload.
5. **The VMs are prepared** (§3.2), and the line's cap is measured (§3.3). If the cap doesn't
   hold for downloads, nothing after this starts until it does.
6. **A timed dry pass by Claude on each VM** (S5-D2): from the checkpoint, the capped line,
   uv's install, the wheel, `setup`, and then the checkpoint is restored. It finds a line that
   fails and measures the downloads. It doesn't reach Claude Code, so it doesn't tell the
   whole clock. On Windows there is no SSH: Victor starts one kit script there.
7. **Victor's first runs** (§3), then **uninstall** (§4) and **one GUI client** (§5). Fixes,
   if any, by §7.

**5b, the release.** It starts only when 5a is recorded and Victor says go:

8. **`main` gets the commit, and everything checkable is checked before the upload.**
   - Victor: the merge by his usual pull request. Its head is `C`, or a commit whose shipped
     paths equal `C`'s.
   - Claude, in a scratch clone of GitHub's `main`: the shipped paths equal `C`'s.
   - Claude: the two files' hashes are the recorded ones; `smoke` once more on the wheel.
   - Claude: every link in the README resolves, the GIF's URL returns the GIF, and the README
     renders with PyPI's renderer. The name `vestibule-mcp` is still free.
9. **Victor uploads** (RELEASE 11): the two files from `m4spike\release\0.1.0\`, by name, with
   a token typed at a prompt.
10. **The confirmation from PyPI:**
    - Claude: `release_smoke.py smoke vestibule-mcp==0.1.0 --allow-drift` on Linux and Windows
      (RELEASE 12), and the page: the description renders and the GIF shows.
    - Victor: one first run from the index (S5-D5).
11. **The docs pass and the acceptance record** (§8). "M4 accepted" is written last.

## 3. The first runs (P6)

**The rule is P6's:** Victor follows only the README, and Claude doesn't help while the clock
runs. A command he types that the README doesn't show fails the run. What counts as the
README's:
- its commands, pasted as they are, with one recorded substitution: the wheel's path in place
  of the package name;
- starting `claude`, and answering its own prompts (trusting the folder, allowing the tool),
  as part of the README's "ask your agent";
- **the words to the agent are fixed:** the README's sentence, typed as it is written (S5-D3).
- The test's own: `clock start` and `clock stop`, and nothing else.

**If the agent uses a tool of its own** (its shell, to run `print(1)` on the machine), that
is not a result: there is no `isolation:` line and no audit record. Victor declines it. The
run fails, and what failed is the README's sentence.

### 3.1 The runs

| Run | Machine | Prerequisites, outside the clock | Expected | Timed |
|---|---|---|---|---|
| U1 | Stock Ubuntu 24.04, no Docker | Claude Code, logged in | `setup` and the first call are `Blocked:` with the user-namespace reason and the Docker fix; no native trees downloaded | no: there is no result to stop on |
| U2 | The same, with Docker Engine from Docker's apt repository, the user in the `docker` group | Claude Code, logged in; Docker | `isolation: container`; no native trees downloaded | yes |
| D1 | Debian 13 minimal | Claude Code, logged in; the README's three packages and the reboot | `isolation: native` | yes |
| W1 | Windows 11 in a VM, Docker Desktop running | Claude Code, logged in; Docker Desktop | `isolation: container` | yes |

- **The Linux VMs are driven from an SSH session** in a terminal on Victor's PC, so the
  README's lines can be pasted and the output copied. The Windows VM is driven in its own
  window.
- **U1 and U2 start from different checkpoints,** so U2 is a first run too.
- **W1 is the first time uv's own installer runs on Windows** in this project. Whether a new
  terminal then finds `uv` and `vestibule-mcp` is what step 0 left open (A-1). **It may not be
  answerable here:** Claude Code's own installer can put the same folder on `PATH` first. The
  `PATH` at each checkpoint is recorded, and if that folder is already on it, A-1 is recorded
  as still not measured.

### 3.2 The checkpoints

Logging Claude Code in is the slow prerequisite, and a fix can force a run again. Each VM gets
a checkpoint **after** its prerequisites and **before** anything of Vestibule's:
- `m4-ubuntu`: `prereq` (Claude Code logged in) for U1, and `prereq-docker` (plus Docker) for U2;
- `m4-debian`: `prereq` (the three packages, the reboot, Claude Code logged in);
- the Windows VM: `prereq`. It has no clean checkpoint to start from, so step 0's leftovers
  are removed by a list that is checked: the old images, the kit folder with its copy of uv
  and its Python, `~/.vestibule`, and the user `PATH` entries they added.

**In every checkpoint, and recorded:** automatic updates paused (Windows Update, apt's timers),
so nothing else downloads during a run; on Linux, the shell set to write its history after
each command.

**Not yet tried on this machine, so tried first:** a checkpoint of the Windows VM, which runs
Docker inside it. One restore and start is timed in step B. If it doesn't come back with
Docker working, W1 takes P6's fallback (a new, empty Windows account on Victor's PC, recorded
as weaker).

**A restored login can have expired.** Before `clock start`, Victor starts `claude` once to
see that it is logged in, and leaves it. That is a prerequisite, outside the clock.

### 3.3 The clock

- **Start:** `clock start`. It first refuses if the machine isn't a first-run machine: uv or
  its cache present, `~/.vestibule` present, either pinned image present, a `vestibule` entry
  in Claude Code, or (on U2 and W1) Docker not answering. Then it writes the time in UTC, and
  the machine's uptime beside it.
- **Stop:** the time (`ts`) of the first audit record of a finished run: a record whose `kind`
  is `end`, whose `outcome` is `ran` and whose `isolation` is the expected one. The server
  writes it itself, in UTC. Any `blocked` record before it, or any `end` with another outcome
  (a refusal inside the run is `start` then `end` with `refused`), is in the record and fails
  a timed run.
  `clock stop` prints the difference, and flags it if the uptime and the wall clock disagree.
- **The line:** 25 Mbit/s, set on the VM's adapter in Victor's admin window.
  - **It has never been used here, and it may limit only what the VM sends.** So it is
    measured first: with the cap on, a download of known size inside the VM.
  - **A run counts only if the speed measured before it is between 20 and 27 Mbit/s.**
  - If the adapter's cap doesn't hold for downloads, the limit is set another way (on the
    host's side of the switch, or in the guest) and measured the same way. The record says
    which.
  - The bytes the VM received during a run are recorded (Hyper-V's own metering), so a split
    of a slow run rests on numbers.
- **Recorded for each run:** the clock, the speed measured, the bytes received, the uv version
  and the Python it chose, the installed package list, and the result's `isolation:` line.

### 3.4 What a run must not be mistaken for

| It would look like a pass if | What stops it |
|---|---|
| the dry pass left uv, the trees or the images behind | the dry pass restores the checkpoint, and `clock start` refuses a machine that has them |
| the agent ran the code with its own shell | no audit record, so no stop; the rule above fails the run |
| the first call was `Blocked:` and a retry worked | the earlier record fails the run |
| the line was faster than a stranger's | the 20 to 27 Mbit/s band |
| a step was filled in from memory | the shell's history, written after each command, is compared with the README |
| the session was on an older install | on Victor's PC (§5): the tool list is empty first, and the installed files are compared with the wheel |

### 3.5 What Claude collects after each run, without touching the run

Over SSH on Linux and by the kit's script on Windows, read-only:
- `setup`'s output as Victor saw it: he copies it from his terminal into the chat, as in step
  4a. On Windows, a screenshot or the copied text;
- `vestibule-mcp audit` and the audit files: every record of the run, its isolation, and the
  client's name;
- what was downloaded: the folders under `~/.vestibule`, and `docker image ls` (criterion 3);
- `uv tool list`, the interpreter's version, and the packages in the tool's environment.

### 3.6 Pass, and what a miss means

- **A timed run passes** when the clock is within 2 minutes, the isolation is the expected
  one, the line was in its band, and nothing outside the README was typed.
- **A wrong isolation, a line of the README that doesn't work, or a message that misleads** is
  a failure that gets fixed (§7).
- **A clock over 2 minutes** is split first, from the bytes received and the audit's times:
  the download at 25 Mbit/s, Vestibule's own part, the agent's turn, and typing. If Vestibule's
  part can be cut, it is. If the time is the line's or the model's, Victor decides between a
  smaller download and a criterion that says what was measured. The plan doesn't decide that
  ahead of the numbers.

## 4. Uninstall (criterion 9)

On D1 (native) and on W1 (container), after their first run. Three things make it a test that
can fail:
1. **A listing before and after.** The kit lists the home folder, the temp folder, the
   containers, the images and (Linux) the user's systemd units at the checkpoint and after the
   uninstall. Whatever is new and isn't on the README's "What stays" list fails it.
2. **A folder the guest locked.** The agent is asked to run one line that makes a folder in
   the workspace and takes away its own write permission on it (on Windows, a read-only
   file). This is the case the README's `chmod` line exists for (step 0, A-2).
3. **A folder that must survive.** The kit makes `sentinel/keep.txt` outside `~/.vestibule`,
   without write permission, and the agent is asked to make a link to it in the workspace.
   - On Linux the link is the guest's, with the folder's full path.
   - On Windows a guest's link is a real one on the host only for a relative target, and
     only if the machine lets a user make links (step 0 had Developer Mode on). The kit
     checks what kind of link arrived. If it isn't a real one, the kit makes one from an
     admin window, and the record says the guest's own link wasn't the one tested.

Then **Victor types the README's uninstall lines,** and nothing else, and Claude checks,
read-only: the listings, no `vestibule-mcp` on `PATH` or in `uv tool list`, no `vestibule` entry
in `claude mcp list`, and `sentinel/keep.txt` there with its content and its permissions.

## 5. One GUI client on the candidate (criterion 3)

On Victor's PC, with the candidate wheel: Cursor, with the README's JSON.
- **Before:** `uv tool list` shows no `vestibule-mcp`. After the install, the installed files
  are compared with the wheel's.
- **The check:** `setup`'s readiness line and the isolation of Cursor's first `run_code` call
  agree, and an audit record written after the session started names Cursor. (Step 4a's
  Cursor records are still in that log, so only newer ones count.)
- **After:** the config entry and the tool are removed.

## 6. What Victor does, in order

Each gets exact "type this in that window" steps when its turn comes. Times are estimates.

| # | What | Time |
|---|---|---|
| A | Commit the follow-ups step and the candidate commit; the secrets scan; start the macOS job on the branch | 15 min |
| B | Admin window: cap one adapter and measure it. Then, per VM: the prerequisites of §3.2 (Claude Code and its login on three VMs, Docker on Ubuntu, the Debian packages), and the checkpoints. One restore of the Windows VM is timed | 75 to 90 min |
| C | Start the kit's dry-pass script in the Windows VM (the Linux ones need nothing from him) | 5 min |
| D | The four runs of §3.1 | 25 min |
| E | Uninstall on two machines (§4) | 15 min |
| F | Cursor on the candidate (§5) | 5 min |
| G | A PyPI account with two-factor sign-in and an API token, if not there yet | 10 min |
| H | The release: the pull request and merge, then the upload | 10 min |
| I | The first run from PyPI on one VM | 10 min |

About three hours, and it can be split over days: A to C, D to F, and G to I are three
sittings.

## 7. When a test fails

- **A docs-only fix** (a README line, a message quoted wrongly): the main session writes it,
  the doc tests run, and `critic` reads the changed sentences.
- **A fix in `src/`:** its own short plan, sign-off, `builder`, gate, `critic`, `verifier`, as
  any step.
- **Either one changes a shipped path, so it makes a new candidate:** a new commit `C`, a new
  build, new hashes, `smoke` again. Then:
  - the first run again, from its checkpoint, on every machine whose README section, `setup`
    path or backend the fix touches;
  - a fix to the README's words in one platform's section doesn't re-run the others;
  - the macOS job again if the fix is in `src/`.
- The record lists every run, including the ones that failed.

## 8. Docs

- **`docs/RELEASE.md`:** rewritten to §2's order, if S5-D1 is taken: the candidate from the
  branch, the folder its files are kept in, what may change after it, the shipped-path check
  against `main`, and the link check before the upload.
- **`README.md`,** in the candidate commit only: the M4 row (S5-D4) and the "ask your agent"
  sentence (S5-D3).
- **`docs/plans/M4/M4-adoption.md`:** dated amendments to P6 (the checkpoints, the clock's
  start and stop, the band, the dry pass, the fixed sentence), to criterion 2 (S5-D5's reading
  of "confirmed from PyPI") and to §6's step 5; and the acceptance record: each criterion with
  its evidence.
- **`docs/PLAN.md`, `docs/ARCHITECTURE.md`, `docs/GETTING_STARTED.md`:** what M4's end changes
  in each.
- **`docs/BUILD_LOG.md`, `docs/HISTORY.md`:** 5a's entry, 5b's entry with `C`'s hash and the
  two files' hashes (RELEASE step 3 asks for the commit), and "M4 accepted".

## 9. What can go wrong

| Case | Handling |
|---|---|
| The name `vestibule-mcp` is taken before the upload | Checked free on 2026-11-07, and again at step 8. If it is taken, the step stops: a new name changes the README and restarts 5a. |
| The secrets scan finds something | Revoke it first. Nothing else in 5a continues until Victor says how the history is handled. |
| 5b stops after the merge (a link is dead, the name is gone) | `main` then names a package that isn't published. If the fix takes longer than a day, the merge is reverted. Victor's call. |
| The upload stops halfway (one file up, one not) | `uv publish` again with the missing file named. A file that is up can't be replaced: a wrong one means 0.1.1. |
| 0.1.0 from the index is broken (`smoke` from PyPI fails) | Yank 0.1.0 on PyPI, which can be undone and makes the README's unpinned install line skip it, while 0.1.1 goes through the loop. |
| The published page renders wrongly | The README already renders with PyPI's own renderer, `twine check` is in `build`, and step 8 fetches every link. If it still does, 0.1.1 fixes it; 0.1.0's page stays. |
| `main`'s shipped paths differ from `C`'s | The step stops at 8. Either the difference is removed, or it is a new candidate (§7). |
| `smoke` reports drift between 5a and the upload | A constraints-only re-freeze, both gates, `smoke`. If the `mcp` minor moved, `pyproject.toml` changes: a new candidate (§7). |
| The cap can't be made to hold for downloads | Nothing timed is run. Victor decides whether the criterion is measured another way. |
| The Windows VM's checkpoint doesn't restore with Docker working | P6's fallback, recorded as weaker. |
| A checkpoint's Claude Code login has expired | Log in again and re-make the checkpoint: outside the clock. |
| Docker Hub refuses pulls (100 an hour here) | Each run pulls two images. Twenty runs fit. |

## 10. Decisions

### S5-D1 (Victor, 2026-11-10: (1)) — Where the candidate is built from, and what is uploaded
- **(1) Recommended: built once from the branch; the tested files are the uploaded files;
  `main` gets the commit when the tests pass.** A failed run costs a branch commit, not a
  round through `main`. The README on `main` names an unpublished package for hours, not for
  the days the VM runs may take. Before the upload, `main`'s shipped paths are checked against
  the candidate's.
- **(2) As `RELEASE.md` says today: `main` first, and build there.** No check to make between
  two commits. Every fix goes back through `main`.

### S5-D2 (Victor, 2026-11-10: (1)) — A timed dry pass by Claude before your runs
- **(1) Recommended: yes,** from each checkpoint, restored afterwards. It finds a line that
  fails and measures the downloads before your time is spent. It isn't a first run and isn't
  counted as one.
- **(2) No.** Your runs are then the first time the candidate meets the VMs.

### S5-D3 (Victor, 2026-11-10: (1)) — What the README tells the user to say to the agent
Today: "ask your agent to run `print(1)`". It names neither Vestibule nor the tool. A Claude
Code session has its own shell, and may run `print(1)` with that, on the machine.
- **(1) Recommended: name the tool,** in the README's one sentence and so in the test: "ask
  your agent to run `print(1)` with Vestibule's `run_code` tool". It is what the sandbox-only
  section already explains, said where a first user reads it.
- **(2) Leave it,** and let the first runs show whether an agent picks the sandbox by itself.
  If one doesn't, that is a failed run and this change then.

### S5-D4 (Victor, 2026-11-10: (1)) — What the published README says about M4
The README is frozen into the package. Its M4 row says "in progress" today.
- **(1) Recommended: "first release, 0.1.0",** with the link to the contract, set in the
  candidate commit. It is true of every copy of the page that ships. The acceptance is
  recorded in the repo after the upload, where it can be dated.
- **(2) Leave "in progress".** True on the day, and stale on PyPI from then on.

### S5-D5 (Victor, 2026-11-10: (1)) — What "confirmed from PyPI" means in criterion 2
The contract says "a short confirmation from PyPI after the upload" and doesn't say how many
machines.
- **(1) Recommended: one machine, Debian, capped and timed like its first run,** with the
  README's own line `uv tool install vestibule-mcp`. Its checkpoint starts quickest. The two
  platforms' installs from the index are covered by `smoke`.
- **(2) All three machines again.** About 25 more minutes of yours.

**Not asked again:** a rehearsal upload to TestPyPI (F1 already says no, with its reason), and a
stranger's run (P6 makes it optional, recorded on top if one happens).

## 11. The kit (outside the repo, `m4spike\vm\step5\`)

Written by Claude before step B, and listed in the record: the capping, metering and checkpoint
commands for the admin window; the speed measurement; the wheel's copy with its hash; `clock`
for each platform, with its refusals; the dry-pass scripts; the listings and the read-only
collectors of §3.5 and §4; the cleaning list for the Windows VM.

## 12. Files

| File | Change |
|---|---|
| `README.md` | the M4 row (S5-D4) and one sentence (S5-D3), in the candidate commit |
| `docs/RELEASE.md` | §2's order (S5-D1) |
| `docs/plans/M4/M4-adoption.md` | amendments, and the acceptance record |
| `docs/PLAN.md`, `docs/ARCHITECTURE.md`, `docs/GETTING_STARTED.md`, `docs/BUILD_LOG.md`, `docs/HISTORY.md` | §8 |

**Not touched unless a test forces it:** anything under `src/`, `tests/`, `scripts/`,
`pyproject.toml`, `requirements/`.

## 13. Review (`skeptic`, 2026-11-10; `verifier` on its checkable claims)

`skeptic`'s verdict on v1 was "sound with amendments": 2 high, 7 medium and 8 low, with §2's
last step needing a rewrite. `verifier` checked its nine claims about existing files: seven
confirmed, two partly, none refuted. Its corrections are in the text above:
- the sdist also holds `.gitignore` and a generated `PKG-INFO`, so `.gitignore` is a shipped
  path;
- a refusal inside a run writes `start` and then `end` with the outcome `refused`, not a
  `blocked` record;
- that the macOS job can be dispatched on a branch couldn't be checked from here; run #1's
  record says it was.

| # | Sev | Finding | In v2 |
|---|---|---|---|
| 1 | high | "Build again from `main` and compare the files" had no way to work: `build` empties `dist\`, the local `main` is stale, and the build tool's version is stamped into the wheel, so two builds needn't match | §2: built once, kept in their own folder, and those files are uploaded; `main` is checked in a scratch clone |
| 2 | high | Whole-tree equality would stop the step by its own records, and a dependency re-freeze would force every run again | §2's rule: only the shipped paths matter |
| 3 | med | The dry pass could leave a warm machine, and Victor's run would download nothing | §2 step 6 restores; `clock start` refuses (§3.3, §3.4) |
| 4 | med | The clock stopped at a run's `start` record, before any result, and a `blocked` record would also stop it | §3.3: the first `end` that ran with the expected isolation |
| 5 | med | The adapter's cap was assumed, and may not limit downloads | §3.3: measured first, a band, the bytes received |
| 6 | med | The macOS job and the secrets scan waited for the merge for no reason | §2 step 4 |
| 7 | med | Nothing fixed what Victor says to the agent, or what happens if it uses its own shell | §3's rule, S5-D3 |
| 8 | med | Uninstall's checks couldn't fail: no locked folder, a link that may not be real on Windows, no before-and-after listing | §4 |
| 9 | med | The Windows VM has no clean checkpoint and no SSH, and a checkpoint of it was never tried | §3.2, §2 step 6, §6 B and C |
| 10 | low | Every check of the published page came after the upload; no row for a broken 0.1.0 | §2 step 8; §9's yank row |
| 11 | low | The evidence named a scrollback and a history that aren't there | §3.5, §3.2 |
| 12 | low | W1 was said to answer A-1, which Claude Code's installer may mask | §3.1 |
| 13 | low | "No images where native is full" is empty on a machine with no Docker | §1 |
| 14 | low | "Confirmed from PyPI" was read as one machine without saying so | S5-D5 |
| 15 | low | Two decisions re-opened what the contract had decided | Dropped (§10's last line) |
| 16 | low | Cursor's session wasn't tied to the candidate | §5 |
| 17 | low | A restored checkpoint rewinds the login and the clock; two statements about `smoke` and the dry pass were wrong | §3.2, §3.3; §2 steps 2 and 6 |
