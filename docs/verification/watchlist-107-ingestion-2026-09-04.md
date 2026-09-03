# Watchlist 107-member daily ingestion verification — 2026-09-04

## Directly measured

- Manifest: pinned `watchlist@29ce3c0`, `watchlist_universe_v1`, hash
  `cc422784b5716ec82ebac80f5c559e62cbe288a34270d0d246fe5fe06b0a04f9`.
- Run: `watchlist-20260903T162416Z-001` through `-011` (11 batches, 107 attempts).
- Configured members: 107; active members with persisted closed daily data: 107/107.
- Batch status: 11/11 success; current failures: 0; remaining after: 0.
- Promoted candles in the verification run: 1,630; observations: 107; quality receipts: 107;
  watermarks: 107.
- Rate-limit/forbidden/server/timeout/unclassified error counters: all 0.
- P95 attempt latency: 1,854.8 ms.
- Duplicate storage keys: 0; future timestamps: 0.
- HK rows persisted under `yahoo_finance_hk`: 4 instruments, 1,321 total daily rows; latest closed
  bar 2026-09-03.

The first scheduled-shaped run exposed the missing `hk_equities` calendar and correctly recorded all
four HK cells as failed. After adding the calendar and rerunning, all four passed; the failed first
receipt remains preserved as evidence and was not overwritten.

## Retention check

The database contains 107 active registry identities plus 13 retired identities from the prior local
58-member Watchlist. Retired rows were retained and are excluded only by the active manifest; no
historical row was deleted.

## Boundaries

The real run used the canonical Market Data Database and dedicated Watchlist lock. The active launchd
schedule, health UI and 8100 runtime were not reloaded in this ticket; scheduler activation and
consumer/runtime deployment are completed by #142.

Automated validation: 357 tests passed under `TZ=UTC`; changed-file Ruff passed; gitleaks passed.
