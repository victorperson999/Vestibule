"""Docs references stay live (M1 step 7b, plan docs/plans/M1/M1-step7-acceptance.md §5.1).

Every relative Markdown link target (badge-wrapped and titled links included), and every
docs/…md path written anywhere in the live docs, the source or the tests (backticked or
not; docstrings mention them as plain text), must point at something that exists, with
the exact case GitHub and Linux will need. Dated records are exempt and stay verbatim:
the CLAUDE.md changelog, docs/reviews/, and docs/archive/. So are git worktrees under
.claude/worktrees/, which are other checkouts, not live docs.
"""
import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_EXEMPT = ("docs/reviews/", "docs/archive/", ".claude/worktrees/")
# Every `](target)`: plain and image links, the outer link of a badge, an optional title.
_LINK = re.compile(r"\]\(([^)\s]+)(?:\s+\"[^\"\n]*\")?\)")
_CODE_SPAN = re.compile(r"`[^`\n]*`")  # link syntax inside inline code is not a link
_DOCS_PATH = re.compile(r"(?<![\w/.-])(?:\./)?docs/[\w./-]+?\.md\b")

# Paths a signed-off plan names before its step creates them (a plan is written before
# its build). Repo-relative, as written. An entry whose file exists fails the test below,
# so each one is removed when its step lands.
_NOT_YET_BUILT: frozenset[str] = frozenset()


def _scanned() -> list[Path]:
    files = [ROOT / "README.md", ROOT / "CLAUDE.md"]
    for folder, pattern in (
        ("docs", "*.md"), (".claude", "*.md"), ("src", "*.py"), ("tests", "*.py"),
    ):
        files += sorted((ROOT / folder).rglob(pattern))
    return [f for f in files if not f.relative_to(ROOT).as_posix().startswith(_EXEMPT)]


def _text(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    if path == ROOT / "CLAUDE.md":  # the changelog is a dated record
        text = text.split("\n## Changelog", 1)[0]
    return text


def _exists_exactly(path: Path) -> bool:
    """Path.exists(), but case-sensitive even on Windows and macOS: GitHub and Linux are."""
    try:
        parts = Path(os.path.normpath(path)).relative_to(ROOT).parts
    except ValueError:  # outside the repo: the case can't be checked here
        return path.exists()
    here = ROOT
    for part in parts:
        try:
            if part not in os.listdir(here):
                return False
        except OSError:  # a file where a directory was expected
            return False
        here = here / part
    return True


def _ok(path: Path) -> bool:
    if _exists_exactly(path):
        return True
    try:
        rel = Path(os.path.normpath(path)).relative_to(ROOT).as_posix()
    except ValueError:
        return False
    return rel in _NOT_YET_BUILT


def _doc_paths(line: str) -> list[str]:
    return [m.group(0) for m in _DOCS_PATH.finditer(line)]


def _link_targets(line: str) -> list[str]:
    return [m.group(1) for m in _LINK.finditer(_CODE_SPAN.sub("", line))]


def _broken(path: Path) -> list[str]:
    rel = path.relative_to(ROOT).as_posix()
    broken = []
    for lineno, line in enumerate(_text(path).splitlines(), 1):
        for ref in _doc_paths(line):
            if not _ok(ROOT / ref):
                broken.append(f"{rel}:{lineno}: {ref}")
        if path.suffix != ".md":
            continue
        for raw in _link_targets(line):
            target = raw.split("#", 1)[0]
            if not target or re.match(r"^[a-z][a-z0-9+.-]*:", target, re.I):
                continue  # in-page anchor, or a URL (http:, mailto:, ...)
            if not _ok(path.parent / target):
                broken.append(f"{rel}:{lineno}: ({raw})")
    return broken


def test_scan_covers_the_live_docs_source_and_tests():
    rels = {f.relative_to(ROOT).as_posix() for f in _scanned()}
    assert {"README.md", "CLAUDE.md", "docs/HISTORY.md", ".claude/WORKFLOW.md",
            "src/vestibule/server.py", "tests/test_acceptance.py",
            "tests/test_docs.py"} <= rels
    assert not any(r.startswith(_EXEMPT) for r in rels)


def test_claude_md_is_scanned_without_its_changelog():
    text = _text(ROOT / "CLAUDE.md")
    assert "Golden rules" in text
    assert not re.search(r"^## Changelog", text, re.M)  # the heading, not mentions of it


# Example paths below that must NOT resolve are built from parts, because this file is
# scanned too.


def test_patterns_see_badges_titles_and_dot_slash_paths():
    badge = "[![MIT](https://img.example/b.svg)](LICENSE)"
    assert _link_targets(badge) == ["https://img.example/b.svg", "LICENSE"]
    assert _link_targets('[x](../src/foo.py "a title")') == ["../src/foo.py"]
    assert _link_targets("[`a.md`](a.md) and `](not-a-link)` in code") == ["a.md"]
    assert _doc_paths("see ./" + "docs" + "/HISTORY.md") == ["./docs/HISTORY.md"]
    assert _doc_paths("other/" + "docs" + "/x.md") == []  # not this repo's docs/


def test_existence_check_is_case_sensitive():
    assert _exists_exactly(ROOT / "docs/HISTORY.md")
    assert _exists_exactly(ROOT / "docs/plans/M1/../M1/M1-container-backend.md")
    assert not _exists_exactly(ROOT / "docs" / "history.md")
    assert not _exists_exactly(ROOT / "docs" / "plans" / "m1" / "M1-container-backend.md")
    assert not _exists_exactly(ROOT / "README.md" / "x.md")


def test_not_yet_built_entries_are_really_not_built():
    built = sorted(p for p in _NOT_YET_BUILT if _exists_exactly(ROOT / p))
    assert built == [], f"these now exist; remove them from _NOT_YET_BUILT: {built}"


def test_every_docs_reference_points_at_an_existing_file():
    broken = [b for f in _scanned() for b in _broken(f)]
    assert broken == [], "stale references:\n" + "\n".join(broken)
