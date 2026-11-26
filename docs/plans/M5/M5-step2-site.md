# M5 Step 2 plan — static landing & config-wizard site (`site/`)

Status: **signed off by Victor on 2026-11-25 (v2).**

Review record: v1 was reviewed by `skeptic` (12 findings: 4 high, 5 medium, 3 low; verdict `sound with amendments`). Checkable repo facts (`requirements/constraints.txt` has no PyYAML; `.github/workflows/macos.yml` pins 40-char SHAs, `persist-credentials: false`, `timeout-minutes`) were confirmed directly. All 12 findings are accepted and folded into v2 below.

Contract: [`docs/plans/M5/M5-visibility-launch.md`](M5-visibility-launch.md) (signed off 2026-11-25: covers **V4** zero-build static site in `site/`, **V5** GitHub Pages hosting, and **V7a** omitting "Early development" on the Hero while keeping **"Not a VM"** prominent). Deployed in Step 3 right after uploading `vestibule-mcp 0.2.0` to PyPI (per **V6**).

---

## 1. Files to create / modify

| File | Action | Purpose |
| --- | --- | --- |
| `site/index.html` | new | Single-page static site (Hero, Interactive Setup & Config Wizard, 4-stage Chamber Walkthrough, Measured Benchmarks & Honest Threat Model, Footer) plus an inert `<script id="vestibule-data" type="application/json">` data block that drives the wizard and lets pure-Python tests check for drift. |
| `site/style.css` | new | Responsive dark systems-tool stylesheet matching `vestibule-mcp ui`'s visual language (375 px mobile to 1440 px desktop, keyboard focus rings, off-screen `.copy-scratch` fallback class, zero external URLs / fonts / `@import`). |
| `site/app.js` | new | Vanilla JS (`"use strict"`): parses `#vestibule-data`, exposes pure deterministic wizard builder functions (testable in Node during dev/build), renders the Setup & Config Wizard with strict numeric validation, clipboard Copy with honest CSS-class fallback, and WAI-ARIA `tablist` Chamber Walkthrough. Zero `fetch()`, zero `innerHTML`/`outerHTML`/`insertAdjacentHTML`/`document.write`/`eval`, zero inline `style` attributes. Works over both `https://` and local `file://`. |
| `site/assets/demo.gif` | new | Self-contained copy of `docs/demo-2.gif` so `site/` renders 100% offline and deploys rooted at `site/` with zero external image loads or broken `../docs/` paths (`docs/demo-2.gif` stays untouched; Step 3 updates to `docs/demo-3.gif`). |
| `site/.nojekyll` | new | Empty file so GitHub Pages serves `site/` verbatim without Jekyll processing. |
| `.github/workflows/pages.yml` | new | Minimal GitHub Pages workflow (`workflow_dispatch` only in Step 2 so pushing Step 2 never triggers a failed deploy or publishes `vestibule-mcp ui` before `0.2.0` is on PyPI) deploying `path: site`, matching `.github/workflows/macos.yml`'s supply-chain rules (40-char commit SHAs, `persist-credentials: false`, `timeout-minutes: 10`, job-scoped `pages: write`/`id-token: write`). |
| `.gitattributes` | edit | Add LF rules for `site/*.html`, `site/*.css`, `site/*.js` (`text eol=lf`) and `site/assets/*.gif -text` so Windows checkouts never introduce CRLF drift. |
| `tests/test_site.py` | new | Pure-Python stdlib pytest suite (`html.parser`, `json`, `tomllib` — zero PyYAML/Node dependency) checking site structure, zero external subresources, zero dangerous DOM sinks, exact data parity with `config.py` / `README.md` / `docs/REFERENCE.md` / `SECURITY.md`, valid GitHub source links, V7a Hero wording, `.github/workflows/pages.yml` hygiene, and `pyproject.toml` + `release_smoke` exclusion of `site/`. |

No changes to `src/`, `pyproject.toml`, `README.md`, `PYPI.md`, or `SECURITY.md` in Step 2 (user-facing docs sync and `0.2.0` release belong to Step 3).

---

## 2. Exact page structure & content contract (`site/index.html`)

