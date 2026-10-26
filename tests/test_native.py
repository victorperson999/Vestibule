"""The native sandbox (M2 step 3, docs/plans/M2/M2-step3-warden-core.md §4.2). Linux only,
against the real trees that `vestibule-mcp setup` made; the `native` marker skips elsewhere with
the reason (tests/nativeutil.py).

Runs use the test-only seccomp bypass unless they test the default. The filter arrives in
step 5, so every result here says `isolation: none` (S3-D8, S3-D9).
"""
import asyncio
import getpass
import json
import logging
import os
import secrets
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest
from nativeutil import holders, native_required

from vestibule import registry, rootfs
from vestibule.backends import native
from vestibule.backends.base import RunRefusedError
from vestibule.backends.native import NativeBackend
from vestibule.config import DEFAULT_IMAGE_PYTHON, Limits

pytestmark = [pytest.mark.native, native_required]

DEVICES = ("null", "zero", "full", "random", "urandom", "tty")
MASKED = ("acpi", "asound", "interrupts", "kcore", "keys", "latency_stats", "sched_debug",
          "scsi", "timer_list", "timer_stats")
READ_ONLY = ("bus", "fs", "irq", "sys", "sysrq-trigger")
STEP3_LABEL = "hard controls not confirmed: seccomp"


@pytest.fixture()
def limits(tmp_path):
    return Limits(workspace_dir=str(tmp_path / "ws"))


@pytest.fixture()
async def backend():
    b = NativeBackend(_skip_seccomp_for_tests=True)
    yield b
    assert await b.drain(15), "a finisher was still running"


def _facts(r):
    """The JSON object the guest printed last."""
    assert r.exit_code == 0, (r.exit_code, r.stderr, r.isolation_detail)
    return json.loads(r.stdout.strip().splitlines()[-1])


def _token():
    return f"vestibule-token-{secrets.token_hex(6)}"


# --- runs, and the jail --------------------------------------------------------------------

@pytest.mark.parametrize("language, code, out", [
    ("python", "import sys; print('hi', sys.version_info[:2])", "hi (3, 12)"),
    ("bash", "echo hi from bash $BASH_VERSION", "hi from bash 5."),
    ("node", "console.log('hi', process.version)", "hi v22."),
])
async def test_hello_in_all_three_languages(backend, limits, language, code, out):
    r = await backend.run(language, code, 10, limits)
    assert r.exit_code == 0 and r.stdout.startswith(out) and r.stderr == ""
    assert r.isolation == "none" and r.isolation_detail == STEP3_LABEL


async def test_exit_codes(backend, limits):
    assert (await backend.run("bash", "exit 3", 10, limits)).exit_code == 3
    r = await backend.run("python", "import os, signal; os.kill(os.getpid(), signal.SIGKILL)",
                          10, limits)
    assert r.exit_code == 137


async def test_a_host_canary_is_unreachable(backend, limits):
    canary = Path.home() / f".vestibule-canary-{secrets.token_hex(6)}"
    canary.mkdir()
    (canary / "id_ed25519").write_text("not for the guest")
    try:
        code = textwrap.dedent(f"""
            import json, os
            def denied(fn):
                try:
                    fn()
                except PermissionError:
                    return True
                return False
            print(json.dumps({{
                "exists": os.path.exists("{canary}/id_ed25519"),
                "root": denied(lambda: os.readlink("/proc/1/root")),
                "cwd": denied(lambda: os.readlink("/proc/1/cwd")),
                "found": [d for d, _, _ in os.walk("/") if "{canary.name}" in d],
            }}))
        """)
        f = _facts(await backend.run("python", code, 30, limits))
    finally:
        (canary / "id_ed25519").unlink()
        canary.rmdir()
    assert f == {"exists": False, "root": True, "cwd": True, "found": []}


