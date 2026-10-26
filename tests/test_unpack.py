"""Layer unpacker (M2 step 2, docs/plans/M2/M2-step2-rootfs.md S2-D2, S2-D6, §4.2). POSIX
only. The layers are built in memory with tarfile, so no binary fixture is committed.

Every refusal case runs next to a sentinel directory holding a sentinel file. Afterwards
everything outside the tree (the sentinels, and the tree's own parent) must be exactly as it
was, including the sentinel file's link count: a hardlink to it would raise st_nlink.
"""
import gzip
import io
import os
import shutil
import stat
import tarfile
from pathlib import Path

import pytest

unpack = pytest.importorskip(
    "vestibule.unpack", reason="POSIX-only: dir fds, O_NOFOLLOW and symlinks",
    exc_type=ImportError,
)
UnpackError = unpack.UnpackError


def d(name, mode=0o755):
    ti = tarfile.TarInfo(name)
    ti.type, ti.mode = tarfile.DIRTYPE, mode
    return ti, None


def f(name, data=b"x", mode=0o644):
    ti = tarfile.TarInfo(name)
    ti.size, ti.mode = len(data), mode
    return ti, data


def sym(name, target):
    ti = tarfile.TarInfo(name)
    ti.type, ti.linkname = tarfile.SYMTYPE, target
    return ti, None


def hard(name, target):
    ti = tarfile.TarInfo(name)
    ti.type, ti.linkname = tarfile.LNKTYPE, target
    return ti, None


def special(name, kind):
    ti = tarfile.TarInfo(name)
    ti.type = kind
    return ti, None


def _layer(path: Path, entries) -> Path:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as tf:
        for ti, data in entries:
            tf.addfile(ti, io.BytesIO(data) if data is not None else None)
    path.write_bytes(gzip.compress(buf.getvalue()))
    return path


@pytest.fixture
def work(tmp_path):
    """tmp/work/{tree/, sentinel/keep}, under umask 022 (modes must not depend on it)."""
    old = os.umask(0o022)
    w = tmp_path / "work"
    (w / "tree").mkdir(parents=True)
    (w / "sentinel").mkdir()
    (w / "sentinel" / "keep").write_text("keep")
    yield w
    os.umask(old)


def _outside(work: Path):
    """Everything under `work` except the tree, with what an escape could change."""
    seen = []
    for dirpath, dirnames, filenames in os.walk(work):  # doesn't follow symlinks
        if Path(dirpath) == work:
            dirnames.remove("tree")
        for n in dirnames + filenames:
            p = Path(dirpath, n)
            st = p.lstat()
            body = p.read_bytes() if stat.S_ISREG(st.st_mode) else None
            seen.append((str(p.relative_to(work)), stat.S_IFMT(st.st_mode), st.st_nlink, body))
    return sorted(seen)


def _run(work: Path, *layers):
    paths = [_layer(work.parent / f"layer{i}.tar.gz", entries) for i, entries in enumerate(layers)]
    unpack.unpack_layers(paths, work / "tree")


ABS = "<sentinel>"  # replaced by the sentinel directory's absolute path


