"""The server's log: a log call never waits for stderr, so a client that stops reading it can't
stall the event loop (M3 step 4, contract A11; docs/plans/M3/M3-step4-stderr.md)."""
from __future__ import annotations

import logging
import os
import queue
import sys
import threading

_QUEUE_MAX = 1000  # A11
_DRAIN_S = 0.02  # Q2: the most an exit path waits for queued lines
_LINE_MAX = 16384  # Q3, in characters: a longer line keeps its first and last 8192
_KEEP = _LINE_MAX // 2

_installed: NonBlockingHandler | None = None


class NonBlockingHandler(logging.Handler):
    """Log lines go into a bounded queue; one daemon thread writes them to its own dup of fd 2.
    A log call never waits for stderr: on a full queue the line is dropped and counted."""

    def __init__(self, fd: int = 2) -> None:
        super().__init__()
        # SimpleQueue, not queue.Queue: Queue holds a plain lock while it allocates, so a
        # finalizer that logs from a GC pass inside put() deadlocks its own thread (plan Q1).
        self._lines: queue.SimpleQueue[str | threading.Event] = queue.SimpleQueue()
        self.dropped = 0  # touched only in emit, which Handler.handle serializes
        # The bytes sys.stderr would have written, as before this handler existed.
        self._encoding = getattr(sys.stderr, "encoding", None) or "utf-8"
        self._writer: threading.Thread | None = None
        try:
            out = os.dup(fd)
        except OSError:  # no stderr at all: lines queue up to the bound, then drop
            return
        self._writer = threading.Thread(target=self._write_lines, args=(out,),
                                        name="vestibule-stderr", daemon=True)
        self._writer.start()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            line = self.format(record)
        except Exception:
            self.handleError(record)
            return
        self._put(line)

    def handleError(self, record: logging.LogRecord) -> None:
        """Plan Q4: one queued note in place of the stock traceback, which writes sys.stderr
        under this handler's lock and so, on a stuck pipe, froze every thread's logging.
        The call's own arguments are left out: one of them may be what raised."""
        if not logging.raiseExceptions:
            return
        exc = sys.exc_info()[1]
        try:
            detail = f"{type(exc).__name__}: {exc}"
        except Exception:  # an exception whose str() raises: its name only
            detail = type(exc).__name__
        self._put(f"{record.name}: a log call failed to format ({detail}) at "
                  f"{record.pathname}:{record.lineno}")

    def _put(self, line: str) -> None:
        if len(line) > _LINE_MAX:  # plan Q3: a message from outside can be any length
            line = f"{line[:_KEEP]}...[cut {len(line) - 2 * _KEEP} chars]...{line[-_KEEP:]}"
        if self._lines.qsize() >= _QUEUE_MAX:
            self.dropped += 1
            return
        if self.dropped:  # the notice goes in where the gap is, with the line after it
            line = f"stderr was not being read: dropped {self.dropped} log line(s)\n{line}"
            self.dropped = 0
        self._lines.put(line)

    def drain(self) -> bool:
        """Wait at most _DRAIN_S for every line queued so far to be written. True if it was;
        False at once without a writer."""
        if self._writer is None:
            return False
        reached = threading.Event()
        self._lines.put(reached)
        return reached.wait(_DRAIN_S)

    def flush(self) -> None:  # logging.shutdown calls this at a normal exit
        self.drain()

    def _write_lines(self, fd: int) -> None:
        while True:
            item = self._lines.get()
            if isinstance(item, threading.Event):
                item.set()
                continue
            try:
                data = (item + "\n").replace("\n", os.linesep).encode(self._encoding,
                                                                     "backslashreplace")
                while data:  # a signal can cut a blocking write short
                    data = data[os.write(fd, data):]
            except Exception:  # EPIPE once the client closed its end, EBADF: drop the line
                pass


def install(fmt: str) -> None:
    """The server's switch: root logs through a NonBlockingHandler, warnings included."""
    global _installed
    handler = NonBlockingHandler()
    logging.basicConfig(handlers=[handler], force=True, format=fmt)
    logging.captureWarnings(True)
    _installed = handler


def drain() -> bool:
    """The exit paths' wait: the installed handler's drain, or False if none is installed."""
    return False if _installed is None else _installed.drain()
