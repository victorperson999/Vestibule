"""Tests for `vestibule-mcp ui` (M5 step 1, docs/plans/M5/M5-step1-ui.md §5).

Covers HTTP hardening, `_AuditStore` cache/safety/summary/filtering, `inspect_workspace_entry`
and `/api/workspace`, CLI/stdout purity, browser launch guard, and deadlock-free shutdown.
"""
from __future__ import annotations

import json
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import webbrowser
from pathlib import Path

import pytest
from homeutil import NoHome, RelativeHome
from stdioutil import SERVER_EXE

from vestibule import config, workspace

A = "20261125T100000Z-100.jsonl"
B = "20261125T100500Z-200.jsonl"
RLO = chr(0x202E)

REQUIRED_SEC_HEADERS = {
    "content-security-policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
        "img-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
    ),
    "x-content-type-options": "nosniff",
    "referrer-policy": "no-referrer",
    "cache-control": "no-store",
}


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in list(os.environ):
        if name.startswith("VESTIBULE_"):
            monkeypatch.delenv(name)


def _ts(n: int) -> str:
    return f"2026-11-25T10:{n // 60:02d}:{n % 60:02d}.000Z"


def _start(i: str, n: int, code: str = "print(1)", language: str = "python", **kw) -> dict:
    return {
        "v": 1, "kind": "start", "id": i, "ts": _ts(n), "language": language,
        "code": code, "code_truncated": False, "backend": "native", "verdict": "native", **kw,
    }


def _end(i: str, n: int, outcome: str = "ran", **kw) -> dict:
    rec = {
        "v": 1, "kind": "end", "id": i, "ts": _ts(n), "outcome": outcome,
        "isolation": "native", "exit_code": 0, "wall_ms": 84,
        "usage": {"cpu_ms": 31, "mem_peak_mb": 11, "pids_peak": 2},
        "limit_hits": {}, "denied_syscalls": [], "stdout_bytes": 2, "stderr_bytes": 0,
        "output_capped": False,
    }
    rec.update(kw)
    return rec


def _blocked(n: int, outcome: str = "refused", reason: str | None = "code is empty.",
             language: str = "python") -> dict:
    rec = {"v": 1, "kind": "blocked", "id": f"b{n}", "ts": _ts(n), "language": language,
           "outcome": outcome}
    if reason is not None:
        rec["reason"] = reason
    return rec


def _write_jsonl(directory: Path, name: str, *lines) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    p = directory / name
    with open(p, "ab") as f:
        for line in lines:
            f.write((line if isinstance(line, bytes) else json.dumps(line).encode()) + b"\n")
    return p


def _raw_http(port: int, payload: bytes, *, shutdown_wr: bool = False,
              timeout: float = 5.0) -> bytes:
    with socket.create_connection(("127.0.0.1", port), timeout=timeout) as sock:
        sock.settimeout(timeout)
        sock.sendall(payload)
        if shutdown_wr:
            sock.shutdown(socket.SHUT_WR)
        chunks = []
        while True:
            data = sock.recv(65536)
            if not data:
                break
            chunks.append(data)
        return b"".join(chunks)


def _parse_response(raw: bytes) -> tuple[str, dict[str, list[str]], bytes]:
    head, _, body = raw.partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    status_line = lines[0]
    headers: dict[str, list[str]] = {}
    for line in lines[1:]:
        k, _, v = line.partition(":")
        headers.setdefault(k.strip().lower(), []).append(v.strip())
    return status_line, headers, body


def _assert_sec_headers(headers: dict[str, list[str]]) -> None:
    assert "server" not in headers
    for k, expected in REQUIRED_SEC_HEADERS.items():
        assert headers.get(k) == [expected], f"header {k}: {headers.get(k)!r}"


def _get(srv, path: str, *, token: str | None = None, host: str | None = None,
         method: str = "GET") -> tuple[int, dict[str, list[str]], bytes]:
    port = srv.server_address[1]
    use_host = srv.expected_host if host is None else host
    lines = [f"{method} {path} HTTP/1.1"]
    if use_host != "":
        lines.append(f"Host: {use_host}")
    if token is not None:
        lines.append(f"X-Vestibule-Token: {token}")
    req = ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1")
    raw = _raw_http(port, req)
    status_line, headers, body = _parse_response(raw)
    code = int(status_line.split()[1])
    return code, headers, body


@pytest.fixture()
def ui_env(tmp_path):
    from vestibule import ui_cli

    audit_dir = tmp_path / "audit"
    ws_dir = tmp_path / "ws"
    srv = ui_cli._UiServer(
        ("127.0.0.1", 0),
        ui_cli._UiHandler,
        token="test-token-12345",
        store=ui_cli._AuditStore(audit_dir),
        assets=ui_cli._load_assets(),
        audit_dir=audit_dir,
        audit_warning=None,
        workspace_path=ws_dir,
        workspace_error=None,
        max_preview_bytes=20_000,
    )
    t = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    t.start()
    try:
        yield srv, audit_dir, ws_dir
    finally:
        srv.shutdown()
        srv.server_close()
        t.join(timeout=2.0)


# --- 1. HTTP security ------------------------------------------------------------------------