@pytest.mark.parametrize("layers, message", [
    ([[f("../x")]], "'..' components are refused"),
    ([[f("/etc/x")]], "absolute names are refused"),
    ([[d("a"), f("a/../../x")]], "'..' components are refused"),
    ([[special("null", tarfile.CHRTYPE)]], "null: tar entry type b'3' is not supported"),
    ([[special("fifo", tarfile.FIFOTYPE)]], "fifo: tar entry type b'6' is not supported"),
    # multi-entry attacks (contract §6)
    ([[sym("a", ABS), f("a/.bashrc")]], "its parent 'a' is missing or not a plain directory"),
    ([[sym("a", ".."), f("a/x")]], "its parent 'a' is missing or not a plain directory"),
    ([[d("real"), f("real/x"), sym("a", "real"), hard("h", "a/x")]],
     "its parent 'a' is missing or not a plain directory"),
    ([[hard("h", f"{ABS}/keep")]], "hardlink target .* absolute names are refused"),
    ([[hard("h", "../sentinel/keep")]], "hardlink target .*'..' components are refused"),
    ([[sym("a", ABS), hard("h", "a/keep")]], "its parent 'a' is missing or not a plain"),
    ([[d("dir"), hard("h", "dir")]], "hardlink target dir is not a regular file"),
    ([[f("x"), sym("s", "x"), hard("h", "s")]], "hardlink target s is not a regular file"),
    ([[hard("h", "missing")]], "hardlink target missing is not a regular file"),
    # S2-D2: what the pinned images don't use
    ([[f("x"), f(".wh.x")]], "whiteouts are not supported"),
    ([[d("etc"), f("etc/.wh..wh..opq")]], "whiteouts are not supported"),
    ([[d("a"), f("a/x")], [f("a")]], "replacing a directory with a non-directory"),
    ([[f("nodir/x")]], "its parent 'nodir' is missing"),
])
def test_refused_entries_fail_and_touch_nothing_outside(work, layers, message):
    sentinel = str(work / "sentinel")
    for entries in layers:
        for ti, _ in entries:
            ti.linkname = ti.linkname.replace(ABS, sentinel)
    before = _outside(work)
    with pytest.raises(UnpackError, match=message):
        _run(work, *layers)
    assert _outside(work) == before


def test_a_nul_in_a_name_is_refused():
    with pytest.raises(UnpackError, match="NUL in a name"):
        unpack._parts("a\0b")


def test_the_real_images_shape_unpacks(work):
    tree = work / "tree"
    _run(work, [
        d(".", 0o700),  # both pinned images start with the root entry (theirs is 0o755)
        d("usr"), d("usr/bin"), f("usr/bin/gunzip", b"gz", 0o755),
        hard("usr/bin/uncompress", "usr/bin/gunzip"),
        sym("bin", "usr/bin"),                      # relative
        sym("usr/bin/awk", "/etc/alternatives/awk"),  # absolute, dangling: stored verbatim
        f("usr/bin/su", b"su", 0o4755), d("tmp", 0o1777), d("var"), d("var/mail", 0o2775),
        f("x" * 150, b"long"),  # beyond ustar's 100 bytes: a PAX long name
    ])
    assert os.readlink(tree / "bin") == "usr/bin"
    assert os.readlink(tree / "usr/bin/awk") == "/etc/alternatives/awk"
    a, b = (tree / "usr/bin/gunzip").stat(), (tree / "usr/bin/uncompress").stat()
    assert (a.st_ino, a.st_nlink) == (b.st_ino, 2)
    assert stat.S_IMODE((tree / "usr/bin/su").stat().st_mode) == 0o555   # 0o4755 & 0o555
    assert stat.S_IMODE((tree / "usr/bin/gunzip").stat().st_mode) == 0o555
    # Directories: sticky/setgid AND group/other write stripped, else (no sticky) any local
    # user could plant entries in a world-writable tmp of every guest's tree
    assert stat.S_IMODE((tree / "tmp").stat().st_mode) == 0o755          # was 0o1777
    assert stat.S_IMODE((tree / "var/mail").stat().st_mode) == 0o755     # was 0o2775
    assert stat.S_IMODE(tree.stat().st_mode) == 0o755  # the root entry's mode isn't applied
    assert (tree / ("x" * 150)).read_bytes() == b"long"
    shutil.rmtree(tree)  # read-only files, owner-writable dirs: deletable without a chmod
    assert not tree.exists()


def test_later_layers_replace_entries(work):
    tree = work / "tree"
    _run(work,
         [f("a", b"old"), hard("b", "a"), sym("s", "a"), f("c"), d("dir", 0o700)],
         [f("a", b"new"),     # writing at a hardlinked name must not write through it
          f("s", b"file"),    # symlink -> file
          d("c"),             # file -> dir
          d("dir", 0o751)])   # a re-listed directory takes the new mode
    assert (tree / "a").read_bytes() == b"new"
    assert (tree / "b").read_bytes() == b"old"
    assert (tree / "a").stat().st_ino != (tree / "b").stat().st_ino
    assert not (tree / "s").is_symlink() and (tree / "s").read_bytes() == b"file"
    assert (tree / "c").is_dir()
    assert stat.S_IMODE((tree / "dir").stat().st_mode) == 0o751 | 0o700
