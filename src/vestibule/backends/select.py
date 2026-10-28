"""M1 step 5: backend selection & capability probing.

Contract: docs/plans/M1/M1-container-backend.md §5, amended by
docs/plans/M1/M1-step5-selection.md (S5-D1 failure-retry cooldown, S5-D2 all-soft-off
degraded retry, S5-D3 per-run image preflight). On the first tool call — never
during the MCP handshake — the selector test-drives the full locked-down container
profile through the SAME code path real runs use, then commits to an honest verdict:
`container`, `container-degraded`, or refused with an actionable fix. A silent
fallback to no isolation is structurally impossible: NaiveBackend is reachable only
via an explicit VESTIBULE_BACKEND=naive.

Step 7a (docs/plans/M1/M1-step7-acceptance.md): the selector tracks every backend it
builds and shares one concurrency budget between them; aclose() cleans up at shutdown
without cancelling cleanup tasks (S7-D1). Since M2 step 1b that in-process shutdown is the
Windows path only; on POSIX the server hands `cleanup_job()` to a detached cleaner instead
(docs/plans/M2/M2-step1-linux-baseline.md, S1-D5). The probe reads its soft limits back from
the container's cgroup, so `container` is claimed only for verified limits (S7-D3),
and its workspace file name is random per probe.

M2 step 6 (docs/plans/M2/M2-step6-selection.md): `VESTIBULE_BACKEND` is `auto | native |
container | naive`. On Linux, `auto` probes native first and follows N5's order: native >
container > native-degraded > container-degraded > refused (S6-D3). The native probe is a
normal native run of the same workspace round-trip; its own per-run verdict is the check
(S6-D1). A native-degraded win over a container that was only refused for now is provisional,
and re-checked after the cooldown (S6-D6). Native runs share the one concurrency budget
(S6-D5) and need nothing at shutdown: they own no host files and end with the server (S6-D2).
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import re
import secrets
import sys
import time
from dataclasses import dataclass, field

from vestibule import registry, rootfs
from vestibule.backends.base import RunRefusedError, RunResult, Warden
from vestibule.backends.container import SOFT_CONTROLS, START_HINTS, ContainerBackend
from vestibule.backends.naive import NaiveBackend
from vestibule.backends.native import NativeBackend
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

# S7-D1 shutdown budget. MCP clients close stdin, wait ~2 s, then kill the server's
# process tree, so the expensive leak (running containers) goes first in one batched
# `rm -f`, then a short non-cancelling wait for the remaining cleanup tasks.
_SHUTDOWN_RM_S = 1.5
_SHUTDOWN_WAIT_S = 0.5
# Bound on draining a rejected probe candidate while the selection lock is held.
# Normal cleanup takes well under a second; on a wedged daemon the leftovers stay
# tracked and shutdown's aclose() reaches them (S7-D1).
_CANDIDATE_DRAIN_S = 10

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
_LIMITS_TAG = "VESTIBULE-LIMITS"
# The probe's workspace file gets a fresh random name per probe (§4.1 of the step-7
# plan): with a fixed name, one guest creating a directory there made every later
# selection fail — in every future server process too, since the workspace persists.
_PROBE_FILE = "@PROBE_FILE@"
# S7-D3: the container reads back its own soft limits (cgroup v2), so `container` is
# claimed only for limits verifiably applied — never on the runtime's word alone.
_LIMITS_READBACK = f"""
cg=/sys/fs/cgroup
if [ -f "$cg/cgroup.controllers" ]; then
    echo "{_LIMITS_TAG} cgroup=v2 memory.max=$(cat "$cg/memory.max" 2>/dev/null)" \\
        "memory.swap.max=$(cat "$cg/memory.swap.max" 2>/dev/null)" \\
        "pids.max=$(cat "$cg/pids.max" 2>/dev/null)" \\
        "cpu.max=$(cat "$cg/cpu.max" 2>/dev/null | tr ' ' ,)" \\
        "tmp=$(stat -f -c %b,%S /tmp 2>/dev/null)"
