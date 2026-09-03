from __future__ import annotations

from ops.verify_consumer_cutover import compare_snapshots, consumer_requests


def test_consumer_request_set_matches_enumerated_runtime_contract() -> None:
    requests = consumer_requests()
    assert len(requests) == 69
    assert sum(item.consumer == "newsletter" for item in requests) == 28
    assert sum(
        item.consumer == "human_review" and not item.conditional for item in requests
    ) == 34
    assert sum(item.conditional for item in requests) == 7
    assert not any(item.ticker in {"DGS2", "DGS10", "T10Y2Y", "MES=F", "MNQ=F"} for item in requests)


def test_snapshot_comparison_allows_only_pre_normalized_backend_metadata() -> None:
    baseline = {
        "cutoff": "2026-09-03T10:00:00+00:00",
        "requests": [
            {
                "request_id": "one",
                "http_status": 200,
                "normalized_sha256": "a",
                "candles_sha256": "b",
                "candle_count": 2,
            }
        ],
    }
    candidate = {
        "cutoff": baseline["cutoff"],
        "requests": [
            {
                "request_id": "one",
                "http_status": 200,
                "normalized_sha256": "a",
                "candles_sha256": "b",
                "candle_count": 2,
                "query_served_from": "market_data_database",
            }
        ],
    }
    result = compare_snapshots(baseline, candidate)
    assert result["difference_count"] == 0
    assert result["exact_match_count"] == 1
    assert result["market_database_requests"] == 1
