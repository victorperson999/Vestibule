# M4 step 3 — the release packaging (F1, F2, P2, P9)

Status: **v2, signed off 2026-11-09 with U1–U5 as written, and built that day (§12).** v1 was reviewed by `skeptic` (19 findings: 2 high, 7
medium, 10 low; verdict "sound with amendments"). `verifier` checked 13 claims about the existing
code, tools and logs: 12 confirmed, 1 refuted (§11). Every finding is taken below.

Contract: [`M4-adoption.md`](M4-adoption.md), F1, F2, P2, P9, and §6's step 0 amendment (the smoke
script runs Python 3.11 through 3.15). Acceptance criteria 1, 10 and 11 are finally judged in step
5, which also does the upload. This step builds what they use. **Nothing is published here.**

## 1. The problem

A release built today would be wrong in five ways (§6 has the measurements):
- **The sdist ships 113 files,** among them `.claude/`, `.continue/`, a screenshot, `docs/` and
  `tests/`. Only `src/`, the README, the license and `pyproject.toml` belong in it.
- **A clean export isn't clean on this machine.** `core.autocrlf=true` makes a plain `git archive`
  write every Python source with CRLF. `.gitattributes` protects only the shell scripts, the
  fixtures and the seccomp profile.
- **The dependency set is one no gate ran.** The published range is `mcp>=1.28.1,<2`, the
  constraints pin 1.28.1, and a fresh install gets 1.30.0, with 30 packages different in all.
- **The images are a month old,** and both tags have moved. Pinned images get no upstream fixes
  until they're re-pinned.
- **Nothing checks a wheel.** V13's point stands: a wheel without `seccomp-profile.json` can't
  start, and nothing re-checks that after M2's benchmark.

## 2. The changes

### 2.1 Metadata

`pyproject.toml` and `src/vestibule/__init__.py` both go to `0.1.0` (`test_config.py:172` keeps them
equal). `pyproject.toml` gains:

```toml
dependencies = [
    # The minor both gates ran (M4 P2). Move it with requirements/constraints.txt.
    "mcp>=1.30.0,<1.31",
]
classifiers = [
    "Development Status :: 3 - Alpha",
    "Operating System :: Microsoft :: Windows",
    "Operating System :: POSIX :: Linux",
    "Programming Language :: Python :: 3.11",
    # ... 3.12, 3.13, 3.14, 3.15: each only if §2.4's smoke passes on a final release of it,
    # on both platforms
]

[project.urls]
Homepage = "https://github.com/victorperson999/Vestibule"
Issues = "https://github.com/victorperson999/Vestibule/issues"

[tool.hatch.build.targets.sdist]
include = ["/src/vestibule", "/README.md", "/LICENSE", "/pyproject.toml"]
```

No macOS classifier: F1 claims what the gates and the first-run test ran, and P9's job is neither.
The README's "early development" line stays.

### 2.2 The dependency set (F1)

**Only what the release wheel pulls in moves (U1).** In a fresh Linux venv, install the wheel with no
constraints, and `pip freeze`. Those packages' lines in `requirements/constraints.txt` take the
resolved versions, and any new one is added. The lines for packages only the dev tools need, and the
four tools themselves, stay. `colorama` and `pywin32` come from a `uv pip compile --universal`. If
installing `.[dev]` on the new file conflicts, the header's full regenerate applies. The header gets
a line saying so.

Then every venv is rebuilt from the new file, and both gates run on it:
`scripts/gate-linux.sh --setup` for the two Linux venvs, and a `uv pip install` for Windows' `.venv`
(§3 has the catch there).

### 2.3 The images (F1)

M1 S6's routine re-pin, as amended in M2 step 2:
1. **Capture** for each tag: the `Digest:` line of a fresh `docker pull`, which must match a
   `RepoDigests` entry, and `docker buildx imagetools inspect` showing an index with `linux/amd64`
   and `linux/arm64`.
2. **Empty the store of it:** `docker image rm` the tag and the digest ref, and assert that `docker
   image inspect <new ref>` now fails. The capture's pull made it succeed, and Windows and WSL share
   one daemon, so this is what makes the next step prove the README's exact command.
3. **Proof gate:** `docker pull <new ref>`, `docker run --pull never` for each, then
   `pytest -m docker`.
