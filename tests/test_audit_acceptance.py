"""M3 acceptance suite (step 5a): the criteria of the M3 contract §5
(docs/plans/M3/M3-audit-usage.md), driven through `server.call_tool` (the dispatch an agent's
tool call reaches) with a fresh BackendSelector, a temp workspace and a real AuditLog on a temp
dir. Real selection, the run, the result's rendering, both audit records and the writer thread
are then all in the path. Plan: docs/plans/M3/M3-step5-acceptance.md §2, §3.1 and §6.1.

A test is here only where real backends or a real run are what's unproven; a behavior already
tested with the real component is mapped instead (plan §2's rule, §3.2). Markers are per test:
native tests need Linux and the trees `vestibule-mcp setup` made, container tests a Docker
daemon with both pinned images, and the rest run everywhere.

Criterion -> evidence:
  1  the right records per call         test_c1_* (native ran, timed_out, cancelled, a full
                                        slot refused, a removed rootfs not_run, blocked;
                                        container ran with its image id, a missing image
                                        refused); test_audit.py's outcome table (the outer
                                        deadline, an unexpected error, a native setup error);
                                        test_native.py::test_an_empty_tree_fails_setup_at_mounts
                                        (the setup error's ran=False, the real helper)
  2  native usage is real               test_c2_*
  3  numbers come only from outside     test_c3_*
  4  missing counters are named         test_c4_* (no bus); test_native.py::
                                        test_the_counters_wait_is_bounded (no report after a
                                        timeout); test_native_verdict.py::
                                        test_a_missing_counter_is_named_with_its_reason and the
                                        helper's counter-file tests (an old kernel)
  5  the audit dir                      test_c5_* (native can't see it); test_audit.py::
                                        test_the_dir_it_makes_is_0700_and_the_file_0600;
                                        test_config.py::test_stdio_relative_audit_dir_blocks_
                                        every_call and test_audit_dir_refused (an overlap is
                                        the same config error); test_lifecycle.py::
                                        test_the_container_command_never_names_the_audit_dir
  6  a failed write                     test_c6_* (a real log that can't write, through the
                                        handler); test_audit.py (each failure on a real
                                        AuditLog, and the note's place in the result)
  7  vestibule-mcp audit                test_audit_cli.py (every bullet, the real writer's file,
                                        the console script over a cp1252 pipe)
  8  stdout stays clean                 test_stdio_e2e.py (stdioutil turns the audit on)
  9  stderr never blocks                test_stderr_log.py (step 4's unit and stdio tests)
  10 the live check                     step 5b (BUILD_LOG)
"""
import asyncio
import json
import logging
import platform
import re
import secrets
import shutil
import subprocess
import time
from pathlib import Path

import anyio
import pytest
from dockerutil import docker_required
from nativeutil import cgroups_required, native_required, scopes

from vestibule import audit, rootfs
from vestibule import server as srv
from vestibule.backends import native, native_cgroup
from vestibule.backends.select import BackendSelector
from vestibule.config import Limits

_ISOLATION = re.compile(r"^isolation: (\S+)(?: \((.*)\))?$", re.M)
_ALLOWED = {"native", "native-degraded", "container", "container-degraded", "none"}
_TIMED_OUT = "[timed out and was terminated]"
_STARTED = re.compile(r"run ([0-9a-f]{16}): starting a native sandbox")
_UNMEASURED = "cpu, mem_peak and pids_peak not measured: "
# M3 A4: memory.peak came in Linux 5.19 and pids.peak in 6.1 (native supports 5.9 and later).
_KERNEL = native._kernel(platform.release()) or (0, 0)  # (0, 0): off Linux, where all skip
HAS_MEM_PEAK, HAS_PIDS_PEAK = _KERNEL >= (5, 19), _KERNEL >= (6, 1)


def _native(test):
    return pytest.mark.native(native_required(cgroups_required(test)))


def _docker(test):
    return pytest.mark.docker(docker_required(test))


def _isolation(text: str) -> tuple[str, str | None]:
    m = _ISOLATION.search(text)
    assert m, f"no isolation line:\n{text}"
    return m.group(1), m.group(2)


def _result(text: str):
    """The JSON a guest printed on its `RESULT ...` line."""
    m = re.search(r"^RESULT (.*)$", text, re.M)
    assert m, text
    return json.loads(m.group(1))


