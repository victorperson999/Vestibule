"""Total-argument-validation tests for server handlers (M1 finding 18 / criterion 13).

Calls the handlers directly — no MCP transport, no Docker. Malformed input must come
back as a legible 'Blocked:' TextContent, never an exception.
"""
import pytest

import vestibule.server as server
from vestibule.config import Limits


async def test_timeout_string_is_blocked():
    out = await server._handle_run_code(
        {"language": "python", "code": "print(1)", "timeout_seconds": "abc"}
    )
    assert out[0].text.startswith("Blocked:")
    assert "timeout_seconds" in out[0].text


async def test_timeout_bool_is_blocked():
    out = await server._handle_run_code(
        {"language": "python", "code": "print(1)", "timeout_seconds": True}
    )
    assert out[0].text.startswith("Blocked:")


async def test_timeout_float_is_blocked():
    out = await server._handle_run_code(
        {"language": "python", "code": "print(1)", "timeout_seconds": 9.5}
    )
    assert out[0].text.startswith("Blocked:")


async def test_non_string_code_is_blocked():
    out = await server._handle_run_code({"language": "python", "code": 123})
    assert out[0].text.startswith("Blocked:")


async def test_non_string_language_is_blocked():
    out = await server._handle_run_code({"language": 5, "code": "print(1)"})
    assert out[0].text.startswith("Blocked:")


async def test_missing_args_are_blocked():
    out = await server._handle_run_code({})
    assert out[0].text.startswith("Blocked:")


async def test_call_tool_survives_none_arguments():
    out = await server.call_tool("run_code", None)
    assert out[0].text.startswith("Blocked:")


@pytest.fixture()
def patched_workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "LIMITS", Limits(workspace_dir=str(tmp_path)))
    return tmp_path


async def test_read_workspace_lists_root(patched_workspace):
    (patched_workspace / "a.txt").write_text("A", encoding="utf-8")
    out = await server._handle_read_workspace({"path": "."})
    assert "a.txt" in out[0].text


async def test_read_workspace_blocks_escape(patched_workspace):
    out = await server._handle_read_workspace({"path": "../outside"})
    assert out[0].text.startswith("Blocked:")


async def test_read_workspace_non_string_path_blocked(patched_workspace):
    out = await server._handle_read_workspace({"path": 42})
    assert out[0].text.startswith("Blocked:")
