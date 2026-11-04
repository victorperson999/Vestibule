"""Config validation and the server's reported version
(docs/plans/M1/M1-followup-config-version.md). Docker-free.

A bad VESTIBULE_* setting must never be silently replaced by its default, and must never
crash startup. The server starts, and every tool call returns a `Blocked:` message that
names the setting, so the agent can tell the user what to fix.
"""
import os
import sys
import tomllib
from pathlib import Path

import pytest
from homeutil import NoHome, RelativeHome
from stdioutil import SERVER_EXE, STDIN, StdioServer

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


def test_rootfs_dir_default_and_override(monkeypatch, tmp_path):
    assert Limits.from_env().rootfs_path == Path("~/.vestibule/rootfs").expanduser()
    monkeypatch.setenv("VESTIBULE_ROOTFS_DIR", str(tmp_path / "rootfs"))
    assert Limits.from_env().rootfs_path == tmp_path / "rootfs"


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

        code, elapsed, phase = await srv.close_like_sdk()
        assert (code, phase) == (0, STDIN), (
            f"exit={code} phase={phase} after {elapsed:.2f}s\n{srv.stderr_tail()}"
        )
        assert srv.bad_stdout == []  # golden rule 1
    finally:
        await srv.kill()


# --- VESTIBULE_AUDIT_DIR (M3 A8; docs/plans/M3/M3-step2-audit-writer.md §5.2) -----------

def _ws_and_rootfs(monkeypatch, tmp_path):
    monkeypatch.setenv("VESTIBULE_WORKSPACE", str(tmp_path / "ws"))
    monkeypatch.setenv("VESTIBULE_ROOTFS_DIR", str(tmp_path / "rootfs"))


@pytest.mark.parametrize("value, expected", [
    (None, "~/.vestibule/audit"),
    ("~/x", "~/x"),
    ("{t}/audit", "{t}/audit"),
])
def test_audit_dir_accepted(monkeypatch, tmp_path, value, expected):
    _ws_and_rootfs(monkeypatch, tmp_path)
    if value is not None:
        monkeypatch.setenv("VESTIBULE_AUDIT_DIR", value.format(t=tmp_path))
    lim = Limits.from_env()
    assert lim.audit_path == Path(expected.format(t=tmp_path)).expanduser()


@pytest.mark.parametrize("value", [
    "audit", "{t}/ws", "{t}/ws/audit", "{t}", "{t}/rootfs", "{t}/rootfs/audit",
    "{t}/WS/audit",  # a case variant: folded on every platform (macOS ignores case)
])
def test_audit_dir_refused(monkeypatch, tmp_path, value):
    _ws_and_rootfs(monkeypatch, tmp_path)
    monkeypatch.setenv("VESTIBULE_AUDIT_DIR", value.format(t=tmp_path))
    with pytest.raises(ConfigError) as e:
        Limits.from_env()
    [problem] = e.value.problems
    assert "VESTIBULE_AUDIT_DIR" in problem
    if value == "audit":
        assert problem == "VESTIBULE_AUDIT_DIR='audit' must be an absolute path or start with ~"
    else:
        assert "overlap; point one of them somewhere else" in problem
        assert ("VESTIBULE_WORKSPACE" in problem) != ("VESTIBULE_ROOTFS_DIR" in problem)


@pytest.mark.skipif(sys.platform == "win32", reason="~user is POSIX")
def test_audit_dir_with_an_unknown_user_is_refused(monkeypatch, tmp_path):
    _ws_and_rootfs(monkeypatch, tmp_path)
    monkeypatch.setenv("VESTIBULE_AUDIT_DIR", "~nosuchuser-vestibule/x")
    with pytest.raises(ConfigError) as e:
        Limits.from_env()
    assert e.value.problems[0].startswith(
        "VESTIBULE_AUDIT_DIR='~nosuchuser-vestibule/x' must be an absolute path or start with ~ (")


