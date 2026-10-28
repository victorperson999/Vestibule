"""M1 step 5 selection tests — no Docker needed (plan §6, Docker-free half).

The `<runtime> version` check and the probe run are monkeypatched; the real
end-to-end selection lives in tests/test_container.py (Docker-marked).
Step 6 adds the S6-D2 un-pinned-image warning and the §4.3 retry-vs-restart shapes.
Step 7a adds S7-D1: tracked backends, one slot budget, note_result identity, and
shutdown/cancellation behavior — cancellation delivered through an anyio task
group, exactly as the MCP SDK delivers it, never only plain task.cancel().
"""
import asyncio
import logging
import re
import time
import types

import anyio
import pytest

import vestibule.backends.select as select_mod
from vestibule.backends.base import RunRefusedError, RunResult, Warden
from vestibule.backends.container import SOFT_CONTROLS, ContainerBackend
from vestibule.backends.naive import NaiveBackend
from vestibule.backends.select import BackendSelector


def _limits(**kw):
    """M1's container selection: on a Linux host with a native rootfs, `auto` now probes
    native first (M2 step 6, S6-D8), so these tests pin `container` unless they say otherwise."""
    from vestibule.config import Limits

    kw.setdefault("backend", "container")
    return Limits(**kw)


@pytest.fixture()
def docker_ok(monkeypatch):
    """`docker version` succeeds; podman does not exist."""
    calls: list[str] = []

    async def vc(runtime):
        calls.append(runtime)
        return None if runtime == "docker" else "not installed"

    monkeypatch.setattr(select_mod, "_version_check", vc)
    return calls


def _probe_that(outcome_for_full, outcome_for_degraded="unused"):
    """A fake BackendSelector._probe keyed on the candidate backend's soft set."""
    entered: list[frozenset] = []

    async def probe(self, backend, limits):
        entered.append(backend._soft_disabled)
        return outcome_for_degraded if backend._soft_disabled else outcome_for_full

    return probe, entered


async def test_naive_override_skips_probing(monkeypatch):
    async def must_not_check(runtime):  # pragma: no cover - the assertion IS the test
        raise AssertionError("naive override must not touch any runtime")

    monkeypatch.setattr(select_mod, "_version_check", must_not_check)
    sel = await BackendSelector().get(_limits(backend="naive"))
    assert isinstance(sel.warden, NaiveBackend)
    assert sel.verdict == "none"


async def test_unknown_backend_value_refused():
    with pytest.raises(RunRefusedError, match="VESTIBULE_BACKEND"):
        await BackendSelector().get(_limits(backend="containr"))


async def test_unknown_runtime_value_refused():
    with pytest.raises(RunRefusedError, match="VESTIBULE_RUNTIME"):
        await BackendSelector().get(_limits(runtime="dokcer"))


async def test_auto_prefers_docker(docker_ok, monkeypatch):
    probe, entered = _probe_that(None)
    monkeypatch.setattr(BackendSelector, "_probe", probe)
    sel = await BackendSelector().get(_limits())
    assert sel.verdict == "container"
    assert isinstance(sel.warden, ContainerBackend)
    assert sel.warden._runtime == "docker"
    assert docker_ok == ["docker"]  # podman never consulted once docker answered


async def test_auto_falls_back_to_podman(monkeypatch):
    async def vc(runtime):
        return "not installed" if runtime == "docker" else None

    monkeypatch.setattr(select_mod, "_version_check", vc)
    probe, _ = _probe_that(None)
    monkeypatch.setattr(BackendSelector, "_probe", probe)
    sel = await BackendSelector().get(_limits())
    assert sel.warden._runtime == "podman"


async def test_forced_runtime_tries_only_itself(monkeypatch):
    calls: list[str] = []

    async def vc(runtime):
        calls.append(runtime)
        return "not installed"

    monkeypatch.setattr(select_mod, "_version_check", vc)
    with pytest.raises(RunRefusedError, match="no usable container runtime"):
        await BackendSelector().get(_limits(runtime="podman"))
    assert calls == ["podman"]


async def test_daemon_down_is_cached_then_rechecked_after_cooldown(monkeypatch):
    calls: list[str] = []

    async def vc(runtime):
        calls.append(runtime)
        return "daemon not reachable (boom) — is Docker Desktop running?"

    monkeypatch.setattr(select_mod, "_version_check", vc)
    s = BackendSelector()

    with pytest.raises(RunRefusedError, match="Docker Desktop"):
        await s.get(_limits())
    checked = len(calls)

    # Within the cooldown: same refusal, no new CLI churn (S5-D1).
    with pytest.raises(RunRefusedError, match="Docker Desktop"):
        await s.get(_limits())
    assert len(calls) == checked

    # Past the cooldown: the next call re-checks.
    s._failed_at = time.monotonic() - select_mod._RETRY_COOLDOWN_S - 1
    with pytest.raises(RunRefusedError):
        await s.get(_limits())
    assert len(calls) > checked


async def test_degraded_fallback_drops_all_soft_limits(docker_ok, monkeypatch):
    probe, entered = _probe_that("probe exit 125: boom", None)
    monkeypatch.setattr(BackendSelector, "_probe", probe)
    sel = await BackendSelector().get(_limits())
    assert sel.verdict == "container-degraded"
    assert sel.detail == "limits not applied: cpu, memory, pids, tmpfs-size"
    assert sel.warden._soft_disabled == frozenset(SOFT_CONTROLS)
    assert entered == [frozenset(), frozenset(SOFT_CONTROLS)]  # full first, then bare


async def test_both_probes_failing_blocks_hard(docker_ok, monkeypatch):
    probe, _ = _probe_that("probe exit 1: no cap-drop", "probe exit 1: no cap-drop")
    monkeypatch.setattr(BackendSelector, "_probe", probe)
    with pytest.raises(RunRefusedError, match="cannot enforce the isolation profile") as ei:
        await BackendSelector().get(_limits())
    # Step 6 §4.3: neutral on cause — a bash-less override image fails the same way.
    assert "image cannot run the probe (it needs bash and coreutils `timeout`)" in str(ei.value)
    assert "probe exit 1: no cap-drop" in str(ei.value)  # the actual reason still shown


