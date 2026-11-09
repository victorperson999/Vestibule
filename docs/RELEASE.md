# Releasing Vestibule

The checklist for a release (M4 F2). **An upload to PyPI can't be taken back,** so only Victor runs
step 10. Work through the steps in order, and stop at the first one that fails. **If a later step
changes a tracked file, go back to step 3:** the upload must be the commit that step records.

The commands are for PowerShell at the repo root, with the dev venv. On Linux use
`.venv/bin/python`, or the gate's `~/.venvs/vestibule/bin/python`.

| # | Step | Who |
|---|---|---|
| 1 | uv is current | Victor |
| 2 | The dependency set and the images are current | Claude |
| 3 | The release commit is pushed and on `main` | Victor |
| 4 | Both gates are green on that set | Claude |
| 5 | The history has no secrets in it | Victor |
| 6 | `build` | Claude |
| 7 | `smoke`, on Linux and Windows | Claude |
| 8 | The macOS job | Victor |
| 9 | The first-run tests on the VMs | Victor |
| 10 | The upload | Victor |
| 11 | The same `smoke`, from PyPI | Claude |

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
4. If the `mcp` minor moved, `dependencies` in `pyproject.toml` moves with it.

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
   README's two `docker pull` lines and its "as of" date.
6. `pytest -m docker`. On Linux, also `vestibule-mcp setup` and `pytest -m network`.

## 3. The release commit

The working tree is clean, the version in `pyproject.toml` and `src/vestibule/__init__.py` is the one
being released, and the commit is pushed and merged to `main`.

The README is PyPI's description. A published description can't be changed, and its links are
absolute URLs into `main`, so open a few of them and check that they resolve.

Record the commit's hash in `docs/BUILD_LOG.md`. There's no git tag: that entry is what ties a
version to a commit.

## 4. Both gates

Windows and Linux, as [`.claude/WORKFLOW.md`](../.claude/WORKFLOW.md) says, on the set from step 3.

## 5. Secrets in the history

Both the tree and every commit, redacted, from the repo root:

```powershell
docker run --rm -v "${PWD}:/repo:ro" zricethezav/gitleaks:v8.30.1 detect --source=/repo --redact --no-banner
```

Exit 0 means nothing was found. **If it finds anything, stop.** A secret in a public repo's history
is already exposed, so revoke it first, whatever else you do.

## 6. Build

```powershell
.venv\Scripts\python.exe scripts\release_smoke.py build
```

It exports `HEAD` with line endings left alone, builds the sdist and then the wheel from it, and prints
every file in the sdist. Only after every check passes does it put the two files in `dist/`.

## 7. Smoke, on Linux and on Windows

```powershell
.venv\Scripts\python.exe scripts\release_smoke.py smoke dist\vestibule_mcp-0.1.0-py3-none-any.whl
```

In WSL, the same with `~/.venvs/vestibule/bin/python`. It tries Python 3.11 through 3.15 in
throwaway directories, each with `setup`, a real client session and `audit`, and runs
`tests/test_stdio_e2e.py` against that install. It needs Docker running and both images present.
Record the `uv` and Python versions it prints.

If a Python fails here for a reason that can't be fixed, drop its classifier from `pyproject.toml` and
from `PYTHONS` in the script, and go back to step 3.

## 8. The macOS job

On GitHub: Actions → macOS check → Run workflow. It only appears once the workflow file is on `main`.
Container runs stay untested on macOS, and the README says so.

## 9. The first-run tests

M4 contract P6: on fresh VMs at 25 Mbit/s, on this wheel, following only the README. The clock and the
pass rule are in [`M4-adoption.md`](plans/M4/M4-adoption.md).

## 10. Upload

Name the files. With none, `uv publish` takes everything in `dist/`.

```powershell
$token = Read-Host -AsSecureString "PyPI token"
$env:UV_PUBLISH_TOKEN = [Net.NetworkCredential]::new("", $token).Password
uv publish dist/vestibule_mcp-0.1.0-py3-none-any.whl dist/vestibule_mcp-0.1.0.tar.gz
Remove-Item Env:UV_PUBLISH_TOKEN
```

## 11. The same smoke, from PyPI

```powershell
.venv\Scripts\python.exe scripts\release_smoke.py smoke vestibule-mcp==0.1.0 --allow-drift
```

On Linux and on Windows. `--allow-drift` because the release is out by now, so a newer package can
only be reported. Record what it reports.
