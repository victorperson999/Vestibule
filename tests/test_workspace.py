"""Jail unit suite for workspace.py (M1 acceptance criterion 10). No Docker required."""
import os
import stat
import sys
import threading

import pytest

from vestibule import workspace
from vestibule.workspace import WorkspacePathError, read_workspace_entry


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
        read_workspace_entry(ws, bad, 20_000)


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


# --- A guest changing the workspace while the tool reads it (M4 step 4 fix) -----------------
#
# The module must open each name once, from the folder it already holds open. These tests swap a
# name for something else just before every filesystem call the module makes, one call at a time.

MARK = "MARKER-OF-THE-OTHER-FOLDER"


class _Calls:
    """Every call the module makes through a watched namespace, and a swap to run before one."""

    def __init__(self):
        self.log = []
        self.at = 0
        self.swap = None
        self.fired = False
        self.taken = 0  # entries taken from a listing's `scandir` (POSIX)
        self.entry_stats = 0  # `stat` asked of those entries themselves
        self.after_scan = None  # run when the listing is done with its `scandir`


class _Taken:
    """Stands in for the iterator `os.scandir` returns: counts the entries taken from it."""

    def __init__(self, calls, it):
        self._calls = calls
        self._it = it

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._it.close()
        if self._calls.after_scan is not None:
            self._calls.after_scan()

    def __iter__(self):
        return self

    def __next__(self):
        entry = next(self._it)
        self._calls.taken += 1
        return _Entry(self._calls, entry)


class _Entry:
    """One entry taken from a `_Taken`; counts the `stat` asked of it."""

    def __init__(self, calls, entry):
        self._calls = calls
        self._entry = entry

    def stat(self, **kwargs):
        self._calls.entry_stats += 1
        return self._entry.stat(**kwargs)

    def __getattr__(self, name):
        return getattr(self._entry, name)


class _Watched:
    """Stands in for the module's `os` (or one of its ctypes libraries) and counts its calls."""

    def __init__(self, calls, real):
        self._calls = calls
        self._real = real

    def __getattr__(self, name):
        attr = getattr(self._real, name)
        if not callable(attr) or isinstance(attr, type):
            return attr
        calls = self._calls

        def counted(*args, **kwargs):
            entry = [name, args, kwargs, None]
            calls.log.append(entry)
            if len(calls.log) == calls.at:
                calls.fired = True
                calls.swap()
            try:
                entry[3] = attr(*args, **kwargs)
            except Exception as e:
                entry[3] = e
                raise
            return _Taken(calls, entry[3]) if name == "scandir" else entry[3]

        return counted


def _watch(monkeypatch):
    calls = _Calls()
    for name in ("os", "_k32", "_nt"):  # the last two: the Windows libraries, where it has them
        if hasattr(workspace, name):
            real = getattr(workspace, name)
            real = getattr(real, "_real", real)  # a second sweep in one test: don't nest
            monkeypatch.setattr(workspace, name, _Watched(calls, real))
    return calls


def _relative_opens(calls):
    """Call numbers of the opens made relative to an open folder: one per path component."""
    return [
        number
        for number, (name, args, kwargs, _) in enumerate(calls.log, 1)
        if (name == "NtOpenFile" and _nt_name_length(args)) or (name == "open" and "dir_fd" in kwargs)
    ]


def _nt_name_length(args):
    """The length of the name an NtOpenFile call asked for; 0 reopens the folder's own handle."""
    return args[2]._obj.ObjectName.contents.Length


def _left_open(calls):
    """Descriptors and handles the logged calls opened, minus those they closed."""
    count = 0
    for name, _, _, result in calls.log:
        if name == "open" and isinstance(result, int):
            count += 1
        elif name == "CreateFileW" and result != workspace._INVALID_HANDLE:
            count += 1
        elif name == "NtOpenFile" and isinstance(result, int) and result >= 0:
            count += 1
        elif name in ("close", "CloseHandle") and not isinstance(result, Exception):
            count -= 1
    return count


def _link_dir(target, link):
    if sys.platform == "win32":
        import _winapi

        _winapi.CreateJunction(str(target), str(link))  # needs no privilege, unlike a symlink
    else:
        os.symlink(target, link)


