"""Tests for provider logic (unit tests, no network calls)."""

from kline.providers.base import ProviderError
from kline.providers.commodity import _resolve_ticker


class TestCommodityAliases:
    def test_resolve_gold(self):
        assert _resolve_ticker("GOLD") == "GC=F"
        assert _resolve_ticker("gold") == "GC=F"
        assert _resolve_ticker("XAUUSD") == "GC=F"

    def test_resolve_oil(self):
        assert _resolve_ticker("OIL") == "CL=F"
        assert _resolve_ticker("CRUDE") == "CL=F"
        assert _resolve_ticker("WTI") == "CL=F"

    def test_passthrough_unknown(self):
        assert _resolve_ticker("GC=F") == "GC=F"
        assert _resolve_ticker("RANDOM") == "RANDOM"


class TestProviderError:
    def test_error_with_suggestions(self):
        err = ProviderError("test error", suggestions=["try this", "or that"])
        assert str(err) == "test error"
        assert err.suggestions == ["try this", "or that"]

    def test_error_without_suggestions(self):
        err = ProviderError("test error")
        assert err.suggestions == []
