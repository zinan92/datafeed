# China index daily freshness verification — 2026-09-04

Issue: #151

## Contract seam

The Watchlist manifest now declares how a daily timestamp represents a trading session:

- `tencent_stock_free` A-shares use `session_date_at_local_midnight`; for example,
  `2026-09-02T16:00:00Z` is the Asia/Shanghai midnight that represents the 2026-09-03 session.
- `tencent_kline` China indexes use `session_date_at_utc_midnight`; for example,
  `2026-09-03T00:00:00Z` represents the 2026-09-03 session directly.

The Watchlist runner and Health matrix both interpret the declared convention and compare the
represented session date with the latest closed session from the instrument calendar. Existing
A-share candle timestamps were not rewritten.

## Before backfill

The fixed Health logic was run read-only against the canonical Market Data Database before the
repair. It reported all three China indexes as stale:

| Instrument | Stored latest | Status | Reason |
|---|---|---|---|
| `WATCH.CROSS.SHCOMP` | `2026-09-02T00:00:00Z` | `stale` | `freshness_sla_exceeded` |
| `WATCH.CROSS.STAR50` | `2026-09-02T00:00:00Z` | `stale` | `freshness_sla_exceeded` |
| `WATCH.CROSS.DIVIDEND` | `2026-09-02T00:00:00Z` | `stale` | `freshness_sla_exceeded` |

This is the regression state that the previous receipt incorrectly called ready.

## Real ingestion

A real 107-instrument run started at `2026-09-04T04:44:47Z` from the issue branch, using the
canonical manifest, Market Data Database and Watchlist lock. The previous latest receipt was backed
up to `/Users/wendy/park-data/market/watchlist-latest.pre-151-20260904.json` before the run.

Receipt: `/Users/wendy/park-data/market/watchlist-latest.json`

| Metric | Result |
|---|---:|
| instruments | 107 |
| persisted instruments | 107 |
| runner status counts | 103 ready / 0 stale / 4 fail |
| observations | 107 |
| quality receipts | 107 |
| watermarks written | 103 |
| promoted candles | 315 |
| rate-limit / 403 / 5xx / timeout errors | 0 / 0 / 0 / 0 |
| P95 latency | 2,848.0 ms |

The four fails were `WATCH.CN.A.688235`, `WATCH.CN.A.688825`, `WATCH.CN.A.688836`, and
`WATCH.CN.A.688981`. The run occurred during the A-share session; Tencent returned a
`2026-09-03T16:00:00Z` timestamp representing the still-forming 2026-09-04 session for those four
short histories. The existing quality gate correctly rejected each with `bar ends after cutoff`.
They were not relabelled ready and no quality rule was weakened.

## Backfill result

| Instrument | 2026-09-03 OHLC | Observation | Quality | Watermark |
|---|---|---|---|---|
| `WATCH.CROSS.SHCOMP` | 3952.79 / 3968.11 / 3930.45 / **3942.09** | success | pass | `2026-09-03T00:00:00Z` |
| `WATCH.CROSS.STAR50` | 1634.52 / 1636.32 / 1596.62 / 1611.17 | success | pass | `2026-09-03T00:00:00Z` |
| `WATCH.CROSS.DIVIDEND` | 3344.68 / 3393.53 / 3338.53 / 3347.41 | success | pass | `2026-09-03T00:00:00Z` |

After the write, the fixed Health model reports all three index daily cells as
`ready_unverified`, with the explicit `session_date_at_utc_midnight` convention visible in cell
metadata. The full Watchlist snapshot truthfully reports 103 `ready_unverified` and the four
forming-bar source observations as failed.

## Automated verification

- Focused Watchlist manifest, registry, runner, Health, and combined-Health tests: 35 passed.
- New regression seams cover local-midnight and UTC-midnight storage for the same `cn_a` calendar,
  stale runner receipts, Health stale display, and rejection of an incorrect manifest convention.
- Existing Screening manifest, worker and #115 runtime were not changed.
