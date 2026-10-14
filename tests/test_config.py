"""Config validation and the server's reported version
(docs/plans/M1/M1-followup-config-version.md). Docker-free.

A bad VESTIBULE_* setting must never be silently replaced by its default, and must never
crash startup. The server starts, and every tool call returns a `Blocked:` message that
names the setting, so the agent can tell the user what to fix.
"""
import os
import tomllib
from pathlib import Path

import pytest
from test_stdio_e2e import SERVER_EXE, StdioServer

import vestibule
import vestibule.server as server
from vestibule.config import ConfigError, Limits

NUMBER_SETTINGS = [
    "VESTIBULE_MAX_TIMEOUT_S", "VESTIBULE_DEFAULT_TIMEOUT_S", "VESTIBULE_MAX_CODE_BYTES",
    "VESTIBULE_MAX_OUTPUT_BYTES", "VESTIBULE_MEM_MB", "VESTIBULE_PIDS_MAX",
    "VESTIBULE_CPU_PCT", "VESTIBULE_MAX_CONCURRENT", "VESTIBULE_TMPFS_MB",
]


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    # The dev shell's own settings must not leak into these tests.
    for name in list(os.environ):
        if name.startswith("VESTIBULE_"):
            monkeypatch.delenv(name)


def test_unset_or_blank_settings_use_the_defaults(monkeypatch):
    monkeypatch.setenv("VESTIBULE_MEM_MB", "")
    monkeypatch.setenv("VESTIBULE_PIDS_MAX", "   ")
    monkeypatch.setenv("VESTIBULE_WORKSPACE_RO", " ")
    assert Limits.from_env() == Limits()


def test_valid_numbers_are_used(monkeypatch):
    monkeypatch.setenv("VESTIBULE_MEM_MB", "512")
    monkeypatch.setenv("VESTIBULE_MAX_TIMEOUT_S", " 120 ")  # surrounding whitespace is fine
    monkeypatch.setenv("VESTIBULE_MAX_CONCURRENT", "1")
    lim = Limits.from_env()
    assert (lim.mem_mb, lim.max_timeout_s, lim.max_concurrent) == (512, 120, 1)


@pytest.mark.parametrize("value, expected", [
    ("1", True), ("TRUE", True), ("yes", True), ("On", True),
    ("0", False), ("false", False), ("No", False), ("OFF", False),
])
def test_on_off_values(monkeypatch, value, expected):
    monkeypatch.setenv("VESTIBULE_WORKSPACE_RO", value)
    assert Limits.from_env().workspace_ro is expected


@pytest.mark.parametrize("value", [
    "512m", "-1", "0", "00", "1.5", "+5", "1_000", "0x10",
    "²",  # passes isdigit() but crashes int(): the old code died at import
    "٣",  # an Arabic-Indic digit that int() accepts as 3
])
def test_invalid_number_is_reported_not_replaced(monkeypatch, value):
    monkeypatch.setenv("VESTIBULE_MEM_MB", value)
    with pytest.raises(ConfigError) as e:
        Limits.from_env()
    assert e.value.problems == [f"VESTIBULE_MEM_MB={value!r} is not a positive whole number"]


@pytest.mark.parametrize("value, shown", [
    ("1" * 19, repr("1" * 19)),
    # Beyond int()'s default 4300-digit limit: once a startup crash. The cap is checked
    # before int(), so the verdict is the same whatever that limit is set to.
    ("1" * 5000, f"{'1' * 40!r}... (5000 characters)"),
])
def test_number_with_too_many_digits_is_too_large(monkeypatch, value, shown):
    monkeypatch.setenv("VESTIBULE_MEM_MB", value)
    with pytest.raises(ConfigError) as e:
        Limits.from_env()
    assert e.value.problems == [
        f"VESTIBULE_MEM_MB={shown} is too large (more than 18 digits)"
    ]


@pytest.mark.parametrize("value, expected", [
    ("9" * 18, int("9" * 18)),
    ("0" * 30 + "512", 512),  # leading zeros don't count toward the cap
])
def test_digit_cap_counts_significant_digits(monkeypatch, value, expected):
    monkeypatch.setenv("VESTIBULE_MEM_MB", value)
    assert Limits.from_env().mem_mb == expected


@pytest.mark.parametrize("name", NUMBER_SETTINGS)
def test_every_number_setting_is_validated(monkeypatch, name):
    monkeypatch.setenv(name, "0")
    with pytest.raises(ConfigError) as e:
        Limits.from_env()
    assert e.value.problems == [f"{name}='0' is not a positive whole number"]


