# M5 Step 3 plan — split-screen demo, docs sync, `0.2.0` release, Pages deploy, and launch

Status: **signed off by Victor on 2026-11-25 (v2, all S3-D1–S3-D4 = (1)).** Review record: v1 reviewed by `skeptic` (10 findings: 3 high, 5 med, 2 low; verdict `sound with amendments`; all 10 accepted and folded into v2).

Contract: [`docs/plans/M5/M5-visibility-launch.md`](M5-visibility-launch.md) (signed off 2026-11-25: **V6** build & release sequence, **V7b** release verification scope). Release procedure: [`docs/RELEASE.md`](../../RELEASE.md). Branch: **`pcbranch`** (same release branch used for `0.1.0`–`0.1.2`; currently at `60b3eaa`, ahead of `origin/main` at `4d55de4` by M4 README follow-up + M5 Steps 1–2, plus an uncommitted Step 2 commit-hash update in `docs/BUILD_LOG.md`).

---

## 1. Decisions for Step 3 (no silent assumptions)

### S3-D1 — Split-screen demo GIF (`docs/demo-3.gif`) timing
`tests/test_docs.py` pins `docs/demo.gif` and `docs/demo-2.gif` by SHA-256 (neither may change because published `0.1.0`–`0.1.2` PyPI pages load them from `main`), and requires `README.md` to show `_PUBLISHED_GIFS[-1][0]` via `https://raw.githubusercontent.com/victorperson999/Vestibule/main/docs/demo-3.gif` (`< 3,000,000` bytes, `GIF87a`/`GIF89a`). `tests/test_site.py` requires `site/assets/demo.gif` to byte-match `_PUBLISHED_GIFS[-1][0]`.
- **(1) Recommended: Record `docs/demo-3.gif` (Victor) before building the `0.2.0` candidate.** A ~15–20 s split-screen capture (left: fake-key + `1.1.1.1` probe on host then via Vestibule `run_code`; right: `vestibule-mcp ui` live-updating with `isolation:` + full logged code + `/workspace`), recorded on a known OS/backend (prefer WSL2 `isolation: native`, or Windows `isolation: container` — captions must match whichever actually ran). Requires a frame-by-frame privacy check before encoding (no `#<token>` URL fragment or sensitive host paths), ffmpeg 2-pass `palettegen` to `< 3,000,000` bytes, SHA-256 appended to `_PUBLISHED_GIFS`, copied to `site/assets/demo.gif`, and `site/index.html`'s `width`/`height`/`alt`/`<figcaption>` updated (preserving LF line endings).
- **(2) Ship `0.2.0` and deploy `site/` now using `docs/demo-2.gif`; record `docs/demo-3.gif` later.** Fastest path to launch today with zero recording wait; `site/` and GitHub can be updated later (though PyPI `0.2.0`'s frozen description stays on `demo-2.gif` until `0.2.1`).

### S3-D2 — Linking `https://victorperson999.github.io/Vestibule/` from `README.md` / `PYPI.md`
`docs/RELEASE.md` §10 item 4 requires every absolute URL in `PYPI.md` to resolve (HTTP 200) *before* §11 uploads `0.2.0` to PyPI.
- **(1) Recommended: Include `https://victorperson999.github.io/Vestibule/` in `README.md` (and thus `PYPI.md`), and deploy GitHub Pages during Phase C §10 *after* §10 checks 1–3 & 5 pass and right before §10 item 4 (URL check) and §11 upload.** Because the URL is not announced until Phase D, the ~5-minute gap between Pages going live and `uv publish` has zero user impact, and PyPI `0.2.0`'s frozen page permanently carries the live setup-wizard link. (Allow a couple of minutes for initial Pages TLS/CDN propagation before retrying the HTTP 200 check.)
- **(2) Omit the Pages URL from `PYPI.md` for `0.2.0`** and add it only to the GitHub repo's "About" Website URL (and `README.md` after `0.2.0` is live). Avoids deploying Pages before PyPI upload, at the cost of PyPI `0.2.0` not linking to the setup wizard.

### S3-D3 — Post-launch `.github/workflows/pages.yml` trigger
- **(1) Recommended:** Keep `.github/workflows/pages.yml` on `workflow_dispatch`-only during `0.2.0` release. Once `0.2.0` is live on PyPI, land a small post-release commit that adds `push: branches: [main], paths: ["site/**"]` to `pages.yml` **and** updates `tests/test_site.py` (`test_pages_workflow_supply_chain_hygiene`) in the same commit to enforce `workflow_dispatch:` + strictly scoped `push:` (`branches: [main]` and `paths: ["site/**"]`), running pytest before committing.
- **(2) Keep `workflow_dispatch` permanently** (manual click in GitHub Actions whenever you edit `site/`; no `tests/test_site.py` change needed).

### S3-D4 — Legacy `0.1.0`–`0.1.2` PyPI README heading anchors (`d68dbd6` discovery)
`skeptic` caught a real conflict: the frozen PyPI pages for `0.1.0`, `0.1.1`, and `0.1.2` link to 7 heading fragments on `github.com/victorperson999/Vestibule` (recorded in [`docs/plans/M4/M4-followup-readme.md`](../M4/M4-followup-readme.md)): `#what-is-and-isnt-sandboxed`, `#connect-a-client`, `#ubuntu-2310-and-later`, `#configuration`, `#what-setup-does`, `#usage-numbers-and-the-audit-log`, and `#status`. Commit `d68dbd6` on `pcbranch` already reorganized `README.md` and removed 6 of those 7 headings (only `#status` remains; all 7 still exist on `origin/main` at `4d55de4`). `tests/test_docs.py` only checks links in the *current* `README.md`, and `_RAW_LINK_HTML` forbids `<a id="...">` HTML shims.
- **(1) Recommended: Accept breaking the 6 legacy `0.1.0`–`0.1.2` fragment anchors and keep `d68dbd6`'s cleaner `README.md` structure.** `0.1.0`–`0.1.2` had minimal/pre-launch traffic, `0.2.0` is the public launch release, and broken GitHub `#fragment` links degrade safely to the top of `README.md` (not 404). Update `docs/plans/M5/M5-visibility-launch.md` with a one-line amendment note so no doc falsely claims those 6 anchors are preserved.
- **(2) Restore the 6 headings in `README.md`** (updating internal links and `docs/REFERENCE.md` to match) so every fragment link on frozen PyPI `0.1.0`–`0.1.2` pages still jumps to its exact section.

---

## 2. Files to change

Split cleanly per `docs/RELEASE.md` (shipped files belong to the **candidate commit**; release-record edits to `docs/` and post-release `pages.yml` updates happen in **post-candidate commits** that do not invalidate the candidate).

### 2A. Candidate commit files (on `pcbranch`, committed *after* Phase A gates + `critic`/`verifier`)

| File | Action | Purpose |
| --- | --- | --- |
| `pyproject.toml` | edit | Bump `version = "0.1.2"` → `"0.2.0"`. |
| `src/vestibule/__init__.py` | edit | Bump `__version__ = "0.1.2"` → `"0.2.0"`. |
| `docs/demo-3.gif` + `site/assets/demo.gif` | new / update *(if S3-D1 = (1))* | Split-screen demo (`< 3,000,000` bytes, `GIF87a`/`GIF89a`, privacy-audited); `demo.gif` and `demo-2.gif` untouched. |
| `tests/test_docs.py` | edit *(if S3-D1 = (1))* | Append `("docs/demo-3.gif", "<sha256>")` to `_PUBLISHED_GIFS`. |
| `site/index.html` | edit *(if S3-D1 = (1))* | Update `assets/demo.gif` `width`/`height`, `alt`, and `<figcaption>` to match the recorded OS/isolation accurately (keep LF line endings). |
| `README.md` | edit | Document `vestibule-mcp ui` under *Verify it works* and *Configuration and auditing*; add M5 row to `## Status`; link `https://victorperson999.github.io/Vestibule/` (if S3-D2 = (1)); update demo GIF to `https://raw.githubusercontent.com/victorperson999/Vestibule/main/docs/demo-3.gif` + honest caption (if S3-D1 = (1)); apply **S3-D4** choice. Preserve all 5 `scripts/pypi_readme.py` `_ONCE` markers verbatim. |
| `PYPI.md` | regenerate | Generated via `.venv\Scripts\python.exe scripts\pypi_readme.py` (never hand-edited). |
| `SECURITY.md` | edit | Add `### The local web inspector (vestibule-mcp ui)` under `## 5. The audit log` (keeping all §1–§7 numbers intact): opt-in zero-port default, read-only `GET`-only boundary (`405` + `Allow: GET`), `127.0.0.1` + Windows `SO_EXCLUSIVEADDRUSE`, exact `Host` allowlist, `#fragment`/`X-Vestibule-Token`, strict CSP / zero `innerHTML`, 5 s head deadline + `BoundedSemaphore(16)`, honest platform split for audit file reads (POSIX: `O_RDONLY \| O_NOFOLLOW \| O_NONBLOCK \| O_CLOEXEC` + `fstat` `S_ISREG`; Windows: `os.stat(follow_symlinks=False)` before `open()` + `(st_dev, st_ino)` + `S_ISREG` verification on the open handle, noting audit dir is user-owned and not guest-writable), `/workspace` handle/descriptor-relative jail + `U+202E` bidi escaping on both OSes, and honest residuals (`--no-open` on multi-user Linux hosts because browser launcher argv briefly holds `/#<token>`; same-user processes can read `~/.vestibule/audit/` directly). Cite every test with the full `tests/test_ui.py::test_...` prefix (required by `tests/test_docs.py`). Update `**Supported versions:** 0.1.x.` → `0.2.x.`. |
| `docs/REFERENCE.md` | edit | Update line 3 to `written for vestibule-mcp 0.2.0.` (`RELEASE.md` §3); document `vestibule-mcp ui [--port PORT] [--no-open]`, honored env vars (`VESTIBULE_AUDIT_DIR`, `VESTIBULE_WORKSPACE`, `VESTIBULE_ROOTFS_DIR`, `VESTIBULE_MAX_OUTPUT_BYTES` capped at 256 KiB in UI), and read-only guarantees under *Usage numbers and the audit log*. |
| `docs/ARCHITECTURE.md` | edit | Add one concise paragraph under *The audit writer* describing `ui_cli.py` + `src/vestibule/ui/` (passive reader, lazy-imported only for `vestibule-mcp ui`, never in the MCP server). |
| `docs/PLAN.md` | edit | Add Milestone 5 summary pointing to `docs/plans/M5/M5-visibility-launch.md` (`0.2.0` + `site/`), and remove/mark completed the stale `## Remaining manual checks before public launch` section (`pypi.org/project/vestibule-mcp` 404 / `<your-handle>/vestibule`). |
| `docs/plans/M5/M5-visibility-launch.md` | edit *(if S3-D4 = (1))* | Add one-line amendment noting S3-D4 accepts breaking the 6 legacy `0.1.0`–`0.1.2` PyPI README fragment anchors removed in `d68dbd6`. |
| `docs/plans/M5/M5-step3-release-launch.md` | new | Signed Step 3 release & launch plan (referenced by `docs/PLAN.md`). |
| `docs/BUILD_LOG.md` | edit | Include the already-staged Step 2 commit hash `60b3eaa` and record Step 3 sign-off (S3-D1–S3-D4). |

### 2B. Post-candidate commits (do NOT touch shipped files; do NOT invalidate the candidate)
- **Release record commit (after Phase B & Phase C):** Update `docs/BUILD_LOG.md` and `docs/HISTORY.md` with the candidate commit hash, built wheel/sdist SHA-256s, smoke Python/`uv` versions, V7b rationale + live `vestibule-mcp ui` check results, Pages URL, and final M5 acceptance.
- **Post-release auto-deploy commit *(only if S3-D3 = (1))*:** Update `.github/workflows/pages.yml` (`push: branches: [main], paths: ["site/**"]`) and `tests/test_site.py` (`test_pages_workflow_supply_chain_hygiene`) together after `0.2.0` is live on PyPI, and verify pytest passes before committing.

---

## 3. Exact Step 3 execution sequence (aligned with `.claude/WORKFLOW.md` & `docs/RELEASE.md`)

Every step is labeled **(Claude)** or **(Victor)**.

### Phase A — Demo (if S3-D1 = (1)), Docs Sync, Gates, Review, and Candidate Commit (on `pcbranch`)
1. **(If S3-D1 = (1)) Record & encode `docs/demo-3.gif`:**
   - **(Victor)** Record a ~15–20 s split-screen capture on a known OS/backend (prefer WSL2 `isolation: native` or Windows `isolation: container`): left side runs the fake-key + `1.1.1.1` probe directly on host then via Vestibule `run_code`; right side shows `vestibule-mcp ui` live-updating with `isolation:` + full logged code + `/workspace`.
   - **(Victor + Claude)** Inspect frames for privacy (no `#<token>` URL fragment or sensitive host paths), encode via ffmpeg 2-pass `palettegen`/`paletteuse` to `< 3,000,000` bytes, copy to `site/assets/demo.gif`, append SHA-256 to `_PUBLISHED_GIFS` in `tests/test_docs.py`, and update `site/index.html` `width`/`height`/`alt`/`<figcaption>` (LF endings).
2. **(Claude) Apply version bump (`0.2.0`) and docs updates** listed in §2A (`pyproject.toml`, `src/vestibule/__init__.py`, `README.md`, regenerate `PYPI.md` via `scripts/pypi_readme.py`, `SECURITY.md`, `docs/REFERENCE.md`, `docs/ARCHITECTURE.md`, `docs/PLAN.md`, `docs/BUILD_LOG.md`, and `M5-visibility-launch.md` if S3-D4 = (1)).
3. **(Claude) Run BOTH gates *before* `critic` and *before* committing** (matching `.claude/WORKFLOW.md`: `build → gate → critic → verifier`):
   - Windows gate: `check_env`, `ruff check`, `mypy`, `pytest -q`.
   - WSL Linux gate: `scripts/gate-linux.sh` (sequentially after Windows, never in parallel).
4. **(Claude) Run `critic` + `verifier` on the Phase A diff** — `critic` explicitly compares `README.md` vs `SECURITY.md` (and `site/index.html` if edited) so claims never drift or oversell. Fix any confirmed findings and re-run affected gate(s).
5. **(Victor) Commit and push the `0.2.0` candidate on `pcbranch`** (NOT `main` yet, per `RELEASE.md` §3). Claude hands Victor the exact staging list and suggested commit message.

### Phase B — Release Verification (`docs/RELEASE.md` §§1–9)
1. **§1 (`uv`):** **(Victor)** Confirm `uv --version` is current on Windows and WSL Linux.
2. **§2 (Dependency set & container image pinning):** **(Claude + Victor)** Keep the `2026-11-09` digests for `python:3.12-slim` and `node:22-slim` in `src/vestibule/config.py` unless Victor explicitly opts to re-pin (re-pinning edits a shipped file and would require returning to Phase A); re-freeze `requirements/constraints.txt` only if `smoke` below reports dependency drift.
3. **§4 (Verify candidate commit gates):** **(Claude)** Confirm `git status` shows no dirty shipped files and the committed `HEAD` matches the Phase A gate-tested tree (or re-run both gates on `HEAD` if anything changed).
4. **§5 (Secrets scan):** **(Victor)** Run `gitleaks` over full git history (`docker run --rm -v "${PWD}:/repo:ro" zricethezav/gitleaks:v8.30.1 detect --source=/repo --redact --no-banner`) → must exit `0`.
5. **§6 (Build once):** **(Claude)** Run `.venv\Scripts\python.exe scripts\release_smoke.py build`. Copy `vestibule_mcp-0.2.0-py3-none-any.whl` and `vestibule_mcp-0.2.0.tar.gz` **out of `dist/`** into a release folder outside the repo, and record both SHA-256 hashes for `docs/BUILD_LOG.md`.
6. **§7 (`smoke` on Windows + WSL Linux):** **(Claude)** Run `scripts/release_smoke.py smoke <copied wheel>` on BOTH Windows and WSL Linux across Pythons 3.11–3.15.
   - If `smoke` reports dependency drift (`RELEASE.md` §2), re-freeze `requirements/constraints.txt`, rebuild dev venvs, and re-run both gates + `smoke` on the same copied wheel (re-freezing `constraints.txt` does not change shipped files).
7. **§8 (macOS job):** **(Victor)** Trigger GitHub Actions → **macOS check** on `pcbranch` and confirm pass.
8. **§9 (First-run VMs — skipped per signed V7b = (1); replaced by live `vestibule-mcp ui` checks):**
   - **(Claude)** Record honestly in `docs/BUILD_LOG.md` that fresh Hyper-V VM installs were skipped per signed **V7b = (1)**: `setup` (`setup_cli.py`) and both backends are unchanged since `0.1.0` (`0.1.0` VM results stand), while Quick Start wording/commands changed in `d68dbd6` and Step 3's `ui` verify step.
   - **(Claude)** Run a concrete live `vestibule-mcp ui --no-open` verification on **both Windows and WSL Linux** against a real MCP stdio `run_code` call that creates a regular file and a symlink in `/workspace`: confirm (a) `/api/runs` returns the new run within 2 s with the exact expected `isolation:` (`container` on Windows, `native` on WSL Linux) and full logged code, (b) `/api/workspace` lists and reads the regular file, and (c) `/api/workspace` refuses the planted symlink.

### Phase C — Merge, Pre-Upload Checks, Pages Deploy, PyPI Upload, and Post-Upload Smoke (`docs/RELEASE.md` §§10–12)
1. **§10 (Merge & pre-upload checks — in strict order):**
   - **(Victor)** Merge `pcbranch` to `main` and push `main`.
   - **(Claude) §10.1:** Verify `git diff --quiet <candidate> origin/main -- src/vestibule README.md PYPI.md LICENSE pyproject.toml .gitignore` against a **fresh scratch clone** (or immediately after `git fetch origin`), never stale local `origin/main`.
   - **(Claude) §10.2:** Re-hash the copied wheel and sdist and confirm both SHA-256s still match Phase B §6.
   - **(Claude) §10.3:** Run `scripts/release_smoke.py smoke <copied wheel>` **once more** right before upload (guards against upstream transitive dependency drift since Phase B §7).
   - **(Claude) §10.5:** Verify `https://pypi.org/pypi/vestibule-mcp/0.2.0/json` returns `404`.
   - **(Victor, if S3-D2 = (1)) Enable GitHub Pages & deploy `site/`:** Repo Settings → Pages → Source: **GitHub Actions** → Actions → **Deploy static site** (`main`) → Run workflow. Wait for workflow green and initial HTTPS propagation.
   - **(Claude) §10.4:** Fetch every absolute URL in `PYPI.md` (including `https://victorperson999.github.io/Vestibule/` if S3-D2 = (1)) and confirm HTTP 200; fetch every GIF in `_PUBLISHED_GIFS` from `main` and byte-compare against the local repo files.
2. **§11 (PyPI upload — Victor only):**
   - **(Victor)** Run `uv publish <copied wheel> <copied sdist>` using a PyPI token (never bare `uv publish`).
3. **§12 (Post-upload smoke from PyPI & post-release wrap-up):**
   - **(Claude)** Wait 5 minutes for PyPI index propagation, then run `scripts/release_smoke.py smoke vestibule-mcp==0.2.0 --allow-drift` on BOTH Windows and WSL Linux and record the output.
   - **(Victor, if S3-D2 = (2))** Enable GitHub Pages (Settings → Pages → GitHub Actions) and run **Deploy static site** on `main` now.
   - **(Claude + Victor, if S3-D3 = (1))** In a post-release commit, add `push: branches: [main], paths: ["site/**"]` to `.github/workflows/pages.yml` **and** update `tests/test_site.py` (`test_pages_workflow_supply_chain_hygiene`) to enforce `workflow_dispatch:` + strictly scoped `push:` (`branches: [main]`, `paths: ["site/**"]`), run pytest (`tests/test_site.py`), and have Victor commit & push.

### Phase D — Public Launch Pack (per `docs/PLAN.md` §Adoption playbook)
1. **(Claude)** Draft concise, honest, non-hype launch posts (delivered in chat/scratch for Victor to edit and post):
   - **Show HN** (`Show HN: Vestibule – Local kernel-isolated code sandbox for AI agents (MCP)`)
   - **r/ClaudeAI, r/LocalLLaMA, r/mcp** tailored posts
   - **MCP registry / `awesome-mcp` PR blurb**
2. **(Victor)** Set GitHub repo "About" Website URL to `https://victorperson999.github.io/Vestibule/` and confirm topics (`mcp`, `sandbox`, `ai-agents`, `security`, `linux`, `python`).
3. **(Claude + Victor)** Record final M5 acceptance, candidate hash, artifact SHA-256s, and smoke results in `docs/HISTORY.md` and `docs/BUILD_LOG.md` (post-candidate release record commit).

---

## 4. Acceptance criteria (closing M5)

1. `vestibule-mcp==0.2.0` is live on PyPI and passes `scripts/release_smoke.py smoke vestibule-mcp==0.2.0 --allow-drift` on Windows and WSL Linux across all supported Pythons.
2. `https://victorperson999.github.io/Vestibule/` is live on GitHub Pages, renders offline/self-contained with zero console errors, and links back to GitHub and PyPI.
3. `README.md`, `PYPI.md`, `SECURITY.md`, `docs/REFERENCE.md`, `docs/ARCHITECTURE.md`, `docs/PLAN.md`, `docs/HISTORY.md`, and `docs/BUILD_LOG.md` are all in sync (`tests/test_docs.py`, `tests/test_site.py`, `tests/test_pins.py`, `tests/test_release_smoke.py` all green; `SECURITY.md` test citations verified; `docs/demo.gif` and `docs/demo-2.gif` byte-identical to `_PUBLISHED_GIFS`).
4. Both Windows and WSL Linux gates pass on the released commit, `gitleaks` exits `0`, `macOS check` passes, and live `vestibule-mcp ui` checks pass on both Windows and WSL Linux.
5. Launch drafts are ready for Victor to post.
