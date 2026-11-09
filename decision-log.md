# Decision Log

## 2026-11-09 - Health refreshes the integrity check in the background (#196)

Decision

- After #194 the event loop stayed free, but when the 600 s integrity cache
  expired, the next health caller waited for the new `PRAGMA integrity_check`:
  24.8 s measured on production, past trading-system's 10 s client timeout.
- Once a result exists, an expired one is served at once (its age is in
  `storage.integrity_age_seconds`) and one background task refreshes it. Only
  the first call after a start waits.

Verification

- `test_an_expired_integrity_result_is_served_while_a_fresh_one_runs_in_the_background`:
  three calls after the TTL return in under 0.3 s with the old result; exactly
  one refresh runs; the next call sees the new one. Full suite 458 passed.

## 2026-11-09 - /api/health no longer blocks the event loop (#194)

Decision

- `/api/health` read SQLite synchronously inside an `async` handler and ran
  `PRAGMA integrity_check` on every call. Measured on production (485 MB
  `kline.db`): the integrity check alone takes ~12 s cold; a cached GOLD 1m
  read took 0.03 s normally and 4.8 s while one health call ran. That stall is
  what trading-system saw as `datafeed unavailable: timed out`.
- The storage part now runs in a worker thread (`asyncio.to_thread`), one
  computation at a time per store, shared by concurrent callers. Coverage and
  observations are reused for 15 s (`KLINE_HEALTH_STORAGE_TTL_SECONDS`), the
  full integrity check for 600 s (`KLINE_HEALTH_INTEGRITY_TTL_SECONDS`).
- The response says how old those facts are: `storage_age_seconds` and
  `storage.integrity_age_seconds`. Nothing else in the payload changed.
- `KlineStore.storage_integrity()` is split out of `storage_health()`, which
  takes an optional recent result; the default still runs the check.

Gotchas

- The 2639+ `$MGCV26.CMX: possibly delisted` lines came from
  human-kline-review asking for an expired dated contract with
  `cache_policy=bypass` and retries. Yahoo calls already run in threads, so
  they cost threads, not the event loop; that client is fixed in its own repo.
- The cache is keyed by the store object, so a test or process with a new
  store never sees another's numbers.

Verification

- `tests/test_health_does_not_block.py`: another coroutine keeps ticking while
  a 0.6 s integrity check runs; five concurrent calls share one check; storage
  refreshes after its TTL while integrity does not.
- Full suite 457 passed.

## 2026-11-07 - OKX silver and crude under the commodity source (#188)

Decision

- `okx_swap_public` now allows `XAG-USDT-SWAP` (silver), `CL-USDT-SWAP` (WTI)
  and `BZ-USDT-SWAP` (Brent) beside gold. They are commodities, so they join
  the commodity source; no new source.
  - Aliases: SILVER/XAG/XAGUSDT, WTI/CL, BRENT/BZ. Canonical ids SILVER, WTI,
    BRENT, matching the Yahoo commodity manifest.
  - Crypto symbols stay outside this source (still rejected before any
    request).
- The three are in `okx_history.SERIES`, so the daily 08:40 job backfills
  them too, each with its own report file.
- Feeds trading-system#1361: silver and crude on the desk from public data.
  No accounts, no keys.

Gotchas

- These swaps are young: XAG listed 2026-01-28, CL 2026-03-05, BZ
  2026-03-24 (OKX `listTime`). 180 days of history fits inside all three
  today; a longer window would hit the listing date.
- Fees are still not read from OKX (`maker_fee_rate`, `taker_fee_rate` stay
  in `missing_fields`). Downstream must show fees as unverified.
- The provider allow-list and the provenance manifest are kept in step by
  hand; a test checks the manifest names all three.

Verification

- `tests/test_okx_swap.py`: each alias resolves to its instId; silver's
  instrument definition reads ctVal 0.01 and the `XAG-USDT` tier family;
  the manifest names silver and crude and not BTC.
- `tests/test_okx_history.py`: the three series sit under the commodity
  source with their own report paths.
- The 9 new tests fail without the change. Full suite: 452 passed.

## 2026-11-05 - OKX BTC history under a crypto source (#186)

Decision

- A new source, `okx_swap_crypto_public` (crypto, BTC/ETH USDT swaps), rather
  than widening `okx_swap_public`.
  - The source model has one asset class per source: `_SOURCE_ASSET_CLASSES`
    and one manifest per source.
  - Gold stays `okx_swap_public` (commodity), byte for byte.
- `okx_history.SERIES` maps instrument to `(source id, asset class)`. Bars,
  funding, the report path and the report's `source` all come from it.
- The daily job runs every series in turn with one provider and one lock. It
  exits 1 if any series failed, after finishing the others.

Gotchas

- `OkxSwapProvider` hard-coded `okx_swap_public` as the source mode on its
  instrument specs. It now takes `source_mode`, so specs fetched through the
  crypto source carry the crypto source id.
- `report_path` raises for an instrument that is not in `SERIES`. A typo
  cannot write a report nobody serves.

Verification

- `tests/test_okx_history.py`: BTC lands under crypto with its own report and
  funding; gold's report path is unchanged; routes refuse a crossed
  source/instrument; `/api/candles/crypto/BTC-USDT-SWAP` serves stored bars
  with the backtest's exact parameters.
- Full suite: 442 passed.

## 2026-11-05 - OKX gold history for the pre-start backtest (#184)

Decision

- Bars go into the `klines` table of the database port 8100 reads, under
  `okx_swap_public` only, so `/api/candles` serves them with
  `cache_policy=require` and no second read path exists.
- Work is one UTC day at a time, each committed on its own. A complete day is
  skipped, which makes resume and the daily top-up the same code path.
- The report separates `venue_no_bar` from `fetch_failed`. Nothing is filled.
- New table `funding_rates` keeps the rate, the realized rate and OKX's raw row.

Gotchas

- OKX `funding-rate-history` stops at about 94 days. 180 days of funding cannot
  be fetched, so the report states where coverage starts.
- `OkxSwapProvider.fetch` caps at 60 pages, about 6,000 bars. The backfill pages
  itself with `history_page`.
- Port 8100 uses `~/datafeed/data/kline.db`, not `~/park-data/market/kline.db`.
  An OKX read asks for an execution venue, so it skips the market database.
- `ops/release.sh` could not install a new job: `cp` of a missing plist and
  `bootout` of an unloaded job both stopped it under `set -e`.

Verification

- `tests/test_okx_history.py`: paging, the forming minute, resume, retry,
  gaps across midnight, funding, routes.
- Real run on the dev store: 259,200 of 259,200 bars, no gaps, 281 funding rows
  from 2026-08-03.

## 2026-11-05 - OKX gold in the execution-market worker

### Decisions

- `INSTRUMENTS` gains `XAU-USDT-SWAP.OKX` (`okx_swap_public`, venue `okx`,
  production). It is served under its own venue; Binance and OKX never borrow
  each other's bars.