async def test_the_mount_table_is_the_planned_one(backend, limits):
    code = textwrap.dedent("""
        import json, os
        rows = [line.split() for line in open("/proc/self/mountinfo")]
        print(json.dumps({
            "mounts": [[r[4], r[r.index("-", 6) + 1]] for r in rows],
            "pids": sorted(int(p) for p in os.listdir("/proc") if p.isdigit()),
            "sys": os.listdir("/sys") if os.path.isdir("/sys") else [],
        }))
    """)
    f = _facts(await backend.run("python", code, 10, limits))
    # The host's /proc comes from the same kernel, so it shows which masks can exist.
    masked = [f"/proc/{p}" for p in MASKED if os.path.exists(f"/proc/{p}")]
    read_only = [f"/proc/{p}" for p in READ_ONLY if os.path.exists(f"/proc/{p}")]
    assert [m[0] for m in f["mounts"]] == [
        "/", "/proc", "/dev", *[f"/dev/{d}" for d in DEVICES], "/dev/pts", "/dev/shm", "/tmp",
        "/workspace", "/sandbox", *masked, *read_only]
    types = dict(f["mounts"])
    assert (types["/proc"], types["/dev/pts"]) == ("proc", "devpts")
    assert {types[p] for p in ("/dev", "/dev/shm", "/tmp", "/sandbox")} == {"tmpfs"}
    assert f["pids"] == [1, 2] and f["sys"] == []


async def test_writes_land_only_where_they_may(backend, tmp_path):
    lim = Limits(workspace_dir=str(tmp_path / "ws"), tmpfs_mb=4)
    code = textwrap.dedent("""
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
                for _ in range(8):
                    f.write(b"x" * (1 << 20))
                    f.flush()
        except OSError as e:
            tmp = errno.errorcode[e.errno]
        print(json.dumps({"root": write("/x"), "sandbox": write("/sandbox/x"), "tmp": tmp,
                          "tmp_size": os.path.getsize("/tmp/fill"), "shm": write("/dev/shm/x"),
                          "workspace": write("/workspace/x")}))
    """)
    f = _facts(await backend.run("python", code, 10, lim))
    assert (f["root"], f["sandbox"], f["tmp"]) == ("EROFS", "EROFS", "ENOSPC")
    assert f["tmp_size"] <= 4 << 20 and (f["shm"], f["workspace"]) == ("ok", "ok")
    assert (tmp_path / "ws" / "x").read_bytes() == b"x"


async def test_a_read_only_workspace(backend, tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "in.txt").write_text("hello")
    r = await backend.run("bash", "cat in.txt; echo x > out.txt",
                          10, Limits(workspace_dir=str(ws), workspace_ro=True))
    assert r.stdout == "hello" and "Read-only file system" in r.stderr
    assert not (ws / "out.txt").exists()
    assert r.isolation_detail == STEP3_LABEL  # the server accepted /workspace as ro


async def test_proc_is_masked_like_docker(backend, limits):
    code = textwrap.dedent("""
        import json, os
        writable = [os.path.join(d, f) for d, _, files in os.walk("/proc/sys") for f in files
                    if os.access(os.path.join(d, f), os.W_OK)]
        print(json.dumps({"keys": open("/proc/keys").read(), "writable": writable}))
    """)
    assert _facts(await backend.run("python", code, 10, limits)) == {"keys": "", "writable": []}


# --- mounts that come locked from the host (S3-D6) ----------------------------------------

_RUNNER = textwrap.dedent("""
    import asyncio, json, sys
    from vestibule.backends.native import NativeBackend
    from vestibule.config import Limits

    async def main():
        b = NativeBackend(_skip_seccomp_for_tests=True)
        r = await b.run("bash", sys.argv[2], 10, Limits(workspace_dir=sys.argv[1]))
        await b.drain(10)
        print(json.dumps({"exit": r.exit_code, "out": r.stdout, "err": r.stderr,
                          "isolation": r.isolation, "detail": r.isolation_detail}))

    asyncio.run(main())
""")


def _in_outer_userns(tmp_path, setup, ws, code):
    """Run one sandbox from inside an outer user namespace in which `setup` made a mount. The
    sandbox's own user namespace then gets that mount locked, as a host's own mounts are."""
    runner = tmp_path / "runner.py"
    runner.write_text(_RUNNER)
    p = subprocess.run(
        ["unshare", "-r", "-m", "--propagation", "private", "sh", "-c",
         f'{setup} && exec "$0" "$1" "$2" "$3"', sys.executable, str(runner), str(ws), code],
        capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout.strip().splitlines()[-1])


