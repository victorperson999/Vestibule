"""The release's checks (M4 F2, scripts/release_smoke.py), with no Docker, on every platform. The
real runs are the checklist's steps in docs/RELEASE.md.
"""
import asyncio
import importlib.util
import json
import os
import subprocess
import sys
import tarfile
import tomllib
import types
import zipfile
from pathlib import Path

import dockerutil
import pytest
import stdioutil

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("release_smoke", ROOT / "scripts" / "release_smoke.py")
assert _spec is not None and _spec.loader is not None
smoke = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(smoke)

GOOD_SDIST = ["PKG-INFO", ".gitignore", "LICENSE", "README.md", "PYPI.md", "pyproject.toml",
              "src/vestibule/__init__.py"]
# V13's files, spelled out here so that a name dropped from the script's own list is caught.
REQUIRED = ["vestibule/backends/native_helper.py", "vestibule/backends/native_seccomp.py",
            "vestibule/backends/seccomp-profile.json"]
READY = "ready in this terminal's environment: isolation "


def test_the_real_shape_of_an_sdist_passes():
    assert smoke.sdist_problems(GOOD_SDIST) == []
    assert smoke.sdist_problems([n for n in GOOD_SDIST if n != ".gitignore"]) == []


@pytest.mark.parametrize("stray", [".claude/settings.local.json", ".continue/agents/new-config.yaml",
                                   "tests/test_x.py", "notes/a.md", "src/other/x.py"])
def test_anything_else_in_an_sdist_is_named(stray):
    assert smoke.sdist_problems([*GOOD_SDIST, stray]) == [f"not allowed in the sdist: {stray}"]


@pytest.mark.parametrize("gone", ["LICENSE", "PKG-INFO", "PYPI.md", "README.md", "pyproject.toml"])
def test_a_file_missing_from_an_sdist_is_named(gone):
    assert smoke.sdist_problems([n for n in GOOD_SDIST if n != gone]) == [
        f"missing from the sdist: {gone}"]


