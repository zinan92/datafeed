"""Provider protocol — every data source implements this."""

from __future__ import annotations

from typing import Protocol

from kline.models import Candle, Timeframe


class Provider(Protocol):
    """Fetch OHLCV candles from an external source."""

    async def fetch(
        self,
        ticker: str,
        timeframe: Timeframe,
        *,
        start: str | None = None,
        end: str | None = None,
        limit: int = 500,
    ) -> list[Candle]:
        """Fetch candles. Raises ProviderError on failure."""
        ...

    def supported_timeframes(self) -> list[Timeframe]:
        """Which timeframes this provider supports."""
        ...


class ProviderError(Exception):
    """Base error for all provider failures."""

    def __init__(self, message: str, *, suggestions: list[str] | None = None) -> None:
        super().__init__(message)
        self.suggestions = suggestions or []
        self.code = "provider_error"


class EntitlementBlocked(ProviderError):
    """The source is unavailable because the operator has no valid entitlement."""

    def __init__(self, message: str = "data source is blocked_for_entitlement") -> None:
        super().__init__(message)
        self.code = "blocked_for_entitlement"
