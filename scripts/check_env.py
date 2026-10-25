"""Check that this venv matches requirements/constraints.txt exactly (M2 step 1, S1-D1).

Run it with the venv's own interpreter, before any other check:

    .venv/Scripts/python.exe scripts/check_env.py              (Windows)
    ~/.venvs/vestibule311/bin/python scripts/check_env.py --expect-python 3.11   (Linux)

The rules:
- every installed distribution must appear in the constraints at exactly the installed
  version. A constraint that is not installed is fine (the Windows-only lines on Linux).
  Exempt: the venv's own bootstrap tools (`python -m venv` installs pip, and setuptools on
  Python < 3.12, at versions fixed by the interpreter build; neither is imported by the code
  or the tests) and a short list of local debugging tools that are not dependencies;
- this project must be installed, and `import vestibule` must resolve to THIS checkout's
  `src/`. The Linux venvs live outside the repo, so a gate run from a second clone would
  otherwise test the first clone's code;
- with --expect-python X.Y, the interpreter must be that version.

pip never uninstalls, so a stale package left by an earlier install fails here instead of
quietly changing what the tests run against.
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import re
import sys
from importlib import metadata
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROJECT = "vestibule-mcp"
BOOTSTRAP = {"pip", "setuptools", "wheel"}
# Local debugging tools, never imported by the code or the tests.
LOCAL_TOOLS = {"py-spy"}


def _norm(name: str) -> str:
    """PEP 503 normalized name: case and runs of -_. don't matter."""
    return re.sub(r"[-_.]+", "-", name).lower()


def read_constraints(path: Path) -> dict[str, str]:
    pins: dict[str, str] = {}
    for n, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        name, sep, version = line.partition("==")
        if not sep or not name.strip() or not version.strip():
            raise SystemExit(f"{path}:{n}: expected name==version, got {raw!r}")
        pins[_norm(name.strip())] = version.strip()
    return pins


def package_problems(pins: dict[str, str]) -> list[str]:
    exempt = {_norm(n) for n in {PROJECT, *BOOTSTRAP, *LOCAL_TOOLS}}
    found: list[str] = []
    for dist in metadata.distributions():
        # .get(): a missing Name is a broken install, not something to skip quietly.
        raw_name = dist.metadata.get("Name")
        if not raw_name:
            where = getattr(dist, "_path", "an unknown location")
            found.append(f"a distribution at {where} has no name in its metadata (broken install)")
            continue
        name = _norm(raw_name)
        if name in exempt:
            continue
        want = pins.get(name)
        if want is None:
            found.append(f"{raw_name}=={dist.version} is installed but not in the constraints")
        elif dist.version != want:
            found.append(f"{raw_name}=={dist.version} is installed; the constraints say {want}")
    return sorted(found, key=str.lower)


def binding_problems() -> list[str]:
    try:
        metadata.distribution(PROJECT)
    except metadata.PackageNotFoundError:
        return [f"{PROJECT} is not installed in this venv"]
    spec = importlib.util.find_spec("vestibule")
    origin = spec.origin if spec is not None else None
    if origin is None:
        return ["`import vestibule` does not resolve in this venv"]
    src = (ROOT / "src").resolve()
    # Compare by identity, not by spelling: /mnt/c (WSL) is case-insensitive, so the same
    # checkout can be reached as /mnt/c/Users/... and /mnt/c/users/...
    parents = Path(origin).resolve().parents
    if not any(p.exists() and os.path.samefile(p, src) for p in parents):
        return [f"`import vestibule` resolves to {origin}, not this checkout ({src}): "
                "the venv is bound to another checkout"]
    return []


def main() -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("--constraints", type=Path, default=ROOT / "requirements" / "constraints.txt")
    ap.add_argument("--expect-python", metavar="X.Y",
                    help="fail unless this interpreter is Python X.Y")
    args = ap.parse_args()
    bad: list[str] = []
    have = "%d.%d" % sys.version_info[:2]
    if args.expect_python and have != args.expect_python:
        bad.append(f"this venv runs Python {have}, expected {args.expect_python}")
    bad += binding_problems()
    bad += package_problems(read_constraints(args.constraints))
    where = f"{sys.executable} (Python {sys.version.split()[0]})"
    if bad:
        print(f"check_env: FAIL for {where}")
        for p in bad:
            print(f"  - {p}")
        print("Fix: rebuild the venv from this checkout with -c requirements/constraints.txt "
              "(Linux: bash scripts/gate-linux.sh --setup).")
        return 1
    print(f"check_env: OK for {where}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