elif [ -d "$cg/memory" ] || [ -d "$cg/pids" ]; then
    echo "{_LIMITS_TAG} cgroup=v1"
else
    echo "{_LIMITS_TAG} cgroup=none"
fi
"""
# D3 hard-tier workspace round-trip, run as a normal bash guest (python:3.12-slim
# ships bash). RW mode: write/read/delete must all work.
_PROBE_RW = f"""
set -e
echo probe > {_PROBE_FILE}
cat {_PROBE_FILE} > /dev/null
rm {_PROBE_FILE}
{_LIMITS_READBACK}
echo {_PROBE_MARKER}
"""
# RO mode (VESTIBULE_WORKSPACE_RO=1): reads must work and writes must FAIL —
# a writable "read-only" workspace is a broken mount, not a bonus.
_PROBE_RO = f"""
set -e
ls /workspace > /dev/null
if echo probe > {_PROBE_FILE} 2>/dev/null; then
    rm -f {_PROBE_FILE}
    echo VESTIBULE-PROBE-RW-LEAK
    exit 1
fi
{_LIMITS_READBACK}
echo {_PROBE_MARKER}
"""
# Runtime warning lines in the CLI's stderr: Docker's `WARNING: …` and Podman's
# logrus forms (`WARN[0000] …`, `time=… level=warning msg=…`).
_RUNTIME_WARNING = re.compile(r"^\s*(?:WARNING\b|WARN\[|.*\blevel=warn(?:ing)?\b)")

# D4: auto prefers Docker; only Docker is tested/supported in M1, Podman runs iff
# its probes pass (documented experimental).
_RUNTIME_ORDER: dict[str, tuple[str, ...]] = {
    "auto": ("docker", "podman"),
    "docker": ("docker",),
    "podman": ("podman",),
}
# M2 step 6 (S6-D3): the four backend values.
_BACKENDS = ("auto", "native", "container", "naive")
# S6-D4: the two settings that explain a refused user namespace, read for the hint only.
_SYSCTLS = {
    "apparmor_restrict_unprivileged_userns":
        "/proc/sys/kernel/apparmor_restrict_unprivileged_userns",
    "max_user_namespaces": "/proc/sys/user/max_user_namespaces",
}


class _ProbeFailed(RunRefusedError):
    """The full container profile ran and failed its probe: deterministic on this host, so a
    native-degraded selection made next to it is final, not provisional (S6-D6)."""


class _RestartRequired(RunRefusedError):
    """A refusal only a restart can fix (config is read once at startup; a running
    process keeps the PATH it was launched with). Its cached copy is never annotated
    with the retry cooldown: waiting cannot help, and saying it might sends the model
    into a wait-and-retry loop."""


@dataclass
class Selection:
    """The committed verdict: a warden plus what its runs will honestly report."""

    warden: Warden
    # "none" (explicit naive only) | "container" | "container-degraded" | "native"
    # | "native-degraded"
    verdict: str
    detail: str | None = None
    # S6-D6: a provisional selection (native-degraded over a container refused for now) is
    # selected again once this monotonic time passes; None means cached for the process.
    recheck_at: float | None = None


@dataclass
class CleanupJob:
    """Everything this process's backends may still own, for the POSIX shutdown hand-off.

    `payload()` is what the detached cleaner gets (vestibule.cleanup); the CLI processes
    are killed by the server itself before it exits.
    """

    runtimes: list[dict[str, list[str]]] = field(default_factory=list)
    tmpdirs: list[str] = field(default_factory=list)
    cli_procs: list[asyncio.subprocess.Process] = field(default_factory=list)
    # The dev-only naive backend's live guests: killed with their process groups.
    guests: list[asyncio.subprocess.Process] = field(default_factory=list)

    @property
    def names(self) -> list[str]:
        return [n for rt in self.runtimes for n in rt["names"]]

    @property
    def empty(self) -> bool:
        """Nothing to hand off: no container that may exist and no temp dir left."""
        return not self.names and not self.tmpdirs

    def payload(self) -> dict[str, object]:
        return {"runtimes": self.runtimes, "tmpdirs": self.tmpdirs}


class BackendSelector:
    """Lazy, cached backend choice (contract §5). One instance per server process."""

    def __init__(self) -> None:
        self._lock: asyncio.Lock | None = None
        self._selection: Selection | None = None
        self._fail_reason: str | None = None
        self._fail_needs_restart = False
        self._failed_at: float = 0.0
        # S7-D1: every ContainerBackend this selector built (probe candidates, degraded,
        # live, dropped) until it is idle, so shutdown can reach all of their containers
        # and cleanup tasks — including a candidate whose probe was cancelled mid-run.
        self._backends: list[ContainerBackend] = []
        # S7-D1: one concurrency budget for the whole process, shared by every backend,
        # so a dropped backend's still-running containers keep counting.
        self._slots: asyncio.Semaphore | None = None

    async def get(self, limits: Limits) -> Selection:
        """The cached verdict; raises RunRefusedError with an actionable message.

        Concurrent first calls share one probe via the lock. Selection runs outside
        the server's per-run outer deadline, so a slow first probe never eats a
        run's time budget.
        """
        lock = self._lock
        if lock is None:  # lazy: bound to the running loop; no await before assignment
            lock = self._lock = asyncio.Lock()
        if self._slots is None:  # created once per process, never per selection
            self._slots = asyncio.Semaphore(limits.max_concurrent)
        async with lock:
            sel = self._selection
            if sel is not None:
                if sel.recheck_at is not None and time.monotonic() >= sel.recheck_at:
                    return await self._recheck_container(limits, sel)
                return sel
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

    def note_result(self, warden: Warden, result: RunResult) -> None:
        """Post-run honesty hook. A selection whose own run reports no isolation has lost
        its backend: the container runtime is gone (S4-D2, an exit-125 start failure), or the
        native rootfs or libseccomp is (N5). A `native` selection whose run comes back
        degraded has lost its limits, so N5's order may now pick the container (S6-D6). Either
        way the cache is dropped and the next call selects again; a degraded selection isn't
        dropped for a degraded run, so this can't loop.

        Only a result from the *current* selection counts (S7-D1): a late result
        from an already-dropped backend must not evict a healthy new one. The
        dropped backend stays tracked until idle, so shutdown still drains it.
        """
        sel = self._selection
        if sel is None or warden is not sel.warden or sel.verdict == "none":
            return
        if result.isolation == "none":
            log.warning("selection: the selected backend lost its isolation; re-checking on "
                        "the next call")
        elif sel.verdict == "native" and result.isolation == "native-degraded":
            log.warning("selection: a native run came back degraded (%s); re-checking on the "
                        "next call", result.isolation_detail)
        else:
            return
        self._selection = None
        self._prune()

    def cleanup_job(self) -> CleanupJob:
        """Snapshot for the POSIX shutdown hand-off (S1-D5): every tracked backend's
        names, owner token, temp dirs and live CLIs, grouped by runtime. Dropped and
        candidate backends are tracked too (S7-D1), so a probe or a run on a backend that
        was replaced mid-session is included. Synchronous: nothing may change before the
        server's os._exit."""
        job = CleanupJob()
        by_runtime: dict[str, dict[str, list[str]]] = {}
        for b in self._backends:
            names, tmpdirs, procs = b.cleanup_snapshot()
            rt = by_runtime.setdefault(b.runtime, {"argv": [b.runtime], "owners": [], "names": []})
            rt["owners"].append(b.owner)
            rt["names"].extend(names)
            job.tmpdirs.extend(tmpdirs)
            job.cli_procs.extend(procs)
        job.runtimes = list(by_runtime.values())
        # The dev-only naive backend has no containers, but its guests are plain child
        # processes that would outlive the server's exit: kill them with the CLIs.
        if self._selection is not None and isinstance(self._selection.warden, NaiveBackend):
            job.guests.extend(self._selection.warden.live_procs())
        return job

    async def aclose(
        self, rm_budget_s: float = _SHUTDOWN_RM_S, wait_budget_s: float = _SHUTDOWN_WAIT_S
    ) -> bool:
        """Shutdown on Windows (S7-D1); True when no cleanup task is left pending.

        First one batched `rm -f` per runtime for every container any tracked backend
        may still own — runs cancelled by the client's disconnect included — then a
        short wait that never cancels the cleanup tasks it waits on. A False return
        means the caller must not hand those tasks to asyncio's cancel-all.
        """
        backends = list(self._backends)
        by_runtime: dict[str, tuple[ContainerBackend, list[str]]] = {}
        for b in backends:
            entry = by_runtime.setdefault(b.runtime, (b, []))
            entry[1].extend(b.active_names())
        for b, names in by_runtime.values():
            if names:
                log.info("shutdown: removing %d container(s)", len(names))
                try:
                    await b.remove_now(names, rm_budget_s)
                except Exception:  # shutdown is best-effort; the self-deadline backstops
                    log.exception("shutdown: batched removal failed")
        for b in backends:
            b.begin_close()

        loop = asyncio.get_running_loop()
        deadline = loop.time() + wait_budget_s
        while True:
            tasks = [t for b in backends for t in b.pending_tasks()]
            # A run still in flight (not yet unwound, or carried past a swallowed
            # cancel) may yet spawn a container and its finisher: never "clean".
            inflight = sum(b.inflight for b in backends)
            remaining = deadline - loop.time()
            if (not tasks and not inflight) or remaining <= 0:
                break
            if tasks:
                await asyncio.wait(tasks, timeout=min(remaining, 0.05))  # never cancels
            else:
                await asyncio.sleep(min(remaining, 0.05))
        if tasks or inflight:
            log.warning("shutdown: %d cleanup task(s) and %d run(s) still pending",
                        len(tasks), inflight)
        return not tasks and not inflight

    def _new_backend(
        self, runtime: str, soft_disabled: frozenset[str] = frozenset()
    ) -> ContainerBackend:
        """A ContainerBackend on the shared budget, tracked from birth (S7-D1)."""
        self._prune()
        backend = ContainerBackend(runtime, soft_disabled=soft_disabled, slots=self._slots)
        self._backends.append(backend)
        return backend

    def _prune(self) -> None:
        live = self._selection.warden if self._selection is not None else None
        self._backends = [b for b in self._backends if b is live or not b.idle()]

    async def _select(self, limits: Limits) -> Selection:
        if limits.backend == "naive":
            log.warning("VESTIBULE_BACKEND=naive: NO isolation (dev-only opt-in)")
            return Selection(NaiveBackend(), "none", "explicitly configured, dev-only")
        if limits.backend not in _BACKENDS:
            raise _RestartRequired(
                f"unknown VESTIBULE_BACKEND {limits.backend!r}; use 'auto', 'native', "
                "'container' or 'naive'. " + _RESTART_HINT
            )
        on_linux = sys.platform == "linux"
        if limits.backend == "native":
            if not on_linux:
                raise _RestartRequired(
                    "VESTIBULE_BACKEND=native: native isolation needs Linux; use 'auto' or "
                    "'container' on this system. " + _RESTART_HINT
                )
            try:
                return await self._try_native(limits)
            except RunRefusedError as e:
                raise type(e)(f"native isolation is unavailable: {e}") from None
        if limits.runtime not in _RUNTIME_ORDER:  # S6-D3: before any probe, so it never hides
            raise _RestartRequired(
                f"unknown VESTIBULE_RUNTIME {limits.runtime!r}; "
                "use 'auto', 'docker', or 'podman'. " + _RESTART_HINT
            )
        if limits.backend == "container" or not on_linux:
            return await self._select_container(limits)
        return await self._select_auto_linux(limits)

    async def _select_auto_linux(self, limits: Limits) -> Selection:
        """N5's order (S6-D3): native > container > native-degraded > container-degraded."""
        native: Selection | None = None
        native_err: RunRefusedError | None = None
        try:
            native = await self._try_native(limits)
        except RunRefusedError as e:
            native_err = e
            log.warning("selection: native isolation unavailable: %s", e)
        if native is not None and native.verdict == "native":
            return native
        try:
            # With native-degraded in hand, the soft-off retry can only produce a
            # container-degraded, which ranks below it: skip it.
            container = await self._select_container(limits, degraded_retry=native is None)
        except RunRefusedError as e:
            if native is None:
                assert native_err is not None
                msg = (f"no isolating backend is available — native: {native_err}; "
                       f"container: {e}")
                both_final = (isinstance(native_err, _RestartRequired)
                              and isinstance(e, _RestartRequired))
                raise (_RestartRequired if both_final else RunRefusedError)(msg) from None
            if not isinstance(e, (_RestartRequired, _ProbeFailed)):  # S6-D6: may clear up
                native.recheck_at = time.monotonic() + _RETRY_COOLDOWN_S
            log.warning("selection: container unavailable (%s); using native-degraded%s", e,
                        "" if native.recheck_at is None
                        else f", re-checked after {_RETRY_COOLDOWN_S} s")
            return native
        if native is None or container.verdict == "container":
            if native is not None:  # native-degraded loses to a full container
                await native.warden.drain(_CANDIDATE_DRAIN_S)
            return container
        await container.warden.drain(_CANDIDATE_DRAIN_S)  # container-degraded loses
        return native

    async def _recheck_container(self, limits: Limits, native: Selection) -> Selection:
        """S6-D6: a provisional native-degraded selection asks the container again, and only
        the container. Native keeps serving and is never probed again: that probe would wait
        for a slot its own runs hold, and a failure there would refuse every call."""
        assert self._slots is not None
        if self._slots.locked():  # every slot busy: never compete with real runs
            native.recheck_at = time.monotonic() + _RETRY_COOLDOWN_S
            return native
        log.info("selection: asking the container again (native-degraded is provisional)")
        try:
            container = await self._select_container(limits, degraded_retry=False)
        except (_RestartRequired, _ProbeFailed) as e:  # it won't clear up on its own: final
            log.info("selection: the container can't be used (%s); native-degraded stays", e)
            native.recheck_at = None
            return native
        except RunRefusedError:  # the daemon not up yet, the image not pulled: ask again later
            native.recheck_at = time.monotonic() + _RETRY_COOLDOWN_S
            return native
        if container.verdict == "container":
            log.warning("selection: the container backend is up; switching from native-degraded")
            self._selection = container
            self._prune()
            return container
        await container.warden.drain(_CANDIDATE_DRAIN_S)  # container-degraded ranks lower
        native.recheck_at = None
        return native

    async def _try_native(self, limits: Limits) -> Selection:
        """S6-D4: native's pre-checks, cheapest first, then its probe (S6-D1). A Selection,
        or a refusal whose message is the reason; restart-required when only a config change
        can fix it."""
        try:
            for var, ref in (("VESTIBULE_IMAGE_PYTHON", limits.image_python),
                             ("VESTIBULE_IMAGE_NODE", limits.image_node)):
                try:
                    registry.parse_ref(ref)
                except registry.RegistryError as e:  # S2-D4: native needs a digest
                    raise _RestartRequired(f"{var}: {e}") from None
            overlap = rootfs.overlap_problem(limits)  # S2-D9; setup refuses it too
            if overlap:
                raise _RestartRequired(overlap)
            if any(rootfs.find(limits.rootfs_path, ref) is None
                   for ref in (limits.image_python, limits.image_node)):
                raise RunRefusedError(
                    f"no native rootfs in {limits.rootfs_path}; run "
                    f"`{rootfs.setup_command(limits)}` once (downloads about 123 MB), then "
                    "restart Vestibule if it already chose another backend"
                )
        except RunRefusedError:
            raise
        except Exception as e:  # user-owned files and settings: a reason, never a crash
            raise RunRefusedError(f"the native checks failed: {e}") from None
        backend = NativeBackend(slots=self._slots)
        t0 = time.monotonic()
        try:
            r = await backend.run("bash", _probe_script(limits), _PROBE_TIMEOUT_S, limits)
        except asyncio.CancelledError:
            raise  # the run's own finisher stops the helper (step 3)
        except BaseException:
            await backend.drain(_CANDIDATE_DRAIN_S)
            raise
        ok = (r.exit_code == 0 and not r.timed_out and _PROBE_MARKER in r.stdout
              and r.isolation in ("native", "native-degraded"))
        log.info("selection: native probe %s in %.0f ms (%s)",  # step 4's placement time
                 r.isolation if ok else "failed", (time.monotonic() - t0) * 1000,
                 r.isolation_detail or "no detail")
        if ok:
            if r.isolation == "native-degraded":
                log.warning("selection: native is degraded (%s)", r.isolation_detail)
            return Selection(backend, r.isolation, r.isolation_detail)
        await backend.drain(_CANDIDATE_DRAIN_S)
        raise RunRefusedError(_native_reason(r, limits, _read_sysctls()))

    async def _select_container(
        self, limits: Limits, degraded_retry: bool = True
    ) -> Selection:
        """M1's container selection (M1 step 5, S7-D3), unchanged but for `degraded_retry`."""
        _warn_unpinned(limits)
        runtime = await self._resolve_runtime(limits)

        # Test-drive the FULL §3 profile through the normal run path — probe = real
        # run, image preflight included — so the probe can never drift from what a
        # real run gets. Success is the only thing that earns `container` (D3).
        # Every rejected candidate is drain()ed: its detached cleanup tasks must
        # not outlive selection, or event-loop shutdown can deadlock on them. On
        # cancellation (client cancel or disconnect) nothing is awaited here: the
        # candidate is already tracked, and aclose() reaches it at shutdown (S7-D1).
        backend = self._new_backend(runtime)
        try:
            failure = await self._probe(backend, limits)
        except asyncio.CancelledError:
            raise
        except BaseException:
            await backend.drain(_CANDIDATE_DRAIN_S)
            raise
        if failure is None:
            # `container` only if the readback verified every soft limit (S7-D3).
            verdict, detail = backend.isolation_status()
            if detail:
                log.warning("selection: running DEGRADED — %s", detail)
            return Selection(backend, verdict, detail)
        await backend.drain(_CANDIDATE_DRAIN_S)
        if not degraded_retry:
            raise _ProbeFailed(f"the full container profile failed its probe ({failure})")

        # S5-D2: one retry with every soft limit off. Hard controls are never
        # stripped, so a hard-tier failure fails this retry too and blocks.
        log.warning("selection: full-profile probe failed (%s); retrying without "
                    "soft limits", failure)
        degraded = self._new_backend(runtime, soft_disabled=frozenset(SOFT_CONTROLS))
        try:
            second = await self._probe(degraded, limits)
        except asyncio.CancelledError:
            raise
        except BaseException:
            await degraded.drain(_CANDIDATE_DRAIN_S)
            raise
        if second is None:
            verdict, detail = degraded.isolation_status()
            log.warning("selection: running DEGRADED — %s", detail)
            return Selection(degraded, verdict, detail)
        await degraded.drain(_CANDIDATE_DRAIN_S)
        if "/workspace" in second:
            # The guest owns the workspace (D1): a run can leave it unusable, e.g. by
            # chmod-ing its root on a Linux host where the guest runs as your uid. That
            # is a fixable folder problem, not a runtime that can't isolate.
            raise RunRefusedError(_workspace_refusal(second, limits))
        # Neutral on cause: a hard-tier failure and an override image that cannot run
        # the probe look identical from here; the parenthetical says which.
        raise RunRefusedError(
            f"the isolation probe failed even with resource limits off ({second}): "
            "either the container runtime cannot enforce the isolation profile, or the "
            "configured image cannot run the probe (it needs bash and coreutils "
            "`timeout`); refusing to run without isolation"
        )

    async def _resolve_runtime(self, limits: Limits) -> str:
        order = _RUNTIME_ORDER[limits.runtime]  # _select checked it first
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
        r = await backend.run("bash", _probe_script(limits), _PROBE_TIMEOUT_S, limits)
        warnings = [ln.strip() for ln in r.stderr.splitlines() if _RUNTIME_WARNING.match(ln)]
        for w in warnings:  # misconfiguration is never silent, whatever the verdict
            log.warning("probe: runtime warning: %s", w[:300])
        ok = (
            r.exit_code == 0
            and not r.timed_out
            and _PROBE_MARKER in r.stdout
            and r.isolation != "none"
        )
        if ok:
            # S7-D3: a passing probe still earns plain `container` only if every soft
            # limit this backend applies is verifiably in force inside the container.
            backend.set_limits_note(
                _limits_note(r.stdout, warnings, backend._soft_disabled, limits)
            )
            return None
        # Generous: runc's "exec: \"timeout\": not found" lands past char 200.
        first_err = (r.stderr.strip().splitlines() or ["no error output"])[0][:400]
        return f"probe exit {r.exit_code}, timed_out={r.timed_out}: {first_err}"