def _kinds(records: list[dict]) -> list[tuple[str, str]]:
    return [(r["kind"], r.get("outcome", "-")) for r in records]


def _own_usage(text: str) -> str:
    """The result's own `usage:` line: the last one, after anything the guest printed."""
    return [line for line in text.splitlines() if line.startswith("usage: ")][-1]


def _same_numbers(text: str, end: dict) -> dict:
    """The numbers on the result's own usage line, checked against its `end` record. A counter
    this kernel lacks must be named instead of shown."""
    line = _own_usage(text)

    def field(name: str, unit: str = "") -> int | None:
        m = re.search(rf"\b{name}=(\d+){unit}", line)
        return int(m.group(1)) if m else None

    shown = {"cpu_ms": field("cpu", "ms"), "mem_peak_mb": field("mem_peak", "MiB"),
             "pids_peak": field("pids_peak")}
    assert shown == end["usage"] and field("wall", "ms") == end["wall_ms"], (line, end)
    if not HAS_MEM_PEAK:
        assert "mem_peak not measured: Linux 5.19+ needed" in line, line
    if not HAS_PIDS_PEAK:
        assert "pids_peak not measured: Linux 6.1+ needed" in line, line
    return shown


async def _until(check, within_s: float, what: str) -> None:
    deadline = time.monotonic() + within_s
    while not check():
        assert time.monotonic() < deadline, what
        await asyncio.sleep(0.02)


class Tool:
    """`run_code` called the way the SDK calls it, with the audit file it writes."""

    def __init__(self, limits: Limits, selector: BackendSelector, audit_dir: Path) -> None:
        self.limits = limits
        self.selector = selector
        self.audit_dir = audit_dir
        self.wardens: list = []  # every warden a call ran on, to drain at the end

    async def run(self, language: str, code: str, timeout_s: int = 30) -> str:
        try:
            out = await srv.call_tool(
                "run_code", {"language": language, "code": code, "timeout_seconds": timeout_s})
        finally:  # a cancelled call's warden too; a result can drop the selection (note_result)
            sel = self.selector._selection
            if sel is not None and all(sel.warden is not w for w in self.wardens):
                self.wardens.append(sel.warden)
        text = "\n".join(c.text for c in out)
        assert not text.startswith("Internal error"), text
        if not text.startswith("Blocked:"):
            lines = _ISOLATION.findall(text)
            assert len(lines) == 1 and lines[0][0] in _ALLOWED, text
        return text

    def records(self) -> list[dict]:
        """Every whole record in the audit dir. A result returns after its records are
        confirmed, so they're in the file by then; a cancelled call's `end` isn't waited for
        (A7), so a poll may meet it half-written, without its newline yet."""
        return [json.loads(line) for f in sorted(self.audit_dir.glob("*.jsonl"))
                for line in f.read_text().split("\n")[:-1]]

    async def drain(self) -> None:
        for w in self.wardens:
            assert await w.drain(30), "a finisher was still running"


@pytest.fixture()
async def make_tool(tmp_path, monkeypatch, caplog):
    """Swap a fresh selector, temp-workspace limits and a real AuditLog into the server's
    globals. At teardown, drain and close what was used, and check that none of this test's
    own native scopes is left (M2's rule, limited to this test's runs)."""
    caplog.set_level(logging.INFO, logger="vestibule.native")
    made: list[Tool] = []

    def make(backend: str = "native", audit_dir: Path | None = None, **overrides) -> Tool:
        directory = audit_dir or tmp_path / "audit"
        limits = Limits(**{"workspace_dir": str(tmp_path / "ws"), "backend": backend,
                           "audit_dir": str(directory), **overrides})
        tool = Tool(limits, BackendSelector(), directory)
        monkeypatch.setattr(srv, "LIMITS", limits)
        monkeypatch.setattr(srv, "CONFIG_ERROR", None)  # not the dev shell's VESTIBULE_*
        monkeypatch.setattr(srv, "SELECTOR", tool.selector)
        monkeypatch.setattr(srv, "AUDIT", audit.AuditLog(str(directory)))
        made.append(tool)
        return tool

    yield make
    for tool in made:
        await tool.drain()
        assert await tool.selector.aclose(rm_budget_s=15, wait_budget_s=60)
    own = {f"vestibule-{m.group(1)}.scope" for r in caplog.get_records("call")
           for m in [_STARTED.search(r.getMessage())] if m}
    if any(tool.limits.backend == "native" for tool in made):
        assert own, "no native run was logged, so the scope check below would be empty"
        live = scopes()
        assert live is not None, "systemctl --user failed, so no scope check is possible"
        deadline = time.monotonic() + 2
        while own & live and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
            live = scopes() or live
        assert not own & live, "scopes left behind"


