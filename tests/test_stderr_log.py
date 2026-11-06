"""Non-blocking stderr (M3 step 4, contract A11, criterion 9; docs/plans/M3/M3-step4-stderr.md).

Each handler here writes to a pipe of its own, never to the real stderr. The rules (plan §5.2):
a call that could block runs in a daemon thread with a join timeout, and no test closes or
writes a pipe while a thread is blocked on it (on Windows that close can wait for the blocked
call). The switch and the exits run in subprocesses: pytest resets warnings.showwarning for
every test, and its log handlers sit on the root logger.
"""
from __future__ import annotations

import inspect
import logging
import os
import secrets
import struct
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from nativeutil import native_required
from stdioutil import SIGINT, StdioServer

from vestibule import stderr_log

if sys.platform == "linux":
    import fcntl
    import termios

_NOTICE = "stderr was not being read: dropped {} log line(s)"
_FILLER = "f" * 16000  # 16 of these is more than any pipe holds; under the line cap


class _Pipe:
    """A pipe a handler under test writes to; one daemon thread reads it on request."""

    def __init__(self) -> None:
        self.r, self.w = os.pipe()
        self.got = bytearray()
        self._reader: threading.Thread | None = None

    def start_reading(self, until: str) -> None:
        """Read in a daemon thread until the line `until` has arrived."""
        end = (until + os.linesep).encode()

        def read() -> None:
            while end not in self.got:
                chunk = os.read(self.r, 65536)
                if not chunk:
                    return
                self.got += chunk

        self._reader = threading.Thread(target=read, daemon=True)
        self._reader.start()

    def wait_for(self, line: str, timeout: float = 10) -> None:
        want = (line + os.linesep).encode()
        deadline = time.monotonic() + timeout
        while want not in self.got:
            assert time.monotonic() < deadline, f"{line!r} never arrived"
            time.sleep(0.01)

    def lines(self, timeout: float = 10) -> list[str]:
        """Every line read, once the reader has seen its last one."""
        assert self._reader is not None
        self._reader.join(timeout)
        assert not self._reader.is_alive(), f"the last line never arrived: {self.got[-300:]!r}"
        return self.got.decode().split(os.linesep)[:-1]

    def close(self) -> None:
        if self._reader is not None and self._reader.is_alive():
            return  # a read is blocked on it: leave the pipe open rather than hang
        os.close(self.r)  # first: a writer blocked on this pipe now fails, and goes on
        os.close(self.w)


@pytest.fixture()
def pipe() -> Iterator[_Pipe]:
    p = _Pipe()
    yield p
    p.close()


def _handler(fd: int) -> tuple[stderr_log.NonBlockingHandler, logging.Logger]:
    """A handler on `fd`, on a logger of its own that doesn't reach root."""
    h = stderr_log.NonBlockingHandler(fd)
    h.setFormatter(logging.Formatter("%(message)s"))
    log = logging.getLogger(f"vestibule-test-{secrets.token_hex(4)}")
    log.propagate = False
    log.setLevel(logging.INFO)
    log.addHandler(h)
    return h, log


def _bounded(fn: Callable[[], Any], timeout: float = 5) -> tuple[Any, float]:
    """fn() in a daemon thread: its result and how long it took. Fails if it doesn't return."""
    out: dict[str, Any] = {}

    def call() -> None:
        t0 = time.perf_counter()  # Windows' monotonic clock ticks every ~15.6 ms
        out["result"] = fn()
        out["took"] = time.perf_counter() - t0

    t = threading.Thread(target=call, daemon=True)
    t.start()
    t.join(timeout)
    assert not t.is_alive(), f"{fn} didn't return within {timeout} s"
    return out["result"], out["took"]