@pytest.mark.parametrize("setup, flag", [
    ('mount -t tmpfs -o noexec,nosuid,nodev t "$2" && echo hi > "$2/in.txt"', "noexec"),
    ('echo hi > "$2/in.txt" && mount --bind "$2" "$2" && mount -o remount,bind,ro "$2"', "ro"),
], ids=["noexec", "read-only"])
def test_a_locked_workspace_mount_keeps_its_flags(tmp_path, setup, flag):
    ws = tmp_path / "ws"
    ws.mkdir()
    r = _in_outer_userns(tmp_path, setup, ws,
                         "cat in.txt; grep ' /workspace ' /proc/self/mountinfo")
    assert r["exit"] == 0 and r["isolation"] == "none" and r["detail"] == STEP3_LABEL
    out, mount = r["out"].splitlines()
    assert out == "hi" and flag in mount.split()[5].split(",")


def test_a_workspace_with_a_mount_inside_is_refused(tmp_path):
    ws = tmp_path / "ws"
    (ws / "sub").mkdir(parents=True)
    r = _in_outer_userns(tmp_path, 'mount -t tmpfs t "$2/sub"', ws, "echo ran > ran")
    assert r["isolation"] == "none" and "another filesystem mounted inside it" in r["detail"]
    assert r["detail"].endswith("nothing was executed") and not (ws / "ran").exists()


# --- what the guest sees (criterion 10), terminal, signals ---------------------------------

async def test_what_the_guest_sees(backend, limits):
    code = textwrap.dedent("""
        import json, os, socket
        def fails(fn):
            try:
                fn()
            except OSError as e:
                return type(e).__name__
            return None
        open_fds = [fd for fd in range(3, 256) if fails(lambda fd=fd: os.fstat(fd)) is None]
        status = {k: v.strip()
                  for k, _, v in (line.partition(":") for line in open("/proc/self/status"))}
        server = socket.create_server(("127.0.0.1", 0))
        def udp():
            socket.socket(socket.AF_INET, socket.SOCK_DGRAM).sendto(b"x", ("1.1.1.1", 53))
        print(json.dumps({
            "ids": [os.getuid(), os.getgid()], "groups": os.getgroups(), "fds": open_fds,
            "caps": [status[k] for k in ("CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb")],
            "nnp": status["NoNewPrivs"], "env": dict(os.environ),
            "proc1": [fails(fn) for fn in (lambda: open("/proc/1/environ").read(),
                                           lambda: os.listdir("/proc/1/fd"),
                                           lambda: os.readlink("/proc/1/cwd"),
                                           lambda: os.readlink("/proc/1/root"),
                                           lambda: open("/proc/1/mem", "rb").read(1))],
            "cmdline": open("/proc/1/cmdline", "rb").read().decode(errors="replace"),
            "hostname": socket.gethostname(), "cwd": os.getcwd(),
            "stdin": os.readlink("/proc/self/fd/0"),
            "ifaces": [line.split(":")[0].strip()
                       for line in open("/proc/net/dev").read().splitlines()[2:]],
            "loopback": fails(lambda: socket.create_connection(server.getsockname(), 2).close()),
            "tcp": fails(lambda: socket.create_connection(("1.1.1.1", 53), 2)),
            "dns": fails(lambda: socket.getaddrinfo("example.com", 80)), "udp": fails(udp),
        }))
    """)
    f = _facts(await backend.run("python", code, 10, limits))
    assert f["ids"] == [os.getuid(), os.getgid()]
    assert set(f["groups"]) <= {os.getgid(), 65534}  # others show as the overflow gid (N6)
    assert f["fds"] == [] and set(f["caps"]) == {"0000000000000000"} and f["nnp"] == "1"
    image = rootfs.find(limits.rootfs_path, limits.image_python)
    assert image is not None and f["env"] == native._guest_env(image.env)
    assert f["proc1"] == ["PermissionError"] * 5
    for host_path in ("/home", getpass.getuser(), sys.prefix):
        assert host_path not in f["cmdline"]
    assert f["cmdline"].startswith("vestibule-warden")
    assert (f["hostname"], f["cwd"], f["stdin"]) == ("vestibule", "/workspace", "/dev/null")
    assert f["ifaces"] == ["lo"] and f["loopback"] is None
    assert None not in (f["tcp"], f["dns"], f["udp"])


