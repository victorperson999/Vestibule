"""The release's checks (M4 F2; the checklist is docs/RELEASE.md). A dev tool: run it with a dev
venv's Python from a checkout, so its stdout is the user's.

    python scripts/release_smoke.py build [--repo PATH] [--uv PATH]
    python scripts/release_smoke.py smoke <wheel-or-requirement> [--python 3.11 ...]
                                    [--expect native|container|blocked] [--uv PATH] [--allow-drift]

`build` makes dist/'s two files from a clean export of HEAD, and puts them there only once every
check has passed: `uv publish` takes all of dist/ by default, and an upload can't be taken back.
`smoke` installs a wheel, or a version from the index, into throwaway directories and tries it the
way a user's client would.
"""
from __future__ import annotations

import argparse
import asyncio
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "scripts"), str(ROOT / "tests")]

PYTHONS = ("3.11", "3.12", "3.13", "3.14", "3.15")  # pyproject's classifiers say the same (F1)
# What ships, and the set `smoke` compares an install against.
_MUST_BE_COMMITTED = ("src", "pyproject.toml", "README.md", "LICENSE",
                      "requirements/constraints.txt")
_SDIST_REQUIRED = ("LICENSE", "PKG-INFO", "README.md", "pyproject.toml")
_SDIST_ALSO_ALLOWED = (".gitignore",)  # hatchling always adds it
# V13: without these a wheel can't start. Packaged data, so nothing else would notice.
_WHEEL_REQUIRED = ("vestibule/backends/native_helper.py", "vestibule/backends/native_seccomp.py",
                   "vestibule/backends/seccomp-profile.json")
_WHEEL_METADATA = re.compile(r"vestibule_mcp-[^/]+\.dist-info/")
_READY = "ready in this terminal's environment: isolation "
_EXPECTED = {"linux": "native", "win32": "container"}  # anywhere else, no runtime in CI: blocked
_CHECK_S = 120
_INSTALL_S = 300
_SETUP_S = 900  # it may really download the images or the trees
_E2E_S = 600
_ANSWER = 'import json, sys, vestibule, vestibule.server; ' \
          'print(json.dumps([vestibule.__file__, sys.version_info.releaselevel, sys.version.split()[0]]))'


def sdist_problems(names: list[str]) -> list[str]:
    """F2: only what the include list allows, and all of it. Names are inside the top folder."""
    allowed = (*_SDIST_REQUIRED, *_SDIST_ALSO_ALLOWED)
    return ([f"not allowed in the sdist: {n}" for n in names
             if n not in allowed and not n.startswith("src/vestibule/")]
            + [f"missing from the sdist: {n}" for n in _SDIST_REQUIRED if n not in names])


def wheel_problems(wheel: zipfile.ZipFile) -> list[str]:
    """V13's files are there, nothing else is, and no source has CRLF (a plain `git archive` on a
    machine with core.autocrlf=true would give it some)."""
    names = wheel.namelist()
    return ([f"missing from the wheel: {n}" for n in _WHEEL_REQUIRED if n not in names]
            + [f"not allowed in the wheel: {n}" for n in names
               if not n.startswith("vestibule/") and not _WHEEL_METADATA.match(n)]
            + [f"CRLF in {n}" for n in names if n.endswith(".py") and b"\r\n" in wheel.read(n)])


def parse_setup(stderr: str, code: int, expect: str) -> str | None:
    """None when `setup` gave exactly the answer expected, else what it said. The whole token, at
    the start of the line: `native` must not match `native-degraded`."""
    for line in stderr.splitlines():
        if line.startswith("not ready:"):
            return line
        if line.startswith("Blocked:"):
            return None if expect == "blocked" and code == 1 else f"exit {code}: {line[:300]}"
        if line.startswith(_READY):
            isolation = line[len(_READY):].split(" ", 1)[0]
            return None if isolation == expect and code == 0 else f"exit {code}: {line[:300]}"
    return f"exit {code}, and setup gave no answer"


def call_problem(text: str, expect: str) -> str | None:
    """None when a `run_code` result is exactly the isolation expected."""
    if expect == "blocked":
        return None if text.startswith("Blocked:") else text[:300]
    return None if text.splitlines()[-1:] == [f"isolation: {expect}"] else text[:300]


def check_install(module_file: str, root: Path, releaselevel: str) -> str | None:
    """That this install is what's being tried, on an interpreter a classifier can rest on."""
    if not Path(module_file).resolve().is_relative_to(root.resolve()):
        return f"vestibule was imported from {module_file}, not the install under {root}"
    if releaselevel != "final":
        return f"this interpreter is a {releaselevel} build; only final releases count"
    return None


