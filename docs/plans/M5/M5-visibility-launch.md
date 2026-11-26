# M5 contract — local live inspector, static site, and public launch (Path A)

Status: **signed off by Victor on 2026-11-25 (v2, all V1–V7 as recommended).** Amended 2026-11-25 by Step 3 S3-D4 = (1): `README.md` keeps `d68dbd6`'s reorganized headings, accepting that 6 legacy `0.1.0`–`0.1.2` PyPI fragment anchors now land at the top of `README.md`.

Review record: v1 was reviewed by `skeptic` (14 findings: 5 high, 5 medium, 4 low; verdict `rework needed`). `verifier` checked all 8 checkable codebase claims: 8 of 8 `CONFIRMED`. Every finding has been accepted and folded into v2 below. Per the M1–M4 milestone pattern, this document is the **milestone contract** (scope, decisions, invariants, acceptance criteria); once Victor signs off, Step 1 gets its own short, concrete step plan (exact JSON schemas, `workspace.py` dataclass signatures, and a Windows open-append-handle stat spike) reviewed by `skeptic` before any `src/` edit.

Inputs:
- `docs/PLAN.md` §Adoption playbook (~1/3 of the project budgeted for launch, docs, and visual proof).
- `docs/BUILD_LOG.md` open issue (2026-11-11): Claude Code collapses `run_code` output behind "Called vestibule", hiding `isolation:` and `usage:` unless the user runs `vestibule-mcp audit` manually.
- Existing primitives already built and tested:
  - `src/vestibule/audit.py` writes paired `start` → `end` (or `blocked`) JSONL records to `~/.vestibule/audit/` in real time (code up to 4 KiB, usage counters, `limit_hits`, `denied_syscalls`, output sizes, client info).
  - `src/vestibule/workspace.py` implements the descriptor/handle-relative, symlink-refusing, size-capped path jail on both POSIX and Windows.
  - `src/vestibule/server.py:run()` lazily dispatches terminal subcommands (`setup`, `audit`) without importing them in the MCP server process.
- Core constraints from `CLAUDE.md`: tiny dependency tree (keep `mcp` as the sole runtime dependency), stdout purity for the MCP server, unprivileged only, honest security reporting, cross-platform Windows + Linux parity.

---

## 1. Scope

**In:**
1. **`vestibule-mcp ui` (Step 1) — opt-in, zero-dependency, read-only local inspector.**
   - Terminal subcommand (`vestibule-mcp ui [--port PORT] [--no-open]`) that serves a local web dashboard bound strictly to `127.0.0.1` using Python's stdlib `http.server` and static HTML/CSS/JS assets bundled inside `vestibule`.
   - **Strictly passive and read-only:** reads `VESTIBULE_AUDIT_DIR` JSONL files and inspects `VESTIBULE_WORKSPACE` through `workspace.py`'s path jail. Never executes code, never mutates config/audit/workspace files, never creates missing directories, and never runs during normal MCP sessions.
   - **Live visibility:** polls every 1 s, shows completed, blocked, timed-out, orphan-`end`, and unended (`no end`) calls honestly, full logged code (up to the 4 KiB audit cap), `isolation:` + `isolation_detail`, usage counters (`wall`, `cpu`, `mem_peak`, `pids_peak`) with `not_measured` and the M3 whole-sandbox caveat, `limit_hits`, `denied_syscalls`, output byte counts, search/filtering with explicit scope, compact summary stats + inline SVG sparkline, and a safe `/workspace` browser that flags symlinks as refused.
2. **Static website in `site/` (Step 2) — outside the PyPI package.**
   - Plain HTML/CSS/JS static site (`site/index.html`, `site/style.css`, `site/app.js`, `site/assets/demo.gif`) excluded from the PyPI wheel/sdist (`pyproject.toml` already includes only `/src/vestibule`, `/README.md`, `/PYPI.md`, `/LICENSE`, `/pyproject.toml`).
   - Interactive **Setup & Config Wizard** (OS × MCP client × optional limits/lockdown flags → exact copy-paste prerequisites, install commands, and `mcpServers` JSON matching `README.md` and `docs/REFERENCE.md`, including exact `tested`/`untested` labels and caveats).
   - Interactive **Isolation Chamber Walkthrough** (layered view of server → helper → init → guest, linking to source files) + **Measured Benchmarks** (numeric comparison table with the Docker Desktop VM caveat verbatim from `docs/REFERENCE.md`, no misleading speedup-ratio bars) + **Honest Threat Model** summary (from `SECURITY.md`).
