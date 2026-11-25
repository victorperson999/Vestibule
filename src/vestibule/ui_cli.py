"""`vestibule-mcp ui [--port PORT] [--no-open]`: local read-only web inspector (M5 step 1;
docs/plans/M5/M5-step1-ui.md).

A terminal command, never imported by the MCP server during normal stdio sessions. Serves a
hardened, read-only dashboard strictly on 127.0.0.1 backed by `VESTIBULE_AUDIT_DIR` JSONL logs
and `workspace.py`'s path jail. Writes only to stderr, never stdout.
"""
from __future__ import annotations

import contextlib
import hmac
import importlib.resources
import io
import json
import math
import os
import re
import secrets
import signal
import socket
import stat
import sys
import threading
import time
import urllib.parse
import webbrowser
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, BinaryIO, NoReturn, cast

from vestibule import __version__, audit, config, workspace
from vestibule.audit_cli import _ascii_unless_utf8, _clean, _err

_USAGE = "usage: vestibule-mcp ui [--port PORT] [--no-open]  (PORT: 0-65535, default 0)"
_PORT_RE = re.compile(r"[0-9]{1,5}")
_NAME = re.compile(r"[0-9]{8}T[0-9]{6}Z-[0-9]+\.jsonl")
_KINDS = ("blocked", "start", "end")
_MAX_LINE_BYTES = 65_536
_UI_MAX_PREVIEW_BYTES = 256 * 1024
_REQUEST_DEADLINE_S = 5.0
_MAX_HEAD_BYTES = 65_536
_MAX_CONCURRENT_CONNS = 16
_CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
    "img-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
)


def _load_assets() -> dict[str, tuple[bytes, str]]:
    """Load bundled static UI assets via importlib.resources (works in wheels and checkouts)."""
    pkg = importlib.resources.files("vestibule.ui")
    return {
        "/": (pkg.joinpath("index.html").read_bytes(), "text/html; charset=utf-8"),
        "/app.css": (pkg.joinpath("app.css").read_bytes(), "text/css; charset=utf-8"),
        "/app.js": (pkg.joinpath("app.js").read_bytes(), "application/javascript; charset=utf-8"),
    }


def _parse_args(args: list[str]) -> tuple[int, bool] | None:
    """Parse `[--port PORT] [--no-open]` strictly; return `(port, no_open)` or None on error."""
    port: int | None = None
    no_open = False
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--no-open":
            if no_open:
                return None
            no_open = True
            i += 1
        elif arg == "--port":
            if port is not None or i + 1 >= len(args):
                return None
            parsed = _parse_port(args[i + 1])
            if parsed is None:
                return None
            port = parsed
            i += 2
        elif arg.startswith("--port="):
            if port is not None:
                return None
            parsed = _parse_port(arg[len("--port="):])
            if parsed is None:
                return None
            port = parsed
            i += 1
        else:
            return None
    return (0 if port is None else port, no_open)


def _parse_port(raw: str) -> int | None:
    if not _PORT_RE.fullmatch(raw):
        return None
    val = int(raw)
    return val if 0 <= val <= 65535 else None


def _parse_max_preview_bytes(raw: str | None) -> int:
    default = config.Limits.max_output_bytes
    if raw is None:
        return default
    v = raw.strip()
    if not v:
        return default
    if not config._WHOLE_NUMBER.fullmatch(v):
        _err(
            f"vestibule ui: warning: VESTIBULE_MAX_OUTPUT_BYTES={_clean(config._shown(raw))} "
            f"is not a positive whole number; using {default}"
        )
        return default
    digits = v.lstrip("0")
    if not digits or len(digits) > config._MAX_DIGITS:
        _err(
            f"vestibule ui: warning: VESTIBULE_MAX_OUTPUT_BYTES={_clean(config._shown(raw))} "
            f"is out of range; using {default}"
        )
        return default
    return min(int(digits), _UI_MAX_PREVIEW_BYTES)


def _json_bytes(obj: Any) -> bytes:
    """Serialize JSON as strict ASCII (escapes surrogates, forbids NaN/Infinity)."""
    return json.dumps(obj, separators=(",", ":"), allow_nan=False).encode("ascii")


def _reject_constant(val: str) -> NoReturn:
    raise ValueError(f"invalid JSON constant: {val}")


# --- Audit store & normalization -------------------------------------------------------------

def _str_cap(val: object, max_bytes: int) -> str | None:
    if not isinstance(val, str):
        return None
    return audit.cut(val, max_bytes)