4. **On Linux:** `vestibule-mcp setup`, then `pytest -m network`. A pin that brings a layer type or
   entry kind native refuses fails here. Today's tags are gzip on both architectures (§6).

The digests go in `config.py` with its "Captured" comment, and in the README's two `docker pull`
lines and its "as of" date.

### 2.4 `scripts/release_smoke.py` (F2)

A dev tool, run with the dev venv's Python, so its stdout is the user's. Every child process has a
time limit. Two subcommands:

**`build [--repo PATH]`** makes the artifacts:
1. Refuses if anything that ships differs from `HEAD`: `git status --porcelain` limited to `src`,
   `pyproject.toml`, `README.md` and `LICENSE`, with `core.autocrlf=input` on POSIX as
   `bench_coldstart.py` does. Untracked files under `src` count, because they wouldn't be in the
   export.
2. Exports `HEAD` with `git -c core.autocrlf=false archive`, into a temp dir.
3. Runs `uv build` there with neither `--sdist` nor `--wheel`, so the wheel is built **from the sdist**
   and the include list is proven to be enough.
4. Checks them in the temp dir, and prints every file in the sdist:
   - `sdist_problems(names)`: nothing outside `src/vestibule/`, `README.md`, `LICENSE`,
     `pyproject.toml`, `PKG-INFO` and `.gitignore` (which hatchling always adds), and all of the
     named ones present.
   - `wheel_problems(zip)`: V13's three files present, only `vestibule/` and its `dist-info`, and
     no CRLF in any `.py`.
5. `uv tool run twine check` on both.
6. **Only then** removes any `dist/vestibule_mcp-*` and moves the two files in. A build that fails
   leaves `dist/` as it was, because `uv publish` takes all of `dist/*` by default and can't be undone.