3. **Demo, docs sync, `0.2.0` release prep, and launch (Step 3).**
   - New split-screen demo recording saved as `docs/demo-3.gif` (appended to `_PUBLISHED_GIFS` in `tests/test_docs.py`; `docs/demo.gif` and `docs/demo-2.gif` stay untouched so published 0.1.0–0.1.2 PyPI pages never break).
   - Docs update: `README.md` (preserving all 7 heading IDs linked by published PyPI pages), `PYPI.md`, `SECURITY.md` (local HTTP threat model & residuals for `vestibule-mcp ui`, plus `Supported versions: 0.2.x`), `docs/REFERENCE.md`, `docs/ARCHITECTURE.md`, `docs/PLAN.md`, `docs/HISTORY.md`, `docs/BUILD_LOG.md`.
   - PyPI release `0.2.0` via `docs/RELEASE.md` + deploy `site/` + public launch (Show HN, Reddit, MCP registries).

**Out (explicitly deferred):**
- **Running or cancelling code from the UI** (no `POST`/`DELETE`, no control plane, no talking to the MCP server process).
- **A hosted public web playground** (violates `SECURITY.md`: shared host kernel, not a VM).
- **An LLM chat client inside Vestibule** (Claude Code, Cursor, Claude Desktop are already the UI).
- **New runtime dependencies** (no FastAPI, Starlette, Flask, Textual, React, or npm build chain in `vestibule-mcp`).
- **Editing MCP client configs or `.env` files on disk** (the site generates copy-paste text; the user pastes it).
- **Core sandbox changes** (Windows→WSL bridge, macOS native isolation, Landlock, custom package layers, `mcp` 2.x migration) — deferred until after launch feedback.

---

## 2. Decisions (for Victor's sign-off)

### V1 — Local inspector interface
- **(1) Recommended: Read-only local web UI (`vestibule-mcp ui`)** using stdlib `ThreadingHTTPServer` + bundled static HTML/CSS/JS. Zero new dependencies; supports full 4 KiB code inspection, search/filtering, inline SVG sparkline, and a safe `/workspace` browser; pairs visually with Cursor, Claude Desktop, and Claude Code for both daily use and the launch demo.
- **(2) Terminal watch mode (`vestibule-mcp audit --watch`) only.** Zero listening sockets, but cramped for 4 KiB code inspection, filtering, workspace browsing, and non-terminal users.
- **(3) Both `vestibule-mcp ui` and `vestibule-mcp audit --watch`.** Extra surface area for overlapping functionality; can add `--watch` later if requested.

