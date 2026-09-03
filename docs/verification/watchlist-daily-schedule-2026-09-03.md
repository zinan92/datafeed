# Watchlist 日线调度与收口证据 — 2026-09-03

## 部署合同

- Issue: #122
- PR: #123（待本证据提交后合并）
- launchd label: `com.wendy.datafeed.watchlist-daily`
- Runtime: `/Users/wendy/datafeed-runtime-watchlist`
- Build at final catch-up: `0541cddbba9539dafab94bb33ecbd3913a696cac`（包含 #124）
- Schedule: 周一至周五北京时间 07:15 (`StartCalendarInterval`)
- Database: `/Users/wendy/park-data/market/kline.db`
- Lock: `/Users/wendy/park-data/market/watchlist-worker.lock`
- `RunAtLoad`、`KeepAlive`、`StartInterval`：均不存在

`launchctl print` 直接显示五个 weekday calendar event，`keepalive=0`；任务使用独立
runtime、manifest、lock、receipt 和日志路径。Resident 8100 与 #115 observer 的 plist、PID、
工作目录均未改变。

## 最终 catch-up

2026-09-03 15:16 CST 由 launchd 手动触发，任务于 15:18 CST 完成，实际 receipt：
`watchlist-20260903T071656Z-001` 到 `watchlist-20260903T071817Z-005`。

| 指标 | 实测 |
| --- | ---: |
| launchd exit code | 0 |
| current-ready | 42/42 |
| current failed/partial | 0 |
| persisted instruments | 42 |
| source observations | 42 |
| quality receipts | 42 |
| watermarks | 42 |
| provider attempts | 42 |
| error counts | `{}` |
| rate-limit / 403 / 5xx / timeout | 0 / 0 / 0 / 0 |
| P95 latency | 1,116.5 ms |

## Freshness and integrity

按市场本地日期归一化后的最新闭合日：

| 类型 | 标的数 | 最新闭合日 |
| --- | ---: | --- |
| A 股个股 | 13 | 2026-09-02 |
| A 股新股 | 3 (`688825`,`688836`,`688981`) | 2026-09-03 |
| A 股 ETF | 4 | 2026-09-02 |
| 美股/韩股 | 22 | 2026-09-02 |

三只 9 月 3 日 A 股行是在 15:00 收盘后观察到的已闭合日线，不是 forming bar。

SQLite 检查：

- `mvp_candles` Watchlist rows: 19,837
- null OHLC: 0
- OHLC invariant failures: 0
- negative volume: 0
- duplicate composite keys: 0
- future timestamps: 0
- `051505` rows: 0
- `PRAGMA quick_check`: `ok`

## A 股源与隔离

20 个 A 股/ETF cells 全部走 Tencent，20/20 HTTP 200；没有切换到新浪、TuShare 或任何新
授权源。Watchlist runner 使用独立 lock，未打开或写入 Screening 的 SAFE_OBSERVER_DB；
`mvp_stock_seed.py`、Screening manifest、resident 8100 和 #115 worker 均未改动。

## 失败可见性

此前 09:39 CST 的手动补跑在 A 股开盘后诚实返回 exit 2（形成中日线），并记录 AMZN/HOOD
当时 Yahoo 的 NaN close；在 #124 修复且收盘后重跑后，最新 receipt 为 exit 0。Runner 的
current-cycle 状态不会被历史 candle 漂绿，失败会保留非零退出码和 receipt/log。