def _run_ids(caplog) -> list[str]:
    return [m.group(1) for r in caplog.records for m in [_STARTED.search(r.getMessage())] if m]


# ------------------------------------------------------- criterion 1: the right records


@_native
async def test_c1_native_records_a_run_a_timeout_and_a_cancel(make_tool, caplog):
    tool = make_tool()
    text = await tool.run("python", "print(1)")
    assert _isolation(text) == ("native", None), text
    start, end = tool.records()
    assert _kinds([start, end]) == [("start", "-"), ("end", "ran")] and start["id"] == end["id"]
    assert (start["backend"], start["verdict"], start["image"]) == (
        "native", "native", tool.limits.image_python)
    image = rootfs.find(tool.limits.rootfs_path, tool.limits.image_python)
    assert image is not None
    assert (end["backend_run"], end["image_ran"]) == (_run_ids(caplog)[-1], image.manifest_digest)

    text = await tool.run("bash", "while :; do :; done", timeout_s=1)
    assert _TIMED_OUT in text, text
    assert _kinds(tool.records()[2:]) == [("start", "-"), ("end", "timed_out")]

    started, returned = tool.limits.workspace_path / "started", []

    async def call():
        returned.append(await tool.run(
            "python", "open('/workspace/started', 'w').close()\nimport time\ntime.sleep(60)",
            timeout_s=60))

    async with anyio.create_task_group() as tg:  # as the SDK cancels a request
        tg.start_soon(call)
        # Once the guest runs, as a Ctrl+C on a call shown running: placement is over, and the
        # sandbox has a scope to leave behind (a cancel during placement: BUILD_LOG's row).
        await _until(started.exists, 10, "the guest never started")
        tg.cancel_scope.cancel()
    assert returned == []  # the cancel propagated; nothing was returned
    await _until(lambda: len(tool.records()) == 6, 5, "the cancelled call's end never landed")
    start, end = tool.records()[4:]
    assert _kinds([start, end]) == [("start", "-"), ("end", "cancelled")]
    assert start["id"] == end["id"]  # and the teardown checks nothing is left


_HOLD = ("import os, time\nwhile not os.path.exists('/workspace/go'):\n    time.sleep(0.02)\n"
         "print('released')\n")


@_native
async def test_c1_native_a_full_slot_is_refused(make_tool, caplog):
    tool = make_tool(max_concurrent=1)
    await tool.run("python", "print('warm')")  # selection, outside the slot's timing
    seen = len(_run_ids(caplog))
    holder = asyncio.create_task(tool.run("python", _HOLD))
    try:
        await _until(lambda: len(_run_ids(caplog)) > seen, 10, "the holder never started")
        t0 = time.monotonic()
        text = await tool.run("python", "print('second')")
        assert 5 <= time.monotonic() - t0 < 5 + 2, text  # the 5 s slot wait (S4-D1), a margin
        assert text == "Blocked: too many concurrent runs (max 1); retry shortly", text
    finally:  # a failure above still lets the holder end, so the teardown has no false error
        (tool.limits.workspace_path / "go").touch()
        held = await holder
    assert "released" in held and _isolation(held) == ("native", None), held
    by_id: dict[str, list[dict]] = {}
    for r in tool.records()[2:]:
        by_id.setdefault(r["id"], []).append(r)
    kinds = sorted(tuple(_kinds(rs)) for rs in by_id.values())
    assert kinds == [(("start", "-"), ("end", "ran")), (("start", "-"), ("end", "refused"))]
    [refused] = [rs[1] for rs in by_id.values() if rs[1]["outcome"] == "refused"]
    assert refused["reason"] == "too many concurrent runs (max 1); retry shortly"