def test_host_header_enforcement(ui_env):
    srv, _, _ = ui_env
    port = srv.server_address[1]
    for path in ("/", "/api/runs"):
        # Wrong Host
        raw = _raw_http(port, (
            f"GET {path} HTTP/1.1\r\nHost: evil.example:{port}\r\n"
            f"X-Vestibule-Token: {srv.token}\r\n\r\n"
        ).encode())
        status, headers, _ = _parse_response(raw)
        assert status.startswith("HTTP/1.0 403 ")
        _assert_sec_headers(headers)

        # Missing Host
        raw = _raw_http(port, (
            f"GET {path} HTTP/1.1\r\nX-Vestibule-Token: {srv.token}\r\n\r\n"
        ).encode())
        status, headers, _ = _parse_response(raw)
        assert status.startswith("HTTP/1.0 403 ")
        _assert_sec_headers(headers)

        # Duplicate Host
        raw = _raw_http(port, (
            f"GET {path} HTTP/1.1\r\nHost: {srv.expected_host}\r\nHost: {srv.expected_host}\r\n"
            f"X-Vestibule-Token: {srv.token}\r\n\r\n"
        ).encode())
        status, headers, _ = _parse_response(raw)
        assert status.startswith("HTTP/1.0 403 ")
        _assert_sec_headers(headers)


def test_malformed_and_http09_requests_emit_hardened_headers(ui_env, monkeypatch):
    from vestibule import ui_cli

    srv, _, _ = ui_env
    port = srv.server_address[1]

    raw_bad = _raw_http(port, b"BADREQUEST\r\n", shutdown_wr=True)
    status_bad, headers_bad, _ = _parse_response(raw_bad)
    assert status_bad.startswith("HTTP/1.0 400 "), raw_bad
    _assert_sec_headers(headers_bad)

    raw_09 = _raw_http(port, b"GET /\r\n\r\n")
    status_09, headers_09, _ = _parse_response(raw_09)
    assert status_09.startswith("HTTP/1.0 403 "), raw_09
    _assert_sec_headers(headers_09)

    # Python 3.14+ sets self.headers = {} (a plain dict with no get_all) on HTTP/0.9 requests.
    real_parse = ui_cli._UiHandler.parse_request

    def parse_as_py314_http09(self):
        ok = real_parse(self)
        if ok:
            self.headers = {}
        return ok

    monkeypatch.setattr(ui_cli._UiHandler, "parse_request", parse_as_py314_http09)
    raw_09_dict = _raw_http(port, b"GET /\r\n\r\n")
    status_09_dict, headers_09_dict, _ = _parse_response(raw_09_dict)
    assert status_09_dict.startswith("HTTP/1.0 403 "), raw_09_dict
    _assert_sec_headers(headers_09_dict)

    # Malformed IPv6 URL target raises ValueError in urlsplit -> hardened 400
    monkeypatch.setattr(ui_cli._UiHandler, "parse_request", real_parse)
    raw_ipv6 = _raw_http(
        port, f"GET http://[::1/ HTTP/1.1\r\nHost: {srv.expected_host}\r\n\r\n".encode(),
    )
    status_ipv6, headers_ipv6, _ = _parse_response(raw_ipv6)
    assert status_ipv6.startswith("HTTP/1.0 400 "), raw_ipv6
    _assert_sec_headers(headers_ipv6)


def test_slowloris_enforces_total_request_deadline(ui_env, monkeypatch):
    from vestibule import ui_cli

    srv, _, _ = ui_env
    monkeypatch.setattr(ui_cli, "_REQUEST_DEADLINE_S", 0.6)
    port = srv.server_address[1]
    t0 = time.monotonic()
    with socket.create_connection(("127.0.0.1", port), timeout=5.0) as sock:
        sock.settimeout(2.0)
        sock.sendall(b"G")
        closed = False
        for ch in b"ET / HTTP/1.1\r\nHost: x\r\n":
            time.sleep(0.25)
            try:
                sock.sendall(bytes([ch]))
                # Check if server already closed our connection
                sock.settimeout(0.05)
                try:
                    data = sock.recv(16)
                    if data == b"":
                        closed = True
                        break
                except TimeoutError:
                    pass
                sock.settimeout(2.0)
            except OSError:
                closed = True
                break
            if time.monotonic() - t0 > 3.0:
                break
        elapsed = time.monotonic() - t0
    assert closed and elapsed < 2.0, f"closed={closed}, elapsed={elapsed:.2f}s"


def test_api_token_enforcement(ui_env):
    srv, _, _ = ui_env
    port = srv.server_address[1]
    for path in ("/api/meta", "/api/runs", "/api/workspace"):
        # Missing token
        code, headers, _ = _get(srv, path)
        assert code == 403
        _assert_sec_headers(headers)

        # Wrong token
        code, headers, _ = _get(srv, path, token="wrong-token")
        assert code == 403
        _assert_sec_headers(headers)

        # Query-string-only token (must NOT be accepted)
        code, headers, _ = _get(srv, f"{path}?token={srv.token}")
        assert code == 403
        _assert_sec_headers(headers)

        # Duplicate token headers
        raw = _raw_http(port, (
            f"GET {path} HTTP/1.1\r\nHost: {srv.expected_host}\r\n"
            f"X-Vestibule-Token: {srv.token}\r\nX-Vestibule-Token: {srv.token}\r\n\r\n"
        ).encode())
        status, headers, _ = _parse_response(raw)
        assert status.startswith("HTTP/1.0 403 ")
        _assert_sec_headers(headers)

        # Non-ASCII latin-1 token header must return hardened 403 (never TypeError)
        raw_non_ascii = _raw_http(
            port,
            f"GET {path} HTTP/1.1\r\nHost: {srv.expected_host}\r\n".encode("ascii")
            + b"X-Vestibule-Token: \xe9\r\n\r\n",
        )
        status_na, headers_na, _ = _parse_response(raw_non_ascii)
        assert status_na.startswith("HTTP/1.0 403 "), raw_non_ascii
        _assert_sec_headers(headers_na)


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD", "TRACE",
                                    "FOO"])
def test_non_get_methods_return_405_with_allow_get(ui_env, method):
    srv, _, _ = ui_env
    code, headers, body = _get(srv, "/", method=method)
    assert code == 405
    assert headers.get("allow") == ["GET"]
    _assert_sec_headers(headers)
    if method == "HEAD":
        assert body == b""


