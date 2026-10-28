"""ContainerBackend tests (M1 steps 3–6). Marked 'docker' — they need a running
daemon and BOTH digest-pinned sandbox images pulled locally; anywhere else they skip
cleanly, and the skip reason carries the exact `docker pull` commands. Nothing here
ever pulls from the network. The skip rule and CLI helpers live in tests/dockerutil.py.

Step-4 additions (plan §6 tests 6–10): cancellation cleanup, deadline-label reaping
(remove past-deadline, spare future-deadline, spare unlabeled), and 4-way concurrency.
Step-6 addition (plan §7 test 6): the pinned refs pass the real preflight and hold the
interpreter versions the config comment claims.
"""
import asyncio
import contextlib
import logging
import re
import secrets
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

from dockerutil import docker_required, plant, ps_names, remove

from vestibule.backends.container import ContainerBackend
from vestibule.config import Limits

pytestmark = pytest.mark.docker


@pytest.fixture()
def limits(tmp_path):
    # M1's container path: `auto` would try native first on Linux (M2 step 6, S6-D8)
    return Limits(workspace_dir=str(tmp_path / "ws"), backend="container")


@pytest.fixture()
async def backend():
    """A backend whose detached finisher/reap tasks are drained before the test
    loop closes."""
    b = ContainerBackend()
    yield b
    await asyncio.gather(*list(b._finishers), return_exceptions=True)
    if b._reap_task is not None:
        with contextlib.suppress(Exception):
            await b._reap_task


@pytest.fixture()
def run_dirs(monkeypatch) -> list[Path]:
    """The temp dirs this test's runs create. Checks look at these, never at the shared
    %TEMP% as a whole: a live Vestibule server on this machine writes there too."""
    made: list[Path] = []
    real = tempfile.mkdtemp

    def spy(*args, **kwargs):
        path = Path(real(*args, **kwargs))
        if path.name.startswith("vestibule-run-"):  # not the POSIX per-backend parent
            made.append(path)
        return str(path)

    monkeypatch.setattr(tempfile, "mkdtemp", spy)
    return made


# ---------------------------------------------------------------- step 3: happy path

@docker_required
async def test_python_hello(backend, limits):
    r = await backend.run("python", "print('hi from container')", 30, limits)
    assert r.exit_code == 0
    assert "hi from container" in r.stdout
    assert r.isolation == "container"


@docker_required
async def test_bash_hello(backend, limits):
    r = await backend.run("bash", "echo hi-bash", 30, limits)
    assert r.exit_code == 0
    assert "hi-bash" in r.stdout


@docker_required
async def test_node_hello(backend, limits):
    r = await backend.run("node", "console.log('hi-node')", 30, limits)
    assert r.exit_code == 0
    assert "hi-node" in r.stdout


@docker_required
async def test_nonzero_exit_and_stderr(backend, limits):
    r = await backend.run("python", "import sys; sys.exit(3)", 30, limits)
    assert r.exit_code == 3
    assert r.timed_out is False


@docker_required
async def test_workspace_write_persists_to_host(backend, limits):
    code = "open('/workspace/out.txt', 'w').write('persisted')"
    r = await backend.run("python", code, 30, limits)
    assert r.exit_code == 0
    assert (limits.workspace_path / "out.txt").read_text() == "persisted"


@docker_required
async def test_network_is_gone(backend, limits):
    # Full network acceptance (TCP + DNS) is criterion 2 in step 7; this is the
    # step-3 smoke version.
    code = (
        "import socket\n"
        "try:\n"
        "    socket.create_connection(('1.1.1.1', 80), timeout=3)\n"
        "    print('CONNECTED')\n"
        "except OSError:\n"
        "    print('NO-NETWORK')\n"
    )
    r = await backend.run("python", code, 30, limits)
    assert "NO-NETWORK" in r.stdout
    assert "CONNECTED" not in r.stdout


@docker_required
async def test_rootfs_is_read_only(backend, limits):
    code = (
        "try:\n"
        "    open('/usr/bin/evil', 'w').write('x')\n"
        "    print('WROTE')\n"
        "except OSError:\n"
        "    print('READ-ONLY')\n"
    )
    r = await backend.run("python", code, 30, limits)
    assert "READ-ONLY" in r.stdout


