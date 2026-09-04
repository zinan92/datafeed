from __future__ import annotations

from ops.watchlist_commodity_catchup import COMMODITY_INSTRUMENT_IDS


def test_commodity_catchup_is_fixed_to_the_three_post_close_instruments() -> None:
    assert COMMODITY_INSTRUMENT_IDS == (
        "WATCH.CROSS.GOLD",
        "WATCH.CROSS.SILVER",
        "WATCH.CROSS.WTI",
    )
