"""M1 step 4 lifecycle unit tests — no Docker needed (plan §6 tests 1–5).

Covers the semaphore cap, the bounded-wait busy refusal (S4-D1) and its server
rendering, the pure reap decision rule (S4-D3), cancellation hygiene, and the
deadline/owner label emission.
"""
import asyncio
import shutil

import pytest

import vestibule.backends.container as container_mod
from vestibule.backends.base import RunRefusedError, RunResult
from vestibule.backends.container import ContainerBackend
from vestibule.config import Limits


def _ok() -> RunResult:
    return RunResult(stdout="ok", stderr="", exit_code=0, timed_out=False, isolation="container")


async def _ok_preflight(image):
    return None


@pytest.fixture()
async def quiet_backend(monkeypatch):
    """A backend whose reaper, cleanup, and image preflight never touch a real runtime."""
    b = ContainerBackend()

    async def noop_cleanup(name, tmpdir):
        shutil.rmtree(tmpdir, ignore_errors=True)  # keep temp hygiene, skip the runtime

    monkeypatch.setattr(b, "_cleanup", noop_cleanup)
    monkeypatch.setattr(b, "_schedule_reap", lambda: None)
    monkeypatch.setattr(b, "_preflight_image", _ok_preflight)
    yield b
    # Detached finishers must not outlive the test's event loop.
    await asyncio.gather(*list(b._finishers), return_exceptions=True)


async def test_semaphore_caps_concurrency(quiet_backend, monkeypatch):
    b = quiet_backend
    limits = Limits(max_concurrent=2)
    in_flight = 0
    peak = 0
    gate = asyncio.Event()

    async def fake_execute(*args):
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await gate.wait()
        in_flight -= 1
        return _ok()

    monkeypatch.setattr(b, "_execute", fake_execute)
    tasks = [asyncio.create_task(b.run("python", "1", 5, limits)) for _ in range(4)]
    await asyncio.sleep(0.05)  # two enter _execute, two queue on the semaphore
    assert peak == 2
    gate.set()
    results = await asyncio.gather(*tasks)
    assert all(r.exit_code == 0 for r in results)
    assert peak == 2  # the queued pair never overlapped the first pair


async def test_busy_refusal_after_bounded_wait(quiet_backend, monkeypatch):
    b = quiet_backend
    limits = Limits(max_concurrent=1)
    monkeypatch.setattr(container_mod, "_SEM_WAIT_S", 0.1)  # keep the test fast
    gate = asyncio.Event()

    async def slow_execute(*args):
        await gate.wait()
        return _ok()

    monkeypatch.setattr(b, "_execute", slow_execute)
    holder = asyncio.create_task(b.run("python", "1", 5, limits))
    await asyncio.sleep(0.02)  # let the holder take the only slot
    with pytest.raises(RunRefusedError, match="too many concurrent runs"):
        await b.run("python", "1", 5, limits)
    gate.set()
    r = await holder
    assert r.exit_code == 0


async def test_server_renders_refusal_as_blocked(monkeypatch):
    from vestibule import server

    class RefusingWarden:
        async def run(self, *args, **kwargs):
            raise RunRefusedError("too many concurrent runs (max 4); retry shortly")

    async def fake_get_warden():  # step 5: get_warden is async (cached selection)
        return RefusingWarden()

    monkeypatch.setattr(server, "get_warden", fake_get_warden)
    out = await server._handle_run_code({"language": "python", "code": "print(1)"})
    assert out[0].text.startswith("Blocked: too many concurrent runs")


NOW = 1_000_000.0


@pytest.mark.parametrize(
    ("name", "deadline_raw", "active", "expected"),
    [
        # past deadline + margin => reap, regardless of state
        ("vestibule-aaaa", str(int(NOW) - 61), set(), True),
        ("vestibule-aaaa", "1", set(), True),  # epoch 1970: long past
        # past deadline but within the margin => keep
        ("vestibule-aaaa", str(int(NOW) - 30), set(), False),
        # future deadline => keep (fresh created/running/exited all covered)
        ("vestibule-aaaa", str(int(NOW) + 500), set(), False),
        # missing or garbage label => never reap on bad data
        ("vestibule-aaaa", "", set(), False),
        ("vestibule-aaaa", "not-a-number", set(), False),
        # our own in-flight run => untouchable even past deadline
        ("vestibule-aaaa", "1", {"vestibule-aaaa"}, False),
    ],
)
def test_reap_decision(name, deadline_raw, active, expected):
    assert ContainerBackend._reap_decision(name, deadline_raw, NOW, active) is expected


