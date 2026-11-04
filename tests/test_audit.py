"""The audit log (docs/plans/M3/M3-step2-audit-writer.md §5.1, §5.3). Docker-free, every platform
unless marked.

The writer's tests build real AuditLogs in temp dirs and wait on the real thread, each with a
time limit. The handler's tests read conftest's in-memory RecordingAudit, cancelled calls
included, plus one end-to-end test with a real AuditLog.
"""
from __future__ import annotations

import asyncio
import errno
import hashlib
import json
import logging
import os
import re
import shutil
import stat
import sys
import threading
import time
import types
from pathlib import Path

import anyio
import pytest
from homeutil import NoHome, RelativeHome

import vestibule.server as server
from vestibule import audit
from vestibule.backends.base import RunRefusedError, RunResult
from vestibule.backends.container import ContainerBackend
from vestibule.backends.select import Selection
from vestibule.config import Limits

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX file semantics")
LATE = "not confirmed (the disk didn't answer within 2 s)"
NO_REPORT = server._NO_REPORT


def _file(directory: Path) -> Path:
    [path] = directory.glob("*.jsonl")
    return path


def _records(directory: Path) -> list[dict]:
    return [json.loads(line) for line in _file(directory).read_bytes().splitlines()]


async def _until(check, within: float = 5) -> None:
    deadline = time.monotonic() + within
    while not check():
        assert time.monotonic() < deadline, "timed out"
        await asyncio.sleep(0.01)


def _patch_os(monkeypatch, log: audit.AuditLog, name: str, *steps):
    """os.<name> (write or fstat), with each `step(real, fd, *args)` doing one of the audit fd's
    next calls in turn; every other call is the real one."""
    real, todo = getattr(os, name), list(steps)

    def stand_in(fd, *args):
        if fd == log._fd and todo:
            return todo.pop(0)(real, fd, *args)
        return real(fd, *args)

    monkeypatch.setattr(os, name, stand_in)


def _eio(real, fd, *args):
    raise OSError(errno.EIO, "Input/output error")


# --- the writer (§5.1) -------------------------------------------------------------------

async def test_a_record_lands_as_one_ascii_line(tmp_path):
    log = audit.AuditLog(str(tmp_path / "audit"))
    record = {"v": 1, "text": "é\x1b[8m\r\u202e"}
    assert await log.confirm(log.submit(record)) is None
    path = _file(tmp_path / "audit")
    assert re.fullmatch(rf"\d{{8}}T\d{{6}}Z-{os.getpid()}\.jsonl", path.name)
    data = path.read_bytes()
    assert data.endswith(b"\n") and data.count(b"\n") == 1
    assert b"\r" not in data  # O_BINARY: no \r\n on Windows
    assert data.isascii() and b"\\u00e9" in data and b"\\u001b" in data and b"\\u202e" in data
    assert json.loads(data) == record


@posix_only
async def test_the_dir_it_makes_is_0700_and_the_file_0600(tmp_path):
    log = audit.AuditLog(str(tmp_path / "audit"))
    assert await log.confirm(log.submit({"n": 1})) is None
    assert stat.S_IMODE(os.stat(tmp_path / "audit").st_mode) == 0o700
    assert stat.S_IMODE(os.stat(_file(tmp_path / "audit")).st_mode) == 0o600


async def test_a_dir_that_cant_be_made_is_not_recorded_then_retried(tmp_path):
    blocker = tmp_path / "audit"
    blocker.write_text("a file, not a dir")
    log = audit.AuditLog(str(blocker))
    note = await log.confirm(log.submit({"n": 1}))
    assert note is not None and note.startswith("not recorded (")
    blocker.unlink()
    assert await log.confirm(log.submit({"n": 2})) is None
    assert _records(blocker) == [{"n": 2}]


def test_one_log_on_two_loops_in_turn(tmp_path):
    log = audit.AuditLog(str(tmp_path))

    async def one(n: int) -> str | None:
        return await asyncio.wait_for(log.submit({"n": n}), 5)

    assert asyncio.run(one(1)) is None
    assert asyncio.run(one(2)) is None  # settled on this loop, not the first one
    assert _records(tmp_path) == [{"n": 1}, {"n": 2}]


