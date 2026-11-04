# M3 step 2 — the audit writer

Status: **signed off 2026-11-03 (v2), W1–W4 included; built 2026-11-03 (§9 has what the build
and the review changed).** v2 was revised after the `skeptic`
review: 18 findings, none high. `verifier` checked the 17 that make claims, confirming 14 and
rating 3 plausible, with none refuted (§8). §3.1 lists the four places where the plan departs
from the contract's wording; Victor approved each.

Inputs:
- the M3 contract, [`M3-audit-usage.md`](M3-audit-usage.md) (signed off 2026-11-01): A2, A3 and
  A5–A8, the record schema (§3), the time budgets (§4), criteria 1, 5, 6 and 8, and §6's step 2;
- step 1's plan, [`M3-step1-usage.md`](M3-step1-usage.md), whose U6 moved `ran`, `run_id` and
  `image` into this step;
- the code at `40f63be`;
- facts checked on 2026-11-02 against the installed MCP SDK (1.28.1):
  - a tool handler can read the client's name and version through
    `server.request_context.session.client_params.clientInfo`, and outside a request
    `request_context` raises `LookupError`;
  - the SDK cancels a handler through its anyio cancel scope, so the handler sees
    `asyncio.CancelledError`;
  - the SDK's JSON parser rejects a lone surrogate (`"\ud800"`) before any handler runs;
  - on Windows, `os.open` without `O_BINARY` writes `\n` as `\r\n`.

---

## 1. Scope

**In:**
- **`src/vestibule/audit.py`, the writer (A5, A7).**
  - One daemon thread per server process. It holds the process's one file descriptor and is fed
    by a bounded queue of 1000 records.
  - Every record is acknowledged back to the event loop. A call waits at most 2 s in all, once
    its run is over (W2).
  - Reopen after a deleted file (POSIX), and newline repair after a torn write.
- **`VESTIBULE_AUDIT_DIR`** (default `~/.vestibule/audit`). It must be absolute or start with
  `~`, and must not overlap the workspace or the rootfs dir. Otherwise it's a config error, and
  every call is `Blocked:` (A8).
- **The records** (`blocked`, `start`, `end`) and every outcome, wired into `_handle_run_code`,
  including cancellation (A6).
- **`RunResult` gains `ran`, `run_id` and `image`** (U6), plus `stdout_bytes`, `stderr_bytes`
  and `output_capped` (W3). Every backend sets them.
- **The `audit:` line** on a result whose records weren't written, or weren't confirmed (A3).
- **Tests and the benchmark stay off the real audit dir.**

**Not in this step:**
- **`vestibule-mcp audit`** (A10) is step 3, and non-blocking stderr (A11) is step 4.
- **The acceptance probe** of the audit dir's absolute path from inside native (criterion 5's
  last bullet) is step 5, with the rest of the acceptance suite. This step adds the container's
  command-shape test, which is a plain unit test.
- **The README's "Audit log" section** is step 5: where the files are, who can read them, and
  A12's "not tamper-proof". This step adds the setting's row and the troubleshooting lines its
  behavior needs (§6).
- **The live check** (criterion 10) is step 5.

---

## 2. What changes

### 2.1 The writer (`src/vestibule/audit.py`, new)

```python
class AuditLog:
    def __init__(self, directory: str) -> None              # no I/O: the server builds it at import
    def submit(self, record: dict[str, Any]) -> asyncio.Future[str | None]  # never blocks or raises
    async def confirm(self, *futs: asyncio.Future[str | None]) -> str | None  # one 2 s wait for all
```

- **`submit`** runs on the event loop, inside one `try` that catches `Exception`:
  - **The loop is the running one, per record.** Each record's future belongs to the loop that
    was running when it was submitted, never to a loop saved earlier. Tests run each test on its
    own loop, and the real server has one.
  - **It serializes the record** with `json.dumps(record, separators=(",", ":"))`, with
    `ensure_ascii` left on. Every non-ASCII and every control character is then a `\u` escape,
    so a line is plain ASCII, and JSON escapes lone surrogates too.
  - **It puts `(line, settle)` on the queue with `put_nowait`.** It starts the thread on the
    first record.
  - **It logs every failure, including on the calls that never wait** (skeptic 2). It adds a
    done callback on the future that logs a non-`None` note as a warning, on the loop thread.
    That covers the cancelled `end`, which is submitted and never confirmed. A3 says "logs a
    warning either way", and criterion 6 says "no record is lost silently".
  - **It never raises.** A full queue gives a future already resolved with `not recorded (the
    audit queue is full: the disk has stopped taking records)`. Any other exception gives
    `not recorded (<reason>)`.
- **The thread** loops over the queue. Its whole loop body, the write and the `settle` call
  together, sits inside a `try` that catches `Exception`, so nothing can stop the thread
  (skeptic 17).
  - **`settle(note)`** hands the result back with `loop.call_soon_threadsafe(fut.set_result,
    note)`. It suppresses the `RuntimeError` a closed loop raises, since nobody is waiting then.
  - **The note** is `None` on success, or `not recorded (<reason>)` after any exception.
  - **The thread never logs.** Today a log call can block on a stuck stderr (step 4 fixes that).
    The loop logs through the done callback above.
- **`confirm(*futs)`** waits for all its futures inside one `asyncio.timeout(2)`, on
  `asyncio.shield` so a timeout never cancels them. Since a late result still arrives, its done
  callback still logs it.
  - **It returns the worse note** (skeptic 9). Any `not recorded` wins over `not confirmed (the
    disk didn't answer within 2 s)`, and that wins over `None`.
  - **It logs its own timeout note.**
- **Opening the file:**
  - **Lazily, on the first record,** so a server that never runs code creates nothing.
  - **The dir:** `Path(directory).expanduser()`, then `mkdir(mode=0o700, parents=True,
    exist_ok=True)`.
    - A default dir that can't expand (no home directory) makes `expanduser` raise. That record
      gets `not recorded (could not find the home directory; set VESTIBULE_AUDIT_DIR)` (W4).
  - **The file:** `os.open(<dir>/<name>, O_RDWR | O_APPEND | O_CREAT | O_BINARY, 0o600)`.
    - `O_BINARY` is `getattr(os, "O_BINARY", 0)`. Without it, Windows' C runtime opens the fd
      in text mode and writes every `\n` as `\r\n`, against A5's "`\n` endings on every
      platform".
    - Read access serves newline repair, below.
    - The fd is non-inheritable (PEP 446), so no helper or CLI process gets it.
  - **The name:** `<UTC time the AuditLog was made>-<pid>.jsonl`, e.g.
    `20261102T214425Z-4242.jsonl`. The server builds the AuditLog at import, so that's its start
    time. A reopen uses the same name.
