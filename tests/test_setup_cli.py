"""`vestibule-mcp setup` (M4 step 2, docs/plans/M4/M4-step2-setup.md; contract P4 and P5).

The flow tests stand in at `BackendSelector._select`, not `.get`, so a selector reused between
two checks would answer from its cache and fail them. Every flow test asserts an empty stdout
(golden rule 1). The real command runs in tests/test_stdio_e2e.py and tests/test_rootfs.py.
"""
import asyncio
import logging
import os
import shutil
import sys
from types import SimpleNamespace

import pytest

import vestibule.backends.select as select_mod
from vestibule import rootfs, setup_cli
from vestibule.backends.base import RunRefusedError, RunResult, Warden
from vestibule.backends.container import ContainerBackend
from vestibule.backends.select import BackendSelector, Selection
from vestibule.config import DEFAULT_IMAGE_NODE as NODE
from vestibule.config import DEFAULT_IMAGE_PYTHON as PY
from vestibule.config import Limits

READY = "ready in this terminal's environment: isolation "
REFUSED = "native sandboxes can't start on this host (x). Ubuntu restricts ..."
NO_BUS = "limits not applied: cpu, memory, pids — no systemd user bus"
UNPULLED = RunRefusedError("image x is not usable (No such image); run `docker pull x`")
NOT_REACHED = RunRefusedError("no usable container runtime (docker: daemon not reachable).")
NO_ISOLATION = RunRefusedError("no isolating backend is available")
FINAL = select_mod._RestartRequired("unknown VESTIBULE_BACKEND")
STOPPED = "daemon not reachable (cannot connect)"
MISSING = "not installed"


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    """Not the dev shell's settings; and `main` changes this logger for the whole process, which
    test_shutdown.py's captured warnings depend on."""
    for name in list(os.environ):
        if name.startswith("VESTIBULE_"):
            monkeypatch.delenv(name)
    logger = logging.getLogger("vestibule")
    level = logger.level
    yield
    logger.setLevel(level)


class _Warden(Warden):
    def __init__(self):
        self.drained = False

    async def run(self, language: str, code: str, timeout_s: int, limits: Limits) -> RunResult:
        raise AssertionError("selection tests never run code")

    async def drain(self, timeout=None):
        self.drained = True
        return True


def _selection(result):
    """"native", ("native-degraded", "why"), or ("container", None, "why native isn't used")."""
    verdict, detail, why = (*((result,) if isinstance(result, str) else result), None, None)[:3]
    return Selection(_Warden(), verdict, detail, why_not_native=why)


def _setup(monkeypatch, capsys, *, platform="linux", backend="auto", userns=None, root=None,
           install=0, results=(), present=(True, True), pulls=(0, 0), daemon=None):
    """Runs the real `main` with the machine stood in. `daemon`: docker's `version` answer."""
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setattr(shutil, "which", lambda name: None)  # it reads sys.platform too
    if backend != "auto":
        monkeypatch.setenv("VESTIBULE_BACKEND", backend)
    calls = SimpleNamespace(install=0, userns=0, pulled=[], checks=0)
    answers = list(results)
    is_present, pull_rc = dict(zip((PY, NODE), present)), dict(zip((PY, NODE), pulls))

    async def select(self, limits):
        calls.checks += 1
        answer = answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return _selection(answer)

    def install_trees(limits):
        calls.install += 1
        return install

    def userns_problem():
        calls.userns += 1
        return userns

    async def version(runtime):
        return daemon if runtime == "docker" else MISSING

    async def image_present(self, ref):
        return is_present[ref]

    async def pull_image(self, ref):
        calls.pulled.append(ref)
        return pull_rc[ref]

    monkeypatch.setattr(BackendSelector, "_select", select)
    monkeypatch.setattr(rootfs, "install", install_trees)
    monkeypatch.setattr(setup_cli, "userns_problem", userns_problem)
    monkeypatch.setattr(setup_cli, "root_refusal", lambda: root)
    monkeypatch.setattr(select_mod, "_version_check", version)
    monkeypatch.setattr(ContainerBackend, "image_present", image_present)
    monkeypatch.setattr(ContainerBackend, "pull_image", pull_image)
    code = setup_cli.main()
    captured = capsys.readouterr()
    assert captured.out == ""  # golden rule 1
    assert not answers, "a check the flow should have made wasn't made"
    return code, calls, captured.err


