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
    # M3 A6, for the audit: False only when the backend knows nothing executed; the backend's own
    # id for the run (the container `vestibule-<id>`, the scope `vestibule-<id>.scope`, the
    # `run <id>:` log lines); the image the run used (a runtime image id, or a native manifest
    # digest); the bytes collected from the run's output streams before decoding, without any
    # notes the backend appends (for a container these are the runtime CLI's streams, so its own
    # errors count too, as on exit 125; native and container stop at twice max_output_bytes;
    # naive has no cap; None when nothing was collected); whether collection hit that cap and
    # stopped the sandbox (W3)
    ran: bool = True
    run_id: str | None = None
    image: str | None = None
    stdout_bytes: int | None = None
    stderr_bytes: int | None = None
    output_capped: bool = False


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