async def test_concurrent_first_calls_share_one_probe(docker_ok, monkeypatch):
    entered = 0
    gate = asyncio.Event()

    async def slow_probe(self, backend, limits):
        nonlocal entered
        entered += 1
        await gate.wait()
        return None

    monkeypatch.setattr(BackendSelector, "_probe", slow_probe)
    s = BackendSelector()
    t1 = asyncio.create_task(s.get(_limits()))
    t2 = asyncio.create_task(s.get(_limits()))
    await asyncio.sleep(0.05)
    gate.set()
    sel1, sel2 = await asyncio.gather(t1, t2)
    assert entered == 1
    assert sel1 is sel2


async def test_note_result_invalidates_on_lost_runtime(docker_ok, monkeypatch):
    probe, entered = _probe_that(None)
    monkeypatch.setattr(BackendSelector, "_probe", probe)
    s = BackendSelector()
    sel = await s.get(_limits())
    assert len(entered) == 1

    # A run that reported honest no-isolation (S4-D2 / exit-125) drops the cache...
    s.note_result(sel.warden, RunResult(stdout="", stderr="", exit_code=125,
                                        timed_out=False, isolation="none"))
    sel = await s.get(_limits())
    assert len(entered) == 2  # ...so the next call re-probed

    # A normal container result never invalidates.
    s.note_result(sel.warden, RunResult(stdout="", stderr="", exit_code=0,
                                        timed_out=False, isolation="container"))
    await s.get(_limits())
    assert len(entered) == 2


async def test_note_result_ignores_naive_selection():
    s = BackendSelector()
    sel = await s.get(_limits(backend="naive"))
    s.note_result(sel.warden, RunResult(stdout="", stderr="", exit_code=0, timed_out=False,
                                        isolation="none"))
    assert s._selection is not None  # naive legitimately reports none; keep it


async def test_probe_checks_marker_and_isolation():
    """The probe accepts only exit 0 + marker + real container isolation."""
    s = BackendSelector()

    class FakeBackend:
        _soft_disabled = frozenset()

        def __init__(self, result):
            self._result = result

        async def run(self, *args):
            return self._result

        def set_limits_note(self, note):
            self.limits_note = note

    ok = RunResult(stdout=f"{select_mod._PROBE_MARKER}\n", stderr="", exit_code=0,
                   timed_out=False, isolation="container")
    assert await s._probe(FakeBackend(ok), _limits()) is None

    for bad in (
        RunResult(stdout="nope", stderr="", exit_code=0, timed_out=False,
                  isolation="container"),
        RunResult(stdout=f"{select_mod._PROBE_MARKER}\n", stderr="x", exit_code=1,
                  timed_out=False, isolation="container"),
        RunResult(stdout=f"{select_mod._PROBE_MARKER}\n", stderr="", exit_code=0,
                  timed_out=True, isolation="container"),
        # started-nothing paths (S4-D2 / exit-125) must never pass the probe
        RunResult(stdout=f"{select_mod._PROBE_MARKER}\n", stderr="", exit_code=0,
                  timed_out=False, isolation="none"),
    ):
        assert await s._probe(FakeBackend(bad), _limits()) is not None


# ----------------------------------------- step 6: pin warning (S6-D2) + §4.3 shapes


async def test_unpinned_override_warns_for_both_refs(docker_ok, monkeypatch, caplog):
    """S6-D2: an un-pinned override is allowed but never silent — and the node ref is
    checked too, although selection only probes the python image."""
    probe, _ = _probe_that(None)
    monkeypatch.setattr(BackendSelector, "_probe", probe)
    with caplog.at_level(logging.WARNING, logger="vestibule.select"):
        sel = await BackendSelector().get(
            _limits(image_python="my-python:latest", image_node="my-node:22")
        )
    assert sel.verdict == "container"  # warned, never refused
    assert "my-python:latest" in caplog.text and "my-node:22" in caplog.text
    assert caplog.text.count("not digest-pinned") == 2


async def test_pinned_defaults_do_not_warn(docker_ok, monkeypatch, caplog):
    probe, _ = _probe_that(None)
    monkeypatch.setattr(BackendSelector, "_probe", probe)
    with caplog.at_level(logging.WARNING, logger="vestibule.select"):
        await BackendSelector().get(_limits())
    assert "not digest-pinned" not in caplog.text


async def test_naive_skips_pin_check(caplog):
    with caplog.at_level(logging.WARNING, logger="vestibule.select"):
        await BackendSelector().get(_limits(backend="naive", image_python="x:latest"))
    assert "not digest-pinned" not in caplog.text


@pytest.mark.parametrize("kw", [{"backend": "containr"}, {"runtime": "dokcer"}])
async def test_config_refusals_say_restart_not_retry(kw):
    """Config is read once at startup, so 'retry' would loop forever; say restart."""
    with pytest.raises(RunRefusedError) as ei:
        await BackendSelector().get(_limits(**kw))
    msg = str(ei.value)
    assert "restart the server" in msg
    assert "retry" not in msg.lower()


async def test_runtime_not_installed_says_install_and_restart_agent(monkeypatch):
    """A running server keeps the PATH it started with: a fresh install is invisible
    until the agent that launched it restarts."""
    async def vc(runtime):
        return "not installed"

    monkeypatch.setattr(select_mod, "_version_check", vc)
    with pytest.raises(RunRefusedError) as ei:
        await BackendSelector().get(_limits())
    msg = str(ei.value)
    assert "Install Docker" in msg and "restart your agent" in msg
    # "not installed" = not on this process's PATH, which also covers an existing
    # install the agent's PATH lacks, or a mis-set VESTIBULE_RUNTIME.
    assert "PATH" in msg and "VESTIBULE_RUNTIME" in msg


async def test_daemon_down_says_start_then_retry(monkeypatch):
    """Mixed causes (docker installed but down, podman absent): starting the daemon is
    the fix, and a retry does work (after the cooldown)."""
    async def vc(runtime):
        return "not installed" if runtime == "podman" else "daemon not reachable (x)"

    monkeypatch.setattr(select_mod, "_version_check", vc)
    with pytest.raises(RunRefusedError) as ei:
        await BackendSelector().get(_limits())
    msg = str(ei.value)
    assert "Start docker (Docker Desktop on Windows/macOS), then retry" in msg
    assert "restart your agent" not in msg


