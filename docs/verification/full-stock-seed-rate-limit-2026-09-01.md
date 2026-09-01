# Full 100+100 free-source seed evidence — 2026-09-01

## Scope and build

- Observer API: `http://127.0.0.1:18171/health-ui`
- Observer database: `/Users/wendy/datafeed-runtime-issue-71/data/kline.db`
- Build: `805eb9785ca01577477b88e3a1e688463a9629b6` (PR #108)
- Worker: `com.wendy.datafeed.mvp-worker`, KeepAlive, 4-hour interval, batch size 10, request interval 0.5 seconds, provider timeout 30 seconds, two retries with 2-second backoff.
- At `2026-09-01 21:27:35 CST`, `launchctl` reported the worker `state = running` with build `805eb978`; its last successful batch was `mvp-stocks-20260901T132635Z-intraday-us_stock-010`.
- Observer SQLite size at the same check: 118.43 MB.
- The resident `8100` service and `/Users/wendy/datafeed/data/kline.db` were not modified. NAS migration/backup remains pending.

## Full-cycle receipt

The first full cycle after the Yahoo ISO-window fix ran from `2026-09-01T13:03:21Z` through `2026-09-01T13:26:52Z`:

| Measure | Observed |
| --- | ---: |
| A-share universe | 100 |
| US-stock universe | 100 |
| Coarse batches (1d + 1w) | 20 |
| Intraday batches (15m + 1h + 4h) | 20 |
| `selected_total` | 400 |
| Persisted source observations | 1,000 |
| Batch status | 29 success / 11 partial |
| `remaining_after` | A-share 1 / US stock 0 |
| Rate-limit errors | 0 |
| Server errors | 1 |
| Worker-reported P95 latency | 3,534.3 ms |

The one server error occurred inside the known `601989` A-share intraday fallback path (`Tonghuashun` returned a transient 502). The same symbol also has Tencent HTTP 200 empty minute responses and a terminal Tonghuashun 404; its three intraday cells therefore remain explicit failures. Daily and weekly data for `601989` are present. No bars or watermarks were fabricated.

Re-reading the 1,000 persisted observation policies gives 1,019 provider attempts, 8 empty-response events, 1 server-error event, and 58 watermark-regression suppressions. Regression suppressions commit the valid candle/receipt facts while refusing to move a watermark backwards.

## Physical coverage

Distinct instrument/timeframe series with persisted `mvp_candles` after the cycle:

| Market | 15m | 1h | 4h | 1d | 1w |
| --- | ---: | ---: | ---: | ---: | ---: |
| A share | 99/100 | 99/100 | 99/100 | 100/100 | 100/100 |
| US stock | 100/100 | 100/100 | 100/100 | 100/100 | 100/100 |

That is **997/1,000 stock series** with real persisted K-line rows. The only missing series are `601989` at 15m/1h/4h, recorded fail-closed with source receipts.

The latest health API snapshot (`2026-09-01T13:28:18Z`) reports technical coverage of 199/208 (15m), 199/209 (1h), 199/208 (4h), and 200/216 for both 1d and 1w. The UI therefore displays 96%, 95%, 96%, 93%, and 93% respectively. Its overall state remains `失败` because three cells are genuinely unavailable and 57 cells retain explicit unverified entitlement metadata; the 0 “正常” count is an authorization policy count, not an assertion that the free-source K-lines are empty.

## Rate-limit probes

Before the full run, staged direct daily probes were executed without persistence:

| Market | Concurrency | Symbols | Success | P95 |
| --- | ---: | ---: | ---: | ---: |
| A share | 2 | 10 | 10/10 | 1,287.2 ms |
| A share | 4 | 20 | 20/20 | 1,768.8 ms |
| A share | 8 | 30 | 30/30 | 1,458.9 ms |
| US stock | 2 | 10 | 10/10 | 2,298.6 ms |
| US stock | 4 | 20 | 20/20 | 2,318.4 ms |
| US stock | 8 | 30 | 30/30 | 6,469.9 ms |

The first coarse experiment, before switching US daily/weekly to Sina primary, did hit 107 Yahoo `Too Many Requests` attempts. The recovery run after the Sina-primary change had zero rate-limit errors, and the first complete 40-batch cycle above also had zero. US intraday uses Yahoo with serial pacing and completed all 100 symbols successfully in the fixed build.

## Source and runtime conclusion

- A share: Tencent free source, Tonghuashun fallback for supported daily/hourly paths; 4h is derived from 15m and 1w from daily.
- US stock: Sina free source for daily/weekly, Yahoo fallback for coarse failures and Yahoo for intraday; 4h is derived from 15m.
- No Tushare renewal or new paid data entitlement was used.
- The worker is currently `running` under launchd and waiting for the next 4-hour cycle after the successful final US batch.
- The seven-day reliability gate (#71) is still open; this first cycle is evidence, not seven days of proof.