def test_favicon_returns_204_without_content_headers(ui_env):
    srv, _, _ = ui_env
    code, headers, body = _get(srv, "/favicon.ico")
    assert code == 204
    assert body == b""
    _assert_sec_headers(headers)
    assert "content-type" not in headers
    assert "content-length" not in headers


def test_concurrency_cap_and_thread_start_failure(ui_env, monkeypatch):
    srv, _, _ = ui_env
    port = srv.server_address[1]

    # Occupy all 16 slots directly and confirm the 17th connection is dropped cleanly.
    acquired = 0
    while srv._slots.acquire(blocking=False):
        acquired += 1
    assert acquired == 16
    try:
        try:
            raw = _raw_http(port, f"GET / HTTP/1.1\r\nHost: {srv.expected_host}\r\n\r\n".encode())
        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
            raw = b""
        assert raw == b""
    finally:
        for _ in range(acquired):
            srv._slots.release()

    # After release, normal requests succeed again.
    code, _, _ = _get(srv, "/")
    assert code == 200

    # Simulated Thread.start failure releases the slot and closes the socket.
    closed = []

    class DummySock:
        def close(self):
            closed.append(True)

    monkeypatch.setattr(srv, "shutdown_request", lambda req: closed.append(req))

    def boom_start(self):
        raise RuntimeError("cannot start thread")

    monkeypatch.setattr(threading.Thread, "start", boom_start)
    with pytest.raises(RuntimeError, match="cannot start thread"):
        srv.process_request(DummySock(), ("127.0.0.1", 9999))
    assert len(closed) == 1
    # Verify all 16 slots are still available (none leaked).
    re_acquired = 0
    while srv._slots.acquire(blocking=False):
        re_acquired += 1
    for _ in range(re_acquired):
        srv._slots.release()
    assert re_acquired == 16


def test_handle_error_with_no_stderr_writes_nothing_to_stdout(ui_env, monkeypatch, capsys):
    srv, _, _ = ui_env
    monkeypatch.setattr(sys, "stderr", None)
    try:
        raise RuntimeError("boom\x1b[31m")
    except RuntimeError:
        srv.handle_error(None, ("127.0.0.1", 12345))
    assert capsys.readouterr().out == ""


def test_port_exclusivity_blocks_hijack_bind(ui_env):
    srv, _, _ = ui_env
    port = srv.server_address[1]
    assert srv.allow_reuse_address is False
    hijack = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if sys.platform == "win32":
            hijack.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        with pytest.raises(OSError):
            hijack.bind(("127.0.0.1", port))
    finally:
        hijack.close()


def test_static_assets_security_audit(ui_env):
    from vestibule import ui_cli

    srv, _, _ = ui_env
    for path, expected_ctype in (
        ("/", "text/html; charset=utf-8"),
        ("/app.css", "text/css; charset=utf-8"),
        ("/app.js", "application/javascript; charset=utf-8"),
    ):
        code, headers, body = _get(srv, path)
        assert code == 200 and body
        assert headers.get("content-type") == [expected_ctype]
        _assert_sec_headers(headers)

    code, headers, _ = _get(srv, "/no-such-route")
    assert code == 404
    _assert_sec_headers(headers)

    assets = ui_cli._load_assets()
    assert set(assets) == {"/", "/app.css", "/app.js"}
    html = assets["/"][0].decode("utf-8")
    css = assets["/app.css"][0].decode("utf-8")
    js = assets["/app.js"][0].decode("utf-8")

    # Zero external URLs across all assets (the W3C SVG XML namespace URI is not a fetch URL)
    for name, text in (("index.html", html), ("app.css", css), ("app.js", js)):
        cleaned = text.replace("http://www.w3.org/2000/svg", "")
        assert "http://" not in cleaned and "https://" not in cleaned, f"external URL in {name}"
        assert "\r\n" not in text, f"CRLF in {name}"

    # Zero inline <script> or <style> blocks in index.html
    assert not re.search(r"<style\b", html, re.IGNORECASE)
    for m in re.finditer(r"<script\b([^>]*)>(.*?)</script>", html, re.IGNORECASE | re.DOTALL):
        assert 'src="' in m.group(1) and m.group(2).strip() == "", m.group(0)

    # Zero dangerous DOM sinks or inline styles in app.js
    forbidden_js = (
        "innerHTML", "outerHTML", "insertAdjacentHTML", "document.write",
        "eval(", ".style.", 'setAttribute("style"', "setAttribute('style'",
    )
    for token in forbidden_js:
        assert token not in js, f"forbidden sink {token!r} found in app.js"


# --- 2. Audit store & /api/runs --------------------------------------------------------------