def _wheel(path: Path, files: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w") as z:
        for name, data in files.items():
            z.writestr(name, data)
    return path


GOOD_WHEEL = {**{n: b"x\n" for n in REQUIRED},
              "vestibule/__init__.py": b"x = 1\n",
              "vestibule_mcp-0.1.0.dist-info/METADATA": b"Name: x\r\n"}


def _wheel_problems(tmp_path, files):
    with zipfile.ZipFile(_wheel(tmp_path / "w.whl", files)) as z:
        return smoke.wheel_problems(z)


def test_a_good_wheel_passes(tmp_path):
    assert _wheel_problems(tmp_path, GOOD_WHEEL) == []


@pytest.mark.parametrize("gone", REQUIRED)
def test_a_wheel_without_v13s_files_is_named(tmp_path, gone):
    files = {k: v for k, v in GOOD_WHEEL.items() if k != gone}
    assert _wheel_problems(tmp_path, files) == [f"missing from the wheel: {gone}"]


def test_crlf_in_a_wheels_source_is_named(tmp_path):
    files = {**GOOD_WHEEL, "vestibule/__init__.py": b"x = 1\r\n"}
    assert _wheel_problems(tmp_path, files) == ["CRLF in vestibule/__init__.py"]


def test_a_stray_entry_in_a_wheel_is_named(tmp_path):
    files = {**GOOD_WHEEL, "tests/x.py": b"x\n"}
    assert _wheel_problems(tmp_path, files) == ["not allowed in the wheel: tests/x.py"]


@pytest.mark.parametrize("stderr, code, expect, passes", [
    (READY + "native", 0, "native", True),
    (READY + "container", 0, "container", True),
    ("vestibule setup: checking\n" + READY + "container\n  a client ...", 0, "container", True),
    (READY + "native-degraded (limits not applied)", 0, "native", False),  # a prefix isn't a match
    (READY + "container-degraded (x)", 0, "container", False),
    (READY + "native", 1, "native", False),
    (READY + "container", 0, "native", False),
    ("not ready: isolation container, but these images are missing", 1, "container", False),
    ("not ready: isolation container, but these images are missing", 0, "container", False),
    ("Blocked: no isolating backend", 1, "blocked", True),
    ("Blocked: no isolating backend", 0, "blocked", False),
    ("Blocked: no isolating backend", 1, "container", False),
    (READY + "container", 0, "blocked", False),
    ("vestibule setup: checking", 0, "native", False),
    ("  " + READY + "native", 0, "native", False),  # at the start of the line only
])
def test_setups_answer_must_be_exactly_the_expected_one(stderr, code, expect, passes):
    assert (smoke.parse_setup(stderr, code, expect) is None) == passes


def test_not_ready_is_reported_as_itself():
    line = "not ready: isolation container, but these images are missing or unconfirmed: x"
    assert smoke.parse_setup(line, 1, "container") == line


@pytest.mark.parametrize("text, expect, passes", [
    ("exit_code: 0\nstdout:\n1\n\nisolation: native", "native", True),
    ("exit_code: 0\nisolation: native-degraded (no bus)", "native", False),
    ("exit_code: 0\nisolation: container", "native", False),
    ("Blocked: no runtime", "blocked", True),
    ("Blocked: no runtime", "container", False),
    ("exit_code: 0\nisolation: none", "blocked", False),
    ("", "native", False),
])
def test_a_calls_result_must_be_exactly_the_expected_isolation(text, expect, passes):
    assert (smoke.call_problem(text, expect) is None) == passes


def test_an_import_from_elsewhere_is_named(tmp_path):
    inside = tmp_path / "root" / "tools" / "vestibule" / "__init__.py"
    assert smoke.check_install(str(inside), tmp_path / "root", "final") is None
    problem = smoke.check_install(str(tmp_path / "dev" / "__init__.py"), tmp_path / "root", "final")
    assert problem and "not the install" in problem


@pytest.mark.parametrize("level", ["beta", "candidate", "alpha"])
def test_a_prerelease_interpreter_is_named(tmp_path, level):
    assert level in smoke.check_install(str(tmp_path / "__init__.py"), tmp_path, level)


def test_drift_names_what_no_gate_ran():
    pins = {"anyio": "4.16.0", "mcp": "1.30.0"}
    assert smoke.drift({"anyio": "4.16.0", "mcp": "1.30.0", "vestibule-mcp": "0.1.0"}, pins) == []
    assert smoke.drift({"anyio": "4.17.0"}, pins) == ["anyio 4.17.0 (the gates ran 4.16.0)"]
    assert smoke.drift({"newpkg": "1"}, pins) == ["newpkg 1 (the gates ran no version of it)"]


@pytest.mark.parametrize("expect, reason, passes", [
    ("native", None, True), ("container", None, True), ("blocked", "no docker", True),
    ("native", "no reachable Docker daemon", False),
    ("container", "pinned sandbox image(s) not pulled", False),
])
def test_skipping_the_end_to_end_tests_fails_except_on_macos(expect, reason, passes):
    assert (smoke.e2e_problem(expect, reason) is None) == passes


def _built(tmp_path: Path, stray: str | None = None, gone: str | None = None) -> Path:
    built = tmp_path / "built"
    built.mkdir()
    src = tmp_path / "src"
    src.mkdir()
    (src / "pkg-file").write_bytes(b"x")
    with tarfile.open(built / "vestibule_mcp-0.1.0.tar.gz", "w:gz") as tf:
        for name in [*(n for n in GOOD_SDIST if n != gone), *([stray] if stray else [])]:
            tf.add(src / "pkg-file", arcname=f"vestibule_mcp-0.1.0/{name}")
    _wheel(built / "vestibule_mcp-0.1.0-py3-none-any.whl", GOOD_WHEEL)
    return built


@pytest.fixture()
def twine_passes(monkeypatch):
    monkeypatch.setattr(smoke, "_run",
                        lambda argv, timeout, **kw: subprocess.CompletedProcess(argv, 0, "", ""))


def test_a_failed_check_leaves_dist_as_it_was(tmp_path, twine_passes):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "vestibule_mcp-0.0.1-py3-none-any.whl").write_bytes(b"old")
    assert smoke.finish_build(_built(tmp_path, ".claude/settings.local.json"), dist, "uv") == 1
    assert [p.name for p in dist.iterdir()] == ["vestibule_mcp-0.0.1-py3-none-any.whl"]


def test_a_failed_twine_check_leaves_dist_as_it_was(tmp_path, monkeypatch):
    monkeypatch.setattr(smoke, "_run",
                        lambda argv, timeout, **kw: subprocess.CompletedProcess(argv, 1, "bad", ""))
    dist = tmp_path / "dist"
    assert smoke.finish_build(_built(tmp_path), dist, "uv") == 1
    assert not dist.exists()


def test_a_good_build_replaces_only_our_own_files(tmp_path, twine_passes):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "vestibule_mcp-0.0.1.tar.gz").write_bytes(b"old")
    (dist / "someone-elses.txt").write_bytes(b"keep")
    assert smoke.finish_build(_built(tmp_path), dist, "uv") == 0
    assert sorted(p.name for p in dist.iterdir()) == [
        "someone-elses.txt", "vestibule_mcp-0.1.0-py3-none-any.whl", "vestibule_mcp-0.1.0.tar.gz"]


