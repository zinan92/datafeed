"""Small, independent execution-market ingestion worker.

The worker owns only the ``execution_market_*`` tables in its database.  It
never falls back between venues and only promotes bars whose upstream close
time is already in the past.
"""
from __future__ import annotations

import argparse
import asyncio
import fcntl
import json
import math
import os
import sqlite3
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

import httpx

BINANCE_URL = "https://fapi.binance.com/fapi/v1/klines"
HYPERLIQUID_TESTNET_URL = "https://api.hyperliquid-testnet.xyz/info"
POLL_SECONDS = 5
STALE_SECONDS = 90
LAST_PRICE_STALE_SECONDS = 30

INSTRUMENTS = {
    "XAUUSDT.BINANCE": {"provider": "binance_usdm_futures", "venue": "binance", "environment": "production", "symbol": "XAUUSDT"},
    "BTC-USD-PERP.HYPERLIQUID": {"provider": "hyperliquid_perpetual", "venue": "hyperliquid", "environment": "testnet", "symbol": "BTC"},
}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_ms(value: Any) -> datetime:
    return datetime.fromtimestamp(float(value) / 1000, tz=timezone.utc)


@dataclass(frozen=True)
class ExecutionBar:
    instrument_id: str
    open_time: str
    close_time: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    provider: str
    venue: str
    environment: str
    fetched_at: str

    @property
    def age_seconds(self) -> float:
        return (datetime.fromisoformat(self.fetched_at.replace("Z", "+00:00")) -
                datetime.fromisoformat(self.close_time.replace("Z", "+00:00"))).total_seconds()

    @property
    def price_time(self) -> str:
        """The local fetch time is the quote timestamp for this provider."""
        return self.fetched_at


class ProviderUnavailable(RuntimeError):
    pass


def is_completed_bar(close_time: datetime, fetched_at: datetime) -> bool:
    return close_time <= fetched_at


def percentile95(values: Iterable[float]) -> float | None:
    ordered = sorted(float(v) for v in values)
    if not ordered:
        return None
    # nearest-rank p95, deterministic and conservative for small samples
    return ordered[max(0, math.ceil(len(ordered) * 0.95) - 1)]


def percentile50(values: Iterable[float]) -> float | None:
    ordered = sorted(float(v) for v in values)
    if not ordered:
        return None
    return ordered[max(0, math.ceil(len(ordered) * 0.50) - 1)]


def age_stats(values: Iterable[float]) -> dict[str, float | int | None]:
    ordered = sorted(float(v) for v in values)
    return {
        "p50": percentile50(ordered),
        "p95": percentile95(ordered),
        "max": max(ordered) if ordered else None,
        "samples": len(ordered),
    }


def next_aligned_run_delay(now: datetime, interval: int = POLL_SECONDS) -> float:
    """Return the delay to the next poll slot, starting one second after a minute."""
    interval = max(1, int(interval))
    epoch = now.timestamp()
    minute_start = math.floor(epoch / 60) * 60
    target = minute_start + 1
    while target <= epoch + 1e-9:
        target += interval
        if target >= minute_start + 60:
            target = minute_start + 61
            break
    return max(0.0, target - epoch)


def _bar(instrument_id: str, item: Any, fetched: datetime) -> ExecutionBar:
    meta = INSTRUMENTS[instrument_id]
    if meta["venue"] == "binance":
        open_ms, close_ms = item[0], item[6]
        values = (item[1], item[2], item[3], item[4], item[5])
    else:
        open_ms, close_ms = item["t"], item.get("T", item["t"] + 59999)
        values = (item["o"], item["h"], item["l"], item["c"], item["v"])
    numbers = tuple(float(v) for v in values)
    if not all(math.isfinite(v) for v in numbers) or numbers[4] < 0:
        raise ProviderUnavailable("upstream returned invalid OHLCV")
    return ExecutionBar(instrument_id, iso(parse_ms(open_ms)), iso(parse_ms(close_ms)),
                        *numbers, meta["provider"], meta["venue"], meta["environment"], iso(fetched))


