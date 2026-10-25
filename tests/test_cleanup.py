"""The detached cleaner (vestibule.cleanup; M2 step 1b, S1-D5) against a fake runtime.

The fake is a Python script run as `[python, fake_rt.py]`, so the tests run on every
platform without Docker. It records every argv it gets and can be told to print container
ids for `ps`, to hang, or to fail. The job's sweep delays are shortened to milliseconds.
"""
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from vestibule import cleanup

# The cleaner only runs on POSIX in production (S1-D5). The tests that make many fake-runtime
# calls run there only: each process start costs ~1-2 s on the Windows dev host.
posix_only = pytest.mark.skipif(
    sys.platform == "win32", reason="cleaner runs on POSIX only; many slow process starts here"
)

FAKE_RT = """\
import json, os, sys, time
args = sys.argv[1:]
with open(os.environ["FAKE_RT_LOG"], "a") as f:
    f.write(json.dumps(args) + "\\n")
if args[:1] == ["ps"]:
    if os.environ.get("FAKE_RT_HANG") == "ps":
        time.sleep(30)
    print(os.environ.get("FAKE_RT_PS", ""))
if args[:1] == ["rm"] and os.environ.get("FAKE_RT_FAIL_RM"):
    print("Error: daemon unreachable", file=sys.stderr)
    sys.exit(1)
"""


@pytest.fixture()
def rt(tmp_path, monkeypatch):
    """(runtime argv, recorded calls(), failure-log dir)."""
    script = tmp_path / "fake_rt.py"
    script.write_text(FAKE_RT, encoding="utf-8")
    log = tmp_path / "rt.log"
    monkeypatch.setenv("FAKE_RT_LOG", str(log))
    for var in ("FAKE_RT_PS", "FAKE_RT_HANG", "FAKE_RT_FAIL_RM", cleanup.TRACE_ENV):
        monkeypatch.delenv(var, raising=False)
    logdir = tmp_path / "systmp"
    logdir.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(logdir))  # where a failure log lands

    def calls() -> list[list[str]]:
        if not log.exists():
            return []
        return [json.loads(ln) for ln in log.read_text(encoding="utf-8").splitlines()]

    return [sys.executable, str(script)], calls, logdir


def _job(argv, tmpdirs, names=("vestibule-aaa",), owners=("own1",), **extra) -> str:
    return json.dumps({
        "runtimes": [{"argv": argv, "owners": list(owners), "names": list(names)}],
        "tmpdirs": [str(d) for d in tmpdirs],
        "sweeps_s": [0.05, 0.1],
        **extra,
    })


def _failure_logs(logdir: Path) -> list[str]:
    return [p.read_text(encoding="utf-8") for p in logdir.glob("vestibule-cleanup-*.log")]


@posix_only
def test_order_names_sweep_tmpdirs_then_later_sweeps(rt, tmp_path, monkeypatch):
    argv, calls, logdir = rt
    work = tmp_path / "vestibule-run-1"
    work.mkdir()
    (work / "main.py").write_text("print(1)")
    trace = tmp_path / "trace.jsonl"
    monkeypatch.setenv("FAKE_RT_PS", "cid1 cid2")
    monkeypatch.setenv(cleanup.TRACE_ENV, str(trace))

    assert cleanup.main(["cleanup", _job(argv, [work])]) == 0

    sweep = [["ps", "-aq", "--filter", "label=vestibule.owner=own1"], ["rm", "-f", "cid1", "cid2"]]
    assert calls() == [["rm", "-f", "vestibule-aaa"], *sweep, *sweep, *sweep]
    assert not work.exists()
    records = [json.loads(ln) for ln in trace.read_text(encoding="utf-8").splitlines()]
    kinds = ["rmtree" if "rmtree" in r else " ".join(r["call"][2:4]) for r in records]
    # Temp dirs go right after the first pass (a late container finds no script), and
    # again at the end.
    assert kinds.index("rmtree") == 3, kinds
    assert kinds[-1] == "rmtree", kinds
    assert all(r.get("rc", 0) == 0 for r in records)
    assert _failure_logs(logdir) == []  # nothing written on success


