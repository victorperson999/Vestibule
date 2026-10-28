"""M1 acceptance suite (step 7b): one test per criterion of the M1 contract §9
(docs/plans/M1/M1-container-backend.md), each driven through `server.call_tool` — the
dispatch an agent's tool call reaches — with a fresh BackendSelector and a temp
workspace, so real selection, image preflight, the run, and result formatting are all
in the path. Docker-marked: skips (with the pull commands) where the daemon or the
pinned images are missing. Plan: docs/plans/M1/M1-step7-acceptance.md §2 and §5.

Criterion -> evidence:
  1  hello in python/bash/node => `container`   test_c01_*; test_stdio_e2e.py::test_stdio_happy_path_and_clean_exit
  2  TCP and DNS fail                           test_c02_*
  3  memory bomb contained                      test_c03_*
  4  fork bomb contained                        test_c04_*
  5  tmpfs cap + read-only rootfs               test_c05_*
  6  infinite loop => timed out, no survivor    test_c06_*
  7  4 concurrent runs, distinct containers     test_c07_*
  8  stale labeled container reaped             test_c08_*
  9  innocent code works under --read-only      test_c09_*
  10 jail suite + guest-planted symlink         test_c10_*; tests/test_workspace.py (unit suite)
  11 degraded path => container-degraded       test_c11_*; tests/test_select.py (Docker-free cases)
  12 no runtime / no image => Blocked          test_c12_*; tests/test_select.py (daemon down, mocked)
  13 malformed args => Blocked                 tests/test_validation.py (mapped only)
  14 ruff + mypy + suite; Docker-free elsewhere the gate, plus one run with Docker hidden from PATH
Also: test_hard_and_soft_controls_apply_inside_the_guest (every §3 control, observed
from inside), and golden rule 1 over a real pipe in test_stdio_e2e.py.

Every result is checked against the honest isolation values; `native` can never
appear in M1. "Host unaffected" is asserted as what is observable from here (on
Docker Desktop the guest runs in a Linux VM): the guest was stopped and the cgroup says
why, the server returned a normal result, the next run works, and no container survived.
"""
import asyncio
import json
import re
import secrets
import shutil
import subprocess
import time

import pytest
from dockerutil import docker_required, plant, ps_names, remove

from vestibule import server as srv
from vestibule.backends.container import ContainerBackend
from vestibule.backends.select import BackendSelector
from vestibule.config import Limits

pytestmark = pytest.mark.docker

_ISOLATION = re.compile(r"^isolation: (\S+)(?: \((.*)\))?$", re.M)
_HONEST = {"container", "container-degraded", "none"}
_TIMED_OUT = "[timed out and was terminated]"


def _isolation(text: str) -> tuple[str, str | None]:
    m = _ISOLATION.search(text)
    assert m, f"no isolation line:\n{text}"
    return m.group(1), m.group(2)


def _result(text: str) -> dict:
    """The JSON object a guest printed on its `RESULT {...}` line."""
    m = re.search(r"^RESULT (\{.*\})$", text, re.M)
    assert m, text
    return json.loads(m.group(1))


class Tool:
    """The two MCP tools, called the way the SDK calls them, plus test helpers."""

    def __init__(self, limits: Limits, selector: BackendSelector) -> None:
        self.limits = limits
        self.selector = selector

    async def run(self, language: str, code: str, timeout_s: int = 30) -> str:
        out = await srv.call_tool(
            "run_code", {"language": language, "code": code, "timeout_seconds": timeout_s}
        )
        text = "\n".join(c.text for c in out)
        assert not text.startswith("Internal error"), text
        assert "isolation: native" not in text, text  # M1 never claims native
        if not text.startswith("Blocked:"):
            lines = _ISOLATION.findall(text)
            assert len(lines) == 1 and lines[0][0] in _HONEST, text
        return text

    async def read(self, path: str = ".") -> str:
        out = await srv.call_tool("read_workspace", {"path": path})
        return "\n".join(c.text for c in out)

    def owner(self) -> str:
        """Owner label of the selected backend (every run of this tool carries it)."""
        sel = self.selector._selection
        assert sel is not None, "no selection yet"
        return sel.warden._owner  # type: ignore[attr-defined]

    async def settle(self, within_s: float = 30) -> bool:
        """True once no container from this tool's backends exists.

        Drains first: an empty listing proves nothing while a `docker run` CLI may
        still create its container (the 2026-10-14 flake in test_container.py).
        """
        backends = list(self.selector._backends)
        for b in backends:
            assert await b.drain(60), "cleanup tasks still pending after 60 s"
        owners = [b._owner for b in backends]
        deadline = time.monotonic() + within_s
        while any(ps_names(f"label=vestibule.owner={o}") for o in owners):
            if time.monotonic() > deadline:
                return False
            await asyncio.sleep(0.5)
        return True


