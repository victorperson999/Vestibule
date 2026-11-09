"""M4 step 1 (F3): the sweep of run dirs a hard-killed server leaves. No Docker needed.

Plan: docs/plans/M4/M4-step1-run-dir-sweep.md, §6 tests 1-20. Tests 21 and 22 need real Docker
and live in test_stdio_e2e.py and test_container.py.

The sweep's layouts run on both platforms: Windows puts dated dirs at the root, POSIX inside a
pid-stamped parent, and one function handles both. The pid rule is stood in wherever a real
`_pid_gone` could run on Windows, where `os.kill(pid, 0)` would send a real Ctrl+C.
"""
import asyncio
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

import vestibule.backends.container as container_mod
from vestibule.backends.base import RunResult
from vestibule.backends.container import ContainerBackend, _RunRecord
from vestibule.config import Limits

NOW = 1_800_000_000
STALE = NOW - 61  # one second past the 60 s margin
LIVE = NOW + 100
UNDATED = "vestibule-run-abcd1234"


def _sweep(root: Path, keep: tuple[str, ...] = (), uid: int | None = None) -> None:
    container_mod._sweep_run_dirs(str(root), NOW, set(keep), uid)


def _dated(root: Path, deadline: int, suffix: str = "abcd1234") -> Path:
    path = root / f"vestibule-run-{deadline}-{suffix}"
    path.mkdir()
    (path / "main.py").write_text("print(1)")
    return path


def _parent(root: Path, pid: int = 4242, suffix: str = "abcd1234") -> Path:
    path = root / f"vestibule-runs-{pid}-{suffix}"
    path.mkdir()
    return path


@pytest.fixture()
def owner_gone(monkeypatch) -> list[int]:
    """Every parent's owner counts as gone. Returns the pids asked about."""
    asked: list[int] = []

    def gone(pid: int) -> bool:
        asked.append(pid)
        return True

    monkeypatch.setattr(container_mod, "_pid_gone", gone)
    return asked


@pytest.fixture()
def owner_alive(monkeypatch) -> None:
    monkeypatch.setattr(container_mod, "_pid_gone", lambda pid: False)


# ------------------------------------------------------------------ the rule (test 1)

@pytest.mark.parametrize(
    ("name", "keep", "stale"),
    [
        (f"vestibule-run-{STALE}-abcd1234", (), True),
        (f"vestibule-run-{NOW - 60}-abcd1234", (), False),  # exactly at the margin: kept
        (f"vestibule-run-{NOW - 59}-abcd1234", (), False),
        (f"vestibule-run-{LIVE}-abcd1234", (), False),
        (f"vestibule-run-{STALE}-abcd_123", (), True),  # mkdtemp's alphabet includes `_`
        (f"vestibule-run-{STALE}-abcd1234", (f"vestibule-run-{STALE}-abcd1234",), False),
        (UNDATED, (), False),
        ("vestibule-run-12345678", (), False),  # undated: mkdtemp's random part can be digits
        ("vestibule-run-12x4-abcd1234", (), False),
        ("vestibule-run--abcd1234", (), False),
        (f"vestibule-run-{chr(0x661) * 3}-abcd1234", (), False),  # non-ASCII digits
        (f"vestibule-run-{STALE}-abcd1234\n", (), False),
        ("vestibule-run-1234567890123-abcd1234", (), False),  # 13 digits
        (f"vestibule-run-{STALE}-ABCD1234", (), False),
        (f"vestibule-runs-{STALE}-abcd1234", (), False),  # a parent's name
        (f"vestibule-run-{STALE}-", (), False),
    ],
)
def test_the_stale_rule(name, keep, stale):
    assert container_mod._run_dir_stale(name, NOW, set(keep)) is stale


# ----------------------------------------------------------- the sweep (tests 2-10, 13)