def _finite_int(val: object) -> int | None:
    return val if type(val) is int else None


def _finite_num(val: object) -> int | float | None:
    if isinstance(val, bool):
        return None
    if isinstance(val, int):
        return val
    if isinstance(val, float) and math.isfinite(val):
        return val
    return None


def _clean_client(val: object) -> dict[str, str] | None:
    if not isinstance(val, dict):
        return None
    name = _str_cap(val.get("name"), 128)
    version = _str_cap(val.get("version"), 128)
    if name is None and version is None:
        return None
    return {"name": name or "", "version": version or ""}


def _clean_limit_hits(val: object) -> dict[str, int]:
    if not isinstance(val, dict):
        return {}
    out: dict[str, int] = {}
    for k, v in val.items():
        if len(out) >= 16:
            break
        if isinstance(k, str) and type(v) is int:
            out[audit.cut(k, 64)] = v
    return out


def _clean_denied(val: object) -> list[str]:
    if not isinstance(val, list):
        return []
    out: list[str] = []
    for item in val:
        if isinstance(item, str):
            out.append(audit.cut(item, 64))
            if len(out) >= 256:
                break
    return out


def _clean_usage(val: object) -> dict[str, int | float | None]:
    if not isinstance(val, dict):
        return {"cpu_ms": None, "mem_peak_mb": None, "pids_peak": None}
    return {
        "cpu_ms": _finite_num(val.get("cpu_ms")),
        "mem_peak_mb": _finite_num(val.get("mem_peak_mb")),
        "pids_peak": _finite_int(val.get("pids_peak")),
    }


def _build_call(
    file_name: str,
    start: dict[str, Any] | None,
    end: dict[str, Any] | None,
    blocked: dict[str, Any] | None,
) -> dict[str, Any]:
    first = start or blocked or end
    assert first is not None
    has_start = start is not None
    has_end = end is not None
    if blocked is not None:
        outcome = _str_cap(blocked.get("outcome"), 128)
        reason = _str_cap(blocked.get("reason"), 1024)
    elif end is not None:
        outcome = _str_cap(end.get("outcome"), 128)
        reason = _str_cap(end.get("reason"), 1024)
    else:
        outcome = "no_end"
        reason = None

    return {
        "id": audit.cut(str(first["id"]), 128),
        "ts": audit.cut(str(first["ts"]), 128),
        "end_ts": _str_cap(end.get("ts"), 128) if end is not None else None,
        "file": file_name,
        "has_start": has_start,
        "has_end": has_end,
        "language": _str_cap((start or blocked or {}).get("language"), 128),
        "outcome": outcome,
        "code": _str_cap(start.get("code"), 4096) if start is not None else None,
        "code_bytes": _finite_int(start.get("code_bytes")) if start is not None else None,
        "code_truncated": bool(start.get("code_truncated") is True) if start is not None else False,
        "code_sha256": _str_cap(start.get("code_sha256"), 256) if start is not None else None,
        "timeout_s": _finite_num(start.get("timeout_s")) if start is not None else None,
        "backend": _str_cap(start.get("backend"), 128) if start is not None else None,
        "runtime": _str_cap(start.get("runtime"), 128) if start is not None else None,
        "verdict": _str_cap(start.get("verdict"), 128) if start is not None else None,
        "image": _str_cap(start.get("image"), 256) if start is not None else None,
        "image_ran": _str_cap(end.get("image_ran"), 256) if end is not None else None,
        "backend_run": _str_cap(end.get("backend_run"), 256) if end is not None else None,
        "client": _clean_client(start.get("client")) if start is not None else None,
        "exit_code": _finite_int(end.get("exit_code")) if end is not None else None,
        "timed_out": bool(end.get("timed_out") is True) if end is not None else False,
        "wall_ms": _finite_num(end.get("wall_ms")) if end is not None else None,
        "isolation": _str_cap(end.get("isolation"), 128) if end is not None else None,
        "isolation_detail": _str_cap(end.get("isolation_detail"), 512) if end is not None else None,
        "usage": _clean_usage(end.get("usage")) if end is not None else {
            "cpu_ms": None, "mem_peak_mb": None, "pids_peak": None,
        },
        "limit_hits": _clean_limit_hits(end.get("limit_hits")) if end is not None else {},
        "not_measured": _str_cap(end.get("not_measured"), 512) if end is not None else None,
        "denied_syscalls": _clean_denied(end.get("denied_syscalls")) if end is not None else [],
        "stdout_bytes": _finite_int(end.get("stdout_bytes")) if end is not None else None,
        "stderr_bytes": _finite_int(end.get("stderr_bytes")) if end is not None else None,
        "output_capped": bool(end.get("output_capped") is True) if end is not None else False,
        "reason": reason,
    }


