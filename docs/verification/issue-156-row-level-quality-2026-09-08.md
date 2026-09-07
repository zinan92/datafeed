# Issue #156 row-level quality verification — 2026-09-08

## Local verification

The focused regression suite passed:

```sh
PYTHONPATH=src python3 -m pytest -q \
  tests/test_ingestion.py \
  tests/test_market_calendar.py \
  tests/test_mvp_storage.py \
  tests/test_quality.py
```

Result: `39 passed`.

The new ingestion regression supplies 100 daily rows with one malformed row.
Observed result: 99 promoted candles, one `partial` quality receipt, and one
`malformed` issue with the source timestamp and reason. The threshold regression
covers exactly 5% (`partial`) and 6% (`fail`). Existing response models and the
`/api/candles` route were not changed.

Full local verification:

```sh
PYTHONPATH=src python3 -m pytest -q
```

Result: `2 failed, 374 passed`. The two failures are pre-existing/outside
scope: the existing same-session-gap expectation is preserved by the focused
suite, while the existing Binance daily test depends on the machine's current
calendar date and returned no closed mocked row. No failure points to the
changed files.

Static checks:

```sh
git diff --check
python3 -m compileall -q src/kline
python3 -m ruff check src tests
```

`diff --check` and compilation passed. Ruff currently reports one pre-existing
unused `timedelta` import in `src/kline/providers/binance_usdm.py`; that file is
outside this issue's scope and was not changed.

## Required owner-side live verification — 待 owner 验证

No service was restarted and no plist was changed. After deploying this PR to
the owner-selected MVP/watchlist runtime, run the next natural cycle and
inspect the original health matrix and database. Suggested commands:

```sh
RUNTIME="$HOME/datafeed-runtime-issue-115"
PYTHONPATH="$RUNTIME/src" python3 -m ops.verify_watchlist_freshness
sqlite3 "$RUNTIME/data/kline.db" \
  "select instrument_id, timeframe, status, invalid_rows, details_json from mvp_quality_receipts where instrument_id in ('US.QCOM', 'KR.000660.KS', 'US.DHR') order by id desc limit 20;"
```

Record the original health URL, the cycle/run id, and the latest receipt rows
for QCOM, `000660.KS`, and DHR. Acceptance is met only when all three appear
in the database and their health-matrix status is no longer `failed`. This
live evidence remains **待 owner 验证** in this PR.

## Coverage note

Per the execution contract, `REGISTRY.md` was intentionally not modified;
the owner updates its coverage snapshot after merge. This document is the
issue-scoped verification record.
