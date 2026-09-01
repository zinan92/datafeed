"""Small shared helpers for the no-membership provider adapters."""

from __future__ import annotations

from datetime import datetime, timezone
import asyncio
import time


def requested_cutoff(end: str | None) -> datetime:
    """Use the ingestion request boundary for deterministic derived bars."""

    if end:
        parsed = datetime.fromisoformat(end.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    return datetime.now(timezone.utc)


class RequestPacer:
    """Keep consecutive public-source requests at least N seconds apart."""

    def __init__(self, interval_seconds: float = 0.25) -> None:
        if interval_seconds < 0:
            raise ValueError("request interval must be non-negative")
        self._interval_seconds = interval_seconds
        self._next_allowed = 0.0

    async def wait(self) -> None:
        now = time.monotonic()
        delay = self._next_allowed - now
        if delay > 0:
            await asyncio.sleep(delay)
        self._next_allowed = time.monotonic() + self._interval_seconds