### 2.1 Top nav & Hero
- Minimal top bar: **Vestibule** wordmark + anchor links (`Setup`, `How it works`, `Benchmarks`, `Security`) + outbound links to GitHub (`https://github.com/victorperson999/Vestibule`) and PyPI (`https://pypi.org/project/vestibule-mcp/`).
- **Hero:**
  - Headline: *"Run untrusted AI-agent code in a kernel-isolated sandbox on your own machine."*
  - Subhead: *"Free, local, MIT-licensed MCP server — one runtime dependency, no cloud sandbox vendor, no per-second bill."*
  - Quick install strip: `uv tool install vestibule-mcp` with a working **Copy** button + secondary CTA scrolling to `#setup` ("Configure for your OS & client").
  - **Honest boundary callout (V7a):** Prominently states **"Not a VM — shares the host kernel on Linux; sandboxes only code sent to `run_code`"** and links to `SECURITY.md` on GitHub. **Omits** any "Early development" badge or warning (per signed V7a, matching `PYPI.md`).
  - **Demo visual:** `<img src="assets/demo.gif" ...>` with the exact honest caption from `README.md` (same script run directly on host vs. blocked on secret file & `1.1.1.1` inside Vestibule reporting `isolation: native` under WSL2).

### 2.2 Interactive Setup & Config Wizard (`#setup`)
Driven strictly by the JSON inside `<script id="vestibule-data" type="application/json">` (must never contain raw `</script` or `<!--`) so the wizard works over `file://` with zero `fetch()` calls and is verified in pure Python:

- **Controls (accessible native form elements):**
  1. **Platform (`<fieldset>` + `<input type="radio" name="platform">`):**
     - `linux` — **Linux with systemd (Debian / Fedora / Arch)** · badge: `isolation: native` *(Debian 13 measured; Debian 12 / Fedora / Arch expected; arm64 untested)*
     - `wsl2` — **WSL2 (Ubuntu / Debian)** · badge: `isolation: native` *(measured on WSL2 Ubuntu 26.04)*
     - `ubuntu` — **Ubuntu 23.10+ (host or VM, not WSL2)** · badge: `isolation: container` *(measured on 24.04 LTS)*
     - `windows` — **Windows** · badge: `isolation: container` *(measured with Docker Desktop; use WSL2 for native)*
     - `macos` — **macOS** · badge: `isolation: container` *(expected — untested)*
  2. **MCP Client (`<fieldset>` + `<input type="radio" name="client">`):**
     - `claude-code` — **Claude Code**
     - `cursor` — **Cursor** *(tested on Windows)*
     - `claude-desktop` — **Claude Desktop** *(tested on Windows; macOS untested)*
     - `generic` — **Other MCP client**
  3. **Optional settings:**
     - Checkbox: **Mount `/workspace` read-only** (`VESTIBULE_WORKSPACE_RO=1`)
     - Checkbox (shown **only** when `client === "claude-code"`): **Sandbox-only lockdown session** (disables built-in agent shell/file tools so only `run_code` and `read_workspace` remain)
     - Compact optional numeric overrides (pre-filled with `config.Limits` defaults — emitted into `env` / `-e` only when valid AND changed from default):
       - Memory cap (`VESTIBULE_MEM_MB`, default `256`, validated against `/^[1-9][0-9]{0,5}$/`)
       - CPU cap (`VESTIBULE_CPU_PCT`, default `75`, validated against `/^[1-9][0-9]{0,5}$/`)
       - Default timeout (`VESTIBULE_DEFAULT_TIMEOUT_S`, default `10`, validated as integer `1..60`, with an explicit inline note: *clamped by `VESTIBULE_MAX_TIMEOUT_S`, default `60` s*)
     - **Strict validation rule:** if any numeric input is invalid (`0`, negative, decimal, non-digit, or timeout `> 60`), show an inline validation error message beside that field and **refuse to emit the invalid env entry** (prevents generating a config that triggers `Blocked:` on every tool call).