async def test_daemon_down_hint_is_runtime_specific(monkeypatch):
    """Critic fix: a Podman user is never pointed at Docker Desktop."""
    async def vc(runtime):
        return "daemon not reachable (cannot connect to Podman)"

    monkeypatch.setattr(select_mod, "_version_check", vc)
    with pytest.raises(RunRefusedError) as ei:
        await BackendSelector().get(_limits(runtime="podman"))
    msg = str(ei.value)
    assert "Start podman (`podman machine start` on Windows/macOS), then retry" in msg
    assert "Docker Desktop" not in msg


@pytest.mark.parametrize("case", ["backend", "runtime", "not-installed"])
async def test_cached_restart_refusal_never_suggests_waiting(case, monkeypatch):
    """Critic fix: a refusal only a restart can fix keeps saying restart when served
    from the cooldown cache, and never adds 'next check in N s' (waiting can't help)."""
    kw: dict[str, str] = {}
    if case == "backend":
        kw = {"backend": "containr"}
    elif case == "runtime":
        kw = {"runtime": "dokcer"}
    else:
        async def vc(runtime):
            return "not installed"

        monkeypatch.setattr(select_mod, "_version_check", vc)

    s = BackendSelector()
    with pytest.raises(RunRefusedError) as first:
        await s.get(_limits(**kw))
    with pytest.raises(RunRefusedError) as cached:
        await s.get(_limits(**kw))
    assert str(cached.value) == str(first.value)  # served from cache, unannotated
    assert "restart" in str(cached.value)
    assert "re-checked" not in str(cached.value)


async def test_cached_refusal_says_when_it_rechecks(monkeypatch):
    """S5-D1 cooldown: a fix made inside the window gets the same refusal, so the
    cached one must say it is cached and when the next real check happens."""
    async def vc(runtime):
        return "daemon not reachable (x)"

    monkeypatch.setattr(select_mod, "_version_check", vc)
    s = BackendSelector()
    with pytest.raises(RunRefusedError) as first:
        await s.get(_limits())
    assert "re-checked" not in str(first.value)  # a fresh verdict carries no annotation

    with pytest.raises(RunRefusedError) as cached:
        await s.get(_limits())
    msg = str(cached.value)
    assert msg.startswith(str(first.value))
    cooldown = select_mod._RETRY_COOLDOWN_S
    assert re.search(rf"re-checked at most every {cooldown} s; next check in \d+ s", msg)


# ------------------------------------------------------------- server rendering


async def test_server_renders_selection_refusal_as_blocked(monkeypatch):
    """Criterion 12: no runtime => actionable Blocked message, never a silent
    naive fallback, never an exception."""
    from vestibule import server

    async def refusing_get_warden():
        raise RunRefusedError(
            "no usable container runtime (docker: not installed). "
            "Start Docker Desktop (or install Docker/Podman), then retry."
        )

    monkeypatch.setattr(server, "get_warden", refusing_get_warden)
    out = await server._handle_run_code({"language": "python", "code": "print(1)"})
    assert out[0].text.startswith("Blocked: no usable container runtime")
    assert "Docker" in out[0].text


async def test_server_renders_degraded_isolation(monkeypatch):
    """Criterion 11: a degraded run is rendered with the exact missing limits."""
    from vestibule import server

    class DegradedWarden:
        async def run(self, *args, **kwargs):
            return RunResult(
                stdout="hi", stderr="", exit_code=0, timed_out=False,
                isolation="container-degraded",
                isolation_detail="limits not applied: cpu, memory, pids, tmpfs-size",
            )

    async def fake_get_warden():
        return DegradedWarden()

    monkeypatch.setattr(server, "get_warden", fake_get_warden)
    out = await server._handle_run_code({"language": "python", "code": "print(1)"})
    assert "isolation: container-degraded (limits not applied: cpu, memory, pids, " \
           "tmpfs-size)" in out[0].text


# ------------------------------------------------- S7-D1: lifecycle across selections


def _none_result():
    return RunResult(stdout="", stderr="", exit_code=125, timed_out=False, isolation="none")


def _track(backend, coro):
    """Register `coro` as one of `backend`'s detached cleanup tasks (like a finisher)."""
    task = asyncio.create_task(coro)
    backend._finishers.add(task)
    task.add_done_callback(backend._finishers.discard)
    return task


async def test_note_result_ignores_a_dropped_backends_late_result(docker_ok, monkeypatch):
    """A late `isolation: none` from an already-dropped backend must not evict the
    healthy selection that replaced it."""
    probe, entered = _probe_that(None)
    monkeypatch.setattr(BackendSelector, "_probe", probe)
    s = BackendSelector()
    old = await s.get(_limits())
    s.note_result(old.warden, _none_result())
    new = await s.get(_limits())
    assert new.warden is not old.warden and len(entered) == 2

    s.note_result(old.warden, _none_result())  # stale: ignored
    assert await s.get(_limits()) is new
    assert len(entered) == 2


async def test_reselection_shares_one_slot_budget(docker_ok, monkeypatch):
    """One semaphore per process: a dropped backend's still-running containers keep
    counting against max_concurrent after re-selection."""
    probe, _ = _probe_that(None)
    monkeypatch.setattr(BackendSelector, "_probe", probe)
    s = BackendSelector()
    first = await s.get(_limits(max_concurrent=3))
    s.note_result(first.warden, _none_result())
    second = await s.get(_limits(max_concurrent=3))
    assert first.warden._sem is second.warden._sem is s._slots
    assert s._slots._value == 3


async def test_degraded_candidates_share_the_budget_too(docker_ok, monkeypatch):
    probe, _ = _probe_that("full profile failed", None)
    monkeypatch.setattr(BackendSelector, "_probe", probe)
    s = BackendSelector()
    sel = await s.get(_limits())
    assert sel.verdict == "container-degraded"
    assert sel.warden._sem is s._slots


