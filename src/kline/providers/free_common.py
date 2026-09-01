"""Small shared helpers for the no-membership provider adapters."""

from __future__ import annotations

from datetime import datetime, timezone


def requested_cutoff(end: str | None) -> datetime:
    """Use the ingestion request boundary for deterministic derived bars."""

    if end:
        parsed = datetime.fromisoformat(end.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    return datetime.now(timezone.utc)
