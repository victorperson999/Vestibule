"""The native rootfs cache and `vestibule-mcp setup` (M2 step 2,
docs/plans/M2/M2-step2-rootfs.md S2-D1, S2-D5, S2-D7, S2-D9, S2-D10).

`setup` fetches each digest-pinned image straight from its registry (registry.py), unpacks
it (unpack.py), and leaves:

    <rootfs_dir>/<index-hex>-<arch>/        the tree: a sandbox's /
    <rootfs_dir>/<index-hex>-<arch>.json    the marker, written last, beside the tree (spike A6)

It touches only those names, its lock file and its own `.vestibule-tmp-*` work dirs, so the
directory may be shared with unrelated files. Only a human runs it: the server never
downloads anything (D2). All output goes to stderr; stdout stays empty.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from vestibule import registry
from vestibule.config import ConfigError, Limits

FORMAT = 1
LOCK_NAME = ".vestibule-setup.lock"
TMP_PREFIX = ".vestibule-tmp-"
_TREE = re.compile(r"[0-9a-f]{64}-(?:amd64|arm64)")
# Docker writes these at run time, even under --network none; without them `localhost`
# doesn't resolve in a network-less sandbox (S2-D10).
HOSTS = "127.0.0.1\tlocalhost\n::1\tlocalhost ip6-localhost ip6-loopback\n"


class SetupError(Exception):
    """An image can't be installed; the message says why."""


@dataclass(frozen=True)
class Rootfs:
    tree: Path
    env: list[str]  # the image config's Env (N9)


def find(rootfs_dir: Path, image_ref: str) -> Rootfs | None:
    """The unpacked tree for `image_ref` on this host, or None if setup hasn't made it."""
    try:
        key = _key(registry.parse_ref(image_ref), registry.host_arch())
    except registry.RegistryError:
        return None
    marker = _read_marker(rootfs_dir, key)
    tree = rootfs_dir / key
    if (marker is None or marker.get("index_digest") != "sha256:" + key[:64]
            or marker.get("platform") != "linux/" + key[65:]
            or not isinstance(marker.get("env"), list)
            or tree.is_symlink() or not tree.is_dir()):
        return None
    return Rootfs(tree, [str(e) for e in marker["env"]])


def overlap_problem(limits: Limits) -> str | None:
    """Why the workspace and the rootfs directory can't be used together, or None (S2-D9):
    a sandbox that owns every file in the workspace could rewrite its own image."""
    ws = os.path.normcase(os.path.realpath(limits.workspace_path))
    rf = os.path.normcase(os.path.realpath(limits.rootfs_path))
    if ws == rf or _inside(ws, rf) or _inside(rf, ws):
        return (f"the workspace ({limits.workspace_path}, VESTIBULE_WORKSPACE) and the rootfs "
                f"directory ({limits.rootfs_path}, VESTIBULE_ROOTFS_DIR) overlap; point one "
                "of them somewhere else")
    return None


def setup_main() -> int:
    """`vestibule-mcp setup`: 0 when every image is ready, 1 otherwise (S2-D1)."""
    if sys.platform != "linux":  # S2-D7; also tells mypy the rest is Linux-only
        _say("vestibule setup: native isolation is Linux-only, so there is nothing to set "
             "up here. The container backend uses the images from the README's "
             "`docker pull` lines.")
        return 0
    try:
        limits = Limits.from_env()  # not server.LIMITS: that falls back to the defaults
    except ConfigError as e:
        _say("vestibule setup: invalid settings, nothing was changed:")
        for problem in e.problems:
            _say(f"  - {problem}")
        return 1
    overlap = overlap_problem(limits)
    if overlap:
        _say(f"vestibule setup: {overlap}")
        return 1
    rootfs_dir = limits.rootfs_path
    _say(f"vestibule setup: rootfs directory {rootfs_dir}")
    try:
        arch = registry.host_arch()
        rootfs_dir.mkdir(parents=True, exist_ok=True)
        lock = _lock(rootfs_dir)
    except (registry.RegistryError, OSError) as e:
        _say(f"vestibule setup: {e}")
        return 1
    if lock is None:
        _say(f"vestibule setup: another setup is running on {rootfs_dir}; wait for it")
        return 1
    failed = False
    try:
        _sweep(rootfs_dir)
        for label, ref in (("python image", limits.image_python),
                           ("node image", limits.image_node)):
            _say(f"{label}: {ref}")
            try:
                _say(f"  {_install(rootfs_dir, ref, arch)}")
            except Exception as e:  # report it, and still try the other image
                failed = True
                _say(f"  failed: {e}")
    finally:
        os.close(lock)
    return 1 if failed else 0