- **Deleted-file checks, before and after each write** (A5, skeptic 17).
  - **Before:** `os.fstat(fd).st_nlink == 0` means the file was deleted (POSIX). The writer
    closes the fd and opens the file again, recreating the dir if needed.
  - **After:** if it's 0 now, the file was deleted while the record went in. That record gets
    `not recorded (the audit file was deleted while this record was written)`, and the next
    record reopens.
  - **On Windows** a held-open file can't be deleted (`PermissionError`, `st_nlink` stays 1).
    The checks are harmless there, so they run with no platform branch.
- **Newline repair (A5), with no empty lines** (skeptic 12).
  - `_dirty` means the file doesn't end in `\n`.
  - The writer loops on `os.write` until the whole line is written. If it raises after some of
    the line's bytes are written, `_dirty` is set, and the next record is written as `\n` plus
    the record. If nothing of the line got written, the file still ends in `\n`.
  - On opening a file that isn't empty, the writer reads its last byte (`lseek(-1, SEEK_END)`,
    `read(1)`). That happens only on a pid-and-second collision.
  - So the file never gets an empty line, and A10's "every unparseable line is skipped and
    counted" holds unchanged.
- **Record builders** live here too, so `server.py` only wires them:
  - `blocked_record(language, outcome, reason)`;
  - `start_record(...)`;
  - `end_record(audit_id, outcome, wall_ms, reason=None)` for the three outcomes with no
    `RunResult`;
  - `end_from_result(audit_id, result, wall_ms)`.

  Each starts with `{"v": 1, "kind": …, "id": …, "ts": …}`. `ts` is `datetime.now(timezone.utc)`
  in ISO form, with milliseconds and a `Z`.
- **`cut(text, limit)`** is `text.encode("utf-8", "replace")[:limit].decode("utf-8", "ignore")`
  (skeptic 7).
  - **`replace` on encode:** a message that embeds a configured path can carry
    surrogate-escaped bytes when the path isn't UTF-8 on POSIX, and a strict encode would raise
    inside the handler. An example is selection's `no native rootfs … in {rootfs_path}`
    (`select.py:487`). `str(OSError)` itself is safe, because it shows the filename's repr.
  - **`ignore` on decode** can then drop only the partial character at the cut.

  The builders do nothing else that can raise: dict construction, `cut`, and `hashlib` over
  `code.encode()`, which can't fail because the SDK rejects lone surrogates.
- **Constants:** `_QUEUE_MAX = 1000`, `_CONFIRM_S = 2`, `CODE_CAP = 4096`, `REASON_CAP = 1024`.

### 2.2 The setting (`config.py`, A8)

- **`Limits` gains** `audit_dir: str = "~/.vestibule/audit"` and an `audit_path` property, the
  same as `workspace_path`. `from_env` reads `VESTIBULE_AUDIT_DIR`.
