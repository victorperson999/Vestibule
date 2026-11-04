"""The audit log: one JSONL file per server process, written by one daemon thread (M3 A5–A7;
docs/plans/M3/M3-step2-audit-writer.md).

The event loop never touches the disk. `submit` serializes a record and queues it with an
acknowledgement future, which the writer thread settles through `call_soon_threadsafe`. A
record that doesn't land never stops a run (A3): the handler shows the note on the result, and
every note is logged. Records keep the code up to 4 KiB and only the sizes of the output (A2).
Nothing here writes to stdout (golden rule 1), and the thread never logs: a stuck stderr would
block it.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import queue
import secrets
import threading
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from vestibule import __version__
from vestibule.backends.base import RunResult
from vestibule.config import ALLOWED_LANGUAGES

log = logging.getLogger("vestibule.audit")

_QUEUE_MAX = 1000
_CONFIRM_S = 2
CODE_CAP = 4096
REASON_CAP = 1024
_FULL = "not recorded (the audit queue is full: the disk has stopped taking records)"
_LATE = "not confirmed (the disk didn't answer within 2 s)"
_NO_HOME = "could not find the home directory; set VESTIBULE_AUDIT_DIR"
_DELETED = "not recorded (the audit file was deleted while this record was written)"
# Without it, Windows' C runtime opens the fd in text mode and writes `\n` as `\r\n` (A5).
_O_BINARY = getattr(os, "O_BINARY", 0)

_Settle = Callable[[str | None], None]


class AuditLog:
    """The process's audit file. Built at import with no I/O; the file appears on the first
    record, named for the time this object was made (the server's start) and the pid."""

    def __init__(self, directory: str) -> None:
        self._directory = directory
        self._name = f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{os.getpid()}.jsonl"
        self._queue: queue.Queue[tuple[bytes, _Settle]] = queue.Queue(_QUEUE_MAX)
        self._thread: threading.Thread | None = None
        self._fd: int | None = None  # the thread's alone, as is _dirty
        self._dirty = False  # the file doesn't end in `\n`, as read when it was opened

    def submit(self, record: dict[str, Any]) -> asyncio.Future[str | None]:
        """Queue a record; never blocks or raises. Call it on the event loop. The future gives
        None once the record is written, or the note for the result."""
        loop = asyncio.get_running_loop()  # per record: each test runs on its own loop
        fut: asyncio.Future[str | None] = loop.create_future()
        fut.add_done_callback(_log_note)  # covers the records nobody waits for (cancels)

        def settle(note: str | None) -> None:  # on the writer thread
            loop.call_soon_threadsafe(fut.set_result, note)

        try:
            # ensure_ascii (the default): every non-ASCII and control character is a \u escape
            line = json.dumps(record, separators=(",", ":")).encode("ascii") + b"\n"
            if self._thread is None:
                thread = threading.Thread(target=self._serve, name="vestibule-audit",
                                          daemon=True)
                thread.start()
                self._thread = thread
            self._queue.put_nowait((line, settle))
        except queue.Full:
            fut.set_result(_FULL)
        except Exception as e:  # noqa: BLE001 - the run goes ahead; the result says why (A3)
            fut.set_result(f"not recorded ({e})")
        return fut

    async def confirm(self, *futs: asyncio.Future[str | None]) -> str | None:
        """Wait at most 2 s in all for the records (A7, W2), never cancelling them, so a late
        result is still logged. The worse note, or None when every record was written."""
        done, pending = await asyncio.wait(futs, timeout=_CONFIRM_S)
        notes = [f.result() for f in done]
        if pending:
            log.warning("audit: %s", _LATE)
            notes.append(_LATE)
        return max(notes, key=_rank, default=None)

    def _serve(self) -> None:
        while True:
            line, settle = self._queue.get()
            try:
                note = self._write(line)
            except Exception as e:  # noqa: BLE001 - this record is lost; the next one retries
                note = f"not recorded ({e})"
            try:
                settle(note)
            except Exception:  # noqa: BLE001 - the loop closed: nobody is waiting
                pass

    def _write(self, line: bytes) -> str | None:
        try:
            if self._fd is not None and os.fstat(self._fd).st_nlink == 0:  # deleted (POSIX)
                self._drop()
            if self._fd is None:
                self._open()
            fd = self._fd
            assert fd is not None
            data = b"\n" + line if self._dirty else line
            while data:
                data = data[os.write(fd, data):]
        except BaseException:
            # Any failure (a torn write on a full disk, a handle gone stale): the next record
            # opens the file again and reads how it ends.
            self._drop()
            raise
        self._dirty = False
        try:
            deleted = os.fstat(fd).st_nlink == 0
        except OSError:  # the record is written; the handle went bad after, so reopen next time
            self._drop()
            return None
        return _DELETED if deleted else None  # deleted during the write: the next one reopens

    def _drop(self) -> None:
        fd, self._fd = self._fd, None
        if fd is not None:
            with contextlib.suppress(OSError):
                os.close(fd)

    def _open(self) -> None:
        try:
            directory = Path(self._directory).expanduser()
        except RuntimeError:  # the default dir with no home directory (W4)
            raise RuntimeError(_NO_HOME) from None
        if not directory.is_absolute():  # the default, with a relative home (W4)
            raise RuntimeError(_NO_HOME)
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(directory / self._name,
                     os.O_RDWR | os.O_APPEND | os.O_CREAT | _O_BINARY, 0o600)
        try:
            # A file that's already there (a pid and second collision, or a reopen after a
            # failed write): start on a new line if its last record was torn.
            self._dirty = False
            if os.lseek(fd, 0, os.SEEK_END):
                os.lseek(fd, -1, os.SEEK_END)
                self._dirty = os.read(fd, 1) != b"\n"
        except BaseException:
            os.close(fd)
            raise
        self._fd = fd


def _log_note(fut: asyncio.Future[str | None]) -> None:
    if not fut.cancelled() and fut.result() is not None:
        log.warning("audit: %s", fut.result())


def _rank(note: str | None) -> int:
    """`not recorded` is worse than `not confirmed`, which is worse than a written record."""
    return 0 if note is None else 1 if note == _LATE else 2


# --- the records (the contract's §3; the plan's §2.4) ------------------------------------

def cut(text: str, limit: int) -> str:
    """At most `limit` bytes of UTF-8, cut on a character boundary. `replace` because a message
    can embed a configured path holding surrogate escapes (POSIX)."""
    return text.encode("utf-8", "replace")[:limit].decode("utf-8", "ignore")


def _head(kind: str, audit_id: str) -> dict[str, Any]:
    ts = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    return {"v": 1, "kind": kind, "id": audit_id, "ts": ts}


def _language(language: object) -> str | None:
    """Never a model-supplied string: past the SDK's schema check it's always an enum value."""
    return language if isinstance(language, str) and language in ALLOWED_LANGUAGES else None


def blocked_record(language: object, outcome: str, reason: str | None) -> dict[str, Any]:
    """A call that ended before a backend was chosen: `refused`, `cancelled` or `failed`
    (W1). A cancel has no reason: the model saw nothing."""
    rec = _head("blocked", secrets.token_hex(8))
    rec.update(pid=os.getpid(), version=__version__, language=_language(language),
               outcome=outcome)
    if reason is not None:
        rec["reason"] = cut(reason, REASON_CAP)
    return rec


def start_record(audit_id: str, *, language: str, code: str, timeout_s: int, backend: str,
                 runtime: str | None, verdict: str, image: str | None,
                 client: dict[str, str] | None) -> dict[str, Any]:
    """Queued just before `warden.run` (A6, W2)."""
    raw = code.encode()  # the SDK rejects lone surrogates, so this can't fail
    rec = _head("start", audit_id)
    rec.update(pid=os.getpid(), version=__version__, language=_language(language),
               timeout_s=timeout_s, backend=backend)
    if runtime is not None:
        rec["runtime"] = runtime
    rec.update(verdict=verdict, image=image, code_sha256=hashlib.sha256(raw).hexdigest(),
               code_bytes=len(raw), code=cut(code, CODE_CAP), code_truncated=len(raw) > CODE_CAP)
    if client is not None:
        rec["client"] = client
    return rec


def end_record(audit_id: str, outcome: str, wall_ms: int,
               reason: str | None = None) -> dict[str, Any]:
    """An `end` with no RunResult behind it: `refused`, `failed` or `cancelled` (A6)."""
    rec = _head("end", audit_id)
    rec.update(outcome=outcome, wall_ms=wall_ms)
    if reason is not None:
        rec["reason"] = cut(reason, REASON_CAP)
    return rec


def end_from_result(audit_id: str, result: RunResult, wall_ms: int) -> dict[str, Any]:
    """An `end` with the RunResult's fields: `ran`, `timed_out` or `not_run` (A6)."""
    outcome = "not_run" if not result.ran else "timed_out" if result.timed_out else "ran"
    rec = _head("end", audit_id)
    rec.update(outcome=outcome, backend_run=result.run_id, image_ran=result.image,
               exit_code=result.exit_code, timed_out=result.timed_out, wall_ms=wall_ms,
               isolation=result.isolation, isolation_detail=result.isolation_detail,
               usage={"cpu_ms": result.cpu_ms, "mem_peak_mb": result.mem_peak_mb,
                      "pids_peak": result.pids_peak},
               limit_hits=dict(result.limit_hits), not_measured=result.not_measured,
               denied_syscalls=list(result.denied_syscalls),
               stdout_bytes=result.stdout_bytes, stderr_bytes=result.stderr_bytes,
               output_capped=result.output_capped)
    return rec
