# Provider Preflight Receipt

- Observed at: `2026-09-01T04:43:01+00:00`
- Targets: `3`
- Cells: `15`
- Decision: **partial**
- Read-only: `True`

## Decision matrix

| Asset | Source | Timeframe | Status | Reason | Rows | Latest closed | Policy |
|---|---|---:|---|---|---:|---|---|
| 600519 | easy_tdx_mac | 15m | partial | entitlement_unverified | 799 | 2026-09-01T03:15:00+00:00 | unverified |
| 600519 | easy_tdx_mac | 1h | partial | entitlement_unverified | 799 | 2026-09-01T02:30:00+00:00 | unverified |
| 600519 | easy_tdx_mac | 1d | partial | entitlement_unverified | 799 | 2026-08-30T16:00:00+00:00 | unverified |
| 600519 | easy_tdx_mac | 4h | blocked | no_complete_derived_bars | 0 | — | unverified |
| 600519 | easy_tdx_mac | 1w | partial | entitlement_unverified | 169 | 2026-08-28T00:00:00+00:00 | unverified |
| 300750 | easy_tdx_mac | 15m | partial | entitlement_unverified | 799 | 2026-09-01T03:15:00+00:00 | unverified |
| 300750 | easy_tdx_mac | 1h | partial | entitlement_unverified | 799 | 2026-09-01T02:30:00+00:00 | unverified |
| 300750 | easy_tdx_mac | 1d | partial | entitlement_unverified | 799 | 2026-08-30T16:00:00+00:00 | unverified |
| 300750 | easy_tdx_mac | 4h | blocked | no_complete_derived_bars | 0 | — | unverified |
| 300750 | easy_tdx_mac | 1w | partial | entitlement_unverified | 169 | 2026-08-28T00:00:00+00:00 | unverified |
| 688981 | easy_tdx_mac | 15m | partial | entitlement_unverified | 799 | 2026-09-01T03:15:00+00:00 | unverified |
| 688981 | easy_tdx_mac | 1h | partial | entitlement_unverified | 799 | 2026-09-01T02:30:00+00:00 | unverified |
| 688981 | easy_tdx_mac | 1d | partial | entitlement_unverified | 799 | 2026-08-30T16:00:00+00:00 | unverified |
| 688981 | easy_tdx_mac | 4h | blocked | no_complete_derived_bars | 0 | — | unverified |
| 688981 | easy_tdx_mac | 1w | partial | entitlement_unverified | 169 | 2026-08-28T00:00:00+00:00 | unverified |

## Next path by asset class

- `a_share`: **partial** — keep as pilot candidate; obtain persistence/derived-use evidence before promotion

## Safety

- No resident database was opened or changed.
- Response bodies are represented by hashes only; errors are redacted.
- `partial`, `blocked`, and `unavailable` cells are not promoted to canonical data.