@docker_required
async def test_timeout_kills_container(backend, limits):
    r = await backend.run("python", "import time; time.sleep(300)", 3, limits)
    assert r.timed_out is True
    # The kill is detached (Codex budget fix 2026-07-05): the result returns first,
    # the finisher removes the container right after — poll until it settles.
    # No survivor from THIS backend (label-global emptiness would race other tests).
    deadline = time.time() + 20
    while time.time() < deadline:
        if ps_names(f"label=vestibule.owner={backend._owner}") == "":
            break
        await asyncio.sleep(0.5)
    assert ps_names(f"label=vestibule.owner={backend._owner}") == ""


@docker_required
async def test_pinned_images_pass_preflight_and_hold_expected_versions(backend, limits):
    """M1 step 6 (plan §7 test 6): both shipped digest refs pass the real preflight, and
    the bytes behind them are what the config comment says — python 3.12, node 22.
    Docker does not check a tag next to a digest, so this test is what keeps the
    `python:3.12-slim` / `node:22-slim` labels honest across re-pins."""
    from vestibule.config import DEFAULT_IMAGE_NODE, DEFAULT_IMAGE_PYTHON

    assert (limits.image_python, limits.image_node) == (DEFAULT_IMAGE_PYTHON, DEFAULT_IMAGE_NODE)
    await backend._preflight_image(DEFAULT_IMAGE_PYTHON)  # raises RunRefusedError if unusable
    await backend._preflight_image(DEFAULT_IMAGE_NODE)

    r = await backend.run(
        "python", "import sys; print('v=%d.%d' % sys.version_info[:2])", 30, limits
    )
    assert (r.exit_code, r.isolation) == (0, "container"), r.stderr
    assert re.search(r"^v=3\.12$", r.stdout, re.M), r.stdout

    r = await backend.run("node", "console.log('v=' + process.versions.node)", 30, limits)
    assert (r.exit_code, r.isolation) == (0, "container"), r.stderr
    assert re.search(r"^v=22\.\d+\.\d+$", r.stdout, re.M), r.stdout


@docker_required
async def test_output_flood_is_capped_and_killed(backend, limits):
    r = await backend.run(
        "python", "print('x' * 1000000)\n" * 200, 60, limits
    )
    # Collection cap is 2x max_output_bytes
    assert len(r.stdout) <= 2 * limits.max_output_bytes


# ------------------------------------------------------------- step 4: lifecycle (§4)

@docker_required
async def test_cancellation_cleans_up_container_and_tmpdir(backend, limits, run_dirs):
    """Plan §6 test 6 — the headline §4.5 behavior: a cancelled request still gets
    its container removed and its temp dir deleted (detached, shielded cleanup)."""
    task = asyncio.create_task(
        backend.run("python", "import time; time.sleep(300)", 60, limits)
    )
    deadline = time.time() + 60
    while time.time() < deadline:  # wait until the container is actually up
        if ps_names(f"label=vestibule.owner={backend._owner}", all_states=False):
            break
        await asyncio.sleep(0.5)
    else:
        pytest.fail("container never started")

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task  # returns immediately; cleanup continues as a detached task

    deadline = time.time() + 20  # poll: the detached cleanup + --rm/rm -f settle
    while time.time() < deadline:
        gone = ps_names(f"label=vestibule.owner={backend._owner}") == ""
        if gone and not any(d.exists() for d in run_dirs):
            break
        await asyncio.sleep(0.5)
    assert ps_names(f"label=vestibule.owner={backend._owner}") == ""
    assert run_dirs and [d for d in run_dirs if d.exists()] == []


@docker_required
async def test_reaper_removes_past_deadline_container(backend):
    """Plan §6 test 7 — a *running* container with deadline=1 (epoch 1970) is removed:
    proves the rule is state-independent (acceptance criterion 8, strengthened)."""
    name = f"vestibule-plant-{secrets.token_hex(4)}"
    plant(name, {"vestibule.run": "1", "vestibule.deadline": "1"})
    try:
        await backend._reap_orphans()
        assert ps_names(f"name={name}") == ""
    finally:
        remove(name)


