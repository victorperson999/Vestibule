"""M2 acceptance suite (step 7a): one test per criterion of the M2 contract §9
(docs/plans/M2/M2-native-warden.md), each driven through `server.call_tool` (the dispatch an
agent's tool call reaches) with a fresh BackendSelector and a temp workspace, so real
selection, the run, the result's rendering and the server's own `note_result` hook are all in
the path. Plan: docs/plans/M2/M2-step7-acceptance.md §2 and §3.

Linux only, against the trees `vestibule-mcp setup` made, with a systemd user manager: the
exam is for a host where native is full, so everything here expects exactly `native`. The
backend is forced to `native` except in c01, which proves `auto`'s choice (S7 §3.4).

Criterion -> evidence:
  1  hello in python/bash/node => native         test_c01_*
  2  no host filesystem visibility               test_c02_*
  3  TCP, DNS, UDP, AF_VSOCK fail; only lo       test_c03_*
  4  memory bomb contained                       test_c04_*
  5  fork bomb stops at pids.max                 test_c05_*
  6  cpu.max holds                               test_c06_*;
       test_native.py::test_the_limits_from_outside (the readback)
  7  /tmp cap; EROFS on / and /sandbox           test_c07_*
  8  timeout, nothing survives                   test_c08_*
  9  denials named; storm bounded inside the     test_c09_*;
     envelope; foreign-arch kill                 test_native.py::test_the_limits_from_outside;
                                                 test_native_seccomp.py (the bad-arch action and the
                                                 host arch only, S7-D2)
  10 the guest's own view                        test_c10_*
  11 server SIGKILLed mid-run => nothing left    test_c11_*
  12 4 runs, 4 PID namespaces                    test_c12_*
  13 degradation by injection                    test_c13_* (rootfs removed, through the handler);
                                                 test_native_select.py (the other four causes)
  14 cold-start benchmark                        scripts/bench_coldstart.py (not a test)
  15 both platforms green                        the gate
  16 the live-agent check                        step 7b (BUILD_LOG)
  17 minimal-environment launch                  test_native_select.py::
       test_the_sdks_minimal_environment_still_selects_native

"The host is unaffected" means what is observable: the guest was stopped and the cgroup says
why, the server returned a normal result, the next run works, and nothing survived.
"""
import asyncio
import json
import logging
import os
import re
import secrets
import time
from pathlib import Path

import pytest
from nativeutil import cgroups_required, holders, native_required, scopes
from stdioutil import StdioServer

from vestibule import rootfs
from vestibule import server as srv
from vestibule.backends import native, native_cgroup
from vestibule.backends.native import NativeBackend
from vestibule.backends.select import BackendSelector
from vestibule.config import Limits

pytestmark = [pytest.mark.native, native_required, cgroups_required]

_ISOLATION = re.compile(r"^isolation: (\S+)(?: \((.*)\))?$", re.M)
_ALLOWED = {"native", "native-degraded", "none"}
_TIMED_OUT = "[timed out and was terminated]"
_STARTED = re.compile(r"run ([0-9a-f]{16}): starting a native sandbox")


def _isolation(text: str) -> tuple[str, str | None]:
    m = _ISOLATION.search(text)
    assert m, f"no isolation line:\n{text}"
    return m.group(1), m.group(2)


def _result(text: str) -> dict:
    """The JSON object a guest printed on its `RESULT {...}` line."""
    m = re.search(r"^RESULT (\{.*\})$", text, re.M)
    assert m, text
    return json.loads(m.group(1))


def _token() -> str:
    return f"vestibule-token-{secrets.token_hex(6)}"


def _own_helpers() -> list[int]:
    """This process's own helper children (the server runs in-process here): found by their
    parent, not by the cgroup they sit in, so where they are charged can be checked."""
    def parent(pid):
        try:
            with open(f"/proc/{pid}/stat") as f:
                return int(f.read().rsplit(")", 1)[1].split()[1])
        except OSError:  # gone meanwhile
            return None
    return [p for p in holders("vestibule-warden") if parent(p) == os.getpid()]


def _live_scopes() -> set[str]:
    live = scopes()
    assert live is not None, "systemctl --user failed, so no scope check is possible"
    return live