def fetch_binance(instrument_id: str, fetched: datetime, client: httpx.Client) -> list[ExecutionBar]:
    try:
        response = client.get(BINANCE_URL, params={"symbol": "XAUUSDT", "interval": "1m", "limit": 3})
        response.raise_for_status()
        data = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise ProviderUnavailable(str(exc)) from exc
    return [_bar(instrument_id, item, fetched) for item in data]


def fetch_hyperliquid(instrument_id: str, fetched: datetime, client: httpx.Client) -> list[ExecutionBar]:
    end_ms = int(fetched.timestamp() * 1000)
    payload = {"type": "candleSnapshot", "req": {"coin": "BTC", "interval": "1m", "startTime": end_ms - 180000, "endTime": end_ms}}
    try:
        response = client.post(HYPERLIQUID_TESTNET_URL, json=payload, headers={"User-Agent": "datafeed-execution-market/1.0"})
        response.raise_for_status()
        data = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise ProviderUnavailable(str(exc)) from exc
    if not isinstance(data, list):
        raise ProviderUnavailable("Hyperliquid returned a non-list payload")
    return [_bar(instrument_id, item, fetched) for item in data]


class ExecutionMarketStore:
    def __init__(self, path: str | Path):
        self.path = str(Path(path).expanduser())
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS execution_market_candles (
          instrument_id TEXT NOT NULL, open_time TEXT NOT NULL, close_time TEXT NOT NULL,
          open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL, close REAL NOT NULL,
          volume REAL NOT NULL, provider TEXT NOT NULL, venue TEXT NOT NULL,
          environment TEXT NOT NULL, fetched_at TEXT NOT NULL, PRIMARY KEY(instrument_id, close_time)
        );
        CREATE TABLE IF NOT EXISTS execution_market_forming_quotes (
          instrument_id TEXT PRIMARY KEY, price REAL NOT NULL, price_time TEXT NOT NULL,
          bar_open_time TEXT NOT NULL, bar_close_time TEXT NOT NULL,
          provider TEXT NOT NULL, venue TEXT NOT NULL, environment TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS execution_market_lag_receipts (
          run_id TEXT PRIMARY KEY, fetched_at TEXT NOT NULL, status TEXT NOT NULL,
          p95_age_seconds REAL, bar_count INTEGER NOT NULL, error TEXT
        );
        CREATE TABLE IF NOT EXISTS execution_market_read_age_samples (
          instrument_id TEXT NOT NULL, sampled_at TEXT NOT NULL,
          age_seconds REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS execution_market_last_age_samples (
          instrument_id TEXT NOT NULL, sampled_at TEXT NOT NULL,
          age_seconds REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS execution_market_events (
          id INTEGER PRIMARY KEY AUTOINCREMENT, event TEXT NOT NULL,
          instrument_id TEXT NOT NULL, occurred_at TEXT NOT NULL, detail TEXT
        );
        CREATE TABLE IF NOT EXISTS execution_market_provider_state (
          instrument_id TEXT PRIMARY KEY, unavailable_since TEXT
        );
        """)
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    def latest(self, instrument_id: str, *, limit: int = 240) -> list[ExecutionBar]:
        """Read only completed bars, newest first, for the execution API."""
        rows = self.db.execute(
            """SELECT instrument_id, open_time, close_time, open, high, low, close,
                      volume, provider, venue, environment, fetched_at
                 FROM execution_market_candles
                WHERE instrument_id=?
                ORDER BY close_time DESC LIMIT ?""",
            (instrument_id, max(1, int(limit))),
        ).fetchall()
        return [ExecutionBar(*row) for row in rows]

    def write(self, bars: list[ExecutionBar], *, run_id: str, fetched_at: datetime, sampled_at: datetime | None = None, status: str = "ok", error: str | None = None) -> dict[str, Any]:
        sampled_at = sampled_at or fetched_at
        with self.db:
            for bar in bars:
                self.db.execute("""INSERT INTO execution_market_candles VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                  ON CONFLICT(instrument_id,close_time) DO UPDATE SET open_time=excluded.open_time, open=excluded.open, high=excluded.high, low=excluded.low, close=excluded.close, volume=excluded.volume, fetched_at=excluded.fetched_at""", tuple(asdict(bar).values()))
            read_age: dict[str, dict[str, float | int | None]] = {}
            for instrument_id in INSTRUMENTS:
                latest = self.latest(instrument_id, limit=1)
                if not latest:
                    continue
                age = (sampled_at - datetime.fromisoformat(latest[0].close_time.replace("Z", "+00:00"))).total_seconds()
                self.db.execute(
                    "INSERT INTO execution_market_read_age_samples VALUES (?,?,?)",
                    (instrument_id, iso(sampled_at), age),
                )
                rows = self.db.execute(
                    "SELECT age_seconds FROM execution_market_read_age_samples WHERE instrument_id=?",
                    (instrument_id,),
                ).fetchall()
                read_age[instrument_id] = age_stats(row[0] for row in rows)
            fetch_age = {
                instrument_id: age_stats(bar.age_seconds for bar in bars if bar.instrument_id == instrument_id)
                for instrument_id in INSTRUMENTS
                if any(bar.instrument_id == instrument_id for bar in bars)
            }
            receipt = {"schema_version": "execution-market-lag-v1", "run_id": run_id, "fetched_at": iso(fetched_at), "status": status, "p95_age_seconds": percentile95(b.age_seconds for b in bars), "fetch_age_seconds": fetch_age, "bar_count": len(bars), "error": error, "read_age_seconds": read_age}
            receipt["read_age_p50_seconds"] = {key: value["p50"] for key, value in read_age.items()}
            receipt["read_age_p95_seconds"] = {key: value["p95"] for key, value in read_age.items()}
            receipt["read_age_max_seconds"] = {key: value["max"] for key, value in read_age.items()}
            self.db.execute("INSERT INTO execution_market_lag_receipts VALUES (?,?,?,?,?,?)", tuple(receipt[k] for k in ("run_id", "fetched_at", "status", "p95_age_seconds", "bar_count", "error")))
        return receipt

    def write_forming(self, bars: list[ExecutionBar], *, sampled_at: datetime | None = None) -> dict[str, dict[str, float | int | None]]:
        """Persist the latest forming close separately from completed candles."""
        sampled_at = sampled_at or utc_now()
        last_age: dict[str, dict[str, float | int | None]] = {}
        with self.db:
            for bar in bars:
                self.db.execute(
                    """INSERT INTO execution_market_forming_quotes
                       VALUES (?,?,?,?,?,?,?,?)
                       ON CONFLICT(instrument_id) DO UPDATE SET
                       price=excluded.price, price_time=excluded.price_time,
                       bar_open_time=excluded.bar_open_time, bar_close_time=excluded.bar_close_time,
                       provider=excluded.provider, venue=excluded.venue,
                       environment=excluded.environment""",
                    (bar.instrument_id, bar.close, bar.price_time, bar.open_time,
                     bar.close_time, bar.provider, bar.venue, bar.environment),
                )
                age = max(0.0, (sampled_at - datetime.fromisoformat(bar.price_time.replace("Z", "+00:00"))).total_seconds())
                self.db.execute(
                    "INSERT INTO execution_market_last_age_samples VALUES (?,?,?)",
                    (bar.instrument_id, iso(sampled_at), age),
                )
                rows = self.db.execute(
                    "SELECT age_seconds FROM execution_market_last_age_samples WHERE instrument_id=?",
                    (bar.instrument_id,),
                ).fetchall()
                last_age[bar.instrument_id] = age_stats(row[0] for row in rows)
        return last_age

    def mark_failure(self, instrument_id: str, now: datetime, error: str) -> bool:
        row = self.db.execute("SELECT unavailable_since FROM execution_market_provider_state WHERE instrument_id=?", (instrument_id,)).fetchone()
        since = row[0] if row and row[0] else iso(now)
        with self.db:
            self.db.execute("INSERT INTO execution_market_provider_state VALUES (?,?) ON CONFLICT(instrument_id) DO UPDATE SET unavailable_since=excluded.unavailable_since", (instrument_id, since))
            stale = (now - datetime.fromisoformat(since.replace("Z", "+00:00"))).total_seconds() >= STALE_SECONDS
            if stale:
                self.db.execute("INSERT INTO execution_market_events(event,instrument_id,occurred_at,detail) VALUES (?,?,?,?)", ("execution_market_unavailable", instrument_id, iso(now), error))
        return stale

    def mark_success(self, instrument_id: str) -> None:
        with self.db:
            self.db.execute("INSERT INTO execution_market_provider_state VALUES (?,NULL) ON CONFLICT(instrument_id) DO UPDATE SET unavailable_since=NULL", (instrument_id,))


class ExecutionMarketLock:
    """A lock namespace owned only by this worker."""
    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser()
        self.fd: int | None = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(self.fd)
            self.fd = None
            raise RuntimeError("execution-market worker lock is already held") from exc
        return self

    def __exit__(self, *_):
        if self.fd is not None:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
            os.close(self.fd)
            self.fd = None


def run_once(db_path: str | Path, receipt_path: str | Path, *, lock_path: str | Path | None = None, client: httpx.Client | None = None, clock: Callable[[], datetime] = utc_now) -> dict[str, Any]:
    lock = ExecutionMarketLock(lock_path) if lock_path else None
    if lock:
        lock.__enter__()
    fetched = clock()
    store = ExecutionMarketStore(db_path)
    own_client = client is None
    client = client or httpx.Client(timeout=15)
    all_bars: list[ExecutionBar] = []
    last_age: dict[str, dict[str, float | int | None]] = {}
    errors: list[dict[str, str]] = []
    try:
        for instrument_id in INSTRUMENTS:
            try:
                bars = fetch_binance(instrument_id, fetched, client) if instrument_id.startswith("XAU") else fetch_hyperliquid(instrument_id, fetched, client)
                completed = [bar for bar in bars if is_completed_bar(datetime.fromisoformat(bar.close_time.replace("Z", "+00:00")), fetched)]
                forming = [bar for bar in bars if not is_completed_bar(datetime.fromisoformat(bar.close_time.replace("Z", "+00:00")), fetched)]
                last_age.update(store.write_forming(forming[-1:], sampled_at=fetched))
                all_bars.extend(completed)
                store.mark_success(instrument_id)
            except ProviderUnavailable as exc:
                errors.append({"instrument_id": instrument_id, "error": str(exc), "stale": str(store.mark_failure(instrument_id, fetched, str(exc))).lower()})
        receipt = store.write(all_bars, run_id=f"execution-{uuid.uuid4().hex[:12]}", fetched_at=fetched, sampled_at=clock(), status="stale" if any(e["stale"] == "true" for e in errors) else ("partial" if errors else "ok"), error=json.dumps(errors) if errors else None)
        receipt["last_age_seconds"] = last_age
        receipt["last_age_p50_seconds"] = {key: value["p50"] for key, value in last_age.items()}
        receipt["last_age_p95_seconds"] = {key: value["p95"] for key, value in last_age.items()}
        receipt["last_age_max_seconds"] = {key: value["max"] for key, value in last_age.items()}
    finally:
        if own_client: client.close()
        store.close()
        if lock: lock.__exit__(None, None, None)
    path = Path(receipt_path).expanduser(); path.parent.mkdir(parents=True, exist_ok=True); path.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    return receipt


async def supervise(args: argparse.Namespace) -> None:
    while True:
        await asyncio.sleep(next_aligned_run_delay(utc_now(), args.interval))
        run_once(args.db, args.receipt, lock_path=args.lock)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="~/park-data/market/execution_market.db")
    parser.add_argument("--receipt", default="~/park-data/market/execution-market-latest.json")
    parser.add_argument("--lock", default="~/park-data/market/execution-market-worker.lock")
    parser.add_argument("--interval", type=int, default=POLL_SECONDS)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    if args.once:
        print(json.dumps(run_once(args.db, args.receipt, lock_path=args.lock), sort_keys=True))
    else:
        asyncio.run(supervise(args))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