def test_runs_pairing_orphans_blocked_and_exact_summary(ui_env):
    srv, audit_dir, _ = ui_env
    _write_jsonl(
        audit_dir, A,
        _start("c1", 1, "print('ok')"),
        _end("c1", 2, outcome="ran", isolation="native", denied_syscalls=["mount"]),
        _start("c2", 3, "print('deg')"),
        _end("c2", 4, outcome="ran", isolation="native-degraded",
             isolation_detail="cgroups unavailable"),
        _start("c3", 5, "while true; do :; done", "bash"),
        _end("c3", 6, outcome="timed_out", isolation="container", exit_code=-1,
             usage={"cpu_ms": None, "mem_peak_mb": None, "pids_peak": None},
             not_measured="container backend does not measure cpu/memory/pids"),
        _start("c4", 7, "print('not_run')"),
        _end("c4", 8, outcome="not_run", isolation="none", exit_code=125),
        _start("c5", 9, "print('refused_after_start')"),
        {"v": 1, "kind": "end", "id": "c5", "ts": _ts(10), "outcome": "refused",
         "wall_ms": 5001, "reason": "busy"},
        _blocked(11, outcome="refused", reason="empty code"),
        _start("c6", 12, "import time\ntime.sleep(5)"),  # no end
        _end("c7", 13, outcome="ran", isolation="container"),  # orphan end
    )
    code, headers, body = _get(srv, "/api/runs", token=srv.token)
    assert code == 200
    _assert_sec_headers(headers)
    data = json.loads(body)
    assert data["audit_dir_exists"] is True
    assert data["file_count"] == 1
    assert data["total_calls"] == 8
    assert data["matched_calls"] == 8
    assert data["returned_calls"] == 8
    assert data["skipped_lines"] == 0
    assert data["failed_files"] == 0

    s = data["summary"]
    assert s["native"] == 1        # c1 only; c2 is native-degraded, NOT counted in native
    assert s["container"] == 2     # c3 + c7
    assert s["degraded"] == 1      # c2
    assert s["none"] == 1          # c4 only; c5/c6/blocked have isolation=None -> NOT counted
    assert s["blocked"] == 2       # c5 (refused) + b11 (blocked)
    assert s["timed_out"] == 1     # c3
    assert s["no_end"] == 1        # c6
    assert s["denied"] == 1        # c1

    by_id = {c["id"]: c for c in data["calls"]}
    assert by_id["c6"]["has_start"] is True and by_id["c6"]["has_end"] is False
    assert by_id["c7"]["has_start"] is False and by_id["c7"]["has_end"] is True
    assert by_id["b11"]["has_start"] is False and by_id["b11"]["has_end"] is False
    # Newest-first ordering
    assert data["calls"][0]["id"] == "c7"
    assert data["calls"][-1]["id"] == "c1"


def test_runs_hostile_json_nan_surrogates_wrong_types_and_caps(ui_env):
    srv, audit_dir, _ = ui_env
    big_code = "x" * 10_000
    nan_line = b'{"v":1,"kind":"blocked","id":"nan1","ts":"2026-11-25T10:00:01.000Z","outcome":"refused","wall_ms":NaN}'
    inf_line = b'{"v":1,"kind":"blocked","id":"inf1","ts":"2026-11-25T10:00:02.000Z","outcome":"refused","wall_ms":Infinity}'
    surrogate_line = (
        b'{"v":1,"kind":"start","id":"s1","ts":"2026-11-25T10:00:03.000Z",'
        b'"language":"python","code":"bad\\ud800code"}'
    )
    wrong_types = _end(
        "s1", 4,
        isolation=12345,
        exit_code=True,
        wall_ms=False,
        usage={"cpu_ms": True, "mem_peak_mb": "bad", "pids_peak": 1.5},
        limit_hits={"ok": 3, "bad_bool": True, "bad_str": "nope"},
        denied_syscalls=["mount", 42, "x" * 200] + [f"s{i}" for i in range(300)],
    )
    big_start = _start("big1", 5, big_code, client={"name": "a" * 300, "version": 99})
    _write_jsonl(audit_dir, A, nan_line, inf_line, surrogate_line, wrong_types, big_start)

    code, _, body = _get(srv, "/api/runs", token=srv.token)
    assert code == 200
    # Strict JSON parse (rejects NaN/Infinity)
    data = json.loads(body.decode("utf-8"), parse_constant=lambda x: (_ for _ in ()).throw(ValueError(x)))
    assert data["skipped_lines"] == 2  # NaN and Infinity rejected
    by_id = {c["id"]: c for c in data["calls"]}
    assert "s1" in by_id
    s1 = by_id["s1"]
    assert s1["isolation"] is None
    assert s1["exit_code"] is None
    assert s1["wall_ms"] is None
    assert s1["usage"] == {"cpu_ms": None, "mem_peak_mb": None, "pids_peak": None}
    assert s1["limit_hits"] == {"ok": 3}
    assert len(s1["denied_syscalls"]) == 256
    assert all(len(x.encode("utf-8")) <= 64 for x in s1["denied_syscalls"])
    assert len(by_id["big1"]["code"].encode("utf-8")) <= 4096
    assert len(by_id["big1"]["client"]["name"].encode("utf-8")) <= 128


def test_runs_oversized_line_drained_as_one_skip(ui_env):
    srv, audit_dir, _ = ui_env
    huge = b'{"v":1,"kind":"blocked","id":"huge","ts":"2026-11-25T10:00:01.000Z","reason":"' + (b"a" * 100_000) + b'"}'
    _write_jsonl(audit_dir, A, huge, _blocked(2, reason="after huge"))
    code, _, body = _get(srv, "/api/runs", token=srv.token)
    assert code == 200
    data = json.loads(body)
    assert data["skipped_lines"] == 1
    assert data["total_calls"] == 1
    assert data["calls"][0]["reason"] == "after huge"