class Tool:
    """The two MCP tools, called the way the SDK calls them, plus test helpers."""

    def __init__(self, limits: Limits, selector: BackendSelector) -> None:
        self.limits = limits
        self.selector = selector
        self.wardens: list = []  # every warden a call ran on, to drain at the end

    async def run(self, language: str, code: str, timeout_s: int = 30) -> str:
        out = await srv.call_tool(
            "run_code", {"language": language, "code": code, "timeout_seconds": timeout_s})
        text = "\n".join(c.text for c in out)
        assert not text.startswith("Internal error"), text
        if not text.startswith("Blocked:"):
            lines = _ISOLATION.findall(text)
            assert len(lines) == 1 and lines[0][0] in _ALLOWED, text
        sel = self.selector._selection
        if sel is not None and all(sel.warden is not w for w in self.wardens):
            self.wardens.append(sel.warden)
        return text

    async def drain(self) -> None:
        for w in self.wardens:
            assert await w.drain(30), "a finisher was still running"


@pytest.fixture()
async def make_tool(tmp_path, monkeypatch, caplog):
    """Swap a fresh selector and temp-workspace limits into the server's globals. At teardown,
    drain every warden used and check that none of this test's own run scopes is left (the
    step-4 rule, limited to this test's runs so a live server elsewhere can't fail it)."""
    caplog.set_level(logging.INFO, logger="vestibule.native")
    made: list[Tool] = []

    def make(**overrides) -> Tool:
        limits = Limits(**{"workspace_dir": str(tmp_path / "ws"), "backend": "native",
                           **overrides})
        tool = Tool(limits, BackendSelector())
        monkeypatch.setattr(srv, "LIMITS", limits)
        monkeypatch.setattr(srv, "CONFIG_ERROR", None)  # not the dev shell's VESTIBULE_*
        monkeypatch.setattr(srv, "SELECTOR", tool.selector)
        made.append(tool)
        return tool

    yield make
    for tool in made:
        await tool.drain()
    own = {f"vestibule-{m.group(1)}.scope" for r in caplog.get_records("call")  # not teardown's
           for m in [_STARTED.search(r.getMessage())] if m}
    assert own, "no run of this test was logged, so the scope check below would be empty"
    deadline = time.monotonic() + 2
    while own & _live_scopes() and time.monotonic() < deadline:
        await asyncio.sleep(0.05)
    assert not own & _live_scopes(), "scopes left behind"


@pytest.fixture()
def tool(make_tool) -> Tool:
    return make_tool()


# ---------------------------------------------------------------------------- criteria 1-13


async def test_c01_auto_picks_native_and_hello_runs_in_three_languages(make_tool):
    tool = make_tool(backend="auto")  # N5's choice, not a forced one
    for language, code, marker in (("python", "print('hi-py')", "hi-py"),
                                   ("bash", "echo hi-bash", "hi-bash"),
                                   ("node", "console.log('hi-node')", "hi-node")):
        text = await tool.run(language, code)
        assert marker in text and _isolation(text) == ("native", None), text
    sel = tool.selector._selection
    assert sel is not None and isinstance(sel.warden, NativeBackend)


_FS = r'''
import json, os
def denied(fn):
    try:
        fn()
    except PermissionError:
        return True
    return False
rows = [line.split() for line in open("/proc/self/mountinfo")]
print("RESULT " + json.dumps({
    "exists": os.path.exists("@CANARY@"),
    "root": denied(lambda: os.readlink("/proc/1/root")),
    "cwd": denied(lambda: os.readlink("/proc/1/cwd")),
    "found": [d for d, _, _ in os.walk("/") if "@NAME@" in d],
    "mounts": [r[4] for r in rows],
    "pids": sorted(int(p) for p in os.listdir("/proc") if p.isdigit()),
    "sys": os.listdir("/sys") if os.path.isdir("/sys") else [],  # the image's empty dir
}))
'''


async def test_c02_no_host_filesystem_is_visible(tool):
    canary = Path.home() / f".vestibule-canary-{secrets.token_hex(6)}"
    canary.mkdir()
    (canary / "id_ed25519").write_text("not for the guest")
    try:
        text = await tool.run("python", _FS.replace("@CANARY@", str(canary / "id_ed25519"))
                              .replace("@NAME@", canary.name), timeout_s=60)
    finally:
        (canary / "id_ed25519").unlink()
        canary.rmdir()
    assert _isolation(text) == ("native", None), text
    f = _result(text)
    assert (f["exists"], f["root"], f["cwd"], f["found"]) == (False, True, True, []), f
    masked = [f"/proc/{p}" for p in native.MASKED if os.path.exists(f"/proc/{p}")]
    read_only = [f"/proc/{p}" for p in native.READ_ONLY if os.path.exists(f"/proc/{p}")]
    assert f["mounts"] == [
        "/", "/proc", "/dev", *[f"/dev/{d}" for d in native._DEVICES], "/dev/pts", "/dev/shm",
        "/tmp", "/workspace", "/sandbox", *masked, *read_only]
    assert f["pids"] == [1, 2] and f["sys"] == []  # no sysfs: nothing mounted on /sys


