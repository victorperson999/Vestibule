"""Warden interface + result type. Server depends on this abstraction, not on any impl."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from vestibule.config import Limits


@dataclass
class RunResult:
    stdout: str
    stderr: str
    exit_code: int
    timed_out: bool
    cpu_ms: int | None = None
    mem_peak_mb: int | None = None
    denied_syscalls: list[str] = field(default_factory=list)
    isolation: str = "none"           # "none" | "container" | "namespaces-only" | "native"


class Warden(ABC):
    """Runs code in *some* level of isolation and reports honestly what it applied."""

    @abstractmethod
    async def run(self, language: str, code: str, timeout_s: int, limits: Limits) -> RunResult:
        ...
