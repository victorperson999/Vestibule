"""Configuration: resource limits, allowed languages, and M1 backend settings.

Everything is overridable via VESTIBULE_* environment variables (see Limits.from_env).
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

ALLOWED_LANGUAGES: tuple[str, ...] = ("python", "bash", "node")

# Setting syntax (docs/plans/M1/M1-followup-config-version.md). `[0-9]`, not `\d` or
# isdigit(): both accept non-ASCII digits, and `²` passes isdigit() but crashes int().
_WHOLE_NUMBER = re.compile(r"[0-9]+")
# A syntax cap, not a limit: far beyond any sensible setting, and it keeps int() well
# inside Python's digit limit (4300 by default, adjustable), so the verdict never
# depends on the interpreter.
_MAX_DIGITS = 18
_ON = ("1", "true", "yes", "on")
_OFF = ("0", "false", "no", "off")


def _shown(raw: str) -> str:
    """A setting's value as typed, for an error message the model reads: long ones cut."""
    return repr(raw) if len(raw) <= 40 else f"{raw[:40]!r}... ({len(raw)} characters)"


class ConfigError(ValueError):
    """One or more VESTIBULE_* settings are invalid; `problems` names every one."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems

# Sandbox images, pinned by multi-platform index digest (D2, M1 step 6 —
# docs/plans/M1/M1-step6-pinning.md S6-D1). Tagless and fully qualified on purpose: Docker
# does not verify a tag that sits next to a digest, so the tag lives here instead, and
# tests/test_container.py checks the interpreter version inside each pinned image.
#   python  <- python:3.12-slim    node  <- node:22-slim
# Captured 2026-10-10 from a fresh `docker pull <tag>` on Docker 28.5.1: the pull's
# "Digest:" line matched RepoDigests, and `docker buildx imagetools inspect` showed an OCI
# index with linux/amd64 + linux/arm64/v8. Proven 2026-10-10 under BOTH Docker Desktop image
# stores, containerd and classic (S6-D4): pull by this exact ref into an empty store, then
# `image inspect` + `run --pull never` (python 3.12.15, node v22.23.3) + `pytest -m docker`.
# To re-pin, follow the plan's "Re-pin cost" (one store mode suffices for the same form).
# On Linux, also run `vestibule-mcp setup` and `pytest -m network`: native unpacks these
# images itself and refuses entry kinds they don't use (docs/plans/M2/M2-step2-rootfs.md S2-D2).
DEFAULT_IMAGE_PYTHON = (
    "docker.io/library/python"
    "@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1"
)
DEFAULT_IMAGE_NODE = (
    "docker.io/library/node"
    "@sha256:c3de60bf2f9dd0ac6370e6117950ff62d6e339527e7472301c9c78a017978392"
)


@dataclass(frozen=True)
class Limits:
    max_timeout_s: int = 60           # server clamps requests to this ceiling
    default_timeout_s: int = 10
    max_code_bytes: int = 256 * 1024  # reject giant payloads before the warden
    max_output_bytes: int = 20_000    # truncate guest output to protect model context
    mem_mb: int = 256                 # (used by container/native backends)
    pids_max: int = 128
    cpu_pct: int = 75

    # M1: workspace + container backend (docs/plans/M1/M1-container-backend.md §8)
    workspace_dir: str = "~/.vestibule/workspace"
    workspace_ro: bool = False
    runtime: str = "auto"             # auto | docker | podman
    backend: str = "auto"             # auto | native | container | naive (naive: dev-only)
    image_python: str = DEFAULT_IMAGE_PYTHON
    image_node: str = DEFAULT_IMAGE_NODE
    max_concurrent: int = 4
    tmpfs_mb: int = 64

    # M2: where `vestibule-mcp setup` unpacks the images (docs/plans/M2/M2-step2-rootfs.md)
    rootfs_dir: str = "~/.vestibule/rootfs"

    # M3: the audit log (docs/plans/M3/M3-audit-usage.md A5, A8)
    audit_dir: str = "~/.vestibule/audit"

    @property
    def workspace_path(self) -> Path:
        return Path(self.workspace_dir).expanduser()

    @property
    def rootfs_path(self) -> Path:
        return Path(self.rootfs_dir).expanduser()

    @property
    def audit_path(self) -> Path:
        return Path(self.audit_dir).expanduser()

    @classmethod
    def from_env(cls) -> "Limits":
        """Limits with the VESTIBULE_* overrides applied. Unset or blank means the default.

        Raises ConfigError naming every invalid setting. An invalid value is never quietly
        replaced by the default: the user would believe a limit is set that isn't
        (golden rule 6).
        """
        problems: list[str] = []

        def _i(name: str, default: int) -> int:
            raw = os.environ.get(name, "")
            v = raw.strip()
            if not v:
                return default
            if not _WHOLE_NUMBER.fullmatch(v):
                problems.append(f"{name}={_shown(raw)} is not a positive whole number")
                return default
            digits = v.lstrip("0")  # leading zeros don't make a number large
            if len(digits) > _MAX_DIGITS:
                problems.append(
                    f"{name}={_shown(raw)} is too large (more than {_MAX_DIGITS} digits)"
                )
                return default
            if not digits:
                problems.append(f"{name}={_shown(raw)} is not a positive whole number")
                return default
            return int(digits)

        def _s(name: str, default: str) -> str:
            v = os.environ.get(name)
            return v if v else default

        def _b(name: str) -> bool:
            raw = os.environ.get(name, "")
            v = raw.strip().lower()
            if not v or v in _OFF:
                return False
            if v in _ON:
                return True
            problems.append(
                f"{name}={_shown(raw)} is not on or off (use 1/true/yes/on or 0/false/no/off)"
            )
            return False

        limits = cls(
            max_timeout_s=_i("VESTIBULE_MAX_TIMEOUT_S", 60),
            default_timeout_s=_i("VESTIBULE_DEFAULT_TIMEOUT_S", 10),
            max_code_bytes=_i("VESTIBULE_MAX_CODE_BYTES", 256 * 1024),
            max_output_bytes=_i("VESTIBULE_MAX_OUTPUT_BYTES", 20_000),
            mem_mb=_i("VESTIBULE_MEM_MB", 256),
            pids_max=_i("VESTIBULE_PIDS_MAX", 128),
            cpu_pct=_i("VESTIBULE_CPU_PCT", 75),
            workspace_dir=_s("VESTIBULE_WORKSPACE", "~/.vestibule/workspace"),
            workspace_ro=_b("VESTIBULE_WORKSPACE_RO"),
            runtime=_s("VESTIBULE_RUNTIME", "auto"),
            backend=_s("VESTIBULE_BACKEND", "auto"),
            image_python=_s("VESTIBULE_IMAGE_PYTHON", DEFAULT_IMAGE_PYTHON),
            image_node=_s("VESTIBULE_IMAGE_NODE", DEFAULT_IMAGE_NODE),
            max_concurrent=_i("VESTIBULE_MAX_CONCURRENT", 4),
            tmpfs_mb=_i("VESTIBULE_TMPFS_MB", 64),
            rootfs_dir=_s("VESTIBULE_ROOTFS_DIR", "~/.vestibule/rootfs"),
            audit_dir=_s("VESTIBULE_AUDIT_DIR", "~/.vestibule/audit"),
        )
        audit = _audit_problem(limits, explicit=bool(os.environ.get("VESTIBULE_AUDIT_DIR")))
        if audit:
            problems.append(audit)
        if problems:
            raise ConfigError(problems)
        return limits


def _audit_problem(limits: Limits, *, explicit: bool) -> str | None:
    """Why the audit dir can't be used (A8), or None. The sandbox must never reach the records,
    so the dir must be absolute and outside the workspace and the rootfs dir. Static, so it runs
    at startup. An unset default that can't expand (no home directory) is not a problem here:
    the writer reports it on every record instead (W4)."""
    shown = f"VESTIBULE_AUDIT_DIR={_shown(limits.audit_dir)}"
    try:
        audit = limits.audit_path
    except RuntimeError as e:  # no home directory, or a ~user that doesn't exist
        return f"{shown} must be an absolute path or start with ~ ({e})" if explicit else None
    if not audit.is_absolute():  # relative, drive-relative (Windows), or a relative home
        if not explicit:
            return None
        if limits.audit_dir.startswith("~"):
            return (f"{shown} expands to {audit}, which isn't absolute: the home directory is "
                    "relative")
        return f"{shown} must be an absolute path or start with ~"
    for what, setting, raw in (("the workspace", "VESTIBULE_WORKSPACE", limits.workspace_dir),
                               ("the rootfs directory", "VESTIBULE_ROOTFS_DIR", limits.rootfs_dir)):
        try:
            other = Path(raw).expanduser()
        except RuntimeError:  # its own first use fails and says so
            continue
        if _overlap(audit, other):
            return (f"the audit directory ({audit}, VESTIBULE_AUDIT_DIR) and {what} ({other}, "
                    f"{setting}) overlap; point one of them somewhere else")
    return None


def _overlap(a: Path, b: Path) -> bool:
    """The same dir, or one inside the other. Case is folded on every platform: macOS's
    normcase keeps it, though its default file system ignores it. Folding refuses only a Linux
    pair that differs by case alone, the safe direction. As rootfs.overlap_problem, which this
    module can't import (rootfs imports config)."""
    x, y = (os.path.normcase(_resolved(p)).casefold() for p in (a, b))
    return x == y or _inside(x, y) or _inside(y, x)


def _resolved(path: Path) -> str:
    """The real path, or the path as written when it can't be resolved now. This runs at the
    server's import, where an error would stop the server: Windows' realpath raises for an
    offline network drive (WinError 1231), and POSIX's for a relative path once the cwd is gone.
    normpath touches nothing, not even the cwd. (realpath can still block on a dead hard NFS
    mount; the plan's as-built section records that.)"""
    try:
        return os.path.realpath(path)
    except OSError:
        return os.path.normpath(path)


def _inside(child: str, parent: str) -> bool:
    try:
        return os.path.commonpath([child, parent]) == parent
    except ValueError:  # different drives on Windows
        return False
