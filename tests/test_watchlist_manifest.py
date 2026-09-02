from pathlib import Path

import pytest

from kline.mvp_manifest import ManifestError
from kline.watchlist_manifest import load_watchlist_manifest


MANIFEST_PATH = Path(__file__).parents[1] / "configs" / "watchlist_manifest.json"


def test_watchlist_manifest_loads_exact_approved_daily_universe() -> None:
    manifest = load_watchlist_manifest(MANIFEST_PATH)

    assert manifest.version == "watchlist_universe_v1"
    assert len(manifest.instruments) == 42
    assert all(item.instrument_id.startswith("WATCH.") for item in manifest.instruments)
    assert all(item.required_timeframes == ("1d",) for item in manifest.instruments)
    assert all(
        set(item.not_applicable_timeframes) == {"15m", "1h", "4h", "1w"}
        for item in manifest.instruments
    )
    assert "051505" not in {item.display_symbol for item in manifest.instruments}
    hynix = next(item for item in manifest.instruments if item.display_symbol == "000660.KS")
    assert hynix.source_status == "configured"
    assert hynix.source_id == "yahoo_finance"
    assert hynix.calendar_id == "kr_equities"


def test_watchlist_manifest_reuses_required_field_validation(tmp_path: Path) -> None:
    invalid = tmp_path / "watchlist.json"
    invalid.write_text('{"version":"watchlist_universe_v1","instruments":[{}]}')

    with pytest.raises(ManifestError, match="instrument\[0\].universe"):
        load_watchlist_manifest(invalid)
