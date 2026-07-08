"""Tests for provenance metadata + freshness computation."""

from datetime import datetime, timezone

from kline.models import AssetClass, Timeframe
from kline.provenance import freshness, provider_meta


class TestProviderMeta:
    def test_every_asset_class_has_metadata(self):
        for asset_class in AssetClass:
            meta = provider_meta(asset_class)
            assert meta.name
            assert meta.source_mode

    def test_every_source_is_flagged_research_only(self):
        # No kline source is an execution venue for a live order loop; the flag
        # is the durable guard against a consumer trusting it as one.
        for asset_class in AssetClass:
            assert "research_only" in provider_meta(asset_class).quality_flags

    def test_crypto_is_spot_and_not_execution_venue(self):
        meta = provider_meta(AssetClass.CRYPTO)
        assert meta.name == "binance_spot"
        assert "not_execution_venue" in meta.quality_flags
        assert meta.continuous is True

    def test_market_hours_sources_are_not_continuous(self):
        for asset_class in (AssetClass.US_STOCK, AssetClass.COMMODITY, AssetClass.A_SHARE):
            assert provider_meta(asset_class).continuous is False


class TestFreshness:
    def test_continuous_recent_bar_is_fresh(self):
        meta = provider_meta(AssetClass.CRYPTO)
        now = datetime(2026, 3, 28, 12, 0, 0, tzinfo=timezone.utc)
        latest = "2026-03-28T11:59:00"  # 1 minute old
        age, max_age, fresh = freshness(latest, meta, Timeframe.MIN_1, now=now)
        assert age == 60.0
        assert max_age == 180.0  # 3 × 60s
        assert fresh is True

    def test_continuous_stale_bar_is_not_fresh(self):
        meta = provider_meta(AssetClass.CRYPTO)
        now = datetime(2026, 3, 28, 12, 0, 0, tzinfo=timezone.utc)
        latest = "2026-03-28T11:50:00"  # 10 minutes old, threshold is 3 min
        age, max_age, fresh = freshness(latest, meta, Timeframe.MIN_1, now=now)
        assert age == 600.0
        assert fresh is False

    def test_market_hours_source_never_asserts_fresh(self):
        # Wall-clock freshness is meaningless when the market is closed; we must
        # not guess a verdict. age is still reported as an honest fact.
        meta = provider_meta(AssetClass.US_STOCK)
        now = datetime(2026, 3, 30, 12, 0, 0, tzinfo=timezone.utc)  # a Monday
        age, max_age, fresh = freshness("2026-03-27", meta, Timeframe.DAY, now=now)
        assert age is not None and age > 0
        assert max_age is None
        assert fresh is None

    def test_unparseable_timestamp_returns_all_none(self):
        meta = provider_meta(AssetClass.CRYPTO)
        assert freshness("not-a-date", meta, Timeframe.MIN_1) == (None, None, None)