- **Validation runs after the other settings,** and adds at most one problem to the
  `ConfigError` list. Each is a `Blocked:` message the model reads.
  - **An explicitly set value that isn't absolute after `expanduser`:**
    `VESTIBULE_AUDIT_DIR='audit' must be an absolute path or start with ~`. That covers a
    relative path, a Windows drive-relative path (`\audit`, `C:audit`), and a `~` that didn't
    expand.
  - **An explicitly set value whose `expanduser` raises** (no home directory, or a `~user` that
    doesn't exist): the same message, with the reason.
  - **The default, when it can't expand,** is not a config error (W4). Today a host with no home
    directory and absolute workspace and rootfs settings works (skeptic 15). Its records get
    `not recorded (…; set VESTIBULE_AUDIT_DIR)` instead, and its runs go ahead (A3).
  - **Overlap with the workspace or the rootfs dir,** in either direction or the same dir. It's
    compared after `realpath`, `normcase` and `casefold`. The message names both settings and
    both paths, plus the fix: `point one of them somewhere else`.
    - **Why `casefold`:** macOS' `normcase` does nothing and its default file system ignores
      case, so `~/WS/audit` would pass beside a `~/ws` workspace (skeptic 14). Folding on every
      platform refuses only a Linux pair that differs by case alone, which is the safe
      direction.
    - **A path that can't expand is left out of the comparison:** the default audit dir in W4,
      or a `~` workspace or rootfs dir with no home directory. Either its own first use fails,
      or the writer reports it.
    - **The comparison** is `os.path.commonpath`, with the `ValueError` guard for different
      Windows drives. That's the same rule as `rootfs.overlap_problem`, written again here
      because `config.py` can't import `rootfs`, which imports `config`.
- **The check is static, so it runs at startup** (A8). `vestibule-mcp setup` reads the same
  settings and refuses on the same problem, as it does for every invalid setting today.

### 2.3 `RunResult` and the backends (U6, W3)

```python
# M3 A6, for the audit: False only when the backend knows nothing executed; the backend's own
# id for the run (the container `vestibule-<id>`, the scope `vestibule-<id>.scope`, the
# `run <id>:` log lines); the image the run used (a runtime image id, or a native manifest
# digest); the bytes collected from the guest before decoding, without any notes the backend
# appends (native and container stop at twice max_output_bytes; naive has no cap; None when
# nothing was collected); whether collection hit that cap and stopped the sandbox
ran: bool = True
run_id: str | None = None
image: str | None = None
stdout_bytes: int | None = None
stderr_bytes: int | None = None
output_capped: bool = False
```

**Native (`native.py`, `rootfs.py`):**
- **`Rootfs` gains `manifest_digest: str | None = None`.** `find` fills it from the marker's
  `manifest_digest` when that's a string, so it's the per-platform manifest `setup` unpacked.
  `find` checks only `index_digest` against the key, so this is recorded as the marker states
  it.
- **`_Run` gains `image`,** set from `found.manifest_digest` in `run()`.
- **`_not_run(why, run_id=None)`** returns `ran=False`. The helper-start failure passes
  `run.run_id`, because its log line already names that id.
- **`_collect`'s result adds:**
  - `ran=_ran(lines)`, `run_id=run.run_id` and `image=run.image`;
  - `stdout_bytes=len(out_buf)` and `stderr_bytes=len(err_buf)`, the guest's bytes, without
    the notes native appends to stderr;
  - `output_capped`, the `truncated` value `_collect` already computes.
- **`_ran` is a new pure function.** It's False only when the first `ready`/`error` line is an
  `error`, which init sends only before the guest exists.
  - "No status report at all" stays `ran=True`. Vestibule doesn't know that nothing ran there,
    and an audit that says `not_run` for code that did run is the dangerous direction.

**Container (`container.py`):**
- **The preflight keeps the id it already asks for.** `_images_ok: set[str]` becomes
  `_image_ids: dict[str, str | None]`, holding `image inspect --format {{.Id}}`'s output, or
  None if that's empty.
  - It's the id **the preflight saw** (skeptic 8). The preflight runs once per image per
    process. With the default digest pins, the image can't change under the ref. With a tag ref,
    a re-pull mid-session can make the id stale; selection already warns about unpinned refs
    (`_warn_unpinned`).
- **`_execute`'s result adds** `run_id`, `image=self._image_ids.get(…)`,
  `stdout_bytes=len(out_buf)`, `stderr_bytes=len(err_buf)` and `output_capped=truncated`.
- **The runtime-missing result** adds `ran=False` and `run_id`. Nothing was collected, so its
  byte counts stay None.
- **Exit 125 stays `ran`, and keeps its image** (contract A6). Its `isolation_detail`, which the
  record keeps, says the container may never have started.

**Naive (`naive.py`, CRLF, kept CRLF):**
- Both exit-127 results add `ran=False`: the interpreter isn't on PATH, or `FileNotFoundError`
  at spawn.
- The normal result adds `stdout_bytes=len(out)` and `stderr_bytes=len(err)`. Naive has no cap,
  no run id and no image.

### 2.4 The records (contract §3, filled in)

| Kind | Fields after `v`, `kind`, `id`, `ts` |
|---|---|
| `blocked` | `pid`, `version`, `language`, `outcome` (`refused` / `cancelled` / `failed`, W1), and `reason` (≤ 1 KiB) unless cancelled |
| `start` | `pid`, `version`, `language`, `timeout_s` (the clamped value), `backend`, `runtime` (containers only), `verdict`, `image`, `code_sha256`, `code_bytes`, `code` (≤ 4 KiB), `code_truncated`, and `client` when the SDK has it |
| `end`, with a `RunResult` | `outcome` (`ran` / `timed_out` / `not_run`), `backend_run`, `image_ran`, `exit_code`, `timed_out`, `wall_ms`, `isolation`, `isolation_detail`, `usage` (`cpu_ms`, `mem_peak_mb`, `pids_peak`), `limit_hits`, `not_measured`, `denied_syscalls`, `stdout_bytes`, `stderr_bytes`, `output_capped` |
| `end`, without one | `outcome` (`refused` / `failed` / `cancelled`), `wall_ms`, and `reason` unless cancelled |

- **`id`** is `secrets.token_hex(8)`. One id covers a call's `start` and `end`; a `blocked`
  record gets its own.
- **`language`** is the argument when it's one of `ALLOWED_LANGUAGES`, otherwise `null`.
  - Past the SDK's schema check it's always an enum value.
  - The handler's own type checks run first and are reachable only by calling the handler
    directly, as tests do. Either way, a record never holds a model-supplied string there.
- **`backend`** uses the contract's and `VESTIBULE_BACKEND`'s words: `native`, `container` or
  `naive` (skeptic 10).
  - It's `container` for a `ContainerBackend`, with `runtime` set to its `docker` or `podman`.
  - Otherwise it comes from the selection's verdict: `native` for `native` and
    `native-degraded`, and `naive` for `none`. Selection gives `none` only to an explicit naive
    backend.
- **`verdict`** is the selection's verdict. The per-run verdict is the `end`'s `isolation`.
- **`image`** is the configured ref for the language: `image_node` for node, otherwise
  `image_python`, as both backends choose it. It's `null` for naive.
- **`client`** is `{"name", "version"}`, each cut to 200 characters. It's left out when there's
  no request context (a direct handler call) or no `client_params` (before initialization).
- **`reason`** is the message the model saw, cut to 1 KiB: the `Blocked:` text, or the first
  line of a failed result.

### 2.5 The handler (`server.py`)

- **`get_warden()` becomes `get_selection()`** and returns the `Selection`, because `start`
  needs its verdict.
  - The six test fakes that patch `get_warden` wrap their warden in
    `Selection(warden, "<verdict>")`. They're in `tests/test_lifecycle.py` and
    `tests/test_select.py`.
  - `scripts/bench_coldstart.py`'s two calls switch to it (skeptic 5).
- **`AUDIT = audit.AuditLog(LIMITS.audit_dir)`** at module level. It does no I/O there.

```python
async def _handle_run_code(args: dict) -> list[TextContent]:
    ...  # the same checks; each `return _blocked(m)` becomes `return await _refuse(m, language)`
    try:
        selection = await get_selection()
    except asyncio.CancelledError:                       # W1
        AUDIT.submit(audit.blocked_record(language, "cancelled", None))
        raise
    except RunRefusedError as e:
        log.info("run_code refused: %s", e)
        return await _refuse(str(e), language)
    except Exception as e:                               # W1; today's text, no start yet
        log.exception("run_code crashed")
        note = await AUDIT.confirm(AUDIT.submit(audit.blocked_record(language, "failed", ...)))
        return [_text(f"Internal error: {e}" + _audit_line(note) + "\n" + _NO_REPORT)]

    audit_id = secrets.token_hex(8)
    start = AUDIT.submit(audit.start_record(audit_id, ...))  # W2: queued, not waited for
    started = time.monotonic_ns()
    try:
        async with asyncio.timeout(timeout_s + 30):
            result = await selection.warden.run(language, code, timeout_s, LIMITS)
        wall_ms = _ms_since(started)
    except asyncio.CancelledError:  # the SDK's cancel: queue the end, never wait (A6, A7)
        AUDIT.submit(audit.end_record(audit_id, "cancelled", _ms_since(started)))
        raise
    except RunRefusedError as e:    # no slot, image missing: after start, so an end (A6)
        note = await AUDIT.confirm(start, AUDIT.submit(audit.end_record(..., "refused", ...)))
        return _blocked(str(e) + _audit_line(note))
    except TimeoutError:            # today's text and log line, then end(failed)
        ...
    except Exception as e:          # today's text and log line, then end(failed)
        ...

    SELECTOR.note_result(selection.warden, result)
    log.info(...)  # unchanged
    note = await AUDIT.confirm(start, AUDIT.submit(audit.end_from_result(audit_id, result, wall_ms)))
    return [_text(_format_result(result, wall_ms, note))]
```

- **One wait per call, after the run (W2).** The `start` is queued before `warden.run`.
  - In the healthy case the writer has it down within microseconds, long before a sandbox can
    spawn.
  - Waiting for it before the run would only delay the run when the disk is stuck. The record
    lands no sooner, and the start's note can only be shown at the end anyway.
  - So `confirm(start, end)` covers both under one 2 s budget. That's at most 2 s per call, not
    the contract's 4. `wall_ms` is computed before that wait, so it never counts it.
- **A cancel queues its record and re-raises at once,** never waiting. In the selection phase
  the record is a `blocked` with outcome `cancelled`; in the run phase it's `end(cancelled)`.
  The done callback (§2.1) logs a failed write.
- **The `audit:` line** is `audit: <the worse note>` (§2.1).
  - In a run result it sits just above `isolation:`, which stays the last line, as in step 1.
  - A `Blocked:` result has no isolation line, so there the audit line goes last.
  - A result whose records were written gets no `audit:` line, so today's exact texts stay.
  - `_format_result(r, wall_ms, audit_note=None)` takes the note. `_refuse(msg, language)`
    writes the `blocked` record with outcome `refused` and appends the line.
- **Not recorded, as the contract says:**
  - a call while `CONFIG_ERROR` is set (blocked in `call_tool`, before `_handle_run_code`);
  - an unknown tool;
  - `read_workspace`;
  - selection's own probe runs, which are Vestibule's runs, not an agent's call.

### 2.6 Tests and the benchmark stay off the real audit dir

- **`tests/conftest.py` (new)** (skeptic 1).
  - An autouse fixture replaces `server.AUDIT` with a small in-memory `RecordingAudit`. Its
    `submit` keeps the record in a list and returns an already-resolved future.
  - No ordinary test touches the disk, waits on a thread, or depends on a disk's timing.
  - The criterion-1 tests read the list directly, cancelled records included, with no race.
- **Real `AuditLog`s appear only in §5.1's writer tests,** plus one handler test that uses a real
  one end to end. Each uses its own temp dir and polls its file with a time limit.
- **`tests/stdioutil.py`.** `StdioServer.start` already strips the dev shell's `VESTIBULE_*`.
  It now also sets `VESTIBULE_AUDIT_DIR` to `<workspace>/../audit`, which a test's own settings
  can override. `tests/test_stdio_e2e.py`'s `StdioServerParameters` env gets the same.
- **`scripts/bench_coldstart.py`** sets `VESTIBULE_AUDIT_DIR=<its temp dir>/audit-<backend>`
  beside its workspace. Its timed calls now include the audit's acknowledgement (sub-millisecond
  on a healthy disk), as a real call does.
