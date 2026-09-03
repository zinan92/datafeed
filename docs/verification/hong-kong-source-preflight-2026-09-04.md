# Hong Kong Yahoo source preflight — 2026-09-04

| Registry code | Yahoo symbol | Asset class | Result | Candles | Latest closed bar |
|---|---|---|---|---:|---|
| 00100 | 0100.HK | hk_stock | pass | 3 | 2026-09-03 |
| 02513 | 2513.HK | hk_stock | pass | 3 | 2026-09-03 |
| 00700 | 0700.HK | hk_stock | pass | 3 | 2026-09-03 |
| 09988 | 9988.HK | hk_stock | pass | 3 | 2026-09-03 |

The production-shaped probe used the registered Yahoo HK adapter and daily timeframe. All four
responses carried `market=HK` and `listing_venue=HKEX`; each current row required the provider's
existing one-row repair path. No row or volume was synthesized.

Automated validation: 357 tests passed under `TZ=UTC`; changed-file Ruff passed. This ticket did not
run the Watchlist worker, write the Market Data Database, reload the scheduler, update the health UI,
deploy 8100, touch Screening/#115, or modify any consumer repository.