def _is_degraded(c: dict[str, Any]) -> bool:
    iso = c["isolation"]
    return isinstance(iso, str) and iso.endswith("-degraded")


def _is_blocked(c: dict[str, Any]) -> bool:
    return (not c["has_start"] and not c["has_end"]) or c["outcome"] in (
        "refused", "failed", "cancelled",
    )


def _is_timed_out(c: dict[str, Any]) -> bool:
    return bool(c["outcome"] == "timed_out")


def _is_no_end(c: dict[str, Any]) -> bool:
    return bool(c["has_start"] and not c["has_end"])


def _is_denied(c: dict[str, Any]) -> bool:
    return bool(c["denied_syscalls"])


_FILTER_PREDICATES = {
    "no_end": _is_no_end,
    "blocked": _is_blocked,
    "timed_out": _is_timed_out,
    "degraded": _is_degraded,
    "denied": _is_denied,
}


def _compute_summary(calls: list[dict[str, Any]]) -> dict[str, int]:
    summary = {
        "native": 0,
        "container": 0,
        "degraded": 0,
        "none": 0,
        "blocked": 0,
        "timed_out": 0,
        "no_end": 0,
        "denied": 0,
    }
    for c in calls:
        iso = c["isolation"]
        if iso == "native":
            summary["native"] += 1
        elif iso == "container":
            summary["container"] += 1
        elif _is_degraded(c):
            summary["degraded"] += 1
        elif iso == "none":
            summary["none"] += 1
        if _is_blocked(c):
            summary["blocked"] += 1
        if _is_timed_out(c):
            summary["timed_out"] += 1
        if _is_no_end(c):
            summary["no_end"] += 1
        if _is_denied(c):
            summary["denied"] += 1
    return summary


_Sig = tuple[int, int, int, int]


@dataclass(frozen=True)
class _CachedFile:
    sig: _Sig
    calls: list[tuple[tuple[str, str, int], dict[str, Any]]]
    skipped: int


@dataclass(frozen=True)
class _Snapshot:
    audit_dir_exists: bool
    file_count: int
    skipped_lines: int
    failed_files: int
    summary: dict[str, int]
    calls: list[dict[str, Any]]


def _empty_snapshot(*, audit_dir_exists: bool, failed_files: int = 0) -> _Snapshot:
    return _Snapshot(
        audit_dir_exists=audit_dir_exists,
        file_count=0,
        skipped_lines=0,
        failed_files=failed_files,
        summary=_compute_summary([]),
        calls=[],
    )