def _tree(base):
    """A workspace, and beside it a folder whose name starts with the workspace's."""
    ws = base / "ws"
    for folder in (ws / "sub", ws / "a" / "b"):
        folder.mkdir(parents=True)
    (ws / "hello.txt").write_text("hello world", encoding="utf-8")
    (ws / "sub" / "nested.txt").write_text("nested", encoding="utf-8")
    (ws / "a" / "b" / "c.txt").write_text("c", encoding="utf-8")
    for other in (ws / "other", base / "ws-outside"):
        (other / "b").mkdir(parents=True)
        for name in ("nested.txt", "c.txt", "b/c.txt", "only-there.txt"):
            (other / name).write_text(MARK, encoding="utf-8")
    return ws


def _read(ws, path):
    try:
        return read_workspace_entry(ws, path, 20_000)
    except WorkspacePathError as e:
        return e


def _sweep(monkeypatch, tmp_path, path, swap, read=_read):
    """Read `path` once per call point, with `swap(ws)` run just before that call.

    Returns what an undisturbed read gives, the outcome at each call point, and the call numbers
    of the undisturbed read's relative opens.
    """
    calls = _watch(monkeypatch)
    baseline = read(_tree(tmp_path / "k0"), path)
    opens = _relative_opens(calls)
    assert opens and _left_open(calls) == 0
    outcomes = []
    while True:
        ws = _tree(tmp_path / f"k{len(outcomes) + 1}")
        calls.log.clear()
        calls.at, calls.fired, calls.swap = len(outcomes) + 1, False, lambda ws=ws: swap(ws)
        outcome = read(ws, path)
        if not calls.fired:
            return baseline, outcomes, opens
        outcomes.append(outcome)
        assert _left_open(calls) == 0, f"call point {len(outcomes)} left something open"


def _check(baseline, outcomes, opens, components, swapped):
    """`swapped` is the index of the component that the swap replaced."""
    for k, outcome in enumerate(outcomes, 1):
        for word in (MARK, "only-there"):
            assert word not in str(outcome), f"call point {k} of {len(outcomes)} gave {word!r}"
    # The helper itself: it must have seen the opens, or the lines below prove nothing.
    assert len(opens) == components, f"saw {len(opens)} relative opens: {opens}"
    edge = opens[swapped]
    assert len(outcomes) > edge
    for k, outcome in enumerate(outcomes, 1):
        if k <= edge:  # the swap came before this component was opened
            assert isinstance(outcome, WorkspacePathError), f"call point {k}: {outcome!r}"
        else:  # it came after: the folder that was opened is still the real one
            assert outcome == baseline, f"call point {k}: {outcome!r}"


@pytest.mark.parametrize("target", ["ws-outside", "ws/other"])
@pytest.mark.parametrize(
    "path, swapped",
    [("sub/nested.txt", "sub"), ("sub", "sub"), ("a/b/c.txt", "a"), ("a/b/c.txt", "a/b")],
)
def test_a_folder_swapped_at_any_moment_never_leaks(monkeypatch, tmp_path, path, swapped, target):
    def swap(ws):
        try:
            os.rename(ws / swapped, ws / "gone")
        except PermissionError:
            if path == swapped:  # the folder being listed: its own rename must not be blocked
                raise
            return  # Windows refuses to rename a folder above an open handle: no swap happened
        _link_dir(ws.parent / target, ws / swapped)

    baseline, outcomes, opens = _sweep(monkeypatch, tmp_path, path, swap)
    _check(baseline, outcomes, opens, path.count("/") + 1, swapped.count("/"))


@pytest.mark.parametrize("target", ["ws-outside/nested.txt", "ws/other/nested.txt"])
def test_a_file_swapped_for_a_link_is_refused(monkeypatch, tmp_path, target):
    _symlink_or_skip(tmp_path, tmp_path / "may-i-link")

    def swap(ws):
        os.rename(ws / "hello.txt", ws / "gone")  # allowed even while the tool has it open
        os.symlink(ws.parent / target, ws / "hello.txt")

    baseline, outcomes, opens = _sweep(monkeypatch, tmp_path, "hello.txt", swap)
    assert baseline == "hello world"
    _check(baseline, outcomes, opens, 1, 0)


def _bounded(call):
    """Run `call` in a thread. Returns what it returned or raised, or None if it is still
    blocked after 5 s (a blocked open can't be interrupted from here, only released)."""
    box = []

    def run():
        try:
            box.append(call())
        except Exception as e:  # the outcome is what the test judges
            box.append(e)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(5)
    return (box[0], thread) if box else (None, thread)


