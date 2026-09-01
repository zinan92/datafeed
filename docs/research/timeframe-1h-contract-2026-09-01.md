# 1h Timeframe Contract Receipt

**Issue:** #68
**Observed:** 2026-09-01 (Asia/Shanghai)
**Status:** contract implemented; source entitlement remains explicit

## Contract

- MVP persisted/queryable timeframes are `15m`, `1h`, `4h`, `1d`, and `1w`.
- `30m` remains excluded from the MVP namespace.
- `1h` native rows are `is_derived=false`.
- `1h` rows derived from `15m` are `is_derived=true` and carry a transform receipt.
- Existing 4h and completed-week transform semantics remain unchanged.

## Runtime changes

- Manifest and reserve identities classify all five timeframes.
- SQLite series keys, transform receipts, and entitlement receipts accept `1h`.
- The worker, ingestion orchestrator, health/serving matrix, TuShare MVP adapter, authorized-US adapter, and Hyperliquid source expose the new timeframe.
- Calendar quality accepts `1h`; `cn_a_session_1h_v1` provides a deterministic 15m→1h transform.
- Hyperliquid BTC/ETH/HYPE `1h` capability is registered as native; configured source validation remains source-aware.

## Evidence

- Full test suite: `241 passed`.
- Ruff checks and `git diff --check`: passed.
- The dedicated 1h tests cover manifest state, SQLite persistence, calendar aggregation, quality gaps, TuShare `60min`, authorized-US native 1h, Hyperliquid native 1h, and ingestion persistence of a derived 1h row with a transform receipt.
- The 3+3 source preflight remains the authority for provider readiness: easy_tdx A-share data is technically available but entitlement is unverified; Yahoo US data is a technical partial; no source is promoted automatically.

## Safety

No provider purchase/renewal, credential logging, full-universe run, NAS migration, production cutover, trading path, or legacy data deletion is part of this change. A source without persistence/derived-use evidence remains `blocked_for_entitlement`/`unavailable` and cannot advance a watermark.