@pytest.mark.parametrize("value", ["ture", "2", "enabled"])
def test_invalid_on_off_is_reported_not_read_as_off(monkeypatch, value):
    # A typo used to mean "off": a writable workspace the user asked to be read-only.
    monkeypatch.setenv("VESTIBULE_WORKSPACE_RO", value)
    with pytest.raises(ConfigError) as e:
        Limits.from_env()
    assert e.value.problems == [
        f"VESTIBULE_WORKSPACE_RO={value!r} is not on or off "
        "(use 1/true/yes/on or 0/false/no/off)"
    ]


def test_every_invalid_setting_is_reported_at_once(monkeypatch):
    monkeypatch.setenv("VESTIBULE_MEM_MB", "abc")
    monkeypatch.setenv("VESTIBULE_WORKSPACE_RO", "ture")
    monkeypatch.setenv("VESTIBULE_MAX_CONCURRENT", "0")
    with pytest.raises(ConfigError) as e:
        Limits.from_env()
    assert [p.split("=")[0] for p in e.value.problems] == [
        "VESTIBULE_MEM_MB", "VESTIBULE_WORKSPACE_RO", "VESTIBULE_MAX_CONCURRENT",
    ]


def test_load_limits_falls_back_to_defaults_with_a_message(monkeypatch):
    monkeypatch.setenv("VESTIBULE_MEM_MB", "512m")
    limits, error = server._load_limits()
    assert limits == Limits()
    assert error is not None
    assert "VESTIBULE_MEM_MB='512m' is not a positive whole number" in error
    assert "restart" in error and "Nothing was run" in error


def test_load_limits_with_a_good_config_has_no_error(monkeypatch):
    monkeypatch.setenv("VESTIBULE_MEM_MB", "512")
    limits, error = server._load_limits()
    assert error is None
    assert limits.mem_mb == 512


class _NoSelector:
    async def get(self, limits):
        raise AssertionError("a misconfigured server must not select a backend")


@pytest.mark.parametrize("tool, arguments", [
    ("run_code", {"language": "python", "code": "print(1)"}),
    ("read_workspace", {"path": "."}),
])
async def test_bad_config_blocks_every_tool(monkeypatch, tmp_path, tool, arguments):
    ws = tmp_path / "ws"
    monkeypatch.setattr(server, "LIMITS", Limits(workspace_dir=str(ws)))
    monkeypatch.setattr(server, "CONFIG_ERROR", "Vestibule's configuration is invalid: X")
    monkeypatch.setattr(server, "SELECTOR", _NoSelector())
    out = await server.call_tool(tool, arguments)
    assert [c.text for c in out] == ["Blocked: Vestibule's configuration is invalid: X"]
    assert not ws.exists()  # the workspace was not even created


def test_server_reports_vestibule_version_not_the_sdks():
    assert server.server.create_initialization_options().server_version == vestibule.__version__
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    with pyproject.open("rb") as f:
        assert tomllib.load(f)["project"]["version"] == vestibule.__version__


@pytest.mark.skipif(not SERVER_EXE.exists(), reason=f"console script not found: {SERVER_EXE}")
async def test_stdio_bad_config_starts_and_blocks(tmp_path):
    """The real console script over a real pipe. Needs no Docker: nothing may run."""
    # A bad config discards VESTIBULE_WORKSPACE with the rest, so the server's workspace
    # is the default under the home dir. Point home at tmp so "nothing was created" is a
    # real check and never touches the developer's own workspace.
    home = tmp_path / "home"
    home.mkdir()
    srv = await StdioServer.start(tmp_path / "ws", {
        "VESTIBULE_MEM_MB": "²", "VESTIBULE_MAX_CONCURRENT": "0",
        "USERPROFILE": str(home), "HOME": str(home),
    })
    try:
        init = await srv.initialize()
        assert init["serverInfo"] == {"name": "vestibule", "version": vestibule.__version__}
        for tool, arguments in (
            ("run_code", {"language": "python", "code": "print(1)"}),
            ("read_workspace", {}),
        ):
            text = await srv.call(tool, arguments, timeout=30)
            assert text.startswith("Blocked: Vestibule's configuration is invalid:"), text
            assert "VESTIBULE_MEM_MB='²'" in text, text
            assert "VESTIBULE_MAX_CONCURRENT='0'" in text, text
        assert srv.run_ids == []
        assert list(home.iterdir()) == []  # no default workspace was created
        assert any("configuration is invalid" in line for line in srv.stderr_lines)

        code, elapsed = await srv.close_like_a_client()
        assert code == 0, f"exit={code} after {elapsed:.2f}s\n{srv.stderr_tail()}"
        assert srv.bad_stdout == []  # golden rule 1
    finally:
        await srv.kill()