_TTY_RUNNER = textwrap.dedent('''
    import asyncio, json, sys
    from vestibule.backends.native import NativeBackend
    from vestibule.config import Limits

    def tty_nr():
        return int(open("/proc/self/stat").read().rsplit(")", 1)[1].split()[4])

    GUEST = """
    import errno, json, os
    try:
        os.close(os.open("/dev/tty", os.O_RDWR))
        tty = "opened"
    except OSError as e:
        tty = errno.errorcode[e.errno]
    print(json.dumps({"tty_nr": int(open("/proc/self/stat").read().rsplit(")", 1)[1].split()[4]),
                      "tty": tty}))
    """

    async def main():
        b = NativeBackend(_skip_seccomp_for_tests=True)
        r = await b.run("python", GUEST, 10, Limits(workspace_dir=sys.argv[1]))
        await b.drain(10)
        print("RESULT " + json.dumps({"runner_tty_nr": tty_nr(), "guest": r.stdout.strip()}))

    asyncio.run(main())
''')


def test_the_guest_gets_no_controlling_terminal(tmp_path):
    """Run from a terminal (util-linux `script` gives the runner one), the guest still can't
    open /dev/tty: init's setsid() (S3-D7, second review finding 1)."""
    runner = tmp_path / "tty_runner.py"
    runner.write_text(_TTY_RUNNER)
    p = subprocess.run(["script", "-qec", f"{sys.executable} {runner} {tmp_path / 'ws'}",
                        "/dev/null"], capture_output=True, text=True, timeout=60,
                       stdin=subprocess.DEVNULL)
    line = next(ln for ln in p.stdout.splitlines() if ln.startswith("RESULT "))
    result = json.loads(line[len("RESULT "):])
    assert result["runner_tty_nr"] != 0, "the runner had no terminal: the test proves nothing"
    assert json.loads(result["guest"]) == {"tty_nr": 0, "tty": "ENXIO"}


async def test_signals_behave_as_in_docker(backend, limits):
    r = await backend.run("bash", "yes | head -1", 10, limits)
    assert (r.exit_code, r.stdout, r.stderr) == (0, "y\n", "")  # no "Broken pipe" (SIGPIPE)
    r = await backend.run("bash", "kill -INT 1; kill -TERM 1; sleep 0.2; echo still-here",
                          10, limits)
    assert (r.exit_code, r.stdout) == (0, "still-here\n")  # PID 1 ignores both


# --- stopping (criteria 8 and 11, without cgroups) -----------------------------------------

@pytest.mark.parametrize("language, code", [
    ("python", "import subprocess\nsubprocess.Popen(['bash', '-c', 'exec -a TOKEN sleep 600'])\n"
               "while True:\n    pass\n"),
    ("python", "import os, signal, subprocess\n"
               "subprocess.Popen(['bash', '-c', 'exec -a TOKEN sleep 600'])\n"
               "os.kill(os.getpid(), signal.SIGSTOP)\n"),
    ("bash", "exec >&- 2>&-\n(exec -a TOKEN sleep 600) &\nsleep 600\n"),
], ids=["busy-loop", "self-sigstop", "closed-output"])
async def test_a_run_past_its_timeout_is_stopped_with_nothing_left(backend, limits, caplog,
                                                                   language, code):
    caplog.set_level(logging.INFO, logger="vestibule.native")
    token = _token()
    t0 = time.monotonic()
    task = asyncio.create_task(backend.run(language, code.replace("TOKEN", token), 1, limits))
    while not holders(token):  # the sandbox's background child is running
        assert time.monotonic() - t0 < 3, "the guest's background child never started"
        await asyncio.sleep(0.02)
    r = await task
    assert r.timed_out and r.exit_code == -1
    assert time.monotonic() - t0 < 1 + 3  # timeout_s, the 2 s grace, and margin
    assert r.isolation_detail == STEP3_LABEL  # the verdict comes from `ready`, read before
    assert await backend.drain(10)
    assert holders(token) == []
    # The helper stopped init itself: its exit means the sandbox is empty (S3-D7).
    assert any("sandbox gone (helper exit 0)" in m for m in caplog.messages), caplog.messages