@pytest.mark.skipif(sys.platform == "win32", reason="Windows has no FIFOs")
@pytest.mark.parametrize("held", [False, True], ids=["nobody-has-it-open", "held-open-silent"])
def test_a_file_swapped_for_a_fifo_cant_hang_the_read(monkeypatch, tmp_path, held):
    holders = []

    def swap(ws):
        os.rename(ws / "hello.txt", ws / "gone")
        os.mkfifo(ws / "hello.txt")
        if held:  # a guest that keeps it open for reading and writing, and writes nothing
            holders.append(os.open(ws / "hello.txt", os.O_RDWR | os.O_NONBLOCK))

    def read(ws, path):
        outcome, thread = _bounded(lambda: _read(ws, path))
        blocked = thread.is_alive()
        if blocked and stat.S_ISFIFO(os.lstat(ws / path).st_mode):
            try:  # let a reader that is stuck in open() go
                holders.append(os.open(ws / path, os.O_WRONLY | os.O_NONBLOCK))
            except OSError:
                pass
        while holders:  # closing the last writer ends a reader that is stuck in read()
            os.close(holders.pop())
        thread.join(5)
        assert not blocked, "read_workspace was still blocked after 5 s"
        return outcome

    baseline, outcomes, opens = _sweep(monkeypatch, tmp_path, "hello.txt", swap, read)
    assert baseline == "hello world"
    _check(baseline, outcomes, opens, 1, 0)


def test_a_swap_tells_nothing_about_the_other_folder(monkeypatch, tmp_path):
    """`sub/name/z` with `sub` swapped for a link to a folder where `name` is a file, is a
    link, or isn't there: the caller must get the same answers, so it can't tell which."""

    def answers(kind):
        def swap(ws):
            outside = ws.parent / "ws-outside"
            if kind == "file":
                (outside / "name").write_text("x", encoding="utf-8")
            elif kind == "link":
                _link_dir(outside / "b", outside / "name")
            os.rename(ws / "sub", ws / "gone")
            _link_dir(outside, ws / "sub")

        _, outcomes, _ = _sweep(monkeypatch, tmp_path / kind, "sub/name/z", swap)
        assert len(outcomes) > 2
        return [str(outcome) for outcome in outcomes]

    assert answers("file") == answers("link") == answers("absent")


def test_nothing_is_opened_for_reading_before_it_is_known_what_it_is(monkeypatch, ws):
    """A guest can put a FIFO anywhere, and opening one for reading can block. So every name is
    opened in a way that can't: without blocking on POSIX, for its attributes only on Windows."""
    calls = _watch(monkeypatch)
    for path in ("hello.txt", "sub", "sub/nested.txt"):
        read_workspace_entry(ws, path, 20_000)
    opens = [(name, args) for name, args, _, _ in calls.log if name in ("open", "NtOpenFile")]
    assert len(opens) >= 4
    reading = 0x80000000 | 0x1  # GENERIC_READ, FILE_READ_DATA
    for name, args in opens:
        if name == "open":
            assert args[1] & os.O_NONBLOCK, args
        elif _nt_name_length(args):
            assert not args[1] & reading, hex(args[1])

    # A link as the last name, to a folder and to a file (a guest's FIFO looks like the second
    # to Windows): refused before anything at all is opened for reading.
    _link_dir(ws / "sub", ws / "inside")
    links = ["inside"]
    try:
        os.symlink(ws / "hello.txt", ws / "link.txt")
        links.append("link.txt")
    except OSError:
        pass  # Windows without the symlink privilege: the folder link is still checked
    for link in links:
        calls.log.clear()
        with pytest.raises(WorkspacePathError):
            read_workspace_entry(ws, link, 20_000)
        assert [e for e in calls.log if e[0] in ("open", "NtOpenFile")]
        assert not [e for e in calls.log if e[0] == "NtOpenFile" and e[1][1] & reading], link


