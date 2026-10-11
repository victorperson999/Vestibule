"""M1 container backend: one throwaway Docker/Podman container per run.

Contract: docs/plans/M1-container-backend.md (§3 execution profile, §4 lifecycle,
D9 script delivery, D10 stdio rules), amended by docs/plans/M1-step4-lifecycle.md
(S4-D1 bounded-wait busy refusal, S4-D3 deadline-label reaping). The runtime CLI is
driven via asyncio.create_subprocess_exec — async-safe, no blocking of the event loop.

Step-4 lifecycle guarantees:
- Cleanup (container kill/rm + temp dir) runs as an independent task, so it survives
  cancellation of the request that started the run and never delays the result —
  including on guest timeout, where the kill is fully detached (§4.5, amended by the
  Codex budget finding 2026-07-05).
- Every container is stamped by its owner with `vestibule.deadline=<unix epoch>`; the
  reaper removes only containers past their own deadline (+margin) — never judging a
  run by this process's config, never touching this process's live runs (S4-D3).
- At most `max_concurrent` containers in flight; a bounded wait for a slot, then a
  legible RunRefusedError (S4-D1).

Step-5 additions (docs/plans/M1-step5-selection.md):
- Per-run image preflight + `--pull never`: a missing image is a legible refusal
  carrying the exact pull command, never an in-call download (S5-D3, upholding D2).
- `soft_disabled` omits soft limits the step-5 probe proved unenforceable; such runs
  honestly report `container-degraded` with the exact list (D3).
- Exit 125 — the runtime's own "run command failed" code — reports `isolation: none`:
  usually the container never started (golden rule 5). A guest that exits 125 itself
  is underclaimed the same way (the safe direction), and the detail says so.

Step-7a additions (docs/plans/M1/M1-step7-acceptance.md):
- S7-D1: an optional shared concurrency semaphore (one budget per process), an
  in-flight count + idle(), and a drain() that never cancels what it waits on.
- S7-D5: every guest runs under `timeout -s KILL <timeout + slack>`, so a container
  ends itself at its labeled deadline even when the server was killed.
- S7-D3: a soft limit the selection probe could not verify inside the container
  makes runs report container-degraded, never plain container.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import secrets
import shutil
import tempfile
import time
from pathlib import Path

from vestibule.backends.base import RunRefusedError, RunResult, Warden
from vestibule.config import Limits

log = logging.getLogger("vestibule.container")

_EXT = {"python": ".py", "bash": ".sh", "node": ".js"}
_INTERPRETER = {"python": "python", "bash": "bash", "node": "node"}

# Soft-tier resource limits (D3): a verified-unenforceable one degrades the run
# loudly instead of blocking it. Names appear in `isolation_detail` and in the
# step-5 degraded retry (select.py).
SOFT_CONTROLS: tuple[str, ...] = ("cpu", "memory", "pids", "tmpfs-size")

# Grace added on top of the guest timeout: container cold start (image unpack,
# Docker Desktop VM wakeup) happens inside `docker run`, and must not eat the
# guest's own budget (§4.4).
_STARTUP_GRACE_S = 5
# Bound on each runtime-CLI call during cleanup/reaping (§4.4/§4.5).
_CLEANUP_STEP_S = 5
# Max wait for a concurrency slot before a legible refusal (S4-D1).
_SEM_WAIT_S = 5
# Owner-stamped deadline = spawn + timeout_s + this slack; covers the startup grace,
# the kill/rm budget, and margin for the owner's own cleanup to finish (S4-D3).
_DEADLINE_SLACK_S = 90
# A reaper removes a container only when now > its deadline + this margin (S4-D3).
_REAP_MARGIN_S = 60
# How to start a runtime that is not responding, for refusal messages. Runtime-specific
# so a Podman user is never pointed at Docker Desktop (and vice versa).
START_HINTS = {
    "docker": " (Docker Desktop on Windows/macOS)",
    "podman": " (`podman machine start` on Windows/macOS)",
}


def _container_user() -> str:
    # Linux host: match the invoking user so workspace files have sane ownership.
    # macOS/Windows (Docker Desktop VM): any fixed unprivileged uid works.
    getuid = getattr(os, "getuid", None)
    getgid = getattr(os, "getgid", None)
    if getuid is not None and getgid is not None:
        return f"{getuid()}:{getgid()}"
    return "1000:1000"


class ContainerBackend(Warden):
    """Runs each snippet in a fresh, locked-down, throwaway container."""

    def __init__(
        self,
        runtime: str = "docker",
        soft_disabled: frozenset[str] = frozenset(),
        slots: asyncio.Semaphore | None = None,
    ) -> None:
        self._runtime = runtime
        # D3: soft limits the step-5 probe proved unenforceable here; runs proceed
        # without them and report container-degraded (never plain container).
        self._soft_disabled = soft_disabled
        # Observability only — never a reap criterion (a crashed owner is
        # indistinguishable from a live one without a liveness oracle; S4-D3).
        self._owner = secrets.token_hex(8)
        self._active: set[str] = set()  # names of this process's in-flight containers
        self._images_ok: set[str] = set()  # images that passed preflight (S5-D3)
        # S7-D1: the selector passes ONE semaphore to every backend it builds, so
        # `max_concurrent` keeps bounding existing containers across re-selection.
        # A standalone backend (tests) lazily makes its own.
        self._sem: asyncio.Semaphore | None = slots
        self._reap_task: asyncio.Task[None] | None = None
        self._finishers: set[asyncio.Task[None]] = set()  # strong refs: loop holds tasks weakly
        self._inflight = 0  # run() calls in progress, counted from entry (before preflight)
        # Set at shutdown (S7-D1): the orphan reaper skips its remaining steps (another
        # server's stale containers can wait for the next start; the client is leaving).
        self._closing = False
        # S7-D3: set by the selection probe when a soft limit could not be verified
        # inside the container (cgroup readback mismatch, or a runtime warning on
        # cgroup v1). Runs then report container-degraded with this detail.
        self._limits_note: str | None = None

    @property
    def runtime(self) -> str:
        return self._runtime

    @property
    def inflight(self) -> int:
        """run() calls in progress on this backend (counted from entry)."""
        return self._inflight

    def set_limits_note(self, note: str | None) -> None:
        self._limits_note = note

    def isolation_status(self) -> tuple[str, str | None]:
        """What a run on this backend honestly reports (D3): plain `container` only
        when the full profile applied AND was verified; otherwise `container-degraded`
        with exactly what is missing."""
        notes = []
        if self._soft_disabled:
            notes.append(f"limits not applied: {', '.join(sorted(self._soft_disabled))}")
        if self._limits_note:
            notes.append(self._limits_note)
        if notes:
            return "container-degraded", "; ".join(notes)
        return "container", None

    def image_for(self, language: str, limits: Limits) -> str:
        # python:3.12-slim is Debian-based and ships bash — it serves both (D2).
        return limits.image_node if language == "node" else limits.image_python

    def _build_command(
        self, run_id: str, language: str, limits: Limits,
        sandbox_host: str, workspace_host: str, deadline_epoch: int, timeout_s: int,
    ) -> list[str]:
        """The exact §3 execution profile. Never add --privileged/--device/host
        namespaces/socket mounts/-i/-t here; the environment is only what we pass."""
        script_name = f"main{_EXT[language]}"
        ws_suffix = ":ro" if limits.workspace_ro else ""
        cmd = [
            self._runtime, "run",
            "--name", f"vestibule-{run_id}",
            "--label", "vestibule.run=1",
            "--label", f"vestibule.run_id={run_id}",
            "--label", f"vestibule.deadline={deadline_epoch}",
            "--label", f"vestibule.owner={self._owner}",
            "--rm", "--init",
            "--pull", "never",  # S5-D3: a missing image must never trigger an in-call pull (D2)
            "--network", "none",
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            "--read-only",
            "--user", _container_user(),
        ]
        if "memory" not in self._soft_disabled:
            cmd += ["--memory", f"{limits.mem_mb}m",
                    "--memory-swap", f"{limits.mem_mb}m"]  # swap == mem => no swap escape
        if "cpu" not in self._soft_disabled:
            cmd += ["--cpus", f"{limits.cpu_pct / 100:g}"]
        if "pids" not in self._soft_disabled:
            cmd += ["--pids-limit", str(limits.pids_max)]
        # The tmpfs itself is not soft (guests need a writable /tmp under --read-only);
        # only its size cap is.
        tmpfs_size = "" if "tmpfs-size" in self._soft_disabled else f",size={limits.tmpfs_mb}m"
        cmd += [
            "--tmpfs", f"/tmp:rw,nosuid,nodev{tmpfs_size}",
            "-e", "HOME=/tmp/home",
            "-e", "TMPDIR=/tmp",
            "-e", "PYTHONDONTWRITEBYTECODE=1",
            "-e", "NODE_OPTIONS=",
            "-v", f"{workspace_host}:/workspace{ws_suffix}",
            "-v", f"{sandbox_host}:/sandbox:ro",
            "--workdir", "/workspace",
            self.image_for(language, limits),
            # S7-D5: the container ends itself at the deadline its owner stamped on the
            # label, even if this server was killed and nobody will ever `kill` it.
            # Normally never fires: the server kills timed-out runs at timeout + grace.
            # Under --init, `timeout` kills its whole process group, then tini exits.
            "timeout", "-s", "KILL", str(timeout_s + _DEADLINE_SLACK_S),
            _INTERPRETER[language], f"/sandbox/{script_name}",
        ]
        return cmd

    async def run(self, language: str, code: str, timeout_s: int, limits: Limits) -> RunResult:
        # Counted before any await: idle() must never mistake a starting run (still in
        # preflight or waiting for a slot) for an idle backend (S7-D1 pruning).
        self._inflight += 1
        try:
            return await self._run(language, code, timeout_s, limits)
        finally:
            self._inflight -= 1

    async def _run(self, language: str, code: str, timeout_s: int, limits: Limits) -> RunResult:
        self._schedule_reap()  # detached housekeeping; costs the run path nothing (§3.3)

        # S5-D3: verify the image is present before anything spawns and before taking
        # a concurrency slot — `docker run` would otherwise auto-pull a missing image
        # inside the tool call (a D2 violation and a multi-minute stall).
        await self._preflight_image(self.image_for(language, limits))

        sem = self._sem
        if sem is None:  # lazy: bound to the running loop; no await before assignment
            sem = self._sem = asyncio.Semaphore(limits.max_concurrent)
        try:
            # asyncio.timeout keeps the acquire in this task: 3.11's wait_for runs it
            # in an inner task and can drop a cancel that lands as the slot frees,
            # carrying a cancelled request on into a spawn (S7-D1, critic finding 1).
            async with asyncio.timeout(_SEM_WAIT_S):
                await sem.acquire()
        except TimeoutError:
            raise RunRefusedError(
                f"too many concurrent runs (max {limits.max_concurrent}); retry shortly"
            ) from None

        slot_handed_off = False
        try:
            run_id = secrets.token_hex(8)
            name = f"vestibule-{run_id}"
            # Stamped after the slot is acquired, so queue time never eats the deadline.
            deadline_epoch = int(time.time()) + timeout_s + _DEADLINE_SLACK_S
            # D9: script goes to a per-run host temp dir mounted read-only at /sandbox —
            # not argv (Windows command-line limits), not stdin (-i forbidden), not the
            # workspace (would pollute the persistent channel).
            tmpdir = tempfile.mkdtemp(prefix="vestibule-run-")
            self._active.add(name)  # before the container can exist: reaper-proof
            try:
                return await self._execute(
                    run_id, name, language, code, timeout_s, limits, tmpdir, deadline_epoch
                )
            finally:
                # §4.5 (amended per Codex P2 review): cleanup is an independent task and
                # is NOT awaited — it must never delay the result past the server's
                # outer deadline, and it survives cancellation of this request. It owns
                # the semaphore slot from here, releasing it only once the container is
                # actually gone, so `max_concurrent` bounds *existing* containers.
                finisher = asyncio.create_task(self._finish_run(sem, name, tmpdir))
                self._finishers.add(finisher)
                finisher.add_done_callback(self._finishers.discard)
                slot_handed_off = True
                self._schedule_reap()
        finally:
            if not slot_handed_off:  # e.g. mkdtemp failed; nothing to clean up
                sem.release()

    async def _execute(self, run_id: str, name: str, language: str,
                       code: str, timeout_s: int, limits: Limits, tmpdir: str, deadline_epoch: int,
    ) -> RunResult:
        # newline="\n": text mode would otherwise translate \n -> \r\n on Windows
        # hosts, and CRLF breaks bash keywords in the Linux guest (`then\r`).
        (Path(tmpdir) / f"main{_EXT[language]}").write_text(
            code, encoding="utf-8", newline="\n"
        )
        workspace = limits.workspace_path
        workspace.mkdir(parents=True, exist_ok=True)

        cmd = self._build_command(
            run_id, language, limits, tmpdir, str(workspace), deadline_epoch, timeout_s
        )
        log.info("run %s: spawning %s container (image=%s)",
                 run_id, self._runtime, self.image_for(language, limits))
        try:
            # D10: DEVNULL stdin always — the server's stdin is the MCP channel.
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except (FileNotFoundError, OSError) as e:
            # S4-D2: nothing ran — never claim container isolation for a non-run.
            return RunResult(
                stdout="", stderr=f"container runtime unavailable: {e}",
                exit_code=127, timed_out=False, isolation="none",
                isolation_detail="runtime unavailable; nothing was executed",
            )

        # Streaming collection (finding 19): cap what we buffer per stream; on
        # overflow kill the container early rather than letting it chatter on.
        cap = 2 * limits.max_output_bytes
        out_buf, err_buf = bytearray(), bytearray()
        overflow = asyncio.Event()
        assert proc.stdout is not None and proc.stderr is not None
        collectors = asyncio.gather(
            self._collect(proc.stdout, out_buf, cap, overflow),
            self._collect(proc.stderr, err_buf, cap, overflow),
        )
        killer = asyncio.create_task(self._kill_on_overflow(overflow, name, run_id))

        # Codex budget finding (2026-07-05): nothing here may await the daemon on the
        # result path — on a wedged daemon the old serialized kill/rm/wait consumed the
        # server's whole outer deadline and the honest result was lost. On timeout the
        # result returns immediately; the detached finisher still kills the container
        # via the runtime (§4.4/§4.5), and a detached reaper collects the CLI process.
        timed_out = False
        cli_wedged = False
        try:
            await asyncio.wait_for(collectors, timeout=timeout_s + _STARTUP_GRACE_S)
            try:
                await asyncio.wait_for(proc.wait(), timeout=_CLEANUP_STEP_S)
            except asyncio.TimeoutError:
                # Streams hit EOF (the guest is done) but the CLI won't exit: a wedged
                # daemon, not a guest timeout — don't mislabel it as one.
                cli_wedged = True
                log.error("run %s: output complete but the %s CLI did not exit; detaching",
                          run_id, self._runtime)
                self._detach_cli_reap(proc, run_id)
        except asyncio.TimeoutError:
            timed_out = True
            log.warning("run %s: timeout after %ds; container kill detached", run_id, timeout_s)
            collectors.cancel()
            self._detach_cli_reap(proc, run_id)
        except asyncio.CancelledError:
            # The request was cancelled (client cancel or shutdown, S7-D1): the finisher
            # kills the container, and this tracked task collects its CLI, so drain()
            # sees the CLI process too instead of leaving it to loop-close cleanup.
            collectors.cancel()
            self._detach_cli_reap(proc, run_id)
            raise
        finally:
            killer.cancel()
            try:
                await collectors
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 - drain best-effort
                pass

        truncated = len(out_buf) >= cap or len(err_buf) >= cap
        exit_code = proc.returncode if proc.returncode is not None else -1
        stderr_text = err_buf.decode(errors="replace")
        if truncated and not timed_out:
            stderr_text += "\n[output exceeded collection cap; container was terminated]"
        if cli_wedged:
            stderr_text += "\n[run finished but the runtime did not report an exit code]"

        isolation, detail = self.isolation_status()
        if exit_code == 125 and not timed_out:
            # 125 is docker/podman's own "the run command itself failed" code: the
            # container never started (daemon died mid-session, image vanished) and
            # no guest code executed — never claim container isolation for a non-run.
            # A guest deliberately calling exit(125) lands here too; underclaiming
            # is the safe direction (the server re-probes on the next call).
            # The wording must hold for BOTH causes: a blanket "retry" would make the
            # model re-run guest code that already ran (workspace side effects included).
            isolation = "none"
            detail = (
                "exit code 125 is the runtime's own code for 'could not start the "
                "container', so no isolation is claimed for this run. If the container "
                "failed to start, retry: the next call re-checks setup and names the fix. "
                "If your code exited with 125 itself, it did run: do not retry; use a "
                "different exit code"
            )

        log.info("run %s: done exit=%s timed_out=%s", run_id, exit_code, timed_out)
        return RunResult(
            stdout=out_buf.decode(errors="replace"),
            stderr=stderr_text,
            exit_code=exit_code,
            timed_out=timed_out,
            isolation=isolation,
            isolation_detail=detail,
        )

    @staticmethod
    async def _collect(
        stream: asyncio.StreamReader, buf: bytearray, cap: int, overflow: asyncio.Event
    ) -> None:
        """Read to EOF, keeping at most `cap` bytes; signal overflow, keep draining."""
        while True:
            chunk = await stream.read(65536)
            if not chunk:
                return
            take = cap - len(buf)
            if take > 0:
                buf.extend(chunk[:take])
            if len(buf) >= cap:
                overflow.set()

    async def _kill_on_overflow(self, overflow: asyncio.Event, name: str, run_id: str) -> None:
        await overflow.wait()
        log.warning("run %s: output cap exceeded, killing container early", run_id)
        # Finisher-tracked + shielded so killer.cancel() (run end racing this kill)
        # can't strand the kill's CLI subprocess mid-call.
        task = asyncio.create_task(self._force_remove(name))
        self._finishers.add(task)
        task.add_done_callback(self._finishers.discard)
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.shield(task)

    def pending_tasks(self) -> list[asyncio.Task[None]]:
        """This backend's detached finisher/CLI-reap/reaper tasks still running."""
        tasks = [t for t in self._finishers if not t.done()]
        if self._reap_task is not None and not self._reap_task.done():
            tasks.append(self._reap_task)
        return tasks

    def idle(self) -> bool:
        """No run in progress and no detached task pending: safe to forget (S7-D1)."""
        return self._inflight == 0 and not self.pending_tasks()

    def active_names(self) -> list[str]:
        """Names of this backend's containers that may still exist."""
        return sorted(self._active)

    async def drain(self, timeout: float | None = None) -> bool:
        """Wait for this backend's detached tasks; True once none is left pending.

        For orderly teardown of a backend that will not be used again — a probe
        candidate the selector rejected, or every backend at shutdown. Each detached
        task is internally bounded, so an unbounded drain still returns promptly.

        Never cancels what it waits on (S7-D1): `asyncio.wait`, not `gather`. Inside
        the MCP SDK's anyio cancel scope the waiter itself is re-cancelled at every
        await, and `gather` would pass that cancellation on to the cleanup tasks: a
        finisher cancelled before its first step never releases its slot or removes
        its container, and one cancelled mid-`create_subprocess_exec` can deadlock
        loop close on Windows (CPython proactor). Loops because a run ending during
        the drain adds a new finisher.
        """
        loop = asyncio.get_running_loop()
        deadline = None if timeout is None else loop.time() + timeout
        while True:
            tasks = self.pending_tasks()
            if not tasks:
                return True
            remaining = None if deadline is None else deadline - loop.time()
            if remaining is not None and remaining <= 0:
                return False
            await asyncio.wait(tasks, timeout=remaining)

    def begin_close(self) -> None:
        """Shutdown (S7-D1): stop optional housekeeping (the orphan reaper). Run
        cleanup itself is never skipped — see `_force_remove`."""
        self._closing = True

    async def remove_now(self, names: list[str], timeout: float) -> None:
        """One batched `rm -f` of `names`, bounded by `timeout` (shutdown path)."""
        if names:
            await self._cli_status("rm", "-f", *names, timeout=timeout)

    def _detach_cli_reap(self, proc: asyncio.subprocess.Process, run_id: str) -> None:
        """Collect a `docker run` CLI process off the result path (Codex budget fix).

        Used when the result must return NOW (guest timeout) or when the CLI is
        wedged after the guest already finished. The finisher's kill/rm makes the
        CLI exit on its own; this task only reaps it — or kills it as a last resort.
        """
        task = asyncio.create_task(self._reap_cli(proc, run_id))
        self._finishers.add(task)
        task.add_done_callback(self._finishers.discard)

    async def _reap_cli(self, proc: asyncio.subprocess.Process, run_id: str) -> None:
        try:
            # Outlasts the finisher's kill (5 s) + rm -f (5 s), with slack.
            await asyncio.wait_for(proc.wait(), timeout=3 * _CLEANUP_STEP_S)
            return
        except asyncio.TimeoutError:
            pass
        except Exception:  # detached task — never propagate
            log.exception("run %s: CLI reap failed", run_id)
            return
        log.error("run %s: %s CLI still alive after container removal; killing it",
                  run_id, self._runtime)
        with contextlib.suppress(Exception):
            proc.kill()
            await proc.wait()

    async def _finish_run(self, sem: asyncio.Semaphore, name: str, tmpdir: str) -> None:
        """Detached post-run task (Codex P2): cleans up, then hands back the slot."""
        try:
            await self._cleanup(name, tmpdir)
        except Exception:  # detached task — never propagate
            log.exception("cleanup for %s failed", name)
        finally:
            self._active.discard(name)  # unconditional — the reaper backstops
            sem.release()

    async def _cleanup(self, name: str, tmpdir: str) -> None:
        """Post-run cleanup; idempotent alongside --rm and the timeout kill path (§4.5).

        Order matters — keep the script removal right after `_force_remove`. A run
        cancelled while `docker run` is still creating its container can have both
        `kill` and `rm -f` miss ("No such container"), after which the CLI starts the
        container anyway (seen in 7 of 12 live tries, 7a critic round 2). Removing
        the script dir (mounted at /sandbox) at once is what stops that late guest:
        it finds no script, exits 127, and `--rm` removes it. Deferring or moving
        this rmtree would let it run until its S7-D5 deadline.
        """
        await self._force_remove(name)
        await asyncio.to_thread(shutil.rmtree, tmpdir, ignore_errors=True)

    async def _force_remove(self, name: str) -> None:
        """`kill` then `rm -f`, each bounded; 'No such container' is fine (races --rm).

        Deliberately NOT skipped at shutdown even though the batched removal just ran:
        a container still being created when that batch ran was missed by it, and only
        this per-name pass catches it (~0.3 s on Docker Desktop, inside the budget).
        """
        await self._cli("kill", name)
        await self._cli("rm", "-f", name)

    def _schedule_reap(self) -> None:
        """Fire-and-forget reap pass; at most one in flight. Never blocks a run (§3.3)."""
        if self._reap_task is not None and not self._reap_task.done():
            return
        self._reap_task = asyncio.create_task(self._reap_orphans())

    async def _reap_orphans(self) -> None:
        """S4-D3: remove labeled containers past their owner-stamped deadline.

        One state-independent rule — no created/exited/running special cases, no
        daemon timestamps, no local timeout config. Containers without a parseable
        deadline are skipped loudly, never removed; this process's in-flight runs
        are protected by the active-name set (and by their future deadlines).
        """
        try:
            listing = await self._cli("ps", "-aq", "--filter", "label=vestibule.run=1")
            ids = listing.split() if listing else []
            if not ids or self._closing:  # shutdown: housekeeping can wait for next time
                return
            # The id is part of the format so a container vanishing between ps and
            # inspect can never misalign the output with the id list.
            fmt = '{{.Id}}\t{{.Name}}\t{{index .Config.Labels "vestibule.deadline"}}'
            info = await self._cli("inspect", "--format", fmt, *ids)
            if info is None:
                return
            now = time.time()
            doomed: list[str] = []
            for line in info.splitlines():
                cid, _, rest = line.partition("\t")
                name, _, deadline_raw = rest.partition("\t")
                if not cid:
                    continue
                if self._reap_decision(name.lstrip("/"), deadline_raw, now, self._active):
                    doomed.append(cid)
            if doomed and not self._closing:
                log.warning("reaper: removing %d stale labeled container(s)", len(doomed))
                await self._cli("rm", "-f", *doomed)
        except Exception:  # detached task — never propagate
            log.exception("reaper: pass failed")

    @staticmethod
    def _reap_decision(name: str, deadline_raw: str, now: float, active: set[str]) -> bool:
        """Pure keep/remove rule (unit-tested without Docker). Never reap on bad data."""
        if name in active:
            return False
        try:
            deadline = int(deadline_raw.strip())
        except ValueError:
            log.warning(
                "reaper: container %s has no parseable vestibule.deadline (%r); skipping",
                name, deadline_raw,
            )
            return False
        return now > deadline + _REAP_MARGIN_S

    async def _preflight_image(self, image: str) -> None:
        """S5-D3: refuse legibly when the image is not locally usable.

        One successful check per image per process; `--pull never` in the run
        profile backstops the race (image deleted after this check passed).
        """
        if image in self._images_ok:
            return
        res = await self._cli_status("image", "inspect", "--format", "{{.Id}}", image)
        if res is None:
            raise RunRefusedError(
                f"container runtime did not respond while checking image {image}; retry shortly"
            )
        code, _out, err = res
        if code != 0:
            # `image inspect` also fails when the daemon is down — the runtime's own
            # first error line tells the user which case they are in.
            reason = (err.strip().splitlines() or ["unknown error"])[0][:200]
            # The pull command quotes the configured ref verbatim: with a digest-pinned
            # default, `pull <tag>` could fetch different bytes and never satisfy this
            # check (M1 step 6 plan §2).
            raise RunRefusedError(
                f"image {image} is not usable ({reason}). If it is missing, run "
                f"`{self._runtime} pull {image}` once and retry; Vestibule never "
                f"pulls images itself. If {self._runtime} itself is not responding, "
                f"start it{START_HINTS.get(self._runtime, '')}, then retry."
            )
        self._images_ok.add(image)

    async def _cli_status(
        self, *args: str, timeout: float = _CLEANUP_STEP_S
    ) -> tuple[int, str, str] | None:
        """One bounded runtime-CLI call (D10 stdio rules).

        Returns (exit_code, stdout, stderr), or None if the call could not be
        spawned or timed out.
        """
        try:
            proc = await asyncio.create_subprocess_exec(
                self._runtime, *args,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as e:
            log.warning("%s %s: cannot spawn: %s", self._runtime, args[0], e)
            return None
        try:
            async with asyncio.timeout(timeout):
                out, err = await proc.communicate()
        except TimeoutError:
            log.error("%s %s: no response in %gs", self._runtime, args[0], timeout)
            proc.kill()
            with contextlib.suppress(Exception):
                await proc.wait()
            return None
        except asyncio.CancelledError:
            # A cancelled caller (e.g. preflight of a cancelled request) must not
            # leave the CLI process running unowned until loop close.
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            raise
        assert proc.returncode is not None  # communicate() returned => process exited
        return proc.returncode, out.decode(errors="replace"), err.decode(errors="replace")

    async def _cli(self, *args: str) -> str | None:
        """`_cli_status`, output-only. Nonzero exits still return output — partial
        results matter (e.g. a batched `inspect` where one id vanished mid-flight),
        and races with --rm are benign.
        """
        res = await self._cli_status(*args)
        if res is None:
            return None
        code, out, err = res
        if code != 0:
            log.debug("%s %s: exit %s: %s", self._runtime, args[0], code, err.strip())
        return out
