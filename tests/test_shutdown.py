"""The POSIX shutdown hand-off as unit tests (M2 step 1b, S1-D5; plan §4.1). All platforms:
`subprocess.Popen` and `os._exit` are stubbed, so nothing is started and nothing exits.

The real signal and EOF paths run in tests/test_signals.py (POSIX) and the container
hand-off end to end in tests/test_stdio_e2e.py.
"""
import asyncio
import json
import os
import secrets
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from vestibule import cleanup, server
from vestibule.config import Limits
from vestibule.backends.container import ContainerBackend, _RunRecord
from vestibule.backends.naive import NaiveBackend
from vestibule.backends.select import BackendSelector, CleanupJob, Selection


class FakeProc:
    """Stands in for an asyncio subprocess: a returncode and a kill()."""

    def __init__(self, returncode: int | None = None) -> None:
        self.returncode = returncode
        self.killed = False

    def kill(self) -> None:
        self.killed = True


class FakeSelector:
    def __init__(self, job: CleanupJob | Exception) -> None:
        self._job = job

    def cleanup_job(self) -> CleanupJob:
        if isinstance(self._job, Exception):
            raise self._job
        return self._job


@pytest.fixture()
def handoff(monkeypatch):
    """Records Popen calls and exit codes; resets the once-only flag."""
    rec: dict[str, list] = {"popen": [], "exit": []}
    monkeypatch.setattr(server, "_handed_off", False)
    monkeypatch.setattr(server.subprocess, "Popen", lambda *a, **kw: rec["popen"].append((a, kw)))
    monkeypatch.setattr(server.os, "_exit", rec["exit"].append)
    return rec


def _job(procs=()) -> CleanupJob:
    return CleanupJob(
        runtimes=[{"argv": ["docker"], "owners": ["own"], "names": ["vestibule-abc"]}],
        tmpdirs=["/tmp/vestibule-run-x"],
        cli_procs=list(procs),
    )


def test_hand_off_kills_clis_starts_a_detached_cleaner_and_exits(handoff, monkeypatch, capfd):
    procs = [FakeProc(), FakeProc()]
    job = _job(procs)
    monkeypatch.setattr(server, "SELECTOR", FakeSelector(job))

    server._hand_off_and_exit("SIGINT")

    assert all(p.killed for p in procs)
    assert len(handoff["popen"]) == 1
    (argv,), kw = handoff["popen"][0]
    # By file path: -I drops PYTHONPATH and the user site, where the package may live.
    assert argv[:4] == [sys.executable, "-I", "-B", cleanup.__file__]
    assert json.loads(argv[4]) == job.payload()
    assert kw["start_new_session"] is True and kw["close_fds"] is True
    assert kw["cwd"] == tempfile.gettempdir()
    for stream in ("stdin", "stdout", "stderr"):
        assert kw[stream] is subprocess.DEVNULL  # golden rule 1
    assert handoff["exit"] == [0]
    out, err = capfd.readouterr()
    assert out == ""  # never touches stdout
    assert "vestibule shutdown (SIGINT): handed 1 container(s) and 1 temp dir(s)" in err


def test_nothing_to_hand_off_starts_nothing(handoff, monkeypatch, capfd):
    monkeypatch.setattr(server, "SELECTOR", FakeSelector(CleanupJob()))
    server._hand_off_and_exit("stdin closed")
    assert handoff["popen"] == [] and handoff["exit"] == [0]
    assert "shutdown (stdin closed): nothing to clean up" in capfd.readouterr().err


def test_a_failing_snapshot_still_exits(handoff, monkeypatch, capfd):
    monkeypatch.setattr(server, "SELECTOR", FakeSelector(RuntimeError("boom")))
    server._hand_off_and_exit("SIGTERM")
    assert handoff["exit"] == [0]
    assert "hand-off failed: RuntimeError('boom')" in capfd.readouterr().err


