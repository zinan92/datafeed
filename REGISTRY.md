# datafeed

## 要去哪里

多资产 K 线数据服务：`ticker + timeframe → 标准化 OHLCV + provenance`，覆盖 A 股、美股、加密和商品；近期喂养 trading-system 与 tokenpulse，远期可作为独立 API 产品外卖。

## 现在在哪里（2026-11-09，白银、原油合约信息从 1.5 秒变成 0.5 秒 · #210）

- 交易后台（GridMind）列纸面品种时要读白银、原油的合约信息，每个要 1.5 秒：要问 OKX 三件事（合约、保证金档位、服务器时间），一件问完才问下一件。后台只等 2 秒，OKX 稍慢一点，白银或原油就会显示成「不可用」30 秒。
- 现在：三件事同时问，实测每个约 0.5 秒。内容一样，任何一件失败照样报错；不加缓存，因为合约信息里带着 OKX 的时间。

## 现在在哪里（2026-11-09，启动从 9 秒变成几十毫秒 · #208）

- 每次启动，端口打开前要等 9 秒：把 135 万行 K 线全表扫一遍，找「旧格式时间戳」。实际一行都找不到——新数据写入时早已统一成标准 UTC 格式。
- 现在：每个数据库只扫一次，扫完记一个标记（`kline_maintenance` 表），之后启动直接跳过。新库或旧库第一次打开时照样会整理。

## 现在在哪里（2026-11-09，重启后黄金读取不再等行情查询器 · #206）

- #204 上线后日志显示：服务本身不再卡，但交易台读黄金（要求「执行交易所」行情）还是等了 18.5 秒，等的是一个它根本不用的查询器。
- 现在：要求执行交易所的读取不再建、也不再等查询器；行情库配置坏了时照样拒绝（503），不放行。

## 现在在哪里（2026-11-09，重启后第一个请求不再卡住整个服务 · #204）

- #202 的新日志在第一次重启时就说出了原因：第一个请求要先建「行情查询器」，它扫一遍行情库，约 18 秒，而且是在主循环上做的，期间所有请求都卡住。#200 里没解释的「重启后第一个请求 28.8 秒」就是它。
- 现在：服务启动后立刻在后台线程里建查询器，日志记下用了多久；建好之前健康检查写「warming」；这期间来的 K 线请求在线程里等，不再卡主循环。建失败时健康检查写「failed」，不会一直 warming。
- 另外量到：启动时注册表初始化 9.3 秒，花在每次启动都全表扫一遍旧时间戳格式（`_normalize_stored_timestamps`）。这次只记录，下一步再处理。

## 现在在哪里（2026-11-09，日志能回答「什么时候、多久、谁卡住了」 · #202）

- 黄金行情中断那次，8100 的日志答不上来：没有时间、没有耗时、也没人说主循环被卡住，原因全靠手动计时找出来。
- 现在：每行日志带 UTC 时间；超过 1 秒的请求记一条警告（路径、状态、耗时）；主循环被卡超过 1 秒记一条警告；启动时记下开始、注册表初始化耗时、启动完成、第一个请求。参数名像密钥的查询值写成 [redacted]。各数据源每次抓取的 INFO 行不进文件。

## 现在在哪里（2026-11-09，健康检查再也不等数据库 · #200）

- #196 上线后实测：完整性检查之外，「存储统计」（K 线总行数、各来源覆盖）每次也要 8–11 秒，而且每 15 秒就要重算一次，所以差不多每隔一次调用就要等将近 10 秒——这才是剩下超时的主因。
- 现在：存储统计和完整性检查一样在后台刷新，调用方永远立刻拿到上一次的结果和它的「年龄」；刷新间隔从 15 秒改成 60 秒。刚启动、还没有第一份结果时写「pending」，不写「ok」；刷新出错写「error」。

## 现在在哪里（2026-11-09，重启后第一次健康检查也不用等 · #198）

- #196 上线后实测：服务刚重启时第一次调用还是要等完整性检查，用了 26.2 秒。
- 现在：第一次调用立刻返回，完整性写「pending」，存储状态也是「pending」，不是「ok」；检查在后台跑完后才变「ok」。要求「ok」的调用方（trading-system 的开盘前检查）在这段时间照样当作没准备好，不会被放行。检查本身出错时写「unavailable」，状态是「error」，不会一直停在 pending。

## 现在在哪里（2026-11-09，完整性检查放到后台刷新 · #196）

- #194 上线后还剩一个尾巴：完整性检查 10 分钟过期一次，过期后第一个调用要等新检查做完，生产上量到 24.8 秒，超过 trading-system 的 10 秒超时。
- 现在：过期后照样立刻返回上一次的结果（带 `integrity_age_seconds`），新检查在后台跑，同一时间只跑一个。只有服务刚启动后的第一次调用要等。

## 现在在哪里（2026-11-09，健康检查不再卡住整个服务 · #194）

- `/api/health` 以前在主线程上同步读库，还每次跑一遍 `PRAGMA integrity_check`。生产库 485 MB，这一步约 12 秒，期间别的请求全部排队：同一个黄金读取平时 0.03 秒，健康检查进行中要 4.8 秒，冷盘时会超时。trading-system 交易台的「黄金行情中断」就是这么来的。
- 现在：读库放到工作线程；并发的健康检查只算一次、共用结果；完整性检查 10 分钟最多跑一次，其余数据 15 秒内复用。响应里多了 `storage_age_seconds` 和 `storage.integrity_age_seconds`，说清楚数据是多久以前的。
- 另一半原因在别的仓库：human-kline-review 一直在请求已到期的 `MGCV26.CMX`（Yahoo），见那边的 Issue。

## 现在在哪里（2026-11-07，OKX 白银和原油 · #188）

- `okx_swap_public`（commodity）在黄金之外放开白银 `XAG-USDT-SWAP`、WTI 原油 `CL-USDT-SWAP`、布伦特 `BZ-USDT-SWAP`。
  - 别名：SILVER/XAG、WTI/CL、BRENT/BZ。加密币仍不走这个源。
- 三个都进 `okx_history.SERIES`，每天 08:40 一起回填，各自一份覆盖报告。
- 这三个合约 2026 年才上线（白银 1 月、原油 3 月），180 天历史够用，再长就超过上市日。
- 手续费仍不从 OKX 读，下游要显示「未核实」。
- 用途：trading-system#1361，交易台用公开数据看白银和原油，不需要账户。

## 现在在哪里（2026-11-05，OKX BTC 历史 · #186）

- 新数据源 `okx_swap_crypto_public`：OKX 公开 USDT 永续，资产类别 crypto，允许 `BTC-USDT-SWAP`、`ETH-USDT-SWAP`。研究用，不是执行行情源。
  - 一个数据源只属于一个资产类别，跟 `binance_usdm_futures` / `binance_usdm_futures_research` 一样；黄金仍是 `okx_swap_public`（commodity），没有改动。
- `okx_history.SERIES` 列出存哪些历史：黄金 → `okx_swap_public`，BTC → `okx_swap_crypto_public`。
  - 每个系列单独写覆盖报告。黄金的文件名不变。
- 每天 08:40 的回填任务补所有系列；`--instrument` 可以只补一个。任何一个系列失败，任务以非 0 退出，其余系列照常补完。
- `/api/history/okx_swap_crypto_public/BTC-USDT-SWAP/report|funding` 可读；源和品种对不上一律 404。
- 用途：trading-system 回测工作台（trading-system#1334）的 BTC 回测。

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