def drift(installed: dict[str, str], pins: dict[str, str]) -> list[str]:
    """F1: what a user would get that neither gate ran."""
    return [f"{name} {version} (the gates ran {pins.get(name, 'no version of it')})"
            for name, version in sorted(installed.items())
            if name != "vestibule-mcp" and pins.get(name) != version]


def e2e_problem(expect: str, skip_reason: str | None) -> str | None:
    """An all-skipped pytest run exits 0, which would pass without proving anything. macOS has no
    Docker to run them with (P9)."""
    return None if skip_reason is None or expect == "blocked" else f"they would skip: {skip_reason}"


def _run(argv: list[str], timeout: float, **kw: object) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, capture_output=True, text=True, errors="replace", timeout=timeout,
                          stdin=subprocess.DEVNULL, check=False, **kw)  # type: ignore[call-overload]


def build(repo: Path, uv: str) -> int:
    # A CRLF checkout, or a local file nothing packages, isn't a change here.
    autocrlf = ["-c", "core.autocrlf=input"] if os.name == "posix" else []
    dirty = _run(["git", *autocrlf, "status", "--porcelain", "--", *_MUST_BE_COMMITTED], _CHECK_S,
                 cwd=repo)
    if dirty.returncode != 0 or dirty.stdout.strip():
        print(f"build: commit these first, the export is of HEAD:\n{dirty.stdout}{dirty.stderr}")
        return 1
    with tempfile.TemporaryDirectory(prefix="vestibule-build-", ignore_cleanup_errors=True) as tmp:
        tree, built = Path(tmp, "tree"), Path(tmp, "built")
        archive = subprocess.run(["git", "-c", "core.autocrlf=false", "archive", "--format=tar", "HEAD"],
                                 cwd=repo, capture_output=True, check=True, timeout=_CHECK_S)
        with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as tf:
            tf.extractall(tree, filter="data")
        # Neither --sdist nor --wheel: the wheel comes from the sdist, so the include list is proven.
        made = _run([uv, "build", "--out-dir", str(built)], _INSTALL_S, cwd=tree)
        if made.returncode != 0:
            print(f"build: uv build failed:\n{made.stderr[-2000:]}")
            return 1
        return finish_build(built, repo / "dist", uv)


def finish_build(built: Path, dist: Path, uv: str) -> int:
    """Every check, and only then dist/."""
    sdist, wheel = next(built.glob("*.tar.gz")), next(built.glob("*.whl"))
    with tarfile.open(sdist) as tf:
        names = sorted(m.name.split("/", 1)[1] for m in tf.getmembers() if m.isfile())
    print(f"{sdist.name}: {len(names)} files")
    for name in names:
        print(f"  {name}")
    with zipfile.ZipFile(wheel) as zf:
        problems = sdist_problems(names) + wheel_problems(zf)
    twine = _run([uv, "tool", "run", "twine", "check", str(sdist), str(wheel)], _INSTALL_S)
    if twine.returncode != 0:
        problems.append(f"twine check failed:\n{twine.stdout[-1500:]}{twine.stderr[-500:]}")
    if problems:
        print("build: FAILED; dist/ was left as it was")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    dist.mkdir(exist_ok=True)
    for old in dist.glob("vestibule_mcp-*"):
        old.unlink()
    for new in (sdist, wheel):
        shutil.move(str(new), dist / new.name)
    print(f"build: OK. In {dist}: {sdist.name} and {wheel.name}")
    return 0


async def _stdio_problem(exe: Path, workspace: Path, env: dict[str, str], expect: str) -> str | None:
    from stdioutil import STDIN, StdioServer

    server = await StdioServer.start(workspace, base_env=env, exe=exe)
    try:
        await server.initialize()
        tools = await server.request("tools/list", {})
        names = sorted(t["name"] for t in tools["tools"])
        if names != ["read_workspace", "run_code"]:
            return f"tools/list gave {names}"
        text = await server.call("run_code", {"language": "python", "code": "print(1)"}, 120)
        problem = call_problem(text, expect)
        if problem:
            return f"run_code gave: {problem}"
        code, _seconds, phase = await server.close_like_sdk()
        await server.finish_reading()
        if server.bad_stdout:
            return f"non-JSON on stdout: {server.bad_stdout[0][:200]}"
        # The phase too: a server that ignores EOF still exits 0 on the client's later SIGTERM.
        return None if (code, phase) == (0, STDIN) else f"exit {code}, in phase {phase}"
    finally:
        await server.kill()


