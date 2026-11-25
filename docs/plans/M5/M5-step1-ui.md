# M5 Step 1 plan — `vestibule-mcp ui` (local read-only inspector)

Status: **signed off by Victor on 2026-11-25 (v2).**

Review record: v1 was reviewed by `skeptic` (12 findings: 4 high, 5 medium, 3 low; verdict `sound with amendments`). `verifier` checked all 5 checkable codebase/CPython claims: 5 of 5 `CONFIRMED`. All 12 findings are accepted and folded into v2 below.

Contract: [`docs/plans/M5/M5-visibility-launch.md`](M5-visibility-launch.md) (signed off 2026-11-25: V1–V3, V6, V7).

---

## 1. Spike measurements (2026-11-25, Windows 11, Python 3.11.9 AMD64)

Measured outside the repo before writing this plan:
1. **Cross-process live append visibility on Windows NTFS:** A child process opened a `.jsonl` file with `AuditLog`'s exact flags (`O_RDWR | O_APPEND | O_CREAT | O_BINARY`) and performed two `os.write()` calls without closing the descriptor. The parent process calling `os.stat(p, follow_symlinks=False)` observed `st_size` jump immediately from `8` → `22` with a real non-zero `st_ino` (`42502721483781032`), whereas `DirEntry.stat(follow_symlinks=False).st_ino` returned `0`.
   - **Design consequence:** `_AuditStore` uses `os.stat(path, follow_symlinks=False)` (never `DirEntry.stat()`) for cache signatures and Windows TOCTOU checks.
2. **Windows port exclusivity (`SO_EXCLUSIVEADDRUSE`):** A server socket with `setsockopt(SOL_SOCKET, SO_EXCLUSIVEADDRUSE, 1)` bound to `("127.0.0.1", 0)` blocked a second socket using `SO_REUSEADDR` from binding the same address/port with `OSError: [WinError 10013]`.
   - **Design consequence:** `_UiServer.server_bind()` sets `SO_EXCLUSIVEADDRUSE` on `sys.platform == "win32"` in addition to `allow_reuse_address = False`.

---

## 2. Files to change

| File | Action | Purpose |
| --- | --- | --- |
| `src/vestibule/workspace.py` | edit | Add structured `WorkspaceEntry`, `WorkspaceDir`, `WorkspaceFile`, `WorkspaceMissing` + `inspect_workspace_entry()`; unify POSIX/Windows traversal through one `_inspect(..., max_chars=...)` so `read_workspace_entry()` stays byte-for-byte identical. |
| `src/vestibule/server.py` | edit | Dispatch `args[:1] == ["ui"]` lazily to `vestibule.ui_cli.main(args[1:])`; update unexpected-args warning string to name `"setup"`, `"audit"` and `"ui"`. |
| `src/vestibule/ui_cli.py` | new | CLI arg parser, `_AuditStore` reader/cache, hardened `127.0.0.1` read-only HTTP server (`_UiServer` / `_UiHandler`), `/api/meta`, `/api/runs`, `/api/workspace`, static asset serving, browser launcher, deadlock-free shutdown (`_serve`). |
| `src/vestibule/ui/__init__.py` | new | Empty package marker so `importlib.resources.files("vestibule.ui")` resolves inside wheels and source trees. |
| `src/vestibule/ui/index.html` | new | Static HTML shell (zero user data, zero inline `<script>`/`<style>`, zero external URLs). |
| `src/vestibule/ui/app.css` | new | Minimal systems-tool stylesheet (no external fonts/CDNs; covers all layout and status badges so `app.js` uses zero inline `style` attributes). |
| `src/vestibule/ui/app.js` | new | Token lifecycle (`#fragment` → `replaceState` → `sessionStorage` → `X-Vestibule-Token`, `403` recovery prompt), 1 s live polling with pause/resume, Runs list + detail drawer + server-side filter/search + inline SVG sparkline, Workspace browser with symlink refusal banner and `URLSearchParams` encoding. Zero `innerHTML`/`outerHTML`/`insertAdjacentHTML` and zero inline `style`. |
| `.gitattributes` | edit | Add `src/vestibule/ui/** text eol=lf`. |
| `scripts/release_smoke.py` | edit | Add the four `vestibule/ui/*` files to `_WHEEL_REQUIRED`, extend `wheel_problems()` CRLF check to `.py`, `.html`, `.css`, `.js`, and add `vestibule-mcp ui --bad-flag` + `ui_cli._load_assets()` wheel checks in `_smoke_one()`. |
| `tests/test_rootfs.py` | edit | Update line 109–110 unexpected-args assertion to match `"setup", "audit" and "ui"` and add `test_ui_runs_the_ui_command`. |
| `tests/test_release_smoke.py` | edit | Update `REQUIRED` with the four `vestibule/ui/*` files, add CRLF tests for `.html`/`.css`/`.js`, update `machine` fixture (distinguishing the two `-c` calls and handling `ui --bad-flag`) and `machine.ran` expectations, and add failure-case tests for both new `ui` smoke checks. |
| `tests/test_ui.py` | new | Full unit + integration + security test suite for `ui_cli.py`, `inspect_workspace_entry`, HTTP hardening, audit store/cache, and CLI lifecycle. |

