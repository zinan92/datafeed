"""OKX history: paged, resumable, OKX-only, and it names what is missing (datafeed#184)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import httpx
import pytest

from kline.models import AssetClass, Timeframe
from kline.okx_history import backfill, day_windows, report_path
from kline.providers.okx_swap import SOURCE_MODE, OkxSwapProvider
from kline.store import KlineStore

NOW = datetime(2026, 11, 5, 3, 10, 30, tzinfo=timezone.utc)
END = NOW.replace(second=0)


def ms(moment: datetime) -> int:
    return int(moment.timestamp() * 1000)


class FakeOkx:
    """OKX paging: newest first, ``after`` is exclusive, ``limit`` rows a page."""

    def __init__(self, days: float = 2) -> None:
        minutes = int(days * 1440) + 5
        self.bars = {ms(END - timedelta(minutes=i)): "1" for i in range(1, minutes)}
        self.bars[ms(END)] = "0"  # the forming minute
        self.funding = [ms(END - timedelta(hours=8 * i)) for i in range(250)]
        self.fail_after: set[int] = set()
        self.refuse_first = 0
        self.history_calls = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        params = request.url.params
        limit = int(params["limit"])
        after = int(params["after"]) if "after" in params else None
        if request.url.path.endswith("history-candles"):
            self.history_calls += 1
            if self.refuse_first > 0:
                self.refuse_first -= 1
                return httpx.Response(429, json={"code": "50011", "msg": "Too Many Requests"})
            if after in self.fail_after:
                return httpx.Response(500, json={"code": "50001", "msg": "down"})
            stamps = sorted((t for t in self.bars if after is None or t < after), reverse=True)[:limit]
            rows = [[str(t), "1", "3", "0.5", "2", "10", "0.01", "20", self.bars[t]] for t in stamps]
            return httpx.Response(200, json={"code": "0", "msg": "", "data": rows})
        assert request.url.path.endswith("funding-rate-history")
        stamps = sorted((t for t in self.funding if after is None or t < after), reverse=True)[:limit]
        rows = [{"instId": "XAU-USDT-SWAP", "fundingTime": str(t), "fundingRate": "0.0001", "realizedRate": "0.00011", "method": "current_period"} for t in stamps]
        return httpx.Response(200, json={"code": "0", "msg": "", "data": rows})


async def no_wait(_seconds: float) -> None:
    return None


async def run(okx: FakeOkx, store: KlineStore, path: Path, days: int = 2, **kwargs) -> dict:
    provider = OkxSwapProvider(transport=httpx.MockTransport(okx.handler))
    try:
        return await backfill(provider, store, path=path, days=days, now=kwargs.pop("now", NOW), sleep=no_wait)
    finally:
        await provider.aclose()


@pytest.fixture
def store(tmp_path: Path) -> KlineStore:
    return KlineStore(str(tmp_path / "kline.db"))


@pytest.fixture
def path(tmp_path: Path) -> Path:
    return report_path(tmp_path / "kline.db")


def stored(store: KlineStore) -> list:
    return store.query("XAU-USDT-SWAP", AssetClass.COMMODITY, Timeframe.MIN_1, source_id=SOURCE_MODE, limit=60000)


def test_days_are_cut_at_utc_midnight_newest_first() -> None:
    windows = day_windows(END - timedelta(days=1), END)
    midnight = datetime(2026, 11, 5, tzinfo=timezone.utc)
    assert windows == [(midnight, END), (END - timedelta(days=1), midnight)]


@pytest.mark.asyncio
async def test_pages_past_one_response_and_skips_the_forming_minute(store: KlineStore, path: Path) -> None:
    report = await run(FakeOkx(), store, path)
    bars = stored(store)
    assert len(bars) == 2880 == report["expected_bars"] == report["bar_count"]
    assert report["gaps"] == [] and report["missing_minutes"] == 0 and report["fetch_failures"] == {}
    assert datetime.fromisoformat(bars[-1].timestamp).replace(tzinfo=timezone.utc) == END - timedelta(minutes=1)
    assert json.loads(path.read_text(encoding="utf-8"))["schema_version"] == "okx-history-report-v1"


@pytest.mark.asyncio
async def test_a_second_run_fetches_only_the_new_minutes(store: KlineStore, path: Path) -> None:
    okx = FakeOkx()
    await run(okx, store, path)
    okx.history_calls = 0
    okx.bars.pop(ms(END), None)
    later = END + timedelta(minutes=2)
    okx.bars.update({ms(END): "1", ms(END + timedelta(minutes=1)): "1"})
    report = await run(okx, store, path, now=later)
    assert okx.history_calls == 2  # today's 192 minutes, two pages; yesterday is complete and skipped
    assert report["missing_minutes"] == 0


@pytest.mark.asyncio
async def test_a_failed_day_is_named_and_the_next_run_finishes_it(store: KlineStore, path: Path) -> None:
    okx = FakeOkx()
    midnight = datetime(2026, 11, 4, tzinfo=timezone.utc)
    okx.fail_after = {ms(midnight)}  # the window that ends at 11/4 00:00 cannot start
    first = await run(okx, store, path)
    assert list(first["fetch_failures"]) == [(END - timedelta(days=2)).isoformat()]
    assert {gap["cause"] for gap in first["gaps"]} == {"fetch_failed"}
    assert first["gaps"][0]["minutes"] == first["missing_minutes"] > 0
    okx.fail_after = set()
    second = await run(okx, store, path)
    assert second["missing_minutes"] == 0 and second["fetch_failures"] == {}


@pytest.mark.asyncio
async def test_a_minute_okx_does_not_have_stays_missing(store: KlineStore, path: Path) -> None:
    okx = FakeOkx()
    hole = [END - timedelta(minutes=m) for m in (100, 101, 102, 500)]
    for moment in hole:
        del okx.bars[ms(moment)]
    report = await run(okx, store, path)
    assert report["missing_minutes"] == 4 and len(stored(store)) == 2876
    assert [(gap["minutes"], gap["cause"]) for gap in report["gaps"]] == [(1, "venue_no_bar"), (3, "venue_no_bar")]
    assert report["gaps"][1]["start"] == hole[2].isoformat() and report["gaps"][1]["end"] == hole[0].isoformat()


@pytest.mark.asyncio
async def test_a_gap_across_midnight_is_one_run(store: KlineStore, path: Path) -> None:
    okx = FakeOkx()
    midnight = datetime(2026, 11, 5, tzinfo=timezone.utc)
    for minute in (-1, 0):
        del okx.bars[ms(midnight + timedelta(minutes=minute))]
    report = await run(okx, store, path)
    assert [gap["minutes"] for gap in report["gaps"]] == [2]


@pytest.mark.asyncio
async def test_a_refused_page_is_retried(store: KlineStore, path: Path) -> None:
    okx = FakeOkx()
    okx.refuse_first = 3
    report = await run(okx, store, path)
    assert report["fetch_failures"] == {} and report["missing_minutes"] == 0


@pytest.mark.asyncio
async def test_funding_keeps_the_raw_row_and_reports_where_it_starts(store: KlineStore, path: Path) -> None:
    okx = FakeOkx()
    report = await run(okx, store, path)
    rows = store.query_funding(SOURCE_MODE, "XAU-USDT-SWAP")
    assert len(rows) == 250 == report["funding"]["count"]
    assert rows[0] == {"funding_time": (END - timedelta(hours=8 * 249)).isoformat(), "funding_rate": 0.0001, "realized_rate": 0.00011}
    assert report["funding"]["first"] == rows[0]["funding_time"] and report["funding"]["error"] is None
    again = await run(okx, store, path)
    assert again["funding"]["count"] == 250


@pytest.mark.asyncio
async def test_bars_land_only_under_the_okx_source(store: KlineStore, path: Path) -> None:
    await run(FakeOkx(), store, path)
    assert store.count("XAU-USDT-SWAP", AssetClass.COMMODITY, Timeframe.MIN_1, source_id="binance_usdm_futures") == 0
    assert store.count("XAU-USDT-SWAP", AssetClass.COMMODITY, Timeframe.MIN_1, source_id=SOURCE_MODE) == 2880


def test_routes_serve_the_report_and_funding_and_refuse_anything_else(store: KlineStore, tmp_path: Path, monkeypatch) -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from kline import api

    monkeypatch.setenv("KLINE_DB_PATH", str(tmp_path / "kline.db"))
    monkeypatch.setattr(api, "get_store", lambda: store)
    app = FastAPI()
    app.include_router(api.router, prefix="/api")
    client = TestClient(app)
    base = "/api/history/okx_swap_public/XAU-USDT-SWAP"
    assert client.get(f"{base}/report").status_code == 404
    report_path(tmp_path / "kline.db").parent.mkdir()
    report_path(tmp_path / "kline.db").write_text('{"schema_version": "okx-history-report-v1"}')
    assert client.get(f"{base}/report").json()["schema_version"] == "okx-history-report-v1"
    store.save_funding(SOURCE_MODE, "XAU-USDT-SWAP", [{"funding_time": "2026-11-05T00:00:00+00:00", "funding_rate": 0.1, "realized_rate": None, "raw": "{}"}])
    assert client.get(f"{base}/funding").json()["funding"][0]["funding_rate"] == 0.1
    assert client.get("/api/history/binance_usdm_futures/XAU-USDT-SWAP/report").status_code == 404
    assert client.get("/api/history/okx_swap_public/BTC-USDT-SWAP/funding").status_code == 404


@pytest.mark.asyncio
async def test_btc_is_kept_under_the_crypto_source_with_its_own_report(store: KlineStore, tmp_path: Path) -> None:
    from kline.okx_history import CRYPTO_SOURCE

    okx = FakeOkx()
    provider = OkxSwapProvider(transport=httpx.MockTransport(okx.handler), allowed_symbols={"XAU-USDT-SWAP", "BTC-USDT-SWAP"})
    btc_path = report_path(tmp_path / "kline.db", "BTC-USDT-SWAP")
    try:
        report = await backfill(provider, store, path=btc_path, instrument="BTC-USDT-SWAP", days=2, now=NOW, sleep=no_wait)
    finally:
        await provider.aclose()
    assert btc_path.name == "okx_swap_crypto_public_BTC-USDT-SWAP.json" and json.loads(btc_path.read_text())["source"] == CRYPTO_SOURCE
    assert report["missing_minutes"] == 0 and report["funding"]["count"] == 250
    assert store.count("BTC-USDT-SWAP", AssetClass.CRYPTO, Timeframe.MIN_1, source_id=CRYPTO_SOURCE) == 2880
    assert store.count("BTC-USDT-SWAP", AssetClass.COMMODITY, Timeframe.MIN_1, source_id=SOURCE_MODE) == 0
    assert len(store.query_funding(CRYPTO_SOURCE, "BTC-USDT-SWAP")) == 250 and store.query_funding(SOURCE_MODE, "BTC-USDT-SWAP") == []
    assert report_path(tmp_path / "kline.db").name == "okx_swap_public_XAU-USDT-SWAP.json"  # gold's file did not move
    with pytest.raises(ValueError):
        report_path(tmp_path / "kline.db", "SOL-USDT-SWAP")


def test_btc_routes_answer_only_under_the_crypto_source(store: KlineStore, tmp_path: Path, monkeypatch) -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from kline import api

    monkeypatch.setenv("KLINE_DB_PATH", str(tmp_path / "kline.db"))
    monkeypatch.setattr(api, "get_store", lambda: store)
    app = FastAPI()
    app.include_router(api.router, prefix="/api")
    client = TestClient(app)
    path = report_path(tmp_path / "kline.db", "BTC-USDT-SWAP")
    path.parent.mkdir()
    path.write_text('{"instrument": "BTC-USDT-SWAP"}')
    assert client.get("/api/history/okx_swap_crypto_public/BTC-USDT-SWAP/report").json() == {"instrument": "BTC-USDT-SWAP"}
    assert client.get("/api/history/okx_swap_public/BTC-USDT-SWAP/report").status_code == 404
    assert client.get("/api/history/okx_swap_crypto_public/XAU-USDT-SWAP/report").status_code == 404


@pytest.mark.asyncio
async def test_stored_btc_bars_are_served_the_way_the_backtest_reads_them(store: KlineStore, tmp_path: Path, monkeypatch) -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from kline import api

    provider = OkxSwapProvider(transport=httpx.MockTransport(FakeOkx().handler), allowed_symbols={"BTC-USDT-SWAP"})
    try:
        await backfill(provider, store, path=report_path(tmp_path / "kline.db", "BTC-USDT-SWAP"), instrument="BTC-USDT-SWAP", days=2, now=NOW, sleep=no_wait)
    finally:
        await provider.aclose()
    monkeypatch.setattr(api, "get_store", lambda: store)
    app = FastAPI()
    app.include_router(api.router, prefix="/api")
    response = TestClient(app).get("/api/candles/crypto/BTC-USDT-SWAP", params={
        "timeframe": "1m", "limit": 20000, "source": "okx_swap_crypto_public", "cache_policy": "require", "quality": "standard",
        "fallback_policy": "none", "start": (END - timedelta(hours=1)).isoformat(), "end": (END - timedelta(seconds=1)).isoformat()})
    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body["candles"]) == 60