# name, machine, exit code, trees installed, refs pulled, checks, words in the output, words not
FLOWS = {
    "full native": (dict(results=["native"], present=(False, False)),
                    0, 1, [], 1, [READY + "native"], ["pulling"]),
    "no namespaces, Docker": (
        dict(userns=REFUSED, present=(False, False),
             results=[UNPULLED, ("container", None, REFUSED)]),
        0, 0, [PY, NODE], 2,
        [f"not downloading the native trees (about 123 MB): {REFUSED}", READY + "container",
         f"native isn't used: {REFUSED}", "checking again"], []),
    "no namespaces, no Docker": (
        dict(userns=REFUSED, results=[NO_ISOLATION], daemon=MISSING),
        1, 0, [], 1, ["Blocked: no isolating backend"], ["ready in", "pulling"]),
    "root": (dict(root="the server runs as root", results=[FINAL]),
             1, 0, [], 1, ["not downloading the native trees", "the server runs as root"], []),
    "native-degraded, Docker works": (
        dict(results=[("native-degraded", NO_BUS), "container"], present=(False, False)),
        0, 1, [PY, NODE], 2, [READY + "container"], []),
    "native-degraded, no Docker": (
        dict(results=[("native-degraded", NO_BUS)], daemon=MISSING),
        0, 1, [], 1, [f"{READY}native-degraded ({NO_BUS})"], ["can't get the sandbox images"]),
    "Windows, images missing": (
        dict(platform="win32", results=[UNPULLED, "container"], present=(False, False)),
        0, 0, [PY, NODE], 2, ["Linux-only", READY + "container"], []),
    "Windows, images present": (
        dict(platform="win32", results=["container"]),
        0, 0, [], 1, [READY + "container"], ["pulling", "checking again"]),
    "macOS, node missing": (
        dict(platform="darwin", results=["container", "container"], present=(True, False)),
        0, 0, [NODE], 2, [READY + "container"], []),
    "the daemon is stopped": (
        dict(platform="win32", results=[NOT_REACHED], daemon=STOPPED),
        1, 0, [], 1, ["can't get the sandbox images yet", "Blocked: no usable"], ["ready in"]),
    "the daemon stops after the check": (
        dict(platform="win32", results=["container"], daemon=STOPPED),
        1, 0, [], 1, ["not ready: isolation container", PY, NODE], ["ready in"]),
    "one pull fails": (
        dict(platform="win32", results=[UNPULLED, "container"], present=(False, False),
             pulls=(0, 1)),
        1, 0, [PY, NODE], 2, [f"could not pull {NODE}", "not ready: isolation container"],
        ["ready in"]),
    "both pulls fail": (
        dict(platform="win32", results=[UNPULLED], present=(False, False), pulls=(1, 1)),
        1, 0, [PY, NODE], 1, ["Blocked: image x"], ["checking again"]),
    "inspect gets no answer": (
        dict(platform="win32", results=["container"], present=(True, None)),
        1, 0, [], 1, ["didn't answer about", "not ready"], ["pulling"]),
    "a pull fails, native-degraded stands": (
        dict(results=[("native-degraded", NO_BUS)], present=(False, False), pulls=(1, 1)),
        0, 1, [PY, NODE], 1, [f"{READY}native-degraded ({NO_BUS})"], ["not ready"]),
    "trees fail, the container works": (
        dict(install=1, results=[UNPULLED, ("container", None, "no native rootfs")],
             present=(False, False)),
        0, 1, [PY, NODE], 2, [READY + "container", "native isn't used: no native rootfs"], []),
    "VESTIBULE_BACKEND=container": (
        dict(backend="container", results=["container"]),
        0, 0, [], 1, ["skipping the native trees", READY + "container"], []),
    "VESTIBULE_BACKEND=native": (
        dict(backend="native", results=[("native-degraded", NO_BUS)], present=(False, False)),
        0, 1, [], 1, [f"{READY}native-degraded"], ["pulling"]),
    "VESTIBULE_BACKEND=naive": (
        dict(backend="naive", results=[("none", "explicitly configured, dev-only")],
             present=(False, False)),
        0, 0, [], 1, [f"{READY}none (explicitly configured, dev-only)"], ["pulling"]),
    "only a restart can fix it": (
        dict(results=[FINAL], present=(False, False)),
        1, 1, [], 1, ["Blocked: unknown VESTIBULE_BACKEND"], ["pulling"]),
}