@pytest.fixture()
async def make_tool(tmp_path, monkeypatch):
    """Swap in a fresh selector and temp-workspace limits (plus any overrides) for
    the server's globals; at teardown, shut each selector down as the server does."""
    made: list[Tool] = []

    def make(**overrides) -> Tool:
        # M1's exam is the container path; `auto` would try native first on Linux (S6-D8)
        limits = Limits(**{"workspace_dir": str(tmp_path / "ws"), "backend": "container",
                           **overrides})
        tool = Tool(limits, BackendSelector())
        monkeypatch.setattr(srv, "LIMITS", limits)
        # Computed at import from the dev shell's VESTIBULE_* settings; these limits replace them.
        monkeypatch.setattr(srv, "CONFIG_ERROR", None)
        monkeypatch.setattr(srv, "SELECTOR", tool.selector)
        made.append(tool)
        return tool

    yield make
    for tool in made:
        clean = await tool.selector.aclose(rm_budget_s=15, wait_budget_s=60)
        assert clean, "cleanup still pending at teardown"


@pytest.fixture()
def tool(make_tool) -> Tool:
    return make_tool()


# ------------------------------------------------------------------- criteria 1-12


@docker_required
async def test_c01_hello_in_three_languages_reports_container(tool):
    for language, code, marker in (
        ("python", "print('hi-py')", "hi-py"),
        ("bash", "echo hi-bash", "hi-bash"),
        ("node", "console.log('hi-node')", "hi-node"),
    ):
        text = await tool.run(language, code)
        assert "exit_code: 0" in text and marker in text, text
        assert _isolation(text) == ("container", None), text


_NETWORK = r'''
import json, socket
res = {}
try:
    socket.create_connection(("1.1.1.1", 443), timeout=5).close()
    res["tcp"] = "CONNECTED"
except OSError as e:
    res["tcp"] = f"failed: {e!r}"
try:
    res["dns"] = f"RESOLVED {socket.getaddrinfo('example.com', 443)[0][4]}"
except OSError as e:
    res["dns"] = f"failed: {e!r}"
try:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.sendto(b"x", ("8.8.8.8", 53))
    res["udp"] = "SENT"
except OSError as e:
    res["udp"] = f"failed: {e!r}"
res["interfaces"] = sorted(name for _, name in socket.if_nameindex())
print("RESULT " + json.dumps(res))
'''


@docker_required
async def test_c02_tcp_dns_and_udp_all_fail(tool):
    res = _result(await tool.run("python", _NETWORK))
    for kind in ("tcp", "dns", "udp"):
        assert res[kind].startswith("failed"), res
    assert res["interfaces"] == ["lo"], res  # an empty network namespace


_MEMORY_BOMB = r'''
import json, os
pid = os.fork()
if pid == 0:  # the bomb: touch ~1 GiB against the memory cap
    hog = [b"\x01" * (1 << 20) for _ in range(1024)]
    os._exit(0)
_, status = os.waitpid(pid, 0)
events = dict(line.split() for line in open("/sys/fs/cgroup/memory.events"))
print("RESULT " + json.dumps({
    "child_signal": os.WTERMSIG(status) if os.WIFSIGNALED(status) else None,
    "oom_kill": int(events.get("oom_kill", 0)),
}))
'''


@docker_required
async def test_c03_memory_bomb_is_oom_killed_and_the_sandbox_carries_on(tool):
    text = await tool.run("python", _MEMORY_BOMB)
    assert "exit_code: 0" in text, text  # the parent survived to report
    res = _result(text)
    assert res["child_signal"] == 9, res  # SIGKILL ...
    assert res["oom_kill"] >= 1, res  # ... and the memory cgroup says it was the OOM killer
    assert _isolation(text) == ("container", None)

    selection = tool.selector._selection
    after = await tool.run("python", "print('still fine')")
    assert "still fine" in after and _isolation(after) == ("container", None), after
    assert tool.selector._selection is selection  # no re-selection was needed
    assert await tool.settle()


