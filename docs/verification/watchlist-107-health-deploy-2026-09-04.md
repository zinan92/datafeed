# Watchlist 107-member Health and runtime deployment verification — 2026-09-04

## Directly measured

- Candidate build: `ea11a73aa9c44194cef663c6c9c02aedb4c76b7f`.
- Health runtime: 18172 on the candidate checkout; combined API scope is 323 instruments
  (Screening 216 + Watchlist 107).
- Watchlist slice: 535 cells (107 instruments × 5 timeframes), 107 daily applicable cells and
  428 not-applicable cells. All 107 daily cells are `ready_unverified`; no registry provenance
  metadata is missing.
- HK grouping: 4 instruments / 20 cells. KR grouping: 1 instrument / 5 cells.
- Every HK cell exposes `hk_stock`, Yahoo/HKEX identity, the pinned registry commit and source hash.
- 8100 candidate build: `market_first`, active runtime `/Users/wendy/datafeed-runtime-market-cutover-107`.
  A real `hk_stock/00100` request returned 3 candles from `market_data_database`; a real A-share
  `600900` request returned 3 Tencent candles from `market_data_database`.
- Newsletter real source bundle: 31/31 ready, 9 Market Data Database and 22 legacy/upstream.
- Human Review real overview: 16 assets; 33 ready, 1 insufficient-history, 14 unavailable; primary
  routes 6 Market Data Database and 28 legacy/upstream.

The combined overall status remains `failed` because the independent Screening slice still has its
existing 16 authorization-blocked cells. The Watchlist-filtered slice is healthy and is not masked
by that aggregate status.

## Cutover and rollback drill

The candidate services were loaded through independent launchd plists using `bootout → bootstrap`:

1. old 8100/18172 services → candidate `ea11a73` services;
2. candidate services → byte-identical pre-142 rollback plists (8100 build `2b6b78c`, 18172 build
   `02896d1`), with real health probes showing the old 58-member scope;
3. rollback services → candidate services, with the real HK/A-share Market DB probes above.

The pre-142 plist hashes are preserved in:

- 8100: `7e9b77de2b893b92bae97c027a0836b75dfb04ee156421f425159bec0e60f05a`
- 18172: `56113af83213caf27a0ccbc8d0a16c32e8569e9e5e9f201757a1d4b07e008ead`

The final canonical plist consolidation happens after PR merge; the rehearsal plists remain
recoverable until then.

## Unchanged processes and boundaries

- #115 API PID 878/build `3167f7d`; #115 worker PID 912/build `3167f7d`.
- Human Review PID 904; consumer repositories unchanged.
- No Screening manifest/runner change, no NAS migration, no Treasury/intraday/weekly expansion.

Automated validation before deployment: 359 tests passed under `TZ=UTC`; changed-file Ruff passed;
gitleaks passed.
