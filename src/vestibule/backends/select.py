"""M1 step 5: backend selection & capability probing.

Contract: docs/plans/M1-container-backend.md §5, amended by
docs/plans/M1-step5-selection.md (S5-D1 failure-retry cooldown, S5-D2 all-soft-off
degraded retry, S5-D3 per-run image preflight). On the first tool call — never
during the MCP handshake — the selector test-drives the full locked-down container
profile through the SAME code path real runs use, then commits to an honest verdict:
`container`, `container-degraded`, or refused with an actionable fix. A silent
fallback to no isolation is structurally impossible: NaiveBackend is reachable only
via an explicit VESTIBULE_BACKEND=naive.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass

from vestibule.backends.base import RunRefusedError, RunResult, Warden
from vestibule.backends.container import SOFT_CONTROLS, ContainerBackend
from vestibule.backends.naive import NaiveBackend
from vestibule.config import Limits

log = logging.getLogger("vestibule.select")

# Bound on each selection CLI call (`<runtime> version`).
_CLI_WAIT_S = 5
# The probe is a real run with its own timeout. A cold Docker Desktop VM that
# misses this window fails once, then passes on the post-cooldown retry (S5-D1).
_PROBE_TIMEOUT_S = 10
# S5-D1: a failed selection is re-checked no sooner than this; success caches for
# the process lifetime. Keeps "start Docker Desktop, then retry" working without
# an MCP session restart, without hammering a dead daemon on every call.
_RETRY_COOLDOWN_S = 30

_PROBE_MARKER = "VESTIBULE-PROBE-OK"
# D3 hard-tier workspace round-trip, run as a normal bash guest (python:3.12-slim
# ships bash). RW mode: write/read/delete must all work.
_PROBE_RW = f"""
set -e
echo probe > /workspace/.vestibule-probe
cat /workspace/.vestibule-probe > /dev/null
rm /workspace/.vestibule-probe
echo {_PROBE_MARKER}
"""
# RO mode (VESTIBULE_WORKSPACE_RO=1): reads must work and writes must FAIL —
# a writable "read-only" workspace is a broken mount, not a bonus.
_PROBE_RO = f"""
set -e
ls /workspace > /dev/null
if echo probe > /workspace/.vestibule-probe 2>/dev/null; then
    rm -f /workspace/.vestibule-probe
    echo VESTIBULE-PROBE-RW-LEAK
    exit 1