_NET = r'''
import json, socket
def fails(fn):
    try:
        fn()
    except OSError as e:
        return e.errno or True
    return None
def udp():
    socket.socket(socket.AF_INET, socket.SOCK_DGRAM).sendto(b"x", ("1.1.1.1", 53))
print("RESULT " + json.dumps({
    "tcp": fails(lambda: socket.create_connection(("1.1.1.1", 53), 2)),
    "dns": fails(lambda: socket.getaddrinfo("example.com", 80)),
    "udp": fails(udp),
    "vsock": fails(lambda: socket.socket(40, socket.SOCK_STREAM)),
    "ifaces": [line.split(":")[0].strip()
               for line in open("/proc/net/dev").read().splitlines()[2:]],
}))
'''


async def test_c03_no_network_and_no_vsock(tool):
    text = await tool.run("python", _NET)
    assert _isolation(text) == ("native", None), text
    f = _result(text)
    assert None not in (f["tcp"], f["dns"], f["udp"]), f
    assert f["vsock"] == 1 and f["ifaces"] == ["lo"], f  # EPERM: the filter, not the netns
    assert "blocked syscalls: socket" in text, text


_OOM_CHILD = ("import subprocess, sys\n"
              "p = subprocess.run([sys.executable, '-c', 'b = b\"x\" * (1 << 30)'])\n"
              "print('child', p.returncode)\n")


async def test_c04_a_memory_bomb_is_killed_and_the_next_run_works(tool):
    text = await tool.run("python", _OOM_CHILD)
    assert "child -9" in text and _isolation(text) == ("native", None), text
    assert "[out of memory: the kernel killed a process in the sandbox at its 256 MiB limit]" \
        in text, text
    warden = tool.selector._selection.warden
    after = await tool.run("python", "print('still fine')")
    assert "still fine" in after and _isolation(after) == ("native", None), after
    assert tool.selector._selection.warden is warden  # the selection was kept


_FORK_UNTIL_REFUSED = r'''
import errno, json, os, time
n, error = 0, None
try:
    while n < @BOUND@:  # bounded: a missing limit must fail the test, not flood the host
        if os.fork() == 0:
            time.sleep(30)
            os._exit(0)
        n += 1
except OSError as e:
    error = errno.errorcode[e.errno]
print("RESULT " + json.dumps({"forks": n, "error": error}))
'''
_CLASSIC_FORK_BOMB = ":(){ :|:& };:\nexec sleep 300\n"


async def test_c05_a_fork_bomb_stops_at_pids_max(tool):
    cap = tool.limits.pids_max
    # (a) Controlled: refused at the cap, init and the guest counting.
    text = await tool.run("python", _FORK_UNTIL_REFUSED.replace("@BOUND@", str(4 * cap)))
    assert _isolation(text) == ("native", None), text  # pids.max, not the RLIMIT backstop
    assert _result(text) == {"forks": cap - 2, "error": "EAGAIN"}, text
    # (b) The classic bash bomb, only now that (a) showed the limit biting.
    t0 = time.monotonic()
    text = await tool.run("bash", _CLASSIC_FORK_BOMB, timeout_s=8)
    assert time.monotonic() - t0 <= 8 + 2 + 3, text
    assert _isolation(text) == ("native", None), text
    assert "Resource temporarily unavailable" in text, text  # the limit was hit
    await tool.drain()
    after = await tool.run("python", "print('still fine')")
    assert "still fine" in after and _isolation(after) == ("native", None), after


_BUSY = ("import time\nt0 = time.time()\nwhile time.time() - t0 < 2:\n    pass\n"
         "print('RESULT {\"ratio\": %f}' % (time.process_time() / (time.time() - t0)))\n")


async def test_c06_a_busy_loop_stays_under_the_cpu_limit(tool):
    text = await tool.run("python", _BUSY)
    assert _isolation(text) == ("native", None), text
    assert _result(text)["ratio"] <= 0.85, text  # cpu_pct 75; no lower bound (a loaded host)


