"""The rootfs cache and `vestibule-mcp setup` (M2 step 2, docs/plans/M2/M2-step2-rootfs.md
§4.1, §4.2). Offline: the setup tests replace the registry with local layer files.
"""
import gzip
import hashlib
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest
from dockerutil import docker_required

import vestibule.server as server
from vestibule import registry, rootfs
from vestibule.config import DEFAULT_IMAGE_NODE, DEFAULT_IMAGE_PYTHON, Limits

linux_only = pytest.mark.skipif(sys.platform != "linux", reason="native setup is Linux-only")
PY, NODE = "1" * 64, "2" * 64


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in list(os.environ):
        if name.startswith("VESTIBULE_"):
            monkeypatch.delenv(name)


# --- the overlap check (S2-D9) and the command line (S2-D1): every platform ---------------

@pytest.mark.parametrize("workspace, rootfs_dir", [
    ("{t}/x", "{t}/x"),                 # equal
    ("{t}/x", "{t}/x/rootfs"),          # rootfs inside the workspace
    ("{t}/x/ws", "{t}/x"),              # workspace inside the rootfs
    ("{t}/x/a/../rootfs", "{t}/x/rootfs"),
    ("~", "~/.vestibule/rootfs"),       # an M1 setup that worked with the container backend
])
def test_overlapping_directories_are_named(tmp_path, workspace, rootfs_dir):
    lim = Limits(workspace_dir=workspace.format(t=tmp_path),
                 rootfs_dir=rootfs_dir.format(t=tmp_path))
    problem = rootfs.overlap_problem(lim)
    assert problem and "VESTIBULE_WORKSPACE" in problem and "VESTIBULE_ROOTFS_DIR" in problem


def test_sibling_directories_are_fine(tmp_path):
    lim = Limits(workspace_dir=str(tmp_path / "ws"), rootfs_dir=str(tmp_path / "ws-rootfs"))
    assert rootfs.overlap_problem(lim) is None


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
def test_overlap_through_a_symlink_is_caught(tmp_path):
    (tmp_path / "real").mkdir()
    (tmp_path / "link").symlink_to(tmp_path / "real")
    lim = Limits(workspace_dir=str(tmp_path / "link"), rootfs_dir=str(tmp_path / "real/rootfs"))
    assert rootfs.overlap_problem(lim) is not None


def test_no_arguments_serve_mcp(monkeypatch):
    served = []

    async def fake_main():
        served.append(True)

    monkeypatch.setattr(server, "_main", fake_main)
    monkeypatch.setattr(sys, "argv", ["vestibule-mcp"])
    server.run()
    assert served == [True]


def test_setup_runs_the_setup_command(monkeypatch):
    monkeypatch.setattr(server.rootfs, "setup_main", lambda: 7)
    monkeypatch.setattr(sys, "argv", ["vestibule-mcp", "setup"])
    with pytest.raises(SystemExit) as e:
        server.run()
    assert e.value.code == 7


def test_other_arguments_warn_and_still_serve(monkeypatch, capsys, caplog):
    served = []

    async def fake_main():
        served.append(True)

    monkeypatch.setattr(server, "_main", fake_main)
    monkeypatch.setattr(sys, "argv", ["vestibule-mcp", "setp"])
    server.run()
    assert served == [True]
    assert 'ignoring unexpected arguments [\'setp\']; the only subcommand is "setup"' in caplog.text
    assert capsys.readouterr().out == ""  # golden rule 1


def test_setup_on_another_os_says_linux_only(monkeypatch, capsys):
    monkeypatch.setattr(sys, "platform", "darwin")
    assert rootfs.setup_main() == 0
    out = capsys.readouterr()
    assert "native isolation is Linux-only" in out.err and out.out == ""