def _make_stuck(h: stderr_log.NonBlockingHandler, log: logging.Logger) -> tuple[int, int]:
    """Fill the pipe, then the queue, until 5 lines in a row were dropped; each dropped call
    must return within 50 ms. Returns the last accepted `line N` and the drops after it."""
    def fill() -> tuple[int, float]:
        for _ in range(16):  # the writer stops inside these, before any `line N`
            log.info(_FILLER)
        i, slowest_drop = 0, 0.0
        while h.dropped < 5 and i < 100_000:
            t0 = time.perf_counter()
            log.info("line %d", i)
            if h.dropped:  # this call was dropped: the stuck case itself
                slowest_drop = max(slowest_drop, time.perf_counter() - t0)
            i += 1
        return i - 1 - h.dropped, slowest_drop

    (last, slowest_drop), _ = _bounded(fill, 10)  # a call that blocks fails, never hangs
    assert h.dropped >= 5, "no line was dropped behind a stuck stderr"
    assert slowest_drop < 0.05, f"a dropped log call took {slowest_drop * 1000:.1f} ms"
    return last, h.dropped


def test_a_stuck_stderr_never_blocks_a_log_call(pipe, capfd):
    """Criterion 9's unit test: on a stuck pipe log calls return at once, and the lines that
    don't fit are dropped and counted. Nothing goes to the real stderr instead."""
    h, _log = _handler(pipe.w)
    _make_stuck(h, _log)
    assert capfd.readouterr() == ("", "")


def test_the_gap_is_marked_where_it_is(pipe):
    h, log = _handler(pipe.w)
    last, dropped = _make_stuck(h, log)
    pipe.start_reading(until="after2")
    pipe.wait_for(f"line {last}")  # the queue is written out: the client reads again
    log.info("after")
    log.info("after2")
    lines = pipe.lines()
    at = lines.index("after")
    assert lines[at - 2:at + 2] == [f"line {last}", _NOTICE.format(dropped), "after", "after2"]
    assert f"line {last + 1}" not in lines  # dropped, so never written late


def test_drain_gives_up_on_a_stuck_pipe(pipe):
    h, log = _handler(pipe.w)
    for _ in range(16):
        log.info(_FILLER)
    reached, took = _bounded(h.drain)
    assert reached is False and 0.015 <= took < 0.5, took


def test_drain_waits_for_the_lines_on_a_pipe_being_read(pipe):
    h, log = _handler(pipe.w)
    pipe.start_reading(until="END")
    for i in range(10):
        log.info("line %d", i)
    for _ in range(50):  # each try waits 20 ms: a busy machine may need more than one
        reached, _took = _bounded(h.drain)
        if reached:
            break
    log.info("END")
    assert reached is True
    assert pipe.lines() == [f"line {i}" for i in range(10)] + ["END"]


def test_no_stderr_at_all():
    """fd 2 closed at start: no writer, a log call still returns, and drain doesn't wait."""
    fd = os.open(os.devnull, os.O_WRONLY)
    os.close(fd)
    h, log = _handler(fd)
    _bounded(lambda: log.info("nowhere to go"))
    reached, took = _bounded(h.drain)
    assert reached is False and took < 0.01, took


def test_a_long_line_keeps_its_ends(pipe):
    """Q3: a 40,000-character line arrives as its first and last 8,192 characters."""
    h, log = _handler(pipe.w)
    msg = "<" + "".join(chr(ord("a") + i % 26) for i in range(39_998)) + ">"
    pipe.start_reading(until="END")
    log.info("%s", msg)
    log.info("END")
    assert pipe.lines() == [f"{msg[:8192]}...[cut 23616 chars]...{msg[-8192:]}", "END"]


def test_a_failed_write_drops_only_that_line(pipe, monkeypatch):
    h, log = _handler(pipe.w)

    class FailsOnce:
        """stderr_log's `os`: its write fails once, on the line `lost`."""

        failed = False

        def write(self, fd: int, data: bytes) -> int:
            if b"lost" in data and not self.failed:
                self.failed = True
                raise BrokenPipeError("stand-in")
            return os.write(fd, data)

        def __getattr__(self, name: str) -> Any:
            return getattr(os, name)

    monkeypatch.setattr(stderr_log, "os", FailsOnce())
    pipe.start_reading(until="END")
    log.info("lost")
    log.info("END")
    assert pipe.lines() == ["END"]