- **Generated outputs (each code block has its own Copy button with honest CSS-class `.copy-scratch` fallback returning `"Copied"` / `"Copy failed"`):**
  1. **Platform prerequisites & caveat box** (verbatim facts from `README.md:57, 64–68, 77`):
     - `linux`: `curl` (minimal Debian: `sudo apt install ca-certificates curl dbus-user-session && sudo reboot`).
     - `wsl2`: `curl` + systemd enabled in WSL2 (no Docker needed).
     - `ubuntu`: `curl` + Docker Engine from **Docker's apt repo**, then `sudo usermod -aG docker $USER` **and log out/in**. Explicitly warns: **do not use the Docker snap** — it rejects `no-new-privileges` — and notes **membership in the `docker` group grants root-equivalent access on Linux**.
     - `windows`: Docker Desktop installed and running; notes `Enable-WindowsOptionalFeature -Online -FeatureName VirtualMachinePlatform -All` in admin PowerShell + reboot if Docker Desktop asks for Virtual Machine Platform, and notes WSL2 for native isolation.
     - `macos`: Docker Desktop for Mac running (explicitly marked **untested**).
  2. **Install & setup commands:**
     - `uv` install (labeled **"Install `uv`, then open a new terminal"** per `README.md:68`):
       - `windows`: `powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"` (`README.md:77` verbatim)
       - `linux`/`wsl2`/`ubuntu`/`macos`: `curl -LsSf https://astral.sh/uv/install.sh | sh`
     - `uv tool install vestibule-mcp`, `uv tool update-shell` (with Windows new-terminal note), `vestibule-mcp setup` (~123 MB pinned runtimes + readiness check).
     - For `claude-code`: exact placement `claude mcp add --scope user vestibule` + optional `-e KEY=VAL` flags (placed **after** `vestibule` and **before** `--`, because `-e` is variadic) + `-- vestibule-mcp` (and on `macos`, notes full binary path from `uv tool dir --bin` if needed).
  3. **MCP config / lockdown block (all 20 `OS × Client` combinations explicitly defined):**
     - `cursor` (`~/.cursor/mcp.json`, tested on Windows): `mcpServers` JSON.
     - `claude-desktop`:
       - `windows`: `%APPDATA%\Claude\claude_desktop_config.json` *(tested)* + `mcpServers` JSON.
       - `macos`: `~/Library/Application Support/Claude/claude_desktop_config.json` *(untested)* + `mcpServers` JSON.
       - `linux`, `wsl2`, `ubuntu`: explicit honest note that Anthropic does not ship an official Linux Claude Desktop build (for `wsl2`, notes Windows Claude Desktop uses the Windows Docker Desktop setup, or recommend Claude Code / Other MCP client), plus the generic `mcpServers` JSON without fabricating a Linux config path.
     - `generic`: `mcpServers` JSON for any MCP client.
     - On `macos` GUI clients (`cursor`, `claude-desktop`): emits full binary path `/Users/you/.local/bin/vestibule-mcp` (noting `uv tool dir --bin`) and explicit `"env": { "PATH": "/usr/local/bin:/usr/bin:/bin", ... }` merged with any toggled `VESTIBULE_*` overrides (`README.md:128–139`).
     - For `claude-code` when **Sandbox-only lockdown** is checked:
       - Emits the exact `--tools "" --allowedTools mcp__vestibule__run_code mcp__vestibule__read_workspace` command — wrapped in `cmd /c --%` when `platform === "windows"`.
       - Emits the stricter `--strict-mcp-config --mcp-config only-vestibule.json --permission-mode dontAsk` command — **also wrapped in `cmd /c --%` when `platform === "windows"`**.
       - Emits a copyable **`only-vestibule.json`** block carrying any active non-default `env` overrides (and macOS `PATH` if applicable), because `--strict-mcp-config` ignores `--scope user`.
       - Displays the mandatory caveat: *Claude Code plugin hooks still run even with `--strict-mcp-config`; disable plugins (`/plugin`) if you don't want hooks running.*
  4. **Verify & watch live:**
     - Ask your agent to run `print(1)` via `run_code`.
     - Verify independently from your terminal with `vestibule-mcp audit`, or launch the live read-only inspector with `vestibule-mcp ui` (written ready for `0.2.0`, since `site/` deploys in Step 3 after `0.2.0` is live on PyPI).