def _smoke_one(python: str, source: str, expect: str, uv: str, allow_drift: bool) -> str | None:
    """The first failing step, or None."""
    from check_env import _norm, read_constraints
    from dockerutil import docker_skip_reason

    with tempfile.TemporaryDirectory(prefix="vestibule-smoke-", ignore_cleanup_errors=True) as tmp:
        root = Path(tmp).resolve()
        env = {k: v for k, v in os.environ.items() if not k.startswith("VESTIBULE_")}
        env.update(UV_TOOL_DIR=str(root / "tools"), UV_TOOL_BIN_DIR=str(root / "bin"),
                   UV_CACHE_DIR=str(root / "cache"), UV_PYTHON_INSTALL_DIR=str(root / "python"),
                   VESTIBULE_WORKSPACE=str(root / "ws"), VESTIBULE_AUDIT_DIR=str(root / "audit"))
        windows = sys.platform == "win32"
        exe = root / "bin" / ("vestibule-mcp.exe" if windows else "vestibule-mcp")
        tool_python = root / "tools" / "vestibule-mcp" / (
            "Scripts/python.exe" if windows else "bin/python")

        r = _run([uv, "tool", "install", "--python", python, source], _INSTALL_S, env=env)
        if r.returncode != 0:
            return f"install: {r.stderr[-1500:]}"

        r = _run([str(tool_python), "-c", _ANSWER], _CHECK_S, env=env)
        if r.returncode != 0:
            return f"import vestibule.server: {r.stderr[-1500:]}"
        module_file, level, version = json.loads(r.stdout.splitlines()[-1])
        print(f"  python {version}", flush=True)
        problem = check_install(module_file, root, level)
        if problem:
            return f"import: {problem}"

        listed = _run([uv, "pip", "list", "--python", str(tool_python), "--format", "json"],
                      _CHECK_S, env=env)
        installed = {_norm(p["name"]): p["version"] for p in json.loads(listed.stdout)}
        moved = drift(installed, read_constraints(ROOT / "requirements" / "constraints.txt"))
        for line in moved:
            print(f"  drift: {line}", flush=True)
        if moved and not allow_drift:
            return "drift: re-freeze requirements/constraints.txt and run both gates (F1)"

        r = _run([str(exe), "setup"], _SETUP_S, env=env)
        if r.stdout:
            return f"setup wrote to stdout: {r.stdout[:300]}"
        problem = parse_setup(r.stderr, r.returncode, expect)
        if problem:
            return f"setup: {problem}"

        problem = asyncio.run(_stdio_problem(exe, root / "ws", env, expect))
        if problem:
            return f"stdio: {problem}"

        r = _run([str(exe), "audit"], _CHECK_S, env=env)
        shown = r.stdout + r.stderr
        if r.returncode != 0 or str(root / "audit") not in shown or "python" not in r.stdout:
            return f"audit: exit {r.returncode}: {shown[-600:]}"

        problem = e2e_problem(expect, docker_skip_reason())
        if problem:
            return f"end-to-end tests: {problem}"
        r = _run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                  "tests/test_stdio_e2e.py"], _E2E_S, cwd=ROOT,
                 env={**env, "VESTIBULE_TEST_SERVER_EXE": str(exe)})
        if r.returncode != 0:
            return f"end-to-end tests: {r.stdout[-1500:]}"
        print(f"  {r.stdout.strip().splitlines()[-1]}", flush=True)
        return None


def smoke(source: str, pythons: list[str], expect: str, uv: str, allow_drift: bool) -> int:
    print(_run([uv, "--version"], _CHECK_S).stdout.strip(), f"| source: {source} | expect: {expect}")
    for python in pythons:
        print(f"Python {python}", flush=True)
        problem = _smoke_one(python, source, expect, uv, allow_drift)
        if problem:
            print(f"smoke: FAILED on Python {python}, at {problem}")
            return 1
    print(f"smoke: OK on {', '.join(pythons)}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    commands = ap.add_subparsers(dest="command", required=True)
    b = commands.add_parser("build", help="make dist/ from a clean export of HEAD, and check it")
    b.add_argument("--repo", type=Path, default=ROOT)
    b.add_argument("--uv", default="uv")
    s = commands.add_parser("smoke", help="install a wheel or a version, and try it")
    s.add_argument("source")
    s.add_argument("--python", nargs="+", default=list(PYTHONS))
    s.add_argument("--expect", choices=("native", "container", "blocked"),
                   default=_EXPECTED.get(sys.platform, "blocked"))
    s.add_argument("--uv", default="uv")
    s.add_argument("--allow-drift", action="store_true",
                   help="report a package no gate ran, and carry on (after the upload)")
    args = ap.parse_args()
    if args.command == "build":
        return build(args.repo.resolve(), args.uv)
    return smoke(args.source, args.python, args.expect, args.uv, args.allow_drift)


if __name__ == "__main__":
    sys.exit(main())
