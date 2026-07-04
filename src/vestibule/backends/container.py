"""M1 container backend: one throwaway Docker/Podman container per run.

Contract: docs/plans/M1-container-backend.md (§3 execution profile, §4 lifecycle,
D9 script delivery, D10 stdio rules). The runtime CLI is driven via
asyncio.create_subprocess_exec — async-safe, no blocking of the event loop.

Step-3 scope note: this file implements the happy path (full §3 profile, read-only
/sandbox script mount, streaming output caps with early kill) plus a minimal
timeout-kill guard. The complete §4 machinery — cancellation-shielded cleanup,
orphan reaping, concurrency semaphore — lands in step 4.
"""
from __future__ import annotations

import asyncio
import logging
import os
import secrets
import shutil
import tempfile
from pathlib import Path

from vestibule.backends.base import RunResult, Warden
from vestibule.config import Limits

log = logging.getLogger("vestibule.container")

_EXT = {"python": ".py", "bash": ".sh", "node": ".js"}
_INTERPRETER = {"python": "python", "bash": "bash", "node": "node"}

# Grace added on top of the guest timeout: container cold start (image unpack,
# Docker Desktop VM wakeup) happens inside `docker run`, and must not eat the
# guest's own budget (§4.4).
_STARTUP_GRACE_S = 5
# Bound on each kill/rm CLI call during cleanup (§4.4).
_CLEANUP_STEP_S = 5


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

    def image_for(self, language: str, limits: Limits) -> str:
        # python:3.12-slim is Debian-based and ships bash — it serves both (D2).
        return limits.image_node if language == "node" else limits.image_python

    def _build_command(
        self,
        run_id: str,
        language: str,
        limits: Limits,
        sandbox_host: str,
        workspace_host: str,
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
        run_id = secrets.token_hex(8)
        name = f"vestibule-{run_id}"

        # D9: script goes to a per-run host temp dir mounted read-only at /sandbox —
        # not argv (Windows command-line limits), not stdin (-i forbidden), not the
        # workspace (would pollute the persistent channel).
        tmpdir = tempfile.mkdtemp(prefix="vestibule-run-")
        try:
            (Path(tmpdir) / f"main{_EXT[language]}").write_text(code, encoding="utf-8")
            workspace = limits.workspace_path
            workspace.mkdir(parents=True, exist_ok=True)

            cmd = self._build_command(run_id, language, limits, tmpdir, str(workspace))
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
                return RunResult(
                    stdout="", stderr=f"container runtime unavailable: {e}",
                    exit_code=127, timed_out=False, isolation="container",
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

            timed_out = False
            try:
                await asyncio.wait_for(collectors, timeout=timeout_s + _STARTUP_GRACE_S)
                await asyncio.wait_for(proc.wait(), timeout=_CLEANUP_STEP_S)
            except asyncio.TimeoutError:
                timed_out = True
                log.warning("run %s: timeout after %ds, killing container", run_id, timeout_s)
                # §4.4: kill the CONTAINER via the runtime, never just the CLI process.
                await self._force_remove(name)
                collectors.cancel()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=_CLEANUP_STEP_S)
                except asyncio.TimeoutError:
                    proc.kill()  # last resort: the CLI itself is wedged
                    await proc.wait()
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

            log.info("run %s: done exit=%s timed_out=%s", run_id, exit_code, timed_out)
            return RunResult(
                stdout=out_buf.decode(errors="replace"),
                stderr=stderr_text,
                exit_code=exit_code,
                timed_out=timed_out,
                isolation="container",
            )
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

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

    async def _force_remove(self, name: str) -> None:
        """`kill` then `rm -f`, each bounded; 'No such container' is fine (races --rm)."""
        for args in (("kill", name), ("rm", "-f", name)):
            try:
                proc = await asyncio.create_subprocess_exec(
                    self._runtime, *args,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await asyncio.wait_for(proc.wait(), timeout=_CLEANUP_STEP_S)
            except asyncio.TimeoutError:
                log.error("container %s: '%s' did not return in %ds",
                          name, " ".join(args), _CLEANUP_STEP_S)
            except OSError as e:
                log.error("container %s: cleanup failed: %s", name, e)
