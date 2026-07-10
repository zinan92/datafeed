# Decision Log

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