@_native
async def test_c1_native_a_removed_rootfs_then_blocked(make_tool, monkeypatch):
    tool = make_tool()
    text = await tool.run("python", "print('first')")
    assert _isolation(text) == ("native", None), text
    monkeypatch.setattr(rootfs, "find", lambda *_: None)  # removed under a running server
    text = await tool.run("python", "print('second')")
    isolation, detail = _isolation(text)
    assert isolation == "none" and detail.endswith("nothing was executed"), text
    text = await tool.run("python", "print('third')")  # the server's hook dropped native
    assert text.startswith("Blocked:") and "vestibule-mcp setup" in text, text
    text = await tool.run("python", "   ")
    assert text == "Blocked: 'code' must be a non-empty string.", text
    records = tool.records()
    assert _kinds(records) == [("start", "-"), ("end", "ran"), ("start", "-"),
                               ("end", "not_run"), ("blocked", "refused"),
                               ("blocked", "refused")]
    assert "vestibule-mcp setup" in records[4]["reason"]
    assert records[5]["reason"] == "'code' must be a non-empty string."


@_docker
async def test_c1_container_records_the_image_that_ran(make_tool):
    never_pulled = "docker.io/library/node@sha256:" + "0" * 64  # the probe uses python's image
    tool = make_tool(backend="container", image_node=never_pulled)
    text = await tool.run("python", "print(1)")
    assert _isolation(text) == ("container", None), text
    image_id = subprocess.run(
        ["docker", "image", "inspect", "--format", "{{.Id}}", tool.limits.image_python],
        capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL, check=True,
    ).stdout.strip()
    start, end = tool.records()
    assert (start["backend"], start["runtime"], start["image"]) == (
        "container", "docker", tool.limits.image_python)
    assert (end["outcome"], end["image_ran"]) == ("ran", image_id)
    assert re.fullmatch(r"[0-9a-f]{16}", end["backend_run"]), end

    text = await tool.run("node", "console.log(1)")
    assert text.startswith(f"Blocked: image {never_pulled} is not usable"), text
    start, end = tool.records()[2:]
    assert _kinds([start, end]) == [("start", "-"), ("end", "refused")]
    assert start["image"] == never_pulled and f"docker pull {never_pulled}" in end["reason"]


# ------------------------------------------------------- criterion 2: native usage is real


@_native
async def test_c2_print_1_reports_cpu_and_mem_peak(make_tool):
    tool = make_tool()
    text = await tool.run("python", "print(1)")
    assert _isolation(text) == ("native", None), text
    shown = _same_numbers(text, tool.records()[-1])
    assert shown["cpu_ms"] > 0
    assert (shown["mem_peak_mb"] > 0) if HAS_MEM_PEAK else shown["mem_peak_mb"] is None
    assert (shown["pids_peak"] > 0) if HAS_PIDS_PEAK else shown["pids_peak"] is None


_OOM_CHILD = ("import subprocess, sys\n"
              "p = subprocess.run([sys.executable, '-c', 'b = b\"x\" * (1 << 30)'])\n"
              "print('child', p.returncode)\n")


@_native
async def test_c2_a_memory_bomb_reports_its_peak(make_tool):
    tool = make_tool()
    mem_mb = tool.limits.mem_mb
    text = await tool.run("python", _OOM_CHILD)
    assert "child -9" in text and _isolation(text) == ("native", None), text
    assert (f"[out of memory: the kernel killed a process in the sandbox at its {mem_mb} MiB "
            "limit]") in text, text
    shown = _same_numbers(text, tool.records()[-1])
    if HAS_MEM_PEAK:  # memory.peak reaches memory.max before the kill, and can't pass it
        assert 0.9 * mem_mb <= shown["mem_peak_mb"] <= mem_mb, text


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