@pytest.mark.parametrize("value", ["\\audit", "C:audit"])  # drive-relative; relative on POSIX
def test_audit_dir_drive_relative_is_refused(monkeypatch, tmp_path, value):
    _ws_and_rootfs(monkeypatch, tmp_path)
    monkeypatch.setenv("VESTIBULE_AUDIT_DIR", value)
    with pytest.raises(ConfigError) as e:
        Limits.from_env()
    assert e.value.problems == [
        f"VESTIBULE_AUDIT_DIR={value!r} must be an absolute path or start with ~"]


def test_no_home_directory_default_audit_dir_is_not_a_config_error(monkeypatch, tmp_path):
    """W4: such a host works today with absolute settings; its records say why they're lost."""
    import vestibule.config as config

    _ws_and_rootfs(monkeypatch, tmp_path)
    monkeypatch.setattr(config, "Path", NoHome)
    assert Limits.from_env().audit_dir == "~/.vestibule/audit"
    monkeypatch.setenv("VESTIBULE_AUDIT_DIR", "~/x")  # an explicit one still is
    with pytest.raises(ConfigError) as e:
        Limits.from_env()
    assert e.value.problems == [
        "VESTIBULE_AUDIT_DIR='~/x' must be an absolute path or start with ~ "
        "(Could not determine home directory.)"]


def test_a_default_that_expands_to_a_relative_path_is_not_a_config_error(monkeypatch, tmp_path):
    """critic, M3 step 2: W4 again. The writer reports it on every record instead."""
    import vestibule.config as config

    _ws_and_rootfs(monkeypatch, tmp_path)
    monkeypatch.setattr(config, "Path", RelativeHome)
    assert Limits.from_env().audit_dir == "~/.vestibule/audit"
    monkeypatch.setenv("VESTIBULE_AUDIT_DIR", "~/x")  # an explicit one still is, named as such
    with pytest.raises(ConfigError) as e:
        Limits.from_env()
    assert e.value.problems == [
        f"VESTIBULE_AUDIT_DIR='~/x' expands to {Path('relative-home', 'x')}, which isn't "
        "absolute: the home directory is relative"]


def test_a_path_that_cant_be_resolved_is_compared_as_written(monkeypatch, tmp_path):
    """critic, M3 step 2: from_env runs at the server's import, and Windows' realpath raises for
    an offline network drive (WinError 1231). That must not stop the server; an overlap is still
    caught."""
    def offline(real):
        def stand_in(path, *args, **kwargs):
            if "offline-drive" in str(path):
                raise OSError(1231, "The network location cannot be reached")
            return real(path, *args, **kwargs)
        return stand_in

    ws = tmp_path / "offline-drive" / "ws"
    monkeypatch.setenv("VESTIBULE_WORKSPACE", str(ws))
    monkeypatch.setenv("VESTIBULE_ROOTFS_DIR", str(tmp_path / "rootfs"))
    # abspath too: on POSIX it calls getcwd, which raises once the cwd is deleted (round 2)
    monkeypatch.setattr(os.path, "realpath", offline(os.path.realpath))
    monkeypatch.setattr(os.path, "abspath", offline(os.path.abspath))
    Limits.from_env()
    monkeypatch.setenv("VESTIBULE_AUDIT_DIR", str(ws / "audit"))
    with pytest.raises(ConfigError) as e:
        Limits.from_env()
    assert "overlap" in e.value.problems[0]


@pytest.mark.skipif(not SERVER_EXE.exists(), reason=f"console script not found: {SERVER_EXE}")
async def test_stdio_relative_audit_dir_blocks_every_call(tmp_path):
    srv = await StdioServer.start(tmp_path / "ws", {"VESTIBULE_AUDIT_DIR": "audit"})
    try:
        await srv.initialize()
        text = await srv.call("run_code", {"language": "python", "code": "print(1)"}, timeout=30)
        assert text == (
            "Blocked: Vestibule's configuration is invalid: VESTIBULE_AUDIT_DIR='audit' must be "
            "an absolute path or start with ~. Fix the setting in the MCP client's config for "
            "this server, then restart it. Nothing was run.")
        assert srv.run_ids == []
    finally:
        await srv.kill()