- The worker dispatches and parses on `meta["venue"]`. An unknown venue raises
  `ProviderUnavailable`, recorded per instrument.
- OKX bars: volume is `volCcy` (XAU), not contracts. A bar whose minute ended
  but which OKX has not confirmed (`confirm=0`) is dropped, not promoted.
- The OKX instrument definition now reads `upstream_server_time` from
  `/public/time` and `margin_maint_rate` from `/public/position-tiers` tier 1
  (cross). Limits OKX does not have (`min_notional`, `max_price`) are `null`
  and listed in the new `not_applicable_fields` with a reason. They are never a
  fake `"0"`. `missing_fields` keeps meaning "the venue has it, we did not read it".

### Gotchas

- The old dispatch `startswith("XAU")` would have sent OKX gold to the Binance
  fetcher. A test pins this.
- Tests that build "today" from `date.today()` fail between 00:00 and 08:00
  Asia/Shanghai, because bars are cut by UTC day. Use
  `datetime.now(timezone.utc).date()`.

### Verification

- 426 passed. A live run into a scratch DB: Binance 451 isolated, OKX
  `XAU-USDT-SWAP` written.

## 2026-11-05 - OKX swaps as a second execution-live venue

### Decisions

- Add `okx_swap_public` (`OkxSwapProvider`): OKX public USDT-margined
  perpetual swaps, starting with `XAU-USDT-SWAP`. It is registered as an
  `execution_venue=True` COMMODITY source beside `binance_usdm_futures`, with
  its own `ProviderMeta`, manifest, aliases (`okx`, `okx_swap`) and
  instrument definition (`XAU-USDT-SWAP.OKX`).
- Selection is explicit. `profile=execution_live` with `source=auto` still
  resolves to Binance; nothing fails over to OKX by itself. A consumer asks
  for `source=okx_swap_public`, and every response says `provider=okx`.
