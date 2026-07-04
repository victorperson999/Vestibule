"""Path jail + read/list logic for the `read_workspace` tool.

Contract: docs/plans/M1-container-backend.md §6 (review findings 8/9). All string
validation happens BEFORE touching the filesystem; resolution then walks the path
component-by-component refusing symlinks/reparse points anywhere in the chain
(guest-planted symlinks are the headline attack). Lives in its own module so the
jail is unit-testable without Docker.

Residual risk, documented not ignored: a guest running concurrently can swap a
validated path for a symlink between our check and the open (TOCTOU). POSIX opens
use O_NOFOLLOW to close the terminal-component race; Windows has no equivalent
pre-open guarantee.
"""
from __future__ import annotations

import os
import stat
from pathlib import Path

_MAX_DIR_ENTRIES = 200

_WINDOWS_RESERVED = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{i}" for i in range(1, 10)}
    | {f"lpt{i}" for i in range(1, 10)}
)


class WorkspacePathError(Exception):
    """A requested path was refused by the jail. The message is agent-legible."""


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


def resolve_in_workspace(root: Path, user_path: str) -> Path:
    """Resolve user_path inside root, refusing symlinks/reparse points in the chain.

    Returns the resolved target (which may not exist — callers report 'Not found').
    Raises WorkspacePathError for anything the jail refuses.
    """
    parts = split_relpath(user_path)
    try:
        real_root = root.resolve(strict=True)
    except OSError as e:
        raise WorkspacePathError(f"workspace root is not accessible: {e}") from e

    current = real_root
    for part in parts:
        current = current / part
        try:
            st = os.lstat(current)
        except FileNotFoundError:
            break  # nothing below a missing component can exist, so nothing to follow
        except OSError as e:
            raise WorkspacePathError(f"cannot inspect {part!r}: {e.strerror or e}") from e
        if _is_symlink_or_reparse(st):
            raise WorkspacePathError("path traverses a symlink or reparse point; refused")

    final = real_root.joinpath(*parts)
    # Belt-and-braces containment — commonpath, never a string-prefix comparison.
    if os.path.commonpath([str(real_root), str(final)]) != str(real_root):
        raise WorkspacePathError("resolved path escapes the workspace")
    return final


def read_workspace_entry(root: Path, user_path: str, max_bytes: int) -> str:
    """Directory -> listing, file -> content, missing -> 'Not found: …'.

    Raises WorkspacePathError for refused paths; never raises for missing ones.
    """
    target = resolve_in_workspace(root, user_path)
    try:
        st = os.lstat(target)
    except FileNotFoundError:
        return f"Not found: {user_path or '.'}"
    except OSError as e:
        raise WorkspacePathError(f"cannot access path: {e.strerror or e}") from e
    if _is_symlink_or_reparse(st):
        raise WorkspacePathError("path is a symlink or reparse point; refused")
    if stat.S_ISDIR(st.st_mode):
        return _list_dir(target)
    if stat.S_ISREG(st.st_mode):
        return _read_file(target, max_bytes)
    raise WorkspacePathError("path is neither a regular file nor a directory")


def _list_dir(path: Path) -> str:
    entries: list[tuple[str, str, int]] = []
    with os.scandir(path) as it:
        for entry in it:
            try:
                st = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            if _is_symlink_or_reparse(st):
                kind = "symlink"
            elif stat.S_ISDIR(st.st_mode):
                kind = "dir"
            elif stat.S_ISREG(st.st_mode):
                kind = "file"
            else:
                kind = "other"
            entries.append((entry.name, kind, st.st_size))
    if not entries:
        return "(empty directory)"
    entries.sort()
    shown = entries[:_MAX_DIR_ENTRIES]
    lines = [f"{kind:<8}{size:>12}  {name}" for name, kind, size in shown]
    if len(entries) > len(shown):
        lines.append(f"...[{len(entries) - len(shown)} more entries not shown]")
    return "\n".join(lines)


def _read_file(path: Path, max_bytes: int) -> str:
    # O_NOFOLLOW closes the terminal-component TOCTOU race on POSIX; it does not
    # exist on Windows (reparse points were checked pre-open in the walk).
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as e:
        raise WorkspacePathError(f"cannot open file: {e.strerror or e}") from e
    with os.fdopen(fd, "rb") as f:
        data = f.read(max_bytes + 1)
    text = data[:max_bytes].decode("utf-8", errors="replace")
    if len(data) > max_bytes:
        text += f"\n...[truncated: file exceeds {max_bytes} bytes]"
    return text