- **Checked by a test, not by listing the real dir** (skeptic 16). Victor's own registered
  server writes there once it runs this code. §5.5's e2e test reads its records from the temp
  dir, which proves the spawned-server plumbing.

---

## 3. Choices this plan makes

### 3.1 Departures from the contract's wording — approved by Victor 2026-11-03

| # | The contract says | This plan does | Why |
|---|---|---|---|
| W1 | `blocked` = "Refused before a backend was chosen" | `blocked` covers every call that ends before a backend was chosen, with an `outcome`: `refused`, `cancelled` (a cancel during selection), or `failed` (an unexpected error in selection) | These calls have no `start`, so they can't get an `end`, and without W1 they'd leave no record. A cancel during the first call's probe is realistic on a cold Docker VM. The `outcome` lets step 3 tell a refusal from a cancel (skeptic 11). |
| W2 | A7: wait for the `start` before the run and for the `end` after it, so at most 4 s | Queue the `start` before the run, then wait for both once, after the run, under one 2 s budget | The early wait never lands the record sooner; it only delays the run on a stuck disk (§2.5). Worst case halves. It also removes a cancellation window and its special case (skeptic 4, 18). |
| W3 | A2: output kept as sizes (`stdout_bytes`, `stderr_bytes`); A4's list of `RunResult` fields | The sizes are the bytes each backend collected before decoding, plus `output_capped`. That's three more `RunResult` fields than A4 lists | Sizes taken from the decoded text count each invalid byte as 3 (U+FFFD), so 30,000 random bytes would read as about 85,000. A capped flood would look like an OOM kill: `exit_code` 137, with the note only in stderr text, which isn't kept (skeptic 3). |
| W4 | A8: the dir must be absolute or start with `~` | The same, but an unset default that can't expand (no home directory) isn't a config error: its records are `not recorded (…; set VESTIBULE_AUDIT_DIR)` | A host with no home directory and absolute workspace and rootfs settings works today. A config error would block every call for a log fault, against A3 and golden rule 6 (skeptic 15). An explicit bad value is still a config error. |

### 3.2 Fixed choices

- **The `audit:` line sits above `isolation:`** (§2.5), so `isolation:` stays last everywhere it
  appears.
- **A record that was written adds nothing to the result.** The model needs the line only when
  a record is missing or unconfirmed.
- **The overlap rule can turn a working config into a config error.** That's A8 as signed, and
  it's stated here so it isn't a surprise. `VESTIBULE_WORKSPACE=~/.vestibule` contains the
  default audit dir.
  - Native already refused that config, at selection, because it also holds the rootfs (S2-D9).
  - A container-only user (Windows, macOS) who set it now gets a `Blocked:` message naming both
    settings and the fix.
