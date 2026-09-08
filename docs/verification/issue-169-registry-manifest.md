# Issue #169 verification

Verification uses this worktree and temporary output paths. The resident Watchlist job under
`~/park-runtime/datafeed`, its database, and launchd state were not touched or restarted.

```text
PYTHONPATH=src python3 -m pytest -q tests/test_watchlist_seed.py tests/test_sync_watchlist_registry.py
PYTHONPATH=src python3 -m ops.sync_watchlist_registry --ref main --dry-run
```

A two-run receipt check is performed with temporary snapshot, manifest, and receipt paths so the
production receipt is not changed. The final output is recorded in the PR description.

No 8100 interface, Screening universe, live/real-money path, resident process, launchd plist in
`~/Library/LaunchAgents`, or production database was changed.
