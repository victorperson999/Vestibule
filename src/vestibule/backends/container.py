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

    def __init__(self, runtime: str = "docker") -> None:
        self._runtime = runtime
        # Observability only — never a reap criterion (a crashed owner is
        # indistinguishable from a live one without a liveness oracle; S4-D3).
        self._owner = secrets.token_hex(8)
        self._active: set[str] = set()  # names of this process's in-flight containers
        self._sem: asyncio.Semaphore | None = None
        self._reap_task: asyncio.Task[None] | None = None
        self._finishers: set[asyncio.Task[None]] = set()  # strong refs: loop holds tasks weakly

    def image_for(self, language: str, limits: Limits) -> str:
        # python:3.12-slim is Debian-based and ships bash — it serves both (D2).
        return limits.image_node if language == "node" else limits.image_python

    def _build_command(
        self, run_id: str, language: str, limits: Limits,
        sandbox_host: str, workspace_host: str, deadline_epoch: int,
    ) -> list[str]:
        """The exact §3 execution profile. Never add --privileged/--device/host
        namespaces/socket mounts/-i/-t here; the environment is only what we pass."""
        script_name = f"main{_EXT[language]}"
        ws_suffix = ":ro" if limits.workspace_ro else ""
        return [
            self._runtime, "run",
            "--name", f"vestibule-{run_id}",
            "--label", "vestibule.run=1",
            "--label", f"vestibule.run_id={run_id}",
            "--label", f"vestibule.deadline={deadline_epoch}",
            "--label", f"vestibule.owner={self._owner}",
            "--rm", "--init",
            "--network", "none",
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            "--read-only",
            "--user", _container_user(),
            "--memory", f"{limits.mem_mb}m",
            "--memory-swap", f"{limits.mem_mb}m",  # swap == mem => no swap escape
            "--cpus", f"{limits.cpu_pct / 100:g}",
            "--pids-limit", str(limits.pids_max),
            "--tmpfs", f"/tmp:rw,nosuid,nodev,size={limits.tmpfs_mb}m",
            "-e", "HOME=/tmp/home",
            "-e", "TMPDIR=/tmp",
            "-e", "PYTHONDONTWRITEBYTECODE=1",
            "-e", "NODE_OPTIONS=",
            "-v", f"{workspace_host}:/workspace{ws_suffix}",
            "-v", f"{sandbox_host}:/sandbox:ro",
            "--workdir", "/workspace",
            self.image_for(language, limits),
            _INTERPRETER[language], f"/sandbox/{script_name}",
        ]

    async def run(self, language: str, code: str, timeout_s: int, limits: Limits) -> RunResult:
        self._schedule_reap()  # detached housekeeping; costs the run path nothing (§3.3)

        sem = self._sem
        if sem is None:  # lazy: bound to the running loop; no await before assignment
            sem = self._sem = asyncio.Semaphore(limits.max_concurrent)
        try:
            await asyncio.wait_for(sem.acquire(), timeout=_SEM_WAIT_S)
        except asyncio.TimeoutError:
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
        (Path(tmpdir) / f"main{_EXT[language]}").write_text(code, encoding="utf-8")
        workspace = limits.workspace_path
        workspace.mkdir(parents=True, exist_ok=True)

        cmd = self._build_command(run_id, language, limits, tmpdir, str(workspace), deadline_epoch)
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

        log.info("run %s: done exit=%s timed_out=%s", run_id, exit_code, timed_out)
        return RunResult(
            stdout=out_buf.decode(errors="replace"),
            stderr=stderr_text,
            exit_code=exit_code,
            timed_out=timed_out,
            isolation="container",
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
        await self._force_remove(name)

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
        """Post-run cleanup; idempotent alongside --rm and the timeout kill path (§4.5)."""
        await self._force_remove(name)
        await asyncio.to_thread(shutil.rmtree, tmpdir, ignore_errors=True)

    async def _force_remove(self, name: str) -> None:
        """`kill` then `rm -f`, each bounded; 'No such container' is fine (races --rm)."""
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
            if not ids:
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
            if doomed:
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

    async def _cli(self, *args: str) -> str | None:
        """One bounded runtime-CLI call (D10 stdio rules).

        Returns stdout text, or None if the call could not be spawned or timed out.
        Nonzero exits still return output — partial results matter (e.g. a batched
        `inspect` where one id vanished mid-flight), and races with --rm are benign.
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
            out, err = await asyncio.wait_for(proc.communicate(), timeout=_CLEANUP_STEP_S)
        except asyncio.TimeoutError:
            log.error("%s %s: no response in %ds", self._runtime, args[0], _CLEANUP_STEP_S)
            proc.kill()
            with contextlib.suppress(Exception):
                await proc.wait()
            return None
        if proc.returncode != 0:
            log.debug("%s %s: exit %s: %s", self._runtime, args[0], proc.returncode,
                      err.decode(errors="replace").strip())
        return out.decode(errors="replace")