async def test_cancelled_run_releases_slot_and_active_name(quiet_backend, monkeypatch):
    b = quiet_backend
    limits = Limits(max_concurrent=1)

    async def hang_execute(*args):
        await asyncio.Event().wait()
        return _ok()  # pragma: no cover

    monkeypatch.setattr(b, "_execute", hang_execute)
    task = asyncio.create_task(b.run("python", "1", 5, limits))
    await asyncio.sleep(0.02)
    assert len(b._active) == 1
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    # Cleanup is detached (Codex P2): give the finisher its turn, then check hygiene.
    await asyncio.gather(*list(b._finishers), return_exceptions=True)
    assert b._active == set()

    # The single slot must be free again: a fresh run acquires it immediately.
    async def quick_execute(*args):
        return _ok()

    monkeypatch.setattr(b, "_execute", quick_execute)
    r = await b.run("python", "1", 5, limits)
    assert r.exit_code == 0


async def test_result_not_delayed_by_cleanup(quiet_backend, monkeypatch):
    """Codex P2 regression: run() returns as soon as the run is done — cleanup runs
    detached — but the concurrency slot stays held until cleanup completes."""
    b = quiet_backend
    limits = Limits(max_concurrent=1)
    monkeypatch.setattr(container_mod, "_SEM_WAIT_S", 0.05)
    finished = asyncio.Event()

    async def blocked_cleanup(name, tmpdir):
        await finished.wait()

    async def quick_execute(*args):
        return _ok()

    monkeypatch.setattr(b, "_cleanup", blocked_cleanup)
    monkeypatch.setattr(b, "_execute", quick_execute)

    r = await b.run("python", "1", 5, limits)  # returns although cleanup is blocked
    assert r.exit_code == 0

    # The slot is still owned by the pending cleanup: a new run is refused...
    with pytest.raises(RunRefusedError):
        await b.run("python", "1", 5, limits)

    # ...and becomes available the moment cleanup finishes.
    finished.set()
    await asyncio.gather(*list(b._finishers))
    r2 = await b.run("python", "1", 5, limits)
    assert r2.exit_code == 0


def test_build_command_stamps_deadline_and_owner():
    b = ContainerBackend()
    cmd = b._build_command("cafe01", "python", Limits(), "/h/sbx", "/h/ws", 1234567890)
    assert "vestibule.deadline=1234567890" in cmd
    assert f"vestibule.owner={b._owner}" in cmd
    # profile sanity: the step-3 invariants survived the restructure
    assert cmd[cmd.index("--network") + 1] == "none"
    assert "-i" not in cmd
    assert "-t" not in cmd


# ------------------------------------------------- Codex budget finding (2026-07-05)


class FakeCliProc:
    """A stand-in `docker run` CLI: streams we control, a wait() we control."""

    def __init__(self, stdout: bytes = b"", eof: bool = False):
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader()
        if stdout:
            self.stdout.feed_data(stdout)
        if eof:
            self.stdout.feed_eof()
            self.stderr.feed_eof()
        self.returncode = None
        self.killed = False
        self._done = asyncio.Event()

    async def wait(self):
        await self._done.wait()
        return self.returncode

    def kill(self):
        self.killed = True
        self.returncode = -9
        self.stdout.feed_eof()
        self.stderr.feed_eof()
        self._done.set()

    def exit(self, code, stderr: bytes = b""):
        """Complete normally with the given exit code (e.g. docker's own 125)."""
        if stderr:
            self.stderr.feed_data(stderr)
        self.returncode = code
        self.stdout.feed_eof()
        self.stderr.feed_eof()
        self._done.set()