def test_a_name_that_isnt_text_is_listed_readably(ws):
    """A guest can make a file whose name isn't valid text. The listing goes to the model as
    JSON, which can't carry such a name, so it must not reach it as it is."""
    try:
        if sys.platform == "win32":
            (ws / f"odd{chr(0xD800)}.txt").write_text("x", encoding="utf-8")
        else:
            with open(os.fsencode(ws) + b"/odd" + bytes([0xFF]) + b".txt", "w") as f:
                f.write("x")
    except OSError:
        pytest.skip("this filesystem only takes names that are valid text")
    listing = read_workspace_entry(ws, ".", 20_000)
    listing.encode("utf-8")  # raises on a name passed through as it is
    assert f"  odd{chr(0xFFFD)}.txt" in listing


def test_a_link_to_a_workspace_folder_is_refused(ws):
    _link_dir(ws / "sub", ws / "inside")
    with pytest.raises(WorkspacePathError, match="symlink or reparse point"):
        read_workspace_entry(ws, "inside/nested.txt", 20_000)
    with pytest.raises(WorkspacePathError, match="symlink or reparse point"):
        read_workspace_entry(ws, "inside", 20_000)


def test_a_path_through_a_file_gives_no_content(ws):
    with pytest.raises(WorkspacePathError, match="hello.txt"):
        read_workspace_entry(ws, "hello.txt/x", 20_000)
    with pytest.raises(WorkspacePathError, match="nested.txt"):
        read_workspace_entry(ws, "sub/nested.txt/x/y", 20_000)


@pytest.mark.skipif(sys.platform == "win32", reason="Windows has no FIFOs")
def test_a_fifo_is_refused(ws):
    os.mkfifo(ws / "pipe")
    outcome, thread = _bounded(lambda: _read(ws, "pipe"))
    if thread.is_alive():
        os.close(os.open(ws / "pipe", os.O_WRONLY | os.O_NONBLOCK))
        thread.join(5)
        pytest.fail("read_workspace was still blocked after 5 s")
    assert isinstance(outcome, WorkspacePathError)
    assert "neither a regular file nor a directory" in str(outcome)


@pytest.mark.skipif(sys.platform == "win32", reason="chmod doesn't take access away on Windows")
def test_an_unreadable_file_or_folder_is_refused_by_name(ws):
    if os.geteuid() == 0:
        pytest.skip("root can read anything")
    (ws / "locked.txt").write_text("x", encoding="utf-8")
    os.chmod(ws / "locked.txt", 0)
    os.chmod(ws / "sub", 0o111)  # search permission only
    try:
        with pytest.raises(WorkspacePathError, match="cannot open 'locked.txt': Permission denied"):
            read_workspace_entry(ws, "locked.txt", 20_000)
        with pytest.raises(WorkspacePathError, match="cannot open 'sub': Permission denied"):
            read_workspace_entry(ws, "sub/nested.txt", 20_000)
    finally:
        os.chmod(ws / "sub", 0o755)


def test_listing_lines_keep_their_format(ws):
    fmt = ws / "fmt"
    (fmt / "d").mkdir(parents=True)
    (fmt / "f.txt").write_text("12345", encoding="utf-8")
    _link_dir(ws / "sub", fmt / "l")
    d, f, link = read_workspace_entry(ws, "fmt", 20_000).split("\n")
    assert f == "file               5  f.txt"
    assert d.startswith("dir     ") and d.endswith("  d")
    assert link.startswith("symlink ") and link.endswith("  l")


def test_a_big_directory_is_counted_exactly(ws):
    big = ws / "big"
    big.mkdir()
    # 60 characters: 200 lines fit the 20,000 cap, and 400 names still span two Windows buffers.
    odd = "0-naïve-日本-" + "n" * 49
    for name in [odd] + [f"{i:03}-" + "n" * 56 for i in range(399)]:
        (big / name).write_text("x", encoding="utf-8")
    lines = read_workspace_entry(ws, "big", 20_000).split("\n")
    assert len(lines) == 201
    assert lines[0] == f"file               1  {odd}"
    assert lines[-1] == "...[200 more entries not shown]"


def test_an_empty_folder_and_the_other_case(ws):
    (ws / "empty").mkdir()
    assert read_workspace_entry(ws, "empty", 20_000) == "(empty directory)"
    other_case = read_workspace_entry(ws, "SUB/NESTED.TXT", 20_000)
    if (ws / "SUB" / "NESTED.TXT").exists():  # a filesystem that ignores case
        assert other_case == "nested"
    else:
        assert other_case.startswith("Not found:")


