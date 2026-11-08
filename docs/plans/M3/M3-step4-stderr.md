# M3 step 4 — Non-blocking stderr

Status: **v2.1, signed off 2026-11-04; built 2026-11-05 (§9).** Victor approved the two
refinements made while building on 2026-11-05 (§9.2). v2.1 is a second pass over Q2–Q4 on
Victor's request:
- Q2's drain no longer checks for room first;
- Q3's memory figure is corrected to characters, not bytes;
- Q4's note is guarded.

v2 was revised after the `skeptic` review, which
found 14 issues (2 medium, 12 low) and gave the verdict "sound with amendments". `verifier`
checked the claims:
- 9 confirmed, 1 plausible and 1 confirmed only in one form;
- 2 refuted as stated, and 1 refuted in substance.

It also found three issues of its own (§8). §3.1 lists the four places where the plan departs
from the contract's wording, or from S1-D5; each needs Victor's OK.

Inputs:
- the M3 contract, [`M3-audit-usage.md`](M3-audit-usage.md) (signed off 2026-11-01): §1's
  "Non-blocking stderr logging" bullet, A11, criterion 9, and §6's step 4;
- S1-D5, the POSIX shutdown hand-off (`docs/HISTORY.md:506–516`): it "exits within ~50 ms";
- the known issue it closes: `docs/BUILD_LOG.md`, "2026-10-25 (M2 step 3 review) | server
  logging";
- the code at `fb26ba1`, plus step 3's uncommitted tree (step 3 doesn't touch logging);
- facts checked on 2026-11-04 (Windows 3.11.9; WSL 3.14.4 and 3.11.17; MCP SDK 1.28.1):
  1. **`Handler.handle` holds the handler's lock (an `RLock`) around `emit`.** Python 3.11
     takes it with `acquire()`, 3.14 with `with self.lock:`. In `QueueHandler.emit`, a record
     that fails to format goes to `handleError`, which writes a traceback to `sys.stderr`.
     `verifier` checked this on a stuck `sys.stderr`: that call blocked, and another thread's
     log call then didn't return within 3 s. Same on all three interpreters.
  2. **A logging handler blocked in a write hangs the process at exit.** `logging.shutdown`
     (run by `atexit`) takes every handler's lock. Measured on all three:
     - a daemon thread logged through a `StreamHandler` into a pipe nobody reads, and the main
       thread returned: the process was still alive 6 s later;
     - the same thread writing with `os.write` on an `os.dup(2)`: it exited in 0.6–1.0 s.
  3. **`queue.Queue.put_nowait` isn't reentrant.** It holds a plain `Lock` while raising
     `queue.Full`, and that raise allocates, so GC can run a finalizer inside the lock. If the
     finalizer logs, the thread deadlocks against itself.
     - **Repro (`verifier`):** a full `Queue(1)`, a finalizer that logs, and
       `gc.set_threshold(1, 1, 1)`. It deadlocked 3 times out of 3 on 3.14, and not on 3.11.
     - **`queue.SimpleQueue`** (implemented in C, reentrant) never deadlocked.
     - **Why it matters:** asyncio's "Task was destroyed" and "exception was never retrieved"
       messages are logged from finalizers.
  4. **Linux pipes:**
     - the default size is 65536;
     - `F_SETPIPE_SZ` 4096 gives 4096, which holds 4000 bytes of 100-byte writes;
     - `FIONREAD` on the read end gives the bytes waiting;
     - a zero-timeout `select` says writable only when a whole page is free (the one-page pipe:
       only when empty). So after a "writable", a line under 4096 bytes doesn't block.
  5. **macOS and FreeBSD** report a pipe writable once PIPE_BUF (512 bytes) is free. This is
     plausible from kernel-source knowledge but couldn't be run here. A longer line can still
     block after a "writable".
  6. **Windows:**
     - `os.write` on a dup of fd 2 writes `\n` as is, while `sys.stderr` writes `\r\n`.
     - On a console (not a pipe) `os.write` bypasses Python's console writer, so non-ASCII text
       is garbled: `café` came out as `caf├⌐` under code page 437.
     - Synchronous I/O is serialized per file object, which a dup shares. A second call on the
       same pipe waits while another thread's write is blocked.
     - `select` takes sockets only. The hand-off is POSIX-only, but `tests/test_shutdown.py`
       runs it on Windows with stubs.
  7. **fd 2 closed at start:** `os.dup(2)` raises `OSError(EBADF)`, and `sys.stderr` is `None`.
  8. **The logging setup is the same as at `c5fe2a2`** (`git show c5fe2a2:src/vestibule/server.py`),
     so a red run on today's tree is the red run the contract asks for "on `c5fe2a2`".
  9. **What gets logged:**
     - **Per `run_code` call:** the server logs 2 lines. Native adds 3, and the SDK adds
       `Processing request of type …` (INFO) for every request
       (`mcp/server/lowlevel/server.py:733`). Each line is about 80–110 bytes.
     - **Values that come from outside, uncapped:**
       - `read_workspace`'s `path` (`server.py:318`, `:321`);
       - the SDK's `Failed to validate notification: … Message was: …`
         (`mcp/shared/session.py:430`). A 200 KB `notifications/progress` parameter produced a
         200,153-byte WARNING line;
       - the SDK's `Received exception from stream: …` (`:713`) is shorter, because pydantic
         shortens `input_value`; the longest of 10 hostile inputs was 2.4 KB.
  10. **No forks in the server.** It never calls `os.fork` (only `native_helper.py`, a separate
      process, does). Children start through `asyncio.create_subprocess_exec`, which doesn't
      run Python's at-fork hooks.
  11. **pytest's warnings plugin resets `warnings.showwarning` for every test,** and its
      `caplog` handlers sit on the root logger. An in-process test of the switch would fight
      both, so §5's switch tests run in a subprocess.

