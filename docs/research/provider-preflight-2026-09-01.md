# Provider Preflight Receipt

- Observed at: `2026-09-01T04:36:42+00:00`
- Targets: `9`
- Cells: `45`
- Decision: **partial**
- Read-only: `True`

## Decision matrix

| Asset | Source | Timeframe | Status | Reason | Rows | Latest closed | Policy |
|---|---|---:|---|---|---:|---|---|
| 600519 | eastmoney_kline | 15m | unavailable | request_failed | 0 | — | unverified |
| 600519 | eastmoney_kline | 1h | unavailable | request_failed | 0 | — | unverified |
| 600519 | eastmoney_kline | 1d | unavailable | request_failed | 0 | — | unverified |
| 600519 | eastmoney_kline | 4h | blocked | missing_input | 0 | — | unverified |
| 600519 | eastmoney_kline | 1w | blocked | missing_input | 0 | — | unverified |
| 300750 | eastmoney_kline | 15m | unavailable | request_failed | 0 | — | unverified |
| 300750 | eastmoney_kline | 1h | unavailable | request_failed | 0 | — | unverified |
| 300750 | eastmoney_kline | 1d | unavailable | request_failed | 0 | — | unverified |
| 300750 | eastmoney_kline | 4h | blocked | missing_input | 0 | — | unverified |
| 300750 | eastmoney_kline | 1w | blocked | missing_input | 0 | — | unverified |
| 688981 | eastmoney_kline | 15m | unavailable | request_failed | 0 | — | unverified |
| 688981 | eastmoney_kline | 1h | unavailable | request_failed | 0 | — | unverified |
| 688981 | eastmoney_kline | 1d | unavailable | request_failed | 0 | — | unverified |
| 688981 | eastmoney_kline | 4h | blocked | missing_input | 0 | — | unverified |
| 688981 | eastmoney_kline | 1w | blocked | missing_input | 0 | — | unverified |
| AAPL | yahoo_chart | 15m | partial | entitlement_unverified | 131 | 2026-08-31T20:00:00+00:00 | unverified |
| AAPL | yahoo_chart | 1h | partial | entitlement_unverified | 36 | 2026-08-31T20:00:00+00:00 | unverified |
| AAPL | yahoo_chart | 1d | partial | entitlement_unverified | 21 | 2026-08-31T13:30:00+00:00 | unverified |
| AAPL | yahoo_chart | 4h | blocked | transform_incomplete | 5 | 2026-08-31T13:30:00+00:00 | unverified |
| AAPL | yahoo_chart | 1w | blocked | transform_incomplete | 4 | 2026-08-28T00:00:00+00:00 | unverified |
| NVDA | yahoo_chart | 15m | partial | entitlement_unverified | 131 | 2026-08-31T20:00:00+00:00 | unverified |
| NVDA | yahoo_chart | 1h | partial | entitlement_unverified | 36 | 2026-08-31T20:00:00+00:00 | unverified |
| NVDA | yahoo_chart | 1d | partial | entitlement_unverified | 21 | 2026-08-31T13:30:00+00:00 | unverified |
| NVDA | yahoo_chart | 4h | blocked | transform_incomplete | 5 | 2026-08-31T13:30:00+00:00 | unverified |
| NVDA | yahoo_chart | 1w | blocked | transform_incomplete | 4 | 2026-08-28T00:00:00+00:00 | unverified |
| TSLA | yahoo_chart | 15m | partial | entitlement_unverified | 131 | 2026-08-31T20:00:00+00:00 | unverified |
| TSLA | yahoo_chart | 1h | partial | entitlement_unverified | 36 | 2026-08-31T20:00:00+00:00 | unverified |
| TSLA | yahoo_chart | 1d | partial | entitlement_unverified | 21 | 2026-08-31T13:30:00+00:00 | unverified |
| TSLA | yahoo_chart | 4h | blocked | transform_incomplete | 5 | 2026-08-31T13:30:00+00:00 | unverified |
| TSLA | yahoo_chart | 1w | blocked | transform_incomplete | 4 | 2026-08-28T00:00:00+00:00 | unverified |
| SPX | yahoo_chart | 15m | partial | entitlement_unverified | 131 | 2026-08-31T20:00:00+00:00 | unverified |
| SPX | yahoo_chart | 1h | partial | entitlement_unverified | 36 | 2026-08-31T20:00:00+00:00 | unverified |
| SPX | yahoo_chart | 1d | blocked | quality_invalid | 20 | 2026-08-31T13:30:00+00:00 | unverified |
| SPX | yahoo_chart | 4h | blocked | transform_incomplete | 5 | 2026-08-31T13:30:00+00:00 | unverified |
| SPX | yahoo_chart | 1w | blocked | transform_incomplete | 4 | 2026-08-28T00:00:00+00:00 | unverified |
| BTC | yahoo_chart | 15m | partial | entitlement_unverified | 402 | 2026-09-01T04:15:00+00:00 | unverified |
| BTC | yahoo_chart | 1h | partial | entitlement_unverified | 100 | 2026-09-01T03:00:00+00:00 | unverified |
| BTC | yahoo_chart | 1d | blocked | quality_invalid | 30 | 2026-08-30T00:00:00+00:00 | unverified |
| BTC | yahoo_chart | 4h | blocked | transform_incomplete | 25 | 2026-09-01T00:00:00+00:00 | unverified |
| BTC | yahoo_chart | 1w | partial | entitlement_unverified | 5 | 2026-08-30T00:00:00+00:00 | unverified |
| GOLD | yahoo_chart | 15m | blocked | quality_invalid | 274 | 2026-09-01T04:15:00+00:00 | unverified |
| GOLD | yahoo_chart | 1h | blocked | quality_invalid | 68 | 2026-09-01T02:00:00+00:00 | unverified |
| GOLD | yahoo_chart | 1d | partial | entitlement_unverified | 22 | 2026-09-01T04:00:00+00:00 | unverified |
| GOLD | yahoo_chart | 4h | blocked | transform_incomplete | 14 | 2026-08-31T23:00:00+00:00 | unverified |
| GOLD | yahoo_chart | 1w | partial | entitlement_unverified | 5 | 2026-08-30T00:00:00+00:00 | unverified |

## Next path by asset class

- `a_share`: **blocked** — resolve entitlement or parser blocker before adapter implementation
- `commodity`: **partial** — keep as pilot candidate; obtain persistence/derived-use evidence before promotion
- `crypto`: **partial** — keep as pilot candidate; obtain persistence/derived-use evidence before promotion
- `index`: **partial** — keep as pilot candidate; obtain persistence/derived-use evidence before promotion
- `us_stock`: **partial** — keep as pilot candidate; obtain persistence/derived-use evidence before promotion

## Safety

- No resident database was opened or changed.
- Response bodies are represented by hashes only; errors are redacted.
- `partial`, `blocked`, and `unavailable` cells are not promoted to canonical data.
