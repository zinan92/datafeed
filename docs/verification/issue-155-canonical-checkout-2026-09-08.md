# Issue 155 canonical checkout verification

Date: 2026-09-08 (Asia/Shanghai)

## Release

- Canonical checkout: `/Users/wendy/park-runtime/datafeed`
- Checkout state: detached, clean
- Release SHA: `79719b402470f183ea3574bb17b315faf981b861`
- Receipt: `/Users/wendy/park-data/datafeed/release_receipt.json`
- Backup directory: `/Users/wendy/park-data/launchd-backup-20260908/`

The five managed plist files are in `ops/launchd/`. The release operation
backed up the installed files, rendered the release SHA, and used
`launchctl bootout` followed by `launchctl bootstrap` for each label. macOS
returned transient exit 5 errors during some bootstraps; bounded retries
completed the registrations.

## Live results

All five labels report the canonical working directory. `com.wendy.datafeed`,
`com.wendy.datafeed.mvp-api`, and `com.wendy.datafeed.mvp-worker` report
`state = running`; the calendar-only watchlist job reports `not running` after
its non-calendar launch attempt. The health dashboard is registered but its
process exits because its explicitly preserved observer database path
`/Users/wendy/datafeed-runtime-issue-71/data/kline.db` no longer exists after
the prior database cutover. This issue does not change database paths.

8100 probe:

```text
runtime_root=/Users/wendy/park-runtime/datafeed
module_root=/Users/wendy/park-runtime/datafeed
build_sha=79719b402470f183ea3574bb17b315faf981b861
database_path=/Users/wendy/datafeed/data/kline.db
```

The MVP API is listening on 18171. The dashboard endpoint is unavailable for
the database-path reason above.

## Not completed

The #134 paired replay was started against the old runtime on temporary port
8101 and the post-release 8100 service. It reached upstream Yahoo requests but
did not finish within the bounded observation window; no comparison receipt was
written and no byte-identical pass is claimed. The temporary baseline process
was stopped.

`REGISTRY.md` was not changed because the owner instruction requires its
post-merge update.
