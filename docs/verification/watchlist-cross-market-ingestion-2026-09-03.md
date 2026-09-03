# Watchlist 58 项（含 16 个跨市场）持久化证据 — 2026-09-03

## 范围与构建

- Issue: #126
- Manifest: `configs/watchlist_manifest.json`
- Manifest version: `watchlist_universe_v1`
- Watchlist roster: 58 项（原 42 项 + 16 个跨市场资产）
- Timeframe: `1d` only；`15m`/`1h`/`4h`/`1w` 明确 `not_applicable`
- Database: `/Users/wendy/park-data/market/kline.db`
- Lock: `/Users/wendy/park-data/market/watchlist-worker.lock`

SPX/NDX 使用显式的 SPY/QQQ 代理身份（`metadata.identity_role=proxy`），DXY 使用
UUP 代理，VIX 使用真实 Yahoo `^VIX`。上证三条仍使用 `tencent_kline`；没有修改
`tencent_kline` provider 或 Screening manifest/worker。

## 真实运行

完整 58 项 runner 运行于 2026-09-03 15:50–16:02 CST，最终 receipt 为：

| 指标 | 实测 |
| --- | ---: |
| instrument_count | 58 |
| persisted_instrument_count | 58 |
| remaining_after | `[]` |
| 6 个批次 | 4 success，2 partial |
| attempts | 61 |
| p95 latency | 5,066.9 ms |
| rate-limit / 403 / 5xx / timeout | 0 / 0 / 0 / 0 |
| persisted candles | 173（本轮；历史数据已存在） |
| watermarks | 56；2 次因旧数据被水位保护跳过 |

本轮两条腾讯请求（002342、688525）返回了比已有水位更旧的日线，系统保持
`watermark regression suppressed`，没有倒退水位，也没有把已有数据删除；因此 runner
receipt 的整体状态按 fail-closed 规则为 `partial`，但 58/58 仍有可用持久化数据。

第一次运行发现 SPX/DXY/VIX 的空响应。根因是 ingestion 原先把展示代码传给 provider，
而不是传 `provider_symbol`；已修正并加回归测试。随后针对 SPY、UUP、`^VIX` 的真实重试
均成功，各返回 500 根日线，并写入对应 `WATCH.CROSS.*` 系列。

## 数据完整性

对 Market Data Database 的 Watchlist 行执行只读检查：

- Watchlist candle rows: 27,841（含历史与本轮写入）
- distinct instruments: 58
- null OHLC: 0
- OHLC invariant failures: 0
- negative volume: 0
- duplicate composite keys: 0
- future timestamps: 0
- `051505` rows: 0
- SQLite `PRAGMA quick_check`: `ok`

## 隔离证明

- `mvp_manifest.json`、`ops/mvp_stock_seed.py`、`tencent_kline` provider 未修改。
- `com.wendy.datafeed.mvp-worker`、`com.wendy.datafeed.mvp-api`（#115）未重启、未改配置。
- resident 8100 未重启。
- Watchlist 继续复用独立的 `com.wendy.datafeed.watchlist-daily` 日历任务、runtime 和 lock。