_FORK_UNTIL_REFUSED = r'''
import errno, json, os, time
pids, err = [], None
for _ in range(@BOUND@):  # bounded: a missing cap must fail the test, not hang it
    try:
        pid = os.fork()
    except OSError as e:
        err = errno.errorcode.get(e.errno)
        break
    if pid == 0:
        time.sleep(60)
        os._exit(0)
    pids.append(pid)
current = open("/sys/fs/cgroup/pids.current").read().strip()
for p in pids:
    os.kill(p, 9)
for p in pids:
    os.waitpid(p, 0)
print("RESULT " + json.dumps({"forked": len(pids), "error": err, "current": current}))
'''
_CLASSIC_FORK_BOMB = ":(){ :|:& };:\nexec sleep 300\n"


@docker_required
async def test_c04_fork_bomb_is_capped_and_the_sandbox_carries_on(tool):
    cap = tool.limits.pids_max
    # (a) Controlled: forks are refused at the cap (tini, timeout and the interpreter
    # hold the first few pids), never hundreds past it.
    res = _result(await tool.run(
        "python", _FORK_UNTIL_REFUSED.replace("@BOUND@", str(cap + 64))
    ))
    assert res["error"] == "EAGAIN", res
    assert cap - 8 <= res["forked"] < cap, res
    assert res["current"] == str(cap), res

    # (b) The classic bash bomb, only now that (a) showed the cap biting. `exec sleep`
    # holds the container open without a fork while the bomb runs against the cap.
    t0 = time.monotonic()
    text = await tool.run("bash", _CLASSIC_FORK_BOMB, timeout_s=8)
    assert time.monotonic() - t0 <= 8 + 20, text
    assert re.search(r"^exit_code: -?\d+$", text, re.M), text  # a normal result
    assert "Resource temporarily unavailable" in text, text  # the cap was hit
    assert await tool.settle(), "a fork-bomb container survived"
    after = await tool.run("python", "print('still fine')")
    assert "still fine" in after and _isolation(after) == ("container", None), after


_DISK = r'''
import errno, json, os
res = {"tmp_written": 0}
try:
    with open("/tmp/fill", "wb", buffering=0) as f:
        chunk = b"\0" * (1 << 20)
        for _ in range(1024):
            f.write(chunk)
            res["tmp_written"] += len(chunk)
except OSError as e:
    res["tmp_error"] = errno.errorcode.get(e.errno)
os.remove("/tmp/fill")
# /var/tmp is world-writable in the image, so only a read-only mount can refuse uid
# 1000 there: EROFS, not EACCES (which a plain permission check on /usr/bin also gives).
for path in ("/var/tmp/vestibule-c05", "/sandbox/vestibule-c05"):
    try:
        open(path, "w").close()
        res[path] = "created"
    except OSError as e:
        res[path] = errno.errorcode.get(e.errno)
res["root_mount"] = [
    line.split()[3].split(",")[0] for line in open("/proc/self/mounts")
    if line.split()[1] == "/"
]
print("RESULT " + json.dumps(res))
'''


@docker_required
async def test_c05_tmpfs_is_capped_and_the_rootfs_is_read_only(tool):
    res = _result(await tool.run("python", _DISK))
    cap = tool.limits.tmpfs_mb * 1024 * 1024
    assert res["tmp_error"] == "ENOSPC", res
    assert cap - 2 * 1024 * 1024 <= res["tmp_written"] <= cap, res
    assert res["/var/tmp/vestibule-c05"] == "EROFS", res
    assert res["/sandbox/vestibule-c05"] == "EROFS", res
    assert res["root_mount"] == ["ro"], res


@docker_required
async def test_c06_infinite_loop_times_out_and_leaves_no_container(tool):
    await tool.run("python", "print('warm-up')")  # selection is not part of the timing
    t0 = time.monotonic()
    text = await tool.run("python", "while True:\n    pass\n", timeout_s=3)
    elapsed = time.monotonic() - t0
    assert _TIMED_OUT in text, text
    assert elapsed <= 3 + 20, f"{elapsed:.1f}s"
    assert await tool.settle(), "the timed-out container survived"


@docker_required
async def test_c07_four_concurrent_runs_get_four_containers(tool):
    await tool.run("python", "print('warm-up')")
    code = "import socket\nprint('host=' + socket.gethostname())\nprint('tok-{i}')\n"
    texts = await asyncio.gather(
        *(tool.run("python", code.replace("{i}", str(i))) for i in range(4))
    )
    hosts = set()
    for i, text in enumerate(texts):
        assert "exit_code: 0" in text and f"tok-{i}" in text, text
        assert _isolation(text) == ("container", None), text
        m = re.search(r"^host=(\S+)$", text, re.M)
        assert m, text
        hosts.add(m.group(1))  # the hostname is the container id
    assert len(hosts) == 4, hosts