class _AuditStore:
    """Read-only, stat-cached reader for `VESTIBULE_AUDIT_DIR` JSONL files.

    All disk I/O runs outside `self._lock`; the lock guards only the per-file cache and
    the merged snapshot. Uses `os.stat(..., follow_symlinks=False)` (never `DirEntry.stat`)
    so Windows NTFS open-handle appends and real `st_ino` are visible immediately.
    """

    def __init__(self, audit_dir: Path) -> None:
        self._audit_dir = audit_dir
        self._lock = threading.Lock()
        self._scan_gen = 0
        self._committed_gen = 0
        self._files: dict[str, _CachedFile] = {}
        self._snapshot: _Snapshot | None = None
        self._snapshot_sig: tuple[tuple[str, int, int, int, int], ...] | None = None

    def snapshot(self) -> _Snapshot:
        with self._lock:
            self._scan_gen += 1
            my_gen = self._scan_gen

        # 1. Scan directory outside lock
        try:
            with os.scandir(self._audit_dir) as it:
                candidates = [
                    (entry.name, entry.path)
                    for entry in it
                    if _NAME.fullmatch(entry.name)
                ]
        except FileNotFoundError:
            with self._lock:
                if my_gen >= self._committed_gen:
                    self._committed_gen = my_gen
                    self._files.clear()
                    self._snapshot = None
                    self._snapshot_sig = None
            return _empty_snapshot(audit_dir_exists=False)
        except OSError:
            return _empty_snapshot(audit_dir_exists=True, failed_files=1)

        # 2. Stat every matching entry via os.stat(follow_symlinks=False) outside lock
        present: dict[str, tuple[str, os.stat_result, _Sig]] = {}
        failed_files = 0
        for name, path in candidates:
            try:
                st = os.stat(path, follow_symlinks=False)
            except FileNotFoundError:
                continue
            except OSError:
                failed_files += 1
                continue
            if workspace._is_symlink_or_reparse(st) or not stat.S_ISREG(st.st_mode):
                continue
            sig: _Sig = (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)
            present[name] = (path, st, sig)

        # 3. Build directory signature
        dir_sig = tuple((name, *present[name][2]) for name in sorted(present))

        # 4. Fast-path cache check + capture reused entries locally under lock
        reused: dict[str, _CachedFile] = {}
        to_read: list[tuple[str, str, os.stat_result, _Sig]] = []
        with self._lock:
            if (
                self._snapshot is not None
                and dir_sig == self._snapshot_sig
                and failed_files == 0
            ):
                return self._snapshot
            for name, (path, st, sig) in present.items():
                cached_entry = self._files.get(name)
                if cached_entry is not None and cached_entry.sig == sig:
                    reused[name] = cached_entry
                else:
                    to_read.append((name, path, st, sig))

        # 5. Read changed/new files outside lock
        read_results: dict[str, _CachedFile] = {}
        non_regular: set[str] = set()
        for name, path, st, _expected_sig in to_read:
            try:
                cached = self._read_file(path, name, st)
            except OSError:
                failed_files += 1
                continue
            if cached is None:
                non_regular.add(name)
                continue
            read_results[name] = cached

        # 6. Build this caller's consistent snapshot locally, then commit only if newest
        view: dict[str, _CachedFile] = {**reused, **read_results}
        merged: list[tuple[tuple[str, str, int], dict[str, Any]]] = []
        skipped_lines = 0
        for cf in view.values():
            merged.extend(cf.calls)
            skipped_lines += cf.skipped
        merged.sort(key=lambda pair: pair[0], reverse=True)
        calls = [c for _, c in merged]
        snap = _Snapshot(
            audit_dir_exists=True,
            file_count=len(present) - len(non_regular),
            skipped_lines=skipped_lines,
            failed_files=failed_files,
            summary=_compute_summary(calls),
            calls=calls,
        )
        sig_exact = (
            failed_files == 0
            and not non_regular
            and all(
                name in view and view[name].sig == sig
                for name, (_, _, sig) in present.items()
            )
        )
        with self._lock:
            if my_gen >= self._committed_gen:
                self._committed_gen = my_gen
                self._files = dict(view)
                self._snapshot = snap
                self._snapshot_sig = dir_sig if sig_exact else None
        return snap

    def _read_file(self, path: str, name: str, st: os.stat_result) -> _CachedFile | None:
        if sys.platform != "win32":
            fd = os.open(
                path,
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
            )
            try:
                st_open = os.fstat(fd)
                if not stat.S_ISREG(st_open.st_mode):
                    os.close(fd)
                    return None
                f = os.fdopen(fd, "rb")
            except BaseException:
                os.close(fd)
                raise
            with f:
                return self._parse_stream(f, name, st_open)
        else:
            with open(path, "rb") as f:
                st_open = os.fstat(f.fileno())
                if (
                    not stat.S_ISREG(st_open.st_mode)
                    or (st_open.st_dev, st_open.st_ino) != (st.st_dev, st.st_ino)
                ):
                    return None
                return self._parse_stream(f, name, st_open)

    def _parse_stream(self, f: BinaryIO, name: str, st_open: os.stat_result) -> _CachedFile:
        # Capture signature BEFORE reading so concurrent appends during read are never masked.
        read_sig: _Sig = (st_open.st_dev, st_open.st_ino, st_open.st_size, st_open.st_mtime_ns)
        raw_calls: list[
            tuple[tuple[str, str, int], dict[str, Any] | None, dict[str, Any] | None,
                  dict[str, Any] | None]
        ] = []
        open_idx: dict[str, int] = {}
        skipped = 0
        line_no = 0
        while True:
            chunk = f.readline(_MAX_LINE_BYTES + 1)
            if not chunk:
                break
            if len(chunk) > _MAX_LINE_BYTES:
                if not chunk.endswith(b"\n"):
                    while True:
                        more = f.readline(_MAX_LINE_BYTES)
                        if not more or more.endswith(b"\n"):
                            break
                skipped += 1
                line_no += 1
                continue
            rec = _parse_record(chunk)
            if rec is None:
                skipped += 1
                line_no += 1
                continue
            key = (rec["ts"], name, line_no)
            kind = rec["kind"]
            if kind == "start":
                open_idx[rec["id"]] = len(raw_calls)
                raw_calls.append((key, rec, None, None))
            elif kind == "end" and rec["id"] in open_idx:
                idx = open_idx.pop(rec["id"])
                old_key, start_rec, _, _ = raw_calls[idx]
                raw_calls[idx] = (old_key, start_rec, rec, None)
            elif kind == "end":
                raw_calls.append((key, None, rec, None))
            else:
                raw_calls.append((key, None, None, rec))
            line_no += 1

        normalized = [
            (key, _build_call(name, start_rec, end_rec, blocked_rec))
            for key, start_rec, end_rec, blocked_rec in raw_calls
        ]
        return _CachedFile(sig=read_sig, calls=normalized, skipped=skipped)