def _probe_script(limits: Limits) -> str:
    """The workspace round-trip, its file name fresh per probe (M1 step 7 plan §4.1)."""
    probe_file = f"/workspace/.vestibule-probe-{secrets.token_hex(6)}"
    return (_PROBE_RO if limits.workspace_ro else _PROBE_RW).replace(_PROBE_FILE, probe_file)


def _workspace_refusal(reason: str, limits: Limits) -> str:
    return (
        f"the sandbox could not use the workspace ({reason}). Check that "
        f"{limits.workspace_path} exists and that you can read and write it "
        "(code run earlier may have changed its permissions), then retry"
    )


def _native_reason(r: RunResult, limits: Limits, sysctls: dict[str, str]) -> str:
    """S6-D4: why a native probe failed, with a hint naming the fix where there is one."""
    if r.isolation == "none":  # the sandbox itself failed
        reason = r.isolation_detail or "the native sandbox gave no reason"
    else:  # the sandbox was fine and the guest failed: say so, as M1's probe does
        first = (r.stderr.strip().splitlines() or ["no error output"])[0][:400]
        reason = (f"probe exit {r.exit_code}, timed out {'yes' if r.timed_out else 'no'}: "
                  f"{first}")
        if "/workspace" in first:
            return _workspace_refusal(reason, limits)
    hint = _native_hint(reason, sysctls)
    return f"{reason}. {hint}" if hint else reason