@pytest.mark.parametrize("name", FLOWS)
def test_a_setup_flow(monkeypatch, capsys, name):
    machine, code, installs, pulled, checks, words, absent = FLOWS[name]
    got, calls, err = _setup(monkeypatch, capsys, **machine)
    assert (got, calls.install, calls.pulled, calls.checks) == (code, installs, pulled, checks), err
    for word in words:
        assert word in err, err
    for word in absent:
        assert word not in err, err


@pytest.mark.parametrize("platform", ["linux", "win32"])
def test_a_forced_container_never_asks_about_namespaces(monkeypatch, capsys, platform):
    _, calls, _ = _setup(monkeypatch, capsys, platform=platform, backend="container",
                         results=["container"])
    assert calls.userns == 0


@pytest.mark.parametrize("platform", ["linux", "win32"])
def test_invalid_settings_are_all_named_and_nothing_runs(monkeypatch, capsys, platform):
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setenv("VESTIBULE_MEM_MB", "512m")
    monkeypatch.setenv("VESTIBULE_CPU_PCT", "lots")

    def must_not_run(*a):
        raise AssertionError("invalid settings must stop setup first")

    monkeypatch.setattr(setup_cli, "_native_step", must_not_run)
    monkeypatch.setattr(setup_cli, "_ready_steps", must_not_run)
    assert setup_cli.main() == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "invalid settings, nothing was changed" in captured.err
    assert "VESTIBULE_MEM_MB='512m'" in captured.err and "VESTIBULE_CPU_PCT='lots'" in captured.err


# --- the real close (P5) ---------------------------------------------------------------------

@pytest.fixture()
def spies(monkeypatch):
    closed = []
    real = BackendSelector.aclose

    async def aclose(self, *a, **k):
        closed.append(self)
        return await real(self, *a, **k)

    monkeypatch.setattr(BackendSelector, "aclose", aclose)
    return closed


async def _select_gives(monkeypatch, answer):
    async def select(self, limits):
        if isinstance(answer, BaseException):
            raise answer
        return answer

    monkeypatch.setattr(BackendSelector, "_select", select)


async def test_a_good_check_drains_the_winner_and_closes(monkeypatch, spies):
    winner = Selection(_Warden(), "native")
    await _select_gives(monkeypatch, winner)
    assert await setup_cli._check(Limits()) is winner
    assert winner.warden.drained and len(spies) == 1


async def test_a_refused_check_closes_and_returns_the_refusal(monkeypatch, spies):
    await _select_gives(monkeypatch, UNPULLED)
    assert await setup_cli._check(Limits()) is UNPULLED
    assert len(spies) == 1


async def test_an_unexpected_failure_is_a_refusal_not_a_traceback(monkeypatch, spies):
    await _select_gives(monkeypatch, PermissionError("the workspace can't be made"))
    result = await setup_cli._check(Limits())
    assert isinstance(result, RunRefusedError)
    assert str(result) == "the readiness check failed: the workspace can't be made"
    assert len(spies) == 1


