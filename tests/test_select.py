"""M1 step 5 selection tests — no Docker needed (plan §6, Docker-free half).

The `<runtime> version` check and the probe run are monkeypatched; the real
end-to-end selection lives in tests/test_container.py (Docker-marked).
"""
import asyncio
import time

import pytest

import vestibule.backends.select as select_mod
from vestibule.backends.base import RunRefusedError, RunResult
from vestibule.backends.container import SOFT_CONTROLS, ContainerBackend
from vestibule.backends.naive import NaiveBackend
from vestibule.backends.select import BackendSelector


def _limits(**kw):
    from vestibule.config import Limits

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
    with pytest.raises(RunRefusedError, match="cannot enforce the isolation profile"):
        await BackendSelector().get(_limits())


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
    await s.get(_limits())
    assert len(entered) == 1

    # A run that reported honest no-isolation (S4-D2 / exit-125) drops the cache...
    s.note_result(RunResult(stdout="", stderr="", exit_code=125, timed_out=False,
                            isolation="none"))
    await s.get(_limits())
    assert len(entered) == 2  # ...so the next call re-probed

    # A normal container result never invalidates.
    s.note_result(RunResult(stdout="", stderr="", exit_code=0, timed_out=False,
                            isolation="container"))
    await s.get(_limits())
    assert len(entered) == 2


async def test_note_result_ignores_naive_selection():
    s = BackendSelector()
    await s.get(_limits(backend="naive"))
    s.note_result(RunResult(stdout="", stderr="", exit_code=0, timed_out=False,
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