async def test_a_writer_blocked_past_2s_is_not_confirmed_then_logged(tmp_path, monkeypatch,
                                                                       caplog):
    log = audit.AuditLog(str(tmp_path))
    gate, real, used = threading.Event(), log._write, []

    def stuck_then_fails(line):
        if not used:
            used.append(1)
            gate.wait(10)
            raise OSError("the disk went away")
        return real(line)

    monkeypatch.setattr(log, "_write", stuck_then_fails)
    with caplog.at_level(logging.WARNING, logger="vestibule.audit"):
        t0 = time.monotonic()
        assert await log.confirm(log.submit({"n": 1})) == LATE
        assert 1.8 <= time.monotonic() - t0 < 3
        gate.set()
        assert await log.confirm(log.submit({"n": 2})) is None  # the thread survived
    assert "audit: not recorded (the disk went away)" in caplog.text  # the late note
    assert _records(tmp_path) == [{"n": 2}]


async def test_a_full_queue_refuses_at_once(tmp_path, monkeypatch):
    log = audit.AuditLog(str(tmp_path))
    gate, entered, real = threading.Event(), threading.Event(), log._write

    def stuck(line):
        entered.set()
        gate.wait(10)
        return real(line)

    monkeypatch.setattr(log, "_write", stuck)
    first = log.submit({"n": 0})
    await _until(entered.is_set)  # the thread holds record 0; the queue is empty
    queued = [log.submit({"n": n}) for n in range(1, 1001)]
    # A blocking put would wait for this timer instead of failing the bound below.
    timer = threading.Timer(3, gate.set)
    timer.start()
    try:
        t0 = time.monotonic()
        over = log.submit({"n": 1001})
        assert time.monotonic() - t0 < 0.1
        assert over.done() and over.result() == (
            "not recorded (the audit queue is full: the disk has stopped taking records)")
    finally:
        gate.set()
        timer.cancel()
    assert await log.confirm(first, *queued) is None
    assert len(_records(tmp_path)) == 1001


@posix_only
async def test_a_dir_deleted_between_records_is_made_again(tmp_path):
    directory = tmp_path / "audit"
    log = audit.AuditLog(str(directory))
    assert await log.confirm(log.submit({"n": 1})) is None
    shutil.rmtree(directory)
    assert await log.confirm(log.submit({"n": 2})) is None
    assert _records(directory) == [{"n": 2}]


@posix_only
async def test_a_file_deleted_during_a_write_is_not_recorded(tmp_path, monkeypatch):
    log = audit.AuditLog(str(tmp_path))
    assert await log.confirm(log.submit({"n": 1})) is None
    path = _file(tmp_path)

    def unlink_first(real, fd, data):
        path.unlink()
        return real(fd, data)

    _patch_os(monkeypatch, log, "write", unlink_first)
    assert await log.confirm(log.submit({"n": 2})) == (
        "not recorded (the audit file was deleted while this record was written)")
    assert await log.confirm(log.submit({"n": 3})) is None  # reopened
    assert _records(tmp_path) == [{"n": 3}]