async def test_an_output_flood_is_cut_and_the_sandbox_stopped(backend, limits, caplog):
    caplog.set_level(logging.INFO, logger="vestibule.native")
    t0 = time.monotonic()
    r = await backend.run("python", "while True:\n    print('x' * 1000)\n", 30, limits)
    assert not r.timed_out and r.exit_code == 137 and time.monotonic() - t0 < 10
    assert len(r.stdout) <= 2 * limits.max_output_bytes
    assert r.stderr.endswith("[output exceeded collection cap; the sandbox was terminated]")
    assert await backend.drain(10)
    assert any("sandbox gone (helper exit 0)" in m for m in caplog.messages), caplog.messages


_SERVER = textwrap.dedent("""
    import asyncio, sys
    from vestibule.backends.native import NativeBackend
    from vestibule.config import Limits

    asyncio.run(NativeBackend(_skip_seccomp_for_tests=True).run(
        "bash", f"exec -a {sys.argv[2]} sleep 600", 60, Limits(workspace_dir=sys.argv[1])))
""")


def test_a_killed_server_leaves_no_sandbox(tmp_path):
    token = _token()
    script = tmp_path / "server.py"
    script.write_text(_SERVER)
    server = subprocess.Popen([sys.executable, str(script), str(tmp_path / "ws"), token],
                              stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL)
    try:
        t0 = time.monotonic()
        while not [p for p in holders(token) if p != server.pid]:  # the guest is running
            assert time.monotonic() - t0 < 15, "the guest never started"
            time.sleep(0.02)
        server.kill()
        server.wait()
        t0 = time.monotonic()
        while holders(token) and time.monotonic() - t0 < 2:
            time.sleep(0.02)
        assert holders(token) == []
    finally:
        server.kill()
        server.wait()


# --- refusing before the guest exists (S3-D8, S3-D9) ---------------------------------------

async def test_the_default_backend_stops_before_the_guest(limits):
    b = NativeBackend()  # no bypass: there is no filter until step 5
    r = await b.run("bash", "echo ran > ran", 10, limits)
    assert await b.drain(10)
    assert (r.isolation, r.exit_code, r.stdout) == ("none", -1, "")
    assert r.isolation_detail == ("native sandbox setup failed at seccomp: the seccomp filter "
                                  "arrives in M2 step 5; nothing was executed")
    assert not (Path(limits.workspace_dir) / "ran").exists()


async def test_an_empty_tree_fails_setup_at_mounts(backend, tmp_path):
    ref, arch = registry.parse_ref(DEFAULT_IMAGE_PYTHON), registry.host_arch()
    key = f"{ref.digest[7:]}-{arch}"
    d = tmp_path / "rootfs"
    (d / key).mkdir(parents=True)
    (d / f"{key}.json").write_text(json.dumps({
        "format": rootfs.FORMAT, "ref": DEFAULT_IMAGE_PYTHON, "index_digest": ref.digest,
        "manifest_digest": "sha256:" + "f" * 64, "platform": f"linux/{arch}",
        "env": ["PATH=/bin"]}))
    r = await backend.run("python", "print(1)", 10,
                          Limits(workspace_dir=str(tmp_path / "ws"), rootfs_dir=str(d)))
    assert r.isolation == "none" and r.stdout == ""
    assert r.isolation_detail.startswith("native sandbox setup failed at mounts: ")
    assert r.isolation_detail.endswith("; nothing was executed")


async def test_a_helper_that_reports_nothing_claims_nothing(backend, limits, tmp_path,
                                                            monkeypatch, capfd):
    fake = tmp_path / "fake_helper.py"
    fake.write_text("import sys\nprint('spoofed')\nprint('to the log', file=sys.stderr)\n")
    monkeypatch.setattr(native, "_HELPER", fake)
    r = await backend.run("python", "print(1)", 10, limits)
    assert (r.isolation, r.exit_code, r.stdout, r.stderr) == ("none", -1, "", "")
    assert r.isolation_detail == ("the sandbox sent no status report; no isolation is claimed "
                                  "for this run")
    # The helper's own output: stdout to /dev/null, never the server's stdout (golden rule 1);
    # stderr to the server's log, never the guest's stderr (S3-D4).
    out, err = capfd.readouterr()
    assert "spoofed" not in out and "to the log" in err


