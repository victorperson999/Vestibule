"""Detached post-shutdown cleaner (M2 step 1b, S1-D5). POSIX only.

Real MCP clients give a stdio server almost no time to exit: Claude Code on Linux sends
SIGINT, then SIGTERM 100 ms later, then SIGKILL. So on POSIX the server snapshots what it
owns, starts this process, and exits at once (server.py `_hand_off_and_exit`). This process
runs in its own session, so neither a kill of the server's PID nor a kill of its process
group reaches it. (Windows keeps M1's in-process cleanup: there the server sits in
kill-on-close Job Objects, and a child of it would die with the session.)

Invoked by file path, `python -I -B <path to this file> '<job JSON>'`: stdlib only, so it
needs no import path (isolated mode drops PYTHONPATH and the user site). The job:

    {"runtimes": [{"argv": ["docker"], "owners": ["<token>", ...], "names": ["vestibule-<id>", ...]}],
     "tmpdirs": ["/tmp/vestibule-run-...", ...]}

with optional "sweeps_s" and "call_timeout_s" (the tests shorten them).

What it does, in order:
1. `rm -f` the named containers, then an owner-label sweep per runtime: `ps -aq --filter
   label=vestibule.owner=<token>` → `rm -f`. The server killed its `docker run` CLIs before
   exiting, but a create request already at the daemon can still produce a container.
2. Remove the per-run temp dirs at once: a container created late then finds no script at
   /sandbox and exits (the M1 7a ordering rule, `ContainerBackend._cleanup`).
3. Sweep again at +5 s and +20 s, then remove the temp dirs once more (Docker Desktop can
   re-create a missing bind source when a late container starts; M1 7b finding).

Stdlib only. It never raises and never writes to stdio (the server started it with all
three on /dev/null). Nothing is written on success; on any failure it leaves
`vestibule-cleanup-<pid>.log` in the system temp dir. `VESTIBULE_CLEANUP_TRACE=<file>`
appends one JSON line per action: diagnostics, and the tests' proof that THIS process
removed a container (not `--rm`, not a finisher).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any

SWEEPS_S: tuple[float, ...] = (5.0, 20.0)  # after start; a late create lands within seconds
CALL_TIMEOUT_S = 5.0  # per runtime-CLI call (M1's _CLEANUP_STEP_S)
TRACE_ENV = "VESTIBULE_CLEANUP_TRACE"


class _Cleaner:
    def __init__(self, job: dict[str, Any], trace_path: str | None) -> None:
        self.runtimes: list[dict[str, Any]] = list(job.get("runtimes", []))
        self.tmpdirs: list[str] = list(job.get("tmpdirs", []))
        self.sweeps = [float(s) for s in job.get("sweeps_s", SWEEPS_S)]
        self.call_timeout = float(job.get("call_timeout_s", CALL_TIMEOUT_S))
        self.trace_path = trace_path
        self.failures: list[str] = []
        self.t0 = time.monotonic()

    def run(self) -> None:
        for rt in self.runtimes:
            names = list(rt.get("names", []))
            if names:
                self.call([*rt["argv"], "rm", "-f", *names])
            self.sweep(rt)
        self.remove_tmpdirs()
        for at in self.sweeps:
            delay = self.t0 + at - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            for rt in self.runtimes:
                self.sweep(rt)
        self.remove_tmpdirs()

    def sweep(self, rt: dict[str, Any]) -> None:
        for owner in rt.get("owners", []):
            out = self.call([*rt["argv"], "ps", "-aq", "--filter", f"label=vestibule.owner={owner}"])
            ids = out.split() if out else []
            if ids:
                self.call([*rt["argv"], "rm", "-f", *ids])

    def call(self, argv: list[str]) -> str | None:
        """One bounded CLI call; its stdout, or None if it could not run or answer."""
        try:
            proc = subprocess.Popen(
                argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE
            )
        except OSError as e:
            self.fail(f"{_show(argv)}: could not start: {e}")
            self.trace({"call": argv, "error": str(e)})
            return None
        try:
            out, err = proc.communicate(timeout=self.call_timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.communicate(timeout=1)  # reap; never wait unbounded
            except subprocess.TimeoutExpired:
                pass
            self.fail(f"{_show(argv)}: no answer within {self.call_timeout:g} s")
            self.trace({"call": argv, "timed_out": True})
            return None
        # stdout too: `rm -f` prints the names it removed but exits 0 for a missing one,
        # so only the output proves THIS call removed a container.
        self.trace({"call": argv, "rc": proc.returncode,
                    "out": out.decode(errors="replace")[:2000]})
        if proc.returncode != 0:
            detail = err.decode(errors="replace").strip()[:300]
            self.fail(f"{_show(argv)}: exit {proc.returncode}: {detail}")
        return out.decode(errors="replace")

    def remove_tmpdirs(self) -> None:
        for d in self.tmpdirs:
            shutil.rmtree(d, ignore_errors=True)
            gone = not os.path.exists(d)
            self.trace({"rmtree": d, "gone": gone})
            if not gone:
                self.fail(f"could not remove {d}")

    def fail(self, msg: str) -> None:
        self.failures.append(msg)

    def trace(self, record: dict[str, Any]) -> None:
        if not self.trace_path:
            return
        record = {"t": round(time.monotonic() - self.t0, 3), **record}
        try:
            with open(self.trace_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record) + "\n")
        except OSError:
            pass  # diagnostics only


def _show(argv: list[str]) -> str:
    return " ".join(argv)[:200]


def main(argv: list[str]) -> int:
    failures: list[str] = []
    try:
        job = json.loads(argv[1])
        cleaner = _Cleaner(job, os.environ.get(TRACE_ENV))
        cleaner.run()
        failures = cleaner.failures
    except Exception as e:  # never raise: nobody is left to see it
        failures.append(f"cleaner crashed: {e!r}")
    if failures:
        log_path = os.path.join(tempfile.gettempdir(), f"vestibule-cleanup-{os.getpid()}.log")
        try:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(f"vestibule cleanup, {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
                f.writelines(f"  - {m}\n" for m in failures)
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
