"""Jail unit suite for workspace.py (M1 acceptance criterion 10). No Docker required."""
import os

import pytest

from vestibule.workspace import WorkspacePathError, read_workspace_entry, resolve_in_workspace


@pytest.fixture()
def ws(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "hello.txt").write_text("hello world", encoding="utf-8")
    (tmp_path / "sub" / "nested.txt").write_text("nested", encoding="utf-8")
    return tmp_path


REJECTED = [
    # traversal
    "..",
    "../x",
    "a/../b",
    "sub/..",
    # absolute (POSIX and backslash forms)
    "/etc/passwd",
    "\\windows\\system32",
    # drive letters (absolute and drive-relative) — the ':' rule
    "C:\\secrets.txt",
    "C:secrets.txt",
    "c:/x",
    # NTFS alternate data stream
    "hello.txt:hidden",
    # UNC / device prefixes
    "//server/share/x",
    "\\\\server\\share\\x",
    "\\\\?\\C:\\x",
    "\\\\.\\pipe\\x",
    # NUL byte
    "a\x00b",
    # empty component
    "a//b",
    # Windows reserved device names (bare and with extension)
    "CON",
    "con.txt",
    "COM1",
    "lpt3.log",
    "NUL",
    # trailing dots / spaces (Windows name-normalization tricks)
    "trailingdot.",
    "trailing ",
    "sub/bad. /x",
]


@pytest.mark.parametrize("bad", REJECTED)
def test_jail_rejects(ws, bad):
    with pytest.raises(WorkspacePathError):
        resolve_in_workspace(ws, bad)


def test_root_listing(ws):
    out = read_workspace_entry(ws, ".", 20_000)
    assert "hello.txt" in out
    assert "sub" in out


def test_read_file(ws):
    assert read_workspace_entry(ws, "hello.txt", 20_000) == "hello world"


def test_nested_read_and_backslash_separator(ws):
    assert read_workspace_entry(ws, "sub/nested.txt", 20_000) == "nested"
    assert read_workspace_entry(ws, "sub\\nested.txt", 20_000) == "nested"


def test_single_dot_components_are_harmless(ws):
    assert read_workspace_entry(ws, "./hello.txt", 20_000) == "hello world"


def test_missing_is_content_not_error(ws):
    assert read_workspace_entry(ws, "nope.txt", 20_000).startswith("Not found:")


def test_missing_intermediate_is_content_not_error(ws):
    assert read_workspace_entry(ws, "no/such/dir.txt", 20_000).startswith("Not found:")


def test_file_truncation(ws):
    (ws / "big.txt").write_text("x" * 100, encoding="utf-8")
    out = read_workspace_entry(ws, "big.txt", 10)
    assert out.startswith("x" * 10)
    assert "truncated" in out


def test_directory_listing_entry_cap(ws):
    many = ws / "many"
    many.mkdir()
    for i in range(205):
        (many / f"f{i:03}.txt").write_text("x", encoding="utf-8")
    out = read_workspace_entry(ws, "many", 20_000)
    assert "more entries not shown" in out


def _symlink_or_skip(target, link, is_dir=False):
    try:
        os.symlink(str(target), str(link), target_is_directory=is_dir)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation not permitted on this host")


def test_terminal_symlink_refused(ws):
    _symlink_or_skip(ws / "hello.txt", ws / "link.txt")
    with pytest.raises(WorkspacePathError):
        read_workspace_entry(ws, "link.txt", 20_000)


def test_symlinked_dir_in_chain_refused(ws, tmp_path):
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir(exist_ok=True)
    (outside / "secret.txt").write_text("secret", encoding="utf-8")
    _symlink_or_skip(outside, ws / "esc", is_dir=True)
    with pytest.raises(WorkspacePathError):
        read_workspace_entry(ws, "esc/secret.txt", 20_000)


def test_symlink_shown_in_listing_but_not_followed(ws):
    _symlink_or_skip(ws / "hello.txt", ws / "link.txt")
    out = read_workspace_entry(ws, ".", 20_000)
    assert "symlink" in out
