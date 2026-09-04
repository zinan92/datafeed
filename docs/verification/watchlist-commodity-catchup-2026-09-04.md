# Watchlist commodity post-close catch-up — 2026-09-04

## Directly measured

- Targeted identities: `WATCH.CROSS.GOLD` (`GC=F`), `WATCH.CROSS.SILVER` (`SI=F`),
  `WATCH.CROSS.WTI` (`CL=F`). No other Watchlist identity was selected.
- Run: `watchlist-20260904T012031Z-001`, one batch, three attempts.
- Result: 3/3 success; 3 observations; 3 quality receipts; 3 watermarks; 12 promoted candles.
- Quality: all three `pass`, zero forming/blocked issues.
- Rate-limit, forbidden, server, timeout and unclassified errors: all zero.
- P95 provider latency: 2,362.5 ms.
- Latest persisted closed daily bars: 2026-09-03 for Gold, Silver and WTI.

The preceding 07:15 scheduled run had correctly recorded these three bars as forming. The targeted
post-close run was performed after 08:15 Beijing time and promoted the same-day closed bars without
relaxing the quality gate.

## Boundaries

The catch-up reused the canonical Market Data Database and Watchlist lock. It did not touch Screening,
#115, 18171, 18172, 8100, manifest membership, source mappings, or consumer repositories.