async def test_idle_backends_are_pruned_live_one_kept(docker_ok, monkeypatch):
    probe, _ = _probe_that(None)
    monkeypatch.setattr(BackendSelector, "_probe", probe)
    s = BackendSelector()
    sel = await s.get(_limits())
    assert s._backends == [sel.warden]
    s.note_result(sel.warden, _none_result())  # dropped and idle => forgotten
    assert s._backends == []


async def test_busy_dropped_backend_stays_tracked(docker_ok, monkeypatch):
    probe, _ = _probe_that(None)
    monkeypatch.setattr(BackendSelector, "_probe", probe)
    s = BackendSelector()
    sel = await s.get(_limits())
    gate = asyncio.Event()
    task = _track(sel.warden, gate.wait())
    s.note_result(sel.warden, _none_result())
    assert s._backends == [sel.warden]  # cleanup still pending => shutdown must reach it
    gate.set()
    await task


async def test_anyio_cancel_during_candidate_drain_spares_its_cleanup(docker_ok, monkeypatch):
    """Skeptic finding 1 (verifier-confirmed): a client cancel or disconnect arrives
    through the SDK's anyio cancel scope while _select drains a rejected candidate.
    The drain must stop waiting WITHOUT cancelling the cleanup task — a finisher
    cancelled before its first step never releases its slot or removes its container."""
    finishers: list[asyncio.Task] = []
    finished = asyncio.Event()

    async def cleanup(slots):
        # Like ContainerBackend._finish_run: owns a slot, releases it when done.
        # Cancelled before its first step, this body never runs and the slot leaks.
        try:
            await asyncio.sleep(0.3)
            finished.set()
        finally:
            slots.release()

    async def failing_probe(self, backend, limits):
        await backend._sem.acquire()  # the probe run's slot, handed to its finisher
        finishers.append(_track(backend, cleanup(backend._sem)))
        return "probe exit 1"  # rejected => _select drains this candidate

    monkeypatch.setattr(BackendSelector, "_probe", failing_probe)
    s = BackendSelector()
    async with anyio.create_task_group() as tg:
        tg.start_soon(s.get, _limits(max_concurrent=2))
        await asyncio.sleep(0.05)  # get() is now inside the candidate's drain
        tg.cancel_scope.cancel()

    assert len(finishers) == 1
    await asyncio.wait_for(finished.wait(), 2)
    assert not finishers[0].cancelled()
    assert s._slots._value == 2  # the shared budget is whole again
    assert await s.aclose()


async def test_anyio_cancel_mid_probe_leaves_candidate_for_aclose(docker_ok, monkeypatch):
    """Shutdown during the first call's probe: the run is cancelled and leaves a
    container + a finisher behind. _select must not await anything on cancellation,
    and aclose() must still reach the candidate (it is tracked from birth)."""
    finished = asyncio.Event()
    removed: list[list[str]] = []

    async def cleanup(backend, name):
        await asyncio.sleep(0.05)
        backend._active.discard(name)
        finished.set()

    async def probe_cancelled_mid_run(self, backend, limits):
        backend._active.add("vestibule-probe-run")
        try:
            await asyncio.sleep(3600)
        finally:  # what run() does on cancellation: hand cleanup to a detached task
            _track(backend, cleanup(backend, "vestibule-probe-run"))

    async def fake_remove_now(self, names, timeout):
        removed.append(list(names))

    monkeypatch.setattr(BackendSelector, "_probe", probe_cancelled_mid_run)
    monkeypatch.setattr(ContainerBackend, "remove_now", fake_remove_now)
    s = BackendSelector()
    async with anyio.create_task_group() as tg:
        tg.start_soon(s.get, _limits())
        await asyncio.sleep(0.05)
        tg.cancel_scope.cancel()

    assert len(s._backends) == 1  # the candidate is still tracked
    assert await s.aclose()
    assert removed == [["vestibule-probe-run"]]
    assert finished.is_set()


async def test_aclose_batches_one_removal_across_live_and_dropped(docker_ok, monkeypatch):
    probe, _ = _probe_that(None)
    monkeypatch.setattr(BackendSelector, "_probe", probe)
    removed: list[list[str]] = []

    async def fake_remove_now(self, names, timeout):
        removed.append(list(names))

    monkeypatch.setattr(ContainerBackend, "remove_now", fake_remove_now)
    s = BackendSelector()
    old = await s.get(_limits())
    old.warden._active.add("vestibule-old")
    gate = asyncio.Event()
    task = _track(old.warden, gate.wait())  # keeps the dropped backend tracked
    s.note_result(old.warden, _none_result())
    new = await s.get(_limits())
    new.warden._active.add("vestibule-new")

    gate.set()
    assert await s.aclose()
    assert removed == [["vestibule-old", "vestibule-new"]]  # one call, both backends
    await task


async def test_aclose_reports_pending_without_cancelling(docker_ok, monkeypatch):
    """Over budget: aclose says so (the server then hard-exits) and leaves the task
    running — it never hands cleanup to a cancellation."""
    probe, _ = _probe_that(None)
    monkeypatch.setattr(BackendSelector, "_probe", probe)
    s = BackendSelector()
    sel = await s.get(_limits())
    slow = _track(sel.warden, asyncio.sleep(0.5))
    assert await s.aclose(rm_budget_s=0.1, wait_budget_s=0.05) is False
    assert not slow.done()
    await slow


async def test_aclose_with_nothing_selected_is_clean():
    assert await BackendSelector().aclose()


# ------------------------------------------------------------- server shutdown (S7-D1)


@pytest.mark.parametrize("aclose_outcome, expect_exit", [
    (True, False),       # clean: return normally into asyncio.run
    (False, True),       # tasks still pending: never hand them to cancel-all
    ("raise", True),     # cleanup itself failed: same
])
async def test_server_shutdown_hard_exits_only_when_cleanup_is_pending(
    aclose_outcome, expect_exit, monkeypatch
):
    from vestibule import server

    exits: list[int] = []

    class FakeSelector:
        async def aclose(self):
            if aclose_outcome == "raise":
                raise RuntimeError("boom")
            return aclose_outcome

    monkeypatch.setattr(server, "SELECTOR", FakeSelector())
    monkeypatch.setattr(server.os, "_exit", exits.append)
    monkeypatch.setattr(server, "_other_live_tasks", lambda: [])
    await server._shutdown()
    assert exits == ([0] if expect_exit else [])


