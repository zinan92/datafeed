# Issue #154 MVP database cutover verification

Status: partial / rolled back because the deployed worker runtime rejected the
canonical database path. No live/real-money path or 8100 service was changed.

## Repository

- Branch: `codex/issue-154-db-cutover`
- Commit: `6ec2c12 ops: prepare MVP database cutover`
- Focused tests: `PYTHONPATH=src /usr/local/bin/python3 -m pytest -q tests/test_merge_mvp_databases.py tests/test_consumer_cutover_backfill.py tests/test_watchlist_launchd.py` → `6 passed`
- Full tests: `375 passed, 1 failed`; the failure is the pre-existing Binance daily cutoff test under the machine's current date.
- Gitleaks: `gitleaks detect --source . --redact --no-banner` → `no leaks found`.

## Cutover evidence

Before cutover, both databases passed `PRAGMA integrity_check`:

| Database | mvp_candles before |
| --- | ---: |
| issue-71 | 333,140 |
| canonical Market Data Database | 381,499 |

After stopping both named jobs, `ops/merge_mvp_databases.py` merged rows at or
after `2026-09-02T00:00:00+00:00`:

```json
{"source_rows_considered":19311,"inserted_rows":19311,"replaced_rows":0,"conflict_rows":0,"target_mvp_candles_after":400810}
```

The retired source database and target both returned `ok` from
`PRAGMA integrity_check`; a business-column comparison found `0` mismatches.
An idempotent rerun returned `inserted_rows=0`, `replaced_rows=0`, and
`unchanged_conflict_rows=19311`.

The database was retained as
`/Users/wendy/datafeed-runtime-issue-71/data/kline.db.retired-20260907` during
the attempted cutover, then restored to its original filename during rollback.
The target database remains at 400,810 rows. Plists were backed up under
`~/park-data/market/launchd-backup-20260907/` before modification.

## Blocker

After switching the worker `--db` to the target, the deployed
`/Users/wendy/datafeed-runtime-issue-115/ops/mvp_stock_seed.py` failed closed:

```text
ValueError: stock seed refuses non-observer database; expected /Users/wendy/datafeed-runtime-issue-71/data/kline.db
```

The two launchd jobs were restored to their original issue-71 paths. A complete
4h post-cutover write cycle is therefore not claimed.

8100 remained healthy and unchanged: runtime
`/Users/wendy/datafeed-runtime-market-cutover-107`, database
`/Users/wendy/datafeed/data/kline.db`, build
`ca53863f9a65073eb912b7ac1c2868be1b652364`.