### V2 — Local HTTP security boundary (`vestibule-mcp ui`)
Opening a `127.0.0.1` port introduces two threats that stdio-only Vestibule does not have: (a) browser-based cross-origin / DNS-rebinding attacks from untrusted web tabs, and (b) same-machine local users on shared Linux hosts reading `0600` audit records or workspace files through the HTTP server.
- **(1) Recommended: Ephemeral port + Windows `SO_EXCLUSIVEADDRUSE` + exact `Host` check + per-session bearer token in URL fragment + strict CSP.**
  - Bind strictly to `127.0.0.1`, port `0` by default (`--port PORT` optional, valid range `0..65535`). Set `allow_reuse_address = False`, and on `sys.platform == "win32"` override `server_bind()` to set `socket.SO_EXCLUSIVEADDRUSE` (`1`) before `bind()` so another local process cannot dual-bind and intercept requests.
  - Generate `secrets.token_urlsafe(24)` once at startup. Print/open `http://127.0.0.1:<port>/#<token>`.
  - `GET /`, `GET /app.css`, `GET /app.js`, and `GET /favicon.ico` (`204 No Content`) serve only static assets (zero user data) and require exact `self.headers.get_all("Host") == [f"127.0.0.1:{port}"]` (blocks DNS rebinding and duplicate `Host` headers).
  - All `/api/*` endpoints require BOTH exact `Host` match AND `X-Vestibule-Token: <token>` checked via `hmac.compare_digest` (`403` on mismatch; never accept token in query string).
  - Browser JS reads `location.hash.slice(1)` into memory, strips it from the address bar via `history.replaceState(null, "", "/")`, stores it in `sessionStorage` for same-tab reloads, and sends it in `X-Vestibule-Token`. If opened without a token **or on any `403` from `/api/*`** (e.g., server restarted on the same `--port`), JS clears `sessionStorage`, pauses polling, and shows an inline token prompt.
  - Reject **every** HTTP method except `GET` (`POST`, `PUT`, `PATCH`, `DELETE`, `OPTIONS`, `HEAD`, `TRACE`, or unknown verbs) in one handler entry point with `405 Method Not Allowed` + `Allow: GET` (never `501`).
  - Route **every** response (including `400`/`403`/`404`/`405`/`500` errors and `204`) through one response helper that suppresses the default `Server:` banner and sends:
    - `Content-Security-Policy: default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'`
    - `X-Content-Type-Options: nosniff`
    - `Referrer-Policy: no-referrer`
    - `Cache-Control: no-store`
  - **Zero `innerHTML` / `outerHTML` / `insertAdjacentHTML`** and **zero inline `style` attributes** in `app.js` — DOM built strictly via `createElement` / `createElementNS` + `textContent` + CSS classes (defends against XSS from hostile guest code or workspace files and complies with strict `style-src 'self'`).
  - **Honest residual documented in `SECURITY.md`:** when `vestibule-mcp ui` auto-opens the browser on a multi-user Linux host, the browser launcher command line briefly contains `/#<token>` (visible in `/proc/<pid>/cmdline` unless `hidepid` is enabled). Users on shared hosts can pass `--no-open` and copy the URL from their terminal.
- **(2) Unauthenticated `127.0.0.1` with `Host` check only.** Simpler, but any other local user on a shared Linux host could read `0600` audit records while `ui` is running, weakening M3's documented audit-file permissions.

### V3 — Safe `/workspace` browser inside `vestibule-mcp ui`
- **(1) Recommended: Include read-only `/workspace` tab backed by `src/vestibule/workspace.py`.**
  - Extract a small, structured entry point `inspect_workspace_entry(root: Path, user_path: str, max_bytes: int)` in `src/vestibule/workspace.py` returning typed dataclasses (`WorkspaceDir` | `WorkspaceFile` | `WorkspaceMissing`, raising `WorkspacePathError` for refused paths/symlinks), with `read_workspace_entry` delegating to it so existing formatting, escaping, `max_chars` truncation, `gone` tracking, and `WorkspacePathError` on missing root stay byte-for-byte identical.
  - Zero duplicated jail logic; both POSIX (`dir_fd` + `O_NOFOLLOW` + `O_NONBLOCK`) and Windows (`NtOpenFile` + `FILE_OPEN_REPARSE_POINT`) guarantees apply identically to the UI.
  - Directory entries include both raw `name` (for path navigation) and sanitized `display_name` (escaping `Cc`/`Cf`/`Cs`/`Zl`/`Zp` like `workspace.py:_shown` so `U+202E` right-to-left override cannot visually spoof filenames in the browser).
  - Never creates the workspace directory if missing.
- **(2) Runs view only (omit `/workspace` tab).** Leaves `workspace.py` untouched, but loses safe workspace inspection.

### V4 — Static website stack and repo location
- **(1) Recommended: Zero-build static HTML + CSS + vanilla JS in `site/` inside this repo.**
  - Files: `site/index.html`, `site/style.css`, `site/app.js`, `site/assets/demo.gif` (self-contained so `site/` works offline and deploys rooted at `site/` with zero external requests or broken `../docs/` paths).
  - No `package.json`, no `node_modules`, no build step, zero impact on PyPI sdist/wheel (`pyproject.toml` sdist include list is explicit and guarded by `tests/test_release_smoke.py`).
  - Verifiable via pytest (`tests/test_site.py`) so wizard defaults, platform caveats, and benchmark numbers never silently drift from `config.py`, `README.md`, and `docs/REFERENCE.md`.