@docker_required
async def test_c08_stale_labeled_container_is_reaped_on_first_use(tool, monkeypatch):
    # Any Vestibule process's reaper on this machine would remove the planted container,
    # which alone proves nothing here: THIS tool's reaper must select it, by name.
    selected: list[str] = []
    decide = ContainerBackend._reap_decision

    def spy(name: str, deadline_raw: str, now: float, active: set[str]) -> bool:
        doomed = decide(name, deadline_raw, now, active)
        if doomed:
            selected.append(name)
        return doomed

    monkeypatch.setattr(ContainerBackend, "_reap_decision", staticmethod(spy))
    name = f"vestibule-plant-{secrets.token_hex(4)}"
    plant(name, {"vestibule.run": "1", "vestibule.deadline": "1"})  # long past
    try:
        assert ps_names(f"name=^{name}$") == name
        text = await tool.run("python", "print('first use')")
        assert "first use" in text, text
        deadline = time.monotonic() + 30
        while ps_names(f"name=^{name}$"):
            assert time.monotonic() < deadline, "the stale container was not reaped"
            await asyncio.sleep(0.5)
        assert name in selected, "removed, but not by this tool's reaper"
    finally:
        remove(name)


_SAVE_MODULES = r'''
import os
os.makedirs("/workspace/mypkg", exist_ok=True)
with open("/workspace/mypkg/__init__.py", "w") as f:
    f.write("from .util import double\n")
with open("/workspace/mypkg/util.py", "w") as f:
    f.write("def double(x):\n    return 2 * x\n")
with open("/workspace/helper.js", "w") as f:
    f.write("module.exports = { triple: (x) => 3 * x };\n")
print("saved")
'''
_USE_MODULES = r'''
import sys
sys.path.insert(0, "/workspace")  # the documented idiom (run_code description)
import csv, decimal, email, http.client, json, os, pathlib, sqlite3, tempfile
import xml.etree.ElementTree
import mypkg
db = sqlite3.connect(":memory:")
db.execute("create table t (x)")
home = pathlib.Path.home() / ".vestibule-c09"
home.write_text("home ok")
fd, tmp = tempfile.mkstemp()
os.write(fd, b"tmp ok")
os.close(fd)
print("RESULT " + json.dumps({
    "double": mypkg.double(21),
    "home": home.read_text(),
    "tmp": open(tmp).read(),
}))
'''


@docker_required
async def test_c09_innocent_multi_module_code_runs_clean_under_read_only(tool):
    assert "saved" in await tool.run("python", _SAVE_MODULES)

    text = await tool.run("python", _USE_MODULES)
    assert "exit_code: 0" in text and "stderr:" not in text, text
    assert _result(text) == {"double": 42, "home": "home ok", "tmp": "tmp ok"}

    text = await tool.run(
        "node", "const h = require('/workspace/helper.js'); console.log('triple=' + h.triple(14));"
    )
    assert "triple=42" in text and "stderr:" not in text, text

    ws = tool.limits.workspace_path
    assert not list(ws.rglob("__pycache__")), "bytecode was written to the workspace"


_PLANT_LINKS = r'''
import os
os.symlink("../canary.txt", "/workspace/rel-link")  # resolves to the host canary
os.symlink("/etc/passwd", "/workspace/abs-link")
os.symlink("..", "/workspace/up-link")
print("planted")
'''
_LINKS = ("rel-link", "abs-link", "up-link")


@docker_required
async def test_c10_guest_planted_symlinks_are_refused_by_read_workspace(tool, tmp_path):
    canary = f"CANARY-{secrets.token_hex(8)}"
    (tmp_path / "canary.txt").write_text(canary)  # just outside the workspace
    try:
        assert "planted" in await tool.run("python", _PLANT_LINKS)
        outputs = [listing := await tool.read(".")]
        for name in _LINKS:
            assert re.search(rf"^symlink\s+\d+\s+{name}$", listing, re.M), listing
            outputs.append(text := await tool.read(name))
            assert text.startswith("Blocked:"), text
        outputs.append(text := await tool.read("up-link/canary.txt"))
        assert text.startswith("Blocked:"), text
        for text in outputs:
            assert canary not in text and "root:x:0:0" not in text, text
    finally:
        await tool.run("bash", "rm -f " + " ".join(f"/workspace/{n}" for n in _LINKS))


@docker_required
async def test_c11_unenforceable_limits_report_container_degraded(make_tool):
    # Real injection: Docker refuses a 1 MB memory limit (its minimum is 6 MB), so the
    # full-profile probe fails and the all-soft-off retry (S5-D2) is what passes.
    tool = make_tool(mem_mb=1)
    text = await tool.run("python", "print('degraded but running')")
    assert "degraded but running" in text, text
    assert _isolation(text) == (
        "container-degraded", "limits not applied: cpu, memory, pids, tmpfs-size"
    ), text


