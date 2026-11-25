"""Path jail + read/list logic for the `read_workspace` tool.

Contract: docs/plans/M1/M1-container-backend.md §6 (review findings 8/9), as amended by
docs/plans/M4/M4-step4-workspace-race.md. All string validation happens BEFORE touching
the filesystem. The path is then opened one component at a time, each name relative to
the folder already held open, and a symlink or reparse point is refused, never followed
(guest-planted symlinks are the headline attack). Lives in its own module so the jail is
unit-testable without Docker.

Nothing is checked by name and then opened by name. A guest that swaps a folder for a link
while this runs gets a refusal, or the folder that was really opened, and no path outside
the workspace is ever asked about. What a concurrently running guest can still do: change
a file's content while it is read, and make its own entries unreadable to this tool.

That holds for a workspace on a local drive. On Windows with the workspace on a network or
WSL share, the far side looks every name up again, so the walk is by name there: no outside
content came back in tests (Windows doesn't follow that share's links), but a guest that
swaps a file for a FIFO at the right moment can hold one call for about 10 s.
"""
from __future__ import annotations

import os
import stat
import sys
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar

_MAX_DIR_ENTRIES = 200
# docs/plans/M4/M4-step4-followups.md §2.3: a guest can make a folder as large as the disk
# allows, so a listing reads this many entries of it at most.
_MAX_SCAN = 10_000
# §2.1: the audit command's set (M3 R1): controls, format characters, surrogates, line and
# paragraph separators. Its own copy: the server must not import audit_cli, which prints.
# No surrogate reaches it today, since both listers decode with "replace"; Cs keeps a listing
# valid if that ever changes (§12 D3).
_ESCAPED = ("Cc", "Cf", "Cs", "Zl", "Zp")
_ESCAPE_NOTE = "...[names are shown with backslash escapes for control characters]"
_LINK_REFUSED = "path traverses a symlink or reparse point; refused"

_WINDOWS_RESERVED = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{i}" for i in range(1, 10)}
    | {f"lpt{i}" for i in range(1, 10)}
)


class WorkspacePathError(Exception):
    """A requested path was refused by the jail. The message is agent-legible."""


@dataclass(frozen=True)
class WorkspaceEntry:
    name: str            # raw UTF-8 (errors="replace") entry name for path navigation
    display_name: str    # _shown(name): Cc/Cf/Cs/Zl/Zp escaped, '\\' doubled (bidi/control safe)
    kind: str            # "dir" | "file" | "symlink" | "other" | "unknown"
    size: int | None     # None when stat failed (rendered as "?" in text listings)


@dataclass(frozen=True)
class WorkspaceDir:
    entries: tuple[WorkspaceEntry, ...]  # up to _MAX_DIR_ENTRIES (200), sorted by name
    total_scanned: int                   # entries scanned before [:200], <= _MAX_SCAN (10_000)
    gone: int                            # entries in [:200] that vanished before cutoff
    over_scan: bool                      # True if directory held > _MAX_SCAN entries

    @property
    def hidden(self) -> int:
        # Matches workspace.py:144 (`over`: the one entry read to learn there were > _MAX_SCAN).
        return self.total_scanned + int(self.over_scan) - len(self.entries) - self.gone


@dataclass(frozen=True)
class WorkspaceFile:
    content: str                         # UTF-8 (errors="replace"), capped at max_bytes
    truncated: bool                      # True if file exceeded max_bytes
    max_bytes: int


@dataclass(frozen=True)
class WorkspaceMissing:
    root_exists: bool                    # False when workspace root does not exist
    error: str | None = None             # underlying FileNotFoundError text when root_exists=False


def split_relpath(user_path: str) -> list[str]:
    """Validate a user-supplied workspace-relative path; return its components.

    Purely lexical — no filesystem access. '.' (or '') means the workspace root
    and returns []. Raises WorkspacePathError on anything absolute, drive-lettered,
    ADS-coloned, UNC/device-prefixed, NUL-containing, '..'-containing, or using
    Windows reserved device names / trailing dots or spaces.
    """
    if "\x00" in user_path:
        raise WorkspacePathError("path contains a NUL byte")
    if ":" in user_path:
        # Kills drive letters (C:\, C:relative) and NTFS alternate data streams
        # (file.txt:stream). Legitimate colon-filenames are accepted collateral.
        raise WorkspacePathError("path must not contain ':'")
    norm = user_path.replace("\\", "/")
    if norm.startswith("/"):
        # Covers POSIX-absolute, UNC (//server, \\server) and device (\\?\, \\.\)
        # forms after backslash normalization.
        raise WorkspacePathError("path must be relative to the workspace root, not absolute")
    if norm in ("", "."):
        return []

    parts: list[str] = []
    for part in norm.split("/"):
        if part == "":
            raise WorkspacePathError("path contains an empty component")
        if part == ".":
            continue
        if part == "..":
            raise WorkspacePathError("path must not contain '..'")
        if part != part.rstrip(". "):
            raise WorkspacePathError(f"component {part!r} has trailing dots or spaces")
        if part.split(".", 1)[0].lower() in _WINDOWS_RESERVED:
            raise WorkspacePathError(f"component {part!r} is a reserved device name")
        parts.append(part)
    return parts