def test_runs_skips_symlink_and_posix_fifo_without_blocking(ui_env, monkeypatch):
    from vestibule import ui_cli

    srv, audit_dir, _ = ui_env
    _write_jsonl(audit_dir, A, _blocked(1, reason="real"))
    try:
        os.symlink(audit_dir / A, audit_dir / B)
        has_link = True
    except OSError:
        has_link = False

    fifo_name = "20261125T101000Z-300.jsonl"
    if sys.platform != "win32":
        os.mkfifo(audit_dir / fifo_name)

    code, _, body = _get(srv, "/api/runs", token=srv.token)
    assert code == 200
    data = json.loads(body)
    assert data["total_calls"] == 1
    if has_link or sys.platform != "win32":
        assert data["file_count"] == 1

    # Directly exercise POSIX _read_file against a FIFO (returns None, no hang, no leaked fd)
    # and a symlink (O_NOFOLLOW raises OSError).
    if sys.platform != "win32":
        store = ui_cli._AuditStore(audit_dir)
        opened: list[int] = []
        closed: list[int] = []
        real_open = os.open
        real_close = os.close

        def spy_open(path, flags, *args, **kwargs):
            fd = real_open(path, flags, *args, **kwargs)
            opened.append(fd)
            return fd

        def spy_close(fd):
            closed.append(fd)
            return real_close(fd)

        monkeypatch.setattr(os, "open", spy_open)
        monkeypatch.setattr(os, "close", spy_close)
        fake_st = os.stat(audit_dir / A, follow_symlinks=False)
        assert store._read_file(str(audit_dir / fifo_name), fifo_name, fake_st) is None
        assert len(opened) == 1 and closed == opened

        with pytest.raises(OSError):
            store._read_file(str(audit_dir / B), B, fake_st)


def test_audit_store_cache_hit_and_live_invalidations(tmp_path, monkeypatch):
    from vestibule import ui_cli

    audit_dir = tmp_path / "audit"
    store = ui_cli._AuditStore(audit_dir)

    # Missing dir: passive, does not create it
    snap0 = store.snapshot()
    assert snap0.audit_dir_exists is False and snap0.calls == []
    assert not audit_dir.exists()

    # Open append handle (exact AuditLog flags): live append visible across polls
    audit_dir.mkdir()
    p = audit_dir / A
    flags = os.O_RDWR | os.O_APPEND | os.O_CREAT | getattr(os, "O_BINARY", 0)
    fd = os.open(p, flags, 0o600)
    try:
        os.write(fd, (json.dumps(_start("live1", 1, "first")) + "\n").encode())
        snap1 = store.snapshot()
        assert len(snap1.calls) == 1 and snap1.calls[0]["has_end"] is False

        # Unchanged poll returns cached snapshot object
        snap1_again = store.snapshot()
        assert snap1_again is snap1

        # Append end record while writer keeps fd open -> invalidates cache immediately
        os.write(fd, (json.dumps(_end("live1", 2)) + "\n").encode())
        snap2 = store.snapshot()
        assert snap2 is not snap1
        assert len(snap2.calls) == 1 and snap2.calls[0]["has_end"] is True
    finally:
        os.close(fd)

    # Cache file B first, then mutate B and make it unreadable -> B's stale data must be evicted
    _write_jsonl(audit_dir, B, _blocked(3, reason="b_initial"))
    snap_with_b = store.snapshot()
    assert snap_with_b.failed_files == 0 and len(snap_with_b.calls) == 2

    real_read = ui_cli._AuditStore._read_file

    def fail_b(self, path, name, st):
        if name == B:
            raise PermissionError("simulated unreadable")
        return real_read(self, path, name, st)

    _write_jsonl(audit_dir, B, _blocked(4, reason="b_second"))
    monkeypatch.setattr(ui_cli._AuditStore, "_read_file", fail_b)
    snap_err = store.snapshot()
    assert snap_err.failed_files == 1
    assert [c["id"] for c in snap_err.calls] == ["live1"]  # B's stale cached call evicted
    monkeypatch.setattr(ui_cli._AuditStore, "_read_file", real_read)
    snap_ok = store.snapshot()
    assert snap_ok.failed_files == 0 and len(snap_ok.calls) == 3

    # File deletion invalidates cache
    (audit_dir / B).unlink()
    snap_del = store.snapshot()
    assert len(snap_del.calls) == 1

    # Deleted audit_dir clears cache and does not recreate dir
    p.unlink()
    audit_dir.rmdir()
    snap_gone = store.snapshot()
    assert snap_gone.audit_dir_exists is False and snap_gone.calls == []
    assert not audit_dir.exists()


