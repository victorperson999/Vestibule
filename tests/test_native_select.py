"""Selection on Linux with real native sandboxes (M2 step 6, docs/plans/M2/M2-step6-selection.md
§4.2 and §4.3). Linux only, against the trees `vestibule-mcp setup` made; the `native` marker
skips elsewhere with the reason. Tests that need limits also need a systemd user manager, and
the ones that compare with the container backend also need Docker.

The causes native can't be used on this host (Ubuntu's user-namespace restriction, a missing
libseccomp, a hidden /proc) are injected with a stand-in helper that reports the failure the
real one would, as contract N5 and criterion 13 say: this WSL kernel can't show them.
"""
import asyncio
import os
import secrets
import time

import pytest
from dockerutil import docker_required
from nativeutil import cgroups_required, holders, native_required, scopes
from stdioutil import StdioServer

import vestibule.backends.select as select_mod
import vestibule.server as server
from vestibule import rootfs
from vestibule.backends import native, native_cgroup
from vestibule.backends.base import RunRefusedError
from vestibule.backends.native import NativeBackend
from vestibule.backends.select import BackendSelector
from vestibule.config import Limits

pytestmark = [pytest.mark.native, native_required]

_SDK_ENV = ("HOME", "LOGNAME", "PATH", "SHELL", "TERM", "USER")  # the MCP SDK's POSIX set


@pytest.fixture()
def lim(tmp_path):
    def make(**kw):
        return Limits(**{"workspace_dir": str(tmp_path / "ws"), **kw})
    return make


@pytest.fixture()
async def selector():
    s = BackendSelector()
    yield s
    sel = s._selection
    if sel is not None:
        assert await sel.warden.drain(15)


@pytest.fixture()
def stand_in_helper(tmp_path, monkeypatch):
    """Replace the helper with one that reports `error` at `stage` the way the real helper
    does, without building anything; the sysctls are pinned for the hint."""
    def make(stage, message, sysctls=None):
        fake = tmp_path / "stand_in_helper.py"
        fake.write_text(
            "import json, os\n"
            "cfg = json.loads(os.read(0, 1 << 22))\n"
            f"os.write(cfg['status_fd'], (json.dumps({{'error': {stage!r}, "
            f"'message': {message!r}}}) + '\\n').encode())\n"
        )
        monkeypatch.setattr(native, "_HELPER", fake)
        monkeypatch.setattr(select_mod, "_read_sysctls", lambda: dict(sysctls or {}))
    return make


async def _blocked(s, limits):
    with pytest.raises(RunRefusedError) as ei:
        await s.get(limits)
    return str(ei.value)


# --- selection --------------------------------------------------------------------------

@cgroups_required
async def test_auto_selects_native_and_a_run_says_so(selector, lim, monkeypatch):
    sel = await selector.get(lim())
    assert (sel.verdict, type(sel.warden)) == ("native", NativeBackend)
    assert sel.warden._sem is selector._slots  # one budget for every backend (S6-D5)
    monkeypatch.setattr(server, "LIMITS", lim())
    monkeypatch.setattr(server, "SELECTOR", selector)
    monkeypatch.setattr(server, "CONFIG_ERROR", None)  # not the dev shell's VESTIBULE_*
    content = await server.call_tool("run_code", {"language": "python", "code": "print(1)"})
    assert content[0].text.endswith("isolation: native"), content[0].text


async def test_without_a_bus_forced_native_names_why(selector, lim, monkeypatch):
    monkeypatch.setattr(native_cgroup, "bus_env", lambda: None)
    sel = await selector.get(lim(backend="native"))
    assert (sel.verdict, sel.detail) == (
        "native-degraded", "limits not applied: cpu, memory, pids — no systemd user bus")


@docker_required
async def test_without_a_bus_auto_prefers_a_full_container(selector, lim, monkeypatch):
    monkeypatch.setattr(native_cgroup, "bus_env", lambda: None)
    sel = await selector.get(lim())
    assert sel.verdict == "container"  # N5: limits protect the host itself


async def test_a_failed_native_probe_is_drained_before_selection_returns(selector, lim,
                                                                         monkeypatch):
    """The loser's helper is stopped before `get()` returns (finding 6): a probe that times out
    leaves a helper still being stopped, which only the drain waits for."""
    built: list[NativeBackend] = []

    class Recorded(NativeBackend):
        def __init__(self, **kw):
            super().__init__(**kw)
            built.append(self)

    monkeypatch.setattr(select_mod, "NativeBackend", Recorded)
    monkeypatch.setattr(select_mod, "_PROBE_RW", "sleep 60")
    monkeypatch.setattr(select_mod, "_PROBE_TIMEOUT_S", 1)
    msg = await _blocked(selector, lim(backend="native"))
    assert "timed out yes" in msg
    assert built and all(t.done() for b in built for t in b._finishers)


async def test_an_unpinned_image_needs_a_digest(selector, lim):
    msg = await _blocked(selector, lim(backend="native", image_python="python:3.12-slim"))
    assert "VESTIBULE_IMAGE_PYTHON" in msg and "digest reference" in msg


async def test_no_rootfs_gives_the_exact_setup_line(selector, lim, tmp_path):
    empty = tmp_path / "empty-rootfs"
    msg = await _blocked(selector, lim(backend="native", rootfs_dir=str(empty)))
    assert f"`VESTIBULE_ROOTFS_DIR={empty} " in msg and "vestibule-mcp setup`" in msg


