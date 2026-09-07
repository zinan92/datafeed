# Issue #154 MVP database cutover verification

Status: cutover verified; the first complete post-cutover worker cycle is
running and remains `待 owner 验证` until a new `observed_at` is written.
No live/real-money path or 8100 service was changed.

## Repository and runtime identity

- Branch/PR: `codex/issue-154-db-cutover` / https://github.com/zinan92/datafeed/pull/159
- Runtime checkout: `/Users/wendy/datafeed-runtime-issue-115`, detached at `436a28e`
- Default observer guard: `/Users/wendy/park-data/market/kline.db`; explicit override is `DATAFEED_OBSERVER_DB`

## Cutover evidence

Pre-cutover commands recorded `launchctl print` for both named jobs, worker
stdout ending at `observed_at=2026-09-07T16:37:06+00:00`, and:

```text
/Users/wendy/datafeed-runtime-issue-71/data/kline.db: ok | 333140
/Users/wendy/park-data/market/kline.db: ok | 400810
```

The merge command was run before cutover with source-wins semantics:

```json
{"source_rows_considered":19311,"inserted_rows":0,"replaced_rows":399,"unchanged_conflict_rows":18912,"target_mvp_candles_after":400810}
```

The source was retained as
`/Users/wendy/datafeed-runtime-issue-71/data/kline.db.retired-20260908`;
its WAL/SHM sidecars were retained as the matching retired files. Plists were
backed up under `~/park-data/market/launchd-backup-20260908/`.

After the worker plist was corrected to point both `--db` and `--lock` at the
canonical directory, the named worker and API were bootstrapped. The worker is
currently running with:

```text
--db /Users/wendy/park-data/market/kline.db
--lock /Users/wendy/park-data/market/mvp-worker.lock
state = running
```

The post-cutover idempotency command used the retired source snapshot and
returned zero changes:

```json
{"source_rows_considered":19311,"inserted_rows":0,"replaced_rows":0,"unchanged_conflict_rows":19311,"target_mvp_candles_after":400810}
```

`curl --fail http://127.0.0.1:18171/api/health` returned HTTP 200 with
`status=ok`, `database_path=/Users/wendy/park-data/market/kline.db`, and
`build_sha=3167f7d1bc4cb19f50089891b01b94663a6efcc2`.

The 8100 `/health` path returned 404 because it is not an existing route; no
8100 plist, process, database, or API interface was changed. Its unchanged
state remains to be checked by the owner using the existing 8100 health/query
route.

## Validation still pending

Run the following after the current worker run completes and confirm the last
`observed_at` is newer than `2026-09-07T16:37:06+00:00`:

```sh
tail -1 ~/Library/Logs/datafeed/mvp-worker.stdout.log
launchctl print gui/$(id -u)/com.wendy.datafeed.mvp-worker | grep -E 'state =|--db|--lock|last exit code'
sqlite3 ~/park-data/market/kline.db 'PRAGMA integrity_check; SELECT count(*) FROM mvp_candles;'
curl --fail --silent http://127.0.0.1:18171/api/health
```

## Rollback

Only after booting out the two named jobs:

```sh
UID_VALUE=$(id -u)
launchctl bootout "gui/$UID_VALUE/com.wendy.datafeed.mvp-worker"
launchctl bootout "gui/$UID_VALUE/com.wendy.datafeed.mvp-api"
cp ~/park-data/market/launchd-backup-20260908/com.wendy.datafeed.mvp-worker.plist ~/Library/LaunchAgents/
cp ~/park-data/market/launchd-backup-20260908/com.wendy.datafeed.mvp-api.plist ~/Library/LaunchAgents/
mv ~/datafeed-runtime-issue-71/data/kline.db.retired-20260908 ~/datafeed-runtime-issue-71/data/kline.db
mv ~/datafeed-runtime-issue-71/data/kline.db.retired-20260908-wal ~/datafeed-runtime-issue-71/data/kline.db-wal
mv ~/datafeed-runtime-issue-71/data/kline.db.retired-20260908-shm ~/datafeed-runtime-issue-71/data/kline.db-shm
launchctl bootstrap "gui/$UID_VALUE" ~/Library/LaunchAgents/com.wendy.datafeed.mvp-worker.plist
launchctl bootstrap "gui/$UID_VALUE" ~/Library/LaunchAgents/com.wendy.datafeed.mvp-api.plist
```

Do not delete either database.