@pytest.mark.skipif(shutil.which("podman") is not None, reason="podman is installed here")
async def test_c12_missing_runtime_is_an_actionable_refusal(make_tool):
    tool = make_tool(runtime="podman")
    text = await tool.run("python", "print('never runs')")
    assert text.startswith("Blocked: no usable container runtime (podman: not installed)"), text
    assert "restart your agent" in text, text


@docker_required
async def test_c12_missing_image_is_an_actionable_refusal(make_tool, monkeypatch):
    never_pulled = "docker.io/library/python@sha256:" + "0" * 64
    tool = make_tool(image_python=never_pulled)
    subcommands: list[str] = []
    real_spawn = asyncio.create_subprocess_exec

    async def spy(*cmd, **kw):
        subcommands.append(cmd[1] if len(cmd) > 1 else "")
        return await real_spawn(*cmd, **kw)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spy)
    for _ in range(2):  # the second call gets the cached refusal
        text = await tool.run("python", "print('never runs')")
        assert text.startswith(f"Blocked: image {never_pulled} is not usable"), text
        assert f"`docker pull {never_pulled}`" in text, text
    assert "run" not in subcommands and "pull" not in subcommands, subcommands


# ---------------------------------------------- every §3 control, seen from inside


_CONTROLS = r'''
import json, os
status = dict(
    line.rstrip("\n").split(":\t", 1) for line in open("/proc/self/status") if ":\t" in line
)


def cgroup(name):
    try:
        return open("/sys/fs/cgroup/" + name).read().strip()
    except OSError:
        return None


st = os.statvfs("/tmp")
print("RESULT " + json.dumps({
    "uid": os.getuid(),
    "status": {k: status.get(k) for k in
               ("CapEff", "CapPrm", "CapBnd", "CapAmb", "NoNewPrivs", "Seccomp")},
    "env": sorted(os.environ),
    "sockets": [p for p in ("/var/run/docker.sock", "/run/docker.sock",
                            "/run/podman/podman.sock") if os.path.exists(p)],
    "root_mount": [line.split()[3].split(",")[0] for line in open("/proc/self/mounts")
                   if line.split()[1] == "/"],
    "memory.max": cgroup("memory.max"),
    "memory.swap.max": cgroup("memory.swap.max"),
    "pids.max": cgroup("pids.max"),
    "cpu.max": cgroup("cpu.max"),
    "tmp_bytes": st.f_blocks * st.f_frsize,
}))
'''
_PROFILE_ENV = {"HOME", "TMPDIR", "PYTHONDONTWRITEBYTECODE", "NODE_OPTIONS"}


def _image_env_keys(image: str) -> set[str]:
    r = subprocess.run(
        ["docker", "image", "inspect", "--format", "{{json .Config.Env}}", image],
        capture_output=True, text=True, timeout=15, stdin=subprocess.DEVNULL, check=True,
    )
    return {kv.split("=", 1)[0] for kv in json.loads(r.stdout) or []}


@docker_required
async def test_hard_and_soft_controls_apply_inside_the_guest(tool, monkeypatch):
    """The hard tier (D3 refuses to run without it) and the soft limits, observed
    from inside the guest rather than read off the command line."""
    monkeypatch.setenv("VESTIBULE_CANARY_ENV", "leaked")  # set in the server process
    lim = tool.limits
    res = _result(await tool.run("python", _CONTROLS))

    assert res["uid"] != 0, res
    zero = "0000000000000000"
    assert res["status"] == {
        "CapEff": zero, "CapPrm": zero, "CapBnd": zero, "CapAmb": zero,
        "NoNewPrivs": "1", "Seccomp": "2",
    }, res
    # Only the four variables the profile sets, the image's own, and the hostname
    # the runtime adds; nothing from the server's environment.
    env = set(res["env"])
    assert _PROFILE_ENV <= env, env
    assert env <= _PROFILE_ENV | _image_env_keys(lim.image_python) | {"HOSTNAME"}, env
    assert res["sockets"] == [], res
    assert res["root_mount"] == ["ro"], res

    assert res["memory.max"] == str(lim.mem_mb * 1024 * 1024), res
    assert res["memory.swap.max"] == "0", res
    assert res["pids.max"] == str(lim.pids_max), res
    assert res["cpu.max"] == f"{lim.cpu_pct * 1000} 100000", res
    assert res["tmp_bytes"] == lim.tmpfs_mb * 1024 * 1024, res