# ------------------------------------- S7-D3 limits readback + §4.1 random probe name

_V2_OK = ("VESTIBULE-LIMITS cgroup=v2 memory.max=268435456 memory.swap.max=0 "
          "pids.max=128 cpu.max=75000,100000 tmp=16384,4096")


def _v2(**over):
    fields = dict(kv.split("=", 1) for kv in _V2_OK.split()[1:])
    fields.update({k.replace("_", "."): v for k, v in over.items()})
    return "VESTIBULE-LIMITS " + " ".join(f"{k}={v}" for k, v in fields.items())


_DOCKER_WARN = ("WARNING: Your kernel does not support swap limit capabilities or the "
                "cgroup is not mounted. Memory limited without swap.")
_PODMAN_LOGRUS = ('time="2026-10-11T12:00:00Z" level=warning msg="Resource limits are not '
                  'supported and ignored on cgroups V1 rootless systems"')
_PODMAN_TTY = "WARN[0000] Resource limits are not supported and ignored on cgroups V1"


@pytest.mark.parametrize("stdout, warnings, soft_off, expected", [
    (_V2_OK, [], frozenset(), None),
    (_v2(memory_max="max"), [], frozenset(),
     "limits not verified: memory (memory.max=max, memory.swap.max=0)"),
    (_v2(memory_swap_max=""), [], frozenset(),  # no swap accounting: swap NOT capped
     "limits not verified: memory (memory.max=268435456, memory.swap.max=?)"),
    (_v2(pids_max="max"), [], frozenset(), "limits not verified: pids (pids.max=max)"),
    (_v2(cpu_max="max,100000"), [], frozenset(),
     "limits not verified: cpu (cpu.max=max,100000)"),
    (_v2(tmp="2000000,4096"), [], frozenset(),  # ~8 GB: the size cap was dropped
     "limits not verified: tmpfs-size (tmp=2000000,4096)"),
    (_v2(pids_max="max", cpu_max="max,100000"), [], frozenset(),
     "limits not verified: pids (pids.max=max); cpu (cpu.max=max,100000)"),
    # a degraded backend never applied those flags: nothing to verify
    (_v2(memory_max="max", memory_swap_max="max", pids_max="max", cpu_max="max,100000",
         tmp="2000000,4096"), [], frozenset(SOFT_CONTROLS), None),
    # v2 readback wins: an unrelated warning does not degrade a verified profile
    (_V2_OK, ["WARNING: The requested image's platform does not match"], frozenset(), None),
    # cgroup v1: no readback, the runtime's own warning is the only signal
    ("VESTIBULE-LIMITS cgroup=v1", [_DOCKER_WARN], frozenset(),
     f"runtime warning: {_DOCKER_WARN[:200]}"),
    ("VESTIBULE-LIMITS cgroup=v1", [_PODMAN_LOGRUS], frozenset(),
     f"runtime warning: {_PODMAN_LOGRUS[:200]}"),
    ("VESTIBULE-LIMITS cgroup=v1", [], frozenset(), None),
    # no cgroupfs visible at all (7a critic round 2): unverifiable, never plain container
    ("VESTIBULE-LIMITS cgroup=none", [], frozenset(),
     "limits not verified: the container cannot see its cgroup"),
    ("no readback line at all", [], frozenset(),
     "limits not verified: the probe could not read them back"),
])
def test_limits_note(stdout, warnings, soft_off, expected):
    assert select_mod._limits_note(stdout, warnings, soft_off, _limits()) == expected


@pytest.mark.parametrize("line, is_warning", [
    (_DOCKER_WARN, True),
    (_PODMAN_LOGRUS, True),
    (_PODMAN_TTY, True),
    ("  WARNING: indented", True),
    ("VESTIBULE-PROBE-OK", False),
    ("cat: /x: no warning here", False),
])
def test_runtime_warning_formats(line, is_warning):
    """Skeptic finding 6: Podman logs through logrus, which the old
    startswith("WARNING") check never matched."""
    assert bool(select_mod._RUNTIME_WARNING.match(line)) is is_warning


async def test_probe_uses_a_fresh_random_file_name():
    s = BackendSelector()
    scripts: list[str] = []

    class FakeBackend:
        _soft_disabled = frozenset()

        async def run(self, language, script, *args):
            scripts.append(script)
            return RunResult(stdout=f"{_V2_OK}\n{select_mod._PROBE_MARKER}\n", stderr="",
                             exit_code=0, timed_out=False, isolation="container")

        def set_limits_note(self, note):
            pass

    for ro in (False, True, False):
        assert await s._probe(FakeBackend(), _limits(workspace_ro=ro)) is None
    names = [re.findall(r"/workspace/\.vestibule-probe-[0-9a-f]{12}\b", sc) for sc in scripts]
    assert all(n for n in names)
    assert len({n[0] for n in names}) == 3  # fresh per probe
    assert all("@PROBE_FILE@" not in sc for sc in scripts)
    assert all("/workspace/.vestibule-probe " not in sc for sc in scripts)


async def test_unverified_limit_makes_the_selection_degraded(docker_ok, monkeypatch):
    """S7-D3 end to end through _select: the probe passed, but the container's own
    cgroup shows no pids cap — so the verdict is container-degraded, and so is every
    run on that backend."""
    async def run(self, language, code, timeout_s, limits):
        return RunResult(stdout=f"{_v2(pids_max='max')}\n{select_mod._PROBE_MARKER}\n",
                         stderr="", exit_code=0, timed_out=False,
                         isolation=self.isolation_status()[0])

    monkeypatch.setattr(ContainerBackend, "run", run)
    sel = await BackendSelector().get(_limits())
    assert sel.verdict == "container-degraded"
    assert sel.detail == "limits not verified: pids (pids.max=max)"
    assert sel.warden.isolation_status() == ("container-degraded", sel.detail)


