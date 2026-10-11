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
import math
import re
import time
from dataclasses import dataclass

from vestibule.backends.base import RunRefusedError, RunResult, Warden
from vestibule.backends.container import SOFT_CONTROLS, START_HINTS, ContainerBackend
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

# S6-D2: an image ref counts as pinned only if it names an exact digest.
_PINNED_REF = re.compile(r"@sha256:[0-9a-f]{64}$")

# `_version_check`'s status for a missing CLI binary (vs. a daemon that won't answer).
_NOT_INSTALLED = "not installed"

# Config is read once at server start (server.py LIMITS) and the process environment is
# fixed when the agent launches the server, so config mistakes need a restart, not a retry.
_RESTART_HINT = (
    "Fix it in the Vestibule server's MCP config, then restart the server "
    "(settings are read once at startup)."
)

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


class _RestartRequired(RunRefusedError):
    """A refusal only a restart can fix (config is read once at startup; a running
    process keeps the PATH it was launched with). Its cached copy is never annotated
    with the retry cooldown: waiting cannot help, and saying it might sends the model
    into a wait-and-retry loop."""


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
        self._fail_needs_restart = False
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
                waited = time.monotonic() - self._failed_at
                if waited < _RETRY_COOLDOWN_S:
                    if self._fail_needs_restart:
                        raise _RestartRequired(self._fail_reason)
                    # Say so: a fix made inside the window would otherwise look failed.
                    raise RunRefusedError(
                        f"{self._fail_reason} (setup is re-checked at most every "
                        f"{_RETRY_COOLDOWN_S} s; next check in "
                        f"{math.ceil(_RETRY_COOLDOWN_S - waited)} s)"
                    )
                log.info("selection: retry cooldown over, re-checking")
                self._fail_reason = None
            try:
                self._selection = await self._select(limits)
            except RunRefusedError as e:
                self._fail_reason = str(e)  # S5-D1: cached, re-checked after cooldown
                self._fail_needs_restart = isinstance(e, _RestartRequired)
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
            raise _RestartRequired(
                f"unknown VESTIBULE_BACKEND {limits.backend!r}; use 'auto' or 'naive'. "
                + _RESTART_HINT
            )
        _warn_unpinned(limits)
        runtime = await self._resolve_runtime(limits)

        # Test-drive the FULL §3 profile through the normal run path — probe = real
        # run, image preflight included — so the probe can never drift from what a
        # real run gets. Success is the only thing that earns `container` (D3).
        # Every rejected candidate is drain()ed: its detached cleanup tasks must
        # not outlive selection, or event-loop shutdown can deadlock on them.
        backend = ContainerBackend(runtime)
        try:
            failure = await self._probe(backend, limits)
        except BaseException:
            await backend.drain()
            raise
        if failure is None:
            return Selection(backend, "container")
        await backend.drain()

        # S5-D2: one retry with every soft limit off. Hard controls are never
        # stripped, so a hard-tier failure fails this retry too and blocks.
        log.warning("selection: full-profile probe failed (%s); retrying without "
                    "soft limits", failure)
        degraded = ContainerBackend(runtime, soft_disabled=frozenset(SOFT_CONTROLS))
        try:
            second = await self._probe(degraded, limits)
        except BaseException:
            await degraded.drain()
            raise
        if second is None:
            detail = f"limits not applied: {', '.join(sorted(SOFT_CONTROLS))}"
            log.warning("selection: running DEGRADED — %s", detail)
            return Selection(degraded, "container-degraded", detail)
        await degraded.drain()
        # Neutral on cause: a hard-tier failure and an override image that cannot run
        # the bash probe look identical from here; the parenthetical says which.
        raise RunRefusedError(
            f"the isolation probe failed even with resource limits off ({second}): "
            "either the container runtime cannot enforce the isolation profile, or the "
            "configured image cannot run the probe (it needs bash); refusing to run "
            "without isolation"
        )

    async def _resolve_runtime(self, limits: Limits) -> str:
        order = _RUNTIME_ORDER.get(limits.runtime)
        if order is None:
            raise _RestartRequired(
                f"unknown VESTIBULE_RUNTIME {limits.runtime!r}; "
                "use 'auto', 'docker', or 'podman'. " + _RESTART_HINT
            )
        problems: list[str] = []
        statuses: list[str] = []
        for rt in order:
            status = await _version_check(rt)
            if status is None:
                log.info("selection: runtime %r is reachable", rt)
                return rt
            log.warning("selection: runtime %r unusable: %s", rt, status)
            problems.append(f"{rt}: {status}")
            statuses.append(status)
        msg = "no usable container runtime (" + "; ".join(problems) + "). "
        if all(s == _NOT_INSTALLED for s in statuses):
            # "not installed" really means "not found on this process's PATH": a fresh
            # install, an install the agent's PATH doesn't include, or a mis-set
            # VESTIBULE_RUNTIME. Every one of those needs a restart to take effect.
            raise _RestartRequired(
                msg + "Install Docker (or Podman); if it is already installed, make sure "
                "it is on the PATH your agent starts Vestibule with, and check "
                "VESTIBULE_RUNTIME. Then restart your agent."
            )
        # Runtime-specific start hints: a Podman user is never pointed at Docker Desktop.
        down = [rt for rt, s in zip(order, statuses) if s != _NOT_INSTALLED]
        start = " or ".join(f"{rt}{START_HINTS.get(rt, '')}" for rt in down)
        raise RunRefusedError(msg + f"Start {start}, then retry.")

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


def _warn_unpinned(limits: Limits) -> None:
    """S6-D2: an un-pinned image override is allowed, but never silent.

    Both refs are checked once per selection pass: the probe only exercises the python
    image, so the node ref would otherwise go unchecked until its first run.
    """
    for var, ref in (
        ("VESTIBULE_IMAGE_PYTHON", limits.image_python),
        ("VESTIBULE_IMAGE_NODE", limits.image_node),
    ):
        if not _PINNED_REF.search(ref):
            log.warning(
                "image %r (%s) is not digest-pinned; the tag can change underneath you. "
                "Pin it as <name>@sha256:<digest>.", ref, var,
            )


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
        return _NOT_INSTALLED
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
        return f"daemon not reachable ({first[:200]})"
    return None