async def test_the_helpers_self_deadline_stops_a_run_the_server_forgot(backend, limits,
                                                                       monkeypatch, caplog):
    """N8's last backstop: the server stops nothing here before 12 s, and the helper's own
    deadline (moved to 1 s) must end the run with nothing left (S3-D7)."""
    caplog.set_level(logging.INFO, logger="vestibule.native")
    monkeypatch.setattr(native, "_SELF_DEADLINE_SLACK_S", -9)  # timeout 10: the helper's at 1 s
    token = _token()
    t0 = time.monotonic()
    r = await backend.run("bash", f"exec -a {token} sleep 600", 10, limits)
    assert not r.timed_out and time.monotonic() - t0 < 5  # only the helper could stop it
    assert r.exit_code == -1 and r.stderr == "\n[the sandbox did not report an exit status]"
    assert await backend.drain(10)
    assert holders(token) == []
    assert any("sandbox gone (helper exit 0)" in m for m in caplog.messages), caplog.messages


_STALLED_STDERR = textwrap.dedent("""
    import asyncio, json, os, sys
    from vestibule.backends import native
    from vestibule.backends.native import NativeBackend
    from vestibule.config import Limits

    r, w = os.pipe()  # this process's stderr becomes a full pipe that nobody reads
    os.dup2(w, 2)
    os.set_blocking(2, False)
    try:
        while True:
            os.write(2, b"x" * 65536)
    except BlockingIOError:
        pass
    os.set_blocking(2, True)
    native._SELF_DEADLINE_SLACK_S = -9  # timeout 10: the helper's deadline at 1 s

    async def main():
        run = asyncio.ensure_future(NativeBackend(_skip_seccomp_for_tests=True).run(
            "bash", "sleep 600", 10, Limits(workspace_dir=sys.argv[1])))
        done, _ = await asyncio.wait({run}, timeout=5)
        print(json.dumps({"ended": bool(done)}), flush=True)
        os._exit(0)  # past the stuck pipe; PDEATHSIG takes any sandbox left

    asyncio.run(main())
""")


def test_a_stalled_server_stderr_cannot_hold_the_self_deadline(tmp_path):
    """The helper's stderr is the server's. A client that stops reading it must not keep the
    backstop from stopping init (second critic round, finding 1)."""
    script = tmp_path / "stalled.py"
    script.write_text(_STALLED_STDERR)
    p = subprocess.run([sys.executable, str(script), str(tmp_path / "ws")],
                       capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL)
    assert json.loads(p.stdout.strip().splitlines()[-1]) == {"ended": True}


# --- concurrency ---------------------------------------------------------------------------

async def test_four_runs_at_once_get_four_pid_namespaces(backend, limits):
    code = "import os; print(os.readlink('/proc/self/ns/pid'))"
    results = await asyncio.gather(*(backend.run("python", code, 10, limits) for _ in range(4)))
    assert [r.exit_code for r in results] == [0] * 4
    assert len({r.stdout for r in results}) == 4


async def test_busy_refusal_after_a_bounded_wait(limits, monkeypatch):
    monkeypatch.setattr(native, "_SEM_WAIT_S", 0.1)  # keep the test fast
    gate = asyncio.Event()

    async def slow_collect(self, run, timeout_s, limits):
        await gate.wait()
        return native._not_run("test")

    monkeypatch.setattr(NativeBackend, "_collect", slow_collect)
    b = NativeBackend(_skip_seccomp_for_tests=True)
    one = Limits(workspace_dir=limits.workspace_dir, max_concurrent=1)
    holder = asyncio.create_task(b.run("python", "1", 5, one))
    await asyncio.sleep(0.05)  # let the holder take the only slot
    with pytest.raises(RunRefusedError, match="too many concurrent runs"):
        await b.run("python", "1", 5, one)
    gate.set()
    await holder
    assert await b.drain(10)