def _native_hint(reason: str, sysctls: dict[str, str]) -> str | None:
    """The fix for a known cause (S6-D4). Evidence in the reason itself comes first. The two
    sysctls only make a cause likely, so they explain an actual setup failure, at any stage:
    stock Ubuntu fails the helper's own id-map write, not a mount."""
    if "libseccomp" in reason:
        return "install libseccomp2 (apt install libseccomp2); native isolation requires it"
    if "failed at mounts" in reason and "/proc: " in reason and "not permitted" in reason:
        return ("the server runs where /proc isn't fully visible (inside a container, or "
                "under systemd's ProtectProc=), so native can't build its sandbox here")
    if "setup failed at" not in reason:
        return None
    if sysctls.get("apparmor_restrict_unprivileged_userns") == "1":
        return ("Ubuntu restricts unprivileged user namespaces on this host, the likely cause; "
                "lifting that needs root, so use the container backend")
    if sysctls.get("max_user_namespaces") == "0":
        return "user namespaces are disabled on this host (user.max_user_namespaces=0)"
    return None


def _read_sysctls() -> dict[str, str]:
    out = {}
    for name, path in _SYSCTLS.items():
        try:
            with open(path) as f:
                out[name] = f.read().strip()
        except OSError:  # absent on this kernel, or not Linux
            pass
    return out