---

## 1. Scope

**In:**
- A11, with §3.1's amendments.
- Criterion 9: the unit test and the stdio e2e test.
- The POSIX hand-off under a full pipe: the server must exit in the client's first phase, not
  wait for its SIGKILL.

**Out, with reasons:**
- **A client that stops reading stdout.** Stdout is the protocol channel, and the SDK owns that
  writer.
- **The residuals, kept as A11 names them:**
  - the thread and unraisable-exception hooks still write `sys.stderr` (crash paths, both rare);
  - the native helper keeps the server's stderr and writes to it only on a crash
    (`native.py:183`).

  `skeptic` suggested routing both hooks through the queue. That isn't taken: the contract
  accepted them, and this step stays at its size. Victor can ask for it.
- **The unexpected-arguments warning in `run()`.** It's written before the switch, by the
  import-time handler: one line at start-up, into an empty pipe.

---

## 2. What changes

### 2.1 The handler (`src/vestibule/stderr_log.py`, new)

```python
_QUEUE_MAX = 1000     # A11
_DRAIN_S = 0.02       # Q2: the most an exit path waits for queued lines
_LINE_MAX = 16384     # Q3, in characters: a longer line keeps its first and last 8192

_installed: NonBlockingHandler | None = None

class NonBlockingHandler(logging.Handler):
    """Log lines go into a bounded queue; one daemon thread writes them to its own dup of fd 2.
    A log call never waits for stderr: on a full queue the line is dropped and counted."""

    def __init__(self, fd: int = 2) -> None:
        super().__init__()
        self._lines: queue.SimpleQueue[str | threading.Event] = queue.SimpleQueue()
        self.dropped = 0  # touched only in emit, which Handler.handle serializes (fact 1)
        self._encoding = getattr(sys.stderr, "encoding", None) or "utf-8"
        try:
            out = os.dup(fd)
        except OSError:  # no stderr at all (fact 7): lines queue, then drop
            self._writer = None
            return
        self._writer = threading.Thread(target=self._write_lines, args=(out,),
                                        name="vestibule-stderr", daemon=True)
        self._writer.start()

    def emit(self, record) -> None:
        # Format and cut to _LINE_MAX; a failure goes to handleError.
        # If qsize() >= _QUEUE_MAX: dropped += 1.
        # Else: put the line, preceded by "stderr was not being read: dropped N log line(s)"
        # if lines were dropped since the last one went in (the notice sits where the gap
        # is), then reset the count.
    def handleError(self, record) -> None:
        # Q4: when logging.raiseExceptions is set (its default, A11), queue one line,
        # "a log call failed to format (<exc>) at <file>:<line>", in place of the stock
        # traceback to sys.stderr.
    def drain(self) -> bool:
        # Put a threading.Event and wait up to _DRAIN_S for the writer to reach it.
        # True: everything queued before it was written. No writer: False at once.
    def flush(self) -> None:  # logging.shutdown calls this at a normal exit
        self.drain()
    def _write_lines(self, fd) -> None:
        # Forever: take an item. An Event gets set; a line is written in full (a loop over
        # os.write). Any exception drops that line and the loop goes on.

def install(fmt: str) -> None:
    """The server's switch: root logs through a NonBlockingHandler, warnings included."""
    # basicConfig(handlers=[h], force=True, format=fmt); captureWarnings(True); _installed = h

def drain() -> bool:
    """The exit paths' wait: the installed handler's drain, or False if none is installed."""
```