def test_a_name_too_long_to_exist_gives_no_content(ws):
    # Windows passes a name's length in 16 bits: 32,772 characters must not wrap round to 4.
    (ws / "aaaa").write_text("short name", encoding="utf-8")
    outcome = _read(ws, "a" * (32768 + 4))
    assert isinstance(outcome, WorkspacePathError) or outcome.startswith("Not found:")


# --- What a listing shows, and how much of a folder it reads (M4 step 4 follow-ups) ----------

BS = "\\"
NOTE = "...[names are shown with backslash escapes for control characters]"


def _line(name, size=1):
    return f"{'file':<8}{size:>12}  {name}"


def _files(folder, names):
    folder.mkdir()
    for name in names:
        (folder / name).write_text("x", encoding="utf-8")


@pytest.mark.skipif(sys.platform == "win32", reason="NTFS can't hold a line break in a name")
def test_a_line_break_in_a_name_cant_forge_an_entry(ws):
    forged = _line("secrets.txt", 42)
    _files(ws / "forge", ["a.txt\n" + forged, "b.txt"])
    lines = read_workspace_entry(ws, "forge", 20_000).split("\n")
    assert lines == [_line(f"a.txt{BS}x0a{forged}"), _line("b.txt"), NOTE]


def test_escapes_are_exact(ws):
    shown = {
        f"rlo{chr(0x202E)}txt.exe": f"rlo{BS}u202etxt.exe",  # a right-to-left override
        f"ls{chr(0x2028)}x": f"ls{BS}u2028x",  # a line separator
        f"nel{chr(0x85)}x": f"nel{BS}x85x",  # a control character
        f"tag{chr(0xE0001)}": f"tag{BS}U000e0001",  # a format character past 0xFFFF
    }
    _files(ws / "odd", shown)
    lines = read_workspace_entry(ws, "odd", 20_000).split("\n")
    assert lines == [_line(shown[name]) for name in sorted(shown)] + [NOTE]

    plain = ["a.txt", "b c.txt", "naïve-日本.md"]
    _files(ws / "plain", plain)
    assert read_workspace_entry(ws, "plain", 20_000) == "\n".join(_line(n) for n in plain)

    if sys.platform != "win32":  # a backslash is a separator in a Windows name
        # Doubled, so a name that reads like an escape can't pass for the name it imitates.
        _files(ws / "slash", [f"a.txt{BS}x0ab"])
        listing = read_workspace_entry(ws, "slash", 20_000)
        assert listing.split("\n") == [_line(f"a.txt{BS}{BS}x0ab"), NOTE]


def test_a_listing_fits_the_cap(ws):
    names = [f"a{i:02}" + "é" * (i * 7 % 40) for i in range(12)]  # é: one character, two bytes
    names += ["b" + "y" * 150] + [f"c{i}" for i in range(8)]
    _files(ws / "mixed", names)
    lines = [_line(name) for name in sorted(names)]
    cap = 600
    k = max(k for k in range(len(lines) + 1) if len("\n".join(lines[:k])) <= cap)
    # The first line that doesn't fit is the long one, and the line after it would.
    assert lines[k] == _line(names[12])
    assert len("\n".join(lines[:k] + lines[k + 1 : k + 2])) <= cap
    listing = read_workspace_entry(ws, "mixed", cap).split("\n")
    assert listing[:-1] == lines[:k] and len("\n".join(listing[:-1])) <= cap
    assert listing[-1] == f"...[{len(names) - k} more entries not shown]"
    # So small a cap that no line fits: the footer alone.
    assert read_workspace_entry(ws, "mixed", 10) == f"...[{len(names)} more entries not shown]"


def _reads(calls):
    """How much of a folder a listing read: on POSIX, the entries taken from `scandir`; on Windows,
    where one call fills a buffer with many records, the calls."""
    if sys.platform != "win32":
        return calls.taken
    classes = (workspace._FILE_FULL_DIR_INFO, workspace._FILE_FULL_DIR_RESTART_INFO)
    return sum(1 for name, args, _, _ in calls.log
               if name == "GetFileInformationByHandleEx" and args[1] in classes)