def test_audit_store_concurrent_snapshot_cannot_poison_cache(tmp_path, monkeypatch):
    """3-thread race: T1 reads stale A(S1) and stalls; T2 caches fresh A(S2); T3 scans (A@S2, B)
    and reads only B; T1 resumes and commits before T3 finishes. Subsequent snapshot() calls must
    never stay poisoned with A(S1)."""
    from vestibule import ui_cli

    audit_dir = tmp_path / "audit"
    _write_jsonl(audit_dir, A, _blocked(1, reason="a_s1"))
    store = ui_cli._AuditStore(audit_dir)

    t1_read_done = threading.Event()
    t1_release = threading.Event()
    t3_read_done = threading.Event()
    t3_release = threading.Event()

    real_read = ui_cli._AuditStore._read_file
    a_read_count = 0
    read_lock = threading.Lock()

    def coord_read(self, path, name, st):
        nonlocal a_read_count
        res = real_read(self, path, name, st)
        if name == A:
            with read_lock:
                a_read_count += 1
                nth = a_read_count
            if nth == 1:
                t1_read_done.set()
                assert t1_release.wait(timeout=5.0)
        elif name == B:
            t3_read_done.set()
            assert t3_release.wait(timeout=5.0)
        return res

    monkeypatch.setattr(ui_cli._AuditStore, "_read_file", coord_read)

    # T1 starts and stalls after reading A(S1)
    t1_out: list[object] = []
    t1 = threading.Thread(target=lambda: t1_out.append(store.snapshot()))
    t1.start()
    assert t1_read_done.wait(timeout=5.0)

    # Mutate A to S2, then T2 runs to completion caching A(S2)
    _write_jsonl(audit_dir, A, _blocked(2, reason="a_s2"))
    snap_t2 = store.snapshot()
    assert {c["reason"] for c in snap_t2.calls} == {"a_s1", "a_s2"}

    # Add B, then T3 starts: sees A@S2 already cached, so T3 only reads B and stalls
    _write_jsonl(audit_dir, B, _blocked(3, reason="b1"))
    t3_out: list[object] = []
    t3 = threading.Thread(target=lambda: t3_out.append(store.snapshot()))
    t3.start()
    assert t3_read_done.wait(timeout=5.0)

    # Release T1 first (stale A@S1 commits), wait for T1 to finish, then release T3
    t1_release.set()
    t1.join(timeout=5.0)
    assert not t1.is_alive()

    t3_release.set()
    t3.join(timeout=5.0)
    assert not t3.is_alive()

    # T3's own returned snapshot must already be consistent (not corrupted by T1 committing late),
    # and subsequent snapshot() calls must also stay fresh with all of A(S2) + B.
    assert len(t3_out) == 1
    assert {c["reason"] for c in t3_out[0].calls} == {"a_s1", "a_s2", "b1"}
    monkeypatch.setattr(ui_cli._AuditStore, "_read_file", real_read)
    final_snap = store.snapshot()
    assert {c["reason"] for c in final_snap.calls} == {"a_s1", "a_s2", "b1"}

    # Also verify an older scan finishing AFTER a newer scan never drops the newer scan's file.
    c_name = "20261125T101000Z-300.jsonl"
    old_done = threading.Event()
    old_release = threading.Event()
    old_blocked = False

    def stall_once(self, path, name, st):
        nonlocal old_blocked
        res = real_read(self, path, name, st)
        if name == A and not old_blocked:
            old_blocked = True
            old_done.set()
            assert old_release.wait(timeout=5.0)
        return res

    _write_jsonl(audit_dir, A, _blocked(4, reason="a_s3"))
    monkeypatch.setattr(ui_cli._AuditStore, "_read_file", stall_once)
    t_old = threading.Thread(target=store.snapshot)
    t_old.start()
    assert old_done.wait(timeout=5.0)

    # Newer scan sees new file C and commits it
    _write_jsonl(audit_dir, c_name, _blocked(5, reason="c1"))
    snap_new = store.snapshot()
    assert "c1" in {c["reason"] for c in snap_new.calls}

    # Older scan finishes AFTER newer scan -> must NOT evict C from cache
    old_release.set()
    t_old.join(timeout=5.0)
    assert not t_old.is_alive()
    assert store.snapshot() is snap_new
    assert "c1" in {c["reason"] for c in store.snapshot().calls}


def test_runs_server_side_filter_and_search_before_limit(ui_env):
    srv, audit_dir, _ = ui_env
    recs = []
    # Put an old needle at the very beginning (oldest), then 15 newer filler records
    recs.append(_start("old_needle", 0, "find_me_secret()"))
    recs.append(_end("old_needle", 1, outcome="timed_out", isolation="container"))
    for i in range(1, 16):
        recs.append(_start(f"fill{i}", i * 2, f"print({i})"))
        recs.append(_end(f"fill{i}", i * 2 + 1))
    _write_jsonl(audit_dir, A, *recs)

    # Limit=3 with q=find_me_secret must still find old_needle
    code, _, body = _get(srv, "/api/runs?limit=3&q=find_me_secret", token=srv.token)
    assert code == 200
    data = json.loads(body)
    assert data["total_calls"] == 16
    assert data["matched_calls"] == 1
    assert data["returned_calls"] == 1
    assert data["calls"][0]["id"] == "old_needle"

    # Limit=3 with filter=timed_out must find old_needle
    code, _, body = _get(srv, "/api/runs?limit=3&filter=timed_out", token=srv.token)
    assert code == 200
    data = json.loads(body)
    assert data["matched_calls"] == 1 and data["calls"][0]["id"] == "old_needle"


# --- 3. Workspace jail & /api/workspace ------------------------------------------------------

def test_workspace_dir_file_truncation_special_chars_and_over_scan(ui_env, monkeypatch):
    srv, _, ws_dir = ui_env

    # Missing root -> passive, NOT created
    code, _, body = _get(srv, "/api/workspace?path=.", token=srv.token)
    assert code == 200
    data = json.loads(body)
    assert data == {"kind": "missing", "path": ".", "display_path": ".", "root_exists": False}
    assert not ws_dir.exists()

    # Traversal MUST return refused even when root does not exist
    for bad in ("../etc/passwd", "C:\\foo", "NUL", "con.txt"):
        q = urllib.parse.urlencode({"path": bad})
        code, _, body = _get(srv, f"/api/workspace?{q}", token=srv.token)
        assert code == 200
        assert json.loads(body)["kind"] == "refused", bad

    # Populate workspace
    ws_dir.mkdir()
    (ws_dir / "hello.txt").write_text("hello world", encoding="utf-8")
    (ws_dir / "big.txt").write_text("a" * 30_000, encoding="utf-8")
    special = "a+b#c&d%20.txt"
    (ws_dir / special).write_text("special ok", encoding="utf-8")

    # Dir listing
    code, _, body = _get(srv, "/api/workspace?path=.", token=srv.token)
    data = json.loads(body)
    assert data["kind"] == "dir"
    assert {e["name"] for e in data["entries"]} == {"hello.txt", "big.txt", special}

    # File preview + truncation
    code, _, body = _get(srv, "/api/workspace?path=big.txt", token=srv.token)
    data = json.loads(body)
    assert data["kind"] == "file" and data["truncated"] is True and data["max_bytes"] == 20_000
    assert len(data["content"]) == 20_000

    # Special characters (+, #, &, %)
    q = urllib.parse.urlencode({"path": special})
    code, _, body = _get(srv, f"/api/workspace?{q}", token=srv.token)
    data = json.loads(body)
    assert data["kind"] == "file" and data["content"] == "special ok"

    # Missing file inside existing root
    code, _, body = _get(srv, "/api/workspace?path=nope.txt", token=srv.token)
    assert json.loads(body) == {
        "kind": "missing", "path": "nope.txt", "display_path": "nope.txt", "root_exists": True,
    }

    # over_scan hidden count exactness
    over_dir = ws_dir / "over"
    over_dir.mkdir()
    for i in range(6):
        (over_dir / f"f{i}.txt").write_text("x", encoding="utf-8")
    monkeypatch.setattr(workspace, "_MAX_SCAN", 5)
    monkeypatch.setattr(workspace, "_MAX_DIR_ENTRIES", 3)
    res = workspace.inspect_workspace_entry(ws_dir, "over", 20_000)
    assert isinstance(res, workspace.WorkspaceDir)
    assert res.over_scan is True and res.total_scanned == 5 and len(res.entries) == 3
    assert res.hidden == 3  # 5 + 1 - 3 - 0