async def test_timed_out_result_returns_before_any_kill(monkeypatch, tmp_path):
    """Codex budget regression (2026-07-05): on guest timeout the honest timed-out
    result returns immediately — kill/rm never serialize into the result path, so a
    wedged daemon can no longer push the backend past the server's outer deadline."""
    b = ContainerBackend()
    limits = Limits(max_concurrent=1, workspace_dir=str(tmp_path / "ws"))
    monkeypatch.setattr(container_mod, "_STARTUP_GRACE_S", 0.2)
    monkeypatch.setattr(container_mod, "_CLEANUP_STEP_S", 0.2)
    monkeypatch.setattr(b, "_schedule_reap", lambda: None)
    monkeypatch.setattr(b, "_preflight_image", _ok_preflight)

    proc = FakeCliProc()  # never EOFs, never exits: the guest loops forever

    async def fake_spawn(*cmd, **kw):
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_spawn)

    cleanup_started = asyncio.Event()
    release = asyncio.Event()

    async def wedged_cleanup(name, tmpdir):
        cleanup_started.set()
        await release.wait()  # a daemon that never answers
        shutil.rmtree(tmpdir, ignore_errors=True)

    monkeypatch.setattr(b, "_cleanup", wedged_cleanup)

    loop = asyncio.get_running_loop()
    t0 = loop.time()
    r = await b.run("python", "while True: pass", 1, limits)
    elapsed = loop.time() - t0

    assert r.timed_out is True
    assert elapsed < 2.0  # ~timeout(1) + grace(0.2); the kill cost the result nothing
    assert not cleanup_started.is_set()  # the result preceded any cleanup work

    release.set()
    await asyncio.gather(*list(b._finishers), return_exceptions=True)
    assert proc.killed  # the detached reaper collected the wedged CLI as last resort


async def test_cli_wedge_after_guest_finished_is_not_a_timeout(
    quiet_backend, monkeypatch, tmp_path
):
    """Output hit EOF (the guest is done) but the CLI never exits: reported honestly
    as a runtime failure — exit code unknown — never as a guest timeout."""
    b = quiet_backend
    limits = Limits(max_concurrent=1, workspace_dir=str(tmp_path / "ws"))
    monkeypatch.setattr(container_mod, "_CLEANUP_STEP_S", 0.2)

    proc = FakeCliProc(stdout=b"done\n", eof=True)  # output complete, CLI wedged

    async def fake_spawn(*cmd, **kw):
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_spawn)

    r = await b.run("python", "print('done')", 5, limits)
    assert r.timed_out is False
    assert r.exit_code == -1
    assert "did not report an exit code" in r.stderr
    assert "done" in r.stdout

    await asyncio.gather(*list(b._finishers), return_exceptions=True)
    assert proc.killed


# ------------------------------------------------------ step 5: preflight & honesty


async def test_preflight_missing_image_refused(quiet_backend, monkeypatch, tmp_path):
    """S5-D3: a missing image is a legible refusal with the pull command — no
    `docker run` is ever spawned (so no auto-pull can happen inside the call)."""
    b = quiet_backend
    limits = Limits(workspace_dir=str(tmp_path / "ws"))

    async def missing(*args):
        return (1, "", "Error: No such image: python:3.12-slim")

    async def must_not_run(*args, **kw):  # pragma: no cover - the assertion IS the test
        raise AssertionError("docker run was spawned for a missing image")

    monkeypatch.setattr(b, "_preflight_image", ContainerBackend._preflight_image.__get__(b))
    monkeypatch.setattr(b, "_cli_status", missing)
    monkeypatch.setattr(b, "_execute", must_not_run)

    with pytest.raises(RunRefusedError, match=r"pull python:3\.12-slim"):
        await b.run("python", "print(1)", 5, limits)


