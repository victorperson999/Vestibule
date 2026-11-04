"""Shared fixtures. Every test gets an in-memory audit log in place of the server's real one
(docs/plans/M3/M3-step2-audit-writer.md §2.6): no ordinary test writes to the user's audit
folder, waits on the writer thread, or depends on a disk's timing. The writer's own tests
(test_audit.py) build real AuditLogs in temp dirs.
"""
from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Any

import pytest

import vestibule.server as server


class RecordingAudit:
    """Keeps every record in `records`, cancelled calls' included. Each submit is already
    confirmed; `confirm` returns `note`, which a test may set."""

    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []
        self.note: str | None = None

    def submit(self, record: dict[str, Any]) -> asyncio.Future[str | None]:
        self.records.append(record)
        fut: asyncio.Future[str | None] = asyncio.get_running_loop().create_future()
        fut.set_result(None)
        return fut

    async def confirm(self, *futs: asyncio.Future[str | None]) -> str | None:
        return self.note


@pytest.fixture(autouse=True)
def recording_audit() -> Iterator[RecordingAudit]:
    # Not monkeypatch: an autouse fixture that requests it sets monkeypatch up first, so its
    # undo would run after every other fixture's teardown, and test_native's scope check
    # would then run with a test's PATH still patched.
    rec, saved = RecordingAudit(), server.AUDIT
    server.AUDIT = rec  # type: ignore[assignment]
    yield rec
    server.AUDIT = saved
