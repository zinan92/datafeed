"""Shared timestamp parsing helpers for read-only health views."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def parse_utc_timestamp(value: Any) -> datetime | None:
    """Parse an ISO timestamp and normalize it to UTC; invalid values return None."""

    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