@docker_required
async def test_no_rootfs_auto_uses_the_container(selector, lim, tmp_path):
    sel = await selector.get(lim(rootfs_dir=str(tmp_path / "empty-rootfs")))
    assert sel.verdict in ("container", "container-degraded")


async def test_an_overlap_is_refused(selector, tmp_path):
    rf = tmp_path / "rootfs"
    with pytest.raises(select_mod._RestartRequired) as ei:  # config: only a restart fixes it
        await selector.get(Limits(backend="native", rootfs_dir=str(rf),
                                  workspace_dir=str(rf / "ws")))
    # the message's own words: the tmp path holds this test's name, so "overlap" alone would
    # match the "no native rootfs" refusal that follows it too
    assert "point one of them somewhere else" in str(ei.value)


async def test_a_guest_that_cannot_use_the_workspace_says_so(selector, lim, tmp_path):
    """The sandbox builds, the guest fails: the reason is M1's workspace message (finding 4)."""
    ws = tmp_path / "ws"
    ws.mkdir()
    ws.chmod(0)
    try:
        msg = await _blocked(selector, lim(backend="native"))
    finally:
        ws.chmod(0o755)
    assert "could not use the workspace" in msg and str(ws) in msg


async def test_a_rootfs_removed_after_selection_is_noticed(selector, lim, monkeypatch):
    sel = await selector.get(lim(backend="native"))
    monkeypatch.setattr(rootfs, "find", lambda *_: None)  # removed under a running server
    r = await sel.warden.run("python", "print(1)", 10, lim(backend="native"))
    assert r.isolation == "none"
    selector.note_result(sel.warden, r)
    assert "vestibule-mcp setup" in await _blocked(selector, lim(backend="native"))
    assert await sel.warden.drain(15)


@pytest.mark.parametrize("stage, message, sysctls, hint", [
    ("helper", "[Errno 1] Operation not permitted: '/proc/self/setgroups'",
     {"apparmor_restrict_unprivileged_userns": "1"}, "Ubuntu restricts"),
    ("mounts", "[Errno 1] mount /: Operation not permitted",
     {"apparmor_restrict_unprivileged_userns": "1"}, "Ubuntu restricts"),
    ("helper", "libseccomp.so.2: cannot open shared object file: No such file or directory",
     {}, "install libseccomp2"),
    ("mounts", "[Errno 1] mount /r/proc: Operation not permitted", {},
     "/proc isn't fully visible"),
], ids=["apparmor-at-helper", "apparmor-at-mounts", "no-libseccomp", "hidden-proc"])
async def test_why_native_is_unavailable_names_the_fix(selector, lim, stand_in_helper, stage,
                                                       message, sysctls, hint):
    """Criterion 13 by injection: Ubuntu's restriction fails the helper's own id-map write on a
    stock install (finding 8), so the hint keys on the setting, not the stage."""
    stand_in_helper(stage, message, sysctls)
    msg = await _blocked(selector, lim(backend="native"))
    assert f"setup failed at {stage}" in msg and hint in msg


# --- the server, end to end (§4.3) --------------------------------------------------------

@cgroups_required
async def test_the_sdks_minimal_environment_still_selects_native(tmp_path):
    """Criterion 17: only the MCP SDK's six variables (plus the workspace); the server finds
    the user bus itself."""
    env = {k: os.environ[k] for k in _SDK_ENV if k in os.environ}
    srv = await StdioServer.start(tmp_path / "ws", base_env=env)
    try:
        await srv.initialize()
        text = await srv.call("run_code", {"language": "python", "code": "print(1)"})
        assert text.endswith("isolation: native"), text + "\n" + srv.stderr_tail()
    finally:
        await srv.kill()


async def test_closing_the_server_mid_run_leaves_no_sandbox(tmp_path):
    """Criterion 11 through the real POSIX shutdown path (S6-D2): native adds nothing to the
    hand-off, and PDEATHSIG plus systemd still leave nothing behind."""
    token = f"vestibule-token-{secrets.token_hex(6)}"
    before = scopes()
    srv = await StdioServer.start(tmp_path / "ws", {"VESTIBULE_BACKEND": "native"})
    try:
        await srv.initialize()
        await srv.call("run_code", {"language": "python", "code": "print('warm')"})
        seen = len(srv.run_ids)
        await srv.send_request("tools/call", {"name": "run_code", "arguments": {
            "language": "bash", "code": f"exec -a {token} sleep 600", "timeout_seconds": 60}})
        await srv.wait_for_new_spawn(seen)
        t0 = time.monotonic()
        while not holders(token):
            assert time.monotonic() - t0 < 10, "the guest never started\n" + srv.stderr_tail()
            await asyncio.sleep(0.02)
        code, _, phase = await srv.close_like_sdk()
        assert code == 0, (code, phase, srv.stderr_tail())
        t0 = time.monotonic()
        while holders(token) and time.monotonic() - t0 < 2:
            await asyncio.sleep(0.02)
        assert holders(token) == []
        if before is not None:
            while (scopes() or set()) - before and time.monotonic() - t0 < 2:
                await asyncio.sleep(0.05)
            assert not (scopes() or set()) - before
    finally:
        await srv.kill()