def test_flat_layout_removes_only_the_stale_dir(tmp_path):
    stale = _dated(tmp_path, STALE)
    live = _dated(tmp_path, LIVE)
    at_margin = _dated(tmp_path, NOW - 60)
    undated = tmp_path / UNDATED
    undated.mkdir()
    garbled = tmp_path / "vestibule-run-12x4-abcd1234"
    garbled.mkdir()
    other = tmp_path / "somebody-elses-folder"
    other.mkdir()

    _sweep(tmp_path)

    assert not stale.exists()
    assert all(p.is_dir() for p in (live, at_margin, undated, garbled, other))
    assert (live / "main.py").is_file()


def test_keep_protects_an_overdue_run_of_this_backend(tmp_path):
    mine = _dated(tmp_path, STALE)
    _sweep(tmp_path, keep=(mine.name,))
    assert mine.is_dir()


def test_keep_protects_an_overdue_run_inside_a_parent(tmp_path, owner_alive):
    """The layout every POSIX run uses."""
    mine = _dated(_parent(tmp_path), STALE)
    _sweep(tmp_path, keep=(mine.name,))
    assert mine.is_dir()


def test_posix_layout_removes_a_stale_child_then_its_empty_dead_parent(tmp_path, owner_gone):
    parent = _parent(tmp_path, pid=4242)
    _dated(parent, STALE)
    _sweep(tmp_path)
    assert not parent.exists()
    assert owner_gone == [4242]


@pytest.mark.parametrize("what", ["a live run", "an undated dir", "a file"])
def test_a_dead_parent_stays_while_anything_is_left_in_it(tmp_path, owner_gone, what):
    parent = _parent(tmp_path)
    if what == "a live run":
        left = _dated(parent, LIVE)
    elif what == "an undated dir":
        left = parent / UNDATED
        left.mkdir()
    else:
        left = parent / "note.txt"
        left.write_text("x")
    _sweep(tmp_path)
    assert parent.is_dir()
    assert left.exists()


def test_a_live_owners_parent_stays_even_empty_and_its_stale_child_goes(tmp_path, owner_alive):
    parent = _parent(tmp_path)
    child = _dated(parent, STALE)
    _sweep(tmp_path)
    assert not child.exists()
    assert parent.is_dir()


def test_an_undated_parent_is_never_entered(tmp_path, owner_gone):
    parent = tmp_path / "vestibule-runs-abcd1234"
    parent.mkdir()
    child = _dated(parent, STALE)
    _sweep(tmp_path)
    assert child.is_dir()
    assert owner_gone == []


def test_another_owners_entries_are_skipped(tmp_path, owner_gone):
    flat = _dated(tmp_path, STALE)
    parent = _parent(tmp_path)
    child = _dated(parent, STALE)
    theirs = os.lstat(flat).st_uid + 1

    _sweep(tmp_path, uid=theirs)
    assert flat.is_dir()
    assert child.is_dir()

    _sweep(tmp_path, uid=theirs - 1)  # the control: the same entries, owned by this uid
    assert not flat.exists()
    assert not parent.exists()


def _link_to_dir(link: Path, target: Path) -> None:
    """A link that needs no special rights: a junction on Windows, a symlink elsewhere."""
    if sys.platform == "win32":
        subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                       check=True, capture_output=True)
    else:
        link.symlink_to(target, target_is_directory=True)


def test_a_link_named_like_a_parent_is_not_followed(tmp_path, owner_gone):
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    victim = _dated(outside, STALE)
    link = root / "vestibule-runs-4242-abcd1234"
    _link_to_dir(link, outside)

    _sweep(root)

    assert victim.is_dir()
    assert (victim / "main.py").is_file()
    assert os.path.lexists(link)
    assert owner_gone == []