- **(2) Framework site (Astro / Next.js) in `site/` or a separate repo.** Richer authoring, but adds a Node toolchain and harder drift checks.

### V5 — Static site hosting
- **(1) Recommended: GitHub Pages** deployed from `site/` (free, tied directly to `victorperson999/Vestibule`, optional custom domain later).
- **(2) Vercel** static deployment (also free, automatic PR preview URLs, simple `vercel.json` pointing to `site/`).

### V6 — Build and release sequence
- **(1) Recommended:** Step 1 (`vestibule-mcp ui`) → Step 2 (`site/` static site) → Step 3 (split-screen `docs/demo-3.gif` + docs sync + `0.2.0` PyPI release + site deploy + public launch).
  - Rationale: `vestibule-mcp ui` makes the launch demo noticeably stronger than a terminal-only GIF and immediately resolves the Claude Code hidden-output UX issue from `docs/BUILD_LOG.md`.
- **(2) Launch on `0.1.2` + `site/` first, then ship `vestibule-mcp ui` as `0.2.0`.** Faster to launch day, but launches without the live inspector in the demo.

### V7 — Public polish choices (`site/` warning badge & `0.2.0` VM checks)
Two explicit calls rather than silent assumptions:
- **V7a (`site/` Hero warning):** Always keep the honest **"Not a VM — shares the host kernel"** boundary. Should the `site/` Hero also carry the **"Early development"** banner, or omit it to match `PYPI.md` (which Victor stripped in `0.1.1`, `docs/BUILD_LOG.md:331`)?
  - **(1) Recommended:** Omit "Early development" on `site/` Hero (matching `PYPI.md`); keep the honest threat-model summary prominent.
  - **(2)** Include the "Early development" banner on `site/` Hero (matching `README.md`).
- **V7b (`docs/RELEASE.md` §9 fresh-VM checks for `0.2.0`):** `0.2.0` does not change `setup`, native isolation, or container backends — only adds the `ui` subcommand and refactors `workspace.py`.
  - **(1) Recommended:** Run full Windows + WSL Linux gates, `release_smoke.py` wheel smoke across Pythons, and live `vestibule-mcp ui` verification on Windows + WSL Linux; skip fresh Hyper-V VM OS installs for `0.2.0` (noting why in the release record).
  - **(2)** Also run fresh Hyper-V VM installs (Debian / Ubuntu / Windows) as in `0.1.0`.

---

## 3. High-level design & invariants — Step 1 (`vestibule-mcp ui`)

*(Exact JSON schemas, `workspace.py` dataclass signatures, and test matrix will be pinned in the dedicated Step 1 plan reviewed by `skeptic` after sign-off.)*

### 3.1 CLI, server lifecycle & terminal hygiene
- Dispatch in `src/vestibule/server.py:run()`:
  - `if args[:1] == ["ui"]:` lazy-import `vestibule.ui_cli` and `sys.exit(ui_cli.main(args[1:]))`.
  - Update the unexpected-args warning string to name `"setup"`, `"audit"`, and `"ui"` — and update the matching assertion in `tests/test_rootfs.py`.
  - Normal MCP server startup never imports `ui_cli` or `http.server` (Golden Rule 1 preserved).