def test_a_failing_spawn_still_exits(handoff, monkeypatch, capfd):
    monkeypatch.setattr(server, "SELECTOR", FakeSelector(_job()))

    def no_spawn(*a, **kw):
        raise OSError("no python")

    monkeypatch.setattr(server.subprocess, "Popen", no_spawn)
    server._hand_off_and_exit("SIGHUP")
    assert handoff["exit"] == [0]
    assert "hand-off failed: OSError('no python')" in capfd.readouterr().err


def test_only_the_first_trigger_acts(handoff, monkeypatch):
    """Signal then EOF, or EOF then signal: the second is a no-op."""
    monkeypatch.setattr(server, "SELECTOR", FakeSelector(_job()))
    server._hand_off_and_exit("SIGINT")
    server._hand_off_and_exit("stdin closed")
    assert len(handoff["popen"]) == 1 and handoff["exit"] == [0]


async def test_the_stdin_wrapper_hands_off_before_the_sdk_sees_eof(monkeypatch):
    class Inner:
        def __init__(self) -> None:
            self.lines = ['{"jsonrpc": "2.0"}\n', ""]

        async def readline(self) -> str:
            return self.lines.pop(0)

    triggers: list[str] = []
    monkeypatch.setattr(server, "_hand_off_and_exit", triggers.append)
    seen = [line async for line in server._HandOffOnEof(Inner())]
    assert seen == ['{"jsonrpc": "2.0"}\n']
    assert triggers == ["stdin closed"]  # before the iteration ended for the SDK


def test_cleanup_job_snapshots_every_tracked_backend(tmp_path):
    sel = BackendSelector()
    d1, d2 = ContainerBackend("docker"), ContainerBackend("docker")
    p1 = ContainerBackend("podman")
    live, done = FakeProc(None), FakeProc(0)
    d1._active.add("vestibule-1")
    d1._runs["vestibule-1"] = _RunRecord(str(tmp_path / "r1"), proc=live)  # type: ignore[arg-type]
    d2._runs["vestibule-2"] = _RunRecord(str(tmp_path / "r2"), proc=done)  # type: ignore[arg-type]
    p1._active.add("vestibule-3")
    sel._backends = [d1, d2, p1]

    job = sel.cleanup_job()

    by_rt = {rt["argv"][0]: rt for rt in job.runtimes}
    assert set(by_rt) == {"docker", "podman"}
    assert by_rt["docker"]["owners"] == [d1.owner, d2.owner]
    assert by_rt["docker"]["names"] == ["vestibule-1"]
    assert by_rt["podman"]["names"] == ["vestibule-3"]
    assert job.tmpdirs == [str(tmp_path / "r1"), str(tmp_path / "r2")]
    assert job.cli_procs == [live]  # an exited CLI needs no kill
    assert not job.empty


def test_naive_guests_are_in_the_kill_list():
    """The dev-only naive backend's guests are plain children: without this they would
    outlive the server's exit (step-1b critic finding 2)."""
    sel = BackendSelector()
    naive = NaiveBackend()
    live, done = FakeProc(None), FakeProc(0)
    naive._procs = {live, done}  # type: ignore[assignment]
    sel._selection = Selection(naive, "none")
    job = sel.cleanup_job()
    assert job.guests == [live] and job.cli_procs == []
    assert job.empty  # nothing for the cleaner; the hand-off kills the guest itself


posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups, /proc")


def _alive(tag: str) -> list[int]:
    """PIDs (not zombies) whose command line contains `tag`."""
    found = []
    for pid in filter(str.isdigit, os.listdir("/proc")):
        try:
            cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
            state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
        except (OSError, IndexError):
            continue
        if tag.encode() in cmdline and state != "Z":
            found.append(int(pid))
    return found


async def _until(predicate, timeout: float = 10) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            return False
        await asyncio.sleep(0.05)
    return True


async def _start_naive_sleeper(tmp_path):
    """A real naive bash guest whose child is a uniquely tagged `sleep`."""
    tag = str(31_000_000 + secrets.randbelow(1_000_000))
    naive = NaiveBackend()
    limits = Limits(workspace_dir=str(tmp_path / "ws"))
    task = asyncio.create_task(naive.run("bash", f"sleep {tag}; echo done\n", 60, limits))
    assert await _until(lambda: _alive(tag)), "the guest's sleep never started"
    return naive, task, tag