fi
echo {_PROBE_MARKER}
"""

# D4: auto prefers Docker; only Docker is tested/supported in M1, Podman runs iff
# its probes pass (documented experimental).
_RUNTIME_ORDER: dict[str, tuple[str, ...]] = {
    "auto": ("docker", "podman"),
    "docker": ("docker",),
    "podman": ("podman",),
}


@dataclass
class Selection:
    """The committed verdict: a warden plus what its runs will honestly report."""

    warden: Warden
    verdict: str  # "none" (explicit naive only) | "container" | "container-degraded"
    detail: str | None = None


class BackendSelector:
    """Lazy, cached backend choice (contract §5). One instance per server process."""

    def __init__(self) -> None:
        self._lock: asyncio.Lock | None = None
        self._selection: Selection | None = None
        self._fail_reason: str | None = None
        self._failed_at: float = 0.0

    async def get(self, limits: Limits) -> Selection:
        """The cached verdict; raises RunRefusedError with an actionable message.

        Concurrent first calls share one probe via the lock. Selection runs outside
        the server's per-run outer deadline, so a slow first probe never eats a
        run's time budget.
        """
        lock = self._lock
        if lock is None:  # lazy: bound to the running loop; no await before assignment
            lock = self._lock = asyncio.Lock()
        async with lock:
            if self._selection is not None:
                return self._selection
            if self._fail_reason is not None:
                if time.monotonic() - self._failed_at < _RETRY_COOLDOWN_S:
                    raise RunRefusedError(self._fail_reason)
                log.info("selection: retry cooldown over, re-checking")
                self._fail_reason = None
            try:
                self._selection = await self._select(limits)
            except RunRefusedError as e:
                self._fail_reason = str(e)  # S5-D1: cached, re-checked after cooldown
                self._failed_at = time.monotonic()
                raise
            log.info(
                "selection: %s via %s",
                self._selection.verdict, type(self._selection.warden).__name__,
            )
            return self._selection

    def note_result(self, result: RunResult) -> None:
        """Post-run honesty hook: a container-tier selection that produced a run
        with no isolation (runtime binary gone — S4-D2 — or an exit-125 container
        start failure) has lost its runtime. Drop the cache so the next call
        re-checks and returns an actionable message instead of failing forever.
        """
        if (
            self._selection is not None
            and self._selection.verdict != "none"
            and result.isolation == "none"
        ):
            log.warning("selection: runtime failed mid-session; re-checking on next call")
            self._selection = None

    async def _select(self, limits: Limits) -> Selection:
        if limits.backend == "naive":
            log.warning("VESTIBULE_BACKEND=naive: NO isolation (dev-only opt-in)")
            return Selection(NaiveBackend(), "none", "explicitly configured, dev-only")
        if limits.backend != "auto":
            raise RunRefusedError(
                f"unknown VESTIBULE_BACKEND {limits.backend!r}; use 'auto' or 'naive'"
            )
        runtime = await self._resolve_runtime(limits)

        # Test-drive the FULL §3 profile through the normal run path — probe = real
        # run, image preflight included — so the probe can never drift from what a
        # real run gets. Success is the only thing that earns `container` (D3).
        backend = ContainerBackend(runtime)
        failure = await self._probe(backend, limits)
        if failure is None:
            return Selection(backend, "container")

        # S5-D2: one retry with every soft limit off. Hard controls are never
        # stripped, so a hard-tier failure fails this retry too and blocks.
        log.warning("selection: full-profile probe failed (%s); retrying without "
                    "soft limits", failure)
        degraded = ContainerBackend(runtime, soft_disabled=frozenset(SOFT_CONTROLS))
        second = await self._probe(degraded, limits)
        if second is None:
            detail = f"limits not applied: {', '.join(sorted(SOFT_CONTROLS))}"
            log.warning("selection: running DEGRADED — %s", detail)
            return Selection(degraded, "container-degraded", detail)
        raise RunRefusedError(
            f"container runtime cannot enforce the isolation profile ({second}); "
            "refusing to run without isolation"
        )

    async def _resolve_runtime(self, limits: Limits) -> str:
        order = _RUNTIME_ORDER.get(limits.runtime)
        if order is None:
            raise RunRefusedError(
                f"unknown VESTIBULE_RUNTIME {limits.runtime!r}; "
                "use 'auto', 'docker', or 'podman'"
            )
        problems: list[str] = []
        for rt in order:
            status = await _version_check(rt)
            if status is None:
                log.info("selection: runtime %r is reachable", rt)
                return rt
            log.warning("selection: runtime %r unusable: %s", rt, status)
            problems.append(f"{rt}: {status}")
        raise RunRefusedError(
            "no usable container runtime (" + "; ".join(problems) + "). "
            "Start Docker Desktop (or install Docker/Podman), then retry."
        )

    async def _probe(self, backend: ContainerBackend, limits: Limits) -> str | None:
        """Run the workspace round-trip probe; None on success, else a short reason.

        RunRefusedError propagates (e.g. the image preflight's pull message IS the
        actionable verdict).
        """
        script = _PROBE_RO if limits.workspace_ro else _PROBE_RW
        r = await backend.run("bash", script, _PROBE_TIMEOUT_S, limits)
        for line in r.stderr.splitlines():
            # Runtimes may only WARN about limits they silently drop (e.g. no swap
            # accounting). We can't verify those behaviorally until the step-7
            # acceptance suite, but misconfiguration must never be silent.
            if line.strip().upper().startswith("WARNING"):
                log.warning("probe: runtime warning: %s", line.strip())
        ok = (
            r.exit_code == 0
            and not r.timed_out
            and _PROBE_MARKER in r.stdout
            and r.isolation != "none"
        )
        if ok:
            return None
        first_err = (r.stderr.strip().splitlines() or ["no error output"])[0][:200]
        return f"probe exit {r.exit_code}, timed_out={r.timed_out}: {first_err}"


async def _version_check(runtime: str) -> str | None:
    """None if `<runtime> version` succeeds (CLI present, daemon reachable);
    otherwise a short human-readable reason."""
    try:
        proc = await asyncio.create_subprocess_exec(
            runtime, "version",
            stdin=asyncio.subprocess.DEVNULL,  # D10 stdio rules
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        return "not installed"
    except OSError as e:
        return f"cannot run: {e}"
    try:
        _out, err = await asyncio.wait_for(proc.communicate(), timeout=_CLI_WAIT_S)
    except asyncio.TimeoutError:
        proc.kill()
        with contextlib.suppress(Exception):
            await proc.wait()
        return "not responding"
    if proc.returncode != 0:
        first = (err.decode(errors="replace").strip().splitlines() or ["unknown error"])[0]
        return f"daemon not reachable ({first[:200]}) — is Docker Desktop running?"
    return None