async def test_a_torn_write_puts_the_next_record_on_its_own_line(tmp_path, monkeypatch):
    log = audit.AuditLog(str(tmp_path))
    assert await log.confirm(log.submit({"n": 1})) is None

    def short(real, fd, data):  # a short write, as on a disk that is filling up
        return real(fd, data[:len(data) // 2])

    def full(real, fd, data):
        raise OSError(errno.ENOSPC, "No space left on device")

    _patch_os(monkeypatch, log, "write", short, full)
    note = await log.confirm(log.submit({"n": 2, "pad": "x" * 40}))
    assert note == "not recorded ([Errno 28] No space left on device)"
    assert await log.confirm(log.submit({"n": 3})) is None
    lines = _file(tmp_path).read_bytes().split(b"\n")
    assert lines[-1] == b"" and b"" not in lines[:-1]  # no empty line
    assert [json.loads(lines[0]), json.loads(lines[2])] == [{"n": 1}, {"n": 3}]
    with pytest.raises(ValueError):
        json.loads(lines[1])  # the torn one, for step 3's reader to skip and count


async def test_a_write_that_fails_before_any_byte_adds_no_newline(tmp_path, monkeypatch):
    log = audit.AuditLog(str(tmp_path))
    assert await log.confirm(log.submit({"n": 1})) is None

    def nothing(real, fd, data):
        raise OSError(errno.ENOSPC, "No space left on device")

    _patch_os(monkeypatch, log, "write", nothing)
    assert (await log.confirm(log.submit({"n": 2}))).startswith("not recorded (")
    assert await log.confirm(log.submit({"n": 3})) is None
    assert _file(tmp_path).read_bytes() == b'{"n":1}\n{"n":3}\n'


@pytest.mark.parametrize("before, after", [
    (b"garbage", b'garbage\n{"n":1}\n'),
    (b"garbage\n", b'garbage\n{"n":1}\n'),
])
async def test_a_file_already_there(tmp_path, before, after):
    log = audit.AuditLog(str(tmp_path))
    (tmp_path / log._name).write_bytes(before)  # the pid-and-second collision
    assert await log.confirm(log.submit({"n": 1})) is None
    assert (tmp_path / log._name).read_bytes() == after


@pytest.mark.parametrize("call", ["write", "fstat"])  # fstat: the check before the write
async def test_a_failure_on_the_held_file_reopens_it_for_the_next_record(tmp_path, monkeypatch,
                                                                         call):
    """critic, M3 step 2: a handle that has gone bad (ESTALE on NFS, say) must not fail every
    later record; the next one opens the file again."""
    log = audit.AuditLog(str(tmp_path))
    opens, real_open = [], log._open

    def counting_open():
        opens.append(1)
        real_open()

    monkeypatch.setattr(log, "_open", counting_open)
    assert await log.confirm(log.submit({"n": 1})) is None
    _patch_os(monkeypatch, log, call, _eio)
    assert await log.confirm(log.submit({"n": 2})) == "not recorded ([Errno 5] Input/output error)"
    assert await log.confirm(log.submit({"n": 3})) is None
    assert len(opens) == 2
    assert _records(tmp_path) == [{"n": 1}, {"n": 3}]


async def test_a_failure_after_the_write_still_confirms_the_record(tmp_path, monkeypatch):
    """critic round 2: the record is in the file, so the result must not say it was lost."""
    log = audit.AuditLog(str(tmp_path))
    assert await log.confirm(log.submit({"n": 1})) is None
    _patch_os(monkeypatch, log, "fstat", lambda real, fd: real(fd), _eio)  # before, then after
    assert await log.confirm(log.submit({"n": 2})) is None
    assert await log.confirm(log.submit({"n": 3})) is None
    assert _records(tmp_path) == [{"n": 1}, {"n": 2}, {"n": 3}]


def test_settle_after_the_loop_closed_keeps_the_thread_alive(tmp_path):
    log = audit.AuditLog(str(tmp_path))
    gate, real = threading.Event(), log._write

    def slow(line):
        gate.wait(10)
        return real(line)

    log._write = slow

    async def submit_and_leave() -> None:
        log.submit({"n": 1})  # never waited for

    asyncio.run(submit_and_leave())  # the loop is closed before the thread settles
    gate.set()

    async def again() -> str | None:
        return await asyncio.wait_for(log.submit({"n": 2}), 5)

    assert asyncio.run(again()) is None
    assert _records(tmp_path) == [{"n": 1}, {"n": 2}]


@pytest.mark.parametrize("stand_in", [NoHome, RelativeHome])
async def test_no_home_directory_is_not_recorded_with_the_fix(tmp_path, monkeypatch, stand_in):
    monkeypatch.chdir(tmp_path)  # a relative dir would land here
    monkeypatch.setattr(audit, "Path", stand_in)
    log = audit.AuditLog("~/.vestibule/audit")
    assert await log.confirm(log.submit({"n": 1})) == (
        "not recorded (could not find the home directory; set VESTIBULE_AUDIT_DIR)")
    assert list(tmp_path.iterdir()) == []


async def test_confirm_returns_the_worse_note(tmp_path, monkeypatch):
    monkeypatch.setattr(audit, "_CONFIRM_S", 0.1)
    log = audit.AuditLog(str(tmp_path))
    loop = asyncio.get_running_loop()
    written, lost, pending = (loop.create_future() for _ in range(3))
    written.set_result(None)
    lost.set_result("not recorded (x)")
    assert await log.confirm(written, lost) == "not recorded (x)"
    assert await log.confirm(pending, lost) == "not recorded (x)"
    assert await log.confirm(written, pending) == LATE
    assert await log.confirm(written, written) is None


def test_cut():
    assert audit.cut("a" * 4096, 4096) == "a" * 4096
    assert audit.cut("a" * 4095 + "é", 4096) == "a" * 4095  # before the partial character
    assert audit.cut("path \udcff", 100) == "path ?"  # a surrogate escape doesn't raise
    for text in ("é" * 3000, "\U0001f600" * 2000, "a\udcffb" * 900, "x"):
        for limit in (1, 2, 3, 1023, 1024):
            out = audit.cut(text, limit)
            assert len(out.encode()) <= limit  # strict: valid UTF-8


# --- the handler's records (§5.3) --------------------------------------------------------

ARGS = {"language": "python", "code": "print(1)"}


@pytest.fixture(autouse=True)
def valid_config(monkeypatch):
    monkeypatch.setattr(server, "CONFIG_ERROR", None)


class FakeWarden:
    def __init__(self, result: RunResult | None = None,
                 error: BaseException | None = None) -> None:
        self.result, self.error, self.calls = result, error, 0

    async def run(self, language, code, timeout_s, limits):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.result


def _ok(**fields) -> RunResult:
    return RunResult(stdout="1\n", stderr="", exit_code=0, timed_out=False, isolation="native",
                     **fields)


def _select(monkeypatch, warden, verdict: str = "native") -> None:
    async def get_selection():
        return Selection(warden, verdict)

    monkeypatch.setattr(server, "get_selection", get_selection)


def _kinds(records: list[dict]) -> list[tuple[str, str]]:
    return [(r["kind"], r.get("outcome", "-")) for r in records]


def _text(out) -> str:
    return "\n".join(c.text for c in out)


@pytest.mark.parametrize("args, expected", [
    ({"language": "python", "code": "  "}, [("blocked", "refused")]),
    ({"language": "python", "code": "x" * (server.LIMITS.max_code_bytes + 1)},
     [("blocked", "refused")]),
])
async def test_refused_arguments_leave_a_blocked_record(monkeypatch, recording_audit, args,
                                                        expected):
    warden = FakeWarden(_ok())
    _select(monkeypatch, warden)
    out = await server.call_tool("run_code", args)
    assert _text(out).startswith("Blocked:")
    assert _kinds(recording_audit.records) == expected
    assert recording_audit.records[0]["reason"] == _text(out).removeprefix("Blocked: ")
    assert warden.calls == 0


async def test_selection_refused_leaves_a_blocked_record(monkeypatch, recording_audit):
    async def refusing():
        raise RunRefusedError("no usable container runtime")

    monkeypatch.setattr(server, "get_selection", refusing)
    out = await server.call_tool("run_code", ARGS)
    assert _text(out) == "Blocked: no usable container runtime"
    [rec] = recording_audit.records
    assert (rec["kind"], rec["outcome"], rec["reason"]) == (
        "blocked", "refused", "no usable container runtime")


@pytest.mark.parametrize("warden, outcome", [
    (FakeWarden(_ok()), "ran"),
    (FakeWarden(RunResult(stdout="", stderr="", exit_code=-1, timed_out=True,
                          isolation="native")), "timed_out"),
    (FakeWarden(error=RunRefusedError("too many concurrent runs (max 4); retry shortly")),
     "refused"),
    (FakeWarden(RunResult(stdout="", stderr="", exit_code=-1, timed_out=False,
                          isolation="none", ran=False)), "not_run"),
    (FakeWarden(error=TimeoutError()), "failed"),  # the outer deadline's own branch
    (FakeWarden(error=RuntimeError("boom")), "failed"),
])
async def test_each_run_outcome_leaves_a_start_and_its_end(monkeypatch, recording_audit,
                                                            warden, outcome):
    _select(monkeypatch, warden)
    await server.call_tool("run_code", ARGS)
    start, end = recording_audit.records
    assert _kinds([start, end]) == [("start", "-"), ("end", outcome)]
    assert start["id"] == end["id"] and re.fullmatch(r"[0-9a-f]{16}", start["id"])
    assert warden.calls == 1


async def test_failed_and_refused_ends_keep_the_message(monkeypatch, recording_audit):
    _select(monkeypatch, FakeWarden(error=RuntimeError("boom")))
    out = await server.call_tool("run_code", ARGS)
    assert _text(out) == f"Internal error: boom\n{NO_REPORT}"  # today's text
    assert recording_audit.records[1]["reason"] == "Internal error: boom"
    _select(monkeypatch, FakeWarden(error=RunRefusedError("no slot")))
    out = await server.call_tool("run_code", ARGS)
    assert _text(out) == "Blocked: no slot"
    assert recording_audit.records[3]["reason"] == "no slot"


async def test_a_cancel_mid_run_queues_an_end_and_propagates(monkeypatch, recording_audit):
    entered = asyncio.Event()

    class Hanging:
        async def run(self, *args):
            entered.set()
            await asyncio.sleep(60)

    _select(monkeypatch, Hanging())
    returned = []

    async def call():
        returned.append(await server.call_tool("run_code", ARGS))

    async with anyio.create_task_group() as tg:  # as the SDK cancels a request
        tg.start_soon(call)
        await entered.wait()
        tg.cancel_scope.cancel()
    assert returned == []  # the cancel propagated; nothing was returned
    start, end = recording_audit.records
    assert _kinds([start, end]) == [("start", "-"), ("end", "cancelled")]
    assert start["id"] == end["id"] and "reason" not in end


async def test_a_cancel_during_selection_is_a_blocked_record(monkeypatch, recording_audit):
    entered = asyncio.Event()

    async def slow_selection():
        entered.set()
        await asyncio.sleep(60)

    monkeypatch.setattr(server, "get_selection", slow_selection)
    returned = []

    async def call():
        returned.append(await server.call_tool("run_code", ARGS))

    async with anyio.create_task_group() as tg:
        tg.start_soon(call)
        await entered.wait()
        tg.cancel_scope.cancel()
    assert returned == []
    [rec] = recording_audit.records
    assert (rec["kind"], rec["outcome"], "reason" in rec) == ("blocked", "cancelled", False)


async def test_an_error_in_selection_is_a_failed_blocked_record(monkeypatch, recording_audit):
    async def broken():
        raise RuntimeError("boom")

    monkeypatch.setattr(server, "get_selection", broken)
    out = await server.call_tool("run_code", ARGS)
    assert _text(out) == f"Internal error: boom\n{NO_REPORT}"  # today's text
    [rec] = recording_audit.records
    assert (rec["kind"], rec["outcome"], rec["reason"]) == (
        "blocked", "failed", "Internal error: boom")


async def test_the_start_record_fields(monkeypatch, recording_audit):
    warden = ContainerBackend("podman")
    monkeypatch.setattr(warden, "run", FakeWarden(_ok()).run)
    _select(monkeypatch, warden, "container-degraded")
    code = "print('é')\n" + "#" * 5000
    await server.call_tool("run_code", {"language": "node", "code": code,
                                        "timeout_seconds": 10_000})
    start = recording_audit.records[0]
    raw = code.encode()
    assert start == {
        "v": 1, "kind": "start", "id": start["id"], "ts": start["ts"],
        "pid": os.getpid(), "version": server.__version__, "language": "node",
        "timeout_s": server.LIMITS.max_timeout_s, "backend": "container", "runtime": "podman",
        "verdict": "container-degraded", "image": server.LIMITS.image_node,
        "code_sha256": hashlib.sha256(raw).hexdigest(), "code_bytes": len(raw),
        "code": raw[:4096].decode(), "code_truncated": True,
    }  # no client: the handler was called directly
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z", start["ts"])


@pytest.mark.parametrize("verdict, backend, image", [
    ("native-degraded", "native", "python"),
    ("none", "naive", None),
])
async def test_the_start_records_backend(monkeypatch, recording_audit, verdict, backend, image):
    _select(monkeypatch, FakeWarden(_ok()), verdict)
    await server.call_tool("run_code", ARGS)
    start = recording_audit.records[0]
    assert (start["backend"], "runtime" in start, start["verdict"]) == (backend, False, verdict)
    assert start["image"] == (server.LIMITS.image_python if image else None)
    assert start["code_truncated"] is False and start["code"] == "print(1)"


async def test_the_end_record_fields(monkeypatch, recording_audit):
    result = RunResult(
        stdout="x", stderr="y", exit_code=3, timed_out=False, cpu_ms=31, mem_peak_mb=11,
        pids_peak=2, limit_hits={"tasks_refused": 4}, not_measured=None,
        denied_syscalls=["ptrace"], isolation="native-degraded", isolation_detail="limits",
        run_id="0123456789abcdef", image="sha256:" + "a" * 64, stdout_bytes=5,
        stderr_bytes=0, output_capped=True,
    )
    _select(monkeypatch, FakeWarden(result))
    await server.call_tool("run_code", ARGS)
    start, end = recording_audit.records
    assert end == {
        "v": 1, "kind": "end", "id": start["id"], "ts": end["ts"], "outcome": "ran",
        "backend_run": "0123456789abcdef", "image_ran": "sha256:" + "a" * 64, "exit_code": 3,
        "timed_out": False, "wall_ms": end["wall_ms"], "isolation": "native-degraded",
        "isolation_detail": "limits", "usage": {"cpu_ms": 31, "mem_peak_mb": 11, "pids_peak": 2},
        "limit_hits": {"tasks_refused": 4}, "not_measured": None, "denied_syscalls": ["ptrace"],
        "stdout_bytes": 5, "stderr_bytes": 0, "output_capped": True,
    }
    assert type(end["wall_ms"]) is int


@pytest.mark.parametrize("language", ["cobol", 5, None])
async def test_a_bad_language_is_recorded_as_null(recording_audit, language):
    out = await server._handle_run_code({"language": language, "code": "x"})
    assert _text(out).startswith("Blocked: language must be one of")
    [rec] = recording_audit.records
    assert (rec["kind"], rec["language"]) == ("blocked", None)


async def test_the_audit_line_sits_above_isolation_and_the_run_still_happens(
        monkeypatch, recording_audit):
    recording_audit.note = "not recorded (disk full)"
    warden = FakeWarden(_ok())
    _select(monkeypatch, warden)
    lines = _text(await server.call_tool("run_code", ARGS)).splitlines()
    assert lines[-2:] == ["audit: not recorded (disk full)", "isolation: native"]
    assert warden.calls == 1
    out = await server.call_tool("run_code", {"language": "python", "code": ""})
    assert _text(out) == ("Blocked: 'code' must be a non-empty string.\n"
                          "audit: not recorded (disk full)")  # last on a Blocked: result
    _select(monkeypatch, FakeWarden(error=RuntimeError("boom")))
    assert _text(await server.call_tool("run_code", ARGS)) == (
        f"Internal error: boom\naudit: not recorded (disk full)\n{NO_REPORT}")


async def test_wall_excludes_the_audits_wait(monkeypatch, recording_audit):
    now = [0]
    monkeypatch.setattr(server, "time", types.SimpleNamespace(monotonic_ns=lambda: now[0]))

    class TimedWarden:
        async def run(self, *args):
            now[0] += 411_000_001
            return RunResult(stdout="", stderr="", exit_code=0, timed_out=False,
                             isolation="native")

    async def slow_confirm(*futs):
        now[0] += 1_000_000_000
        return None

    monkeypatch.setattr(recording_audit, "confirm", slow_confirm)
    _select(monkeypatch, TimedWarden())
    out = await server.call_tool("run_code", ARGS)
    assert _text(out) == "exit_code: 0\nusage: wall=412ms\nisolation: native"
    assert recording_audit.records[1]["wall_ms"] == 412


async def test_no_record_for_a_config_error_or_read_workspace(monkeypatch, recording_audit,
                                                               tmp_path):
    monkeypatch.setattr(server, "LIMITS", Limits(workspace_dir=str(tmp_path)))
    await server.call_tool("read_workspace", {"path": "."})
    monkeypatch.setattr(server, "CONFIG_ERROR", "Vestibule's configuration is invalid: X")
    await server.call_tool("run_code", ARGS)
    assert recording_audit.records == []


async def test_the_handler_with_a_real_log(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "AUDIT", audit.AuditLog(str(tmp_path / "audit")))
    _select(monkeypatch, FakeWarden(_ok(run_id="0123456789abcdef")))
    out = await server.call_tool("run_code", ARGS)
    assert "audit" not in _text(out)
    start, end = _records(tmp_path / "audit")  # both landed before the result came back
    assert _kinds([start, end]) == [("start", "-"), ("end", "ran")]
    assert start["id"] == end["id"] and end["backend_run"] == "0123456789abcdef"