- Why now: on 2026-11-04 Binance started answering HTTP 451 ("restricted
  location") to this machine for both `fapi` and `api`, so the only gold
  execution source went dark. OKX, Bitget, Kraken and Hyperliquid answered
  200 from the same machine; Bybit answered 403 (CloudFront geo-block).

### Gotchas

- OKX returns candles newest first; the provider reverses them.
- `/market/candles` only covers the most recent 1,440 bars; older bars come
  from `/market/history-candles` (100 per page). Both page backwards with
  `after=<ts>`.
- Daily bars default to Hong Kong midnight. The provider asks for `1Dutc` /
  `1Wutc` so a day is a UTC day like every other source here.
- Swap volume `vol` is in contracts. The provider uses `volCcy` (base units,
  XAU) so volume means the same as Binance base volume; `amount` is
  `volCcyQuote`.
- Sizes are in contracts (`ctVal` = 0.001 XAU). The instrument definition
  converts lot, min and max sizes to base units so `price × quantity` is the
  USDT notional. Fees, maintenance margin, `max_price` and `min_notional` are
  listed in `missing_fields`, never invented.
- OKX `XAU-USDT-SWAP` is not the same contract as Binance `XAUUSDT`: prices
  differ by a few dollars and tick size is 0.1, not 0.01. Switching a Paper
  track between them is a strategy-level decision, not a data-layer one.

### Verification

- `tests/test_okx_swap.py`: 9 passed (ordering, UTC days, history paging,
  error codes, 451, allowlist, contract-to-base conversion, inverse refused,
  registry identity).
- Full suite: 420 passed, 1 failed. The failure is
  `test_binance_daily_excludes_current_active_bar_and_validates_ohlc`, which
  fails the same way on `main` without this change (date-sensitive).
- Live smoke from this machine (isolated instance on :18100, temp DB):
  600 × 30m bars across both endpoints, 400 daily bars, instrument
  `XAU-USDT-SWAP.OKX` TRADING. The default Binance path still returned 451
  with no fallback.
- Production `~/park-runtime/datafeed` and `:8100` were not touched.

## 2026-09-08 - Forming execution price and v2 freshness (#179)

### Decisions

- The worker keeps the latest forming 1m bar close in a dedicated
  `execution_market_forming_quotes` table; it never promotes that row into
  `execution_market_candles`. The existing `price` and `bars` fields remain
  the last completed-bar view.
- `last_price` is the forming close and `price_time` is the local `fetched_at`,
  because neither configured public candle payload provides a reliable bar
  update timestamp. `fresh` now uses only `last_age_seconds <= 30`, with the
  threshold configurable by `KLINE_EXECUTION_MARKET_LAST_PRICE_STALE_SECONDS`.
- The read response is `execution-market-v2`; all v1 fields are retained.

### Gotchas

- A v1 database without the forming quote table remains readable but reports
  no last price and `fresh=false`; the API never treats the old closed-bar age
  as a substitute for forming-price freshness.
- The owner must run the 30-minute XAU/BTC runtime window and trading-system
  dual comparison after publication; this branch uses temporary paths only and
  does not touch the active worker, 8100, or launchd.

## 2026-09-08 - Execution-market 1m read API (#172)

### Decisions

- 8100 exposes a read-only `GET /api/execution-market/{venue}/{instrument}`
  response with schema `execution-market-v1`, the last closed 1m price, a
  bounded recent-bar series, freshness, trust, source, observation time, and
  provenance.
- The API reads only the dedicated execution-market database and requires an
  exact venue/instrument identity. It never falls back across venues, serves a
  forming bar, or changes the existing `/api/candles` and `/api/health` paths.

### Gotchas

- A stale or missing bar remains an explicit response with `fresh=false` and a
  reason; HTTP 200 is not execution evidence. Live ten-sample freshness remains
  an owner/runtime acceptance gate and is not proved by fixture tests.

## 2026-09-08 - Isolated execution-market 1m ingestion (#171)

### Decisions

- Execution bars use a dedicated `execution_market_*` SQLite table set and a
  separate lock/process; the existing 8100 database, API, and four-hour worker
  are not changed.
- The first two identities are fixed to `XAUUSDT.BINANCE` via public Binance
  USD-M klines and `BTC-USD-PERP.HYPERLIQUID` via the Hyperliquid testnet
  public candle endpoint. There is no venue fallback.
- Bars are keyed by `(instrument_id, close_time)`, forming bars are discarded,
  and upserts make replay/backfill idempotent. Each run writes a lag receipt;
  an outage lasting 90 seconds writes `stale` and an
  `execution_market_unavailable` event.

### Gotchas

- The 30-minute p95 acceptance window and launchd publication remain owner
  verification items. This PR's one-shot evidence uses a temporary database
  and receipt path; it does not touch the active paper grid or launchd.

## 2026-09-08 - Pair Screening derived 4h candles with transform receipts (#166)

### Decisions

- Screening's Tencent and Yahoo free-source `4h` paths are known 15m-to-4h
  aggregations. If a provider returns usable derived rows without the optional
  transform metadata, ingestion reconstructs the bounded transform receipt
  from the source identity and fixed calendar contract before atomic promotion.
- The fallback is limited to those two Screening sources and `4h`; native
  timeframes and other worker/source paths retain their existing provenance
  behavior.
- Native `4h` providers such as Hyperliquid must remain `is_derived=false`;
  timeframe alone is not evidence of aggregation. The ingestion boundary now
  derives that flag from `TimeframeTransform.timeframe_origin`.

### Gotchas

- The storage layer intentionally remains fail-closed: every derived candle
  still needs a same-run transform receipt. This fix repairs the missing
  receipt at the ingestion boundary rather than weakening that invariant.
- A full Screening manifest includes cross-market native `4h` cells; testing
  only the 200 stock cells can miss this failure even when their receipts are
  correct.
- The owner must rerun the canonical Screening command for live
  `last_success_at` and combined-matrix evidence; this branch does not touch
  launchd, plist files, services, or production databases.

## 2026-09-08 - Consolidate datafeed launchd jobs on one canonical checkout (#155)

### Decisions

- The five product runtime jobs use `/Users/wendy/park-runtime/datafeed` as their
  single source checkout; a release is a detached, clean checkout at an explicit
  Git SHA followed by per-job `bootout` and `bootstrap`.
- `ops/launchd/` owns the five plist contracts. Database paths remain unchanged:
  8100 keeps the resident database, while the MVP and dashboard paths keep their
  existing market/observer database assignments.
- `ops/release.sh` creates a dated launchd backup, refuses a dirty checkout, checks
  the four-hour worker safety window, renders the exact release SHA, restarts each
  job, and writes an external release receipt.

### Gotchas

- The receipt is stored outside the Git checkout so the deployed checkout remains
  clean and can be independently verified.
- A launchd `running` state alone is not acceptance evidence; health identity and
  the #134 byte-level consumer comparison must be checked after restart.
- `REGISTRY.md` is intentionally not changed in this issue because the owner rule
  requires its post-merge update.

## 2026-09-08 - Scope Screening health and worker to 1d + 4h (#157)

### Decisions

- Screening stock identities use free-source adapters only for `1d` and `4h`; `15m`, `1h`, and
  `1w` are explicit `not_applicable` cells and are excluded from coverage ratios and aggregate
  status. The existing `/api/candles` query path remains able to read already-persisted bars.
- Added `--scope screening` to the existing `ops.mvp_reliability` worker. It runs the full 216
  manifest with an explicit `1d + 4h` ingestion plan and keeps the four-hour scheduling/lock
  behavior. Watchlist daily scheduling remains independent.
- Corrected the retired A-share manifest identity `CN.A.601989` to `CN.A.600150` and
  `600150.SH`; no historical data is deleted or rewritten.
- This branch does not update `REGISTRY.md`, launchd plists, running services, or production
  databases. Screening launch and live coverage acceptance are owner-owned after #155.

### Gotchas

- The local after snapshot in `docs/verification/issue-157-timeframe-scope-2026-09-08.md` is a
  contract fixture, not proof of live 4h/1d coverage. Do not mark `last_success_at` or either
  90% gate verified until the owner has started the canonical worker and saved its receipts.
- Existing 15m/1h rows remain queryable; changing their health applicability does not delete them.

## 2026-09-08 - Row-level quality promotion for MVP ingestion (#156)

Objective: preserve trustworthy rows when a small number of upstream bars are
malformed, duplicated, gapped, or forming, instead of rejecting the entire
series.

### Decisions

- MVP quality uses a configurable `bad_row_threshold`, defaulting to 5%.
  At or below the threshold a series is `partial`; only a ratio above the
  threshold is `fail`.
- Ingestion isolates malformed and duplicate rows during normalization and
  excludes malformed, duplicate, out-of-session, and forming rows from
  promotion. Remaining closed rows are written with the normal source,
  quality, and watermark receipts.
- Quality receipt `details.issues[]` retains each issue's `timestamp`,
  `status`, and reason. Empty upstream responses and provider/entitlement
  failures remain unavailable/blocked and fail closed.
- `/api/candles` and its response schema are unchanged; persisted bad rows are
  never queried as candles.

### Gotchas

- `partial` is not proof that every upstream row is usable: consumers must
  inspect the persisted quality receipt and its row-level issues.
- A forming-only response remains `partial` but promotes zero rows. The
  opening-buffer behavior from the prior contract is therefore preserved.
- The required live confirmation for QCOM, `000660.KS`, and DHR must be
  captured by the owner after deployment; this worktree does not restart the
  production worker.

### Verification

- `PYTHONPATH=src python3 -m pytest -q tests/test_ingestion.py tests/test_market_calendar.py tests/test_mvp_storage.py tests/test_quality.py` -> 39 passed.
- Full-suite command and owner-side live verification are recorded in
  `docs/verification/issue-156-row-level-quality-2026-09-08.md`.

## 2026-09-07 - Empty MVP rows remain an explicit unavailable cell

Objective: prevent one provider response with zero rows from terminating the
MVP worker cycle while preserving truthful quality and health receipts.

### Decisions

- Reuse the already-derived `instrument + timeframe + manifest` series key at
  the ingestion call site when `_quality_receipt` receives no rows.
- Persist an existing `missing` quality status for an empty response, retain
  the `missing` issue detail and blocked-cell count, and expose the cell as
  `unavailable` in the run and health matrix. No candle or watermark is
  promoted.
- Treat an empty upstream response as an observed attempt for health read-model
  purposes, so the persisted cell is `unavailable` rather than a whole-run
  `failed` status. Other cells in the same run continue normally.

### Gotchas

- The real two-cycle launchd acceptance remains owner-owned: this worktree did
  not restart or reload `com.wendy.datafeed.mvp-worker` and cannot claim fresh
  stderr or launchd-run evidence.
- `missing` is a quality-receipt status, while `unavailable` is the dashboard
  cell status; they are intentionally distinct layers of the contract.

### Verification

- `PYTHONPATH=src uv run pytest -q tests/test_ingestion.py tests/test_mvp_storage.py tests/test_health_matrix.py` -> 38 passed.
- See `docs/verification/issue-153-empty-rows-receipt-2026-09-07.md` for the
  local regression evidence and owner deployment command.

## 2026-07-09 - Trading live data path

Objective: make datafeed reusable as a standard market-data layer while keeping research/cache data clearly separated from trading/live data.

### Decisions

- Added a separate `binance_usdm_futures` provider instead of replacing the existing crypto or commodity providers. Research users still get Binance spot/Yahoo behavior; trading live requests opt in with `mode=live` or `strict=true`.
- In strict/live mode the REST endpoint always fetches upstream and never reads SQLite cache. Upstream failure, empty response, stale latest candle, gaps, duplicate timestamps, or out-of-order candles become explicit errors/blocked envelopes.
- `is_synthetic` remains hard-coded false across success and error envelopes. There is still no synthetic/fabricated candle path.
- Extended the provenance envelope with `execution_venue`, `reject_reason`, and `access_issues` so consumers can fail closed without parsing provider-specific text.
- Kept normalized candle upsert uniqueness on `(ticker, asset_class, timeframe, timestamp)` and added a separate `raw_upstream_responses` table for raw REST payload capture.
- Added `/api/ws/candles/{asset_class}/{ticker}` for Binance USD-M Futures candle updates. The wire format remains a standard `CandleResponse` with `served_from=websocket`.

### Gotchas

- `strict=true` and `mode=live` are intentionally equivalent. Both mean upstream-only, no cache, no fallback.
- `GOLD` and `XAUUSD` are accepted as aliases in live mode, but the execution-venue symbol is normalized to `XAUUSDT`.
- Yahoo commodity `GOLD -> GC=F` remains a research path only. It is not an execution venue and should not drive a live trading canvas.
- Health reports registered provider availability and supported timeframes, but it does not do a network probe by default. This avoids making health checks depend on Binance latency or regional availability.
- WebSocket updates can repeat the same candle while it is still forming. SQLite upsert prevents duplicate normalized rows for those repeated timestamps.
- Gap detection is applied to continuous/live sources. Research market-hours data can have calendar gaps and should be interpreted with market calendars.

### Verification

- `python3 -m compileall src/kline`
- `python3 -m ruff check .` -> all checks passed
- `python3 -m pytest` -> 43 passed in 1.33s

## 2026-07-09 - Source and policy abstraction

Objective: keep datafeed as a pure data service while making every data response explicit about source, cache, quality, fallback, and execution-venue requirements.

### Decisions

- Reframed the API around `source`, `cache_policy`, `quality`, `fallback_policy`, `require_execution_venue`, and optional `profile` shortcuts instead of treating `research` and `trading` as first-class datafeed modes.
- Kept `mode=live`, `strict=true`, and `refresh=true` as backward-compatible shortcuts. Internally they resolve to explicit policies:
  - `mode=live` / `strict=true` -> `profile=execution_live`
  - `refresh=true` -> `cache_policy=bypass`
- Added `CachePolicy`, `QualityPolicy`, and `FallbackPolicy` enums to the response envelope so downstream consumers can audit what policy produced the candles.
- Added source normalization and a source registry. `source=auto` keeps the asset-class default source; explicit sources can select `binance_spot_public`, `binance_usdm_futures`, `yahoo_finance`, `yahoo_finance_futures`, or `tushare_pro`.
- Made `profile=realtime` generic. It is not tied to trading; any realtime-capable source can use `cache_policy=bypass`, `quality=strict`, and `fallback_policy=none`.
- Kept `profile=execution_live` as the convenience path for XAUUSDT on Binance USD-M Futures, with `require_execution_venue=true`.
- Canonicalized `GOLD` / `XAUUSD` / `XAUUSDT` to `XAUUSDT` for `binance_usdm_futures` before cache/save. This prevents execution-venue candles from polluting commodity research `GOLD` cache rows.

### Gotchas

- `strict=true` is now a compatibility shortcut for execution-live behavior, not the generic strict quality switch. New callers that want strict quality on a non-execution source should use `quality=strict` or `profile=realtime`.
- `fallback_policy=explicit` is modeled but no fallback provider chain exists yet. The important invariant is still no hidden fallback.
- `cache_policy=require` never fetches upstream. A miss returns `cache_miss`.
- `require_execution_venue=true` rejects Binance Spot, Yahoo, and TuShare even if they can return fresh candles.
- The storage key still does not include `source_mode`; canonicalizing futures aliases avoids the known GOLD/XAUUSDT collision, but a future schema migration should consider source-aware cache keys if multiple sources serve the same asset/ticker.

### Verification

- `python3 -m compileall src/kline`
- `python3 -m ruff check .` -> all checks passed
- `python3 -m pytest` -> 46 passed in 2.51s

## 2026-07-09 - Ports and adapters for broker data sources

Objective: make any broker/exchange feed fit into datafeed by implementing one standard market-data port instead of changing API routing for every provider.

### Decisions

- Added `src/kline/ports.py` as the domain-facing boundary:
  - `ProviderMeta` describes trust/source semantics.
  - `SourceManifest` describes the adapter capability contract and ticker aliases.
  - `MarketDataPort` is the required adapter protocol.
  - `ProviderBackedMarketDataAdapter` wraps existing provider classes so the old Binance/Yahoo/TuShare code can fit the new port without rewriting them.
- Refactored `registry.py` to register adapters by `source_id`. `register_adapter(adapter)` is now the extension point for new brokers.
- Kept source manifests separate from adapter availability. This allows source capability metadata to exist even when an adapter is unavailable, such as TuShare without a token.
- Moved ticker canonicalization into source manifests/adapter behavior. `GOLD` / `XAUUSD` -> `XAUUSDT` is now a source-level alias for `binance_usdm_futures`, not an API special case.
- Refactored API upstream fetch and WebSocket streaming to call the `MarketDataPort` methods (`fetch_candles`, `stream_candles`) instead of provider-specific methods.
- Added a test-only fake broker adapter to prove a new `fake_broker_feed` source can be registered and consumed through the normal candles endpoint without adding an API branch.

### Gotchas

- The existing provider classes still exist under `providers/`; they are now adapter internals. New broker work should prefer implementing `MarketDataPort` directly or wrapping a provider with `ProviderBackedMarketDataAdapter`.
- Storage uniqueness is still `(ticker, asset_class, timeframe, timestamp)`, not source-aware. The manifest alias layer reduces collisions, but a future multi-source cache design should include `source_id` in the normalized candle key.
- Dynamic source manifests are process-local registrations. Production plugin loading will need a durable config or entry-point mechanism if adapters should load outside Python startup code.
- `ProviderBackedMarketDataAdapter` delegates raw response capture to `provider.last_raw_response`; adapters that need auditability should expose that property explicitly.

### Verification

- `python3 -m compileall src/kline`
- `python3 -m ruff check .` -> all checks passed
- `python3 -m pytest` -> 47 passed in 6.10s

## 2026-07-10 - WebSocket proxy runtime dependency

Objective: make the standardized Binance USD-M WebSocket path dependency-
complete and fail visibly when the upstream stream is silent.

### Decisions

- Added `python-socks[asyncio]` as an explicit runtime dependency. `websockets`
  auto-detects SOCKS proxy settings, so proxy support is part of the production
  stream path rather than an optional development convenience.
- Kept the existing `binance_usdm_futures` source, strict quality policy, and
  `fallback_policy=none`. No alternate source is selected when the stream fails.
- Ignored SQLite `-wal` and `-shm` sidecars created by a running datafeed service.

### Gotchas

- `/api/health` validates provider registration, not a live upstream socket. A
  healthy service can still fail its first WebSocket subscription if proxy
  runtime dependencies are missing.
- Installing proxy support removes the immediate import failure but does not
  prove market messages flow. On this machine both XAUUSDT and BTCUSDT complete
  the WebSocket handshake and then receive no frames through the current
  network path.
- The provider now times out a silent upstream and raises `ProviderError`; the
  API returns a structured `stream_error` with `fallback_policy=none` rather
  than leaving consumers connected indefinitely.

### Verification

- `python3 -m pytest` -> 48 passed.
- Real local subscription returned `stream_error` after 30 seconds with
  `served_from=websocket`, `is_synthetic=false`, `execution_venue=true`, and
  `reject_reason=upstream_error`.
- No real candle was received. Tick-level server streaming remains blocked by
  the machine's Binance WebSocket network path; REST and cached data were not
  substituted into the WebSocket response.

## 2026-07-10 - Versioned execution instrument definition

Objective: give paper and shadow execution engines one upstream-derived GOLD
contract definition instead of duplicating precision and margin assumptions.

### Decisions

- Added `instrument-definition-v1` and
  `GET /api/instruments/{asset_class}/{ticker}`.
- Binance USD-M definitions are parsed from live `exchangeInfo`; the response
  preserves price/quantity increments, limits, currencies, contract status,
  order types, and normalized initial/maintenance margin rates.
- Public `exchangeInfo` does not provide account fee rates. Those fields remain
  null and are named in `missing_fields`.
- The USD-M adapter returns `contract_multiplier=1` as an explicitly derived
  field because linear USD-M notional is `price * quantity`; `derived_fields`
  records that derivation so consumers do not mistake it for a raw field.
- The response always reports `served_from=upstream` and
  `is_synthetic=false`. Upstream failure returns an explicit 502 error.

### Gotchas

- `requiredMarginPercent` and `maintMarginPercent` are percentages upstream;
  the v1 response converts them to decimal rates (`5.0000` -> `0.0500`).
- XAUUSDT currently reports `TRADIFI_PERPETUAL`, not generic `PERPETUAL`.
  Consumers must preserve the upstream contract type.
- `exchangeInfo?symbol=XAUUSDT` can still return a multi-symbol payload, so the
  provider selects the exact symbol and fails if it is absent.
- Derived fields must carry a stable explanation in `derived_fields`; consumers
  must reject an unexplained multiplier rather than silently defaulting it.
- Raw instrument responses are auditable through `last_raw_response` but are
  not yet persisted in a dedicated instrument-history table.

### Verification

- Provider tests assert exact XAUUSDT increments, minimums, currencies, margin
  conversion, missing fields, and raw response capture.

## 2026-07-15 - Independent multi-source data plane foundation

Objective: make datafeed the source-aware, auditable market-data boundary that trading
consumers can use without direct exchange or private SQLite access.

### Decisions

- Upgraded normalized candle identity to include `source_id`. Two upstreams can now store
  the same ticker/timestamp without overwriting each other.
- Added an in-place SQLite schema migration. Pre-v0.3 rows are labeled
  `legacy_unknown`; datafeed does not guess which upstream produced old rows.
- Added config and package entry-point adapter discovery. A new source can ship as an
  adapter package plus JSON config and environment-backed credentials without changing
  API routing or the core registry.
- Implemented caller-explicit fallback. `fallback_policy=explicit` is rejected unless
  named fallback sources are provided, and responses expose `selected_source`,
  `selection_reason`, and `attempted_sources`.
- Persisted source observations and exposed source-scoped coverage through `/api/health`.
  Added `/health-ui` as the browser-visible source health and provenance surface.

### Gotchas

- `legacy_unknown` rows remain queryable only through the legacy store API; source-scoped
  API requests intentionally do not reuse them because their origin cannot be proven.
- Entry-point/config adapters must use a lowercase stable `source_id` and implement the
  complete `MarketDataPort`, including explicit unsupported behavior for streaming or
  instrument definitions.
- Explicit fallback does not imply semantic equivalence. Callers must only name sources
  whose instrument mapping and market semantics are acceptable for that request.
- Provider health means the latest observed request result, not merely registration.
  A registered source remains `registered` until it has an actual observation.

### Verification

- `python3 -m pytest -q` -> 65 passed.
- `python3 -m ruff check .` -> all checks passed.

### Follow-up decisions in the same milestone

- Added canonical `instrument_id` separate from each provider symbol. GOLD now remains
  one canonical instrument while Binance uses `XAUUSDT` and Yahoo uses `GC=F`.
- Added explicit asset-class extension points for futures, forex, index, ETF, and macro.
  These classes fail closed until a default adapter is installed; datafeed does not invent
  a provider for them.
- Moved the Tiger COMEX futures quote path into a datafeed-owned adapter. It imports only
  Tiger quote APIs and intentionally has no order client.
- Migrated 329,353 proven-source legacy rows into datafeed: Binance GOLD 1m/5m and Tiger
  MGCmain 1m. Unknown FRED rows were reported as skipped rather than mislabeled.
- Canonicalized intraday timestamps to UTC `+00:00` and merged logical duplicates created
  by old naive timestamps versus new timezone-aware timestamps.
- Added `/api/compare/...` and the health UI comparison card. Binance XAUUSDT and Yahoo
  GC=F coexist, are compared by canonical GOLD timestamps, and are never blended.

### Additional gotchas

- SQLite bulk upserts can exceed the build-dependent bind-variable limit. KlineStore now
  chunks large saves into 500-row batches; the first migration attempt exposed this and
  was safely retried.
- Provider symbol and canonical instrument must never be collapsed into one field. Doing
  so would make `GOLD`, `XAUUSDT`, `GC=F`, and `MGCmain` appear interchangeable when their
  contracts and venue semantics differ.
- Existing FRED macro rows are not yet migrated because no datafeed-owned FRED adapter and
  manifest exists. They remain explicitly skipped, not silently assigned to another source.

## 2026-07-15 - Binance USD-M daily candles for production grid context

### Decisions

- Added native `1d` support to the existing Binance USD-M Futures adapter. GOLD strategy
  planning now receives D1 candles from the same `XAUUSDT` execution-venue source used by
  intraday market data; no Yahoo, synthetic, or silent fallback was introduced.
- Kept the normalized candle and provenance contracts unchanged. The new interval is an
  additional explicit capability of `binance_usdm_futures`, not a derived or blended feed.

### Gotchas

- Binance's upstream futures kline API supports `1d`, but the adapter capability map had
  stopped at `4h`. The trading console therefore showed healthy 1m prices while its
  fail-closed StrategyPlan preview correctly refused to start without trusted D1 context.
- A healthy realtime ticker is not sufficient proof that a strategy can start. Every fixed
  planning timeframe (D1, 4H, 1H, 15m) must independently satisfy the trusted-source gate.

### Verification

- Binance provider/API/provenance focused tests: `24 passed`.
- Live datafeed request returned 20 non-synthetic Binance USD-M `1d` candles for `XAUUSDT`.
- Browser-visible downstream proof:
  `/Users/wendy/park-io/008_codex session insights and decision logs/交易系统/evidence/goldbot-v5-start-ready-binance-d1-2026-07-15.png`.

## 2026-07-15 - Multi-asset adapter completion and owner-side health

### Decisions

- Added datafeed-owned FRED adapters for macro, flow-proxy, and event-proxy daily
  series. DXY, US10Y_REAL, GLD_FLOW, and FED_CPI_EVENTS now use the same candle,
  provenance, storage, and health contracts as tradable instruments.
- Added a configurable OANDA v20 adapter. Credentials are resolved by datafeed's
  adapter config and never by trading-orchestrator.
- Added Tiger market-session capability and `/api/sessions/...`; trading no longer
  needs Tiger's quote SDK to determine market windows.
- Added owner-side SQLite integrity to `/api/health`, without exposing the storage
  path to consumers.
- Binance range fetching now paginates in datafeed for explicit historical windows.

### Gotchas

- FRED series are daily observations and can be published with date lag. Their
  `fresh` value remains unknown rather than applying a 24/7-market freshness rule.
- A configured adapter and a healthy adapter are different states. Tiger and OANDA
  remain absent/unavailable until their datafeed-owned credential config is enabled.
- OANDA pricing credentials belong to the market-data adapter; OANDA account/order
  credentials remain a separate execution concern.
- Large Binance backfills must have explicit start/end windows. A latest-only request
  without a start cannot safely page backward without changing semantics.

### Verification

- Full datafeed suite: `70 passed`; Ruff: all checks passed.
- Owner storage receipt: integrity `ok`, 330k+ candles, six stored sources.
- All 376 formerly skipped FRED rows were migrated with explicit source/asset identity;
  the final legacy migration reports zero skipped rows.
- Visual proof:
  `/Users/wendy/park-io/008_codex session insights and decision logs/交易系统/evidence/2026-07-15-datafeed-source-health-final.png`.
# 2026-08-31 - Repair Yahoo latest-session OHLC gaps without stale fallback

- Yahoo occasionally returned a latest-session row with valid volume but
  `NaN` OHLC, causing strict ETF/index requests to fail with `502 upstream_error`.
- The provider now requests a small upstream context window and retries with
  yfinance `repair=True` only after the raw response fails OHLC validation.
- Repaired rows are explicitly recorded in `source_identity`; output remains
  upstream, non-synthetic, and clipped to the caller's requested cutoff.
- No cache fallback, previous-close substitution, or implicit source change was
  introduced. Focused and full test suites pass (147 tests).

## 2026-09-02 - Persist the 42-member Watchlist Universe independently

- Issue #120 v2 corrected the original Screening-specific assumptions. The Watchlist now has
  its own 42-member manifest, `WATCH.*` instrument namespace, no freeze/reserve policy, and a
  dedicated daily runner. `051505` remains explicitly excluded because its identity is unknown.
- A-share equities retain the existing `tencent_stock_free` source unchanged. Four A-share ETFs
  use the new `tencent_etf_free` source with explicit `5 -> sh` and `15/16 -> sz` routing.
  `tencent_kline` remains index-only.
- `000660.KS` was re-probed after #118 and returned real Yahoo daily candles, so it is configured
  as the 42nd member rather than mislabeled as entitlement-blocked.
- The runner only accepts `/Users/wendy/park-data/market/kline.db` and its dedicated
  `watchlist-worker.lock`, fixes the timeframe to `1d`, and never calls the Screening free-source
  overlay. A single lock covers the complete 42-member cycle.
- The final real run persisted 42/42 instruments, 19,808 candles, 42 source observations, 42
  quality receipts, and 42 watermarks. A-share/ETF evidence recorded 21 attempts for 20 cells;
  one empty transport error retried successfully. 429/403/5xx/timeout counts were all zero and
  A-side P95 was 5,369.5 ms. See `docs/verification/watchlist-ingestion-2026-09-02.md`.
- This does not cut the 8100 Query Service over to the Market Data Database. Consumer migration
  remains a separately reviewed spec because `klines` and `mvp_candles` have different schemas.

## 2026-09-03 - Define Yahoo daily finalization by session, not midnight

- The first launchd catch-up for #122 proved that raw Yahoo already exposed the September 2 U.S.
  session while `USStockProvider` still returned September 1. The old cutoff treated every current
  New York calendar date as forming until midnight, even hours after the market had closed.
- #124 uses a conservative 18:00 source-local finalization boundary. Before it, the current local
  trade date stays excluded; at or after it, an upstream-supplied row may pass. Missing rows are
  never synthesized.
- Date-only `end` remains an exclusive trade-date boundary. A timezone-aware timestamp `end` is
  converted to the source timezone before choosing its trade date, matching the orchestrator's real
  request shape. `.KS` symbols use `Asia/Seoul`.
- QCOM, AAPL and `000660.KS` were probed with aware start/end timestamps at 2026-09-03 01:31 UTC;
  all returned September 2 as the latest daily session. Resident 8100 and #115 were not restarted.

## 2026-09-03 - Schedule Watchlist daily catch-up independently

- #122's initial 07:15 launchd catch-up was held as partial because it ran before #124 and Yahoo
  still clipped the U.S. session. After #124 merged, the dedicated job was reloaded at
  `0541cddbba9539dafab94bb33ecbd3913a696cac` and manually kicked after the A-share close.
- The final receipt reported exit code 0, 42/42 current-ready, 42 source observations, 42 quality
  receipts, 42 watermarks, zero missing cells, zero duplicate/future/invalid rows, and zero
  `051505` rows. A-share/ETF attempts were 20/20 HTTP 200 with zero 429/403/5xx/timeout and P95
  1,116.5 ms. The weekday 07:15 calendar trigger remains loaded.
- Latest normalized trade dates were A-share 2026-09-02 (13 members) and 2026-09-03 (three
  newly-listed members after today's close), ETF 2026-09-02 (four), and US/KR 2026-09-02 (22).
  The three 2026-09-03 rows are closed bars observed after the A-share 15:00 close.
- The Watchlist schedule is independent of Screening's issue-71 worker and resident 8100; neither
  was restarted or reconfigured during this delivery.

## 2026-09-03 - Persist the 16 cross-market Watchlist members

- #126 extends the independent Watchlist manifest from 42 to 58 daily members. The 16 additions
  are SPX/NDX (explicit SPY/QQQ proxies), DXY (UUP proxy), SCHD, VIX, BTC, ETH, HYPE, the three
  Shanghai indices, Nikkei 225, KOSPI, WTI, Gold, and Silver.
- Cross-market entries keep their own `WATCH.*` identities and source/provider provenance. VIX is
  mapped to real Yahoo `^VIX`; the Shanghai entries retain `tencent_kline` and its three-symbol
  allowlist; no Treasury symbols are added.
- Ingestion now passes `provider_symbol` to adapters rather than `display_symbol`. This preserves
  display identity while allowing explicit proxy/provider mappings to fetch the correct upstream
  symbol. A regression test covers SPX → SPY, and the full suite remains green.
- The real 58-member run persisted all 58 instruments. The final receipt recorded zero rate-limit,
  403, 5xx, and timeout errors; two older Tencent responses were held by the watermark-regression
  guard rather than overwriting newer data. See
  `docs/verification/watchlist-cross-market-ingestion-2026-09-03.md`.
- This work does not update #115, the Screening manifest/worker, resident 8100, or the existing
  health observer. Dashboard visibility remains a separate dual-store task.

## 2026-09-03 - Observe Screening and Watchlist through an isolated read-only dashboard

- #128 adds a combined health read model while keeping the existing Screening matrix endpoint and
  18171 service unchanged. Screening and Watchlist remain separate datasets and separate SQLite
  files; the dashboard merges only their redacted read models.
- Both combined-route stores are forced through SQLite `mode=ro` plus `query_only`, fail closed when
  a configured database is missing, and reject public mutation methods. The dedicated launchd
  service runs on 18172 from `main@ded8741`.
- The Watchlist-filtered page shows 58 assets / 290 cells: A-share/ETF 20, US/KR 22, cross-market
  16. Daily technical coverage is 58/58; all other Watchlist timeframes are explicitly
  `not_applicable`. Public-source entitlement uncertainty remains `partial`, not fabricated ready.
- A real API read left both database SHA-256 hashes unchanged. Resident 8100 and both #115 services
  retained their original PID, build, and runtime. See
  `docs/verification/combined-health-dashboard-2026-09-03.md`.

## 2026-09-03 - Normalize proxy identity metadata

- #132 makes proxy discovery uniform across the Watchlist: SPX/SPY, NDX/QQQ, and DXY/UUP all
  declare `metadata.identity_role=proxy` together with the exact `proxy_for` target.
- VIX remains a real `^VIX` index identity and is explicitly kept out of the proxy set.
- The validator now enforces the complete proxy metadata pair and exact target names. Existing
  candle rows are not rewritten; source IDs, provider symbols, routes, and runner behavior do not
  change.

## 2026-09-03 - Separate healthy unverified data from real degradation

- #133 introduces `ready_unverified` for cells whose candle, quality receipt, freshness, and
  watermark are healthy while the public/free source lacks a separate commercial entitlement
  receipt. The API keeps `status_reason=entitlement_unverified` and does not claim certified ready.
- `ready_unverified` counts as data-healthy for aggregate readiness and coverage, while
  `quality_partial`, `watermark_missing`, `transform_receipt_missing`, stale, failed, blocked, and
  unavailable keep their existing severity and continue to degrade the overall status.
- The Chinese UI gives `ready_unverified` its own filter, label, and visual treatment. A fully
  healthy Watchlist can show `数据正常（授权未认证）`; mixed real problems remain partial/failed.
- No provider, entitlement mechanism, Screening manifest/runner, or quality-gate ordering changes.

## 2026-09-03 - Serve Watchlist consumers through an identity-aware dual-store query layer

- #134 corrects ADR 0004's disproved “change only `db_path`” assumption. The resident service keeps
  the legacy `klines` database and adds a read-only `mvp_candles` adapter behind
  `KLINE_QUERY_BACKEND=market_first`; the public 8100 request and response contract remains stable.
- The exact runtime denominator is 69 logical requests: Newsletter 28 after owner-excluded Treasury,
  Human Review 34 primary and seven conditional 1h fallbacks. Only Watchlist daily identities may
  resolve to the Market Data Database; Screening is not queried by the adapter.
- Ten eligible daily histories were explicitly backfilled from 2021 with the canonical Watchlist
  lock. Both real runs passed 10/10 quality cells and promoted 14,603 candles with no blocked cells.
- Fail-closed routing keeps intraday, two dated micro contracts, insufficient history,
  `volume_semantics=not_applicable`, and UUP's intermittently missing 2026-08-28 history on the
  visible legacy path. No candle or volume is synthesized.
- The switched Market Data Database set was byte-identical 15/15 in the final same-build lockstep
  replay. Literal 69/69 live bytes are not a valid oracle for the remaining upstream routes: eight
  adjacent Yahoo/Tencent calls returned different provider data despite identical build, URL,
  cutoff and policy. This limitation is preserved in the verification record rather than reported
  as a pass.
- The real rollback drill used `bootout → bootstrap`: candidate → byte-identical legacy plist →
  candidate. A real Newsletter source bundle was 31/31 ready (9 market / 22 legacy); Human Review
  returned 16 assets with six Market Data Database and 28 legacy primary identities.

## 2026-09-04 - Pin Park Exposure Registry and compile the 107-member Price Universe

- #139 adopts `zinan92/watchlist@29ce3c0ad6c6d5f822c860c42ae5ccd251c240d2` as the pinned source for
  Watchlist membership. The committed snapshot contains 16 price assets and 91 unique listed
  company targets (38 CN, 48 US, 4 HK, 1 KR), for 107 daily K-line identities.
- Multi-sector duplicate targets (`000660.KS`, 长江电力, MRVL) are deduplicated for collection while
  all registry memberships and reasons remain provenance metadata. BTC/ETH/HYPE target rows are not
  double-counted because they are already assets.
- The generated artifact remains offline and byte-stable. Existing cross-market IDs, source/provider
  fields and proxy metadata are preserved; the active 58-member manifest is intentionally not switched
  by #139. #140 adds the live HK adapter, #141 backfills/activates the 107-member run, and #142
  updates health/deployment in that order.
- Manifest schema identity stays `watchlist_universe_v1`; upstream registry commit/hash are separate
  snapshot provenance, so membership upgrades do not fork candle identity or duplicate history.
- HK instruments use a truthful `hk_stock` declaration and exact Yahoo symbols (`0100.HK`, `2513.HK`,
  `0700.HK`, `9988.HK`). No provider adapter or live HK request was added in this ticket.

## 2026-09-04 - Add truthful Hong Kong Yahoo daily routing

- #140 registers `hk_stock` as a distinct public asset class and maps the four pinned registry codes
  to exact Yahoo symbols: `00100→0100.HK`, `02513→2513.HK`, `00700→0700.HK`, `09988→9988.HK`.
- HK daily cutoff uses `Asia/Hong_Kong`; provider receipts expose `market=HK` and `listing_venue=HKEX`.
  Existing US, KR, CN, cross-market and Screening source registrations are unchanged.
- Real Yahoo preflight returned three closed daily candles for all four symbols on 2026-09-04; each
  required one yfinance repair pass for the current row, and none was synthesized. The active 58-item
  Watchlist and scheduled runtime remain unchanged until #141/#142.

## 2026-09-04 - Persist the 107-member Watchlist daily universe

- #141 ran the compiled pinned manifest against the canonical Market Data Database in 11 batches.
  The final rerun recorded 107/107 current-ready, 107 observations, 107 quality receipts, 107
  watermarks and 1,630 promoted candles; P95 attempt latency was 1,854.8 ms.
- Final rate-limit, forbidden, server, timeout and unclassified error counters were all zero. Four
  HK cells failed only in the first run because the calendar declaration had not yet been wired;
  after the calendar fix they passed and the original failed receipt remains preserved.
- The database now contains all 107 active registry identities; the 13 old local-only identities
  remain as retained historical rows and are excluded by current membership. The 07:15 scheduler and
  health/8100 deployment remain for #142.

## 2026-09-04 - Activate 107-member Watchlist Health and Query runtime

- #142 wired the compiled 107-item manifest into the read-only health surface and 8100. Candidate
  build `ea11a73` reports 107 Watchlist instruments / 535 cells: 107 daily applicable and 428
  not-applicable; all 107 daily cells are `ready_unverified` with pinned registry provenance.
- The operational grouping now separates A-share, U.S., Hong Kong, Korean and cross-market members.
  Real 8100 requests for HK `00100` and A-share `600900` both returned Market Data Database rows with
  truthful identities. Newsletter remained 31/31 ready and Human Review remained 16 assets with its
  pre-existing explicit unavailable/insufficient states.
- A real candidate→rollback→candidate launchd drill passed for 8100 and 18172 using independent
  plists, byte-identical pre-142 backups, `bootout → bootstrap`, and old/new health probes. #115,
  18171, Human Review and consumer code were untouched.
- The combined aggregate can still say failed because Screening retains its independent blocked
  cells; the Watchlist-filtered view is healthy. Final canonical plist consolidation follows merge.

## 2026-09-04 - Harden HK provenance and market grouping after Sonnet review

- The Sonnet review found no P0/P1 issue. The valid P2 concern was provider identity state under
  future direct concurrency; #147 now serializes `HKStockProvider` state and verifies the public
  adapter seam with interleaved requests.
- HK remains a truthful `hk_stock` source. KR deliberately remains `us_stock` at the public API for
  backward compatibility, while `WATCH.KR.*` and `registry_market=KR` keep health grouping truthful.
- Health tests now pin HK at 4 instruments/20 cells and KR at 1 instrument/5 cells, including commit
  and source-hash provenance. No runtime restart or data mutation was needed for this hardening.

## 2026-09-04 - Move Watchlist daily trigger to 08:15 and catch up commodities

- #149 moves the weekday Watchlist trigger from 07:15 to 08:15 Beijing time. The earlier scheduled
  run correctly found Gold, Silver and WTI still forming at 07:15 because the Yahoo continuous-future
  daily bar closes at the UTC-day boundary.
- A targeted post-close run selected only `WATCH.CROSS.GOLD`, `WATCH.CROSS.SILVER` and
  `WATCH.CROSS.WTI`. All three passed quality, wrote 3 observations/receipts/watermarks and promoted
  12 candles; rate-limit/403/5xx/timeout/unclassified counts were all zero, P95 2,362.5ms.

## 2026-09-04 - Interpret China daily timestamps by declared session convention

- #151 declares `session_date_at_local_midnight` for Tencent A-share stock rows and
  `session_date_at_utc_midnight` for the remaining Watchlist daily sources, including the three
  Tencent China-index rows. All 107 declarations live in validated Watchlist manifest metadata and
  are generated from the pinned registry compiler.
- Watchlist runner receipts and Health now compare the represented session date with the latest
  closed calendar session. A historical row can no longer remain ready merely because its raw UTC
  timestamp falls inside the previous 26-hour grace window.
- A real run backfilled SHCOMP, STAR50 and DIVIDEND through 2026-09-03. SHCOMP closed at 3942.09;
  all three wrote successful observations, pass quality receipts and watermarks. Four A-share
  current-session rows remained explicit forming-bar failures; no quality gate or historical
  timestamp was changed. The 107-row reconciliation recorded zero mismatches between reported and
  expected status. See `docs/verification/china-index-freshness-2026-09-04.md`.

## 2026-09-08 - Cut MVP worker and API over to the canonical Market Data Database (#154)

- The deployed stock seed now defaults to the canonical path and remains
  fail-closed; `DATAFEED_OBSERVER_DB` is the only explicit override. The
  worker checkout must be detached at the exact PR commit before the plist
  switch, otherwise the old issue-71 guard rejects the cutover.
- The issue-71 observer database is merged into `/Users/wendy/park-data/market/kline.db` with
  `ops/merge_mvp_databases.py`. Only `mvp_candles` is copied because run/receipt tables contain
  database-local IDs; the exact candle identity is the source-aware series key plus timestamp.
  Rows at or after 2026-09-02 are considered, and issue-71 wins conflicting candle values.
- The merge is transactional and repeatable: new rows are inserted, differing conflicts are
  replaced, identical conflicts are counted, and every invocation emits a JSON receipt. The old
  issue-71 database is retained under `.retired-20260908` for rollback and retention review.
- Gotchas: cutover must happen while the worker is idle between its observed cycle and the next
  `xx:05Z` run; plist files are backed up before editing; `launchctl bootout/bootstrap` is limited to the
  two named MVP jobs. The 8100 Query Service and its third database path remain unchanged.

## 2026-09-08 - Refresh Watchlist manifest from registry before daily ingestion (#169)

- The Watchlist daily job resolves `zinan92/watchlist` `--ref` to one commit, fetches that commit's
  `watchlist.yaml`, validates it, and atomically writes the registry snapshot and generated manifest
  before the existing daily seed is allowed to run. Fetch, parse, or validation failure writes a
  versioned blocker receipt and stops the job with a non-zero exit.
- `watchlist-registry-receipt-v1` records registry SHA, source SHA-256, generated manifest hash,
  added/removed instrument IDs, `changed`, and observation time. Re-running the same revision with
  the same generated manifest records `changed=false`; receipt output is configurable and defaults to
  `~/park-data/market/watchlist-registry-receipt.json`.
- Gotchas: `configs/watchlist_manifest.json` is a retained legacy consumer path and is not edited by
  this story; the daily seed now defaults to generated `configs/watchlist_registry_manifest.json`.
  The production plist is changed in-repository only; it is not loaded or restarted by this issue.

## 2026-09-08 - Align execution-market polling and record read-time freshness (#177)

- The execution-market worker defaults to a 5-second poll interval and schedules each poll on a
  close-aligned phase, one second after the UTC minute boundary. The repository plist records the
  same interval; the owner-controlled online worker was not restarted or modified here.
- Each receipt preserves the existing fetch-age p95 and adds per-instrument fetch-age statistics,
  plus cumulative per-instrument read-age p50/p95/max samples calculated as `now - last_closed_close_time`.
  Samples are stored in the worker-owned SQLite database so a 30-minute process receipt represents
  the complete observation window rather than only the latest cycle.
- Gotchas: the 30-minute acceptance must use temporary DB, receipt, and lock paths because the
  production lock is outside this worktree; read-age acceptance is separate from API schema and
  does not require changing the 8100 endpoint.
