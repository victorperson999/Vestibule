"""Docs references stay live (M1 step 7b, plan docs/plans/M1/M1-step7-acceptance.md §5.1).

Every relative Markdown link target (badge-wrapped and titled links included), and every
docs/…md path written anywhere in the live docs, the source or the tests (backticked or
not; docstrings mention them as plain text), must point at something that exists, with
the exact case GitHub and Linux will need. Dated records are exempt and stay verbatim:
the CLAUDE.md changelog, docs/reviews/, and docs/archive/. So are git worktrees under
.claude/worktrees/, which are other checkouts, not live docs.
"""
import ast
import hashlib
import importlib.util
import os
import re
import shutil
import subprocess
import sys
import tomllib
import unicodedata
from collections import Counter
from pathlib import Path

import pytest

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
    files = [ROOT / "README.md", ROOT / "CLAUDE.md", ROOT / "SECURITY.md"]
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
    assert {"README.md", "CLAUDE.md", "SECURITY.md", "docs/HISTORY.md", ".claude/WORKFLOW.md",
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


# M4 step 4 (docs/plans/M4/M4-step4-docs.md §2.9, §5): the README is also the PyPI page,
# which breaks relative links and drops GitHub's heading ids. So every README link is an
# https:// URL, and the checks the relative forms got above are made on the URLs instead.
_README = ROOT / "README.md"
_NOT_HTTPS = re.compile(r"\]\((?!https://)([^)\n]*)")  # any `](`, however it's written
_RAW_LINK_HTML = re.compile(r"<(?:a|img)[\s/>]", re.I)
_REF_DEFINITION = re.compile(r" {0,3}\[[^\]\n]+\]:")  # every reference-style link needs one
_REPO = r"victorperson999/Vestibule"  # GitHub takes the owner and the repo in any case
_HEADING_LINK = re.compile(rf"https://github\.com/{_REPO}/?#([^\s)\]>\"'`]*)", re.I)
_FILE_ANCHOR = re.compile(  # groups: the file, the id
    rf"https://github\.com/{_REPO}/blob/main/([^\s)\]>\"'`#?]+)(?:\?[^\s)\]>\"'`#]*)?"
    r"#([^\s)\]>\"'`]*)",
    re.I,
)
_HOME_QUERY = re.compile(rf"https://github\.com/{_REPO}/?\?", re.I)  # ?tab=readme-ov-file
_GITHUB = re.compile(  # any GitHub URL; groups: the site, the owner/repo, the rest
    r"(https?://(?:www\.)?github\.com|https?://raw\.githubusercontent\.com)"
    r"/([^/\s)\]>\"'`#?]*/?[^/\s)\]>\"'`#?]*)([^\s)\]>\"'`]*)",
    re.I,
)
_SITES = ("https://github.com", "https://raw.githubusercontent.com")  # as the README writes them
_ON_MAIN = re.compile(r"(?:(blob|tree|raw)/)?main(?:/(.*))?")  # groups: the kind, the path
_IMAGE = re.compile(r"!\[(?:[^\[\]\n]|\[[^\[\]\n]*\])*\]\(\s*<?([^)\s>]+)")  # group: its URL
# Pages of this repo besides its own page, its files and its folders that the README may link,
# such as "issues". It links none today.
_REPO_PAGES: frozenset[str] = frozenset()
_FENCE = re.compile(r" {0,3}(`{3,}|~{3,})")
_ATX_HEADING = re.compile(r" {0,3}#{1,6}[ \t]+(.*?)(?:[ \t]+#+)?[ \t]*$")


def _github_slug(heading: str) -> str:
    """github-slugger's rule, which GitHub's heading ids follow: lowercase, keep letters,
    marks, digits, `_`, `-` and spaces, and make each space a hyphen."""
    kept = [
        c for c in heading.lower()
        if c in " -" or unicodedata.category(c)[0] in "LM"
        or unicodedata.category(c) in ("Nd", "Pc")
    ]
    return "".join(kept).replace(" ", "-")


def _heading_ids(text: str) -> set[str]:
    """The ids GitHub gives a page's ATX headings, a repeated one numbered -1, -2, ..."""
    seen: dict[str, int] = {}
    fence = None
    for line in text.splitlines():
        if m := _FENCE.match(line):
            if fence is None:
                fence = m.group(1)
            elif m.group(1).startswith(fence):  # the same character, at least as many
                fence = None
        elif fence is None and (h := _ATX_HEADING.match(line)):
            base = slug = _github_slug(h.group(1))
            while slug in seen:
                seen[base] += 1
                slug = f"{base}-{seen[base]}"
            seen[slug] = 0
    return set(seen)


def test_readme_links_are_absolute():
    bad = []
    for lineno, line in enumerate(_text(_README).splitlines(), 1):
        prose = _CODE_SPAN.sub("", line)
        bad += [f"README.md:{lineno}: ]({t})" for t in _NOT_HTTPS.findall(prose)]
        if _RAW_LINK_HTML.search(prose) or _REF_DEFINITION.match(prose):
            bad.append(f"README.md:{lineno}: {line.strip()}")
    assert bad == [], "not an https:// link, which PyPI needs:\n" + "\n".join(bad)


def _git_ignored(paths: list[str]) -> set[str]:
    """Which of these paths git ignores: they are never pushed, so a link to one 404s. A tree
    without .git (one exported from the repo) has no ignore rules to apply, so none."""
    if not paths or not (ROOT / ".git").exists():
        return set()
    git = shutil.which("git")
    assert git, "this is a git checkout, but git isn't on PATH to read its ignore rules"
    done = subprocess.run(  # no shell; check-ignore only reads, and no optional index refresh
        [git, "-C", str(ROOT), "check-ignore", "-z", "--stdin"],
        input="\0".join(paths), capture_output=True, text=True, encoding="utf-8",
        env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"}, timeout=60,
    )
    assert done.returncode in (0, 1), f"git check-ignore failed: {done.stderr}"  # 1: none
    return set(done.stdout.split("\0")) - {""}


def test_readme_links_into_this_repo_exist():
    seen, bad, paths = 0, [], []
    for lineno, line in enumerate(_text(_README).splitlines(), 1):
        for url in _IMAGE.findall(_CODE_SPAN.sub("", line)):  # a blob/ URL is a page, not bytes
            g = _GITHUB.match(url)
            if g and g.group(1).lower() != _SITES[1] and not g.group(3).startswith("/raw/"):
                bad.append(f"README.md:{lineno}: {url} (an image needs a raw URL)")
        for m in _GITHUB.finditer(line):  # the README links no other GitHub repo
            site, repo, rest = m.groups()
            where = f"README.md:{lineno}: {m.group(0)}"
            page = re.split(r"[?#]", rest, maxsplit=1)[0].strip("/")
            on_main = _ON_MAIN.fullmatch(page)
            raw = site.lower() == _SITES[1]
            clone = not rest and repo.lower() == f"{_REPO}.git".lower()  # git clone's URL
            if site.lower() not in _SITES:
                bad.append(f"{where} (write it as {_SITES[0]}/... or {_SITES[1]}/...)")
            elif repo.lower() != _REPO.lower() and not clone:
                bad.append(f"{where} (not this repo)")
            elif not raw and (page == "" or page in _REPO_PAGES):
                continue  # the repo's own page: the heading test checks its #id
            # github.com puts blob/, tree/ or raw/ before the branch; raw.githubusercontent none
            elif not on_main or (on_main.group(1) is None) != raw:
                bad.append(f"{where} (only blob/, tree/ and raw links on main)")
            else:
                seen += 1
                path = (on_main.group(2) or "").rstrip("/")
                kind = "raw" if raw else on_main.group(1)
                if "#" in rest and kind != "blob":
                    bad.append(f"{where} (an #id here can't be checked)")
                elif not path and kind != "tree":  # tree/main alone is the root folder
                    bad.append(f"{where} (names no file)")
                elif ".." in path.split("/") or not _exists_exactly(ROOT / path):
                    bad.append(f"{where} (no such path)")
                elif kind == "raw" and not (ROOT / path).is_file():  # blob/tree: GitHub redirects
                    bad.append(f"{where} (a raw URL names a file, not a folder)")
                elif path:
                    paths.append((where, path))
    ignored = _git_ignored([path for _, path in paths])
    bad += [f"{where} (git ignores it)" for where, path in paths if path in ignored]
    assert bad == [], "links into GitHub that won't work there:\n" + "\n".join(bad)
    assert seen, "found no link to a file or folder of this repo, so none was checked"


def test_readme_heading_links_resolve():
    """Permanent: step 5's fix loop is when the README's headings change. A link to a
    heading of another file in the repo is held to that file's headings."""
    text = _text(_README)
    ids = {"README.md": _heading_ids(text)}
    links, bad = 0, []
    for lineno, line in enumerate(text.splitlines(), 1):
        if _HOME_QUERY.search(line):
            bad.append(f"README.md:{lineno}: a query on the repo's page; link .../Vestibule#id")
        own = [("README.md", slug) for slug in _HEADING_LINK.findall(line)]
        links += len(own)
        for path, slug in own + _FILE_ANCHOR.findall(line):
            if path not in ids:
                md = path.endswith(".md") and _exists_exactly(ROOT / path)
                ids[path] = _heading_ids((ROOT / path).read_text(encoding="utf-8")) if md else set()
            if slug not in ids[path]:
                bad.append(f"README.md:{lineno}: {path}#{slug}")
    assert links, "found no link to a heading, so nothing was checked"
    assert bad == [], "heading links that don't resolve on GitHub:\n" + "\n".join(bad)


# SECURITY.md ends each claim with its source; a test or test file it names must exist.
_SECURITY = ROOT / "SECURITY.md"
_CITATION = re.compile(r"(?<![\w/.-])([\w./-]+\.py)\b(?:::(\w+))?")  # groups: file, test


def _test_names(path: Path) -> set[str]:
    return {
        node.name for node in ast.parse(path.read_text(encoding="utf-8")).body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name.startswith("test")
    }


def test_security_md_cites_only_what_exists():
    tests = files = 0
    bad = []
    for lineno, line in enumerate(_text(_SECURITY).splitlines(), 1):
        for m in _CITATION.finditer(line):
            path, test = m.groups()
            if not path.startswith("tests/"):  # other .py files may be named in prose
                if test or Path(path).name.startswith("test_"):
                    bad.append(f"SECURITY.md:{lineno}: {m.group(0)} (cite it from tests/)")
                continue
            if test:
                tests += 1
            else:
                files += 1
            if not _exists_exactly(ROOT / path) or (test and test not in _test_names(ROOT / path)):
                bad.append(f"SECURITY.md:{lineno}: {m.group(0)}")
    assert tests and files, f"found {tests} test and {files} file citations; expected both"
    assert bad == [], "no such test or file:\n" + "\n".join(bad)


# The README's demo GIFs (M4 steps 4b and 5d), oldest first; the README shows the last. Published
# pages load each one from main, and their captions can't be edited, so none may change or go.
_PUBLISHED_GIFS = [
    ("docs/demo.gif", "85d12697e8d591237e85363fc8b3294fc5f77fafd999e94d2868a16e54dca722"),
    ("docs/demo-2.gif", "0436af530a7de9ea8367062cbd302426d896e1a2f67880f9224f34fb76289e17"),
]


@pytest.mark.parametrize("path, sha256", _PUBLISHED_GIFS, ids=[p for p, _ in _PUBLISHED_GIFS])
def test_a_published_demo_gif_is_there_unchanged(path, sha256):
    assert _exists_exactly(ROOT / path), f"{path} is gone, and a published page loads it"
    data = (ROOT / path).read_bytes()
    assert data[:6] in (b"GIF87a", b"GIF89a"), f"{path} isn't a GIF: it starts {data[:6]!r}"
    # Criterion 7's "under 3 MB", read as 3,000,000 bytes.
    assert len(data) < 3_000_000, f"{path} is {len(data):,} bytes, not under 3 MB"
    assert hashlib.sha256(data).hexdigest() == sha256, f"{path} changed, and a page loads it"


def test_the_readme_shows_the_newest_demo_gif():
    shown = []  # the README's images that are GIFs under docs/ on main
    for line in _text(_README).splitlines():
        for url in _IMAGE.findall(_CODE_SPAN.sub("", line)):
            g = _GITHUB.match(url)
            if not g or g.group(1).lower() != _SITES[1]:
                continue
            on_main = _ON_MAIN.fullmatch(re.split(r"[?#]", g.group(3), maxsplit=1)[0].strip("/"))
            path = (on_main and on_main.group(2)) or ""
            if path.startswith("docs/") and path.endswith(".gif"):
                shown.append(path)
    assert shown == [_PUBLISHED_GIFS[-1][0]]


# M4 step 5c (docs/plans/M4/M4-step5c-pypi-page.md §5): PyPI's page is PYPI.md, which
# scripts/pypi_readme.py cuts from the README, the one text anyone edits.
_PYPI = ROOT / "PYPI.md"
_LEFT_OUT = ("Status", "Documentation")  # the sections PyPI's page leaves out (§3, Y3)


@pytest.fixture()
def pypi_readme():
    script = ROOT / "scripts" / "pypi_readme.py"
    spec = importlib.util.spec_from_file_location("pypi_readme", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_pypi_md_is_what_the_script_makes_of_the_readme(pypi_readme):
    """As text, so a checkout with CRLF line endings (core.autocrlf=true) compares the same."""
    want = pypi_readme.pypi_text(_README.read_text(encoding="utf-8"))
    assert _PYPI.read_text(encoding="utf-8") == want, "run scripts/pypi_readme.py"


def test_pypi_md_loses_only_the_badge_the_warning_and_two_sections():
    readme, pypi = _README.read_text(encoding="utf-8"), _PYPI.read_text(encoding="utf-8")
    assert {_github_slug(title) for title in _LEFT_OUT} <= _heading_ids(readme)
    kept, inside = [], False  # the README's lines outside those sections
    for line in readme.splitlines():
        if line.startswith("## "):
            inside = line[3:] in _LEFT_OUT
        if not inside:
            kept.append(line)
    assert _heading_ids(pypi) == _heading_ids("\n".join(kept))
    row = next(line for line in kept if "[![Status: Early Development]" in line)
    warning = next(line for line in kept if line.startswith("> ⚠️ **Early development"))
    lines = pypi.splitlines()
    assert Counter(kept) - Counter(lines) == Counter([row, warning, ""])  # "": after the warning
    # The badge's row keeps its other badges, in order and one space apart.
    badge = re.search(r" \[!\[Status: Early Development\]\S*", row)
    assert badge
    assert list((Counter(lines) - Counter(kept)).elements()) == [row.replace(badge.group(0), "")]
    assert "early development" not in pypi.lower()


def _main(pypi_readme, monkeypatch, tmp_path, readme, pypi, *args):
    """The script's exit code, run on these two files as a checkout with core.autocrlf=true
    has them (CRLF), in tmp_path instead of the repo."""
    (tmp_path / "README.md").write_text(readme, encoding="utf-8", newline="\r\n")
    (tmp_path / "PYPI.md").write_text(pypi, encoding="utf-8", newline="\r\n")
    monkeypatch.setattr(pypi_readme, "ROOT", tmp_path)
    monkeypatch.setattr(sys, "argv", ["pypi_readme.py", *args])
    try:
        return pypi_readme.main()
    except SystemExit as stopped:
        return stopped.code


# Each part the script takes out, as the README has it, and a rename that hides it.
_PARTS = [("[![Status: Early Development]", "[![Status: Early]"),
          ("> ⚠️ **Early development", "> ⚠️ **Early days"),
          ("## Status\n", "## State\n"),
          ("## Documentation\n", "## Docs\n")]


@pytest.mark.parametrize("times", [0, 2])
@pytest.mark.parametrize("part, renamed", _PARTS, ids=["badge", "warning", "status", "docs"])
def test_the_script_writes_nothing_unless_each_part_is_there_once(
        pypi_readme, monkeypatch, tmp_path, part, renamed, times):
    readme = _README.read_text(encoding="utf-8")
    assert readme.count(part) == 1
    if times == 0:
        readme = readme.replace(part, renamed)
    else:  # the paragraph that holds it, twice
        start = readme.rfind("\n", 0, readme.index(part)) + 1
        end = readme.index("\n\n", start) + 2
        readme = readme[:end] + readme[start:end] + readme[end:]
    assert _main(pypi_readme, monkeypatch, tmp_path, readme, "as it was\n") not in (0, None)
    assert (tmp_path / "PYPI.md").read_text(encoding="utf-8") == "as it was\n"


def test_the_script_writes_nothing_unless_license_ends_the_two_sections(
        pypi_readme, monkeypatch, tmp_path):
    readme = _README.read_text(encoding="utf-8")
    assert readme.count("\n## License\n") == 1
    readme = readme.replace("\n## License\n", "\n## Licence\n")
    assert _main(pypi_readme, monkeypatch, tmp_path, readme, "as it was\n") not in (0, None)
    assert (tmp_path / "PYPI.md").read_text(encoding="utf-8") == "as it was\n"


def test_check_passes_only_an_up_to_date_pypi_md_and_the_default_writes_it(
        pypi_readme, monkeypatch, tmp_path):
    readme = _README.read_text(encoding="utf-8")
    want = pypi_readme.pypi_text(readme)
    assert _main(pypi_readme, monkeypatch, tmp_path, readme, want, "--check") == 0
    assert _main(pypi_readme, monkeypatch, tmp_path, readme, "as it was\n", "--check") not in (
        0, None)
    assert (tmp_path / "PYPI.md").read_text(encoding="utf-8") == "as it was\n"
    assert _main(pypi_readme, monkeypatch, tmp_path, readme, "as it was\n") == 0
    assert (tmp_path / "PYPI.md").read_bytes() == want.encode("utf-8")


def test_pyproject_makes_pypi_md_the_description_and_ships_it():
    with open(ROOT / "pyproject.toml", "rb") as f:
        project = tomllib.load(f)
    assert project["project"]["readme"] == "PYPI.md"
    assert "/PYPI.md" in project["tool"]["hatch"]["build"]["targets"]["sdist"]["include"]