def _limits_note(
    stdout: str, warnings: list[str], soft_disabled: frozenset[str], limits: Limits
) -> str | None:
    """S7-D3: None if every enabled soft limit is verified, else the degraded detail.

    cgroup v2: compare the container's own cgroup files with what was requested.
    cgroup v1 (no readback): the runtime's own warnings are the only signal. No
    cgroupfs visible at all: nothing is verifiable, so never plain `container`. A
    mismatch only ever underclaims — the safe direction for golden rule 5.
    """
    line = next((ln for ln in stdout.splitlines() if ln.startswith(_LIMITS_TAG)), None)
    if line is None:
        return "limits not verified: the probe could not read them back"
    fields = dict(kv.split("=", 1) for kv in line.split()[1:] if "=" in kv)
    if fields.get("cgroup") == "v1":
        return f"runtime warning: {warnings[0][:200]}" if warnings else None
    if fields.get("cgroup") != "v2":  # no cgroupfs visible: nothing can be verified
        return "limits not verified: the container cannot see its cgroup"

    def got(*keys: str) -> str:
        return ", ".join(f"{k}={fields.get(k) or '?'}" for k in keys)

    bad: list[str] = []
    if "memory" not in soft_disabled and (
        fields.get("memory.max") != str(limits.mem_mb * 1024 * 1024)
        or fields.get("memory.swap.max") != "0"  # --memory-swap == --memory: no swap
    ):
        bad.append(f"memory ({got('memory.max', 'memory.swap.max')})")
    if "pids" not in soft_disabled and fields.get("pids.max") != str(limits.pids_max):
        bad.append(f"pids ({got('pids.max')})")
    if "cpu" not in soft_disabled and not _cpu_matches(fields.get("cpu.max", ""), limits):
        bad.append(f"cpu ({got('cpu.max')})")
    if "tmpfs-size" not in soft_disabled and not _tmp_matches(fields.get("tmp", ""), limits):
        bad.append(f"tmpfs-size ({got('tmp')})")
    return f"limits not verified: {'; '.join(bad)}" if bad else None


def _cpu_matches(cpu_max: str, limits: Limits) -> bool:
    """`cpu.max` is "<quota>,<period>" (space turned to comma); quota/period = --cpus."""
    quota, _, period = cpu_max.partition(",")
    try:
        return abs(int(quota) / int(period) - limits.cpu_pct / 100) < 0.005
    except ValueError:  # "max" (no limit) or unreadable
        return False


def _tmp_matches(tmp: str, limits: Limits) -> bool:
    """`/tmp` size from `stat -f` ("<blocks>,<block size>"): non-zero and within the cap."""
    blocks, _, bsize = tmp.partition(",")
    try:
        size = int(blocks) * int(bsize)
    except ValueError:
        return False
    return 0 < size <= limits.tmpfs_mb * 1024 * 1024


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
        async with asyncio.timeout(_CLI_WAIT_S):
            _out, err = await proc.communicate()
    except TimeoutError:
        proc.kill()
        with contextlib.suppress(Exception):
            await proc.wait()
        return "not responding"
    except asyncio.CancelledError:  # never leave the CLI running unowned
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        raise
    if proc.returncode != 0:
        first = (err.decode(errors="replace").strip().splitlines() or ["unknown error"])[0]
        return f"daemon not reachable ({first[:200]})"
    return None