No changes to `pyproject.toml` dependencies (`mcp` remains the sole runtime dependency). No MCP tool schema changes.

---

## 3. Exact `workspace.py` contract

Add four frozen dataclasses and one public function to `src/vestibule/workspace.py`:

```python
@dataclass(frozen=True)
class WorkspaceEntry:
    name: str            # raw UTF-8 (errors="replace") entry name for path navigation
    display_name: str    # _shown(name): Cc/Cf/Cs/Zl/Zp escaped, '\\' doubled (bidi/control safe)
    kind: str            # "dir" | "file" | "symlink" | "other" | "unknown"
    size: int | None     # None when stat failed (rendered as "?" in text listings)

@dataclass(frozen=True)
class WorkspaceDir:
    entries: tuple[WorkspaceEntry, ...]  # up to _MAX_DIR_ENTRIES (200), sorted by name
    total_scanned: int                   # entries scanned before [:200], <= _MAX_SCAN (10_000)
    gone: int                            # entries in [:200] that vanished before cutoff
    over_scan: bool                      # True if directory held > _MAX_SCAN entries

    @property
    def hidden(self) -> int:
        # Matches workspace.py:144 (`over`: the one entry read to learn there were > _MAX_SCAN).
        return self.total_scanned + int(self.over_scan) - len(self.entries) - self.gone

@dataclass(frozen=True)
class WorkspaceFile:
    content: str                         # UTF-8 (errors="replace"), capped at max_bytes
    truncated: bool                      # True if file exceeded max_bytes
    max_bytes: int

@dataclass(frozen=True)
class WorkspaceMissing:
    root_exists: bool                    # False when workspace root does not exist
    error: str | None = None             # underlying FileNotFoundError text when root_exists=False
```

### Single internal traversal & byte-for-byte parity
- One internal helper `_inspect(real_root: str, parts: list[str], max_bytes: int, *, max_chars: int | None) -> WorkspaceDir | WorkspaceFile | None` backs both `inspect_workspace_entry` (`max_chars=None`) and `read_workspace_entry` (`max_chars=max_bytes`).
- Inside `_collect_dir(entries, describe, max_chars, over)`:
  - Sorts scanned entries by decoded name, iterates `entries[:_MAX_DIR_ENTRIES]`, calls `describe(key)` lazily (skipping and counting `gone` on `None`).
  - When `max_chars is not None`, computes the rendered line length `len(f"{kind:<8}{size_str:>12}  {display_name}")` and stops immediately when `length > max_chars` — preserving zero extra `os.stat` calls on POSIX and exact `gone`/`hidden` counts on truncated listings (`test_a_listing_fits_the_cap`).
