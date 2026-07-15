from kline.models import Timeframe
from kline.providers.tiger import TigerOpenFuturesAdapter


class FakeQuoteClient:
    def get_future_bars(self, contracts, **kwargs):
        assert contracts == ["MGCmain"]
        assert kwargs["period"] == "1m"
        return [
            {
                "identifier": "MGCmain",
                "time": 1784080800000,
                "open": 4060.0,
                "high": 4062.0,
                "low": 4059.0,
                "close": 4061.0,
                "volume": 12,
            }
        ]

    def get_future_trading_times(self, contract, trading_date=None):
        assert contract == "MGCmain"
        assert trading_date == "2026-07-15"
        return [
            {
                "start": 1784080800000,
                "end": 1784084400000,
                "trading": True,
                "bidding": False,
                "zone": "UTC",
            }
        ]


async def test_tiger_adapter_normalizes_quote_rows_without_execution_client():
    adapter = TigerOpenFuturesAdapter(
        {"contract": "MGCmain", "exchange": "COMEX"},
        quote_client=FakeQuoteClient(),
    )
    candles = await adapter.fetch_candles("MGC", Timeframe.MIN_1, limit=10)

    assert adapter.manifest.source_id == "tiger_openapi_comex"
    assert adapter.manifest.meta.execution_venue is True
    assert adapter.manifest.canonical_instrument_id("MGCmain") == "MGC_CONTINUOUS"
    assert candles[0].close == 4061.0
    assert candles[0].timestamp == "2026-07-15T02:00:00+00:00"
    assert adapter.last_raw_response["response_body"][0]["identifier"] == "MGCmain"


async def test_tiger_adapter_owns_market_sessions():
    adapter = TigerOpenFuturesAdapter(
        {"contract": "MGCmain", "exchange": "COMEX"},
        quote_client=FakeQuoteClient(),
    )

    result = await adapter.fetch_market_sessions("MGC", trading_date="2026-07-15")

    assert result["schema_version"] == "market-sessions-v1"
    assert result["instrument_id"] == "MGC_CONTINUOUS"
    assert len(result["trading_windows"]) == 1