class _NoStr(Exception):
    def __str__(self) -> str:
        raise RuntimeError("no str either")


class _Unprintable:
    def __str__(self) -> str:
        raise _NoStr()


def test_a_log_call_that_fails_to_format(pipe, capfd, monkeypatch):
    """Q4: a note in the pipe, never the stock traceback to sys.stderr, and nothing at all
    when logging.raiseExceptions is off. An exception whose str() raises gives its name."""
    h, log = _handler(pipe.w)
    pipe.start_reading(until="END")
    first = inspect.currentframe().f_lineno + 1
    log.info("%d", "x")
    monkeypatch.setattr(logging, "raiseExceptions", False)
    log.info("%d", "silent")
    monkeypatch.setattr(logging, "raiseExceptions", True)
    second = inspect.currentframe().f_lineno + 1
    log.info("%s", _Unprintable())
    log.info("END")
    lines = pipe.lines()
    prefix = f"{log.name}: a log call failed to format ("
    assert len(lines) == 3 and lines[2] == "END", lines
    assert lines[0].startswith(prefix + "TypeError: ") and "silent" not in lines[0]
    assert lines[0].endswith(f"test_stderr_log.py:{first}"), lines[0]
    assert lines[1].startswith(prefix + "_NoStr) at ")
    assert lines[1].endswith(f"test_stderr_log.py:{second}"), lines[1]
    assert h.dropped == 0
    assert capfd.readouterr() == ("", "")


def _env() -> dict[str, str]:
    """The child's environment, without the dev shell's VESTIBULE_* settings."""
    return {k: v for k, v in os.environ.items() if not k.startswith("VESTIBULE_")}


_SWITCH_CHILD = """
import logging, warnings
import vestibule.server as server
from vestibule import stderr_log
root = logging.getLogger()
print([type(h).__name__ for h in root.handlers])
stderr_log.install(server._LOG_FORMAT)
print([type(h).__name__ for h in root.handlers])
warnings.warn("vestibule-test-warning")
for _ in range(100):
    if stderr_log.drain():
        break
"""


def test_the_switch():
    """Importing the server keeps the plain handler (setup, audit and the tests use it);
    install() replaces it, and warnings go through it too."""
    p = subprocess.run([sys.executable, "-c", _SWITCH_CHILD], capture_output=True, text=True,
                       stdin=subprocess.DEVNULL, env=_env(), timeout=60)
    assert p.returncode == 0, p.stderr
    assert p.stdout.splitlines() == ["['StreamHandler']", "['NonBlockingHandler']"]
    assert "vestibule WARNING" in p.stderr and "UserWarning: vestibule-test-warning" in p.stderr


def _exits_with_stuck_stderr(code: str, within_s: float) -> int:
    """Run `code` in a child whose stderr nobody reads; its exit code, or fail if it hangs."""
    r, w = os.pipe()
    try:
        p = subprocess.Popen([sys.executable, "-c", code], stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=w, env=_env())
    finally:
        os.close(w)
    try:
        return p.wait(within_s)
    except subprocess.TimeoutExpired:
        p.kill()
        p.wait()
        pytest.fail(f"the child didn't exit within {within_s} s with its stderr stuck")
    finally:
        os.close(r)


_EXIT_CHILD = """
import logging, time
from vestibule import stderr_log
stderr_log.install("%(message)s")
for i in range(2000):
    logging.getLogger().warning("x" * 100 + " %d", i)
time.sleep(0.5)  # the writer reaches its blocked write, as it has long before a server exits
"""