- `inspect_workspace_entry(root: Path, user_path: str, max_bytes: int) -> WorkspaceDir | WorkspaceFile | WorkspaceMissing`:
  1. Calls `parts = split_relpath(user_path)` **first** — traversal (`..`), absolute paths, `:`, NUL, reserved names, trailing dots/spaces ALWAYS raise `WorkspacePathError`, even if `root` does not exist.
  2. Calls `root.resolve(strict=True)`:
     - On `FileNotFoundError as e`: check `if root.is_symlink(): raise WorkspacePathError(f"workspace root is not accessible: {e}") from e` (a dangling symlink at `VESTIBULE_WORKSPACE` is refused, never reported as "will be created"); otherwise return `WorkspaceMissing(root_exists=False, error=str(e))`.
     - On any other `OSError as e`: raise `WorkspacePathError(f"workspace root is not accessible: {e}") from e`.
  3. Calls `_inspect(str(real_root), parts, max_bytes, max_chars=None)`; returns `WorkspaceMissing(root_exists=True)` if `None`.
- `read_workspace_entry(root: Path, user_path: str, max_bytes: int) -> str`:
  - Calls `parts = split_relpath(user_path)`, resolves `root.resolve(strict=True)` (raising `WorkspacePathError` on any `OSError`, including `FileNotFoundError`), calls `_inspect(str(real_root), parts, max_bytes, max_chars=max_bytes)`, and renders the exact existing text format (`(empty directory)`, `hidden`, `over_scan`, `_ESCAPE_NOTE`, `...[truncated: file exceeds {max_bytes} bytes]`, `Not found: {user_path or '.'}`).
  - Every existing test in `tests/test_workspace.py` and `tests/test_acceptance.py` must pass with zero modifications.

---

## 4. Exact `ui_cli.py` design

### 4.1 CLI parsing, config, and stderr hygiene
- Entry point: `main(args: list[str]) -> int`.
- Calls `_ascii_unless_utf8(sys.stderr)` at startup (same helper as `audit_cli.py`). Every dynamic string written to stderr passes through `_clean(...)`, and `_err()` checks `if sys.stderr is not None`. Nothing in `ui_cli.py` writes to `sys.stdout`.
- Strict manual arg parser (exit code `2` + usage line on stderr on any bad/unknown/duplicate arg):
  - `--port <N>` or `--port=<N>`: ASCII digits `[0-9]{1,5}`, `0 <= int(N) <= 65535` (default `0`).
  - `--no-open`: boolean flag (default `False`).
  - Usage: `usage: vestibule-mcp ui [--port PORT] [--no-open]  (PORT: 0-65535, default 0)`
- Config from `os.environ` (same tolerance as `audit_cli.py`):
  - `audit_dir` resolved via `config.audit_dir(limits)`: if `None`, print sanitized error and return `1`.
  - `audit_warning = config._audit_problem(limits, explicit=True)`: if set, print warning to stderr and surface in `/api/meta`.
  - `workspace_path`: try `limits.workspace_path`; catch `RuntimeError as e` → set `workspace_path = None, workspace_error = str(e)` (so the Runs view still works when `HOME` is unset and only `VESTIBULE_AUDIT_DIR` is set).
  - `max_preview_bytes`: parse `VESTIBULE_MAX_OUTPUT_BYTES` with positive ASCII whole-number validation (invalid → sanitized warning to stderr + default `20_000`), capped at `_UI_MAX_PREVIEW_BYTES = 256 * 1024` (256 KiB).

### 4.2 HTTP server hardening (`_UiServer` & `_UiHandler`)
- `_UiServer(ThreadingHTTPServer)`:
  - `daemon_threads = True`, `allow_reuse_address = False`.
  - `server_bind(self)`: on `sys.platform == "win32"`, set `self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)` before `super().server_bind()`.
  - Concurrency cap: `self._slots = threading.BoundedSemaphore(16)`. In `process_request(self, request, client_address)`:
    - If `not self._slots.acquire(blocking=False)`: immediately `self.shutdown_request(request)` and return without spawning a thread.
    - Spawn daemon thread running `try: self.process_request_thread(request, client_address) finally: self._slots.release()`.
    - Wrap `t.start()` in `try ... except BaseException: self._slots.release(); self.shutdown_request(request); raise` so a thread-creation failure never leaks a slot or socket.
  - Override `handle_error(self, request, client_address)`: silently ignore `BrokenPipeError`, `ConnectionResetError`, `ConnectionAbortedError`, `TimeoutError`; log any other exception via `_err(_clean(...))` (never calling `BaseServer.handle_error`, which prints raw tracebacks to `sys.stdout` when `sys.stderr is None`).
  - Bind failure (`OSError`) prints sanitized error to stderr and returns `1`.