def test_workspace_broken_symlink_root_and_live_symlink_inside(ui_env, tmp_path):
    srv, _, ws_dir = ui_env

    # Broken symlink/junction at workspace root -> refused, never "missing root"
    dangling_target = tmp_path / "does-not-exist-target"
    created_root_link = False
    try:
        os.symlink(dangling_target, ws_dir, target_is_directory=True)
        created_root_link = True
    except OSError:
        if sys.platform == "win32":
            import _winapi

            _winapi.CreateJunction(str(dangling_target), str(ws_dir))
            created_root_link = True
    assert created_root_link
    code, _, body = _get(srv, "/api/workspace?path=.", token=srv.token)
    assert json.loads(body)["kind"] == "refused"
    if sys.platform == "win32":
        ws_dir.rmdir() if ws_dir.is_dir() else ws_dir.unlink()
    else:
        ws_dir.unlink()

    # Live symlink/junction inside workspace: listed as kind="symlink", direct access refused
    ws_dir.mkdir()
    (ws_dir / "real").mkdir()
    (ws_dir / "real" / "f.txt").write_text("hi", encoding="utf-8")
    try:
        os.symlink(ws_dir / "real", ws_dir / "linkdir", target_is_directory=True)
    except OSError:
        if sys.platform == "win32":
            import _winapi

            _winapi.CreateJunction(str(ws_dir / "real"), str(ws_dir / "linkdir"))
        else:
            raise

    code, _, body = _get(srv, "/api/workspace?path=.", token=srv.token)
    entries = {e["name"]: e for e in json.loads(body)["entries"]}
    assert entries["linkdir"]["kind"] == "symlink"

    code, _, body = _get(srv, "/api/workspace?path=linkdir", token=srv.token)
    assert json.loads(body)["kind"] == "refused"


def test_workspace_bidi_and_control_escaped_in_display_name_and_path(ui_env):
    srv, _, ws_dir = ui_env
    ws_dir.mkdir()
    odd_dir = f"dir{RLO}x"
    odd_file = f"f{RLO}txt.exe"
    (ws_dir / odd_dir).mkdir()
    (ws_dir / odd_dir / odd_file).write_text("content", encoding="utf-8")

    code, _, body = _get(srv, "/api/workspace?path=.", token=srv.token)
    entries = {e["name"]: e for e in json.loads(body)["entries"]}
    assert entries[odd_dir]["display_name"] == "dir\\u202ex"

    q = urllib.parse.urlencode({"path": f"{odd_dir}/{odd_file}"})
    code, _, body = _get(srv, f"/api/workspace?{q}", token=srv.token)
    data = json.loads(body)
    assert data["kind"] == "file"
    assert RLO not in data["display_path"]
    assert data["display_path"] == "dir\\u202ex/f\\u202etxt.exe"

    # Newline in requested path is escaped in display_path on refusal/missing too
    q_nl = urllib.parse.urlencode({"path": "sub\nfake"})
    code, _, body = _get(srv, f"/api/workspace?{q_nl}", token=srv.token)
    data = json.loads(body)
    assert "\n" not in data["display_path"]
    assert data["display_path"] == "sub\\x0afake"


def test_workspace_error_and_meta_oserror_handling(ui_env, monkeypatch):
    srv, _, _ = ui_env

    # Unexpandable workspace (workspace_path is None)
    monkeypatch.setattr(srv, "workspace_path", None)
    monkeypatch.setattr(srv, "workspace_error", "Could not determine home directory.")
    code, _, body = _get(srv, "/api/workspace?path=.", token=srv.token)
    assert code == 200
    data = json.loads(body)
    assert data["kind"] == "error" and "home directory" in data["reason"]

    # Raw OSError from os.stat on /api/meta does not cause 500
    real_stat = os.stat

    def bad_stat(path, *args, **kwargs):
        if str(path) == str(srv.audit_dir):
            raise PermissionError("access denied")
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(os, "stat", bad_stat)
    code, _, body = _get(srv, "/api/meta", token=srv.token)
    assert code == 200
    meta = json.loads(body)
    assert meta["audit_dir_exists"] is False
    assert meta["workspace_dir"] is None
    assert meta["workspace_error"] == "Could not determine home directory."


# --- 4. CLI, stdout purity, browser guard, shutdown ------------------------------------------

