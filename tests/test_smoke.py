"""Smoke test for the M0 naive backend. Runs without an agent."""
import pytest

from vestibule.backends.naive import NaiveBackend
from vestibule.config import Limits


@pytest.mark.asyncio
async def test_python_hello():
    result = await NaiveBackend().run("python", "print('hi')", 10, Limits())
    assert result.exit_code == 0
    assert "hi" in result.stdout
    assert result.isolation == "none"


@pytest.mark.asyncio
async def test_timeout_is_enforced():
    code = "import time; time.sleep(30)"
    result = await NaiveBackend().run("python", code, 1, Limits())
    assert result.timed_out is True


@pytest.mark.asyncio
async def test_unknown_language_is_clean():
    result = await NaiveBackend().run("ruby", "puts 1", 5, Limits())
    assert result.exit_code == 127


@pytest.mark.asyncio
async def test_spawn_failure_reports_isolation_none(monkeypatch):
    # An interpreter that vanishes between the PATH check and the spawn must still
    # report the honest no-isolation state: server honesty logic keys off the exact
    # isolation strings, so this path may never emit anything outside the enum.
    async def raise_not_found(*args: object, **kwargs: object) -> object:
        raise FileNotFoundError("interpreter vanished")

    monkeypatch.setattr(
        "vestibule.backends.naive.asyncio.create_subprocess_exec", raise_not_found
    )
    result = await NaiveBackend().run("python", "print('hi')", 5, Limits())
    assert result.exit_code == 127
    assert result.timed_out is False
    assert result.isolation == "none"


@pytest.mark.asyncio
async def test_guest_stdin_is_closed_not_inherited():
    # Guest stdin must be DEVNULL: inheriting the server's stdin would expose the
    # MCP JSON-RPC channel to untrusted code (and hangs the child on Windows).
    code = "import sys; data = sys.stdin.read(); print(f'read {len(data)} bytes')"
    result = await NaiveBackend().run("python", code, 5, Limits())
    assert result.timed_out is False
    assert result.exit_code == 0
    assert "read 0 bytes" in result.stdout