def _parse_record(line: bytes) -> dict[str, Any] | None:
    try:
        rec = json.loads(line, parse_constant=_reject_constant)
    except (ValueError, RecursionError):
        return None
    if (
        isinstance(rec, dict)
        and type(rec.get("v")) is int
        and rec["v"] == 1
        and rec.get("kind") in _KINDS
        and isinstance(rec.get("id"), str)
        and isinstance(rec.get("ts"), str)
    ):
        return rec
    return None


# --- HTTP server & handler -------------------------------------------------------------------

def _dir_exists(path: Path | None) -> bool:
    if path is None:
        return False
    try:
        st = os.stat(path, follow_symlinks=False)
    except OSError:
        return False
    return stat.S_ISDIR(st.st_mode)


def _display_path(rel_path: str) -> str:
    norm = rel_path.replace("\\", "/")
    if norm in ("", "."):
        return "."
    return "/".join(workspace._shown(seg) for seg in norm.split("/"))


class _UiServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(
        self,
        server_address: tuple[str, int],
        handler_class: type[BaseHTTPRequestHandler],
        *,
        token: str,
        store: _AuditStore,
        assets: dict[str, tuple[bytes, str]],
        audit_dir: Path,
        audit_warning: str | None,
        workspace_path: Path | None,
        workspace_error: str | None,
        max_preview_bytes: int,
    ) -> None:
        self.token = token
        self.store = store
        self.assets = assets
        self.audit_dir = audit_dir
        self.audit_warning = audit_warning
        self.workspace_path = workspace_path
        self.workspace_error = workspace_error
        self.max_preview_bytes = max_preview_bytes
        self.expected_host = ""
        self._slots = threading.BoundedSemaphore(_MAX_CONCURRENT_CONNS)
        super().__init__(server_address, handler_class)

    def server_bind(self) -> None:
        if sys.platform == "win32":
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()
        self.expected_host = f"127.0.0.1:{self.server_address[1]}"

    def process_request(self, request: Any, client_address: Any) -> None:
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        t = threading.Thread(
            target=self._run_request,
            args=(request, client_address),
            daemon=True,
        )
        try:
            t.start()
        except BaseException:
            self._slots.release()
            self.shutdown_request(request)
            raise

    def _run_request(self, request: Any, client_address: Any) -> None:
        try:
            self.process_request_thread(request, client_address)
        finally:
            self._slots.release()

    def handle_error(self, request: Any, client_address: Any) -> None:
        _, exc, _ = sys.exc_info()
        if isinstance(
            exc,
            (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError),
        ):
            return
        _err(f"vestibule ui: request error: {_clean(repr(exc))}")


