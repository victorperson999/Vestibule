"""ContainerBackend tests (M1 steps 3–4). Marked 'docker' — they need a running
daemon and the two pulled images; they skip cleanly anywhere else.

Step-4 additions (plan §6 tests 6–10): cancellation cleanup, deadline-label reaping
(remove past-deadline, spare future-deadline, spare unlabeled), and 4-way concurrency.
"""
import asyncio
import contextlib
import logging
import secrets
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

from vestibule.backends.container import ContainerBackend
from vestibule.config import Limits


def _docker_ready() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        r = subprocess.run(
            ["docker", "version", "--format", "{{.Server.Version}}"],
            capture_output=True, timeout=10, stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0


pytestmark = pytest.mark.docker

docker_required = pytest.mark.skipif(not _docker_ready(), reason="no reachable Docker daemon")


@pytest.fixture()
def limits(tmp_path):
    return Limits(workspace_dir=str(tmp_path / "ws"))


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


def _ps_names(*filters: str, all_states: bool = True) -> str:
    cmd = ["docker", "ps", "--format", "{{.Names}}"]
    if all_states:
        cmd.insert(2, "-a")
    for f in filters:
        cmd += ["--filter", f]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=15,
                       stdin=subprocess.DEVNULL)
    return r.stdout.strip()


def _plant(name: str, labels: dict[str, str]) -> None:
    """Start a labeled sleeper container (no --rm: removal is the subject under test)."""
    cmd = ["docker", "run", "-d", "--name", name, "--network", "none"]
    for k, v in labels.items():
        cmd += ["--label", f"{k}={v}"]
    cmd += [Limits().image_python, "sleep", "300"]
    subprocess.run(cmd, check=True, capture_output=True, timeout=60,
                   stdin=subprocess.DEVNULL)


def _remove(name: str) -> None:
    subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=15,
                   stdin=subprocess.DEVNULL)


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
    # No survivor from THIS backend (label-global emptiness would race other tests).
    assert _ps_names(f"label=vestibule.owner={backend._owner}") == ""


@docker_required
async def test_output_flood_is_capped_and_killed(backend, limits):
    r = await backend.run(
        "python", "print('x' * 1000000)\n" * 200, 60, limits
    )
    # Collection cap is 2x max_output_bytes
    assert len(r.stdout) <= 2 * limits.max_output_bytes


# ------------------------------------------------------------- step 4: lifecycle (§4)

@docker_required
async def test_cancellation_cleans_up_container_and_tmpdir(backend, limits):
    """Plan §6 test 6 — the headline §4.5 behavior: a cancelled request still gets
    its container removed and its temp dir deleted (detached, shielded cleanup)."""
    tmp_root = Path(tempfile.gettempdir())
    before = set(tmp_root.glob("vestibule-run-*"))

    task = asyncio.create_task(
        backend.run("python", "import time; time.sleep(300)", 60, limits)
    )
    deadline = time.time() + 60
    while time.time() < deadline:  # wait until the container is actually up
        if _ps_names(f"label=vestibule.owner={backend._owner}", all_states=False):
            break
        await asyncio.sleep(0.5)
    else:
        pytest.fail("container never started")

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task  # returns immediately; cleanup continues as a detached task

    deadline = time.time() + 20  # poll: the detached cleanup + --rm/rm -f settle
    while time.time() < deadline:
        gone = _ps_names(f"label=vestibule.owner={backend._owner}") == ""
        no_new_dirs = not (set(tmp_root.glob("vestibule-run-*")) - before)
        if gone and no_new_dirs:
            break
        await asyncio.sleep(0.5)
    assert _ps_names(f"label=vestibule.owner={backend._owner}") == ""
    assert set(tmp_root.glob("vestibule-run-*")) - before == set()


@docker_required
async def test_reaper_removes_past_deadline_container(backend):
    """Plan §6 test 7 — a *running* container with deadline=1 (epoch 1970) is removed:
    proves the rule is state-independent (acceptance criterion 8, strengthened)."""
    name = f"vestibule-plant-{secrets.token_hex(4)}"
    _plant(name, {"vestibule.run": "1", "vestibule.deadline": "1"})
    try:
        await backend._reap_orphans()
        assert _ps_names(f"name={name}") == ""
    finally:
        _remove(name)


@docker_required
async def test_reaper_spares_future_deadline_container(backend):
    """Plan §6 test 8 — a live run (future deadline) is untouchable, whatever the
    reaper's own config says."""
    name = f"vestibule-plant-{secrets.token_hex(4)}"
    _plant(name, {"vestibule.run": "1", "vestibule.deadline": str(int(time.time()) + 100_000)})
    try:
        await backend._reap_orphans()
        assert _ps_names(f"name={name}") == name
    finally:
        _remove(name)


@docker_required
async def test_reaper_spares_and_warns_on_missing_deadline(backend, caplog):
    """Plan §6 test 9 — no deadline label => never reaped, warned loudly."""
    name = f"vestibule-plant-{secrets.token_hex(4)}"
    _plant(name, {"vestibule.run": "1"})
    try:
        with caplog.at_level(logging.WARNING, logger="vestibule.container"):
            await backend._reap_orphans()
        assert _ps_names(f"name={name}") == name
        assert "no parseable vestibule.deadline" in caplog.text
    finally:
        _remove(name)


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