def test_a_build_without_pypi_md_fails(tmp_path, twine_passes, capsys):
    """M4 step 5c: PYPI.md is PyPI's page, so an sdist without it never reaches dist/."""
    dist = tmp_path / "dist"
    assert smoke.finish_build(_built(tmp_path, gone="PYPI.md"), dist, "uv") == 1
    assert "missing from the sdist: PYPI.md" in capsys.readouterr().out
    assert not dist.exists()


def test_build_refuses_what_isnt_committed(monkeypatch, tmp_path, capsys):
    calls = []

    def run(argv, timeout, **kw):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, " M README.md\n", "")

    def no_export(*args, **kwargs):
        raise AssertionError("nothing may be exported from a tree with changes")

    monkeypatch.setattr(smoke, "_run", run)
    monkeypatch.setattr(smoke.subprocess, "run", no_export)
    assert smoke.build(tmp_path, "uv") == 1
    assert "M README.md" in capsys.readouterr().out
    assert calls[0][-6:] == ["src", "pyproject.toml", "README.md", "PYPI.md", "LICENSE",
                             "requirements/constraints.txt"]


# --- the gates in the smoke itself ---------------------------------------------------------------

@pytest.fixture()
def machine(monkeypatch):
    """Stands in for every child process `_smoke_one` starts; a test changes one answer."""
    from check_env import read_constraints

    state = types.SimpleNamespace(
        module=None, level="final", extra_package=None, setup=(0, "", READY + "native"),
        audit=(0, None), pytest_code=0, skip_reason=None, stdio=None, ran=[])
    pins = read_constraints(ROOT / "requirements" / "constraints.txt")

    def run(argv, timeout, **kw):
        env = kw.get("env", {})
        root = Path(env["UV_TOOL_DIR"]).parent

        def done(code, out="", err=""):
            return subprocess.CompletedProcess(argv, code, out, err)

        if argv[1:3] == ["tool", "install"]:
            state.ran.append("install")
            return done(0)
        if argv[1:2] == ["-c"]:
            state.ran.append("import")
            module = state.module or str(root / "tools" / "vestibule" / "__init__.py")
            return done(0, json.dumps([module, state.level, "3.11.9"]))
        if argv[1:3] == ["pip", "list"]:
            packages = [{"name": n, "version": v} for n, v in pins.items()]
            if state.extra_package:
                packages.append({"name": state.extra_package, "version": "9.9"})
            return done(0, json.dumps(packages))
        if argv[-1] == "setup":
            state.ran.append("setup")
            code, out, err = state.setup
            return done(code, out, err)
        if argv[-1] == "audit":
            state.ran.append("audit")
            code, shown = state.audit
            return done(code, shown if shown is not None else f"python {root / 'audit'}")
        if "pytest" in argv:
            state.ran.append("pytest")
            return done(state.pytest_code, "7 passed")
        raise AssertionError(f"unexpected child: {argv}")

    async def stdio(exe, workspace, env, expect):
        state.ran.append("stdio")
        return state.stdio

    monkeypatch.setattr(smoke, "_run", run)
    monkeypatch.setattr(smoke, "_stdio_problem", stdio)
    monkeypatch.setattr(dockerutil, "docker_skip_reason", lambda: state.skip_reason)
    return state


def _one(expect="native", allow_drift=False):
    return smoke._smoke_one("3.11", "the.whl", expect, "uv", allow_drift)


def test_a_good_install_passes_every_step(machine):
    assert _one() is None
    assert machine.ran == ["install", "import", "setup", "stdio", "audit", "pytest"]


def test_an_install_imported_from_elsewhere_fails(machine):
    machine.module = "/the/dev/checkout/vestibule/__init__.py"
    assert "import:" in _one()
    assert "setup" not in machine.ran


def test_a_beta_interpreter_fails(machine):
    machine.level = "beta"
    assert "beta" in _one()


def test_a_package_no_gate_ran_fails(machine):
    machine.extra_package = "somethingnew"
    assert _one().startswith("drift:")
    assert "setup" not in machine.ran


def test_after_the_upload_drift_is_only_reported(machine, capsys):
    machine.extra_package = "somethingnew"
    assert _one(allow_drift=True) is None
    assert "drift: somethingnew 9.9" in capsys.readouterr().out


def test_setup_writing_to_stdout_fails(machine):
    machine.setup = (0, "stray", READY + "native")
    assert "wrote to stdout" in _one()


def test_setups_wrong_answer_fails(machine):
    machine.setup = (0, "", READY + "native-degraded (no bus)")
    assert _one().startswith("setup:")


def test_a_stdio_problem_fails(machine):
    machine.stdio = "non-JSON on stdout: x"
    assert _one().startswith("stdio:")


@pytest.mark.parametrize("audit", [(1, "python and the folder"), (0, "nothing useful")])
def test_an_audit_that_doesnt_show_the_run_fails(machine, audit):
    machine.audit = audit
    assert _one().startswith("audit:")


