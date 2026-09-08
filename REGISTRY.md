# datafeed

## 要去哪里

多资产 K 线数据服务：`ticker + timeframe → 标准化 OHLCV + provenance`，覆盖 A 股、美股、加密和商品；近期喂养 trading-system 与 tokenpulse，远期可作为独立 API 产品外卖。

## 现在在哪里（截至 2026-09-08）

- #117 已合并：股票 worker 的四小时周期锚定到周期开始，并加入 A 股 15m/1h 开盘缓冲；forming bar 在缓冲期不持久化并保持 partial。PR 验证为全套 373 tests passed；24 小时真实运行证据仍待验收。
- #158 已合并：MVP 空 rows 保存为 `missing` quality receipt，并在 health matrix 中显式为 `unavailable`，单个空响应不会终止整轮；PR 验证为 38 个聚焦测试通过，完整套件为 373 passed、1 个既有日期敏感失败。真实两轮 worker 证据仍由 owner 补齐。
- #159 已合并：MVP stock seed、MVP API/worker 使用 canonical Market Data Database `/Users/wendy/park-data/market/kline.db`；issue-71 数据库保留为 retired 来源。PR 记录了切换前后数据库检查、幂等复跑和 18171 HTTP 200；首个完整切换后 worker cycle 当时仍待 owner 验证。
- #160 已合并：坏行按行隔离并记录 timestamp/reason，默认 5% threshold；可用 closed rows 继续 promotion，`/api/candles` schema 不变。PR 验证为 39 个聚焦测试通过，完整套件为 375 passed、1 个既有日期敏感失败；QCOM、`000660.KS`、DHR 的 live next-cycle 仍待 owner 验证。
- #162 已合并：五个 managed launchd job 收敛到 `/Users/wendy/park-runtime/datafeed` canonical checkout；PR 验证为 plist lint、release script syntax、3 个 canonical launchd tests 通过，完整套件为 382 passed、1 个既有 watchlist 环境失败。PR 明确未改变 health-dashboard DB path。
- #161 已合并：Screening ingestion/health scope 收敛为 `1d + 4h`，其余股票周期为 `not_applicable`，并修正 `CN.A.601989` 为 `CN.A.600150`；PR rebase 后完整套件为 384 passed、1 个既有 watchlist 环境失败。live Screening 启动与 90% coverage gate 仍待 owner 启动。
- owner 在 issue #163 报告的线上实测（截至 2026-09-08）：8100、18171、18172 均 HTTP 200；worker 01:06Z 周期 `status=success`、无 crash。health-dashboard 的 `KLINE_DB_PATH` 已由 owner 指向 `/Users/wendy/park-data/market/kline.db`，本仓 canonical plist 已同步这一事实；本票不重启服务。

## 下一步

- #138：采用 pinned Park Exposure Registry 作为 107-member daily K-line Watchlist。
- #43：Market Data Database MVP v1 合同的整体收口。
- #54：真实 100×100、30-day acceptance、restore verification 与最终 Registry 回执。
- #67：中文 asset × timeframe health dashboard 的完整产品合同。
- #71：七天 dashboard reliability gate；仍需自然七天运行和浏览器故障/恢复/过期证据，且被 #70 阻塞。
