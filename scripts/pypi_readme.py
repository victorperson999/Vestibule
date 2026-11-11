"""PyPI's page (M4 step 5c): PYPI.md is README.md without the parts only GitHub's page keeps.
README.md is the one text anyone edits; after an edit, run this and commit PYPI.md with it.

    python scripts/pypi_readme.py            writes PYPI.md
    python scripts/pypi_readme.py --check    writes nothing; fails if PYPI.md isn't up to date
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# What the page leaves out (docs/plans/M4/M4-step5c-pypi-page.md §3): the status badge with the
# space before it, the warning with the blank line after it, and the sections Status and
# Documentation, which run together up to License.
_BADGE = r" \[!\[Status: Early Development\]\([^)\s]*\)\]\([^)\s]*\)"
_WARNING = r"^> ⚠️ \*\*Early development[^\n]*\n\n"
_SECTIONS = r"^## Status\n.*?^## Documentation\n.*?(?=^## License\n)"
# Each is in README.md exactly once, or nothing is written.
_ONCE = {"the status badge": _BADGE, "the early-development warning": _WARNING,
         "## Status": r"^## Status\n", "## Documentation": r"^## Documentation\n",
         "## License right after ## Status and ## Documentation": _SECTIONS}
_FLAGS = re.MULTILINE | re.DOTALL


def pypi_text(readme: str) -> str:
    for name, part in _ONCE.items():
        found = len(re.findall(part, readme, _FLAGS))
        if found != 1:
            raise SystemExit(f"pypi_readme: README.md has {name} {found} times, not once. "
                             "PYPI.md was not written.")
    for part in (_BADGE, _WARNING, _SECTIONS):
        readme = re.sub(part, "", readme, flags=_FLAGS)
    return readme


def main() -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("--check", action="store_true",
                    help="write nothing; fail if PYPI.md isn't what README.md makes")
    args = ap.parse_args()
    # Read as text, so a checkout with CRLF line endings (core.autocrlf=true) reads as LF.
    want = pypi_text((ROOT / "README.md").read_text(encoding="utf-8"))
    pypi = ROOT / "PYPI.md"
    if not args.check:
        pypi.write_text(want, encoding="utf-8", newline="\n")
        return 0
    if pypi.read_text(encoding="utf-8") != want:
        print("pypi_readme: PYPI.md isn't what README.md makes: run scripts/pypi_readme.py")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