async def test_verified_limits_keep_plain_container(docker_ok, monkeypatch):
    async def run(self, language, code, timeout_s, limits):
        return RunResult(stdout=f"{_V2_OK}\n{select_mod._PROBE_MARKER}\n", stderr="",
                         exit_code=0, timed_out=False, isolation="container")

    monkeypatch.setattr(ContainerBackend, "run", run)
    sel = await BackendSelector().get(_limits())
    assert (sel.verdict, sel.detail) == ("container", None)


# ----------------------------------------- 7a critic round 1 (verifier-confirmed fixes)


async def test_cancelled_handler_leaves_no_run_behind(monkeypatch):
    """Critic finding 1: on Python 3.11 `asyncio.wait_for` ran the run in an inner
    task that could outlive a handler the SDK cancelled through its anyio scope —
    a run nothing tracked, still in flight at shutdown. The run must unwind inside
    the handler task itself."""
    from vestibule import server

    b = ContainerBackend()
    monkeypatch.setattr(b, "_schedule_reap", lambda: None)
    gate = asyncio.Event()

    async def slow_to_unwind_preflight(image):
        try:
            await gate.wait()
        except asyncio.CancelledError:
            await asyncio.sleep(0.2)  # unwinding takes a while, like a CLI subprocess
            raise

    monkeypatch.setattr(b, "_preflight_image", slow_to_unwind_preflight)

    async def fake_get_warden():
        return b

    monkeypatch.setattr(server, "get_warden", fake_get_warden)
    async with anyio.create_task_group() as tg:
        tg.start_soon(server._handle_run_code, {"language": "python", "code": "1"})
        await asyncio.sleep(0.05)
        tg.cancel_scope.cancel()

    assert b.inflight == 0  # unwound before the handler finished
    assert server._other_live_tasks() == []


async def test_aclose_is_not_clean_while_a_run_is_in_flight(docker_ok, monkeypatch):
    probe, _ = _probe_that(None)
    monkeypatch.setattr(BackendSelector, "_probe", probe)
    s = BackendSelector()
    sel = await s.get(_limits())
    sel.warden._inflight = 1  # a run that has not unwound yet
    assert await s.aclose(rm_budget_s=0.1, wait_budget_s=0.1) is False
    sel.warden._inflight = 0
    assert await s.aclose(rm_budget_s=0.1, wait_budget_s=0.1) is True


async def test_server_shutdown_hard_exits_when_any_other_task_is_alive(monkeypatch):
    """Belt and braces: even after a 'clean' aclose(), any live task at all means
    asyncio.run's cancel-all would have something to cancel mid-spawn."""
    from vestibule import server

    exits: list[int] = []

    class CleanSelector:
        async def aclose(self):
            return True

    monkeypatch.setattr(server, "SELECTOR", CleanSelector())
    monkeypatch.setattr(server.os, "_exit", exits.append)
    stray = asyncio.create_task(asyncio.sleep(0.2))
    await server._shutdown()
    assert exits == [0]
    await stray


async def test_unusable_workspace_gets_its_own_refusal(docker_ok, monkeypatch):
    """Critic finding 7: a guest can leave the workspace unusable (e.g. chmod 555 on a
    Linux host, where it runs as your uid). That must read as a fixable folder
    problem naming the folder — not as a runtime that cannot isolate."""
    probe, _ = _probe_that(
        "probe exit 1, timed_out=False: bash: line 3: "
        "/workspace/.vestibule-probe-0123456789ab: Permission denied",
        "probe exit 1, timed_out=False: bash: line 3: "
        "/workspace/.vestibule-probe-ba9876543210: Permission denied",
    )
    monkeypatch.setattr(BackendSelector, "_probe", probe)
    lim = _limits(workspace_dir="~/vestibule-ws-under-test")
    with pytest.raises(RunRefusedError) as ei:
        await BackendSelector().get(lim)
    msg = str(ei.value)
    assert "could not use the workspace" in msg
    assert str(lim.workspace_path) in msg
    assert "Permission denied" in msg
    assert "cannot enforce" not in msg


# --- M2 step 6: the four values, N5's order, provisional selections, the reasons -------
# (docs/plans/M2/M2-step6-selection.md §4.1). `_try_native` and `_select_container` are
# replaced by fakes; the platform is the module's own `sys` reference, nothing global.

class _FakeWarden(Warden):
    def __init__(self) -> None:
        self.drained = False

    async def run(self, language, code, timeout_s, limits):  # pragma: no cover
        raise AssertionError("selection tests never run code")

    async def drain(self, timeout=None):
        self.drained = True
        return True


def _on(monkeypatch, platform):
    monkeypatch.setattr(select_mod, "sys", types.SimpleNamespace(platform=platform))


def _fake_backends(monkeypatch, native, container):
    """`native`/`container`: a verdict, or an exception to raise. Returns what was called."""
    calls = {"native": 0, "retry": []}
    nw, cw = _FakeWarden(), _FakeWarden()

    async def try_native(self, limits):
        calls["native"] += 1
        if isinstance(native, BaseException):
            raise native
        return select_mod.Selection(nw, native, None)

    async def select_container(self, limits, degraded_retry=True):
        calls["retry"].append(degraded_retry)
        if isinstance(container, BaseException):
            raise container
        return select_mod.Selection(cw, container, None)

    monkeypatch.setattr(BackendSelector, "_try_native", try_native)
    monkeypatch.setattr(BackendSelector, "_select_container", select_container)
    return calls, nw, cw


_LATER = RunRefusedError("daemon not reachable")  # may clear up on its own
_FAILED = select_mod._ProbeFailed("the full container profile failed its probe (exit 1)")
_UNPULLED = RunRefusedError("image python@sha256:… is not usable (No such image); run "
                            "`docker pull python@sha256:…` once and retry")
_NEVER = select_mod._RestartRequired("not installed; restart your agent")


