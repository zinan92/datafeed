from types import SimpleNamespace

from ops.verify_watchlist_e2e import _diff, _matrix_probe


def _instrument(instrument_id: str):
    return SimpleNamespace(
        instrument_id=instrument_id,
        display_symbol=instrument_id,
        asset_class="us_stock",
        provider_symbol=instrument_id,
        source_id="yahoo_finance",
    )


def test_diff_returns_added_and_removed_instrument_ids() -> None:
    before = SimpleNamespace(instruments=(_instrument("WATCH.US.OLD"), _instrument("WATCH.US.KEEP")))
    after = SimpleNamespace(instruments=(_instrument("WATCH.US.KEEP"), _instrument("WATCH.US.NEW")))

    removed, added = _diff(before, after)

    assert [item.instrument_id for item in removed] == ["WATCH.US.OLD"]
    assert [item.instrument_id for item in added] == ["WATCH.US.NEW"]


def test_matrix_probe_requires_ready_watchlist_daily_cell(monkeypatch) -> None:
    monkeypatch.setattr(
        "ops.verify_watchlist_e2e._get_json",
        lambda url, timeout: (200, {"cells": [{"dataset": "watchlist", "instrument_id": "WATCH.US.X", "timeframe": "1d", "status": "ready"}]}),
    )

    result = _matrix_probe("http://127.0.0.1:8100", _instrument("WATCH.US.X"), timeout=1)

    assert result["status"] == "pass"
    assert result["cell_status"] == "ready"


def test_matrix_probe_fails_for_missing_cell(monkeypatch) -> None:
    monkeypatch.setattr(
        "ops.verify_watchlist_e2e._get_json",
        lambda url, timeout: (200, {"cells": []}),
    )

    result = _matrix_probe("http://127.0.0.1:8100", _instrument("WATCH.US.X"), timeout=1)

    assert result["status"] == "fail"
    assert "cell_missing" in result["issues"]