_DISK = r'''
import errno, json, os
def write(path):
    try:
        with open(path, "wb") as f:
            f.write(b"x")
        return "ok"
    except OSError as e:
        return errno.errorcode[e.errno]
tmp = "ok"
try:
    with open("/tmp/fill", "wb") as f:
        for _ in range(1024):
            f.write(b"x" * (1 << 20))
            f.flush()
except OSError as e:
    tmp = errno.errorcode[e.errno]
size = os.path.getsize("/tmp/fill")
print("RESULT " + json.dumps({"tmp": tmp, "size": size, "root": write("/x"),
                              "sandbox": write("/sandbox/x")}))
'''


async def test_c07_tmp_is_capped_and_the_rootfs_is_read_only(tool):
    text = await tool.run("python", _DISK)
    assert _isolation(text) == ("native", None), text
    f = _result(text)
    assert f["tmp"] == "ENOSPC" and f["size"] <= tool.limits.tmpfs_mb << 20, f
    assert (f["root"], f["sandbox"]) == ("EROFS", "EROFS"), f


@pytest.mark.parametrize("guest", [
    "import subprocess\nsubprocess.Popen(['bash', '-c', 'exec -a TOKEN sleep 600'])\n"
    "while True:\n    pass\n",
    "import os, signal, subprocess\nsubprocess.Popen(['bash', '-c', 'exec -a TOKEN sleep 600'])\n"
    "os.kill(os.getpid(), signal.SIGSTOP)\n",
], ids=["busy-loop", "self-sigstop"])
async def test_c08_a_run_past_its_timeout_is_stopped_and_nothing_survives(tool, guest):
    await tool.run("python", "print('warm')")  # selection isn't timed
    token = _token()
    t0 = time.monotonic()
    text = await tool.run("python", guest.replace("TOKEN", token), timeout_s=2)
    assert _TIMED_OUT in text, text
    assert time.monotonic() - t0 <= 2 + 2 + 3, text  # the timeout, the 2 s grace, a margin
    await tool.drain()
    assert holders(token) == []


_DENIALS = r'''
import ctypes, json, socket
libc = ctypes.CDLL(None, use_errno=True)
def call(rc):
    return 0 if rc == 0 else ctypes.get_errno()
print("RESULT " + json.dumps({
    "mount": call(libc.mount(b"none", b"/tmp/x", b"tmpfs", 0, None)),
    "unshare": call(libc.unshare(0x10000000)),
    "keyctl": call(libc.syscall(250, 0, 0, 0, 0, 0)),
}))
'''
_STORM = r'''
import ctypes, os, subprocess, threading, time
libc = ctypes.CDLL(None, use_errno=True)
subprocess.Popen(["bash", "-c", "exec -a TOKEN sleep 600"])
def spin():
    for _ in range(3000):
        libc.mount(b"none", b"/tmp/x", b"tmpfs", 0, None)
threads = [threading.Thread(target=spin) for _ in range(20)]
for t in threads:
    t.start()
for t in threads:
    t.join()
while not os.path.exists("/workspace/go"):
    time.sleep(0.02)
print("done")
'''


async def test_c09_denials_are_named_and_the_storm_stays_in_the_envelope(tool):
    text = await tool.run("python", _DENIALS)
    assert _isolation(text) == ("native", None), text
    assert _result(text) == {"mount": 1, "unshare": 1, "keyctl": 1}, text  # EPERM each
    assert re.search(r"^blocked syscalls: mount, unshare, keyctl$", text, re.M), text

    token = _token()
    task = asyncio.create_task(tool.run("python", _STORM.replace("TOKEN", token)))
    try:
        t0 = time.monotonic()
        while not (found := holders(token)):  # kept: mid-exec, the cmdline can read empty
            assert time.monotonic() - t0 < 10, "the storm guest never started"
            await asyncio.sleep(0.02)
        with open(f"/proc/{found[0]}/cgroup") as f:
            scope = f.read().strip().removeprefix("0::").rpartition("/guest/sandbox")[0]
        root = native_cgroup.CGROUP_FS + scope
        helpers = _own_helpers()
        assert len(helpers) == 1, helpers
        with open(f"/proc/{helpers[0]}/cgroup") as f:  # the helper answering the storm...
            assert f.read().strip() == f"0::{scope}/supervisor"
        # ...is charged inside the scope, whose own cpu.max is the envelope (S4-D6)
        assert Path(f"{root}/cpu.max").read_text().strip() == "100000 100000"
    finally:  # a failure above still ends the storm, so teardown doesn't add a false error
        (Path(tool.limits.workspace_dir) / "go").touch()
        text = await task
    assert "done" in text and _isolation(text) == ("native", None), text
    assert re.search(r"^blocked syscalls: mount$", text, re.M), text  # deduplicated
    assert "reporting stopped" in text, text  # the 256 cap