async def test_preflight_passes_once_then_cached(quiet_backend, monkeypatch, tmp_path):
    b = quiet_backend
    limits = Limits(workspace_dir=str(tmp_path / "ws"))
    calls = []

    async def present(*args):
        calls.append(args)
        return (0, "sha256:abc\n", "")

    async def quick_execute(*args):
        return _ok()

    monkeypatch.setattr(b, "_preflight_image", ContainerBackend._preflight_image.__get__(b))
    monkeypatch.setattr(b, "_cli_status", present)
    monkeypatch.setattr(b, "_execute", quick_execute)

    await b.run("python", "print(1)", 5, limits)
    await b.run("python", "print(2)", 5, limits)
    assert len(calls) == 1  # one `image inspect` per image per process


async def test_preflight_unresponsive_runtime_refused(quiet_backend, monkeypatch, tmp_path):
    b = quiet_backend
    limits = Limits(workspace_dir=str(tmp_path / "ws"))

    async def dead(*args):
        return None  # spawn failure or timeout inside _cli_status

    monkeypatch.setattr(b, "_preflight_image", ContainerBackend._preflight_image.__get__(b))
    monkeypatch.setattr(b, "_cli_status", dead)

    with pytest.raises(RunRefusedError, match="did not respond"):
        await b.run("python", "print(1)", 5, limits)


def test_build_command_soft_flags_and_pull_never():
    limits = Limits()
    full = ContainerBackend()._build_command("c1", "python", limits, "/h/sbx", "/h/ws", 1)
    assert ["--pull", "never"] == full[full.index("--pull"):full.index("--pull") + 2]
    for flag in ("--memory", "--memory-swap", "--cpus", "--pids-limit"):
        assert flag in full
    assert any(a.startswith("/tmp:") and "size=" in a for a in full)

    from vestibule.backends.container import SOFT_CONTROLS
    bare = ContainerBackend(soft_disabled=frozenset(SOFT_CONTROLS))._build_command(
        "c2", "python", limits, "/h/sbx", "/h/ws", 1
    )
    for flag in ("--memory", "--memory-swap", "--cpus", "--pids-limit"):
        assert flag not in bare
    assert any(a.startswith("/tmp:") and "size=" not in a for a in bare)  # tmpfs itself stays
    assert "--pull" in bare
    # hard tier is never stripped
    for flag in ("--network", "--cap-drop", "--read-only", "--user"):
        assert flag in bare


async def test_exit_125_reports_no_isolation(quiet_backend, monkeypatch, tmp_path):
    """Exit 125 = the runtime's own 'run failed' code: the container never started,
    so claiming container isolation would be a lie (golden rule 5)."""
    b = quiet_backend
    limits = Limits(workspace_dir=str(tmp_path / "ws"))

    proc = FakeCliProc()
    proc.exit(125, stderr=b"docker: Cannot connect to the Docker daemon\n")

    async def fake_spawn(*cmd, **kw):
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_spawn)

    r = await b.run("python", "print(1)", 5, limits)
    assert r.exit_code == 125
    assert r.isolation == "none"
    assert r.isolation_detail is not None and "nothing was executed" in r.isolation_detail


async def test_degraded_backend_reports_detail(monkeypatch, tmp_path):
    """A soft-disabled backend reports container-degraded + the exact missing list."""
    from vestibule.backends.container import SOFT_CONTROLS

    b = ContainerBackend(soft_disabled=frozenset(SOFT_CONTROLS))
    limits = Limits(workspace_dir=str(tmp_path / "ws"))
    monkeypatch.setattr(b, "_schedule_reap", lambda: None)
    monkeypatch.setattr(b, "_preflight_image", _ok_preflight)

    async def noop_cleanup(name, tmpdir):
        shutil.rmtree(tmpdir, ignore_errors=True)

    monkeypatch.setattr(b, "_cleanup", noop_cleanup)

    proc = FakeCliProc(stdout=b"hi\n")
    proc.exit(0)

    async def fake_spawn(*cmd, **kw):
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_spawn)

    r = await b.run("python", "print('hi')", 5, limits)
    assert r.isolation == "container-degraded"
    assert r.isolation_detail == "limits not applied: cpu, memory, pids, tmpfs-size"

    await asyncio.gather(*list(b._finishers), return_exceptions=True)
