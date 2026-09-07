# Issue #154 MVP database cutover runbook

Scope: only `com.wendy.datafeed.mvp-worker` and `com.wendy.datafeed.mvp-api`.
The 8100 Query Service, its database, all other runtime directories and all
live/real-money paths are out of scope.

## Planned commands (operator report before execution)

```sh
set -euo pipefail
SOURCE=/Users/wendy/datafeed-runtime-issue-71/data/kline.db
TARGET=/Users/wendy/park-data/market/kline.db
BACKUP_DIR="$HOME/park-data/market/launchd-backup-$(date +%Y%m%d)"
mkdir -p "$BACKUP_DIR"
cp "$HOME/Library/LaunchAgents/com.wendy.datafeed.mvp-worker.plist" "$BACKUP_DIR/"
cp "$HOME/Library/LaunchAgents/com.wendy.datafeed.mvp-api.plist" "$BACKUP_DIR/"
PYTHONPATH=src python -m ops.merge_mvp_databases --source "$SOURCE" --target "$TARGET" \
  --since 2026-09-02T00:00:00+00:00 --receipt "$HOME/park-data/market/issue-154-merge.json"
mv "$SOURCE" "$HOME/datafeed-runtime-issue-71/data/kline.db.retired-$(date +%Y%m%d)"
plutil -replace ProgramArguments.6 -string "$TARGET" "$HOME/Library/LaunchAgents/com.wendy.datafeed.mvp-worker.plist"
plutil -replace EnvironmentVariables.KLINE_DB_PATH -string "$TARGET" "$HOME/Library/LaunchAgents/com.wendy.datafeed.mvp-api.plist"
launchctl bootout "gui/$(id -u)/com.wendy.datafeed.mvp-worker"
launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.wendy.datafeed.mvp-worker.plist"
launchctl bootout "gui/$(id -u)/com.wendy.datafeed.mvp-api"
launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.wendy.datafeed.mvp-api.plist"
```

Before executing: record `launchctl print`, `tail` of worker stdout, both DB
integrity checks/counts, and confirm the worker is idle. Execute only after the
last `observed_at` and before the next `xx:05Z` cycle. The old database is
renamed, never deleted, and remains available for at least seven days.

After executing: verify both launchd argument/environment paths, API health at
`127.0.0.1:18171`, worker output, target integrity/counts, and a complete 4h
cycle. Confirm 8100 is unchanged. Re-run the merge command and retain its
zero-insert/zero-replace receipt as the idempotency check.

Rollback: restore the backed-up plists with `cp`, bootout the two named jobs,
rename the retired database back to `kline.db` only after confirming the target
jobs are stopped, then bootstrap the two named jobs from the restored plists.
Do not delete either database.