- **No file until the first record,** and its name records the server's start time.
- **No `fsync`.** A record is in the OS's cache when it's acknowledged, not necessarily on
  disk. A machine crash (not a server crash) can lose the last records. An fsync per record
  would cost a disk flush on every call, and the contract asks for neither.
- **Acknowledged means written to the fd.** `not confirmed` means the thread didn't finish in
  time; the record may still land (A3), and its late note is still logged.

---

## 4. Edge cases

| Case | What happens |
|---|---|
| The audit dir can't be created (a file is in the way, no permission) | Each call's records get `not recorded (<OSError>)`, and the result shows it. The run goes ahead, and the next record tries again. |
| The disk is full | `os.write` raises: `not recorded`. If part of a line went down first, the next record starts on a new line. |
| The disk hangs (a network share) | Each call waits at most 2 s, after its run: `not confirmed`. Behind a hung write, 1000 records queue, then each new one gets `not recorded (the audit queue is full: …)` at once. |
| The file or the dir is deleted between records (POSIX) | The next record sees `st_nlink == 0`, reopens, and recreates the dir. Earlier records go with the file; nothing after is lost. |
| The file is deleted while a record is being written (POSIX) | The check after the write catches it: that record is `not recorded`, and the next reopens. |
| The same, on Windows | A held-open file can't be deleted. |
| The client cancels during selection | `blocked` with outcome `cancelled` (W1). |
| The client cancels mid-run | `end(cancelled)` is queued behind the already-queued `start`; the cancel propagates at once. |
| The client cancels during the final wait | Both records are already queued; the cancel propagates. |
| A cancelled call's record fails to write | Logged as a warning by the future's done callback; no result is left to carry it. |
| The server exits mid-run (POSIX hand-off `os._exit`, a crash, a kill) | The daemon thread dies with the process. A queued `end` can be lost, leaving a `start` alone: the honest record (A6, A7). |
| A torn last line at exit | Each process has its own file, so the next server's records are unaffected. Step 3's reader skips the damaged line and counts it. |
| Several runs at once | One thread writes in queue order; the `id` ties each `start` to its `end`. |
| Code of exactly 4096 bytes, 4097 bytes, or a multibyte character at the cut | Kept whole and not truncated; cut and truncated; cut before the partial character. `code_sha256` and `code_bytes` always describe the full code. |
| A refusal message holding a non-UTF-8 configured path (POSIX) | `cut` replaces the surrogates; nothing raises in the handler. |
| A hostile client name (control characters, 10 KB) | Cut to 200 characters. JSON escapes every control character, so it can't break a line. Step 3's sanitizer handles the terminal. |
| `VESTIBULE_AUDIT_DIR` is the workspace's parent | Overlap: config error, since the sandbox could write next to the records. |
| A case-variant overlap on macOS (`~/WS/audit`, `~/ws`) | Caught by `casefold`: config error. |
| No home directory, absolute workspace and rootfs settings | Works as today. Records get `not recorded (…; set VESTIBULE_AUDIT_DIR)` (W4). |
| A tag image ref, re-pulled mid-session | The container's `image_ran` is the id the preflight saw (§2.3). |
| A custom `VESTIBULE_AUDIT_DIR` on Windows | The mode bits don't apply; the files inherit the folder's ACL. The README states it (§6). |

---

## 5. Tests

Each new test must be shown to fail on a mutant (§5.6).

### 5.1 The writer (`tests/test_audit.py`, new, every platform unless marked)

- **A record lands as one line.**
  - The file's bytes end in `\n`, with no `\r`, on Windows too (`O_BINARY`).
  - The line parses back to the record.
  - Non-ASCII and control characters arrive as `\u` escapes.
- **POSIX only:** a dir it creates is 0700, and the file is 0600.
- **A dir that can't be made** (a regular file at its path) gives `not recorded (…)`. A later
  record lands once the file is removed.
- **One `AuditLog`, two loops in turn** (two `asyncio.run` calls). Both loops' records are
  confirmed, which pins the loop per record (skeptic 1).
- **A writer blocked past 2 s.** A stand-in `_write` waits on a `threading.Event`.
  - `confirm` returns `not confirmed` after 1.8–3 s.
  - After the event is set, the late result is logged (`caplog`), and the next record lands
    and confirms. That shows the thread survived the abandoned wait.
