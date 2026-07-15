from pathlib import Path

from kline.api import compare_sources
from kline.models import AssetClass, Candle, Timeframe
from kline.store import KlineStore


async def test_compare_sources_keeps_series_separate(monkeypatch, tmp_path: Path):
    store = KlineStore(str(tmp_path / "compare.db"))
    store.save(
        "XAUUSDT",
        AssetClass.COMMODITY,
        Timeframe.MIN_5,
        [Candle(timestamp="2026-07-15T10:00:00Z", open=100, high=101, low=99, close=100, volume=1)],
        source_id="binance_usdm_futures",
    )
    store.save(
        "GC=F",
        AssetClass.COMMODITY,
        Timeframe.MIN_5,
        [Candle(timestamp="2026-07-15T10:00:00Z", open=101, high=102, low=100, close=101, volume=1)],
        source_id="yahoo_finance_futures",
    )
    monkeypatch.setattr("kline.api.get_store", lambda: store)

    result = await compare_sources(
        AssetClass.COMMODITY,
        "GOLD",
        timeframe=Timeframe.MIN_5,
        sources=["binance_usdm_futures", "yahoo_finance_futures"],
        limit=10,
    )
    assert result["instrument_id"] == "GOLD"
    assert result["provider_symbols"] == {
        "binance_usdm_futures": "XAUUSDT",
        "yahoo_finance_futures": "GC=F",
    }
    assert result["overlap_count"] == 1
    assert result["max_close_deviation_pct"] == 1.0
    assert result["is_blended"] is False
