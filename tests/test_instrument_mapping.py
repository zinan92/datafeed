from kline.models import AssetClass
from kline.provenance import source_manifest


def test_gold_has_one_canonical_id_across_provider_symbols():
    binance = source_manifest("binance_usdm_futures", AssetClass.COMMODITY)
    yahoo = source_manifest("yahoo_finance_futures", AssetClass.COMMODITY)

    assert binance.canonical_ticker("GOLD") == "XAUUSDT"
    assert yahoo.canonical_ticker("GOLD") == "GC=F"
    assert binance.canonical_instrument_id("XAUUSDT") == "GOLD"
    assert yahoo.canonical_instrument_id("GC=F") == "GOLD"