@docker_required
async def test_reaper_spares_future_deadline_container(backend):
    """Plan §6 test 8 — a live run (future deadline) is untouchable, whatever the
    reaper's own config says."""
    name = f"vestibule-plant-{secrets.token_hex(4)}"
    plant(name, {"vestibule.run": "1", "vestibule.deadline": str(int(time.time()) + 100_000)})
    try:
        await backend._reap_orphans()
        assert ps_names(f"name={name}") == name
    finally:
        remove(name)


@docker_required
async def test_reaper_spares_and_warns_on_missing_deadline(backend, caplog):
    """Plan §6 test 9 — no deadline label => never reaped, warned loudly."""
    name = f"vestibule-plant-{secrets.token_hex(4)}"
    plant(name, {"vestibule.run": "1"})
    try:
        with caplog.at_level(logging.WARNING, logger="vestibule.container"):
            await backend._reap_orphans()
        assert ps_names(f"name={name}") == name
        assert "no parseable vestibule.deadline" in caplog.text
    finally:
        remove(name)


@docker_required
async def test_four_simultaneous_runs_succeed(backend, limits):
    """Plan §6 test 10 — acceptance criterion 7: 4 concurrent runs, distinct
    containers (unique names by construction), all succeed."""
    results = await asyncio.gather(
        *(backend.run("python", f"print('tok-{i}')", 60, limits) for i in range(4))
    )
    for i, r in enumerate(results):
        assert r.exit_code == 0
        assert f"tok-{i}" in r.stdout


# ------------------------------------------------------------ step 5: selection (§5)


async def _drain(warden) -> None:
    await warden.drain()


@docker_required
async def test_real_selection_reports_container(limits):
    """This machine's Docker passes the full profile: verdict `container`, and a
    post-probe run through the selected warden reports it honestly."""
    from vestibule.backends.select import BackendSelector

    sel = await BackendSelector().get(limits)
    try:
        # S7-D3: plain `container` now also means the probe read every soft limit
        # back from the container's own cgroup and found it applied.
        assert (sel.verdict, sel.detail) == ("container", None)
        r = await sel.warden.run("python", "print('post-probe')", 30, limits)
        assert r.exit_code == 0
        assert r.isolation == "container"
        assert "post-probe" in r.stdout
    finally:
        await _drain(sel.warden)


@docker_required
async def test_probe_leaves_no_trace(limits):
    """Plan §6: the probe cleans its workspace file and its container goes through
    the normal detached cleanup."""
    from vestibule.backends.select import BackendSelector

    sel = await BackendSelector().get(limits)
    try:
        # The probe file name is random per probe (step 7a): no probe file of any name
        # may be left behind.
        assert list(limits.workspace_path.glob(".vestibule-probe*")) == []
        deadline = time.time() + 20
        while time.time() < deadline:
            if ps_names(f"label=vestibule.owner={sel.warden._owner}") == "":
                break
            await asyncio.sleep(0.5)
        assert ps_names(f"label=vestibule.owner={sel.warden._owner}") == ""
    finally:
        await _drain(sel.warden)


@docker_required
async def test_selection_with_read_only_workspace(tmp_path):
    """RO mode: the probe demands that reads work AND writes fail."""
    from vestibule.backends.select import BackendSelector

    lim = Limits(workspace_dir=str(tmp_path / "ws-ro"), workspace_ro=True, backend="container")
    sel = await BackendSelector().get(lim)
    try:
        assert sel.verdict == "container"
    finally:
        await _drain(sel.warden)


@docker_required
async def test_failed_selection_drains_and_leaves_no_tasks(tmp_path, monkeypatch):
    """Regression (build-time hang): a FAILED selection must drain its rejected
    probe backends' detached cleanup tasks. Leaked ones get mass-cancelled at
    event-loop shutdown, which can orphan a subprocess spawn waiter on Windows
    (CPython proactor wart) and deadlock loop close."""
    import vestibule.backends.select as select_mod
    from vestibule.backends.base import RunRefusedError
    from vestibule.backends.select import BackendSelector

    monkeypatch.setattr(select_mod, "_PROBE_RW", "exit 7")  # both probes run for real; both fail
    with pytest.raises(RunRefusedError, match="cannot enforce"):
        await BackendSelector().get(Limits(workspace_dir=str(tmp_path / "ws"),
                                           backend="container"))

    leftovers = [
        t for t in asyncio.all_tasks()
        if t is not asyncio.current_task() and not t.done()
    ]
    assert leftovers == []


