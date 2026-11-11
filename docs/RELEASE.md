# Releasing Vestibule

The checklist for a release (M4 F2; the order is M4 step 5's S5-D1). **An upload to PyPI can't be
taken back,** so only Victor runs step 11. Work through the steps in order, and stop at the first
one that fails.

**The candidate is built once, and what is tested is what is uploaded.** Step 6 builds the sdist
and the wheel from the candidate commit and puts them aside. The smoke test, the first-run tests
and the upload all take those two files. Nothing is built again.

**If a later step changes a shipped file, go back to step 3.** The shipped files are
`src/vestibule/`, `README.md`, `PYPI.md`, `LICENSE`, `pyproject.toml` and `.gitignore`. A change
anywhere else (`docs/`, `tests/`, `scripts/`, `requirements/`) doesn't make a new candidate.

**PyPI's page is `PYPI.md`, not the README** (since 0.1.1). It is the README with the status
badge, the early-development warning and the `Status` and `Documentation` sections taken out.
After any edit to the README, run `.venv\Scripts\python.exe scripts\pypi_readme.py` and commit
both files; a doc test fails if they have drifted apart.

The commands are for PowerShell at the repo root, with the dev venv. On Linux use
`.venv/bin/python`, or the gate's `~/.venvs/vestibule/bin/python`.

| # | Step | Who |
|---|---|---|
| 1 | uv is current | Victor |
| 2 | The dependency set and the images are current | Claude |
| 3 | The candidate commit is pushed | Victor |
| 4 | Both gates are green on it | Claude |
| 5 | The history has no secrets in it | Victor |
| 6 | `build`, and the two files put aside | Claude |
| 7 | `smoke`, on Linux and Windows | Claude |
| 8 | The macOS job | Victor |
| 9 | The first-run tests on the VMs | Victor |
| 10 | `main` gets the commit, and the checks before the upload | Victor, then Claude |
| 11 | The upload | Victor |
| 12 | The same `smoke`, from PyPI | Claude |

## 1. uv

`uv --version` should be recent. An old one offers only a beta of the newest Python, and `smoke`
rejects a beta. On Windows: `winget upgrade astral-sh.uv`.

## 2. The dependency set and the images

**The dependency set is due if `smoke` reports any drift** (step 7 says so), after which this step and the ones after it run again.
1. In a fresh venv, install the release wheel alone, with no constraints, and take `pip freeze`.
2. Those packages' lines in `requirements/constraints.txt` take the resolved versions. The dev tools
   and what only they need stay where they are. `pywin32` comes from `uv pip compile --universal`.
3. Rebuild the venvs: `bash scripts/gate-linux.sh --setup` in WSL, and on Windows
   `uv pip install --python .venv\Scripts\python.exe -e ".[dev]" -c requirements/constraints.txt`.
   Windows won't replace files a running server has loaded, so stop any `vestibule-mcp` first.
4. If the `mcp` minor moved, `dependencies` in `pyproject.toml` moves with it. That is a shipped
   file, so it makes a new candidate.

A re-freeze of `requirements/constraints.txt` alone doesn't make a new candidate: run both gates
and `smoke` again on the same two files.

**The images are due when you judge that the tags have moved far enough.** Pinned images get no
upstream fixes until they're re-pinned. For `python:3.12-slim` and `node:22-slim`, following
[M1 step 6's procedure](plans/M1/M1-step6-pinning.md):
1. `docker pull <tag>`, and note the `Digest:` line. It must match an entry of
   `docker image inspect --format "{{json .RepoDigests}}" <tag>`.
2. `docker buildx imagetools inspect docker.io/library/<name>@sha256:<digest>` must show an index
   with `linux/amd64` and `linux/arm64`.
3. `docker image rm <tag> docker.io/library/<name>@sha256:<digest>`, and
   `docker image inspect docker.io/library/<name>@sha256:<digest>` must now fail.
4. `docker pull docker.io/library/<name>@sha256:<digest>`, exactly the README's command, and
   `docker run --rm --pull never <that ref> python -c "print(1)"` (node: `node -e "console.log(1)"`).
5. Paste the digests into `src/vestibule/config.py`, with the date in its comment, and into the
   README's two `docker pull` lines, its `docker image rm` line (under Uninstall) and its "as of"
   date.
6. `pytest -m docker`. On Linux, also `vestibule-mcp setup` and `pytest -m network`.

## 3. The candidate commit

The working tree is clean, the version in `pyproject.toml` and `src/vestibule/__init__.py` is the one
being released, and the commit is pushed to its branch. It doesn't go to `main` yet: `main`'s
README is the one people follow, and it shouldn't describe a release that hasn't passed its
tests. `main` gets it at step 10, just before the upload.

`PYPI.md`, cut from the README, is PyPI's description, and a published description can't be
changed. Two things are true of the README before this commit: private vulnerability reporting is turned on for the repo
(`SECURITY.md` links to it), and each "tested" or "untested" mark in the README's client table
says what really happened.

Record the commit's hash in `docs/BUILD_LOG.md`. There's no git tag: that entry is what ties a
version to a commit.

## 4. Both gates

Windows and Linux, as [`.claude/WORKFLOW.md`](../.claude/WORKFLOW.md) says, on the candidate commit.

## 5. Secrets in the history

Both the tree and every commit, redacted, from the repo root:

```powershell
docker run --rm -v "${PWD}:/repo:ro" zricethezav/gitleaks:v8.30.1 detect --source=/repo --redact --no-banner
```

Exit 0 means nothing was found. **If it finds anything, stop.** A secret in a public repo's history
is already exposed, so revoke it first, whatever else you do.

## 6. Build, and the two files put aside

```powershell
.venv\Scripts\python.exe scripts\release_smoke.py build
```

It exports `HEAD` with line endings left alone, builds the sdist and then the wheel from it, and prints
every file in the sdist. Only after every check passes does it put the two files in `dist/`.

**Copy both files out of `dist/` at once,** to a folder outside the repo, and record their sha256
beside the commit's hash in `docs/BUILD_LOG.md`. `build` empties `dist/` every time it runs, and
two builds of one commit needn't be the same bytes (the build tool writes its own version into the
wheel). Every later step names the copies.

## 7. Smoke, on Linux and on Windows

```powershell
.venv\Scripts\python.exe scripts\release_smoke.py smoke <the copied wheel>
```

In WSL, the same with `~/.venvs/vestibule/bin/python`. It tries Python 3.11 through 3.15 in
throwaway directories, each with `setup`, a real client session and `audit`, and runs
`tests/test_stdio_e2e.py` against that install. It needs Docker running and both images present.
Record the `uv` and Python versions it prints.

If a Python fails here for a reason that can't be fixed, drop its classifier from `pyproject.toml` and
from `PYTHONS` in the script, and go back to step 3.

## 8. The macOS job

On GitHub: Actions → macOS check → Run workflow, choosing the candidate's branch. If the branch
isn't offered, run it on `main` at step 10, before the upload. Container runs stay untested on
macOS, and the README says so.

## 9. The first-run tests

M4 contract P6: on fresh VMs at 25 Mbit/s, on the copied wheel, following only the README. The clock
and the pass rule are in [`M4-adoption.md`](plans/M4/M4-adoption.md), and how they are run is in
[`M4-step5-release.md`](plans/M4/M4-step5-release.md).

**This step is skipped only when nothing a first run touches has changed** since the last
release whose first runs were made: no command in the README's quick start, nothing in `setup`,
nothing in either backend. The record names that release and says why, and that release's
results stand as they were, misses included. The uninstall test is skipped by the same rule,
for the commands under Uninstall.

0.1.1 is skipped this way ([its plan](plans/M4/M4-step5c-pypi-page.md), §6). Against 0.1.0:
the code differs only in the version number; PyPI's description is now cut from the README;
and the README's other edits are two lines in Uninstall's "What stays" list and one sentence
in the speed note. No command changed.

## 10. `main` gets the commit, and the checks before the upload

Victor merges the candidate's branch to `main`. Then, before anything is uploaded:
1. **`main` ships the same files as the candidate.** In a fresh clone of GitHub's `main`, in a
   scratch folder:
   `git diff --quiet <candidate> origin/main -- src/vestibule README.md PYPI.md LICENSE pyproject.toml .gitignore`.
   If it reports a difference, stop: what would be uploaded isn't what `main` holds.
2. **The two files are the recorded ones:** their sha256 match step 6's.
3. **`smoke` once more** on the copied wheel. Drift found here is step 2's.
4. **The page's links work.** The absolute URLs in `PYPI.md` resolve now: fetch each one, and
   the demo GIF's.
5. **The version isn't on PyPI yet:** `https://pypi.org/pypi/vestibule-mcp/<version>/json`
   returns 404. A version that is up can never be uploaded again.

## 11. Upload

Name the two copied files. With none, `uv publish` takes everything in `dist/`.

```powershell
$token = Read-Host -AsSecureString "PyPI token"
$env:UV_PUBLISH_TOKEN = [Net.NetworkCredential]::new("", $token).Password
uv publish <the copied wheel> <the copied sdist>
Remove-Item Env:UV_PUBLISH_TOKEN
```

If it stops with one file up, run it again with the other file named. A file that is up can't be
replaced.

## 12. The same smoke, from PyPI

```powershell
.venv\Scripts\python.exe scripts\release_smoke.py smoke vestibule-mcp==<version> --allow-drift
```

On Linux and on Windows. `--allow-drift` because the release is out by now, so a newer package can
only be reported. Record what it reports.

**If the release from the index is broken,** yank it on PyPI. A yank can be undone, and an install
line with no version then skips it, while the fix goes out as the next version.