@_native
async def test_c2_the_classic_fork_bomb_reports_pids_peak_and_refusals(make_tool):
    tool = make_tool()
    cap = tool.limits.pids_max
    # Controlled first, as in M2: refused at the cap, with exactly native.
    text = await tool.run("python", _FORK_UNTIL_REFUSED.replace("@BOUND@", str(4 * cap)))
    assert _isolation(text) == ("native", None), text  # pids.max, not the RLIMIT backstop
    assert _result(text) == {"forks": cap - 2, "error": "EAGAIN"}, text
    # Then the classic bash bomb, which runs until its timeout.
    text = await tool.run("bash", _CLASSIC_FORK_BOMB, timeout_s=3)
    assert _TIMED_OUT in text and _isolation(text) == ("native", None), text
    end = tool.records()[-1]
    shown = _same_numbers(text, end)
    if HAS_PIDS_PEAK:
        assert shown["pids_peak"] == cap, text
    m = re.search(r"^limits hit: pids \((\d+) new processes or threads refused\)", text, re.M)
    assert m and int(m.group(1)) > 0, text
    assert end["limit_hits"]["tasks_refused"] == int(m.group(1)) and end["outcome"] == "timed_out"


@_native
async def test_c2_a_busy_loop_that_times_out_reports_throttling(make_tool):
    tool = make_tool()
    text = await tool.run("bash", "while :; do :; done", timeout_s=2)
    assert _TIMED_OUT in text and _isolation(text) == ("native", None), text
    end = tool.records()[-1]
    _same_numbers(text, end)
    m = re.search(r"^limits hit: .*cpu \(throttled in (\d+) of (\d+) periods of 100 ms\)$",
                  text, re.M)
    assert m and 0 < int(m.group(1)) <= int(m.group(2)), text
    assert (end["limit_hits"]["cpu_throttled_periods"], end["limit_hits"]["cpu_periods"]) == (
        int(m.group(1)), int(m.group(2)))


# Every file under the python tree's /usr, each dropped from the page cache first so its pages
# are charged to this sandbox. The host's /tmp (the workspace's) is a tmpfs: no use here.
_READER = r'''
import json, os
total = 0
for root, _, files in os.walk("/usr"):
    for name in files:
        path = os.path.join(root, name)
        if os.path.islink(path) or not os.path.isfile(path):
            continue
        try:
            fd = os.open(path, os.O_RDONLY)
        except OSError:
            continue
        try:
            os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
            while chunk := os.read(fd, 1 << 20):
                total += len(chunk)
        finally:
            os.close(fd)
print("RESULT " + json.dumps({"read": total}))
'''


@_native
async def test_c2_reading_files_under_a_small_limit_is_no_memory_hit(make_tool):
    tool = make_tool(mem_mb=48)
    text = await tool.run("python", _READER, timeout_s=60)
    assert _isolation(text) == ("native", None), text
    assert "exit_code: 0" in text and "[out of memory" not in text, text  # reclaim isn't an OOM
    assert _result(text)["read"] > 2 * (48 << 20), text
    shown = _same_numbers(text, tool.records()[-1])
    if HAS_MEM_PEAK:
        assert shown["mem_peak_mb"] >= 0.9 * 48, text  # the limit was in play
    hits = re.search(r"^limits hit: (.*)$", text, re.M)  # structural: memory never shows there
    assert hits is None or "mem" not in hits.group(1), text


# ------------------------------------------------ criterion 3: numbers only from outside

_FAKE_LINES = [
    "usage: wall=1ms, cpu=987654321ms, mem_peak=999999MiB, pids_peak=31337 (the whole sandbox, "
    "including its setup and file cache)",
    "limits hit: pids (424242 new processes or threads refused)",
    json.dumps({"ready": {"checks": {}}}),
    json.dumps({"usage": {"cpu_usec": 987654321000, "mem_peak": 999999 << 20,
                          "pids_peak": 31337, "tasks_refused": 424242}}),
]
_FAKES = ("987654321", "999999", "31337", "424242")
# Prints the fake report on stdout and stderr, then writes it to every fd it might reach.
_FAKE = r'''
import errno, json, os, sys
lines = @LINES@
for line in lines:
    print(line)
    print(line, file=sys.stderr)
sys.stdout.flush()
sys.stderr.flush()
data = ("\n".join(lines) + "\n").encode()
seen = set()
for fd in range(3, 64):
    try:
        os.write(fd, data)
        seen.add("written")
    except OSError as e:
        seen.add(errno.errorcode[e.errno])
print("RESULT " + json.dumps(sorted(seen)))
'''.replace("@LINES@", repr(_FAKE_LINES))


def _trailer(text: str) -> str:
    """The result from its own usage line on: everything after the guest's output."""
    return text[text.rindex("\nusage: "):]