# ------------------------------------------------- step 7a: probe name + self-deadline


@docker_required
async def test_planted_probe_name_cannot_break_selection(tmp_path):
    """Plan §4.1 regression: a guest that once created a directory at the probe's old
    fixed name made every later selection fail — forever, since the workspace
    persists. The probe file name is now random per probe."""
    from vestibule.backends.select import BackendSelector

    lim = Limits(workspace_dir=str(tmp_path / "ws"), backend="container")
    (lim.workspace_path / ".vestibule-probe").mkdir(parents=True)
    sel = await BackendSelector().get(lim)
    try:
        assert sel.verdict == "container"
        assert [p.name for p in lim.workspace_path.iterdir()] == [".vestibule-probe"]
    finally:
        await _drain(sel.warden)


@docker_required
async def test_container_ends_itself_without_a_server(tmp_path, monkeypatch):
    """S7-D5: the exact run profile, launched with no Vestibule server watching it
    (as after a hard kill): the in-container `timeout` ends the guest at its own
    deadline and the daemon's --rm removes the container."""
    import vestibule.backends.container as container_mod

    monkeypatch.setattr(container_mod, "_DEADLINE_SLACK_S", 2)  # deadline = 1 + 2 s
    sandbox = tmp_path / "sbx"
    sandbox.mkdir()
    (sandbox / "main.py").write_text("import time\ntime.sleep(300)\n", newline="\n")
    run_id = secrets.token_hex(8)
    lim = Limits(workspace_dir=str(tmp_path / "ws"))
    lim.workspace_path.mkdir(parents=True)
    cmd = ContainerBackend()._build_command(
        run_id, "python", lim, str(sandbox), str(lim.workspace_path),
        int(time.time()) + 60, 1,
    )
    t0 = time.monotonic()
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=60, stdin=subprocess.DEVNULL)
        elapsed = time.monotonic() - t0
        assert proc.returncode == 137, proc.stderr  # killed by its own deadline
        assert elapsed < 30  # nowhere near the guest's 300 s sleep
        deadline = time.time() + 10
        while ps_names(f"name=^vestibule-{run_id}$") and time.time() < deadline:
            await asyncio.sleep(0.25)
        assert ps_names(f"name=^vestibule-{run_id}$") == ""
    finally:
        remove(f"vestibule-{run_id}")


@docker_required
async def test_run_cancelled_at_spawn_leaves_no_container(
    backend, limits, monkeypatch, run_dirs
):
    """7a critic round 2: a run cancelled while `docker run` is still creating its
    container can have the finisher's `kill` and `rm -f` both miss, after which the
    CLI starts the container anyway. What stops that late guest is `_cleanup`
    removing the script dir right after: it finds no script, exits, and --rm removes
    it. Without that, this 300 s guest would still be running at the end."""
    spawned = asyncio.Event()
    real_spawn = asyncio.create_subprocess_exec

    async def spy(*cmd, **kw):
        proc = await real_spawn(*cmd, **kw)
        if len(cmd) > 1 and cmd[1] == "run":
            spawned.set()
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spy)
    for _ in range(3):  # the race is ~50/50 per try; any lost one must still end clean
        spawned.clear()
        task = asyncio.create_task(
            backend.run("python", "import time; time.sleep(300)", 60, limits)
        )
        await asyncio.wait_for(spawned.wait(), 60)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    # An empty listing proves nothing while a cancelled `docker run` CLI may still
    # create its container: wait until every CLI has exited (drain covers the tracked
    # CLI reaps). Before this, the poll could pass on its first, vacuous `ps` and the
    # assert below then catch a late container in its sub-second life (2026-10-14).
    assert await backend.drain(60)
    deadline = time.time() + 20
    while ps_names(f"label=vestibule.owner={backend._owner}") and time.time() < deadline:
        await asyncio.sleep(0.5)
    assert ps_names(f"label=vestibule.owner={backend._owner}") == ""
    # A late container's start re-creates its deleted bind source on Docker Desktop;
    # the CLI reap removes it again once the CLI has exited (7b gate finding).
    assert len(run_dirs) == 3 and [d for d in run_dirs if d.exists()] == []