def test_a_file_named_like_a_run_dir_or_parent_stays(tmp_path, owner_gone, caplog):
    run_file = tmp_path / f"vestibule-run-{STALE}-abcd1234"
    parent_file = tmp_path / "vestibule-runs-4242-abcd1234"
    run_file.write_text("x")
    parent_file.write_text("x")
    with caplog.at_level(logging.INFO, logger="vestibule.container"):
        _sweep(tmp_path)
    assert run_file.is_file()
    assert parent_file.is_file()
    assert "sweep: removing" not in caplog.text  # rmtree would refuse it, after a false log line


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
def test_a_parent_we_cant_read_doesnt_end_the_pass(tmp_path, owner_alive):
    locked = _parent(tmp_path, suffix="locked12")
    stale = _dated(tmp_path, STALE)
    other = _parent(tmp_path, suffix="other123")
    child = _dated(other, STALE)
    locked.chmod(0)
    try:
        _sweep(tmp_path)
    finally:
        locked.chmod(0o700)
    assert not stale.exists()
    assert not child.exists()


def test_an_entry_that_vanishes_after_the_listing_doesnt_end_the_pass(tmp_path, monkeypatch):
    gone = _dated(tmp_path, STALE, "gone1234")
    stale = _dated(tmp_path, STALE, "stale123")
    real_scandir = os.scandir
    with real_scandir(tmp_path) as entries:
        listing = list(entries)
    shutil.rmtree(gone)

    class Listing:
        def __enter__(self):
            return iter(listing)

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(
        container_mod.os, "scandir",
        lambda path: Listing() if path == str(tmp_path) else real_scandir(path),
    )
    _sweep(tmp_path)
    assert not stale.exists()


# ------------------------------------------------------------- the pid rule (tests 11-12)

@pytest.mark.parametrize(
    ("result", "gone"),
    [(ProcessLookupError(), True), (PermissionError(), False), (None, False)],
)
def test_pid_gone_on_posix(monkeypatch, result, gone):
    def kill(pid, sig):
        assert (pid, sig) == (4242, 0)
        if result is not None:
            raise result

    monkeypatch.setattr(container_mod, "os", SimpleNamespace(name="posix", kill=kill))
    assert container_mod._pid_gone(4242) is gone


def test_pid_gone_never_signals_on_windows(monkeypatch):
    """os.kill(pid, 0) on Windows is a real Ctrl+C (signal 0 is CTRL_C_EVENT), not a probe."""

    def kill(pid, sig):
        raise AssertionError("os.kill must never be called on Windows")

    monkeypatch.setattr(container_mod, "os", SimpleNamespace(name="nt", kill=kill))
    assert container_mod._pid_gone(4242) is False


@pytest.mark.skipif(sys.platform == "win32", reason="the real signal probe is POSIX-only")
def test_pid_gone_for_real():
    assert container_mod._pid_gone(os.getpid()) is False
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    assert container_mod._pid_gone(child.pid) is True


# ------------------------------------------------------------------ wiring (tests 14-20)

class _FakeProc:
    """Any runtime CLI call: it has already exited 0 with no output."""

    def __init__(self) -> None:
        self.returncode = 0
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader()
        self.stdout.feed_eof()
        self.stderr.feed_eof()

    async def communicate(self):
        return b"", b""

    async def wait(self):
        return 0

    def kill(self):
        pass


async def _ok_preflight(image):
    return None


@pytest.fixture()
def temp_root(tmp_path, monkeypatch) -> Path:
    """`tempfile.gettempdir()`, on a folder of this test's own."""
    root = tmp_path / "temp"
    root.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(root))
    return root


@pytest.fixture()
def spawned(monkeypatch) -> list[list[str]]:
    """Every runtime CLI the backend starts, answered at once: `ps` lists no containers."""
    calls: list[list[str]] = []

    async def fake_spawn(*cmd, **kw):
        calls.append(list(cmd))
        return _FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_spawn)
    return calls


@pytest.fixture()
def sweeps(monkeypatch) -> list[SimpleNamespace]:
    """Replaces the scan with a recorder of what it was asked, and from which thread."""
    made: list[SimpleNamespace] = []

    def record(root, now, keep, uid):
        made.append(SimpleNamespace(root=root, keep=keep, uid=uid,
                                    thread=threading.get_ident()))

    monkeypatch.setattr(container_mod, "_sweep_run_dirs", record)
    return made