### 2.3 Interactive Chamber Architecture Walkthrough (`#how-it-works`)
- Subtitle explicitly notes: *Native Linux isolation below (no Docker, no root). On Windows, macOS, and stock Ubuntu 23.10+, Vestibule runs a locked-down throwaway container via [`src/vestibule/backends/container.py`](https://github.com/victorperson999/Vestibule/blob/main/src/vestibule/backends/container.py).*
- Full WAI-ARIA `role="tablist"` / `role="tab"` (`aria-selected`, `aria-controls`, roving `tabindex`) / `role="tabpanel"` with keyboard navigation (`ArrowLeft`, `ArrowRight`, `Home`, `End`) across 4 stages:
  1. **MCP Server** (`src/vestibule/server.py`, `src/vestibule/audit.py`)
  2. **Warden Helper** (`src/vestibule/backends/native_helper.py`, `src/vestibule/backends/native_cgroup.py`, `src/vestibule/backends/native_seccomp.py`)
  3. **PID 1 Init** (`src/vestibule/backends/native_helper.py`)
  4. **Guest Process & `/workspace` Doorway** (`src/vestibule/workspace.py`, `src/vestibule/ui_cli.py`)
- Every GitHub `blob/main/...` source link is verified case-sensitively against the repo by `tests/test_site.py`.

### 2.4 Measured Benchmarks & Honest Threat Model (`#benchmarks`, `#security`)
- **Benchmarks (side-by-side numeric table/cards — NO speedup-ratio bar chart or "Nx faster" claim):**
  - Displays the exact 2026-11-06 measured figures from `docs/REFERENCE.md:50-57` (50 `print(1)` runs per backend on WSL2 Ubuntu 26.04, 12 CPUs, Docker Desktop 28.5.1, commit `03f0fb7`):
    - `native`: **118 ms** median · **141 ms** p95
    - `container`: **895 ms** median · **1002 ms** p95
  - Quotes `docs/REFERENCE.md:57` **verbatim**: *"The container path here goes through Docker Desktop's VM, a hop a Docker Engine user on Linux doesn't pay, and each container run also starts one cleanup check (`docker ps`). So these are this host's numbers, not a speed-up factor."* Plus link to `scripts/bench_coldstart.py`.
- **Honest Threat Model (2-column matrix sourced directly from `SECURITY.md` §1):**
  - **What holds (`isolation: native` or `container`, short of a kernel or runtime bug):** host files outside `/workspace` unreachable; no network egress (`lo` only, `AF_VSOCK` blocked on native); memory (no swap), CPU, processes (`pids.max`), `/tmp`, and timeout capped; honest `isolation:` reporting on every run.
  - **What stays possible & what it isn't:** **Not a VM** (shares host kernel on Linux); **only `run_code` is sandboxed** (agent's other tools run on host unless restricted); `/workspace` is writable & uncapped on disk and may contain guest-planted symlinks (refused by `read_workspace` and `vestibule-mcp ui`); run output returns to your agent/model provider; **untested** on arm64 Linux, Podman, and macOS container runs.

---

## 3. GitHub Pages workflow (`.github/workflows/pages.yml`)

- Trigger: **`workflow_dispatch` only** in Step 2 (prevents failed Actions runs before GitHub Pages is switched to "Source: GitHub Actions" and prevents publishing `vestibule-mcp ui` before `0.2.0` is uploaded in Step 3). Step 3 sequences: merge → upload `0.2.0` → enable Pages → trigger deploy (and optionally add `push: paths: ["site/**"]` once `0.2.0` is live).
- Supply-chain hygiene matching `.github/workflows/macos.yml`:
  - Top-level `permissions: { contents: read }`.
  - Single `deploy` job on `ubuntu-latest`, `timeout-minutes: 10`, job-scoped `permissions: { pages: write, id-token: write }`, `concurrency: { group: pages, cancel-in-progress: false }`, `environment: { name: github-pages, url: ${{ steps.deployment.outputs.page_url }} }`.
  - All four actions pinned to full 40-char commit SHAs with `# vX.Y.Z` comments (resolved via `git ls-remote` on 2026-11-25):
    - `actions/checkout@fbc6f3992d24b796d5a048ff273f7fcc4a7b6c09  # v5.1.0` with `persist-credentials: false` (reused verbatim from `.github/workflows/macos.yml`)
    - `actions/configure-pages@45bfe0192ca1faeb007ade9deae92b16b8254a0d  # v6.0.0`
    - `actions/upload-pages-artifact@fc324d3547104276b827a68afc52ff2a11cc49c9  # v5.0.0` (with `path: site`)
    - `actions/deploy-pages@368f82528645a54fb793d4d04e342629a3f51346  # v5.0.1`

---