def _is_symlink_or_reparse(st: os.stat_result) -> bool:
    if stat.S_ISLNK(st.st_mode):
        return True
    # Windows: refuse ANY reparse point (symlinks, junctions, mount points).
    attrs = getattr(st, "st_file_attributes", 0)
    return bool(attrs & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def inspect_workspace_entry(
    root: Path, user_path: str, max_bytes: int,
) -> WorkspaceDir | WorkspaceFile | WorkspaceMissing:
    """Structured workspace inspection backed by the same path jail as `read_workspace_entry`.

    Raises WorkspacePathError for refused paths (including dangling symlink/reparse root);
    returns WorkspaceMissing when the root or target does not exist without creating anything.
    """
    parts = split_relpath(user_path)
    try:
        real_root = root.resolve(strict=True)
    except FileNotFoundError as e:
        try:
            is_link = _is_symlink_or_reparse(os.lstat(root))
        except OSError:
            is_link = False
        if is_link or root.is_symlink():
            raise WorkspacePathError(f"workspace root is not accessible: {e}") from e
        return WorkspaceMissing(root_exists=False, error=str(e))
    except OSError as e:
        raise WorkspacePathError(f"workspace root is not accessible: {e}") from e
    found = _inspect(str(real_root), parts, max_bytes, max_chars=None)
    return WorkspaceMissing(root_exists=True) if found is None else found


def read_workspace_entry(root: Path, user_path: str, max_bytes: int) -> str:
    """Directory -> listing, file -> content, missing -> 'Not found: …'.

    Raises WorkspacePathError for refused paths; never raises for missing ones.
    """
    parts = split_relpath(user_path)
    try:
        real_root = root.resolve(strict=True)
    except OSError as e:
        raise WorkspacePathError(f"workspace root is not accessible: {e}") from e
    # The root is the one name opened by its full path: a guest can't rename its own mount point.
    found = _inspect(str(real_root), parts, max_bytes, max_chars=max_bytes)
    if found is None:
        return f"Not found: {user_path or '.'}"
    if isinstance(found, WorkspaceFile):
        text = found.content
        if found.truncated:
            text += f"\n...[truncated: file exceeds {found.max_bytes} bytes]"
        return text
    if not found.total_scanned:
        return "(empty directory)"
    lines: list[str] = []
    escaped = False
    for entry in found.entries:
        size_str = "?" if entry.size is None else str(entry.size)
        lines.append(f"{entry.kind:<8}{size_str:>12}  {entry.display_name}")
        escaped |= entry.display_name != entry.name
    hidden = found.hidden
    if found.over_scan:
        lines.append(
            f"...[{hidden}+ more entries not shown: the folder holds over {_MAX_SCAN}, "
            f"and only the first {_MAX_SCAN} read were sorted]"
        )
    elif hidden:
        lines.append(f"...[{hidden} more entries not shown]")
    if escaped:
        lines.append(_ESCAPE_NOTE)
    return "\n".join(lines) or "(empty directory)"


_K = TypeVar("_K")


def _collect_dir(
    entries: list[tuple[str, _K]],
    describe: Callable[[_K], tuple[str, int | None] | None],
    max_chars: int | None,
    over: bool,
) -> WorkspaceDir:
    """Name order, up to the first 200 entries (optionally bounded by `max_chars` when rendered
    for text output). `describe` gives an entry's kind and size (or None size on stat failure),
    or None for one gone since the scan, whose place stays empty (§12 D1: no other entry is
    looked up). `over`: the folder holds more than was read."""
    if not entries:
        return WorkspaceDir(entries=(), total_scanned=0, gone=0, over_scan=over)
    entries.sort()
    kept: list[WorkspaceEntry] = []
    length = -1  # of the lines joined: the first has no line break before it
    gone = 0
    for name, key in entries[:_MAX_DIR_ENTRIES]:
        found = describe(key)
        if found is None:
            gone += 1
            continue
        kind, size = found
        display_name = _shown(name)
        if max_chars is not None:
            size_str = "?" if size is None else str(size)
            line = f"{kind:<8}{size_str:>12}  {display_name}"
            length += 1 + len(line)
            if length > max_chars:
                break
        kept.append(WorkspaceEntry(name=name, display_name=display_name, kind=kind, size=size))
    return WorkspaceDir(
        entries=tuple(kept), total_scanned=len(entries), gone=gone, over_scan=over,
    )


def _shown(name: str) -> str:
    """A name as one line (§2.1). Unlike the audit command's text, which is code full of `\\n`,
    a backslash is doubled: one in a listing always starts an escape, so a name of valid text
    can't imitate an escaped one (§12 D3)."""
    return "".join(
        "\\\\" if c == "\\" else _escape(c) if unicodedata.category(c) in _ESCAPED else c
        for c in name
    )


def _escape(c: str) -> str:
    n = ord(c)
    return f"\\x{n:02x}" if n < 0x100 else f"\\u{n:04x}" if n < 0x10000 else f"\\U{n:08x}"


def _read_file(fd: int, max_bytes: int) -> WorkspaceFile:
    with os.fdopen(fd, "rb", closefd=False) as f:
        data = f.read(max_bytes + 1)
    text = data[:max_bytes].decode("utf-8", errors="replace")
    return WorkspaceFile(content=text, truncated=len(data) > max_bytes, max_bytes=max_bytes)


if sys.platform != "win32":

    def _inspect(
        real_root: str, parts: list[str], max_bytes: int, *, max_chars: int | None,
    ) -> WorkspaceDir | WorkspaceFile | None:
        """Walk to the entry with descriptors only; None means a component is missing."""
        # O_NONBLOCK: a FIFO a guest put there must not hang the open. Nothing is read from
        # anything before fstat has said what it is.
        flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK
        fd = os.open(real_root, flags | os.O_DIRECTORY)
        try:
            for i, part in enumerate(parts):
                more = os.O_DIRECTORY if i < len(parts) - 1 else 0
                try:
                    # The stat is for the message only. O_NOFOLLOW is what refuses a link.
                    if stat.S_ISLNK(os.stat(part, dir_fd=fd, follow_symlinks=False).st_mode):
                        raise WorkspacePathError(_LINK_REFUSED)
                    nxt = os.open(part, flags | os.O_NOFOLLOW | more, dir_fd=fd)
                except FileNotFoundError:
                    return None
                except OSError as e:
                    raise WorkspacePathError(f"cannot open {part!r}: {e.strerror or e}") from e
                os.close(fd)
                fd = nxt
            mode = os.fstat(fd).st_mode
            if stat.S_ISDIR(mode):
                return _list_dir(fd, max_chars)
            if stat.S_ISREG(mode):
                return _read_file(fd, max_bytes)
            raise WorkspacePathError("path is neither a regular file nor a directory")
        finally:
            os.close(fd)

    def _list_dir(fd: int, max_chars: int | None) -> WorkspaceDir:
        """Names only: a size is asked for, by name in this folder, only for a line shown."""
        names: list[str] = []
        over = False
        with os.scandir(fd) as it:
            for entry in it:
                if len(names) == _MAX_SCAN:
                    over = True
                    break
                names.append(entry.name)

        def describe(name: str) -> tuple[str, int | None] | None:
            try:  # never follows a link
                st = os.stat(name, dir_fd=fd, follow_symlinks=False)
            except FileNotFoundError:  # gone since the scan: left out
                return None
            except OSError:  # there, but can't be looked up: a folder that isn't searchable
                return "unknown", None
            if _is_symlink_or_reparse(st):
                kind = "symlink"
            elif stat.S_ISDIR(st.st_mode):
                kind = "dir"
            elif stat.S_ISREG(st.st_mode):
                kind = "file"
            else:
                kind = "other"
            return kind, st.st_size

        # A name that isn't valid text can't go to the model as JSON.
        entries = [(os.fsencode(name).decode("utf-8", "replace"), name) for name in names]
        return _collect_dir(entries, describe, max_chars, over)

else:
    import ctypes
    import msvcrt
    import struct
    from ctypes import wintypes

    # Win32 can't open a name relative to an open folder; NtOpenFile, the call every Win32
    # open ends in, can. With FILE_OPEN_REPARSE_POINT a link is opened as itself, never followed.
    # Each name is opened for its attributes only, and reopened for reading, by an empty name,
    # once those say it is no reparse point. On a local drive the second open looks no name up
    # again. It is there for a workspace on a WSL share: a guest's FIFO is a reparse point, and
    # opening one for reading there blocks for about 10 s and leaves a reader waiting in WSL.

    class _UnicodeString(ctypes.Structure):
        _fields_ = [
            ("Length", wintypes.USHORT),
            ("MaximumLength", wintypes.USHORT),
            ("Buffer", wintypes.LPCWSTR),
        ]

    class _ObjectAttributes(ctypes.Structure):
        _fields_ = [
            ("Length", wintypes.ULONG),
            ("RootDirectory", wintypes.HANDLE),
            ("ObjectName", ctypes.POINTER(_UnicodeString)),
            ("Attributes", wintypes.ULONG),
            ("SecurityDescriptor", wintypes.LPVOID),
            ("SecurityQualityOfService", wintypes.LPVOID),
        ]

    class _IoStatusBlock(ctypes.Structure):
        _fields_ = [("Status", wintypes.LPVOID), ("Information", ctypes.c_size_t)]

    _nt = ctypes.WinDLL("ntdll")
    _nt.NtOpenFile.argtypes = [
        ctypes.POINTER(wintypes.HANDLE), wintypes.DWORD, ctypes.POINTER(_ObjectAttributes),
        ctypes.POINTER(_IoStatusBlock), wintypes.ULONG, wintypes.ULONG,
    ]
    _nt.NtOpenFile.restype = ctypes.c_long
    _nt.RtlNtStatusToDosError.argtypes = [ctypes.c_long]
    _nt.RtlNtStatusToDosError.restype = wintypes.ULONG

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _k32.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    _k32.CreateFileW.restype = wintypes.HANDLE
    _k32.GetFileInformationByHandleEx.argtypes = [
        wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD,
    ]
    _k32.GetFileInformationByHandleEx.restype = wintypes.BOOL
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]
    _k32.CloseHandle.restype = wintypes.BOOL

    _INVALID_HANDLE = wintypes.HANDLE(-1).value
    _READ_ATTRIBUTES = 0x80 | 0x00100000  # FILE_READ_ATTRIBUTES | SYNCHRONIZE
    _READ_DATA = 0x80000000 | 0x00100000  # GENERIC_READ | SYNCHRONIZE
    _SHARE_ALL = 0x7  # read, write, delete: a guest can still write, rename and delete it
    _OPEN_EXISTING = 3
    _FILE_FLAG_BACKUP_SEMANTICS = 0x02000000  # CreateFileW needs it to open a directory
    _OBJ_CASE_INSENSITIVE = 0x40
    _FILE_DIRECTORY_FILE = 0x1
    _FILE_SYNCHRONOUS_IO_NONALERT = 0x20
    _FILE_OPEN_REPARSE_POINT = 0x00200000
    _ATTR_DIRECTORY = 0x10
    _ATTR_REPARSE_POINT = 0x400  # symlinks, junctions, mount points
    _FILE_BASIC_INFO = 0
    _FILE_FULL_DIR_INFO = 14
    _FILE_FULL_DIR_RESTART_INFO = 15
    _ERROR_FILE_NOT_FOUND = 2
    _ERROR_NO_MORE_FILES = 18

    def _open_child(parent: int, name: str, access: int, directory: bool) -> int:
        """Open `name` inside the open folder `parent`. A link is opened as itself.
        An empty name opens what `parent` itself is, again."""
        size = len(name.encode("utf-16-le", "surrogatepass"))
        if size > 0xFFFE:  # the length field is 16 bits, and no name that long exists
            raise FileNotFoundError(name)
        ustr = _UnicodeString(size, size, name)
        attrs = _ObjectAttributes(
            ctypes.sizeof(_ObjectAttributes), parent, ctypes.pointer(ustr),
            _OBJ_CASE_INSENSITIVE, None, None,
        )
        options = _FILE_OPEN_REPARSE_POINT | _FILE_SYNCHRONOUS_IO_NONALERT
        if directory:
            options |= _FILE_DIRECTORY_FILE
        handle = wintypes.HANDLE()
        status = _nt.NtOpenFile(
            ctypes.byref(handle), access, ctypes.byref(attrs), ctypes.byref(_IoStatusBlock()),
            _SHARE_ALL, options,
        )
        if status < 0:
            raise ctypes.WinError(_nt.RtlNtStatusToDosError(status))
        assert handle.value is not None
        return handle.value

    def _attributes(handle: int) -> int:
        info = ctypes.create_string_buffer(40)  # FILE_BASIC_INFO: four times, then the attributes
        if not _k32.GetFileInformationByHandleEx(handle, _FILE_BASIC_INFO, info, len(info)):
            raise ctypes.WinError(ctypes.get_last_error())
        return int(struct.unpack_from("<L", info, 32)[0])

    def _inspect(
        real_root: str, parts: list[str], max_bytes: int, *, max_chars: int | None,
    ) -> WorkspaceDir | WorkspaceFile | None:
        """Walk to the entry with handles only; None means a component is missing."""
        handle = _k32.CreateFileW(
            real_root, _READ_ATTRIBUTES, _SHARE_ALL, None, _OPEN_EXISTING,
            _FILE_FLAG_BACKUP_SEMANTICS, None,
        )
        if handle == _INVALID_HANDLE:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            part = "."
            for i, part in enumerate(parts):
                try:
                    nxt = _open_child(handle, part, _READ_ATTRIBUTES, i < len(parts) - 1)
                except FileNotFoundError:
                    return None
                except OSError as e:
                    raise WorkspacePathError(f"cannot open {part!r}: {e.strerror or e}") from e
                _k32.CloseHandle(handle)
                handle = nxt
                if _attributes(handle) & _ATTR_REPARSE_POINT:
                    raise WorkspacePathError(_LINK_REFUSED)
            directory = bool(_attributes(handle) & _ATTR_DIRECTORY)
            try:
                nxt = _open_child(handle, "", _READ_DATA, directory)
            except OSError as e:
                raise WorkspacePathError(f"cannot open {part!r}: {e.strerror or e}") from e
            _k32.CloseHandle(handle)
            handle = nxt
            if directory:
                return _list_dir(handle, max_chars)
            fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
            handle = _INVALID_HANDLE  # the descriptor owns it now
            try:
                return _read_file(fd, max_bytes)
            finally:
                os.close(fd)
        finally:
            if handle != _INVALID_HANDLE:
                _k32.CloseHandle(handle)

    def _known(found: tuple[str, int]) -> tuple[str, int | None]:
        return found

    def _list_dir(handle: int, max_chars: int | None) -> WorkspaceDir:
        """List through the handle: FILE_FULL_DIR_INFO records, one buffer at a time."""
        entries: list[tuple[str, tuple[str, int]]] = []
        buf = ctypes.create_string_buffer(64 * 1024)
        info_class = _FILE_FULL_DIR_RESTART_INFO
        while True:
            if not _k32.GetFileInformationByHandleEx(handle, info_class, buf, len(buf)):
                error = ctypes.get_last_error()
                # A drive's root has no "." entry, so an empty one fails its first call.
                first = info_class == _FILE_FULL_DIR_RESTART_INFO
                if error == _ERROR_NO_MORE_FILES or (first and error == _ERROR_FILE_NOT_FOUND):
                    return _collect_dir(entries, _known, max_chars, False)
                raise ctypes.WinError(error)
            info_class = _FILE_FULL_DIR_INFO
            data = buf.raw  # one copy per buffer, not one per entry
            offset = 0
            while True:
                # NextEntryOffset at 0, EndOfFile at 40, FileAttributes and FileNameLength
                # at 56, FileName at 68.
                next_offset, = struct.unpack_from("<L", data, offset)
                size, = struct.unpack_from("<q", data, offset + 40)
                attributes, name_bytes = struct.unpack_from("<LL", data, offset + 56)
                # "replace": a name that isn't valid text can't go to the model as JSON.
                name = data[offset + 68 : offset + 68 + name_bytes].decode("utf-16-le", "replace")
                if name not in (".", ".."):
                    if len(entries) == _MAX_SCAN:
                        return _collect_dir(entries, _known, max_chars, True)
                    if attributes & _ATTR_REPARSE_POINT:
                        kind = "symlink"
                    elif attributes & _ATTR_DIRECTORY:
                        kind = "dir"
                    else:
                        kind = "file"
                    entries.append((name, (kind, size)))
                if next_offset == 0:
                    break
                offset += next_offset