@pytest.mark.parametrize("native, container, chosen, drained, provisional", [
    ("native", "container", "native", None, False),
    ("native-degraded", "container", "container", "native", False),
    ("native-degraded", "container-degraded", "native", "container", False),
    ("native-degraded", _LATER, "native", None, True),
    ("native-degraded", _FAILED, "native", None, False),  # deterministic: final (F2)
    ("native-degraded", _UNPULLED, "native", None, True),  # a pull fixes it (round 2, G1)
    ("native-degraded", _NEVER, "native", None, False),
    (RunRefusedError("no native rootfs"), "container", "container", None, False),
    (RunRefusedError("no native rootfs"), "container-degraded", "container", None, False),
])
async def test_auto_on_linux_follows_n5s_order(monkeypatch, native, container, chosen, drained,
                                                provisional):
    _on(monkeypatch, "linux")
    calls, nw, cw = _fake_backends(monkeypatch, native, container)
    sel = await BackendSelector().get(_limits(backend="auto"))
    assert sel.warden is (nw if chosen == "native" else cw)
    assert (nw.drained, cw.drained) == (drained == "native", drained == "container")
    assert (sel.recheck_at is not None) == provisional
    if native == "native":
        assert calls["retry"] == []  # full native: the container is never probed
    else:  # the soft-off retry only when no native-degraded is in hand (S6-D3)
        assert calls["retry"] == [isinstance(native, BaseException)]


async def test_auto_with_neither_names_both_and_retries(monkeypatch):
    _on(monkeypatch, "linux")
    _fake_backends(monkeypatch, RunRefusedError("no native rootfs"), _LATER)
    with pytest.raises(RunRefusedError) as ei:
        await BackendSelector().get(_limits(backend="auto"))
    assert not isinstance(ei.value, select_mod._RestartRequired)  # one part can still change
    assert "native: no native rootfs" in str(ei.value)
    assert "container: daemon not reachable" in str(ei.value)


async def test_a_restart_refusal_next_to_a_retryable_one_is_not_restart_only(monkeypatch):
    """Only when both parts need a restart is the whole refusal restart-required (F6)."""
    _on(monkeypatch, "linux")
    _fake_backends(monkeypatch, select_mod._RestartRequired("overlap"), _LATER)
    with pytest.raises(RunRefusedError) as ei:
        await BackendSelector().get(_limits(backend="auto"))
    assert not isinstance(ei.value, select_mod._RestartRequired)


async def test_auto_with_neither_fixable_without_restart_says_restart(monkeypatch):
    _on(monkeypatch, "linux")
    _fake_backends(monkeypatch, select_mod._RestartRequired("overlap"), _NEVER)
    with pytest.raises(select_mod._RestartRequired):
        await BackendSelector().get(_limits(backend="auto"))


@pytest.mark.parametrize("native, refused", [
    (RunRefusedError("no native rootfs"), True),
    ("native-degraded", False),
])
async def test_forced_native_never_probes_the_container(monkeypatch, native, refused):
    _on(monkeypatch, "linux")
    calls, nw, _ = _fake_backends(monkeypatch, native, "container")
    if refused:
        with pytest.raises(RunRefusedError, match="native isolation is unavailable: no native"):
            await BackendSelector().get(_limits(backend="native"))
    else:
        assert (await BackendSelector().get(_limits(backend="native"))).warden is nw
    assert calls["retry"] == []


async def test_native_off_linux_is_restart_required(monkeypatch):
    _on(monkeypatch, "win32")
    with pytest.raises(select_mod._RestartRequired, match="needs Linux"):
        await BackendSelector().get(_limits(backend="native"))


async def test_auto_off_linux_never_tries_native(monkeypatch):
    _on(monkeypatch, "darwin")
    calls, _, cw = _fake_backends(monkeypatch, AssertionError("probed"), "container")
    assert (await BackendSelector().get(_limits(backend="auto"))).warden is cw
    assert calls["native"] == 0


async def test_unknown_backend_names_all_four():
    with pytest.raises(select_mod._RestartRequired) as ei:
        await BackendSelector().get(_limits(backend="nativ"))
    assert all(f"'{v}'" in str(ei.value) for v in ("auto", "native", "container", "naive"))


async def test_a_runtime_typo_is_refused_before_native_is_probed(monkeypatch):
    _on(monkeypatch, "linux")
    calls, _, _ = _fake_backends(monkeypatch, "native", "container")
    with pytest.raises(select_mod._RestartRequired, match="VESTIBULE_RUNTIME"):
        await BackendSelector().get(_limits(backend="auto", runtime="dokcer"))
    assert calls["native"] == 0


async def test_a_provisional_selection_is_rechecked_after_the_cooldown(monkeypatch):
    _on(monkeypatch, "linux")
    _fake_backends(monkeypatch, "native-degraded", _LATER)
    s = BackendSelector()
    first = await s.get(_limits(backend="auto"))
    assert first.recheck_at is not None and await s.get(_limits(backend="auto")) is first
    calls, _, cw = _fake_backends(monkeypatch, "native-degraded", "container")  # Docker's up
    first.recheck_at = time.monotonic() - 1  # the cooldown passed
    assert (await s.get(_limits(backend="auto"))).warden is cw
    # the switch sticks: cached, and never asked again (round 2, G2)
    assert s._selection.warden is cw and (await s.get(_limits(backend="auto"))).warden is cw
    assert calls["retry"] == [False]


@pytest.mark.parametrize("selected, run, dropped", [
    ("native", "native-degraded", True),  # it lost its limits: N5 may now pick the container
    ("native-degraded", "native-degraded", False),  # can't loop
    ("native-degraded", "none", True),
    ("native", "native", False),
])
async def test_note_result_for_native(monkeypatch, selected, run, dropped):
    _on(monkeypatch, "linux")
    _, nw, _ = _fake_backends(monkeypatch, selected, _NEVER)
    s = BackendSelector()
    await s.get(_limits(backend="native"))
    s.note_result(nw, RunResult(stdout="", stderr="", exit_code=0, timed_out=False,
                                isolation=run))
    assert (s._selection is None) == dropped


def _r(isolation, detail=None, exit_code=0, stderr="", timed_out=False):
    return RunResult(stdout="", stderr=stderr, exit_code=exit_code, timed_out=timed_out,
                     isolation=isolation, isolation_detail=detail)


_SETUP_FAILED = "native sandbox setup failed at helper: [Errno 1] Operation not permitted"