- **Bytes on the wire match today's on pipes and files:**
  - `sys.stderr`'s encoding with `errors="backslashreplace"`;
  - every `\n` becomes `os.linesep` (fact 6).

### 2.2 The server (`src/vestibule/server.py`)

- **`_LOG_FORMAT`:** the format string, now a constant shared by the import-time
  `basicConfig` and the switch.
- **The switch: `stderr_log.install(_LOG_FORMAT)`, the first line of `_main()`.**
  - `vestibule-mcp setup`, `vestibule-mcp audit` and every test that imports `server` keep the
    import-time handler.
  - So do the `run()` tests, which stub `_main` (`tests/test_rootfs.py:66`, `:96`).
- **The hand-off's `finally`:**
  ```python
  with contextlib.suppress(Exception):
      stderr_log.drain()          # the lines logged before this one (Q2), at most 20 ms
      if _stderr_has_room():      # A11: skipped on a full pipe
          data = f"vestibule shutdown ({trigger}): {note}".encode(errors="replace")
          os.write(2, data[:511] + b"\n")   # under PIPE_BUF everywhere (fact 5)
  os._exit(0)
  ```
  - `_stderr_has_room()` is `bool(select.select([], [2], [], 0)[1])`, A11's check.
  - If fd 2 is closed, `select` raises, the `suppress` catches it, and the server exits.
- **Windows `_shutdown`:**
  - The `os._exit` branch calls `stderr_log.drain()` inside its own `suppress`, before the
    stream flushes. A raise here would skip `os._exit` and send the server into the cancel-all
    that this branch exists to avoid.
  - The return branch needs nothing, because `logging.shutdown` calls `flush`.

### 2.3 The test helper (`tests/stdioutil.py`)

`StdioServer.start(…, stderr=fd)` passes that fd as the server's stderr and skips the stderr
reader.

---

## 3. Choices this plan makes

### 3.1 Departures — for Victor's sign-off

- **Q1 — A plain `logging.Handler` with our own writer thread, a `SimpleQueue` and `os.write`.**
  - A11 says "a `QueueHandler`" and "a `QueueListener` thread". Three facts rule those out:
    - **`QueueListener` writes through handlers.** A handler blocked on a full pipe holds its
      lock, and `logging.shutdown` waits for that lock at exit (fact 2). The server then
      doesn't exit until the client kills it (300 ms for Claude Code on Windows, 2 s for the
      SDK), and its exit code and last lines are lost.
    - **`QueueHandler` sits on `queue.Queue`,** which can deadlock a thread against itself on
      a full queue (fact 3). A `SimpleQueue` has no size bound, so `emit` checks `qsize()`.
    - **Typing:** a `QueueHandler` whose items are strings fights typeshed's `LogRecord`
      signatures.
  - The thread does what A11 asks: it writes to its own `os.dup(2)`, and it holds no lock that
    other code takes.
- **Q2 — The exit paths wait up to 20 ms for queued lines. This amends S1-D5's "~50 ms".**
  - Without it, a line logged just before exit dies in the queue. Two such lines:
    - `server stopped with an error`, logged right before the POSIX hand-off;
    - Windows `shutdown: removing 1 container(s)`, which `tests/test_stdio_e2e.py:165` asserts.
  - **Both platforms always drain, bounded at 20 ms.** A pipe with room drains in microseconds.
    The worst case is a stuck stderr or a slow reader: S1-D5's ~50 ms plus 20 ms.
  - **Why the drain doesn't check for room first (v2.1):** a check would skip the drain whenever
    the pipe is full at that moment, including when the client is reading slowly and would
    still have taken the lines. It would save at most 20 ms. Windows has no such check anyway
    (fact 6). A11's check stays where it's needed, before the hand-off's own `os.write`, which
    has no bound.
  - §5.4's e2e test runs the hand-off in Claude Code's 100 ms SIGINT phase. `tests/test_signals.py`
    still runs it on an idle server.