- `_UiHandler(BaseHTTPRequestHandler)`:
  - `default_request_version = "HTTP/1.0"` and `protocol_version = "HTTP/1.0"` (no keep-alive; prevents `BaseHTTPRequestHandler`'s `"HTTP/0.9"` default from silencing headers on parse errors).
  - `server_version = ""`, `sys_version = ""`, `log_message = no-op`.
  - `_send(self, status: int, body: bytes, content_type: str, *, extra_headers: dict[str, str] | None = None) -> None`:
    - Writes the `HTTP/1.0 <status> <phrase>\r\n` status line and headers **directly** to `self.wfile` (never emits `Server:`, never suppressed by `request_version == "HTTP/0.9"`):
      - Always includes:
        - `Content-Security-Policy: default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'`
        - `X-Content-Type-Options: nosniff`
        - `Referrer-Policy: no-referrer`
        - `Cache-Control: no-store`
        - `Connection: close`
      - When `status != 204`: includes `Content-Type: <content_type>` and `Content-Length: <len(body)>` (omits both on `204 No Content`, per RFC 9110 §8.6).
      - Includes any `extra_headers` (e.g. `Allow: GET`).
      - Writes `body` ONLY when `body` is non-empty AND `getattr(self, "command", None) != "HEAD"`.
  - Override `send_error(self, code, message=None, explain=None)` to delegate to `_send(code, f"{code} {message or ''}\n".encode("utf-8", "replace"), "text/plain; charset=utf-8")`.
  - Override `handle_one_request(self)` with a **true total request deadline** (`_REQUEST_DEADLINE_S = 5.0`, `_MAX_HEAD_BYTES = 65_536`):
    1. Read raw bytes from `self.connection` into a `bytearray` until `\r\n\r\n` or `\n\n` appears, updating `self.connection.settimeout(max(0.001, deadline - time.monotonic()))` before every `recv()`.
       - On empty EOF (`not chunk` with empty buffer) or `TimeoutError`/`OSError`: set `self.close_connection = True` and return cleanly.
       - If buffer exceeds `_MAX_HEAD_BYTES` without end-of-headers: `_send(431, b"Request Header Fields Too Large\n", "text/plain; charset=utf-8")` and return.
    2. Swap `self.rfile = io.BytesIO(bytes(buf))`, set `self.raw_requestline = self.rfile.readline(_MAX_HEAD_BYTES + 1)`, and call `self.parse_request()`. If `False`, return (`send_error` already emitted hardened `400`/`505`).
    3. **Host check (every route, every method):** `if self.headers.get_all("Host") != [server.expected_host]:` → `_send(403, b"Forbidden\n", "text/plain; charset=utf-8")` and return.
    4. **Method check:** `if self.command != "GET":` → `_send(405, b"Method Not Allowed\n", "text/plain; charset=utf-8", extra_headers={"Allow": "GET"})` and return.
    5. Dispatch `GET` path (`urllib.parse.urlsplit(self.path)`):
       - `/` → `200`, `text/html; charset=utf-8`, `index.html`
       - `/app.css` → `200`, `text/css; charset=utf-8`, `app.css`
       - `/app.js` → `200`, `application/javascript; charset=utf-8`, `app.js`
       - `/favicon.ico` → `204`, `b""`, `""`
       - `/api/meta`, `/api/runs`, `/api/workspace`:
         - Require `tokens = self.headers.get_all("X-Vestibule-Token") or []` to satisfy `len(tokens) == 1 and hmac.compare_digest(tokens[0], server.token)`; else `_send(403, _json_bytes({"error": "invalid or missing token"}), "application/json; charset=utf-8")`.
         - Parse query string via `urllib.parse.parse_qs(parsed.query, keep_blank_values=True)`.
         - Dispatch inside `try ... except Exception`: log sanitized summary to stderr and return `500` `{"error": "internal server error"}`.
       - Everything else → `404`, `text/plain; charset=utf-8`, `Not Found\n`.
  - `_json_bytes(obj: Any) -> bytes`: `json.dumps(obj, separators=(",", ":"), allow_nan=False).encode("ascii")` (`ensure_ascii=True`, preventing `UnicodeEncodeError` on lone surrogates and invalid JSON on `NaN`/`Infinity`).

### 4.3 Deadlock-free shutdown & safe browser launch
- Factor `_serve(srv: _UiServer, stop: threading.Event) -> None` so tests can control shutdown without monkeypatching:
  - Starts a daemon thread running `srv.serve_forever(poll_interval=0.2)`.
  - Main thread waits in `try: while not stop.wait(0.2): pass except KeyboardInterrupt: pass finally: srv.shutdown(); srv.server_close()`.
- Signal registration in `main()`:
  - Only register `signal.SIGTERM` when `hasattr(signal, "SIGTERM") and threading.current_thread() is threading.main_thread()` (saving and restoring the previous handler in `finally`); handler ONLY calls `stop.set()`.
- Browser launch (skipped when `--no-open`):
  - On `sys.platform.startswith("linux")`: if neither `os.environ.get("DISPLAY")` nor `os.environ.get("WAYLAND_DISPLAY")` is set, print `"no display detected; open the URL above manually"` to stderr and skip.
  - Otherwise launch a daemon thread that calls `webbrowser.get()` + `browser.open(url)` inside `try ... except Exception:` (so `webbrowser.Error` on WSLg/minimal Linux never crashes startup and `xdg-settings` latency never delays the serve loop).
  - On Linux, only call `browser.open(url)` if `_is_gui_browser(browser)` holds:
    `isinstance(b, webbrowser.BackgroundBrowser) or (isinstance(b, webbrowser.UnixBrowser) and getattr(b, "background", False)) or isinstance(b, webbrowser.Konqueror)`
    (allows `xdg-open`, `gio`, Firefox/`Mozilla`, Chrome/`Chromium`, Opera, Konqueror; rejects TTY browsers like `GenericBrowser`/`lynx`/`w3m`/`links` and `Elinks`). On failure or non-GUI browser, print a sanitized manual-open hint to stderr.

### 4.4 `_AuditStore` (cache, FIFO/symlink/TOCTOU safety, JSON hardening, exact summary rules)
- Filename pattern: `_NAME = re.compile(r"[0-9]{8}T[0-9]{6}Z-[0-9]+\.jsonl")`.
- `_MAX_LINE_BYTES = 65_536`.
- Poll flow (`store.snapshot()`):
  1. Outside lock: scan `audit_dir` via `os.scandir(audit_dir)`.
     - On `FileNotFoundError`: acquire `self._lock`, clear `self._files`, `self._snapshot`, `self._snapshot_sig`, and return empty snapshot (`audit_dir_exists=False`).
     - On other `OSError`: return empty snapshot (`audit_dir_exists=True, failed_files=1`) without caching it.
  2. For each matching entry name, call `os.stat(path, follow_symlinks=False)` outside lock:
     - Skip symlinks / Windows reparse points (`workspace._is_symlink_or_reparse(st)`) and non-regular files (`not stat.S_ISREG(st.st_mode)`).
     - Signature includes device + inode + size + mtime: `sig = (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)`.
  3. Build `dir_sig = tuple((name, *sig) for name, sig in sorted_entries)`.
  4. Under `self._lock`: if `self._snapshot is not None and dir_sig == self._snapshot_sig and failed == 0`: return `self._snapshot`. Otherwise collect changed/new filenames.
  5. Outside lock: for each changed/new file, open safely:
     - POSIX: `fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)`; inside `try ... except: os.close(fd); raise`, call `st_open = os.fstat(fd)`, verify `stat.S_ISREG(st_open.st_mode)` (refusing FIFOs/devices/dirs without leaking `fd`), then wrap with `os.fdopen(fd, "rb")`.
     - Windows: open with `open(path, "rb")`, call `st_open = os.fstat(f.fileno())`, verify `stat.S_ISREG(st_open.st_mode)` AND `(st_open.st_dev, st_open.st_ino) == (st.st_dev, st.st_ino)` (closes symlink-swap TOCTOU between `os.stat` and `open`).
     - Capture `read_sig = (st_open.st_dev, st_open.st_ino, st_open.st_size, st_open.st_mtime_ns)` **before** reading (so a concurrent write during read can never be falsely cached as up to date).
     - Read line by line with `f.readline(_MAX_LINE_BYTES + 1)`: if `len(chunk) > _MAX_LINE_BYTES and not chunk.endswith(b"\n")`, drain remaining chunks until `\n` or EOF and increment `skipped` by **1**.
     - Parse JSON with `json.loads(line, parse_constant=_reject_constant)` (rejecting `NaN`/`Infinity`/`-Infinity`) and the same schema check as `audit_cli.py`.
     - Pair `start` + `end` per file by `id`, preserving `blocked` records AND orphan `end` records (`has_start=False, has_end=True`).
     - Defensively type-check and cap every field (`code` <= 4096 B, `reason` <= 1024 B, `isolation_detail` <= 512 B, `not_measured` <= 512 B, `id`/`backend`/`runtime`/`verdict`/`isolation`/`outcome`/`language` <= 128 B, `image`/`image_ran`/`backend_run`/`code_sha256` <= 256 B, `denied_syscalls` <= 256 strings × 64 B, `client: {"name": str(<=128), "version": str(<=128)} | None`, `limit_hits: dict[str(<=64), int]` excluding `bool` up to 16 keys, finite ints/floats only via `math.isfinite`).
     - If reading a file raises `OSError`: increment `failed_files` and mark that filename as failed (step 6 drops any stale cached entry for it and leaves `self._snapshot_sig = None` so the next poll retries).
  6. Under `self._lock`: update `self._files` (pruning deleted or failed filenames, storing `read_sig` for newly read files), merge calls sorted **newest-first** by `(ts, file, line_no)` descending, precompute `summary`, store `self._snapshot`, and set `self._snapshot_sig = dir_sig` only when `failed == 0` and every newly read file had `read_sig == expected_sig`.
- **Exact `summary` counting predicates** (computed across all loaded calls):
  - `native`: `isolation == "native"`
  - `container`: `isolation == "container"`
  - `degraded`: `isinstance(isolation, str) and isolation.endswith("-degraded")` (not double-counted in `native` or `container`)
  - `none`: `isolation == "none"` (NEVER counts `isolation is None`)
  - `blocked`: `(not has_start and not has_end) or outcome in ("refused", "failed", "cancelled")`
  - `timed_out`: `outcome == "timed_out"`
  - `no_end`: `has_start and not has_end`
  - `denied`: `bool(denied_syscalls)`
- **Server-side filtering (`GET /api/runs?limit=200&q=&filter=`):**
  - `limit`: clamped `1..1000` (default `200`).
  - `filter`: `"all" | "no_end" | "blocked" | "timed_out" | "degraded" | "denied"` (using the exact same predicates as `summary`).
  - `q`: stripped, max 200 chars, case-insensitive substring match over `(id, language, outcome, isolation, isolation_detail, code, reason, ",".join(denied_syscalls))`.
  - Runs across all loaded calls BEFORE slicing `[:limit]`.

### 4.5 Exact JSON schemas

- **`GET /api/meta`** (dir existence checked via `os.stat(..., follow_symlinks=False)` wrapped in `except OSError`, never `500` on offline drive / `EACCES`):
  `{version, audit_dir, audit_dir_exists, audit_warning, workspace_dir, workspace_dir_exists, workspace_error, max_preview_bytes}`
- **`GET /api/runs`**:
  `{audit_dir_exists, file_count, total_calls, matched_calls, returned_calls, limit, skipped_lines, failed_files, summary: {native, container, degraded, none, blocked, timed_out, no_end, denied}, calls: [...]}`
- **`GET /api/workspace?path=.`** (every response includes `display_path`, escaping each path segment with `workspace._shown` so breadcrumbs and headers cannot be bidi-spoofed):
  - `dir`: `{kind: "dir", path, display_path, entries: [{name, display_name, kind, size}], total_scanned, gone, hidden, over_scan}`
  - `file`: `{kind: "file", path, display_path, content, truncated, max_bytes}`
  - `missing`: `{kind: "missing", path, display_path, root_exists}`
  - `refused`: `{kind: "refused", path, display_path, reason}`
  - `error`: `{kind: "error", path, display_path, reason}`

### 4.6 Frontend (`src/vestibule/ui/{index.html,app.css,app.js}`)
- Zero external URLs across `index.html`, `app.css`, and `app.js` (100% offline).
- Zero `innerHTML`, `outerHTML`, `insertAdjacentHTML`, `document.write`, `eval`, or inline `style` attributes.
- All API URLs built via `URLSearchParams` (handles `+`, `#`, `&`, `%` in workspace paths and search queries cleanly).
- Token lifecycle: read `location.hash.slice(1)` → store in memory + `sessionStorage` → strip via `history.replaceState(null, "", "/")` → send in `X-Vestibule-Token`. If missing or on any `403`, clear `sessionStorage`, pause polling, and show inline token prompt.
- Runs view: header + status dot + Pause/Resume + tabs + overlap warning banner; summary pills + filter bar (`"Showing X of Y matching (Z total)"`); compact stats strip (median `wall_ms` + median `mem_peak_mb` with explicit `"not measured on container runs"` / M3 whole-sandbox note + inline SVG sparkline via `createElementNS`); split list + detail drawer showing full logged `code`, `isolation` + `isolation_detail`, `usage` + `not_measured` + M3 note, `limit_hits`, `denied_syscalls`, output sizes, `backend`/`runtime`/`image_ran`, `client`, `code_sha256`, `reason`. Unended calls display factual `no end` + elapsed age + M3 tooltip; orphan `end` displays `"(no start record found)"`.
- Workspace view: breadcrumbs using `display_path`, entry list using `display_name` + `kind` + size (`"—"` when `null`), file preview via `textContent`, symlink refusal banner, passive missing-root empty state.

### 4.7 Packaging & `release_smoke.py`
- `.gitattributes`: append `src/vestibule/ui/** text eol=lf`.
- `scripts/release_smoke.py`:
  - Add `"vestibule/ui/__init__.py"`, `"vestibule/ui/index.html"`, `"vestibule/ui/app.css"`, `"vestibule/ui/app.js"` to `_WHEEL_REQUIRED`.
  - Extend `wheel_problems()` CRLF check to `n.endswith((".py", ".html", ".css", ".js"))`.
  - In `_smoke_one()` after `audit`:
    1. Run `[str(exe), "ui", "--bad-flag"]` → expect exit code `2` and empty stdout.
    2. Run `[str(tool_python), "-c", _UI_ASSETS_CHECK]` (`from vestibule import ui_cli; assert ui_cli._load_assets()`) → expect exit code `0`.
- `tests/test_release_smoke.py`:
  - Update `REQUIRED` with the four `vestibule/ui/*` files + add CRLF test for `.html`/`.css`/`.js`.
  - Update `machine` fixture (`tests/test_release_smoke.py:228-287`) to distinguish the `vestibule.server` `-c` call from the `ui_cli._load_assets()` `-c` call, handle `[exe, "ui", "--bad-flag"]`, update `machine.ran` expectations, and add failure-case tests for both new `ui` smoke steps.

---

## 5. Verification plan (`tests/test_ui.py` + gates)

Every test written red-first before implementation:
1. **HTTP security (`tests/test_ui.py`):**
   - Wrong `Host`, missing `Host`, duplicate `Host` → `403` on `/` and `/api/runs`, with all 4 security headers and no `Server:`.
   - Malformed request (`BADREQUEST\r\n`) and `HTTP/0.9` request (`GET /\r\n\r\n`) → hardened `HTTP/1.0 400` / `403` WITH status line and all 4 security headers (regression for `default_request_version`).
   - Slowloris test: a client trickling 1 byte every 0.3 s against a short test deadline is disconnected at the total deadline (does not reset per `recv`).
   - Missing, wrong, duplicate, or query-string-only `X-Vestibule-Token` → `403` on `/api/*`.
   - `POST`, `PUT`, `PATCH`, `DELETE`, `OPTIONS`, `HEAD`, `TRACE`, and unknown verb (`FOO`) → `405` + `Allow: GET` + all 4 security headers (`HEAD` writes zero body bytes).
   - `GET /favicon.ico` → `204` with all 4 security headers and NO `Content-Type` / `Content-Length`.
   - Concurrency cap: 16 occupied slots reject the 17th connection cleanly; simulated `Thread.start` failure releases the slot and closes the socket.
   - `handle_error` with `sys.stderr = None` writes nothing to `sys.stdout`.
   - Windows `SO_EXCLUSIVEADDRUSE`: on `win32`, a second `SO_REUSEADDR` socket fails to bind the running UI server's port.
   - Static asset audit: `app.js` has zero `innerHTML`/`outerHTML`/`insertAdjacentHTML`/`document.write`/`eval(`/`.style.`/`setAttribute("style"`, and `index.html`, `app.css`, and `app.js` have zero `http://`/`https://` external URLs and zero inline `<script>`/`<style>` blocks.
2. **Audit store & `/api/runs` (`tests/test_ui.py`):**
   - Paired `start`/`end`, `blocked`, unended `start` (`no_end`), orphan `end` (`has_start: False, has_end: True`).
   - Exact `summary` counts: `isolation=None` does NOT increment `summary.none`; `native-degraded` increments `degraded` only (not `native`).
   - Hand-edited `NaN`/`Infinity`, lone surrogates, wrong types, and > 4 KiB `code` never cause HTTP `500` or invalid JSON.
   - Oversized line (> 64 KiB without `\n`) drained and counted as **1** skipped line; following valid lines still parsed.
   - Symlink matching `.jsonl` regex skipped; POSIX FIFO (`os.mkfifo`) skipped without blocking and without leaking an fd.
   - Cache hit on unchanged files + live invalidation on open-handle append, on file deletion, on newly-unreadable file, and on deleted `audit_dir` (cache cleared, dir not created).
   - Server-side `q` and `filter` match across > `limit` calls before slicing to `limit`.
3. **Workspace jail & `/api/workspace` (`tests/test_ui.py`):**
   - Existing `tests/test_workspace.py` passes unchanged (byte-for-byte parity, including `max_chars` and `gone`/`hidden`).
   - `/api/workspace` dir listing (`hidden` exact when `over_scan=True`), file preview, truncation, special characters (`+`, `#`, `&`, `%`), missing file (`root_exists: true`), missing root (`root_exists: false`, root NOT created), broken-symlink root (`kind: "refused"`).
   - Traversal (`../etc/passwd`, `C:\foo`, `NUL`, `con.txt`) returns `kind: "refused"` even when workspace root does not exist.
   - Live symlink inside workspace appears as `kind: "symlink"` and returns `kind: "refused"` when requested directly.
   - Filename/dir with `U+202E` (RLO) or `\n` returns escaped `display_name` AND escaped `display_path`.
   - Unexpandable `VESTIBULE_WORKSPACE` / raw `OSError` returns `kind: "error"`, and `/api/meta` handles `OSError` cleanly.
4. **CLI, stdout purity, browser guard, shutdown & packaging (`tests/test_ui.py`, `tests/test_rootfs.py`, `tests/test_release_smoke.py`):**
   - Importing `vestibule.server` does NOT import `vestibule.ui_cli` or `http.server`.
   - Bad CLI args exit `2` with empty stdout; invalid `VESTIBULE_AUDIT_DIR` exits `1` with sanitized stderr and empty stdout.
   - Linux browser guard: headless (`DISPLAY`/`WAYLAND_DISPLAY` unset) skips `webbrowser.open`; `_is_gui_browser` accepts `BackgroundBrowser`, `Mozilla`/`Chrome` (`UnixBrowser` with `background=True`), and `Konqueror`, and rejects `GenericBrowser` and `Elinks`; `webbrowser.Error` in daemon thread never aborts server startup.
   - `_serve(srv, stop)` and POSIX `SIGTERM` shut down cleanly within 2 s (no deadlock), and running in a non-main thread never raises `ValueError`.
   - `tests/test_release_smoke.py` passes with updated `REQUIRED`, CRLF checks on `.html`/`.css`/`.js`, and both new `_smoke_one` checks.
5. **Gates & live check:**
   - Full Windows gate (`check_env`, `ruff`, `mypy`, `pytest`) + WSL Linux gate (`scripts/gate-linux.sh`).
   - Live check: run `vestibule-mcp ui --no-open`, trigger a real `run_code` + workspace write via MCP, verify live appearance in `/api/runs` & `/api/workspace`, then `Ctrl+C` cleanly.