@pytest.mark.parametrize("r, sysctls, hint", [
    (_r("none", _SETUP_FAILED), {"apparmor_restrict_unprivileged_userns": "1"},
     "Ubuntu restricts unprivileged user namespaces"),
    (_r("none", "native sandbox setup failed at mounts: [Errno 1] mount /: "
                "Operation not permitted"), {"apparmor_restrict_unprivileged_userns": "1"},
     "Ubuntu restricts unprivileged user namespaces"),  # any stage, keyed on the setting
    (_r("none", _SETUP_FAILED), {"max_user_namespaces": "0"}, "user namespaces are disabled"),
    (_r("none", "native sandbox setup failed at helper: libseccomp.so.2: cannot open shared "
                "object file"), {}, "install libseccomp2"),
    (_r("none", "native sandbox setup failed at mounts: [Errno 1] mount /r/proc: Operation "
                "not permitted; nothing was executed"), {}, "/proc isn't fully visible"),
    (_r("none", _SETUP_FAILED), {}, None),
])
def test_the_native_reason_names_the_fix(r, sysctls, hint):
    reason = select_mod._native_reason(r, _limits(), sysctls)
    assert reason.startswith(r.isolation_detail)
    assert (hint in reason) if hint else reason == r.isolation_detail


def test_a_rootfs_path_holding_proc_is_not_a_hidden_proc():
    """Round 2, G3: only a mount *of* /proc failing is that evidence."""
    reason = select_mod._native_reason(
        _r("none", "native sandbox setup failed at mounts: [Errno 1] mount "
                   "/home/u/procurement/rootfs/abc: Operation not permitted"),
        _limits(), {"apparmor_restrict_unprivileged_userns": "1"})
    assert "Ubuntu restricts" in reason and "/proc isn't fully visible" not in reason


def test_a_failed_guest_still_gets_a_reason():
    """The sandbox was fine (`native`, no detail) but the guest failed: M1's form, not ''."""
    reason = select_mod._native_reason(
        _r("native", exit_code=124, timed_out=True, stderr="slow\n"), _limits(), {})
    assert reason == "probe exit 124, timed out yes: slow"


def test_a_guest_that_cannot_use_the_workspace_gets_m1s_message():
    lim = _limits(workspace_dir="~/vestibule-ws-under-test")
    reason = select_mod._native_reason(
        _r("native", exit_code=127, stderr="vestibule: cannot start the program: [Errno 13] "
                                           "Permission denied: '/workspace'\n"), lim, {})
    assert "could not use the workspace" in reason and str(lim.workspace_path) in reason


# --- the provisional re-check asks only the container (critic round, F1 and F2) -------------

async def _provisional(monkeypatch):
    _on(monkeypatch, "linux")
    _fake_backends(monkeypatch, "native-degraded", _LATER)
    s = BackendSelector()
    first = await s.get(_limits(backend="auto"))
    assert first.recheck_at is not None
    first.recheck_at = time.monotonic() - 1  # the cooldown passed
    return s, first


async def test_a_failing_recheck_keeps_the_working_native_selection(monkeypatch):
    """F1: native is never probed again (its probe would compete for the shared slots); a
    container still not up keeps the working native-degraded selection, still provisional."""
    s, first = await _provisional(monkeypatch)
    calls, _, _ = _fake_backends(monkeypatch, RunRefusedError("too many concurrent runs"), _LATER)
    assert await s.get(_limits(backend="auto")) is first
    assert calls["native"] == 0 and calls["retry"] == [False]
    assert first.recheck_at is not None and first.recheck_at > time.monotonic()


async def test_a_recheck_that_finds_the_image_unpulled_keeps_waiting(monkeypatch):
    s, first = await _provisional(monkeypatch)
    _fake_backends(monkeypatch, "native-degraded", _UNPULLED)
    assert await s.get(_limits(backend="auto")) is first
    assert first.recheck_at is not None and first.recheck_at > time.monotonic()


async def test_a_recheck_that_fails_for_good_makes_native_final(monkeypatch):
    s, first = await _provisional(monkeypatch)
    _fake_backends(monkeypatch, "native-degraded", _FAILED)
    assert await s.get(_limits(backend="auto")) is first
    assert first.recheck_at is None


async def test_a_recheck_that_finds_a_degraded_container_keeps_native(monkeypatch):
    s, first = await _provisional(monkeypatch)
    _, _, cw = _fake_backends(monkeypatch, "native-degraded", "container-degraded")
    assert await s.get(_limits(backend="auto")) is first
    assert first.recheck_at is None and cw.drained  # N5: native-degraded ranks first


async def test_a_recheck_waits_while_every_slot_is_busy(monkeypatch):
    """Never compete with real runs for the shared budget: postpone instead."""
    s, first = await _provisional(monkeypatch)
    calls, _, _ = _fake_backends(monkeypatch, "native-degraded", "container")
    for _ in range(_limits().max_concurrent):
        await s._slots.acquire()
    assert await s.get(_limits(backend="auto")) is first
    assert calls["retry"] == [] and first.recheck_at > time.monotonic()


def test_a_missing_library_is_named_before_the_apparmor_guess():
    """F5: concrete evidence in the reason wins over the sysctl, which only makes a cause
    likely; and the AppArmor hint needs an actual setup failure, not a missing report."""
    apparmor = {"apparmor_restrict_unprivileged_userns": "1"}
    reason = select_mod._native_reason(
        _r("none", "native sandbox setup failed at helper: libseccomp.so.2: cannot open "
                   "shared object file"), _limits(), apparmor)
    assert "install libseccomp2" in reason and "Ubuntu" not in reason
    reason = select_mod._native_reason(
        _r("none", "the sandbox sent no status report; no isolation is claimed for this run"),
        _limits(), apparmor)
    assert "Ubuntu" not in reason


async def test_the_full_profile_failing_its_probe_is_final(docker_ok, monkeypatch):
    """The real raise site: next to native-degraded, a full-profile probe failure is
    `_ProbeFailed`, the refusal that makes the selection final (round 2, G1)."""
    probe, _ = _probe_that("probe exit 1, timed_out=False: boom")
    monkeypatch.setattr(BackendSelector, "_probe", probe)
    with pytest.raises(select_mod._ProbeFailed):
        await BackendSelector()._select_container(_limits(), degraded_retry=False)
