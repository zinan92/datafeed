# Watchlist registry compiler verification — 2026-09-04

## Directly measured

- Source repository: `zinan92/watchlist`.
- Pinned commit: `29ce3c0ad6c6d5f822c860c42ae5ccd251c240d2`.
- Upstream YAML SHA-256: `e43fdea4c50b0d559803f271ef098b2dbcac1aa0631dfde2c5150a601baddb0b`.
- Assets compiled: 16.
- Unique listed companies compiled: 91 (CN 38, US 48, HK 4, KR 1).
- Total generated Market Instruments: 107.
- Daily contract: 107 required `1d` cells and 428 explicit not-applicable cells.
- Hong Kong symbols compiled: `0100.HK`, `2513.HK`, `0700.HK`, `9988.HK`.
- Recompiling the committed snapshot produced byte-identical JSON.
- Focused registry/manifest/health regression tests: 19 passed.
- Full suite under `TZ=UTC`: 354 passed.
- Changed-file Ruff: passed.

## Code-reviewed facts

- The active 58-member Watchlist manifest is unchanged by #139; activation is deliberately blocked
  behind #140, #141 and #142.
- Existing 16 cross-market instrument IDs, source/provider fields, session fields and proxy metadata
  match the current manifest on all identity fields.
- Multi-sector duplicate targets are deduplicated for collection and retain all membership/reason
  records. BTC/ETH/HYPE target rows are not duplicated over their asset entries.
- The compiler has no runtime GitHub dependency. It consumes an explicitly pinned local source and
  commits the normalized snapshot plus generated manifest for inspection.

## Boundaries

No HK live provider adapter, historical backfill, schedule reload, health UI change, 8100 deployment,
Screening change, #115 change, consumer-repository change, NAS migration, intraday/weekly collection,
or historical-row deletion was performed by #139.