async def test_a_cancelled_check_still_closes(monkeypatch, spies):
    await _select_gives(monkeypatch, asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await setup_cli._check(Limits())
    assert len(spies) == 1


class _HardExit(BaseException):
    """Stops the test where os._exit would have ended the process."""


@pytest.fixture()
def exits(monkeypatch):
    codes = []

    def fake_exit(code):
        codes.append(code)
        raise _HardExit

    monkeypatch.setattr(setup_cli.os, "_exit", fake_exit)
    return codes


def _steps_that(monkeypatch, *, leave_a_task=False, returns=None, raises=None):
    keep = []

    async def steps(limits):
        if leave_a_task:
            keep.append(asyncio.create_task(asyncio.sleep(60)))
        if raises is not None:
            raise raises
        return returns

    monkeypatch.setattr(setup_cli, "_steps", steps)


def test_a_clean_finish_returns_normally(monkeypatch, exits):
    _steps_that(monkeypatch, returns=7)
    assert asyncio.run(setup_cli._ready_steps(Limits())) == 7
    assert exits == []


@pytest.mark.parametrize("raises, code", [(None, 7), (asyncio.CancelledError(), 130),
                                          (ValueError("boom"), 1)])
def test_a_task_left_alive_means_a_hard_exit_with_the_right_code(monkeypatch, exits, capsys, raises,
                                                                 code):
    """S7-D1's rule: never back into asyncio.run with a task left. A crash isn't a Ctrl+C, and
    a Ctrl+C still says so, because `main`'s message never runs after a hard exit."""
    _steps_that(monkeypatch, leave_a_task=True, returns=7, raises=raises)
    with pytest.raises(_HardExit):
        asyncio.run(setup_cli._ready_steps(Limits()))
    assert exits == [code]
    assert ("interrupted" in capsys.readouterr().err) == (code == 130)


def test_a_crash_is_printed_and_exits_1(monkeypatch, exits, capsys):
    _steps_that(monkeypatch, raises=ValueError("boom"))
    assert asyncio.run(setup_cli._ready_steps(Limits())) == 1
    assert "failed: ValueError('boom')" in capsys.readouterr().err and exits == []


def test_ctrl_c_is_a_message_and_130(monkeypatch, capsys):
    def interrupted(limits):
        raise KeyboardInterrupt

    monkeypatch.setattr(setup_cli, "_native_step", interrupted)
    assert setup_cli.main() == 130
    assert "interrupted" in capsys.readouterr().err


# --- the report (P5) -------------------------------------------------------------------------

def test_the_report_lists_what_can_differ_for_another_client(monkeypatch, capsys):
    monkeypatch.setattr(shutil, "which", lambda name: f"/opt/{name}" if name == "docker" else None)
    monkeypatch.setenv("VESTIBULE_ROOTFS_DIR", "/data/a\x1b[31mb")
    monkeypatch.setenv("VESTIBULE_IMAGE_PYTHON", "python:3.12-slim")
    monkeypatch.setenv("VESTIBULE_IMAGE_NODE", NODE)
    monkeypatch.setenv("VESTIBULE_RUNTIME", "   ")  # blank means unset
    setup_cli._say_inputs(Limits(), NO_ISOLATION)
    err = capsys.readouterr().err
    assert "container runtime on this PATH: /opt/docker" in err
    assert "VESTIBULE_ROOTFS_DIR='/data/a\\x1b[31mb'" in err  # escaped: it can't drive the terminal
    assert "VESTIBULE_IMAGE_PYTHON='python:3.12-slim' (not digest-pinned)" in err
    assert f"VESTIBULE_IMAGE_NODE={NODE!r}\n" in err  # pinned: no note
    assert "VESTIBULE_RUNTIME" not in err


def test_the_report_says_so_when_nothing_is_found_or_set(monkeypatch, capsys):
    monkeypatch.setattr(shutil, "which", lambda name: None)
    setup_cli._say_inputs(Limits(), NO_ISOLATION)
    err = capsys.readouterr().err
    assert "container runtime on this PATH: none" in err and "no VESTIBULE_* settings" in err


def test_the_runtime_shown_follows_the_setting(monkeypatch, capsys):
    monkeypatch.setattr(shutil, "which", lambda name: f"/opt/{name}")
    setup_cli._say_inputs(Limits(runtime="podman"), NO_ISOLATION)
    assert "container runtime on this PATH: /opt/podman" in capsys.readouterr().err


def test_the_runtime_shown_is_the_one_selection_used(monkeypatch, capsys):
    """Docker installed but stopped, Podman running: a client's PATH needs podman, not docker."""
    monkeypatch.setattr(shutil, "which", lambda name: f"/opt/{name}")
    setup_cli._say_inputs(Limits(), Selection(ContainerBackend("podman"), "container"))
    assert "container runtime on this PATH: /opt/podman" in capsys.readouterr().err


def test_nothing_printed_can_drive_the_terminal(capsys):
    r"""Whatever a line carries: a setting, or a registry's own error text. `\` isn't doubled, so
    a Windows path still reads as one."""
    rootfs._say("pulling a\x1b]0;x\x07b from C:\\Program Files")
    captured = capsys.readouterr()
    assert captured.err == "pulling a\\x1b]0;x\\x07b from C:\\Program Files\n" and captured.out == ""


def test_a_closed_stderr_doesnt_turn_into_stdout(monkeypatch, capsys):
    """`setup 2>&-`: print(file=None) writes to stdout (golden rule 1)."""
    monkeypatch.setattr(sys, "stderr", None)
    rootfs._say("a line")
    assert capsys.readouterr().out == ""


def test_setup_keeps_selections_log_lines_out_of_its_report(monkeypatch):
    monkeypatch.setattr(setup_cli, "_native_step", lambda limits: None)

    async def ready(limits):
        return 0

    monkeypatch.setattr(setup_cli, "_ready_steps", ready)
    assert setup_cli.main() == 0
    assert logging.getLogger("vestibule").level == logging.ERROR
