"""Layer unpacker for `vestibule-mcp setup` (M2 step 2, docs/plans/M2/M2-step2-rootfs.md
S2-D2, S2-D6). POSIX only (native is Linux-only); stdlib only.

Unpacks verified gzip tar layers, in order, into the tree that becomes a sandbox's `/`. The
stdlib's safe filter can't be used: `tarfile.data_filter` rejects absolute symlinks, and
Debian trees are full of them (contract §6). So the rules live here:
- each entry's parent is walked one component at a time from the tree's root fd with
  O_NOFOLLOW | O_DIRECTORY, so a symlink from an earlier entry is never followed;
- every filesystem call is one name relative to a resolved parent fd, never a path: an
  absolute path would make the kernel ignore dir_fd;
- symlinks are stored verbatim; a hardlink only to a regular file already in the tree,
  its linkname held to the same rules as a name;
- files lose their write bits and setuid/setgid/sticky; directories lose group/other write
  and the set-id/sticky bits but keep owner rwx, so the cache can be deleted without a chmod
  (the read-only mount is the real guard);
- what the pinned images don't use is refused and fails the image (S2-D2): whiteouts, a
  directory replaced by a non-directory, a missing parent, devices, FIFOs, other types.
Ownership, mtimes and xattrs are not applied.
"""
from __future__ import annotations

import gzip
import os
import shutil
import stat
import sys
import tarfile
from pathlib import Path

if sys.platform == "win32":  # S2-D7: the Linux path imports this; mypy reads the guard
    raise ImportError("vestibule.unpack is POSIX-only")

_DIR = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_NEW_FILE = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
_CHUNK = 1024 * 1024


class UnpackError(Exception):
    """A layer has an entry the unpacker refuses; the message names it."""


def unpack_layers(layers: list[Path], tree: Path) -> None:
    """Unpack the gzip tar `layers`, in order, into the directory `tree`."""
    root = os.open(tree, _DIR)
    try:
        for i, layer in enumerate(layers, 1):
            with gzip.open(layer, "rb") as gz, tarfile.open(fileobj=gz, mode="r|") as tf:
                for member in tf:
                    try:
                        _apply(root, member, tf)
                    except UnpackError as e:
                        raise UnpackError(f"layer {i}: {e}") from None
    finally:
        os.close(root)


def _apply(root: int, m: tarfile.TarInfo, tf: tarfile.TarFile) -> None:
    parts = _parts(m.name)
    if parts is None:  # the root entry: the tree's own mode stays
        return
    if parts[-1].startswith(".wh."):
        raise UnpackError(f"{m.name}: whiteouts are not supported")
    parent = _walk(root, parts[:-1], m.name)
    leaf = parts[-1]
    try:
        existing = _lstat(parent, leaf)
        if m.isdir():
            if existing is not None and not stat.S_ISDIR(existing.st_mode):
                os.unlink(leaf, dir_fd=parent)
                existing = None
            if existing is None:
                os.mkdir(leaf, 0o700, dir_fd=parent)
            fd = os.open(leaf, _DIR, dir_fd=parent)
            try:
                # No group/other write: without the (stripped) sticky bit, a world-writable
                # /var/tmp would let any local user plant entries in every guest's tree.
                os.fchmod(fd, (m.mode & 0o755) | 0o700)
            finally:
                os.close(fd)
            return
        if not (m.isreg() or m.issym() or m.islnk()):
            raise UnpackError(f"{m.name}: tar entry type {m.type!r} is not supported")
        if existing is not None:
            if stat.S_ISDIR(existing.st_mode):
                raise UnpackError(f"{m.name}: replacing a directory with a "
                                  "non-directory is not supported")
            os.unlink(leaf, dir_fd=parent)
        if m.issym():
            os.symlink(m.linkname, leaf, dir_fd=parent)
        elif m.islnk():
            _hardlink(root, m, parent, leaf)
        else:
            _write_file(parent, leaf, m, tf)
    finally:
        if parent != root:
            os.close(parent)


def _parts(name: str) -> list[str] | None:
    """A name's components, or None for the root entry itself (`.` or `./`)."""
    if "\0" in name:
        raise UnpackError(f"{name!r}: NUL in a name")
    if name.startswith("/"):
        raise UnpackError(f"{name}: absolute names are refused")
    while name.startswith("./"):
        name = name[2:]
    name = name.rstrip("/")
    if name in ("", "."):
        return None
    parts = name.split("/")
    if any(p in ("", ".", "..") for p in parts):
        raise UnpackError(f"{name}: empty, '.' or '..' components are refused")
    return parts


def _walk(root: int, parts: list[str], name: str) -> int:
    """An fd for the directory `parts` names under root, following no symlink. The caller
    closes it unless it is root."""
    fd = root
    for p in parts:
        try:
            nxt = os.open(p, _DIR, dir_fd=fd)
        except OSError as e:
            raise UnpackError(f"{name}: its parent {p!r} is missing or not a plain "
                              f"directory ({e.strerror})") from None
        finally:
            if fd != root:
                os.close(fd)
        fd = nxt
    return fd


def _lstat(parent: int, leaf: str) -> os.stat_result | None:
    try:
        return os.stat(leaf, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        return None


def _write_file(parent: int, leaf: str, m: tarfile.TarInfo, tf: tarfile.TarFile) -> None:
    src = tf.extractfile(m)
    with os.fdopen(os.open(leaf, _NEW_FILE, 0o600, dir_fd=parent), "wb") as out:
        if src is not None:
            shutil.copyfileobj(src, out, _CHUNK)
        os.fchmod(out.fileno(), m.mode & 0o555)


def _hardlink(root: int, m: tarfile.TarInfo, parent: int, leaf: str) -> None:
    try:
        parts = _parts(m.linkname)
    except UnpackError as e:
        raise UnpackError(f"{m.name}: hardlink target {e}") from None
    if parts is None:
        raise UnpackError(f"{m.name}: hardlink to the root")
    src_parent = _walk(root, parts[:-1], m.name)
    try:
        target = _lstat(src_parent, parts[-1])
        if target is None or not stat.S_ISREG(target.st_mode):
            raise UnpackError(f"{m.name}: hardlink target {m.linkname} is not a regular "
                              "file already in the tree")
        os.link(parts[-1], leaf, src_dir_fd=src_parent, dst_dir_fd=parent,
                follow_symlinks=False)
    finally:
        if src_parent != root:
            os.close(src_parent)
