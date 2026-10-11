"""Shared helpers for the Docker-marked test files (test_container, test_stdio_e2e,
test_acceptance): one skip rule, and small read/plant/remove CLI wrappers.

The skip rule: these tests need a running daemon and BOTH digest-pinned sandbox images
pulled locally. Anywhere else they skip cleanly with the exact `docker pull` commands as
the reason. Nothing here ever pulls from the network.
"""
import shutil
import subprocess

import pytest

from vestibule.config import Limits


def docker(*args: str) -> int | None:
    """Exit code of a short read-only docker CLI call; None if it could not run."""
    try:
        r = subprocess.run(["docker", *args], capture_output=True, timeout=10,
                           stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.returncode


def docker_skip_reason() -> str | None:
    """None if the Docker-marked tests can run here, else why not (with the fix).

    Daemon up but images missing must SKIP, not fail: the backend would refuse every
    run (preflight), which is correct product behavior but says nothing about the code.
    """
    if shutil.which("docker") is None:
        return "docker CLI not found"
    if docker("version", "--format", "{{.Server.Version}}") != 0:
        return "no reachable Docker daemon"
    lim = Limits()
    missing = [ref for ref in (lim.image_python, lim.image_node)
               if docker("image", "inspect", "--format", "{{.Id}}", ref) != 0]
    if missing:
        return "pinned sandbox image(s) not pulled; run: " + "; ".join(
            f"docker pull {ref}" for ref in missing
        )
    return None


SKIP_REASON = docker_skip_reason()
docker_required = pytest.mark.skipif(SKIP_REASON is not None, reason=SKIP_REASON or "")


def ps_names(*filters: str, all_states: bool = True) -> str:
    cmd = ["docker", "ps", "--format", "{{.Names}}"]
    if all_states:
        cmd.insert(2, "-a")
    for f in filters:
        cmd += ["--filter", f]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=15,
                       stdin=subprocess.DEVNULL)
    return r.stdout.strip()


def plant(name: str, labels: dict[str, str]) -> None:
    """Start a labeled sleeper container (no --rm: removal is the subject under test).
    `--pull never`: tests must never touch the network (a missing image skips instead)."""
    cmd = ["docker", "run", "-d", "--pull", "never", "--name", name, "--network", "none"]
    for k, v in labels.items():
        cmd += ["--label", f"{k}={v}"]
    cmd += [Limits().image_python, "sleep", "300"]
    subprocess.run(cmd, check=True, capture_output=True, timeout=60,
                   stdin=subprocess.DEVNULL)


def remove(name: str) -> None:
    subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=15,
                   stdin=subprocess.DEVNULL)