def test_importing_server_does_not_import_ui_cli_or_http_server():
    code = (
        "import sys, vestibule.server; "
        "assert 'vestibule.ui_cli' not in sys.modules; "
        "assert 'http.server' not in sys.modules"
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr


@pytest.mark.parametrize("args", [
    ["--bad-flag"], ["--port"], ["--port=abc"], ["--port=65536"], ["--port=-1"],
    ["--port=²"], ["--port", "8000", "--port", "8001"], ["--no-open", "--no-open"],
    ["extra"],
])
def test_bad_cli_args_exit_2_with_empty_stdout(monkeypatch, capsys, args):
    from vestibule import ui_cli

    assert ui_cli.main(args) == 2
    out, err = capsys.readouterr()
    assert out == ""
    assert "usage: vestibule-mcp ui" in err


@pytest.mark.parametrize("stand_in, value", [
    (NoHome, None),
    (RelativeHome, None),
    (None, "relative/audit\x1b[31m"),
])
def test_invalid_audit_dir_exits_1_with_sanitized_stderr_and_empty_stdout(
    monkeypatch, capsys, stand_in, value
):
    from vestibule import ui_cli

    if stand_in is not None:
        monkeypatch.setattr(config, "Path", stand_in)
    if value is not None:
        monkeypatch.setenv("VESTIBULE_AUDIT_DIR", value)
    assert ui_cli.main(["--no-open"]) == 1
    out, err = capsys.readouterr()
    assert out == ""
    assert "\x1b" not in err
    assert err.startswith("vestibule ui: ")


def test_linux_browser_guard_and_gui_classification(monkeypatch, capsys):
    from vestibule import ui_cli

    # Headless Linux skips webbrowser.get / open completely
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    called = []
    monkeypatch.setattr(webbrowser, "get", lambda: called.append(True))
    t = ui_cli._open_browser_async("http://127.0.0.1:1234/#tok")
    assert t is None and called == []
    assert "no display detected" in capsys.readouterr().err

    # _is_gui_browser classification
    assert ui_cli._is_gui_browser(webbrowser.BackgroundBrowser("xdg-open")) is True
    assert ui_cli._is_gui_browser(webbrowser.Mozilla("firefox")) is True
    assert ui_cli._is_gui_browser(webbrowser.Chrome("chromium")) is True
    assert ui_cli._is_gui_browser(webbrowser.Konqueror()) is True
    assert ui_cli._is_gui_browser(webbrowser.GenericBrowser("lynx")) is False
    assert ui_cli._is_gui_browser(webbrowser.Elinks("elinks")) is False

    # webbrowser.Error in daemon thread never crashes
    monkeypatch.setenv("DISPLAY", ":0")

    def raise_err():
        raise webbrowser.Error("no runnable browser")

    monkeypatch.setattr(webbrowser, "get", raise_err)
    t = ui_cli._open_browser_async("http://127.0.0.1:1234/#tok")
    assert t is not None
    t.join(timeout=2.0)
    assert not t.is_alive()
    assert "open the URL above manually" in capsys.readouterr().err


def test_serve_shutdown_and_non_main_thread_safety(tmp_path, monkeypatch):
    from vestibule import ui_cli

    # 1. _serve shuts down cleanly within 2 s via stop Event
    audit_dir = tmp_path / "audit"
    srv = ui_cli._UiServer(
        ("127.0.0.1", 0),
        ui_cli._UiHandler,
        token="tok",
        store=ui_cli._AuditStore(audit_dir),
        assets=ui_cli._load_assets(),
        audit_dir=audit_dir,
        audit_warning=None,
        workspace_path=tmp_path / "ws",
        workspace_error=None,
        max_preview_bytes=20_000,
    )
    stop = threading.Event()
    timer = threading.Timer(0.15, stop.set)
    timer.start()
    t0 = time.monotonic()
    ui_cli._serve(srv, stop)
    elapsed = time.monotonic() - t0
    timer.cancel()
    assert elapsed < 2.0

    # 2. Running main() in a non-main thread must never raise ValueError on signal.signal
    monkeypatch.setenv("VESTIBULE_AUDIT_DIR", str(audit_dir))
    monkeypatch.setenv("VESTIBULE_WORKSPACE", str(tmp_path / "ws"))
    real_serve = ui_cli._serve

    def fast_serve(s, ev):
        ev.set()
        real_serve(s, ev)

    monkeypatch.setattr(ui_cli, "_serve", fast_serve)
    box: list[object] = []

    def worker():
        try:
            box.append(ui_cli.main(["--no-open"]))
        except BaseException as e:
            box.append(e)

    th = threading.Thread(target=worker)
    th.start()
    th.join(timeout=3.0)
    assert box == [0]

    # 3. SIGTERM handler must already be installed BEFORE "listening on" is printed
    prev_handler = signal.getsignal(signal.SIGTERM)
    seen_during_listening: list[object] = []
    real_err = ui_cli._err

    def spy_err(msg: str) -> None:
        if "listening on " in msg:
            seen_during_listening.append(signal.getsignal(signal.SIGTERM))
        real_err(msg)

    monkeypatch.setattr(ui_cli, "_err", spy_err)
    assert ui_cli.main(["--no-open"]) == 0
    assert len(seen_during_listening) == 1
    assert seen_during_listening[0] is not prev_handler
    assert signal.getsignal(signal.SIGTERM) == prev_handler


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX SIGTERM shutdown test")
def test_posix_sigterm_shuts_down_cleanly(tmp_path):
    env = {k: v for k, v in os.environ.items() if not k.startswith("VESTIBULE_")}
    env["VESTIBULE_AUDIT_DIR"] = str(tmp_path / "audit")
    env["VESTIBULE_WORKSPACE"] = str(tmp_path / "ws")
    proc = subprocess.Popen(
        [str(SERVER_EXE), "ui", "--no-open"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    try:
        assert proc.stderr is not None
        line = proc.stderr.readline()
        assert "listening on http://127.0.0.1:" in line, line
        t0 = time.monotonic()
        proc.send_signal(signal.SIGTERM)
        rc = proc.wait(timeout=3.0)
        elapsed = time.monotonic() - t0
        assert rc == 0 and elapsed < 2.0
        assert proc.stdout is not None and proc.stdout.read() == ""
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=2.0)