- **A full queue.** With the writer blocked, record 1001's `submit` returns in under 0.1 s, with
  `not recorded (the audit queue is full…)`.
  - A `threading.Timer` unblocks the writer after 3 s, so mutant 6 below fails by timing rather
    than hanging the suite (skeptic 6: there's no pytest-timeout).
- **POSIX only, deleted files:**
  - a file deleted between records is reopened, and the next record lands;
  - a file deleted during a write (a stand-in `os.write` unlinks it first) gives `not recorded`.
- **A torn write.** A stand-in `os.write` writes half a line and then raises `OSError(ENOSPC)`,
  once. That record gets `not recorded`. The next is on a line of its own that parses, and the
  file has no empty line.
- **A write that fails before any byte** adds no newline: the file has no empty line.
- **A non-empty file at open,** pre-created with `garbage` and no newline. The first record
  starts on a new line. With `garbage\n`, it gets no extra newline.
- **`settle` after the loop closed** doesn't raise, and the thread keeps writing.
- **An unexpandable dir** (a stand-in `expanduser` that raises `RuntimeError`) gives
  `not recorded (could not find the home directory; set VESTIBULE_AUDIT_DIR)`.
- **`cut`:**
  - 4096 ASCII bytes stay whole;
  - 4095 ASCII bytes plus `é` cut before the `é`;
  - a surrogate-escaped string doesn't raise;
  - the result is always valid UTF-8 and at most the limit.

### 5.2 The setting (`tests/test_config.py`)

- **Accepted:** the default, `~/x`, and an absolute path.
- **Refused, each a `ConfigError` naming `VESTIBULE_AUDIT_DIR`:**
  - a relative path;
  - the workspace itself, a dir inside the workspace, and the workspace's parent;
  - the same three for the rootfs dir;
  - a case variant of the workspace (`casefold`);
  - an explicit `~nosuchuser/x`, POSIX only.
- **Windows only:** `\audit` and `C:audit` (drive-relative).
- **W4:** with no home directory (a stand-in `expanduser`) and absolute workspace and rootfs
  settings, the default is not a config error.
- **Through the real server:** a relative value makes a `run_code` call `Blocked:` with the
  config message. The existing config-error test pattern spawns the console script.

### 5.3 The handler (`tests/test_audit.py`, fake wardens, `RecordingAudit`, every platform)

- **Criterion 1's table, through `call_tool`.** Each case leaves exactly the records the
  contract lists:
  - empty code, oversize code and selection refused give `blocked(refused)`;
  - a normal run gives `start` + `end(ran)`;
  - `timed_out=True` gives `end(timed_out)`;
  - `RunRefusedError` from `warden.run` gives `end(refused)`;
  - `ran=False` gives `end(not_run)`;
  - the outer deadline (a fake that raises `TimeoutError`, as `test_select.py` does) and an
    unexpected error give `end(failed)`;
  - **cancellation through an anyio task group,** as the SDK cancels, gives `start` +
    `end(cancelled)`, and the cancel propagates.
- **W1:** a cancel during selection gives `blocked(cancelled)` with no reason. An error raised
  by selection gives `blocked(failed)`, and today's `Internal error` text stays.
- **The fields:**
  - `start` has the code, its hash and size, `backend`, `runtime` (container fakes only),
    `verdict`, `image` and `timeout_s`. Code over 4 KiB is cut, with `code_truncated` set.
  - `end` has every `RunResult` field it maps (§2.4).
  - `language` is `null` for a non-enum value given to the handler directly.
- **The `audit:` lines.** A stand-in confirm returns a chosen note.
  - It sits above `isolation:` in a run result, and last in a `Blocked:` result.
  - The worse note wins: `not recorded` over `not confirmed`.
  - The run still happens: the fake warden counts its calls.
- **Wall excludes the audit's wait.** Step 1's fake clock, with a stand-in confirm that moves it
  1 s: still `wall=412ms`.
- **No record** for a config error or for `read_workspace`.
- **One end-to-end handler test with a real `AuditLog`** in a temp dir: a normal run's `start`
  and `end` land in its file, in that order, with the same `id`.

### 5.4 The backends

- **Native, pure (`tests/test_native_verdict.py`):** `_ran` is False only for an `error` first
  line, and `_not_run` gives `ran=False`.
- **Native, live (`tests/test_native.py`, Linux):**
  - a run's `run_id` is 16 hex characters, and its `image` is the marker's `manifest_digest`;
  - the output-flood test's result has `output_capped` set and `stdout_bytes` equal to the cap;
  - the stand-in helper's `error` first line gives `ran=False`.
- **Rootfs (`tests/test_rootfs.py`):** `find` returns the marker's `manifest_digest`, and None
  when it isn't a string.
- **Container, Docker-free (`tests/test_lifecycle.py`):**
  - the spawn-failure result has `ran=False` and a `run_id`;
  - a result carries the preflight's image id (a fake `_cli_status` answers `sha256:…`);
  - **A8:** `_build_command` with `VESTIBULE_AUDIT_DIR` set never names the dir in any argument.
- **Container, live (`tests/test_container.py`, Docker):**
  - a run's `image` is the `image inspect` id;
  - the existing output-flood test also checks `output_capped`.
- **Naive (`tests/test_smoke.py`):** an unknown interpreter gives `ran=False`, and `print(1)`
  gives `stdout_bytes` of 2 (3 on Windows, where print writes `\r\n`).

### 5.5 Over real stdio (`tests/test_stdio_e2e.py::test_stdio_happy_path_and_clean_exit`, Docker)

The test already makes four container runs over real stdio, with `clientInfo` set to
`vestibule-e2e`. It now also reads its audit dir (`tmp_path / "audit"`) after the clean exit:
- four `start`s and four `end(ran)`s, paired by `id`;
- `backend` is `container`, with `runtime` set;
- each `backend_run` is one of the run ids the server logged (`server.run_ids`);
- `client` is `{"name": "vestibule-e2e", "version": "0"}`.

Its existing check that stdout carries only JSON-RPC now runs with the audit on (criterion 8).

### 5.6 Mutants (each must turn a test red; a green control run first)

1. **The confirm wait is 20 s:** the blocked-writer test.
2. **No `end` on cancel:** the cancellation test.
3. **No done-callback logging:** the blocked-writer test's late-result check.
4. **The future bound to a loop saved at thread start:** the two-loops test.
5. **`O_BINARY` dropped:** the one-line test, red on Windows only. This mutant runs on Windows.
6. **`put` instead of `put_nowait`:** the full-queue test, which fails on its 0.1 s bound once
   the timer frees the writer.
7. **The pre-write `st_nlink` check removed:** the between-records deletion test (POSIX).
8. **`_dirty` set even when nothing was written:** the no-byte failure test (empty line).
9. **`_dirty` never set:** the torn-write test.
10. **`cut` with a strict encode:** the surrogate `cut` test.
11. **`ran=False` mapped to `ran`:** criterion 1's `not_run` case.
12. **A refused run recorded as `blocked`:** criterion 1's `refused` case.
13. **`confirm` returns the first note, not the worse:** the worse-note test.
14. **`wall_ms` computed after the audit's wait:** the wall test.
15. **The overlap check without `casefold`:** test_config's case-variant case.
16. **The W4 default made a config error:** test_config's no-home case.
17. **`_ran` true for an `error` line:** the pure `_ran` test.
18. **The `audit:` line below `isolation:`:** the line-position test.
19. **`output_capped` left False in native:** the native flood test.

### 5.7 The gate

The full gate: Windows, then WSL on 3.14 and 3.11, then stdio.

---

## 6. Docs (this step)

- **README, configuration table:** a `VESTIBULE_AUDIT_DIR` row. It gives:
  - the default;
  - the rule: absolute or `~`, outside the workspace and the rootfs dir;
  - that on Windows a custom dir inherits its folder's permissions (A2);
  - a pointer to the full section, which step 5 writes.
- **GETTING_STARTED, troubleshooting:**
  - `audit: not recorded (…)` and `audit: not confirmed (…)`: what each means, that the run
    still happened, and what to check;
  - the overlap config error.
- **ARCHITECTURE:** one paragraph on the writer thread, why it isn't `to_thread` (A7), and the
  one wait after the run (W2).
- **The contract** (`M3-audit-usage.md`): amendment blockquotes under A6 (W1), A7 (W2), A2 and A4
  (W3), and A8 (W4), once you sign them off.
- **At done:** HISTORY bullets (the sign-off, then done), a BUILD_LOG entry, and this plan's
  as-built section.

---

## 7. Files and size

| File | Change | Rough size |
|---|---|---|
| `src/vestibule/audit.py` | new: writer, builders, `cut` | ~150 |
| `src/vestibule/server.py` | `get_selection`, `AUDIT`, the handler's records, `_refuse`, `_audit_line`, `_client`, `_backend` | ~70 |
| `src/vestibule/config.py` | `audit_dir`, `audit_path`, validation | ~35 |
| `src/vestibule/backends/base.py` | six fields | ~8 |
| `src/vestibule/backends/native.py` | `_ran`, `_Run.image`, `_not_run`'s `run_id`, the result's fields | ~15 |
| `src/vestibule/backends/container.py` | `_image_ids`, the result's fields | ~10 |
| `src/vestibule/backends/naive.py` | `ran=False` twice, the byte counts | ~4 |
| `src/vestibule/rootfs.py` | `manifest_digest` | ~4 |
| `scripts/bench_coldstart.py` | `get_selection`, its own audit dir | ~4 |
| `tests/conftest.py`, `tests/stdioutil.py` | `RecordingAudit`, the spawned servers' dir | ~35 |
| `tests/test_audit.py` | new: §5.1, §5.3 | ~300 |
| `tests/test_config.py`, `test_native_verdict.py`, `test_native.py`, `test_rootfs.py`, `test_lifecycle.py`, `test_container.py`, `test_smoke.py`, `test_stdio_e2e.py`, `test_select.py` | §5.2, §5.4, §5.5, and the six `get_selection` fakes | ~110 |

src ~300, against the contract's estimate of 250: the extra is the logging callback, the
post-write check, the last-byte read, and W3's and W4's lines. Tests ~445, against its 300: the
in-memory recorder, the fakes, the stdio plumbing, and the cases the review added.

---

## 8. Review (`skeptic`, then `verifier`, 2026-11-02)

`skeptic`'s verdict: "sound with amendments", with 18 findings (5 med, 13 low, no high).
`verifier` checked findings 1–17 against the code at `40f63be`: 14 confirmed and 3 plausible
(7, 16, 17), with repros where it could run one. Finding 18 is a simplicity argument with no
claim to check. Every finding is taken. The severities below are the verifier's where it gave
one; it rated 2, 5 and 6 between low and med.

Verifier notes that changed v2's wording:
- **7:** `str(OSError)` is safe, since it shows the filename's repr. The real route is a message
  built around a configured path (`select.py:487`).
- **3:** naive has no collection cap, and the container appends notes to stderr too. Both are
  reflected in §2.3.
- **16:** the risk exists only on Windows, where the registered server is an editable install.
  The WSL Claude Code config has no Vestibule entry.

| # | Sev. | Finding | In v2 |
|---|---|---|---|
| 1 | med | One shared real writer across pytest's per-test loops: acknowledgements bound to a stale loop, exact-text tests coupled to disk timing, the cancel record read racily | `RecordingAudit` stand-in for every test (§2.6); a loop per record (§2.1); the two-loops test |
| 2 | med | Records that are never confirmed (cancel paths) could fail with no log, against A3 and criterion 6 | A done callback on every future logs any note (§2.1) |
| 3 | med | Sizes from decoded text count U+FFFD as 3 bytes; a capped flood looks like an OOM kill | W3: raw collected bytes and `output_capped` |
| 4 | med | A cancel during the start's wait was claimed but untested | Moot under W2: there's no wait before the run |
| 5 | med | `scripts/bench_coldstart.py` calls `get_warden` and would write to the real dir | In the file table; `get_selection`; its own audit dir (§2.6) |
| 6 | low | Mutant 8 would hang the suite: `Queue.put` blocks the loop, and there's no pytest-timeout | A timer frees the writer; the test checks a 0.1 s bound (§5.1) |
| 7 | low | The builders sit outside the never-raise guard; `cut`'s strict encode can raise on a surrogate-escaped reason | `cut` encodes with `replace`; the builders can't raise (§2.1) |
| 8 | low | The container's `image_ran` can be stale for a re-pulled tag | Stated as "the id the preflight saw" (§2.3) |
| 9 | low | `start_note or end_note` hides a definite failure behind a soft one | `confirm` returns the worse note |
| 10 | low | `docker`/`podman` as `backend` departs from the contract's words | `backend` = `container`, plus `runtime` |
| 11 | low | W1's cancel has no "message the model saw"; `blocked` stops meaning refused | `outcome` on `blocked`; no reason for a cancel |
| 12 | low | Newline repair's empty lines conflict with A10's "skipped and counted" | Repair only after a partial write, plus a last-byte read on open: no empty lines |
| 13 | low | §5.5 described an e2e test that doesn't exist in that form | Named the real test and its four runs (§5.5) |
| 14 | low | macOS' `normcase` doesn't fold case, so a case-variant overlap passes | `casefold` on every platform (§2.2) |
| 15 | low | A host with no home directory and absolute settings works today, and would regress | W4 |
| 16 | low | Listing the real dir around the gate can false-fail (Victor's own server writes there) | Dropped; the e2e test proves the plumbing (§2.6) |
| 17 | low | A deletion during a write is acknowledged; an exception in `settle` kills the thread | A post-write check; the loop body's `try` covers `settle` (§2.1) |
| 18 | low | Simplicity: `shield` plus the `done()` check are redundant; optionally one wait after the run | `shield` kept (late notes must still be logged), the `done()` check dropped; the one wait is W2 |


---

## 9. As built (2026-11-03)

Built as §2–§6 say, except for the changes below. One came from the build, two from the first
gate run, and the rest from the review.

**From the build:**
- **`confirm` uses `asyncio.wait(futs, timeout=2)`,** not `asyncio.shield` inside
  `asyncio.timeout`. `asyncio.wait` never cancels what it waits on, so a late result still
  arrives and its done callback still logs it, with less machinery. `critic` checked it and
  found it equivalent.
- **`_failure(e)`** writes the first line of a call that ended with no `RunResult`, for both
  the selection phase and the run phase. So today's texts and log lines stay the same,
  including `Execution failed: sandbox did not return in time.` for a `TimeoutError`.

**From the first gate run (test-only):**
- **conftest's autouse fixture doesn't use `monkeypatch`.** An autouse fixture that requests
  it sets `monkeypatch` up first, so its undo ran after `test_native.py`'s `no_scope_left`
  teardown. Six tests that strip `busctl`/`systemctl` from `PATH` then failed that check.
  The fixture now saves and restores `server.AUDIT` itself.
- **The drive-relative test runs everywhere.** On POSIX, `\audit` and `C:audit` are relative
  paths and get the same message, and the Linux gate fails on any skip.

**Review.** `critic` ran twice, each round checked by `verifier`:
- **Round 1:** 1 medium and 5 low findings, all confirmed (#6 only in part).
- **Round 2,** on the fixes: no high or medium findings. 8 low ones: 7 confirmed, 1 plausible.

| # | Sev. | Finding | Done |
|---|---|---|---|
| 1.1 | med | `_overlap` called `realpath` inside `from_env`, which runs at the server's import. Windows' realpath raises for an offline network drive (WinError 1231), and only `ConfigError` is caught there, so the server died at startup | `_resolved`: `realpath`, or the path as written when that raises. The overlap is still caught. Test: realpath raising for one path |
| 1.2 | low | A default that expands to a relative path (relative `HOME`) became a config error blaming a setting the user never set | W4 extended: that default isn't a config error either, and the writer gives it the no-home note |
| 1.3 | low | The `setup` command the server prints didn't pass `VESTIBULE_AUDIT_DIR`, though setup now checks it. A valid server config could print a command that setup refuses | `setup_command` names the audit dir, except a default with no usable home, which setup skips too. Test: the printed command's settings pass `from_env` |
| 1.4 | low | The `RunResult` comment said the sizes were the guest's bytes; a container's include the runtime CLI's own stderr, as on exit 125 | Comment reworded. The container can't separate the two streams |
| 1.5 | low | The writer reopened only on `st_nlink == 0`; after an error on the held fd (ESTALE on NFS) every later record failed | Any failure drops the fd. The next record reopens the file and reads its last byte, so newline repair now lives in `_open` alone (below) |
| 1.6 | low | Test size; one redundant test | `test_written_records_add_nothing_to_the_result` removed. The other one named was refuted as a duplicate: it alone checks an `end` record's `reason` |
| 2.1 | low | A failure of the `fstat` after a completed write reported the record as lost | That check has its own `try`: the record counts as written, and the fd is dropped |
| 2.2 | low | `_resolved`'s `abspath` fallback calls `getcwd`, which raises on POSIX once the cwd is deleted (a relative workspace setting) | `normpath`, which touches nothing |
| 2.3 | low (plausible) | `realpath` at import can block on a dead hard-mounted NFS export, so the server never answers `initialize` | **Not fixed, recorded** (BUILD_LOG known issues). Avoiding it means either comparing paths lexically, which lets a symlink hide an overlap (A8's safety side), or a thread with a timeout at import. A workspace, rootfs or audit dir on a dead hard mount is unusable anyway |
| 2.4 | low | An explicit `~/audit` with a relative home got "must be an absolute path or start with ~", which it does | That case now says `expands to <path>, which isn't absolute: the home directory is relative` |
| 2.5 | low | `setup_command`'s W4 branch had no test | Test with both home stand-ins; mutants 27 and 28 |
| 2.6 | low | The reopen test failed only an `os.write`, not the `fstat` before it | Parametrized over both; mutant 21 |
| 2.7 | low | This plan described the old newline repair | This section |
| 2.8 | low | Size; the home stand-ins written twice | `tests/homeutil.py`, shared by three test files |

**Newline repair, as built (1.5).** §2.1 said `_dirty` is set when a write raises after some
bytes, and that the last-byte read happens only on a pid-and-second collision. Now every
failure inside `_write` drops the fd. The next record opens the file again, and `_open`
reads its last byte, so `_dirty` comes from the file itself. The outcome is the same: no
glued records and no empty lines. One rule now covers a torn write, a stale handle and a
deleted file.

**Mutants.** All 28 turned a test red, each with exit code 1 (a failing test, not a
collection error), after a green control. Every touched file was restored byte for byte and
checked against its hash.
- §5.6's 19, with 8 and 9 moved onto `_open`'s last-byte read (always dirty, never dirty).
- Nine for the review's fixes:
  - no drop on failure;
  - the pre-write `fstat` outside the `try`;
  - the writer's relative-home check removed;
  - a post-write `fstat` failure reported as lost;
  - no `OSError` fallback in `_resolved`;
  - the relative-default exemption removed;
  - the audit dir left out of `setup_command`;
  - its `RuntimeError` guard removed;
  - its `is_absolute` check removed.

27 ran in WSL, and `O_BINARY` (5) ran on Windows. The harness lives in the session scratchpad
only.

**Gate (after round 2's fixes):**
- check_env, ruff and mypy clean on both platforms.
- Windows: 533 passed, 140 POSIX/Linux-only skips.
- Linux, Python 3.14: 693 passed, 0 skipped.
- Linux, Python 3.11 Docker-free: 650.
- stdio: 5 of 5, the extended `test_stdio_happy_path_and_clean_exit` included.

**Size,** against §7's estimates:
- **src:** `audit.py` is 239 lines, about 170 of them code (estimate ~150). The other `src/`
  files changed by +235/−45 (estimate ~150). The difference is the review fixes, and
  `_failure`/`_ms_since`/`_backend`/`_client` as separate helpers.
- **tests:** `test_audit.py` 606, `conftest.py` 43 and `homeutil.py` 25. The other test files
  changed by +302/−20. That's ~980 against ~445: the two review rounds added 9 tests, the
  outcome table and the field tests are exact-match, and the writer's failure tests each need a
  stand-in for one syscall.
