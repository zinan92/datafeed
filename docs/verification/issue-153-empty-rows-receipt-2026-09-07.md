# Issue #153 empty-rows receipt verification — 2026-09-07

## Contract and boundary

Issue: [#153](https://github.com/zinan92/datafeed/issues/153).

The reported pre-fix evidence was 275 identical tracebacks in
`com.wendy.datafeed.mvp-worker` stderr and `launchd runs = 277`; the failure
was `rows[0].key` in `_quality_receipt` after a provider returned zero rows.
This worktree only changes the ingestion receipt path, the accepted quality
receipt statuses, and regression tests. It does not change the 8100 HTTP API,
data sources, DB paths, live/real-money paths, launchd plists, or running
workers.

## Local post-fix regression evidence

Command:

```sh
PYTHONPATH=src uv run pytest -q \
  tests/test_ingestion.py \
  tests/test_mvp_storage.py \
  tests/test_health_matrix.py
```

Result: `38 passed`.

The new regression exercises one run containing BTC with zero provider rows
and ETH with one valid candle. Observed assertions:

| Evidence | Result |
| --- | --- |
| Empty BTC run cell | `unavailable` |
| Empty BTC quality receipt | `missing`, `blocked_cells=1`, issue `missing` |
| ETH in the same run | `ready`, one candle promoted |
| Health matrix BTC cell | `unavailable` |
| Health matrix ETH cell | `ready_unverified` (technical status remains ready) |
| Existing non-empty ingestion/storage/health tests | Included in 38 passed |

This is deterministic local evidence, not a substitute for the required
post-deployment two-cycle observation.

## Required owner deployment evidence — pending

Status: **待 owner 部署后验证**. This turn did not restart or reload the
worker, so there is no honest post-fix two-cycle stderr/run-count result here.

After deploying the PR to `~/datafeed-runtime-issue-115`, the owner should
capture a baseline, observe two natural 4-hour cycles, and record the output:

```sh
RUNTIME="$HOME/datafeed-runtime-issue-115"
LOG="$HOME/Library/Logs/datafeed/mvp-worker.stderr.log"
launchctl print "gui/$(id -u)/com.wendy.datafeed.mvp-worker" \
  | rg 'state =|runs =|path =|program|stdout path|stderr path'
tail -n 0 -F "$LOG"
```

At each cycle boundary, record the worker `runs` value and the new stderr
lines. Acceptance is met only if two consecutive 4-hour cycles add no new
traceback and `runs` does not increase once per cycle; the health UI/API must
show the affected cell as `unavailable`, not an overall `failed` round. Append
the two-cycle before/after values and the original health URL to this document
after owner-side deployment.
