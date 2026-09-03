# ADR 0004: Preserve the Query Service HTTP contract with an identity-aware dual-store adapter

- Status: Accepted
- Date: 2026-09-03
- Decision owner: Park
- Scope: Watchlist consumers on `127.0.0.1:8100`

## Context

The earlier version of this ADR assumed that consumer migration required only changing
`KLINE_DB_PATH`. Runtime inspection disproved that assumption:

- the legacy database exposes `klines` keyed by ticker;
- the Market Data Database exposes `mvp_candles` keyed by source-aware `instrument_id`,
  `manifest_version`, timeframe and adjustment basis;
- changing the path alone makes the existing Query Service query a table that does not exist.

Newsletter and Human K-line Review must keep their existing routes, request policies and response
shape. They cannot be changed as part of this migration. Treasury level/spread series are not Candle
Instruments and remain outside this database and this cutover.

## Decision

The resident Query Service keeps `KLINE_DB_PATH` for its legacy cache and adds a read-only
Market Data Database input configured by `KLINE_MARKET_DB_PATH`. The default
`KLINE_QUERY_BACKEND=legacy` changes nothing. `market_first` enables an identity-aware adapter:

1. Resolve only Watchlist identities, preferring `display_symbol` and then `provider_symbol`.
2. Permit the five measured legacy asset-class aliases only: UUP, SPY, QQQ, SCHD and `^VIX`
   requested as `us_stock`.
3. Serve only persisted native daily series with a watermark, sufficient history for both the
   configured minimum and requested limit, and compatible volume semantics.
4. Route every miss through the unchanged legacy cache/upstream path. The response explicitly
   records `source_identity.served_from`, `query_served_from`, and a stable miss reason.
5. Keep the public top-level `served_from` contract unchanged because Newsletter validates it.
   The exact storage origin is nested in `source_identity`.

The adapter also preserves the provider observation identity persisted with the Market Data
Database receipt. It does not fabricate volume, synthesize missing rows or relax any quality gate.
Series with `volume_semantics=not_applicable` remain legacy-served for byte compatibility. DXY/UUP
also remains legacy-served because its longer Yahoo history intermittently omits 2026-08-28; this is
reported as `market_window_unverified`, not hidden.

## Consequences

- Newsletter and Human Review can use Market Data Database rows without code changes.
- Intraday cells, the two dated micro contracts, insufficient histories and unverified series remain
  explicit legacy/upstream reads.
- `/api/health.query_backend` reports market hits, misses, unique legacy cells and miss reasons
  without exposing database paths.
- Rollback is configuration-only: restore the backed-up legacy plist with `bootout` then
  `bootstrap`; no database rewrite or code revert is required.
- Screening Universe routing, #115, 18171 and 18172 are unaffected.

## Rejected alternative

Pointing `KLINE_DB_PATH` directly at the Market Data Database was rejected because it breaks on the
schema boundary and discards source-aware identity and provenance.