class _UiHandler(BaseHTTPRequestHandler):
    default_request_version = "HTTP/1.0"
    protocol_version = "HTTP/1.0"
    server_version = ""
    sys_version = ""

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _send(
        self,
        status: int,
        body: bytes,
        content_type: str,
        *,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        phrase = self.responses.get(status, ("",))[0]
        headers = [
            f"HTTP/1.0 {status} {phrase}",
            f"Content-Security-Policy: {_CSP}",
            "X-Content-Type-Options: nosniff",
            "Referrer-Policy: no-referrer",
            "Cache-Control: no-store",
            "Connection: close",
        ]
        if status != 204:
            headers.append(f"Content-Type: {content_type}")
            headers.append(f"Content-Length: {len(body)}")
        if extra_headers:
            for k, v in extra_headers.items():
                headers.append(f"{k}: {v}")
        head = ("\r\n".join(headers) + "\r\n\r\n").encode("latin-1")
        self.wfile.write(head)
        if body and getattr(self, "command", None) != "HEAD":
            self.wfile.write(body)
        self.wfile.flush()

    def send_error(
        self, code: int, message: str | None = None, explain: str | None = None,
    ) -> None:
        self._send(
            code,
            f"{code} {message or ''}\n".encode("utf-8", "replace"),
            "text/plain; charset=utf-8",
        )

    def _header_values(self, name: str) -> list[str]:
        # Python 3.14+ sets `self.headers = {}` (plain dict) on HTTP/0.9 requests.
        get_all = getattr(self.headers, "get_all", None)
        if callable(get_all):
            vals = get_all(name)
            return list(vals) if vals is not None else []
        return []

    def handle_one_request(self) -> None:
        self.close_connection = True
        srv = cast(_UiServer, self.server)

        # 1. Read raw request head under a true total request deadline
        deadline = time.monotonic() + _REQUEST_DEADLINE_S
        buf = bytearray()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            self.connection.settimeout(max(0.001, remaining))
            try:
                chunk = self.connection.recv(4096)
            except (TimeoutError, OSError):
                return
            if not chunk:
                if not buf:
                    return
                break
            buf.extend(chunk)
            idx_crlf = buf.find(b"\r\n\r\n")
            idx_lf = buf.find(b"\n\n")
            if idx_crlf != -1 or idx_lf != -1:
                end = min(i for i in (idx_crlf, idx_lf) if i != -1)
                if end > _MAX_HEAD_BYTES:
                    self._send(
                        431, b"Request Header Fields Too Large\n", "text/plain; charset=utf-8",
                    )
                    return
                break
            if len(buf) > _MAX_HEAD_BYTES:
                self._send(
                    431, b"Request Header Fields Too Large\n", "text/plain; charset=utf-8",
                )
                return

        # 2. Parse request from buffered bytes
        old_rfile = self.rfile
        self.rfile = io.BytesIO(bytes(buf))
        with contextlib.suppress(Exception):
            old_rfile.close()
        self.raw_requestline = self.rfile.readline(_MAX_HEAD_BYTES + 1)
        if not self.raw_requestline:
            return
        if not self.parse_request():
            return

        # 3. Host check (every route, every method)
        if self._header_values("Host") != [srv.expected_host]:
            self._send(403, b"Forbidden\n", "text/plain; charset=utf-8")
            return

        # 4. Method check (read-only: GET only)
        if self.command != "GET":
            self._send(
                405,
                b"Method Not Allowed\n",
                "text/plain; charset=utf-8",
                extra_headers={"Allow": "GET"},
            )
            return

        # 5. Dispatch GET route
        try:
            parsed = urllib.parse.urlsplit(self.path)
        except ValueError:
            self._send(400, b"Bad Request\n", "text/plain; charset=utf-8")
            return
        if parsed.path in srv.assets:
            data, ctype = srv.assets[parsed.path]
            self._send(200, data, ctype)
            return
        if parsed.path == "/favicon.ico":
            self._send(204, b"", "")
            return
        if parsed.path in ("/api/meta", "/api/runs", "/api/workspace"):
            tokens = self._header_values("X-Vestibule-Token")
            if (
                len(tokens) != 1
                or not tokens[0].isascii()
                or not hmac.compare_digest(tokens[0], srv.token)
            ):
                self._send(
                    403,
                    _json_bytes({"error": "invalid or missing token"}),
                    "application/json; charset=utf-8",
                )
                return
            qs = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
            try:
                if parsed.path == "/api/meta":
                    payload = self._api_meta(srv)
                elif parsed.path == "/api/runs":
                    payload = self._api_runs(srv, qs)
                else:
                    payload = self._api_workspace(srv, qs)
            except Exception as e:  # noqa: BLE001
                _err(f"vestibule ui: API error on {_clean(parsed.path)}: {_clean(repr(e))}")
                self._send(
                    500,
                    _json_bytes({"error": "internal server error"}),
                    "application/json; charset=utf-8",
                )
                return
            self._send(200, _json_bytes(payload), "application/json; charset=utf-8")
            return

        self._send(404, b"Not Found\n", "text/plain; charset=utf-8")

    def _api_meta(self, srv: _UiServer) -> dict[str, Any]:
        return {
            "version": __version__,
            "audit_dir": str(srv.audit_dir),
            "audit_dir_exists": _dir_exists(srv.audit_dir),
            "audit_warning": srv.audit_warning,
            "workspace_dir": str(srv.workspace_path) if srv.workspace_path is not None else None,
            "workspace_dir_exists": _dir_exists(srv.workspace_path),
            "workspace_error": srv.workspace_error,
            "max_preview_bytes": srv.max_preview_bytes,
        }

    def _api_runs(self, srv: _UiServer, qs: dict[str, list[str]]) -> dict[str, Any]:
        snap = srv.store.snapshot()
        limit = 200
        raw_limit = qs.get("limit", ["200"])[0]
        try:
            limit = max(1, min(int(raw_limit), 1000))
        except ValueError:
            limit = 200

        flt = qs.get("filter", ["all"])[0]
        pred = _FILTER_PREDICATES.get(flt)
        q = qs.get("q", [""])[0].strip()[:200].lower()

        matched: list[dict[str, Any]] = []
        for c in snap.calls:
            if pred is not None and not pred(c):
                continue
            if q:
                haystack = "\n".join((
                    c["id"] or "",
                    c["language"] or "",
                    c["outcome"] or "",
                    c["isolation"] or "",
                    c["isolation_detail"] or "",
                    c["code"] or "",
                    c["reason"] or "",
                    ",".join(c["denied_syscalls"]),
                )).lower()
                if q not in haystack:
                    continue
            matched.append(c)

        returned = matched[:limit]
        return {
            "audit_dir_exists": snap.audit_dir_exists,
            "file_count": snap.file_count,
            "total_calls": len(snap.calls),
            "matched_calls": len(matched),
            "returned_calls": len(returned),
            "limit": limit,
            "skipped_lines": snap.skipped_lines,
            "failed_files": snap.failed_files,
            "summary": snap.summary,
            "calls": returned,
        }

    def _api_workspace(self, srv: _UiServer, qs: dict[str, list[str]]) -> dict[str, Any]:
        rel_path = qs.get("path", ["."])[0]
        disp = _display_path(rel_path)

        # Always run lexical path validation first so traversal returns "refused" even if
        # workspace_path could not be expanded or does not exist.
        try:
            workspace.split_relpath(rel_path)
        except workspace.WorkspacePathError as e:
            return {"kind": "refused", "path": rel_path, "display_path": disp, "reason": str(e)}

        if srv.workspace_path is None:
            return {
                "kind": "error",
                "path": rel_path,
                "display_path": disp,
                "reason": srv.workspace_error or "workspace directory is unavailable",
            }

        try:
            res = workspace.inspect_workspace_entry(
                srv.workspace_path, rel_path, srv.max_preview_bytes,
            )
        except workspace.WorkspacePathError as e:
            return {"kind": "refused", "path": rel_path, "display_path": disp, "reason": str(e)}
        except (OSError, RuntimeError) as e:
            return {"kind": "error", "path": rel_path, "display_path": disp, "reason": str(e)}

        if isinstance(res, workspace.WorkspaceMissing):
            return {
                "kind": "missing",
                "path": rel_path,
                "display_path": disp,
                "root_exists": res.root_exists,
            }
        if isinstance(res, workspace.WorkspaceFile):
            return {
                "kind": "file",
                "path": rel_path,
                "display_path": disp,
                "content": res.content,
                "truncated": res.truncated,
                "max_bytes": res.max_bytes,
            }
        return {
            "kind": "dir",
            "path": rel_path,
            "display_path": disp,
            "entries": [
                {
                    "name": e.name,
                    "display_name": e.display_name,
                    "kind": e.kind,
                    "size": e.size,
                }
                for e in res.entries
            ],
            "total_scanned": res.total_scanned,
            "gone": res.gone,
            "hidden": res.hidden,
            "over_scan": res.over_scan,
        }


# --- Shutdown & safe browser launch ----------------------------------------------------------

def _is_gui_browser(browser: Any) -> bool:
    return (
        isinstance(browser, webbrowser.BackgroundBrowser)
        or (isinstance(browser, webbrowser.UnixBrowser) and getattr(browser, "background", False))
        or isinstance(browser, webbrowser.Konqueror)
    )


def _open_browser_async(url: str) -> threading.Thread | None:
    if sys.platform.startswith("linux") and not (
        os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")
    ):
        _err("vestibule ui: no display detected; open the URL above manually")
        return None

    def _launch() -> None:
        try:
            browser = webbrowser.get()
            if sys.platform.startswith("linux") and not _is_gui_browser(browser):
                _err("vestibule ui: no graphical browser detected; open the URL above manually")
                return
            if not browser.open(url):
                _err(
                    "vestibule ui: could not open browser automatically; "
                    "open the URL above manually"
                )
        except Exception as e:  # noqa: BLE001
            _err(
                f"vestibule ui: could not open browser ({_clean(str(e))}); "
                "open the URL above manually"
            )

    t = threading.Thread(target=_launch, name="vestibule-ui-browser", daemon=True)
    t.start()
    return t


def _serve(srv: _UiServer, stop: threading.Event) -> None:
    """Run the HTTP server in a daemon thread while the main thread waits on `stop`."""
    t = threading.Thread(
        target=srv.serve_forever,
        kwargs={"poll_interval": 0.2},
        name="vestibule-ui-http",
        daemon=True,
    )
    t.start()
    try:
        while not stop.wait(0.2):
            pass
    except KeyboardInterrupt:
        pass
    finally:
        srv.shutdown()
        srv.server_close()
        t.join(timeout=2.0)


def main(args: list[str]) -> int:
    """0 on clean stop, 1 on config/bind failure, 2 on bad CLI arguments."""
    _ascii_unless_utf8(sys.stderr)
    parsed = _parse_args(args)
    if parsed is None:
        _err(_USAGE)
        return 2
    port, no_open = parsed

    env = os.environ
    raw_audit = env.get("VESTIBULE_AUDIT_DIR")
    limits = config.Limits(
        audit_dir=raw_audit or config.Limits.audit_dir,
        workspace_dir=env.get("VESTIBULE_WORKSPACE") or config.Limits.workspace_dir,
        rootfs_dir=env.get("VESTIBULE_ROOTFS_DIR") or config.Limits.rootfs_dir,
    )
    audit_dir, problem = config.audit_dir(limits)
    if audit_dir is None:
        _err(f"vestibule ui: {_clean(problem) if raw_audit and problem else audit._NO_HOME}")
        return 1

    audit_warning = config._audit_problem(limits, explicit=True)
    if audit_warning:
        _err(
            f"vestibule ui: warning: {_clean(audit_warning)}. The server refuses that setting, "
            "so the records there may not be its own."
        )

    workspace_path: Path | None
    workspace_error: str | None = None
    try:
        workspace_path = limits.workspace_path
    except RuntimeError as e:
        workspace_path = None
        workspace_error = str(e)

    max_preview_bytes = _parse_max_preview_bytes(env.get("VESTIBULE_MAX_OUTPUT_BYTES"))
    assets = _load_assets()
    token = secrets.token_urlsafe(24)

    try:
        srv = _UiServer(
            ("127.0.0.1", port),
            _UiHandler,
            token=token,
            store=_AuditStore(audit_dir),
            assets=assets,
            audit_dir=audit_dir,
            audit_warning=audit_warning,
            workspace_path=workspace_path,
            workspace_error=workspace_error,
            max_preview_bytes=max_preview_bytes,
        )
    except OSError as e:
        _err(f"vestibule ui: failed to bind 127.0.0.1:{port}: {_clean(str(e))}")
        return 1

    stop = threading.Event()
    prev_sigterm = None
    installed_sigterm = False
    if hasattr(signal, "SIGTERM") and threading.current_thread() is threading.main_thread():
        prev_sigterm = signal.getsignal(signal.SIGTERM)
        signal.signal(signal.SIGTERM, lambda _signum, _frame: stop.set())
        installed_sigterm = True
    try:
        url = f"http://{srv.expected_host}/#{token}"
        _err(f"vestibule ui: listening on {url}")
        _err(f"vestibule ui: audit dir: {_clean(str(audit_dir))}")
        if workspace_path is not None:
            _err(f"vestibule ui: workspace: {_clean(str(workspace_path))}")
        elif workspace_error is not None:
            _err(f"vestibule ui: workspace unavailable: {_clean(workspace_error)}")
        _err("vestibule ui: press Ctrl+C to stop")

        if not no_open and not stop.is_set():
            _open_browser_async(url)

        if not stop.is_set():
            _serve(srv, stop)
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
        if installed_sigterm:
            signal.signal(signal.SIGTERM, prev_sigterm)
    return 0
