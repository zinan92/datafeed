# datafeed

## 要去哪里

多资产 K 线数据服务：`ticker + timeframe → 标准化 OHLCV + provenance`，覆盖 A 股、美股、加密和商品；近期喂养 trading-system 与 tokenpulse，远期可作为独立 API 产品外卖。

## 现在在哪里（2026-11-05，OKX 黄金历史回补 · #184）

- `ops/okx_history_backfill.py` 把 `XAU-USDT-SWAP` 180 天 1 分钟线写进 8100 读的库（`okx_swap_public`），只用 OKX，不补不算。
- 按 UTC 天取，一天一提交；已完整的天跳过，所以中断后重跑即可。launchd `com.wendy.datafeed.okx-history` 每天 08:40 补最新。
- 覆盖报告在库文件同目录的 `history_reports/`，经 `/api/history/okx_swap_public/XAU-USDT-SWAP/report` 读；资金费走 `.../funding`。
- 实测：259,200 根全到，0 缺口。资金费 281 条，只回到 2026-08-03（约 94 天），更早接口不给，报告里写明起点，之后每天累积。

_下一步_：trading-system #1308 用这份数据做开仓前回测。

## 现在在哪里（2026-11-05，OKX 黄金进入 execution-market worker）

- Park 2026-11-05 选定 Paper 黄金用 OKX `XAU-USDT-SWAP`。
- execution-market worker 新增 `XAU-USDT-SWAP.OKX`（venue `okx`，provider `okx_swap_public`），API 走 `/api/execution-market/okx/XAU-USDT-SWAP`。
- 分派与解析改按 venue，不再按 `XAU` 前缀（否则 OKX 黄金会被送去 Binance）；OKX 未 `confirm` 的已过分钟 bar 不入库。
- 一个 venue 失败不影响另一个：本机实测 Binance 451、OKX 正常写入（4068.0）。
- 修了 `test_timeframe_contract` 三处用本地日期的测试（上海 00:00–08:00 必挂）。
- 未部署：生产 `~/park-runtime/datafeed` 仍在 `4a2f0cf`，部署要 Park 点头。

## 现在在哪里（2026-11-05，OKX 第二个执行行情源）

- 2026-11-04 起 Binance 对本机返回 HTTP 451（地区限制），`fapi` 和 `api` 都是，黄金唯一的执行行情源断了；trading-system 的 `/trade` 因此一直“读取中”，按设计拒绝新开仓。
- 新增 `okx_swap_public`：OKX 公开 USDT 永续（先开 `XAU-USDT-SWAP`），K 线 + 合约定义，`execution_venue=true`。**只在显式 `source=okx_swap_public` 时使用**，默认仍是 Binance，不做静默切换。
- 未部署：生产 `~/park-runtime/datafeed`（:8100）仍在 `4a2f0cf`。

_下一步_：Park 决定黄金 Paper 用哪个源（OKX `XAU-USDT-SWAP` / Hyperliquid `xyz:GOLD` / 修网络让 Binance 恢复）；选定后部署本仓并在 trading-system 切换 `datafeed.source`。

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