def test_skipped_end_to_end_tests_fail_and_pytest_isnt_started(machine):
    machine.skip_reason = "no reachable Docker daemon"
    assert "would skip" in _one()
    assert "pytest" not in machine.ran


def test_on_macos_the_end_to_end_tests_may_skip(machine):
    machine.skip_reason = "docker CLI not found"
    machine.setup = (1, "", "Blocked: no runtime")
    assert _one(expect="blocked") is None


def test_failing_end_to_end_tests_fail(machine):
    machine.pytest_code = 1
    assert _one().startswith("end-to-end tests:")


class _FakeServer:
    """What `_stdio_problem` needs of a real client session."""

    tools, text, ending, bad_stdout = ["read_workspace", "run_code"], "isolation: native", None, []

    @classmethod
    async def start(cls, workspace, base_env=None, exe=None):
        return cls()

    async def initialize(self):
        pass

    async def request(self, method, params):
        return {"tools": [{"name": n} for n in self.tools]}

    async def call(self, tool, arguments, timeout):
        return self.text

    async def close_like_sdk(self):
        return self.ending or (0, 0.1, stdioutil.STDIN)

    async def finish_reading(self):
        pass

    async def kill(self):
        pass


@pytest.mark.parametrize("change, passes", [
    ({}, True),
    ({"ending": (0, 2.0, "SIGTERM")}, False),  # it ignored EOF, and only the signal ended it
    ({"ending": (-9, 2.0, "killed")}, False),
    ({"bad_stdout": ["stray"]}, False),
    ({"tools": ["run_code"]}, False),
    ({"text": "isolation: none"}, False),
])
async def test_a_client_session_has_to_go_exactly_right(monkeypatch, tmp_path, change, passes):
    server = type("Server", (_FakeServer,), change)
    monkeypatch.setattr(stdioutil, "StdioServer", server)
    problem = await smoke._stdio_problem(tmp_path / "exe", tmp_path / "ws", {}, "native")
    assert (problem is None) == passes, problem


# --- what the release claims stays what was tested ---------------------------------------------

def _project() -> dict:
    with open(ROOT / "pyproject.toml", "rb") as f:
        return tomllib.load(f)


def test_the_published_mcp_range_is_the_tested_minor():
    """P2: a fresh install must be able to get only the minor both gates ran."""
    from check_env import read_constraints

    pin = read_constraints(ROOT / "requirements" / "constraints.txt")["mcp"]
    major, minor, _ = pin.split(".")
    assert _project()["project"]["dependencies"] == [
        f"mcp>={major}.{minor}.0,<{major}.{int(minor) + 1}"]


def test_the_python_classifiers_are_what_the_smoke_runs():
    prefix = "Programming Language :: Python :: "
    claimed = [c[len(prefix):] for c in _project()["project"]["classifiers"] if c.startswith(prefix)]
    assert tuple(claimed) == smoke.PYTHONS


def test_the_sdist_include_list_is_what_the_check_allows():
    include = _project()["tool"]["hatch"]["build"]["targets"]["sdist"]["include"]
    names = [("src/vestibule/x.py" if entry == "/src/vestibule" else entry[1:]) for entry in include]
    assert smoke.sdist_problems([*names, "PKG-INFO"]) == []


# --- which binary a release check drives ---------------------------------------------------------

async def test_start_runs_the_binary_its_given(monkeypatch, tmp_path):
    seen = []
    real = asyncio.create_subprocess_exec

    async def spawn(program, *args, **kw):
        seen.append(program)
        return await real(sys.executable, "-c", "", **kw)

    monkeypatch.setattr(stdioutil.asyncio, "create_subprocess_exec", spawn)
    server = await stdioutil.StdioServer.start(tmp_path / "ws", exe=tmp_path / "the-install")
    await server.kill()
    assert seen == [str(tmp_path / "the-install")]


@pytest.mark.parametrize("value, ending", [("/some/where/server", "server"),
                                           ("", "vestibule-mcp")])
def test_the_variable_moves_the_default_binary(value, ending):
    env = {**os.environ, "VESTIBULE_TEST_SERVER_EXE": value}
    code = "import sys; sys.path.insert(0, 'tests'); import stdioutil; print(stdioutil.SERVER_EXE)"
    r = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True,
                       timeout=60, env=env)
    assert Path(r.stdout.strip()).stem == ending, r.stderr


def test_a_gate_cant_run_against_another_install():
    env = {**os.environ, "VESTIBULE_TEST_SERVER_EXE": "/stale/install"}
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "check_env.py")], capture_output=True,
                       text=True, timeout=60, env=env)
    assert r.returncode == 1 and "VESTIBULE_TEST_SERVER_EXE" in r.stdout, r.stdout + r.stderr