## 4. Verification plan (`tests/test_site.py` + Node wizard test + browser check)

1. **Pure-Python stdlib pytest suite (`tests/test_site.py` — zero PyYAML/Node dependency, zero skips):**
   - Required files exist (`site/index.html`, `site/style.css`, `site/app.js`, `site/assets/demo.gif`, `site/.nojekyll`, `.github/workflows/pages.yml`).
   - `site/assets/demo.gif` byte-matches the latest published GIF in `tests/test_docs.py` (`test_docs._PUBLISHED_GIFS[-1][0]`).
   - `html.parser.HTMLParser` audit of `site/index.html`:
     - Exactly one inline `<script>` tag (`id="vestibule-data"`, `type="application/json"`, containing zero `</script` or `<!--`), zero `<style>` tags, zero `on*=` attributes, zero `style=` attributes.
     - Zero external subresource loads (`src`/`href` on `<script>`, `<link>`, `<img>`, `<source>`, `<iframe>` starting with `http://`, `https://`, or `//`, checking both single and double quotes), plus CSS `url(http` / `url(//` / `@import` checks across `site/style.css` and `site/app.js`.
     - Every relative local `src`/`href` in `site/index.html` resolves case-sensitively inside `site/`.
   - Static JS audit: zero `innerHTML`, `outerHTML`, `insertAdjacentHTML`, `document.write`, `eval(`, `.style.`, or `setAttribute("style"` in `site/app.js`.
   - Data parity against repo truth:
     - `#vestibule-data` matches `config.Limits` defaults (`mem_mb`, `cpu_pct`, `default_timeout_s`, `max_timeout_s`, `workspace_dir`).
     - Exact `README.md` commands & caveats present: Windows `powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"`, `open a new terminal`, Ubuntu `(host or VM, not WSL2)` + `log out/in` + snap/docker-group warnings, macOS `untested` + `uv tool dir --bin` + `PATH`, Cursor Windows-only tested label, Claude Code lockdown `cmd /c --%` + `/plugin` hooks caveat, and Linux/WSL2/Ubuntu × Claude Desktop honest note.
     - Benchmarks contain `118`, `141`, `895`, `1002`, quote `docs/REFERENCE.md:57` verbatim, and contain zero speedup-ratio claims (`r"\b[0-9]+(?:\.[0-9]+)?\s*[x×]\b"` / `faster than Docker`).
     - Every `https://github.com/victorperson999/Vestibule/blob/main/...` file link in `site/index.html` and `#vestibule-data` resolves case-sensitively in the repo (including `src/vestibule/backends/container.py`).
     - V7a Hero check: Hero contains `Not a VM` and omits `Early development`.
   - Packaging exclusion: parse `pyproject.toml` via `tomllib` to assert `site` is absent from `[tool.hatch.build.targets.sdist].include` and `[tool.hatch.build.targets.wheel]`, AND confirm `release_smoke.sdist_problems` rejects `site/index.html` and `wheel_problems` rejects `site/index.html`.
   - `.github/workflows/pages.yml` stdlib audit: UTF-8, LF line endings, zero tabs, `workflow_dispatch`, no `push:` trigger in Step 2, top-level `contents: read`, job-scoped `pages: write` & `id-token: write`, `timeout-minutes:`, `persist-credentials: false`, `path: site`, and every `uses:` line pinned to a 40-char lowercase hex SHA.
2. **Dev/build runtime verification of `site/app.js` (outside pytest so `gate-linux.sh` has zero skips):**
   - `node --check site/app.js`.
   - Run a small scratch Node test script against `site/app.js`'s exported pure builder functions + parsed `#vestibule-data` verifying all **20 `OS × Client` combinations** (valid JSON output, exact `-e` placement before `--`, `only-vestibule.json` + both `cmd /c --%` commands on Windows lockdown, macOS `PATH` + `VESTIBULE_*` merging, default omission, invalid numeric input rejection without emitting broken env vars, and zero runtime errors).
3. **Browser check + both platform gates:**
   - Preview `site/index.html` in Chrome at desktop (`1280×800`) and mobile (`375×812`) viewports, testing wizard combos, Copy buttons, and Chamber `tablist` keyboard navigation with zero console errors.
   - Run full Windows + WSL Linux gates (`check_env`, `ruff`, `mypy`, `pytest`).
