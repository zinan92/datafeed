# Phase 1 39-cell matrix

The committed matrix is [`ops/phase1_matrix.py`](../../ops/phase1_matrix.py). It
contains exactly 17 assets and 39 required cells: daily and weekly for every
asset, plus 4H for DXY, Bitcoin, WTI, Gold, and Silver.

Run the read-only HTTP verifier against a disposable local runtime:

```bash
PYTHONPATH=src:. python3 ops/verify_phase1_matrix.py \
  --base-url http://127.0.0.1:18100 \
  --db-path /tmp/datafeed-phase1-matrix/kline.db \
  > /tmp/datafeed-phase1-matrix.json
```

Every request is a GET with:

```text
cache_policy=bypass&quality=strict&fallback_policy=none
```

The verifier does not write SQLite/cache rows, restart services, or change
launchd/.env. When `--db-path` is supplied it opens that database in read-only
mode and records before/after counts; `database_unchanged` must be `true`. Use
a temporary `KLINE_DB_PATH` when the service under test must also be isolated
from the resident 8100 process. A matrix result is
`ready` only when the response contains non-empty candles and no reject reason;
HTTP/source/quality failures remain visible as `unavailable` or `blocked` with
the source, provider symbol, timeframe, and failure reason.

The health envelope must identify `runtime`, `build_sha` (or explicitly
`unknown`), `registry_version`, module/runtime roots, and the database path.
The `providers.sources` section is capability/configuration evidence (`configured`
is separate from `available`, which remains false until a live probe); the
matrix cells are the live request evidence and must not be inferred from
registration alone.

On 2026-08-20, an isolated local run on the canonical implementation returned
31 `ready` and 8 `blocked` cells. The blocked cells were disclosed rather than
silently substituted: SCHD had a Yahoo historical OHLC invariant rejection,
and the three Yahoo futures had upstream no-data responses in that run. This
receipt is observational, not a deployment or 8100 cutover claim.
