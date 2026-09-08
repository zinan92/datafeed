# Issue #170 verification

## Contract and scope

The verification command is read-only. It fetches the two supplied immutable
`zinan92/watchlist` revisions, compiles each into the Watchlist manifest, computes
added/removed instrument IDs, and probes only GET endpoints on the supplied 8100
base URL. It does not run the daily job, restart a service, modify a database, or
change launchd configuration.

## Real registry demonstration

Command:

```text
PYTHONPATH=src python3 -m ops.verify_watchlist_e2e \
  --before 30ba250fcb6fddff08eb5bbe84c895f2ce8a2ad6 \
  --after 29ce3c0ad6c6d5f822c860c42ae5ccd251c240d2 \
  --base-url http://127.0.0.1:8100 \
  --output docs/verification/issue-170-watchlist-e2e-2026-09-08.json
```

The command exited `0` with `status=pass`, `added_count=0`,
`removed_count=0`, and `instrument_delta_count=0`. The registry revisions do
contain a non-instrument metadata change (`geopolitical-crisis.links_assets`),
but the compiled 107-instrument K-line universe is unchanged. Consequently no
差异 instrument exists for which it would be truthful to report an 8100 candle
or matrix result. The machine receipt is in
`issue-170-watchlist-e2e-2026-09-08.json`.

Real post-daily verification remains `owner_pending`: an owner-provided registry
revision that actually adds/removes an instrument must be followed by the real
daily update, then this command must be rerun against 8100. No online job was
touched here.

## #174 legacy manifest finding

The concern is confirmed. PR #174 moved the daily seed to the generated
`configs/watchlist_registry_manifest.json`, while
`src/kline/market_query.py:323` still loads
`configs/watchlist_manifest.json`. This is a retained legacy consumer path, not
a reason to alter `src` in #170; no source file was changed.

## Validation

```text
PYTHONPATH=src python3 -m pytest -q tests/test_verify_watchlist_e2e.py
3 passed

python3 -m py_compile ops/verify_watchlist_e2e.py
git diff --check
```