_VIEW = r'''
import json, os, socket
def fails(fn):
    try:
        fn()
    except OSError as e:
        return type(e).__name__
    return None
status = {k: v.strip() for k, _, v in (line.partition(":") for line in open("/proc/self/status"))}
print("RESULT " + json.dumps({
    "ids": [os.getuid(), os.getgid()], "groups": os.getgroups(),
    "fds": [fd for fd in range(3, 256) if fails(lambda fd=fd: os.fstat(fd)) is None],
    "caps": [status[k] for k in ("CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb")],
    "nnp": status["NoNewPrivs"], "seccomp": status["Seccomp"], "env": dict(os.environ),
    "proc1": [fails(fn) for fn in (lambda: open("/proc/1/environ").read(),
                                   lambda: os.listdir("/proc/1/fd"),
                                   lambda: os.readlink("/proc/1/cwd"),
                                   lambda: os.readlink("/proc/1/root"),
                                   lambda: open("/proc/1/mem", "rb").read(1))],
    "cmdline": open("/proc/1/cmdline", "rb").read().decode(errors="replace"),
    "hostname": socket.gethostname(), "cwd": os.getcwd(),
}))
'''


async def test_c10_the_guests_own_view(tool):
    text = await tool.run("python", _VIEW)
    assert _isolation(text) == ("native", None), text
    f = _result(text)
    assert f["ids"] == [os.getuid(), os.getgid()]
    assert set(f["groups"]) <= {os.getgid(), 65534}  # others show as the overflow gid (N6)
    assert f["fds"] == [] and set(f["caps"]) == {"0000000000000000"}
    assert (f["nnp"], f["seccomp"]) == ("1", "2")  # the guest's own filter, seen from inside
    image = rootfs.find(tool.limits.rootfs_path, tool.limits.image_python)
    assert image is not None and f["env"] == native._guest_env(image.env)
    assert f["proc1"] == ["PermissionError"] * 5
    assert f["cmdline"].startswith("vestibule-warden") and "/home" not in f["cmdline"]
    assert (f["hostname"], f["cwd"]) == ("vestibule", "/workspace")


async def test_c11_a_server_killed_mid_run_leaves_nothing(tmp_path):
    token = _token()
    srv_proc = await StdioServer.start(tmp_path / "ws", {"VESTIBULE_BACKEND": "native"})
    try:
        await srv_proc.initialize()
        await srv_proc.call("run_code", {"language": "python", "code": "print('warm')"})
        seen = len(srv_proc.run_ids)
        await srv_proc.send_request("tools/call", {"name": "run_code", "arguments": {
            "language": "bash", "code": f"exec -a {token} sleep 600", "timeout_seconds": 60}})
        await srv_proc.wait_for_new_spawn(seen)
        t0 = time.monotonic()
        while not holders(token):
            assert time.monotonic() - t0 < 10, "the guest never started"
            await asyncio.sleep(0.02)
        await srv_proc.kill()  # SIGKILL: no handler runs
        t0 = time.monotonic()
        own = {f"vestibule-{rid}.scope" for rid in srv_proc.run_ids}
        while (holders(token) or own & _live_scopes()) and time.monotonic() - t0 < 2:
            await asyncio.sleep(0.02)
        assert holders(token) == [] and not own & _live_scopes()
    finally:
        await srv_proc.kill()


async def test_c12_four_runs_at_once_get_four_pid_namespaces(tool):
    await tool.run("python", "print('warm')")
    code = ("import os, time\ntime.sleep(1)\n"
            "print('RESULT {\"ns\": %d}' % os.stat('/proc/self/ns/pid').st_ino)\n")
    texts = await asyncio.gather(*(tool.run("python", code) for _ in range(4)))
    assert all(_isolation(t) == ("native", None) for t in texts), texts
    assert len({_result(t)["ns"] for t in texts}) == 4


async def test_c13_a_rootfs_removed_after_selection_is_noticed_through_the_server(
        tool, monkeypatch):
    """The server's own `note_result` hook drops the selection (S7 §3.4); the call after that
    selects again and names the fix."""
    text = await tool.run("python", "print('first')")
    assert _isolation(text) == ("native", None), text
    monkeypatch.setattr(rootfs, "find", lambda *_: None)  # removed under a running server
    text = await tool.run("python", "print('second')")
    isolation, detail = _isolation(text)
    assert isolation == "none" and detail.endswith("nothing was executed"), text
    text = await tool.run("python", "print('third')")
    assert text.startswith("Blocked:") and "vestibule-mcp setup" in text, text