def test_a_folder_is_read_up_to_the_scan_cap(monkeypatch, ws):
    cap, huge = 300, 1_000_000  # `huge`: no character cap, this test is about the scan
    # raising=False: a lister without the cap must fail on what it reads, not on this line.
    monkeypatch.setattr(workspace, "_MAX_SCAN", cap, raising=False)
    # 120 characters: a Windows buffer holds about 210 of these names, not 740.
    names = [f"{i:04}-" + "n" * 115 for i in range(1500)]

    # Exactly at the cap: the listing is today's. On Windows `.` and `..` must not count.
    _files(ws / "edge", names[:cap])
    footer = read_workspace_entry(ws, "edge", huge).split("\n")[-1]
    assert footer == f"...[{cap - 200} more entries not shown]"
    (ws / "edge" / names[cap]).write_text("x", encoding="utf-8")
    footer = read_workspace_entry(ws, "edge", huge).split("\n")[-1]
    assert footer == (f"...[{cap + 1 - 200}+ more entries not shown: the folder holds over {cap}, "
                      f"and only the first {cap} read were sorted]")

    _files(ws / "big", names)
    calls = _watch(monkeypatch)
    read_workspace_entry(ws, "big", huge)
    capped = _reads(calls)
    monkeypatch.setattr(workspace, "_MAX_SCAN", 10**9)
    calls.log.clear()
    calls.taken = 0
    read_workspace_entry(ws, "big", huge)
    whole = _reads(calls)
    if sys.platform == "win32":  # several buffers, and the cap stops in the second
        assert whole >= 6 and 2 * capped < whole, (capped, whole)
    else:
        assert (capped, whole) == (cap + 1, len(names))


def _lookups(calls):
    """The `stat` calls a POSIX listing made: the walk's, and the entries'."""
    return calls.entry_stats + sum(1 for entry in calls.log if entry[0] in ("stat", "lstat"))


@pytest.mark.skipif(sys.platform == "win32", reason="a Windows record carries its entry's size")
def test_sizes_are_asked_only_for_entries_shown(monkeypatch, ws):
    _files(ws / "many", [f"{i:04}.txt" for i in range(1000)])
    calls = _watch(monkeypatch)
    lines = read_workspace_entry(ws, "many", 20_000).split("\n")
    assert len(lines) == 201 and calls.taken == 1000  # the stand-in saw the listing
    stats = _lookups(calls)
    assert stats == 200 + 1, stats  # the entries shown, and the walk's one name


@pytest.mark.skipif(sys.platform == "win32", reason="a Windows record carries its entry's size")
def test_entries_gone_since_the_scan(monkeypatch, ws):
    names = [f"{i:04}.txt" for i in range(1000)]
    for folder in ("some", "all", "few"):
        _files(ws / folder, names[:3] if folder == "few" else names)
    calls = _watch(monkeypatch)

    def listing(folder, going):
        def remove():
            for name in going:
                os.unlink(ws / folder / name)

        calls.log.clear()
        calls.entry_stats = 0
        calls.after_scan = remove
        return read_workspace_entry(ws, folder, 20_000)

    # All of them go between the scan and the lookups: no more than 200 lookups all the same.
    text = listing("all", names)
    assert _lookups(calls) == 200 + 1  # the first 200 names, and the walk's one
    assert text == "...[800 more entries not shown]"
    # A tenth of the first 200 go: their places stay empty, and the rest is counted.
    gone = names[:200:10]
    kept = [_line(name) for name in names[:200] if name not in gone]
    assert listing("some", gone).split("\n") == kept + ["...[800 more entries not shown]"]
    assert _lookups(calls) == 200 + 1
    # A folder with no entry left is empty.
    assert listing("few", names[:3]) == "(empty directory)"


@pytest.mark.skipif(sys.platform == "win32", reason="chmod doesn't take access away on Windows")
def test_an_entry_that_cant_be_looked_up_is_shown(monkeypatch, ws):
    if os.geteuid() == 0:
        pytest.skip("root can read anything")
    names = [f"{i:04}.txt" for i in range(300)]
    _files(ws / "unsearchable", names)
    os.chmod(ws / "unsearchable", 0o444)  # its names can be read, nothing in it looked up
    try:
        calls = _watch(monkeypatch)
        lines = read_workspace_entry(ws, "unsearchable", 20_000).split("\n")
    finally:
        os.chmod(ws / "unsearchable", 0o755)
    unknown = [f"{'unknown':<8}{'?':>12}  {name}" for name in names[:200]]
    assert lines == unknown + ["...[100 more entries not shown]"]
    assert _lookups(calls) == 200 + 1
