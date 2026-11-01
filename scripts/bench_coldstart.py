"""Cold-start benchmark: native against container, through the server's own dispatch (M2 step 7,
criterion 14; plan docs/plans/M2/M2-step7-acceptance.md §3.1). A dev script, not part of the
gate. Linux only, on a host where `vestibule-mcp setup` has run and Docker works (WSL here):

    ~/.venvs/vestibule/bin/python scripts/bench_coldstart.py

What it does:
1. installs a fresh venv, ~/.venvs/vestibule-bench, builds a wheel of this working tree with
   it, and checks the wheel holds the native helper, the seccomp binding and the vendored
   profile (a wheel without the profile can't even import the server);
2. installs that wheel into the venv with requirements/constraints.txt;
3. per backend, forced (VESTIBULE_BACKEND=native, then container), inside that venv: one
   untimed warm-up call, so selection is cached, then 50 calls of `print(1)` through
   `server.call_tool`, each timed end to end. Every timed call must render exactly
   `isolation: <backend>` and keep the same selection, or the script stops, so a degraded run
   or a re-selection never mixes into a row. Between calls, untimed, the warden's detached
   work is drained, so one run's cleanup (a container's kill and `rm -f`, both CLI calls)
   can't overlap the next. Each container run still starts its own reaper pass (one
   `docker ps`) beside its `docker run`, as every call does in use, so that is in its time;
4. prints the median and p95 per backend, with the host, the commit and the Docker version.

No ratio is printed: where Docker runs in a VM (Docker Desktop, WSL), the container numbers
include that hop, which a Docker Engine user on Linux doesn't pay.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import platform
import re
import statistics
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BENCH_VENV = Path.home() / ".venvs" / "vestibule-bench"
BACKENDS = ("native", "container")
RUNS = 50
WHEEL_MUST_HOLD = ("vestibule/backends/native_helper.py", "vestibule/backends/native_seccomp.py",
                   "vestibule/backends/seccomp-profile.json")


async def _measure(backend: str) -> dict:
    """Inside the bench venv, VESTIBULE_BACKEND forced by the parent: the timed calls."""
    from vestibule import server  # reads the settings from the environment

    async def call() -> None:
        out = await server.call_tool("run_code", {"language": "python", "code": "print(1)"})
        text = "\n".join(c.text for c in out)
        if not (re.search(rf"^isolation: {backend}$", text, re.M)
                and re.search(r"^exit_code: 0$", text, re.M)):
            raise SystemExit(f"not a clean {backend} run; stopping:\n{text}")

    await call()  # the warm-up: selection and its probe run aren't timed
    warden = await server.get_warden()
    times = []
    for _ in range(RUNS):
        t0 = time.perf_counter()
        await call()
        times.append(time.perf_counter() - t0)
        if await server.get_warden() is not warden:
            raise SystemExit("the selection changed during the benchmark; stopping")
        if not await warden.drain(30):
            raise SystemExit("the warden's detached work outlasted 30 s; stopping")
    return {"times": times, "module": server.__file__}


def _out(*cmd: str | Path) -> str:
    return subprocess.run([str(c) for c in cmd], check=True, capture_output=True,
                          text=True).stdout.strip()


def _or_unknown(*cmd: str | Path) -> str:
    try:
        return _out(*cmd)
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--measure", choices=BACKENDS, help=argparse.SUPPRESS)  # the child's mode
    args = ap.parse_args()
    if args.measure:
        print(json.dumps(asyncio.run(_measure(args.measure))))
        return 0
    if sys.platform != "linux":
        print("native isolation needs Linux: run this in WSL or on a Linux host", file=sys.stderr)
        return 2

    rows = {}
    with tempfile.TemporaryDirectory(prefix="vestibule-bench-") as tmp:
        print(f"building {BENCH_VENV} and a wheel of {REPO} ...", file=sys.stderr)
        _out(sys.executable, "-m", "venv", "--clear", BENCH_VENV)
        _out(BENCH_VENV / "bin" / "pip", "wheel", "-q", "--no-deps", "-w", tmp, REPO)
        wheel = next(Path(tmp).glob("*.whl"))
        missing = [n for n in WHEEL_MUST_HOLD if n not in zipfile.ZipFile(wheel).namelist()]
        if missing:
            print(f"{wheel.name} is missing {', '.join(missing)}", file=sys.stderr)
            return 1
        _out(BENCH_VENV / "bin" / "pip", "install", "-q", wheel,
             "-c", REPO / "requirements" / "constraints.txt")
        for backend in BACKENDS:
            print(f"{backend}: {RUNS} timed calls ...", file=sys.stderr)
            env = {k: v for k, v in os.environ.items() if not k.startswith("VESTIBULE_")}
            env.update(VESTIBULE_BACKEND=backend, VESTIBULE_WORKSPACE=f"{tmp}/ws-{backend}")
            p = subprocess.run([str(BENCH_VENV / "bin" / "python"), "-I", __file__, "--measure",
                                backend], env=env, cwd=tmp, capture_output=True, text=True)
            if p.returncode != 0:
                print(f"{backend}: stopped\n{p.stdout}{p.stderr[-4000:]}", file=sys.stderr)
                return 1
            rows[backend] = json.loads(p.stdout.splitlines()[-1])
            if not rows[backend]["module"].startswith(str(BENCH_VENV)):  # the wheel, not src/
                print(f"measured {rows[backend]['module']}, not the wheel", file=sys.stderr)
                return 1

    git = ("git", "-C", REPO, "-c", "core.autocrlf=input")  # CRLF checkouts aren't changes
    # What the wheel and its install are made from, untracked files included (hatchling packs them)
    dirty = _or_unknown(*git, "status", "--porcelain", "--", "src", "pyproject.toml", "requirements")
    try:
        distro = platform.freedesktop_os_release().get("PRETTY_NAME", "Linux")
    except OSError:
        distro = "Linux"
    print(f"Vestibule cold start: run_code print(1) through server.call_tool, {RUNS} timed "
          f"calls per backend after one warm-up")
    print(f"host:     {distro}, kernel {os.uname().release}, {os.cpu_count()} CPUs, "
          f"Python {platform.python_version()}")
    print(f"commit:   {_or_unknown(*git, 'rev-parse', '--short', 'HEAD')}"
          + (" (+ uncommitted changes in src/, pyproject.toml or requirements/)"
             if dirty not in ("", "unknown") else ""))
    print(f"docker:   {_or_unknown('docker', 'version', '--format', '{{.Server.Version}}')}")
    print(f"measured: {wheel.name}, installed in {BENCH_VENV}")
    print(f"{'backend':<10} {'median':>10} {'p95':>10}")
    for backend, row in rows.items():
        t = sorted(row["times"])
        p95 = t[math.ceil(0.95 * len(t)) - 1]  # nearest rank
        print(f"{backend:<10} {statistics.median(t) * 1000:>7.1f} ms {p95 * 1000:>7.1f} ms")
    print("No ratio: the container numbers include any VM hop to the Docker daemon.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