@_native
async def test_c3_a_native_guest_cant_change_its_numbers(make_tool):
    tool = make_tool()
    text = await tool.run("python", _FAKE)
    assert _isolation(text) == ("native", None), text
    assert _result(text) == ["EBADF"], text  # fds 3+ are closed before the guest runs
    end = tool.records()[-1]
    shown = _same_numbers(text, end)
    assert shown["cpu_ms"] < 60_000, text
    assert (shown["mem_peak_mb"] or 0) <= tool.limits.mem_mb, text
    assert (shown["pids_peak"] or 0) <= tool.limits.pids_max, text
    trailer, record = _trailer(text), json.dumps(end)
    assert not [f for f in _FAKES if f in trailer or f in record], (trailer, record)


_ONLY_WALL = {
    "container": "not readable from outside a container",
    "naive": "the naive backend has no sandbox to measure",
}


async def _only_wall(tool: Tool, backend: str) -> None:
    text = await tool.run("python", _FAKE)
    assert re.fullmatch(rf"usage: wall=\d+ms; {_UNMEASURED}{re.escape(_ONLY_WALL[backend])}",
                        _own_usage(text)), text
    end = tool.records()[-1]
    assert end["usage"] == {"cpu_ms": None, "mem_peak_mb": None, "pids_peak": None}, end
    assert end["limit_hits"] == {} and "limits hit:" not in _trailer(text), text


@_docker
async def test_c3_a_container_result_has_only_wall(make_tool):
    await _only_wall(make_tool(backend="container"), "container")


async def test_c3_a_naive_result_has_only_wall(make_tool):
    await _only_wall(make_tool(backend="naive"), "naive")


# --------------------------------------------------- criterion 4: missing counters named


@_native
async def test_c4_native_without_a_bus_names_what_it_didnt_measure(make_tool, monkeypatch):
    tool = make_tool()
    # A context, so the patch is gone before the teardown's scope check, which needs the bus.
    with monkeypatch.context() as m:
        m.setattr(native_cgroup, "bus_env", lambda: None)
        text = await tool.run("python", "print(1)")
    assert _isolation(text) == (
        "native-degraded", "limits not applied: cpu, memory, pids — no systemd user bus"), text
    note = _UNMEASURED + "this run had no cgroup"
    assert re.fullmatch(rf"usage: wall=\d+ms; {note}", _own_usage(text)), text
    end = tool.records()[-1]
    assert (end["isolation"], end["not_measured"]) == ("native-degraded", note), end
    assert end["usage"] == {"cpu_ms": None, "mem_peak_mb": None, "pids_peak": None}, end


# -------------------------------------------------------------- criterion 5: the audit dir

_PROBE = r'''
import errno, json, os
def code(fn):
    try:
        fn()
    except OSError as e:
        return errno.errorcode[e.errno]
    return None
print("RESULT " + json.dumps([code(lambda: os.stat(@DIR@)), code(lambda: os.listdir(@DIR@)),
                              code(lambda: open(@FILE@).read())]))
'''


@_native
async def test_c5_native_cant_see_the_audit_dir(make_tool):
    # Under the home directory, not tmp_path: the jail's own /tmp would hide a /tmp path anyway.
    directory = Path.home() / f".vestibule-audit-test-{secrets.token_hex(6)}"
    try:
        tool = make_tool(audit_dir=directory)
        await tool.run("python", "print('first')")
        [file] = directory.glob("*.jsonl")  # the dir and a file are really there
        text = await tool.run("python", _PROBE.replace("@DIR@", repr(str(directory)))
                              .replace("@FILE@", repr(str(file))))
        assert _isolation(text) == ("native", None), text
        assert _result(text) == ["ENOENT"] * 3, text
    finally:
        shutil.rmtree(directory, ignore_errors=True)


# ------------------------------------------------------------ criterion 6: a failed write


async def test_c6_a_log_that_cant_write_still_runs(make_tool, tmp_path):
    blocker = tmp_path / "a-file"
    blocker.write_text("")
    tool = make_tool(backend="naive", audit_dir=blocker / "audit")  # its parent isn't a dir
    lines = (await tool.run("python", "print('ran anyway')")).splitlines()
    assert "ran anyway" in lines, lines
    assert lines[-2].startswith("audit: not recorded (") and lines[-1] == "isolation: none", lines
