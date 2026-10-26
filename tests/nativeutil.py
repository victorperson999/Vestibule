"""Shared helpers for the native-marked tests (M2 step 3): one skip rule, and a scan for the
host processes that carry a token in their command line.

The skip rule: these tests need Linux, working unprivileged user namespaces, and both pinned
trees that `vestibule-mcp setup` makes in the default rootfs directory. Anywhere else they skip
with the reason. Nothing here downloads anything.
"""
import os
import subprocess
import sys

import pytest

from vestibule import rootfs
from vestibule.config import Limits

_USERNS_PROBE = ("import ctypes, sys; "
                 "sys.exit(ctypes.CDLL(None, use_errno=True).unshare(0x10000000) != 0)")


def native_skip_reason() -> str | None:
    """None if the native-marked tests can run here, else why not (with the fix)."""
    if sys.platform != "linux":
        return "native isolation is Linux-only"
    lim = Limits()
    if any(rootfs.find(lim.rootfs_path, ref) is None for ref in (lim.image_python, lim.image_node)):
        return f"no native rootfs in {lim.rootfs_path}; run `vestibule-mcp setup` first"
    probe = subprocess.run([sys.executable, "-c", _USERNS_PROBE], timeout=10,
                           stdin=subprocess.DEVNULL, capture_output=True)
    if probe.returncode != 0:
        return "unprivileged user namespaces don't work here"
    return None


SKIP_REASON = native_skip_reason()
native_required = pytest.mark.skipif(SKIP_REASON is not None, reason=SKIP_REASON or "")


def holders(token: str) -> list[int]:
    """PIDs of the host's processes whose command line contains `token`."""
    found = []
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        try:
            with open(f"/proc/{name}/cmdline", "rb") as f:
                if token.encode() in f.read():
                    found.append(int(name))
        except OSError:
            pass
    return found
