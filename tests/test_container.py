"""ContainerBackend happy-path tests (M1 step 3). Marked 'docker' — they need a
running daemon and the two pulled images; they skip cleanly anywhere else."""
import shutil
import subprocess

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


@docker_required
async def test_python_hello(limits):
    r = await ContainerBackend().run("python", "print('hi from container')", 30, limits)
    assert r.exit_code == 0
    assert "hi from container" in r.stdout
    assert r.isolation == "container"


@docker_required
async def test_bash_hello(limits):
    r = await ContainerBackend().run("bash", "echo hi-bash", 30, limits)
    assert r.exit_code == 0
    assert "hi-bash" in r.stdout


@docker_required
async def test_node_hello(limits):
    r = await ContainerBackend().run("node", "console.log('hi-node')", 30, limits)
    assert r.exit_code == 0
    assert "hi-node" in r.stdout


@docker_required
async def test_nonzero_exit_and_stderr(limits):
    r = await ContainerBackend().run("python", "import sys; sys.exit(3)", 30, limits)
    assert r.exit_code == 3
    assert r.timed_out is False


@docker_required
async def test_workspace_write_persists_to_host(limits):
    code = "open('/workspace/out.txt', 'w').write('persisted')"
    r = await ContainerBackend().run("python", code, 30, limits)
    assert r.exit_code == 0
    assert (limits.workspace_path / "out.txt").read_text() == "persisted"


@docker_required
async def test_network_is_gone(limits):
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
    r = await ContainerBackend().run("python", code, 30, limits)
    assert "NO-NETWORK" in r.stdout
    assert "CONNECTED" not in r.stdout


@docker_required
async def test_rootfs_is_read_only(limits):
    code = (
        "try:\n"
        "    open('/usr/bin/evil', 'w').write('x')\n"
        "    print('WROTE')\n"
        "except OSError:\n"
        "    print('READ-ONLY')\n"
    )
    r = await ContainerBackend().run("python", code, 30, limits)
    assert "READ-ONLY" in r.stdout


@docker_required
async def test_timeout_kills_container(limits):
    r = await ContainerBackend().run("python", "import time; time.sleep(300)", 3, limits)
    assert r.timed_out is True
    # No survivor: the named container must be gone (racing --rm is fine, both end gone).
    ps = subprocess.run(
        ["docker", "ps", "-a", "--filter", "label=vestibule.run=1", "--format", "{{.Names}}"],
        capture_output=True, text=True, timeout=15, stdin=subprocess.DEVNULL,
    )
    assert ps.stdout.strip() == ""


@docker_required
async def test_output_flood_is_capped_and_killed(limits):
    r = await ContainerBackend().run(
        "python", "print('x' * 1000000)\n" * 200, 60, limits
    )
    # Collection cap is 2x max_output_bytes
    assert len(r.stdout) <= 2 * limits.max_output_bytes