- **Q3 — A queued line is capped at 16 KiB.**
  - **Why:** A11 bounds the queue by count, not by size, and lines that come from outside can
    be any length: the SDK's 200 KB WARNING line, and `read_workspace`'s `path` (fact 9).
  - **The limit is in characters (16,384), not bytes.** Python stores up to 4 bytes per
    character, so the queue holds at most about 64 MB in the worst case. That case needs 1000
    hostile, non-Latin lines waiting behind a stuck stderr. A line is at most 64 KiB in UTF-8.
  - **How:** a longer line keeps its first and last 8,192 characters around a
    `…[cut N chars]…` marker, so a long traceback keeps both its message and its innermost
    frame.
- **Q4 — A log call that fails to format queues a one-line note,** in place of the stock
  traceback to `sys.stderr`. The stock path writes `sys.stderr` while holding the handler's
  lock, so on a stuck pipe it froze every thread's logging (fact 1). Triggers include library
  code: mismatched `%` arguments, or a `repr` that raises. `logging.raiseExceptions` keeps its
  default and still decides whether anything is reported (A11).
  - **The note** holds the logger's name, the exception's type and message, and the call's
    `file:line`. It goes through the same cap and bound as any other line.
  - **The note's own argument isn't logged,** because that argument may be what raised.
  - **Building the note is guarded (v2.1).** An exception whose `repr` itself raises counts as
    a dropped line instead of escaping from `emit`.

### 3.2 Fixed choices

- **The drop count is touched only in `emit`.** `Handler.handle` serializes `emit` under the
  handler's lock, and the writer never takes that lock.
- **No stderr (fd 2 closed):** no writer, lines queue up to the bound and then drop, and
  `drain` returns False at once.
- **A failed write** (EPIPE once the client has closed its end, since Python ignores SIGPIPE;
  EBADF; Windows EINVAL) drops that line, and the writer goes on.
- **A short write is finished** by looping over `os.write`. A signal can cut a blocking pipe
  write short. A test can't force that reliably, so no mutant covers it.
- **The hand-off line is capped at 512 bytes** (fact 5). Its `note` can hold an exception's
  `repr`.
- **The e2e test makes its pipe one page** with `F_SETPIPE_SZ`. It then reads the capacity
  with `F_GETPIPE_SZ`, as criterion 9 says. One page needs about 20 native calls; the 64 KiB
  default would need about 150.

---

## 4. Edge cases

| Case | What happens |
|---|---|
| The client stops reading stderr, then starts again | The queued lines arrive late, in order. If lines were dropped, the `dropped N` notice comes just before the first line accepted after the gap. |
| The client closes stderr's read end | Each write fails at once, and lines are dropped silently: there's nowhere to report them. |
| Stderr is stuck when the server exits | The drain gives up after 20 ms, and the queued lines are lost; nobody was reading them. The POSIX hand-off skips its own line, then exits. |
| What kind of fd a real client hands the server as stderr (a pipe, or a socket) | The design doesn't depend on it: `os.write` and `select` work on both. The e2e test uses a pipe, as the Python SDK does. Step 5's live check records what Claude Code hands over, on Linux and on Windows. |
| Many threads log at once (the loop, the audit writer, `to_thread` workers) | `emit` runs under the handler's lock for a few microseconds, and `SimpleQueue.put` never blocks. |
| A finalizer logs inside `emit` on the same thread | The lock is an `RLock`, and `SimpleQueue` is reentrant (fact 3). |
| Another thread's line takes the last of the pipe between the hand-off's second check and its write | That write waits until the client reads, or kills the server. The window is a few instructions, and the client must stop reading inside it. This is a stated residual. |
| Windows exit with a stuck stderr | `drain` waits 20 ms, then the server exits. `sys.stderr.flush()` in the `os._exit` branch has nothing buffered, since nothing on the server path writes `sys.stderr` after the switch except the residuals. So it doesn't touch the pipe that the writer is blocked on (fact 6). |
| A developer runs `vestibule-mcp` in a Windows console | Non-ASCII in log lines is garbled (fact 6). This is cosmetic: clients use pipes, which keep today's bytes. |
| A character stderr's encoding can't hold | `backslashreplace`, as today. |

---

## 5. Tests

### 5.1 Red first

Before any `src/` change, §5.4's e2e test runs on today's tree, which has `c5fe2a2`'s logging
(fact 8). It must fail: a call doesn't answer within its 30 s. The result goes in §9. The other
tests need the module, so their red comes from the mutants (§5.5).

### 5.2 The handler (`tests/test_stderr_log.py`, every platform)