@posix_only
def test_no_names_still_sweeps_by_owner(rt, tmp_path):
    argv, calls, _ = rt
    assert cleanup.main(["cleanup", _job(argv, [], names=())]) == 0
    assert calls()[0][0] == "ps"  # no `rm -f` with an empty name list
    assert all(c[0] == "ps" for c in calls())  # FAKE_RT_PS unset: nothing to remove


def test_a_hanging_call_is_bounded_and_the_rest_still_runs(rt, tmp_path, monkeypatch):
    argv, _, logdir = rt
    work = tmp_path / "vestibule-run-2"
    work.mkdir()
    monkeypatch.setenv("FAKE_RT_HANG", "ps")
    t0 = time.monotonic()
    assert cleanup.main(["cleanup", _job(argv, [work], sweeps_s=[], call_timeout_s=0.5)]) == 0
    assert time.monotonic() - t0 < 10
    assert not work.exists()  # temp dirs are removed even though a call hung
    logs = _failure_logs(logdir)
    assert len(logs) == 1 and "no answer within 0.5 s" in logs[0], logs


def test_a_failed_call_leaves_a_log_and_never_raises(rt, tmp_path, monkeypatch):
    argv, _, logdir = rt
    monkeypatch.setenv("FAKE_RT_FAIL_RM", "1")
    assert cleanup.main(["cleanup", _job(argv, [], sweeps_s=[])]) == 0
    logs = _failure_logs(logdir)
    assert len(logs) == 1 and "exit 1" in logs[0] and "daemon unreachable" in logs[0], logs


def test_a_missing_runtime_binary_is_logged(rt, tmp_path):
    _, _, logdir = rt
    work = tmp_path / "vestibule-run-3"
    work.mkdir()
    missing = str(tmp_path / "no-such-runtime")
    assert cleanup.main(["cleanup", _job([missing], [work], sweeps_s=[])]) == 0
    assert not work.exists()
    logs = _failure_logs(logdir)
    assert len(logs) == 1 and "could not start" in logs[0], logs


def test_a_bad_job_never_raises(rt):
    _, _, logdir = rt
    assert cleanup.main(["cleanup", "{not json"]) == 0
    logs = _failure_logs(logdir)
    assert len(logs) == 1 and "cleaner crashed" in logs[0], logs


@posix_only
def test_entry_point_by_file_path_needs_no_import_path(rt, tmp_path):
    """`python -I -B <cleanup.py> <job>`, the server's command line, run by the BASE
    interpreter (outside any venv) from a neutral directory: vestibule is not importable
    there, so this proves the cleaner is stdlib-only and needs no PYTHONPATH (step-1b
    critic finding 1: `-m vestibule.cleanup` failed silently under -I for PYTHONPATH and
    --user installs)."""
    argv, calls, _ = rt
    work = tmp_path / "vestibule-run-4"
    work.mkdir()
    trace = tmp_path / "trace.jsonl"
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env[cleanup.TRACE_ENV] = str(trace)
    base = getattr(sys, "_base_executable", sys.executable)
    no_pkg = subprocess.run([base, "-I", "-c", "import vestibule"], capture_output=True,
                            cwd=tmp_path, env=env, check=False)
    assert no_pkg.returncode != 0, "test premise: the base interpreter must not see vestibule"
    proc = subprocess.run(
        [base, "-I", "-B", cleanup.__file__, _job(argv, [work])],
        env=env, cwd=tmp_path, capture_output=True, timeout=60, check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == b"" and proc.stderr == b""  # silent: its stdio is /dev/null anyway
    assert not work.exists()
    assert calls()[0] == ["rm", "-f", "vestibule-aaa"]
    assert trace.exists()