def _stub_cli(monkeypatch, backend: ContainerBackend) -> list[tuple[str, ...]]:
    calls: list[tuple[str, ...]] = []

    async def cli(*args):
        calls.append(args)
        return ""

    monkeypatch.setattr(backend, "_cli", cli)
    return calls


async def test_a_runs_dir_carries_its_containers_deadline(temp_root, spawned, monkeypatch,
                                                          tmp_path):
    b = ContainerBackend()
    monkeypatch.setattr(b, "_preflight_image", _ok_preflight)
    await b.run("python", "print(1)", 7, Limits(workspace_dir=str(tmp_path / "ws")))
    await b.drain(5)

    cmd = next(c for c in spawned if c[1] == "run")
    label = next(a for a in cmd if a.startswith("vestibule.deadline="))
    source = next(a for a in cmd if a.endswith(":/sandbox:ro")).removesuffix(":/sandbox:ro")
    name = re.fullmatch(r"vestibule-run-([0-9]+)-[a-z0-9_]+", Path(source).name)
    assert name, source
    assert name[1] == label.split("=")[1]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only parent dir")
def test_a_parents_name_carries_this_servers_pid(temp_root):
    parent = ContainerBackend()._run_dir_parent()
    assert parent is not None
    assert re.fullmatch(rf"vestibule-runs-{os.getpid()}-[a-z0-9_]+", Path(parent).name), parent


async def test_the_sweep_runs_at_first_use_and_after_the_run(temp_root, spawned, sweeps,
                                                             monkeypatch, tmp_path):
    """With `ps` listing no containers: the state a hard kill usually leaves."""
    b = ContainerBackend()
    monkeypatch.setattr(b, "_preflight_image", _ok_preflight)

    async def held_execute(*args):
        assert b._reap_task is not None
        await b._reap_task  # the first pass is over, so the second can't merge with it
        return RunResult(stdout="", stderr="", exit_code=0, timed_out=False,
                         isolation="container")

    monkeypatch.setattr(b, "_execute", held_execute)
    await b.run("python", "print(1)", 5, Limits(workspace_dir=str(tmp_path / "ws")))
    await b.drain(5)

    assert [s.root for s in sweeps] == [str(temp_root)] * 2


async def test_the_scan_runs_off_the_event_loop(sweeps, monkeypatch):
    b = ContainerBackend()
    _stub_cli(monkeypatch, b)
    await b._reap_orphans()
    assert len(sweeps) == 1
    assert sweeps[0].thread != threading.get_ident()


async def test_no_sweep_starts_after_shutdown_began(sweeps, monkeypatch):
    b = ContainerBackend()
    _stub_cli(monkeypatch, b)
    b.begin_close()
    await b._reap_orphans()
    assert sweeps == []


async def test_a_failed_sweep_is_logged_and_the_reaper_still_runs(monkeypatch, caplog):
    b = ContainerBackend()
    calls = _stub_cli(monkeypatch, b)

    def boom(root, now, keep, uid):
        raise OSError("no such temp folder")

    monkeypatch.setattr(container_mod, "_sweep_run_dirs", boom)
    with caplog.at_level(logging.ERROR, logger="vestibule.container"):
        await b._reap_orphans()
    assert "sweep: pass failed" in caplog.text
    assert calls and calls[0][0] == "ps"


async def test_the_pass_gets_this_backends_runs_and_this_users_uid(sweeps, monkeypatch):
    b = ContainerBackend()
    _stub_cli(monkeypatch, b)
    b._runs["vestibule-x"] = _RunRecord(os.path.join("some", "where", "vestibule-run-1-abcd1234"))
    await b._reap_orphans()
    assert sweeps[0].keep == {"vestibule-run-1-abcd1234"}
    assert sweeps[0].uid == getattr(os, "getuid", lambda: None)()  # None on Windows