@posix_only
async def test_naive_cancel_kills_the_guest_and_its_children(tmp_path):
    """Step-1b critic round 2: a cancelled naive run must not leave its guest running
    (POSIX exits with os._exit, which skips the teardown that used to kill it)."""
    naive, task, tag = await _start_naive_sleeper(tmp_path)
    assert naive.live_procs()  # registered while it runs
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await _until(lambda: not _alive(tag), 5), "the guest's child survived the cancel"
    assert naive.live_procs() == []


@posix_only
async def test_naive_hand_off_kills_the_guest_group(tmp_path, monkeypatch):
    """The hand-off kills a running naive guest's whole process group, its `sleep` child
    included (the first fix killed only the interpreter)."""
    naive, task, tag = await _start_naive_sleeper(tmp_path)
    sel = BackendSelector()
    sel._selection = Selection(naive, "none")
    exits: list[int] = []
    spawned: list[dict] = []
    # Not the `handoff` fixture: its global Popen stub would stop the guest from starting.
    monkeypatch.setattr(server, "_handed_off", False)
    monkeypatch.setattr(server, "SELECTOR", sel)
    monkeypatch.setattr(server, "_spawn_cleaner", spawned.append)
    monkeypatch.setattr(server.os, "_exit", exits.append)
    try:
        server._hand_off_and_exit("SIGINT")
        assert exits == [0] and spawned == []  # no cleaner needed
        assert await _until(lambda: not _alive(tag), 5), "the guest's child survived"
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def test_snapshot_retires_records_whose_cli_has_since_exited(tmp_path):
    """A record kept after its finisher (CLI still alive then) must not linger once the
    CLI exits, or every later shutdown starts a cleaner (step-1b critic finding 5)."""
    b = ContainerBackend("docker")
    proc = FakeProc(None)
    b._runs["vestibule-z"] = _RunRecord(str(tmp_path / "z"), proc=proc)  # type: ignore[arg-type]
    b._forget_run("vestibule-z", finished=True)
    assert b.cleanup_snapshot()[1] == [str(tmp_path / "z")]  # CLI alive: still listed
    proc.returncode = 0
    assert b.cleanup_snapshot() == ([], [], [])
    assert "vestibule-z" not in b._runs


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only parent dir")
def test_run_dirs_live_in_a_private_parent_the_snapshot_cleans(tmp_path):
    """A late container re-creates its deleted run dir as root; in sticky /tmp the user
    could never remove it, inside this user-owned parent they can (Linux gate finding).
    An empty parent is removed at snapshot time; a non-empty one goes to the cleaner last."""
    b = ContainerBackend("docker")
    parent = b._run_dir_parent()
    assert parent is not None and Path(parent).is_dir()
    assert b._run_dir_parent() == parent  # one per backend
    run_dir = tempfile.mkdtemp(prefix="vestibule-run-", dir=parent)
    b._runs["vestibule-p"] = _RunRecord(run_dir)
    _, tmpdirs, _ = b.cleanup_snapshot()
    assert tmpdirs == [run_dir, parent]  # the parent after its children
    del b._runs["vestibule-p"]
    Path(run_dir).rmdir()
    assert b.cleanup_snapshot() == ([], [], [])
    assert not Path(parent).exists()


def test_run_record_is_retired_only_when_finisher_and_cli_are_done():
    b = ContainerBackend("docker")
    for order in (("finished", "cli_reaped"), ("cli_reaped", "finished")):
        b._runs["vestibule-x"] = _RunRecord("/tmp/x", proc=FakeProc(None))  # type: ignore[arg-type]
        b._forget_run("vestibule-x", **{order[0]: True})
        assert "vestibule-x" in b._runs, order  # half done: a snapshot must still see it
        b._forget_run("vestibule-x", **{order[1]: True})
        assert "vestibule-x" not in b._runs, order
    # A run whose CLI never spawned, or already exited, retires with its finisher.
    b._runs["vestibule-y"] = _RunRecord("/tmp/y")
    b._forget_run("vestibule-y", finished=True)
    assert "vestibule-y" not in b._runs