**`smoke <wheel-or-requirement> [--python ...] [--expect native|container|blocked] [--uv PATH]
[--allow-drift]`** checks an install. `--expect` defaults to `native` on Linux, `container` on
Windows and `blocked` on macOS, whose runner has no Docker. Everything lives in one throwaway root
(tools, uv's cache, managed Pythons, the workspace and the audit folder), through one environment
for all the steps below with `VESTIBULE_*` from the shell removed. For each Python:
1. `uv tool install --python <X> <source>`.
2. The tool's own Python imports `vestibule.server`. `check_install` then requires `vestibule.__file__`
   under the throwaway root, and `sys.version_info.releaselevel == "final"`: no classifier rests on
   a beta.
3. **Drift:** the install's packages against `requirements/constraints.txt`. Any difference fails,
   because no gate ran that set (F1). `--allow-drift` is for step 10 of the checklist, after the
   upload, when a difference can only be reported.
4. `vestibule-mcp setup`, compared by `parse_setup` (anchored at line start, the whole token):
   stdout empty, `ready …: isolation X` with X exactly `--expect` and exit 0, or `Blocked:` and exit 1
   for `blocked`. `not ready:` always fails.
5. Over real stdio, with `StdioServer.start(..., exe=<this install>)`: `initialize`, `tools/list` has
   the two tools, one `tools/call` of `print(1)` whose isolation equals `--expect`, no non-JSON on
   stdout, exit 0 after stdin closes.
6. `vestibule-mcp audit`: exit 0, names the folder it read, shows this run's record.
7. `pytest tests/test_stdio_e2e.py` against that install, through §2.5's variable. It **fails if
   `dockerutil.docker_skip_reason()` gives a reason,** except under `--expect blocked`: an all-skipped
   run exits 0, and this is what `gate-linux.sh` forbids.

It prints `uv --version` and each interpreter's full version, which criterion 2 asks to record. It
exits non-zero on the first Python that fails, naming the step.

### 2.5 The test helper

`tests/stdioutil.py`:
- `StdioServer.start` gains `exe: Path | None = None`, defaulting to `SERVER_EXE`. `smoke` passes
  each install's path, because `SERVER_EXE` is fixed at import.
- `SERVER_EXE` reads `VESTIBULE_TEST_SERVER_EXE` when it's set, for step 7's child pytest.
- `scripts/check_env.py` fails if that variable is set, so a shell with it exported can't run a gate
  against a stale install.

### 2.6 `RELEASE.md`, under `docs/` (F2)

The checklist, in order, each step with its command and who runs it:
1. A clean tree on the release commit, **pushed and merged to `main`,** with the README's absolute
   URLs resolving: a published description can't be changed. Record the hash in `BUILD_LOG`.
2. Update uv (a final 3.15 needs a recent one).
3. Re-freeze and re-pin when due. The dependency set is due on any `smoke` drift. The images are due
   if the tags have moved by more than a routine amount of time; that one is Victor's judgment.
4. Both gates green on that set.
5. The history checked for secrets: `gitleaks` in Docker at a pinned version, redacted (U3).
6. `release_smoke.py build`.
7. `release_smoke.py smoke dist/<wheel>` on Linux and on Windows.
8. P9's macOS job.
9. P6's first-run tests on the VMs.
10. Victor uploads: `uv publish dist/vestibule_mcp-0.1.0*`, naming the files. It can't be taken back,
    so Claude never runs it. No TestPyPI (F1).
11. `release_smoke.py smoke vestibule-mcp==0.1.0 --allow-drift` from the index, on Linux and on Windows.

### 2.7 `.github/workflows/macos.yml` (P9)

`workflow_dispatch` only, on `macos-latest`, with `permissions: contents: read`, 20-minute timeout,
and both actions pinned by commit hash. It installs uv and Python 3.11, builds `.venv` from the
constraints, then runs `check_env.py`, `pytest -m "not docker and not native and not network"` (which
includes `test_signals.py`), `release_smoke.py build`, and `release_smoke.py smoke <wheel> --python
3.11 --allow-drift`.

**`tests/test_shutdown.py:249` is fixed here.** Its guard is only against win32 and `_alive` lists
`/proc`, so on a Mac two tests fail, and in `verifier`'s simulation the run then hung in teardown.
It becomes `sys.platform != "linux"`. A sweep of the other unmarked tests, and that simulation, found
no other case.

**P9's "stdio end-to-end tests" reduce to the smoke's stdio run there (U5):** `test_stdio_e2e.py` is
Docker-marked, and these runners have no Docker.

## 3. The order of the build

Each step depends on the one before. **Any code change after a gate means the gate runs again.**

1. `pyproject.toml`, `__init__.py`, then the re-freeze (§2.2).
2. Rebuild the venvs. **On Windows this needs this session's own `vestibule` MCP server stopped,**
   because its Python has `.venv`'s compiled modules loaded and Windows won't replace a loaded file
   (U2).
3. The re-pin (§2.3), on the new set.
4. The script, its tests, the workflow, the guard fix, `RELEASE.md`.
5. **Real smoke, Windows and WSL, Python 3.11–3.15,** on a wheel built from a scratch copy of the
   working tree, with a uv that offers a final 3.15. The classifiers keep only what passes.
6. **`build`'s negative path, before the commit:** a scratch clone of today's `HEAD` (version 0.0.1, no
   include list), with the script run from outside it through `--repo`. It has to fail on the sdist
   list, and leave that clone's `dist/` empty.
7. Gate, `critic`, `verifier`, the mutants.
8. Docs and the staging list.
9. **After Victor commits:** one real `release_smoke.py build` on the clean export, the positive path.
10. **After Victor's usual pull request:** the workflow is on `main`, and he can press Run. Any
    failures come back to Claude as a small follow-up, not at step 5.

## 4. For Victor's sign-off

- **U1 — Only what the wheel pulls in moves.** The dev tools and their own dependencies stay, so the
  gate's checks don't change in the same step as the set they check. F1 says "a fresh resolve of the
  release wheel".
- **U2 — Stopping this session's `vestibule` MCP server once.** It's this Claude Code session's own
  child, and the session doesn't need it. It comes back with the next session. The alternative is for
  Victor to close this session and start another before the build.
- **U3 — Victor runs the secret scan, at release time.** I tried `gitleaks` on the history while
  planning and the permission system refused it, so **the history hasn't been scanned.** The checklist
  gives the exact command.
- **U4 — The macOS job runs after Victor's usual pull request.** GitHub only offers the Run button
  once the file is on the default branch. `main` holds the same content as the pushed `pcbranch` plus
  17 merge commits, so a normal PR does it.
- **U5 — P9's "stdio end-to-end tests" become the smoke's stdio run on macOS,** because that test file
  needs Docker.

Other readings, stated:
- **The re-pin happens once, here,** unless the checklist says it's due again.
- **No git tag.** The contract doesn't ask for one. `BUILD_LOG` records the release's hash.

## 5. Edge cases

| Case | What happens |
|---|---|
| `build` with an uncommitted change that ships | Refused, with the reason |
| `build` with a failure at any check | `dist/` is left as it was |
| An old `0.0.1` wheel in `dist/` | Removed by a passing `build`; and step 10 names exact files |
| A wheel from a plain `git archive` on Windows | `wheel_problems` fails it on CRLF |
| `smoke` where the trees or images are missing | `setup` fetches them, as a user's would |
| Docker stopped on Linux or Windows | Step 7 fails with the reason, instead of skipping green |
| `setup` says `not ready:` (a node image failed) | Step 4 fails |
| A package moved since the re-freeze | Step 3 fails before the upload |
| Python 3.15 fails on one platform, or only a beta is offered | Its classifier isn't added. uv may still pick 3.15 for a user, so that goes in step 4's README |
| A re-pin brings a zstd layer or a whiteout | `setup` fails loudly (S2-D2); the tag is then Victor's decision |
| `mcp` publishes 1.31 before the upload | The range holds at 1.30.x, as P2 says |
| `mcp` publishes 1.30.1 | Step 3's drift says so, and the set is re-frozen before the upload |

## 6. Facts measured for this plan (2026-11-09)

All in a scratch copy.
- **`git archive`:** plain, `server.py` is 565 CRLF lines; with `-c core.autocrlf=false`, 0. The
  profile and `gate-linux.sh` are LF both ways.
- **The sdist:** today 113 files. With the include list, 27, 115 KB. Hatchling adds `.gitignore` and
  `PKG-INFO` itself.
- **The wheel:** 120 KB, 27 files, V13's three present, the license in `dist-info/licenses`.
- **`twine check`:** passes on both with the planned metadata. It doesn't fetch links.
- **A fresh resolve:** `mcp` 1.30.0 is the newest 1.x (2.3.0 is the newest overall). 30 packages
  differ from the constraints, among them `anyio` 4.14.1 → 4.16.0, `pydantic` 2.13.4 → 2.14.3,
  `starlette` 1.3.1 → 1.10.0.
- **The tags:** `python:3.12-slim` and `node:22-slim` both moved. Both are OCI indexes with amd64 and
  arm64, gzip layers only, 43 MB and 80 MB on amd64: the contract's 123 MB.
- **PyPI:** `vestibule-mcp` still returns 404. All five Python classifiers and the OS ones exist.
- **uv:** Windows' `PATH` one is 0.11.15 and offers only `cpython-3.15.0b1`; the kit's is 0.13.7.
- **`uv publish`** with no files takes `dist/*`. `uv build` with no flags builds the wheel from the
  sdist.
- **`main` on GitHub:** the same content as pushed `pcbranch`, plus 17 PR merge commits. Local
  `pcbranch` is 2 commits ahead of the pushed one.

## 7. Tests

New `tests/test_release_smoke.py`, on every platform, with no Docker:

| # | Test | Would fail if |
|---|---|---|
| 1 | `sdist_problems`: the real list passes; `.claude/settings.local.json`, a test file and a stray doc are named; a missing `LICENSE` is named | The list loosens |
| 2 | `wheel_problems` on a built zip: passes; each of V13's three missing is named; a CRLF `.py` is named; a stray top-level entry is named | A wheel check goes |
| 3 | `parse_setup`: `ready …: isolation native`, `… container-degraded (detail)`, `native-degraded (…)`, `not ready: …` and `Blocked: …`, against exit codes and `--expect` | A prefix match or a loose parse comes back |
| 4 | `check_install`: a file outside the root fails; a beta fails | The wrong binary or a beta passes |
| 5 | The drift comparison: same, moved, and new packages; `vestibule-mcp` exempt | Drift goes unseen |
| 6 | Docker-skip rule: a reason fails, except under `blocked` | An all-skipped run goes green |
| 7 | `build` leaves `dist/` alone when a check fails, and replaces only `vestibule_mcp-*` when they pass | An unchecked file reaches the upload |
| 8 | The `mcp` range is exactly the constraints pin's minor (`>=X.Y.0,<X.(Y+1)`) | The range and the tested set drift (P2) |
| 9 | The Python classifiers are the script's default list | A classifier claims what the smoke doesn't run |
| 10 | The sdist include list names only what `sdist_problems` allows | The two lists drift |
| 11 | `StdioServer.start(exe=…)` spawns that path; `VESTIBULE_TEST_SERVER_EXE` moves `SERVER_EXE` | The wrong binary is driven |
| 12 | `check_env` fails when the variable is set | A gate can test a stale install |

**Real runs, the heart of this step:**
- `smoke` on Windows and in WSL, Python 3.11–3.15, on a real wheel (§3.5).
- **It has to be able to fail, at each stage:** a wheel with `seccomp-profile.json` removed fails at
  step 2; on Linux, `--expect container` fails at step 4; a wheel whose server prints to stdout fails
  at step 5; `build` on the scratch clone fails on the sdist list (§3.6).
- The re-pin's proof gate, `pytest -m docker`, and `pytest -m network` on Linux.
- `actionlint` on the workflow, as a one-off with no new dependency.

**Mutants** (foreground, byte-for-byte restore, named tests, green control first): the sdist allowing
anything; the required-names check dropped; a V13 file off the list; the CRLF check dropped; the
stray-entry check dropped; the `dist/` move before the checks; `parse_setup` matching a prefix; `not
ready:` accepted; the exit code not compared; the file-location check dropped; the release-level check
dropped; drift ignored; the Docker-skip rule dropped; the `mcp` range loosened to `<2`; a classifier
added; `exe` ignored; the env override ignored; `check_env`'s new rule dropped.

## 8. Docs

- `RELEASE.md` (new, under `docs/`).
- `README.md`: the two pins and the date (`test_pins.py` holds it).
- `requirements/constraints.txt`'s header: U1's procedure.
- `GETTING_STARTED.md`: a pointer to the checklist.
- `M4-adoption.md`: a dated amendment for U1 and U5.
- The HISTORY bullet and the BUILD_LOG entry.

## 9. Size

Contract: src ~20, script ~100. Expect src about 15 (the version, the digests) plus about 35 lines of
`pyproject.toml`, the script about 320, tests about 300, `RELEASE.md` about 120, and the workflow
about 40. The script is well over the contract's guess: its seven smoke steps each need a check that
can fail, and step 1's and step 2's estimates ran low too.

## 10. What this step doesn't settle

- **The history hasn't been scanned for secrets** (U3).
- **The macOS job hasn't run.** A sweep and a simulation found one failure, now fixed, but the
  simulation can't show kqueue, APFS or a real runner's timing: `test_signals.py` needs an exit within
  0.1 s of SIGINT. §3.10 gets the first real run.
- **The positive path of `build`** waits for the commit (§3.9), and step 5 repeats it on the release
  candidate.
- **Users get transitive packages fresh after the upload.** Before it, the drift check holds them to
  the gated set. After it, P2 accepts that, and F5 §4 says so.

## 11. Review (`skeptic`, then `verifier`, 2026-11-09)

`skeptic` read v1 with no context from this session. `verifier` checked the claims it made about
existing code, tools and logs: 12 confirmed, and 1 refuted, that `test_stderr_log.py` would fail on
macOS (its one risky test is `@native_required`, so it skips). `verifier` also found that the
"37 commits behind" in v1 came from a stale local ref.

| # | Sev. | Finding | In v2 |
|---|---|---|---|
| 1 | high | `smoke` could drive the wrong binary for every Python after the first, silently, and §2.1's classifiers rest on it | `exe=` per install, `check_install`'s location check, tests 4 and 11, a negative control at step 5 |
| 2 | high | A failed build leaves its artifacts in the upload glob; `uv publish` takes `dist/*` | Build in a temp dir, move only after every check; the checklist names exact files |
| 3 | med | The macOS job would fail on its first run, and it was deferred on a wrong premise | Guard fixed; the first run follows Victor's usual PR (§3.10, U4) |
| 4 | med | `build`'s negative path can't be reached past its own dirty check | `--repo` and a scratch clone (§3.6) |
| 5 | med | The readiness comparison misses the exit code and `not ready:` | `parse_setup`, test 3 |
| 6 | med | An all-skipped e2e passes | Step 7's Docker-skip rule, test 6 |
| 7 | med | The proof gate's precondition is false after the capture's pull | §2.3 step 2 |
| 8 | med | F1's "before the upload" isn't enforced | Step 3's drift check; "due" means any difference |
| 9 | med | The checklist never requires the commit to be pushed and on `main` | Checklist step 1 |
| 10 | low | `--sdist --wheel` doesn't prove the sdist builds | Neither flag |
| 11 | low | Steps 3–5 weren't isolated from the user's `~/.vestibule/audit` | One root, one environment |
| 12 | low | No way to choose uv, and a beta could earn a classifier | `--uv`; the release-level check |
| 13 | low | The dirty check always refuses in WSL | Limited to what ships; `autocrlf=input` on POSIX |
| 14 | low | Gate and review ran before the smoke that can change things | Smoke first; any code change re-runs the gate |
| 15 | low | The override could leak into a gate | `check_env` fails when it's set |
| 16 | low | No time bounds on the children | Every child has one |
| 17 | low | P9's reduction wasn't among the sign-off items | U5 |
| 18 | low | `--expect`'s VM rationale has no caller | Dropped; the flag stays for macOS and the negative control |
| 19 | low | "Dev tools stay" didn't hold for their own dependencies | U1 now keeps those lines too |

## 12. As built (2026-11-09)

**Built as §2 says, with these changes from the diff review.** `critic` found 1 medium and 3 low
issues. `verifier` confirmed all 4, most with a repro, and all are fixed:
- **The gates inside `_smoke_one` had tests only for their helpers,** so deleting the drift failure,
  the Docker-skip rule or the `check_install` call left the suite green. It now has a stand-in for
  every child process, and a test for each gate at its call site.
- **`build`'s refusal of an uncommitted change had no test.** It has one now.
- **"Exit 0 after stdin closes" wasn't what was checked.** A server that ignored EOF and exited on
  the later SIGTERM would have passed. The phase has to be `stdin`.
- **The checklist could change things after recording the commit.** The re-freeze and re-pin now
  come first, so the release commit is step 3 and not step 1 as §2.6 has it, and any later change goes
  back to it. `build` also refuses an uncommitted `constraints.txt`.

The first of those needed new tests and the others touched only their own lines, so `critic` didn't
run again. The final gate ran after all of them.

**The dependency set:** 20 lines moved, among them `mcp` 1.28.1 → 1.30.0, `anyio` 4.14.1 → 4.16.0 and
`pydantic` 2.13.4 → 2.14.3. A Linux gate ran on that set alone, before the re-pin: 935, 885 and 7.

**The re-pin:** `python:3.12-slim` is now `sha256:5e2bdb1b…` (3.12.16) and `node:22-slim` is
`sha256:5ec23b7c…` (v22.23.4). Both digests matched `RepoDigests`, both are multi-platform indexes,
`image inspect` failed after removal, and a pull by the exact ref then worked. `setup` unpacked both
on Linux, and `pytest -m network` passed.

**The real smoke, on a wheel of this tree, uv 0.13.7:**
- Windows, `container`: Python 3.11.9, 3.12.3, 3.13.1, 3.14.10 and 3.15.2. Each also ran
  `test_stdio_e2e.py` against that install: 5 passed, 2 POSIX-only skips.
- WSL, `native`: Python 3.11.18, 3.12.16, 3.13.18, 3.14.4 and 3.15.2, with 7 passed each.
- All five are final releases, so the classifiers stay. After the review's fixes, one Python per
  platform ran again and passed.

**It can fail, at each stage, for real:** a wheel without `seccomp-profile.json` stopped at the import;
`--expect container` on Linux stopped at `setup`; a server printing to stdout stopped at the stdio
check; `build` on a clone of `ff10e3c` printed 113 files and stopped on the sdist list, leaving no
`dist/`; `build` refused a changed README. `actionlint` passes on the workflow.

**Mutants:** 41 of 41 caught on their named tests after a green control. "`not ready` accepted" first
survived, because such a line fails anyway, only with a misleading message. A test pins the message
now.

**Gate** (the final tree, before this docs pass; `tests/test_docs.py` re-run after it):
- check_env, ruff and mypy clean on both platforms.
- Linux (380 s of 540): 3.14 full suite 1014 passed, 0 skipped; 3.11 Docker-free 964; stdio 7.
- Windows: 826 passed, plus 168 POSIX/Linux-only skips, none of them for Docker.

**Size:** `src/` +8 lines and `pyproject.toml` +23, the script 290 against about 320,
tests about 453 against about 300, `RELEASE.md` 132 lines and the workflow 33. The
tests are over because of the review's call-site tests.