def _install(rootfs_dir: Path, image_ref: str, arch: str) -> str:
    if sys.platform == "win32":  # never: setup_main returns first; the guard is for mypy
        raise SetupError("Linux only")
    if find(rootfs_dir, image_ref) is not None:
        return "already present"
    from vestibule import unpack  # the Linux path only (S2-D7)

    t0 = time.monotonic()
    ref = registry.parse_ref(image_ref)
    reg = registry.Registry(ref)
    image = registry.resolve(reg, arch)
    key = _key(ref, arch)
    work = Path(tempfile.mkdtemp(prefix=TMP_PREFIX, dir=rootfs_dir))
    try:
        blobs = [work / f"layer{i}" for i in range(len(image.layers))]
        for layer, blob in zip(image.layers, blobs):
            reg.blob_to_file(layer, blob)  # verified before anything parses it (S2-D3)
        tree = work / "tree"
        tree.mkdir()
        os.chmod(tree, 0o755)
        written = unpack.unpack_layers(blobs, tree)
        for blob in blobs:
            blob.unlink()
        _add_mount_points(tree)
        os.sync()  # file data on disk before a marker can vouch for it
        _remove(rootfs_dir, key)  # a stale tree of ours that find() didn't accept
        tree.rename(rootfs_dir / key)
        _write_marker(work / "marker", rootfs_dir / f"{key}.json", {
            "format": FORMAT, "ref": image_ref, "index_digest": ref.digest,
            "manifest_digest": image.manifest_digest, "platform": f"linux/{arch}",
            "env": image.env,
        })
    finally:
        shutil.rmtree(work, ignore_errors=True)
    downloaded = sum(int(layer["size"]) for layer in image.layers)
    return (f"ready: {downloaded / 1e6:.1f} MB downloaded, {written / 1e6:.1f} MB unpacked, "
            f"in {time.monotonic() - t0:.1f} s")


def _add_mount_points(tree: Path) -> None:
    """What the warden mounts onto and Docker adds at run time (S2-D10)."""
    for name in ("workspace", "sandbox"):
        p = tree / name
        if p.is_symlink() or (p.exists() and not p.is_dir()):
            raise SetupError(f"the image has /{name} as something other than a directory")
        p.mkdir(exist_ok=True)
        os.chmod(p, 0o755)
    etc, hosts = tree / "etc", tree / "etc" / "hosts"
    if etc.is_symlink() or not etc.is_dir():
        raise SetupError("the image has no plain /etc directory")
    if hosts.is_symlink() or (hosts.exists() and not hosts.is_file()):
        raise SetupError("the image has /etc/hosts as something other than a file")
    hosts.unlink(missing_ok=True)
    hosts.write_text(HOSTS, encoding="ascii")
    os.chmod(hosts, 0o444)


def _sweep(rootfs_dir: Path) -> None:
    """Under the lock: setup's own leftovers go; nothing else is touched (S2-D5)."""
    for p in rootfs_dir.iterdir():
        if p.name.startswith(TMP_PREFIX):
            _remove(rootfs_dir, p.name)
        elif _TREE.fullmatch(p.name) and _read_marker(rootfs_dir, p.name) is None:
            _remove(rootfs_dir, p.name)


def _remove(rootfs_dir: Path, name: str) -> None:
    """Remove one of setup's own entries (and a tree's marker first)."""
    (rootfs_dir / f"{name}.json").unlink(missing_ok=True)
    p = rootfs_dir / name
    if p.is_symlink() or p.is_file():
        p.unlink()
    elif p.is_dir():
        shutil.rmtree(p)  # files are read-only, directories owner-writable (S2-D6)


def _read_marker(rootfs_dir: Path, key: str) -> dict[str, Any] | None:
    try:
        marker = json.loads((rootfs_dir / f"{key}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return marker if isinstance(marker, dict) and marker.get("format") == FORMAT else None


def _write_marker(tmp: Path, path: Path, marker: dict[str, Any]) -> None:
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(marker, f, indent=1)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _lock(rootfs_dir: Path) -> int | None:
    """An exclusive flock on the lock file, or None if another setup holds it."""
    if sys.platform == "win32":  # never: setup_main returns first; the guard is for mypy
        raise SetupError("Linux only")
    import fcntl

    fd = os.open(rootfs_dir / LOCK_NAME, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    return fd


def _key(ref: registry.Ref, arch: str) -> str:
    return f"{ref.digest[7:]}-{arch}"


def _inside(child: str, parent: str) -> bool:
    try:
        return os.path.commonpath([child, parent]) == parent
    except ValueError:  # different drives on Windows
        return False


def _say(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)