def test_setup_reads_its_own_settings(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(sys, "platform", "linux")  # the config check comes first
    monkeypatch.setenv("VESTIBULE_MEM_MB", "512m")
    monkeypatch.setenv("VESTIBULE_ROOTFS_DIR", str(tmp_path / "rootfs"))
    assert rootfs.setup_main() == 1
    err = capsys.readouterr().err
    assert "invalid settings, nothing was changed" in err and "VESTIBULE_MEM_MB='512m'" in err
    assert not (tmp_path / "rootfs").exists()


def test_setup_refuses_an_overlap_before_writing_anything(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("VESTIBULE_WORKSPACE", str(tmp_path / "ws"))
    monkeypatch.setenv("VESTIBULE_ROOTFS_DIR", str(tmp_path / "ws" / "rootfs"))
    assert rootfs.setup_main() == 1
    assert "overlap" in capsys.readouterr().err
    assert not (tmp_path / "ws").exists()


# --- setup and the cache (S2-D5, S2-D10): Linux ---------------------------------------------

def _layer(path: Path, entries) -> Path:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as tf:
        for name, kind, data in entries:
            ti = tarfile.TarInfo(name)
            ti.type, ti.mode = kind, 0o755
            ti.size = len(data or b"")
            tf.addfile(ti, io.BytesIO(data) if data is not None else None)
    path.write_bytes(gzip.compress(buf.getvalue()))
    return path


BASE = [(".", tarfile.DIRTYPE, None), ("etc", tarfile.DIRTYPE, None),
        ("bin", tarfile.DIRTYPE, None), ("bin/sh", tarfile.REGTYPE, b"#!")]


@pytest.fixture
def env(monkeypatch, tmp_path):
    """Settings pointing at tmp, and a fake registry serving local layers per digest."""
    d = tmp_path / "rootfs"
    monkeypatch.setenv("VESTIBULE_ROOTFS_DIR", str(d))
    monkeypatch.setenv("VESTIBULE_WORKSPACE", str(tmp_path / "ws"))
    monkeypatch.setenv("VESTIBULE_IMAGE_PYTHON", f"docker.io/library/python@sha256:{PY}")
    monkeypatch.setenv("VESTIBULE_IMAGE_NODE", f"docker.io/library/node@sha256:{NODE}")
    images = {
        PY: [_layer(tmp_path / "py.tgz", BASE)],
        NODE: [_layer(tmp_path / "node.tgz", BASE)],
    }
    calls: list[str] = []

    def resolve(reg, arch):
        calls.append(reg.ref.digest)
        layers = [{"digest": f"sha256:{i:064x}", "size": p.stat().st_size, "path": str(p)}
                  for i, p in enumerate(images[reg.ref.digest[7:]])]
        return registry.Image(reg.ref, "sha256:" + "f" * 64, ["PATH=/bin", "LANG=C.UTF-8"], layers)

    def blob_to_file(self, desc, dest):
        shutil.copyfile(desc["path"], dest)

    monkeypatch.setattr(registry, "resolve", resolve)
    monkeypatch.setattr(registry.Registry, "blob_to_file", blob_to_file)
    return d, images, calls


def _arch():
    return registry.host_arch()


@linux_only
def test_setup_makes_both_trees_with_their_mount_points(env, capsys):
    d, _, calls = env
    assert rootfs.setup_main() == 0
    out = capsys.readouterr()
    assert out.out == ""  # stderr only
    assert out.err.count("ready:") == 2
    for hexd in (PY, NODE):
        found = rootfs.find(d, f"docker.io/library/x@sha256:{hexd}")
        assert found is not None and found.tree == d / f"{hexd}-{_arch()}"
        assert found.env == ["PATH=/bin", "LANG=C.UTF-8"]
        assert (found.tree / "workspace").is_dir() and (found.tree / "sandbox").is_dir()
        assert (found.tree / "etc/hosts").read_text() == rootfs.HOSTS
        assert (found.tree / "bin/sh").read_bytes() == b"#!"
        marker = json.loads((d / f"{hexd}-{_arch()}.json").read_text())
        assert marker["platform"] == f"linux/{_arch()}" and marker["format"] == 1
    assert len(calls) == 2


@linux_only
def test_already_present_makes_no_registry_call(env, capsys):
    d, _, calls = env
    assert rootfs.setup_main() == 0
    calls.clear()
    assert rootfs.setup_main() == 0
    assert calls == []
    assert capsys.readouterr().err.count("already present") == 2


@linux_only
def test_a_tree_without_its_marker_is_absent_and_replaced(env, monkeypatch, capsys):
    d, _, _ = env
    real, crashed = rootfs._write_marker, []

    def crash_once(*args):
        if not crashed:
            crashed.append(True)
            raise OSError("crash between the rename and the marker")
        real(*args)

    monkeypatch.setattr(rootfs, "_write_marker", crash_once)
    assert rootfs.setup_main() == 1  # python failed; node still installed
    assert "failed: crash between" in capsys.readouterr().err
    assert (d / f"{PY}-{_arch()}").is_dir() and not (d / f"{PY}-{_arch()}.json").exists()
    assert rootfs.find(d, f"python@sha256:{PY}") is None
    assert rootfs.find(d, f"node@sha256:{NODE}") is not None
    assert rootfs.setup_main() == 0
    assert rootfs.find(d, f"python@sha256:{PY}") is not None


@linux_only
def test_setup_removes_only_its_own_leftovers(env):
    d, _, _ = env
    d.mkdir()
    (d / ".vestibule-tmp-stale").mkdir()
    (d / ".vestibule-tmp-stale/file").write_text("x")
    unrelated = [d / "keyrings", d / ".tmp-mine", d / f"{PY}-s390x", d / "x.json"]
    for p in unrelated[:3]:
        p.mkdir()
        (p / "keep").write_text("keep")
    unrelated[3].write_text("{}")
    assert rootfs.setup_main() == 0
    assert not (d / ".vestibule-tmp-stale").exists()
    assert all(p.exists() for p in unrelated)
    assert (d / "keyrings" / "keep").read_text() == "keep"


@linux_only
@pytest.mark.parametrize("marker", ["not json", '{"format": 99}', "[]"])
def test_an_invalid_marker_means_refetch(env, marker):
    d, _, calls = env
    assert rootfs.setup_main() == 0
    (d / f"{PY}-{_arch()}.json").write_text(marker)
    calls.clear()
    assert rootfs.setup_main() == 0
    assert calls == [f"sha256:{PY}"]
    assert rootfs.find(d, f"python@sha256:{PY}") is not None


@linux_only
def test_a_held_lock_means_another_setup_is_running(env, capsys):
    import fcntl

    d, _, calls = env
    d.mkdir()
    fd = os.open(d / rootfs.LOCK_NAME, os.O_RDWR | os.O_CREAT)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        assert rootfs.setup_main() == 1
    finally:
        os.close(fd)
    assert "another setup is running" in capsys.readouterr().err
    assert calls == []


@linux_only
def test_find_refuses_a_symlinked_tree(env, tmp_path):
    d, _, _ = env
    assert rootfs.setup_main() == 0
    tree = d / f"{PY}-{_arch()}"
    tree.rename(tmp_path / "elsewhere")
    tree.symlink_to(tmp_path / "elsewhere")
    assert rootfs.find(d, f"python@sha256:{PY}") is None


@linux_only
def test_an_image_with_a_file_at_a_mount_point_is_refused(env, tmp_path, capsys):
    d, images, _ = env
    images[PY] = [_layer(tmp_path / "bad.tgz", [*BASE, ("workspace", tarfile.REGTYPE, b"")])]
    assert rootfs.setup_main() == 1
    assert "/workspace as something other than a directory" in capsys.readouterr().err
    assert rootfs.find(d, f"python@sha256:{PY}") is None
    assert not list(d.glob(".vestibule-tmp-*"))  # the work dir went with the failure


@linux_only
def test_an_arm64_tree_round_trips(env, monkeypatch):
    d, _, _ = env
    monkeypatch.setattr(registry.platform, "machine", lambda: "aarch64")
    assert rootfs.setup_main() == 0
    assert (d / f"{PY}-arm64").is_dir()
    assert json.loads((d / f"{PY}-arm64.json").read_text())["platform"] == "linux/arm64"
    assert rootfs.find(d, f"python@sha256:{PY}") is not None


# --- the real pinned images (§4.3): Linux, network; the oracle also needs Docker ------------

@pytest.fixture(scope="session")
def real_trees(tmp_path_factory):
    """Both pinned images through setup's real path, once per session (4 registry pulls)."""
    if sys.platform != "linux":
        pytest.skip("native setup is Linux-only")
    d = tmp_path_factory.mktemp("rootfs")
    arch = registry.host_arch()
    for ref in (DEFAULT_IMAGE_PYTHON, DEFAULT_IMAGE_NODE):
        rootfs._install(d, ref, arch)
    return d


@pytest.mark.network
def test_the_pinned_images_install(real_trees):
    py = rootfs.find(real_trees, DEFAULT_IMAGE_PYTHON)
    node = rootfs.find(real_trees, DEFAULT_IMAGE_NODE)
    assert py is not None and node is not None
    assert "LANG=C.UTF-8" in py.env and any(e.startswith("PATH=") for e in py.env)
    assert any(e.startswith("PATH=") for e in node.env)
    for path in (py.tree / "usr/local/bin/python3.12", node.tree / "usr/local/bin/node"):
        assert path.is_file() and not path.is_symlink()
    assert not (py.tree / "usr/bin/su").lstat().st_mode & (stat.S_ISUID | stat.S_ISGID)


# What differs from `docker export` by design, fixed from the first diff (plan §4.3; the
# 2026-10-24 build). Asserted exactly, so any other difference fails.
ONLY_DOCKER = {".dockerenv", "dev/console", "dev/pts", "dev/shm", "etc/mtab"}  # its container
ONLY_OURS = {"workspace", "sandbox"}  # S2-D10's mount points
# Docker rewrites these when it creates a container; ours keep the image's, except hosts
# (S2-D10).
DIFFERENT = {"etc/hostname", "etc/hosts", "etc/resolv.conf"}


def _norm(name: str) -> str:
    while name.startswith("./"):
        name = name[2:]
    return name.rstrip("/")


def _docker_export(ref: str) -> dict[str, tuple]:
    cid = subprocess.run(["docker", "create", "--pull", "never", ref], capture_output=True,
                         text=True, check=True, timeout=60, stdin=subprocess.DEVNULL).stdout.strip()
    entries: dict[str, tuple] = {}
    try:
        proc = subprocess.Popen(["docker", "export", cid], stdout=subprocess.PIPE,
                                stdin=subprocess.DEVNULL)
        assert proc.stdout is not None
        with tarfile.open(fileobj=proc.stdout, mode="r|") as tf:
            for m in tf:
                name = _norm(m.name)
                if name in ("", "."):
                    continue
                if m.issym():
                    entries[name] = ("sym", m.linkname)
                elif m.islnk():
                    entries[name] = ("hardlink", _norm(m.linkname))
                elif m.isdir():
                    entries[name] = ("dir", (m.mode & 0o777) | 0o700)  # S2-D6's formula
                elif m.isreg():
                    data = tf.extractfile(m).read()  # type: ignore[union-attr]
                    entries[name] = ("file", m.mode & 0o555, hashlib.sha256(data).hexdigest())
                else:
                    entries[name] = ("other", m.type)
        assert proc.wait(timeout=120) == 0
    finally:
        subprocess.run(["docker", "rm", "-f", cid], capture_output=True, timeout=30,
                       stdin=subprocess.DEVNULL)
    return entries


def _ours(tree: Path) -> tuple[dict[str, tuple], dict[str, tuple[int, int]]]:
    entries: dict[str, tuple] = {}
    inodes: dict[str, tuple[int, int]] = {}
    for dirpath, dirnames, filenames in os.walk(tree):
        for n in dirnames + filenames:
            p = Path(dirpath, n)
            rel, st = p.relative_to(tree).as_posix(), p.lstat()
            if stat.S_ISLNK(st.st_mode):
                entries[rel] = ("sym", os.readlink(p))
            elif stat.S_ISDIR(st.st_mode):
                entries[rel] = ("dir", stat.S_IMODE(st.st_mode))
            else:
                entries[rel] = ("file", stat.S_IMODE(st.st_mode),
                                hashlib.sha256(p.read_bytes()).hexdigest())
                inodes[rel] = (st.st_dev, st.st_ino)
    return entries, inodes


@pytest.mark.docker
@pytest.mark.network
@docker_required
@pytest.mark.parametrize("ref", [DEFAULT_IMAGE_PYTHON, DEFAULT_IMAGE_NODE], ids=["python", "node"])
def test_our_tree_matches_docker_export(real_trees, ref):
    """Docker's own unpacker is the reference: same paths, types, symlink targets, contents
    and hardlink pairs, and our mode is S2-D6's formula applied to Docker's, for EVERY
    entry (checking a few known files would let a kept setuid bit through)."""
    theirs = _docker_export(ref)
    found = rootfs.find(real_trees, ref)
    assert found is not None
    ours, inodes = _ours(found.tree)
    for name, entry in list(theirs.items()):  # a hardlink is its target's file, same inode
        if entry[0] == "hardlink":
            theirs[name] = theirs[entry[1]]
            assert inodes.get(name) == inodes.get(entry[1]) is not None, f"{name} -> {entry[1]}"
    only_docker = set(theirs) - set(ours)
    only_ours = set(ours) - set(theirs)
    different = {n for n in set(theirs) & set(ours) if theirs[n] != ours[n]}
    assert (only_docker, only_ours, different) == (ONLY_DOCKER, ONLY_OURS, DIFFERENT), (
        {n: theirs[n] for n in different}, {n: ours[n] for n in different})
