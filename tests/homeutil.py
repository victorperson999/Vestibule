"""Stand-ins for a host whose home directory can't be used (M3 step 2, W4). A test patches one
over a module's `Path`, so only that module's `expanduser` calls see it. Shared by
tests/test_audit.py, tests/test_config.py and tests/test_rootfs.py.
"""
from __future__ import annotations

from pathlib import Path


class NoHome(type(Path())):  # type: ignore[misc]
    """No home directory: `~` doesn't expand, as with no HOME (or USERPROFILE) and no passwd
    entry."""

    def expanduser(self):
        if str(self).startswith("~"):
            raise RuntimeError("Could not determine home directory.")
        return self


class RelativeHome(type(Path())):  # type: ignore[misc]
    """A relative home: HOME=relative-home, or Windows' HOMEPATH without HOMEDRIVE."""

    def expanduser(self):
        text = str(self)
        return type(self)(text.replace("~", "relative-home", 1)) if text.startswith("~") else self