- CLI args (exit code `2` on bad args): `--port PORT` (`0..65535`, default `0`), `--no-open`.
- Terminal output goes to `stderr` only, applying `audit_cli.py`'s exact hygiene (`_ascii_unless_utf8(sys.stderr)`, `_clean(...)` on all dynamic paths/warnings, and guarding `sys.stderr is not None`).
- **Browser launch (when `--no-open` is absent):**
  - On Linux (`sys.platform.startswith("linux")`), skip auto-opening whenever neither `DISPLAY` nor `WAYLAND_DISPLAY` is set (prevents `webbrowser.open()` from launching `lynx`/`w3m`/`links` attached to the user's TTY on WSL2/SSH/minimal Debian) and print a manual-open note to stderr instead.
  - Otherwise launch in a daemon thread after the listening socket is bound; print a manual-open hint if `webbrowser.open` returns `False` or raises.
- **Server concurrency, timeouts & deadlock-free shutdown:**
  - `ThreadingHTTPServer(("127.0.0.1", port), handler)` with `daemon_threads = True`, `allow_reuse_address = False`, and `SO_EXCLUSIVEADDRUSE` on Windows.
  - Bound concurrency via `threading.BoundedSemaphore(16)` in `process_request` (reject excess connections immediately instead of spawning unbounded OS threads).
  - Keep `HTTP/1.0` (no keep-alive), set per-socket timeout (`5.0 s`) + total request deadline, and silence `log_message()`.
  - Run `serve_forever(poll_interval=0.2)` in a **daemon thread**; the main thread waits on a `threading.Event` in a short-timeout loop (so Windows `KeyboardInterrupt` stays responsive). On `KeyboardInterrupt` or `SIGTERM` (handler only sets the event, never calls `shutdown()` directly — avoiding the POSIX `shutdown()` deadlock), the main thread calls `server.shutdown()` and `server.server_close()` and exits `0`.

### 3.2 Environment & config reading
- Matches `audit_cli.py`: reads `VESTIBULE_AUDIT_DIR`, `VESTIBULE_WORKSPACE`, `VESTIBULE_ROOTFS_DIR`, and `VESTIBULE_MAX_OUTPUT_BYTES` directly from `os.environ` without failing on unrelated invalid `VESTIBULE_*` settings.
- Uses `config.audit_dir(limits)` for the audit directory (exit code `1` on invalid path/missing home) and `config._audit_problem(limits, explicit=True)` for the non-fatal overlap warning.
- Validates `VESTIBULE_MAX_OUTPUT_BYTES` with the same positive-integer rule (falling back to `Limits.max_output_bytes = 20_000` with a sanitized stderr warning if invalid).

### 3.3 Audit reader, caching & honesty
- Scans `audit_dir` using `os.scandir` matching `[0-9]{8}T[0-9]{6}Z-[0-9]+\.jsonl` and refusing symlinks. Missing directory returns empty runs + `audit_dir_exists: false` (never creates it).
- **Safe file opening & Windows live-stat:**
  - Cache key uses `os.stat(path, follow_symlinks=False)` (NOT `DirEntry.stat()`, whose `st_ino` is `0` on Windows and whose NTFS directory metadata can lag behind open append handles). Step 1's plan will include a quick Windows spike confirming live append visibility against an open `AuditLog` handle (and if `os.stat` lags on an active open handle on Windows, re-stat via `os.fstat` on the newest file or handle accordingly).
  - On POSIX, open with `O_RDONLY | O_NOFOLLOW | O_NONBLOCK | O_CLOEXEC` and verify `stat.S_ISREG(os.fstat(fd).st_mode)` (prevents a planted FIFO matching the `.jsonl` regex from hanging the server). On Windows, check non-reparse + `S_ISREG`.
  - All disk I/O happens **outside** `threading.Lock`; the lock protects only cache dictionary updates and the cached merged snapshot.
  - Per-line byte cap (`_MAX_LINE_BYTES = 65_536`): if a line hits the cap without `\n`, drain to the next `\n`/EOF and count it as **one** skipped line (never splitting chunks into `json.loads`).
- **Pairing, honesty & performance:**
  - Pair `start` + `end` per file by `id`, preserving `blocked` records AND orphan `end` records (`"(no start record found)"`), matching `audit_cli.py`.
  - **No `in flight?` guesswork:** a `start` with no `end` is labeled factually `no end` with its elapsed age since `start.ts` (if valid ISO timestamp) and the exact M3 tooltip (*"still running, the server stopped, or the record was lost"*).
  - Surface `not_measured` (always relevant on Windows/Docker where `cpu`/`mem_peak`/`pids_peak` are null) and the M3 U5 whole-sandbox note (*"counts the whole sandbox, including its setup and file cache"*) in both the detail drawer and stats strip.
  - Cache the merged/sorted snapshot (recomputed only when a file changes/appears/disappears) so idle 1 s polls do zero re-sorting. Make filter/search scope explicit (server-side query filtering or clearly labeled `"filtering latest N calls"`).

### 3.4 Workspace endpoint (`GET /api/workspace?path=...`)
- Runs `workspace.split_relpath(rel_path)` **first** so traversal/invalid paths (`../etc/passwd`, `C:\...`, NUL, reserved device names) always return `kind: "refused"`, even when the workspace root does not exist.
- Calls `workspace.inspect_workspace_entry(workspace_path, rel_path, max_bytes)` (which handles missing root as `WorkspaceMissing(root_exists=False)` without creating it, while `read_workspace_entry` keeps raising `WorkspacePathError`).
- Catches raw `OSError` and `RuntimeError` (unexpandable `~` / missing `HOME`) cleanly, returning a structured error response without crashing the handler.
- Preserves exact `read_workspace_entry` byte-for-byte behavior (existing `tests/test_workspace.py` and `tests/test_acceptance.py` unchanged).
- Returns both raw `name` and escaped `display_name` (`Cc`/`Cf`/`Cs`/`Zl`/`Zp`) for directory entries so browser rendering cannot be spoofed by `U+202E` bidi characters.

### 3.5 Bundled UI assets & packaging hygiene
- Files under `src/vestibule/ui/`: `__init__.py`, `index.html`, `app.css`, `app.js`, loaded via `importlib.resources.files("vestibule.ui")`.
- Add `src/vestibule/ui/** text eol=lf` to `.gitattributes`.
- Extend `scripts/release_smoke.py`:
  - Add the UI assets to `_WHEEL_REQUIRED`.
  - Extend `wheel_problems()` CRLF check from `.py` only to `.py`, `.html`, `.css`, `.js`.
  - Add a quick `vestibule-mcp ui` smoke check (e.g., bad-args exit `2` + ephemeral `--port 0 --no-open` bind/health/shutdown or module resource load) to `smoke` so wheel packaging is verified on every supported Python.
- Zero external CDN requests (100% offline).

---

## 4. High-level design — Step 2 (`site/` static website)

- Files: `site/index.html`, `site/style.css`, `site/app.js`, `site/assets/demo.gif`.
- Strictly excluded from PyPI sdist/wheel (`pyproject.toml` untouched; verified by `tests/test_release_smoke.py`).
- Four focused sections:
  1. **Hero:** One-sentence value prop, install command (`uv tool install vestibule-mcp`), self-contained `site/assets/demo.gif`, GitHub/PyPI links, honest **"Not a VM"** boundary (plus optional "Early development" badge per V7a).
  2. **Interactive Setup & Config Wizard:**
     - Inputs: OS (`Linux (Debian / Fedora / Arch)`, `WSL2`, `Ubuntu 23.10+`, `Windows`, `macOS`), Client (`Claude Code`, `Cursor`, `Claude Desktop`, `Other MCP client`), optional toggles (`Read-only workspace`, `Sandbox-only lockdown` — **shown only for Claude Code**, `Custom limits`: memory / CPU / timeout).
     - Preserves every exact `README.md` / `SECURITY.md` caveat:
       - `tested` vs `untested` status (macOS container & Claude Desktop untested; Cursor tested on Windows only),
       - macOS GUI full binary path (`uv tool dir --bin`) + explicit `PATH`,
       - Ubuntu 23.10+ apt-only Docker Engine (no snap) + `docker`-group root-equivalence warning,
       - Claude Code lockdown PowerShell `cmd /c --%` wrapper on Windows + plugin-hooks caveat (`/plugin`).
     - Copy buttons + `vestibule-mcp ui` / `vestibule-mcp audit` verification step.
  3. **Chamber Architecture Walkthrough:** Interactive 4-stage diagram (`MCP server` → `warden helper` → `init PID 1` → `guest`) + `/workspace` doorway, linking directly to the real source files on GitHub.
  4. **Benchmarks & Threat Model:** Side-by-side numeric table/cards of the 2026-11-06 measured cold-start numbers (`118 ms` median / `141 ms` p95 native vs `895 ms` / `1002 ms` container) with **no speedup-ratio bar chart** and the exact Docker Desktop VM + `docker ps` caveat from `docs/REFERENCE.md`, plus the concise "Protected vs. Not protected" matrix from `SECURITY.md`.

---

## 5. Build steps & verification (goal-driven)

1. **Step 1 — `vestibule-mcp ui` (`src/` + `tests/`):**
   - Write concrete Step 1 plan (exact JSON schemas, `workspace.py` dataclass signatures, Windows open-append-handle spike) → `skeptic` review → Victor sign-off → build → Windows + WSL Linux gates (`check_env`, `ruff`, `mypy`, `pytest`) → `critic` + `verifier` → live check alongside Claude Code.
   - Includes updating `tests/test_rootfs.py`, `.gitattributes`, `scripts/release_smoke.py`, and `tests/test_release_smoke.py`.
2. **Step 2 — Static website (`site/`):**
   - Build `site/{index.html,style.css,app.js,assets/demo.gif}` + `tests/test_site.py` (drift checks against `config.py` defaults, `README.md` client paths/caveats, zero external CDN URLs, and confirming `site/` stays out of sdist/wheel).
   - Verify in browser on desktop + mobile viewports across every OS × Client wizard combination; run tests.
3. **Step 3 — Split-screen demo, docs sync, `0.2.0` release, and launch:**
   - Record split-screen demo as `docs/demo-3.gif` (appended to `_PUBLISHED_GIFS`; `demo.gif` and `demo-2.gif` untouched).
   - Update `README.md` (keeping all 7 published PyPI heading IDs intact), regenerate `PYPI.md`, update `SECURITY.md` (`vestibule-mcp ui` threat model & residuals + `Supported versions: 0.2.x`), `docs/REFERENCE.md`, `docs/ARCHITECTURE.md`, `docs/PLAN.md`, `docs/HISTORY.md`, `docs/BUILD_LOG.md`.
   - Bump to `0.2.0`, run `docs/RELEASE.md` checklist (per V7b), Victor uploads to PyPI, deploy `site/` (per V5), execute public launch posts.

---

## 6. Acceptance criteria

1. **Zero new runtime dependencies:** `pyproject.toml` `[project].dependencies` stays `mcp` only; `vestibule-mcp` installs cleanly with `uv tool install`.
2. **MCP server isolation & stdout purity untouched:** normal `vestibule-mcp` stdio startup does not import `ui_cli` or `http.server` and opens no listening sockets.
3. **Local HTTP hardening proven by tests:** wrong/missing/duplicate `Host` → `403`, missing/wrong token → `403`, all non-`GET` methods → `405` + `Allow: GET`, all 4 security headers present on every response including errors, `Server:` banner suppressed, zero `innerHTML`/inline `style` in `app.js`, concurrency cap enforced, `SO_EXCLUSIVEADDRUSE` set on Windows, FIFO/symlink audit files refused, and `workspace.py` symlink/traversal/bidi defenses verified on both Windows and Linux.
4. **Live visibility proven end-to-end:** running `run_code` through a live MCP client appears in `vestibule-mcp ui` within 2 s with full logged code, `isolation:`, and usage counters (or honest `not_measured`), and files written to `/workspace` appear in the Workspace tab (with planted symlinks shown as refused).
5. **Passive & non-destructive:** running `vestibule-mcp ui` against nonexistent `~/.vestibule/audit` or `~/.vestibule/workspace` creates neither directory and displays a clear empty state; invalid paths still return `"refused"`.
6. **Static site works offline/zero-build and stays outside PyPI:** `site/` renders cleanly on desktop & mobile with zero external requests, every OS × Client wizard combination matches `README.md` caveats, and `release_smoke.py` confirms `site/` is absent from sdist and wheel.
7. **Honest documentation & intact PyPI links:** `SECURITY.md` documents `vestibule-mcp ui`'s local HTTP boundary, `--no-open` multi-user residual, and `0.2.x` support; `docs/demo.gif` and `docs/demo-2.gif` remain byte-identical; `site/` quotes cold-start numbers with the Docker Desktop VM caveat verbatim and no speedup-ratio claim.
8. **Both gates green:** `check_env`, `ruff`, `mypy`, and `pytest` pass on Windows and WSL Linux.