The rules for every test here:
- a call that could block runs in a daemon thread with a join timeout;
- no test touches a pipe from its own thread while a reader thread is blocked on that pipe
  (fact 6).

1. **A stuck stderr never blocks a log call** (criterion 9's unit test):
   - **Setup:** a handler on a pipe nobody reads, attached to a private logger with
     `propagate=False`.
   - **Logging:** `line 0`, `line 1`, … go out until `dropped > 0`, capped at 100,000 lines and
     a 10 s join.
   - **Expect:** nothing reaches `sys.stderr` or fd 2 (`capfd`).
2. **The gap is marked where it is.**
   - **Setup:** test 1, then a reader thread drains the pipe.
   - **Steps:** wait until the last accepted line arrives, then log `after` and `after2`.
   - **Expect:**
     - the line before `after` is `stderr was not being read: dropped N log line(s)`, with N
       the drop count;
     - the first dropped line never arrives;
     - there's no second notice before `after2`.
3. **`drain`:**
   - on a stuck pipe it returns False, after at least 15 ms and under 0.5 s;
   - on a pipe being read it returns True;
   - with no writer it returns False in under 10 ms.
4. **No stderr:** a handler on a closed fd doesn't raise, and a log call returns.
5. **A long line is cut:** a 40,000-character message arrives as its first 8,192 characters,
   the marker, and its last 8,192.
6. **A write error doesn't stop the writer.** `stderr_log`'s own `os` name is swapped for a
   stand-in whose `write` raises once. Only that module sees it, so pytest's capture is
   untouched. The next line still arrives.
7. **A log call that fails to format** (`log.info("%d", "x")`):
   - nothing reaches `capfd`;
   - one `a log call failed to format` line arrives in the pipe;
   - with `logging.raiseExceptions` False, nothing arrives;
   - an argument whose `repr` raises, inside an exception whose `repr` also raises, is counted
     as dropped, and `emit` returns.
8. **The switch, in a subprocess** (fact 11):
   - after `import vestibule.server`, root holds a plain `StreamHandler`;
   - after `stderr_log.install(server._LOG_FORMAT)`, root holds a `NonBlockingHandler`, and a
     `warnings.warn` shows up in the child's stderr, which the parent reads.
9. **A normal exit with a stuck stderr** (Q1, in a subprocess). The child installs the handler,
   logs 2,000 lines into a pipe nobody reads, and returns. Expect exit 0 within 5 s. A build
   whose writer goes through a `StreamHandler` hangs here (fact 2).
10. **A finalizer that logs on a full queue** (fact 3, in a subprocess). The setup is:
    - `gc.set_threshold(1, 1, 1)`;
    - a stuck pipe, so the queue is full;
    - objects whose `__del__` logs.

    Expect exit within 5 s. A `queue.Queue` build deadlocks here on 3.14, which the Linux gate
    runs.

### 5.3 The exits (`tests/test_shutdown.py`)

These are wiring tests with stubs. They're deterministic, because a real writer could deliver
a line during any syscall (§8, finding 1). Test 3 checks what `drain` itself does.

11. **Hand-off with no room:** `_stderr_has_room` stubbed False. Expect a `drain`, no line,
    and `os._exit(0)`.
12. **Hand-off with room:** the `drain` stub records what `capfd` has on fd 2 when it's called.
    Expect nothing there yet. Afterwards the shutdown line is there, and `os._exit(0)`.
13. **A long note:** a snapshot raising an exception with a 2,000-character message. Expect the
    line on fd 2 to be at most 512 bytes, ending in `\n`.
14. **Windows `_shutdown`, the `os._exit` branch** (plain async code, so it runs on every
    platform):
    - `drain` is called before `os._exit`;
    - a `drain` that raises still reaches `os._exit(0)`.

The `handoff` fixture stubs `_stderr_has_room` to True on Windows only (fact 6). On POSIX,
`capfd`'s fd 2 is a file, so the real `select` says writable.

### 5.4 End to end (`tests/test_stderr_log.py`, Linux; not Docker-marked)

15. **A client that never reads stderr:**
    - **Setup:**
      - the server gets a pipe made one page with `F_SETPIPE_SZ`;
      - the capacity comes from `F_GETPIPE_SZ`;
      - the read end stays open and unread;
      - the backend is the default (native on the gate).
    - **Phase 1:** `run_code` calls (`print(1)`) until `FIONREAD` is within 512 bytes of the
      capacity (n calls; at most 200).
    - **Phase 2:** `max(n, 10)` more calls, which is at least the pipe's worth again. Every call
      must answer within 30 s.
    - **Then:** close like Claude Code. Expect exit 0 in the SIGINT phase, and nothing but
      JSON-RPC on stdout.

    This test is also the wiring check for the switch in `_main()`.

### 5.5 Mutants

Each must turn a test red, after a green control. The harness backs up each file and checks
hashes on restore. Each run has its own timeout, which counts as red: a blocking mutant hangs
pytest's exit, because `logging.shutdown` takes every handler's lock (fact 2).

| # | Mutant | Caught by |
|---|---|---|
| 1 | no `qsize` bound | 1 |
| 2 | the writer writes `sys.stderr` instead of its dup | 1 |
| 3 | a drop isn't counted | 2 |
| 4 | the count isn't reset after its notice | 2 |
| 5 | `drain` waits without a bound | 3 |
| 6 | `drain` returns True at once | 3 |
| 7 | no `captureWarnings` | 8 |
| 8 | `install` at import time | 8 |
| 9 | no `install` in `_main()` | 15 |
| 10 | a note whose `repr` raises escapes `emit` (the guard removed) | 7 |
| 11 | the room check removed | 11 (§9.3: 15 before the drain gate) |
| 12 | the hand-off never drains | 12 |
| 13 | no cap on the hand-off line | 13 |
| 14 | no `drain` in Windows `_shutdown` | 14 |
| 15 | the Windows `drain` outside a `suppress` | 14 |
| 16 | no line cap in `emit` | 5 |
| 17 | the writer's `except` removed | 6 |
| 18 | the stock `handleError` | 7 |
| 19 | the writer writes through a `StreamHandler` | 9 |
| 20 | `queue.Queue(1000)` with `put_nowait` | 10 (on 3.14) |

### 5.6 The gate

Windows, then Linux (3.14 full, 3.11 Docker-free, stdio), with no new skips on Linux.

---

## 6. Docs (this step)

- **The contract:** an A11 amendment blockquote for Q1–Q4.
- **HISTORY:**
  - the sign-off bullet, which records Q2 as an amendment to S1-D5;
  - the done bullet.
- **ARCHITECTURE:** a short paragraph under the stdio rule. It says log calls never wait for
  stderr: lines go through a bounded queue to a writer thread. It lists the residuals.
- **GETTING_STARTED troubleshooting:**
  - what `stderr was not being read: dropped N log line(s)` means: the client stopped reading
    the server's log for a while, and nothing else was affected;
  - what `a log call failed to format` means.
- **BUILD_LOG:**
  - the 2026-10-25 known issue is marked closed by step 4, with the residuals;
  - the status row and the entry.

---

## 7. Files and size

| File | Change | Lines |
|---|---|---|
| `src/vestibule/stderr_log.py` | new | ~85 |
| `src/vestibule/server.py` | `_LOG_FORMAT`, the switch, the hand-off's checks and cap, the Windows drain | ~15 |
| `tests/test_stderr_log.py` | new: 1–10, 15 | ~230 |
| `tests/test_shutdown.py` | 11–14, the fixture's Windows stub | ~55 |
| `tests/stdioutil.py` | the `stderr` argument | ~6 |

The contract estimated src ~60 and tests ~100. The extra src comes from Q2–Q4, and the extra
tests are the subprocess and wiring tests that the review asked for.

---

## 8. Review (`skeptic`, then `verifier`, 2026-11-04)

`skeptic`'s verdict was "sound with amendments", with 14 findings. `verifier` checked the
claims and added three findings of its own (V1–V3). Every confirmed finding is taken.

| # | Sev. (verifier) | Finding | In v2 |
|---|---|---|---|
| 1 | med | The drain tests race the writer. Any syscall before the stubbed `os._exit` can deliver the line without a drain (Windows: 295/300). | Wiring tests with stubs (11, 12, 14). `drain`'s own behaviour is checked through its return value and timing (3). |
| 2 | med | Nothing tests a real exit with a stuck stderr, so a `QueueListener` build would pass. | Test 9 (subprocess); mutant 19. Q1 states the stakes. |
| 3 | low | Mutant 14: refuted as stated (pytest resets `showwarning` per test, so the switch test catches it); plausible for a move of `basicConfig` alone. | Test 8 checks root in a fresh subprocess. |
| 4 | low | `handleError` writes `sys.stderr` under the handler's lock: one bad log call freezes all logging on a stuck pipe. | Q4. The hooks stay residuals (§1). |
| 5 | low (plausible) | macOS and FreeBSD say "writable" at 512 bytes free; the hand-off line is uncapped. | The line is capped at 512 bytes. |
| 6 | low | Q2 changes S1-D5's "~50 ms". | Q2 is an amendment to S1-D5; the bound is lowered to 20 ms. |
| 7 | low | A raise from the Windows drain would skip `os._exit`; "each root handler" pulls in pytest's handlers. | A `suppress`; `stderr_log.drain()` drains only the installed handler. |
| 8 | low | The drop notice lands next to the oldest queued line, 1000 lines before the gap. | The notice goes in with the first line accepted after the gap. |
| 9 | low | The switch test's fixture: mechanism confirmed, the "stale handlers" consequence refuted (pytest re-adds its handlers each phase). | Moot: the switch is tested in a subprocess (8). |
| 10 | low | Mutants that block hang pytest at exit. | A per-run timeout counts as red; blocking calls run in daemon threads. |
| 11 | low | An `enqueue(line: str)` override fails mypy (only once annotated). | A plain `logging.Handler` (Q1). |
| 12 | low | A Windows console garbles non-ASCII from `os.write`. | Stated as cosmetic (§4). |
| 13 | low → med | `queue.Queue` isn't reentrant: deadlocked 3/3 on 3.14 under an aggressive GC threshold. | `SimpleQueue` with a `qsize` bound (Q1); test 10; mutant 20. |
| 14 | low | ARCHITECTURE would claim more than holds. | "Log calls never wait for stderr", with the residuals listed. |
| V1 | — | `tests/test_rootfs.py:66` and `:96` run `server.run()` on the server path, so a switch in `run()` would run inside them (and `:103`'s `caplog` check would fail). | The switch is the first line of `_main()`, which those tests stub. |
| V2 | — | The SDK logs a client-controlled line uncapped: 200,153 bytes from one notification. | Fact 9; Q3. |
| V3 | — | Windows serializes I/O on a file object shared by a dup. | §4: `sys.stderr` holds nothing on that path. Test rule in §5.2. |

Refuted in substance: `server.py:713`'s line is shortened by pydantic (2.4 KB at most in 10
hostile tries).

---

## 9. Build (2026-11-05)

### 9.1 Red first

Before any `src/` change, test 15 ran on the tree at `a9dfc09`, which has `c5fe2a2`'s logging
(fact 8), in WSL on 3.14. It failed after 32 s: `call 6 didn't answer within 30 s; 3997 bytes
sat unread in the server's stderr`, with a 4,096-byte pipe. After the change it passes in about
2.3 s.

### 9.2 Changes from v2.1

**For Victor's OK (approved 2026-11-05):**
1. **The hand-off writes its line only if the drain finished** (review round 1, finding 4). In
   v2.1 it drained, then checked for room on its own.
   - **Why:** the race between the check and the write is wider than §4 said. When the drain
     times out, the log's writer is blocked on the same pipe, and the client read that makes
     `select` say "room" also wakes the writer, which can take that room first. On Linux,
     with a 16 KB line queued, the hand-off's write blocked in 161 of 200 tries.
   - **The cost** (round 2, finding 1): on a busy machine the writer can just be late, and the
     line is then skipped though nothing was stuck. With 9 runnable processes on one CPU, 84
     of 1,500 drains missed the 20 ms; idle, 0 of 1,500. The line is lost, never the exit.
   - **Going back to v2.1** is a one-line change, `stderr_log.drain()` on its own line, then
     `if _stderr_has_room():`, and brings the race back.
2. **Q4: an exception whose `str()` raises is named by its type,** instead of counting as a
   dropped line. Drops are reported as `stderr was not being read: dropped N log line(s)`,
   which would be untrue for this one.

**Smaller, no decision needed:**
- **Marker:** the cut marker is ASCII, `...[cut N chars]...`, not `…`. Stderr's encoding may not
  hold `…` (a Windows console), and the server's other markers are ASCII.
- **Test 15** sets `VESTIBULE_BACKEND=native` rather than relying on the default, so it can't
  fall back to a container.
- **Test 10** loops for 1 s, with a 10 s exit bound instead of 5 s.
- **Tests 9 and 15:** test 9's child waits 0.5 s before it returns, and test 15 fills the pipe
  to its last byte before SIGINT (§9.3).
- **The review's additions:**
  - test 1 times each dropped call (under 50 ms);
  - test 11 has a second case, where the drain didn't finish but there's room;
  - test 14 checks that the drain comes after the warning;
  - test 3's drain on a pipe being read tries up to 50 times;
  - `_bounded` times with `perf_counter`, since Windows' monotonic clock ticks about every
    15.6 ms.

### 9.3 Mutants

23, all red after a green control: WSL on 3.14, each run bounded at 150 s, every file restored
and hash-checked. Two were first caught by the wrong test, so the tests were fixed:
- **Mutant 19** (the writer goes through a `StreamHandler`) passed test 9. The child returned
  before its writer had blocked while holding the inner handler's lock. The child now waits
  0.5 s, and test 9 goes red against the mutant on 3.14 and 3.11.
- **Mutant 11** (the room check removed) passed test 15. The writer blocks on a line of about
  100 bytes, which can leave room for the 50-byte shutdown line. Test 15 now fills the pipe to
  its last byte.
  - With §9.2's drain gate, the drain times out first in test 15, so only test 11 catches
    mutant 11. §5.5's row 11 now reads "11".
  - Test 15 still catches a hand-off with no guard at all, `c5fe2a2`'s. `verifier` found it red
    3 of 3 times with the fill, and green 3 of 3 without it.

New mutants:
- 21, the drain's result ignored: caught by test 11's second case;
- 22, the Windows drain moved above the warning: test 14;
- 23, a 0.2 s block on each drop: test 1.

### 9.4 Review

`critic` ran twice, and `verifier` checked each round. No finding was high or medium; round 2's
finding 2 was raised to low–medium by `verifier`.

| # | Finding | `verifier` | Done |
|---|---|---|---|
| R1-1 | Test 1 never timed a log call: a 1.5 ms block on every call passed | confirmed, low | Each dropped call is timed (< 50 ms); mutant 23 |
| R1-2 | Q4's type-name note departs from v2.1 | confirmed, low | Recorded, §9.2 |
| R1-3 | The Windows drain test checked only drain before exit | confirmed, low | The drain must see the warning logged; mutant 22 |
| R1-4 | The hand-off's race is wider when the drain timed out | plausible, low (161 of 200 on Linux with a 16 KB line) | The drain gate, §9.2 |
| R1-5 | The drain test on a pipe being read depends on 20 ms | confirmed, low (1 in 1,000–3,000 at 4–8× oversubscription) | Up to 50 tries |
| R1-6 | The module docstring repeated the class's | confirmed, nit | Cut to 2 lines |
| R2-1 | The drain gate also skips the line when the writer is only late | confirmed, low (84 of 1,500 with 9 runnable on one CPU; 0 idle) | The comment says so; §9.2, for Victor |
| R2-2 | The contract amendment claimed approval for §9.2's two changes, and there was no §9 | confirmed, low–medium | This section; the amendment's header |
| R2-3 | Test 15 no longer catches mutant 11, and the fill is dead weight | confirmed in part: the fill guards the unguarded hand-off | Row 11 corrected, the fill's purpose stated |
| R2-4 | Sizes are over §7 | confirmed | §9.5 |
| R2-5 | On Windows, an exception escaping `server.run` writes its traceback to `sys.stderr`; it isn't listed as a residual | confirmed, low; older than step 4 | Listed: ARCHITECTURE, the amendment |

### 9.5 Sizes against §7

| File | §7 | Built |
|---|---|---|
| `src/vestibule/stderr_log.py` | ~85 | 111 |
| `src/vestibule/server.py` | ~15 | +26 / −5 |
| `tests/test_stderr_log.py` | ~230 | 407 |
| `tests/test_shutdown.py` | ~55 | +77 |
| `tests/stdioutil.py` | ~6 | +10 / −8 |

The source grew with Q2–Q4's comments and the drain gate. The tests grew with the pipe and
subprocess helpers, and with the review's additions.

### 9.6 Gate

- **After round 1's fixes:**
  - Windows: 637 passed, plus 142 POSIX/Linux-only skips.
  - Linux 3.14, full suite: 799 passed, 0 skipped.
  - Linux 3.11, Docker-free: 756.
  - stdio: 5 of 5.
- **Round 2's fixes** changed only comments, a docstring and docs. Ruff, mypy, the step-4 tests
  (35 on 3.14 and on 3.11; 33 plus 4 skips on Windows) and `test_docs.py` were run again, all
  green.
