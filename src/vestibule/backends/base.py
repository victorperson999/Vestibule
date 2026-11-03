"""Warden interface + result type. Server depends on this abstraction, not on any impl."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from vestibule.config import Limits


class RunRefusedError(Exception):
    """Run refused before any code executed (e.g. concurrency limit reached).

    Nothing ran, so there is no RunResult to fabricate; the server catches this
    and returns the message as `Blocked:` content (handlers never raise).
    Step 5 reuses it for hard-tier capability failures.
    """


@dataclass
class RunResult:
    stdout: str
    stderr: str
    exit_code: int
    timed_out: bool
    cpu_ms: int | None = None
    mem_peak_mb: int | None = None
    pids_peak: int | None = None
    # nonzero only: "tasks_refused"; "cpu_throttled_periods" with "cpu_periods"
    limit_hits: dict[str, int] = field(default_factory=dict)
    # what this result's usage line lacks, and why; None when nothing is missing
    not_measured: str | None = None
    denied_syscalls: list[str] = field(default_factory=list)
    # "none" | "container" | "container-degraded" | "native" | "native-degraded"
    isolation: str = "none"
    # per-control status when degraded, e.g. "limits not applied: memory, pids"
    isolation_detail: str | None = None


class Warden(ABC):
    """Runs code in *some* level of isolation and reports honestly what it applied."""

    @abstractmethod
    async def run(self, language: str, code: str, timeout_s: int, limits: Limits) -> RunResult:
        ...

    async def drain(self, timeout: float | None = None) -> bool:
        """Wait for detached background work, never cancelling it; True when none is left.

        Backends without detached work (naive) have nothing to wait for.
        """
        return True