def test_a_normal_exit_with_a_stuck_stderr():
    """Q1: logging.shutdown takes every handler's lock at exit, so a writer that held one
    while blocked on the pipe would hang the exit."""
    assert _exits_with_stuck_stderr(_EXIT_CHILD, 5) == 0


_FINALIZER_CHILD = """
import gc, logging, time
from vestibule import stderr_log
stderr_log.install("%(message)s")
log = logging.getLogger("t")

class Logs:
    def __del__(self):
        log.warning("from a finalizer")

gc.set_threshold(1, 1, 1)  # collections often enough that one lands inside a log call
end, i = time.monotonic() + 1, 0
while time.monotonic() < end:
    a = Logs()
    a.self = a  # a cycle: only the collector frees it
    del a
    log.warning("line %d", i)
    i += 1
"""


def test_a_finalizer_that_logs_inside_a_log_call():
    """Q1: queue.Queue holds a plain lock while it allocates; a finalizer that logs from a
    collection inside it deadlocked its own thread (3 of 3 on Python 3.14)."""
    assert _exits_with_stuck_stderr(_FINALIZER_CHILD, 10) == 0


_CALL_S = 30  # each call's bound; a server blocked on stderr never answers


def _waiting(fd: int) -> int:
    """Bytes in the pipe that nobody has read."""
    return struct.unpack("i", fcntl.ioctl(fd, termios.FIONREAD, b"\0\0\0\0"))[0]


def _fill_to_the_last_byte(r: int) -> None:
    """The server's writer blocks on a line that doesn't fit, which can leave room for a
    shorter one. Fill that room, so that a hand-off writing its line unguarded (c5fe2a2's)
    would block here. Through a write end of our own: opened anew, so its O_NONBLOCK isn't
    shared with the server's fd 2."""
    fd = os.open(f"/proc/self/fd/{r}", os.O_WRONLY | os.O_NONBLOCK)
    try:
        while True:
            os.write(fd, b"z")
    except BlockingIOError:
        pass
    finally:
        os.close(fd)


async def _run_print(srv: StdioServer, i: int, r: int) -> None:
    try:
        text = await srv.call("run_code", {"language": "python", "code": "print(1)"}, _CALL_S)
    except TimeoutError:
        pytest.fail(f"call {i} didn't answer within {_CALL_S} s; {_waiting(r)} bytes sat "
                    "unread in the server's stderr")
    assert "exit_code: 0" in text, text


@native_required
async def test_a_client_that_never_reads_stderr(tmp_path):
    """Criterion 9: every call answers though the client never reads stderr, and the server
    still exits in Claude Code's first phase. Red on c5fe2a2's logging (plan §5.1)."""
    r, w = os.pipe()
    try:
        fcntl.fcntl(w, fcntl.F_SETPIPE_SZ, 4096)  # one page: few calls fill it (plan §3.2)
        cap = fcntl.fcntl(w, fcntl.F_GETPIPE_SZ)
        srv = await StdioServer.start(tmp_path / "ws", {"VESTIBULE_BACKEND": "native"},
                                      stderr=w)
    finally:
        os.close(w)  # the server holds its own copy
    try:
        await srv.initialize()
        n = 0
        while _waiting(r) < cap - 512:  # phase 1: until stderr is full
            assert n < 200, f"200 calls left only {_waiting(r)} of {cap} bytes in stderr"
            await _run_print(srv, n, r)
            n += 1
        for i in range(max(n, 10)):  # phase 2: at least the pipe's worth again
            await _run_print(srv, n + i, r)
        _fill_to_the_last_byte(r)  # the hand-off's own line can't fit either
        code, elapsed, phase = await srv.close_like_claude_code()
        await srv.finish_reading()
        assert (code, phase) == (0, SIGINT), f"exit={code} phase={phase} after {elapsed:.3f}s"
        assert srv.bad_stdout == []  # golden rule 1
    finally:
        await srv.kill()
        os.close(r)
