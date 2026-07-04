"""Configuration: resource limits, allowed languages, and M1 backend settings.

Everything is overridable via VESTIBULE_* environment variables (see Limits.from_env).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ALLOWED_LANGUAGES: tuple[str, ...] = ("python", "bash", "node")

# M1 step 6 pins these by digest captured from a fresh `docker pull` + `docker inspect`
# (never invented, never :latest). Tag-only until that step lands.
DEFAULT_IMAGE_PYTHON = "python:3.12-slim"
DEFAULT_IMAGE_NODE = "node:22-slim"


@dataclass(frozen=True)
class Limits:
    max_timeout_s: int = 60           # server clamps requests to this ceiling
    default_timeout_s: int = 10
    max_code_bytes: int = 256 * 1024  # reject giant payloads before the warden
    max_output_bytes: int = 20_000    # truncate guest output to protect model context
    mem_mb: int = 256                 # (used by container/native backends)
    pids_max: int = 128
    cpu_pct: int = 75

    # M1: workspace + container backend (docs/plans/M1-container-backend.md §8)
    workspace_dir: str = "~/.vestibule/workspace"
    workspace_ro: bool = False
    runtime: str = "auto"             # auto | docker | podman
    backend: str = "auto"             # auto | naive (naive = explicit dev-only opt-in)
    image_python: str = DEFAULT_IMAGE_PYTHON
    image_node: str = DEFAULT_IMAGE_NODE
    max_concurrent: int = 4
    tmpfs_mb: int = 64

    @property
    def workspace_path(self) -> Path:
        return Path(self.workspace_dir).expanduser()

    @classmethod
    def from_env(cls) -> "Limits":
        def _i(name: str, default: int) -> int:
            v = os.environ.get(name)
            return int(v) if v and v.isdigit() else default

        def _s(name: str, default: str) -> str:
            v = os.environ.get(name)
            return v if v else default

        def _b(name: str) -> bool:
            return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")

        return cls(
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
        )
