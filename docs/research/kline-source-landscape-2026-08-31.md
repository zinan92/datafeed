# 全市场 K 线定时采集：数据源、容量与 NAS 部署调研

> 调研日期：2026-08-31<br>
> 状态：**Daily v2 的 19 项清单已本地核实；数据授权、15m 适用范围和 NAS 能力尚未验明，不能直接进入实现**<br>
> 范围：OHLCV/K 线；`15m`、`4h`、`1d`、`1w`，明确不做 `30m`；全 A 股、全美股、Daily 的跨市场资产清单。

> 2026-08-31 scope update：Park 已决定将 `DGS2`、`DGS10`、`T10Y2Y` 从 Candle MVP 排除；美股公司 universe 收缩为固定约 30 只，SPX 与 Nasdaq 保持独立指数 identity。后续 spec 不再以旧 19 项数量为目标，而会生成新的 versioned MVP manifest。

## 1. 先给结论

这不是“给现有 datafeed 加一个 cron”这么小的改动，而是三个系统问题叠在一起：全市场数据授权、数亿行数据的增量/回填、以及网络存储上的数据库可靠性。

1. **A 股主源：TuShare Pro 是这次个人/体验版唯一现实的全市场候选。** 它有包含 SSE/SZSE/BSE、上市/退市状态的股票主表，原生 `15m`、`1d`、`1w`，历史分钟自 2009 年；`4h` 必须从 `15m`/`60m` 派生。历史分钟个人价目前为 ¥2,000/年，实时分钟另为 ¥1,000/月；机构价是个人价的 10 倍。[股票主表](https://tushare.pro/document/1?doc_id=25)、[历史分钟](https://tushare.pro/document/2?doc_id=370)、[权限与现价](https://tushare.pro/document/1?doc_id=290)
2. **美股主源：Massive（原 Polygon.io）在技术上最匹配。** 它有 point-in-time ticker master、退市标记、全市场分钟/日 flat files、全市场分钟 WebSocket `AM.*`、公司行动和分拆前/后口径。[ticker master](https://massive.com/docs/rest/stocks/tickers/all-tickers)、[分钟 flat files](https://massive.com/docs/flat-files/stocks/minute-aggregates)、[全市场分钟流](https://massive.com/docs/websocket/stocks/aggregates-per-minute)
3. **但 Massive 目前有明确的合同阻断。** 个人市场数据条款把用途限定为个人、非商业，并默认 display-only；未经许可禁止再分发、衍生作品和 non-display/投资策略用途，终止后还要求删除数据。持久化到数据库并用于指标、回测或 agent 分析不能仅凭“API 能调”推定为获准，必须拿到书面许可或适用的 Business/Enterprise 合同。[市场数据条款](https://massive.com/terms/market_data_terms.pdf)、[Business 方案](https://massive.com/business)
4. **Alpaca 可做低成本美股 pilot，但不是理想的全量回填主源。** 它原生请求支持 `15Min`、`4Hour`、`1Day`、`1Week`，SIP 覆盖全美交易所，历史自 2016；但没有全市场 flat-file 下载路径，历史 securities master 对退市和 point-in-time 的保证弱于 Massive。[bars API](https://docs.alpaca.markets/us/reference/stockbars)、[套餐与历史范围](https://docs.alpaca.markets/us/docs/about-market-data-api)
5. **Yahoo/yfinance、腾讯和新浪不能继续承担生产主源。** yfinance 自己声明未获 Yahoo 背书、数据限个人使用；Yahoo 条款禁止未经许可的自动抓取。腾讯/新浪端点没有一手 API 合同、SLA、限流、历史范围和归档许可。[yfinance README](https://github.com/ranaroussi/yfinance/blob/main/README.md)、[Yahoo Terms](https://legal.yahoo.com/us/en/yahoo/terms/otos/index.html)
6. **Futu、Tiger、Longbridge/Longport 都不能做全市场主源。** 三者的历史 K 线按“唯一证券数”计额度，上限分别约 2,000、2,000、3,000，均低于约 5,534 只 A 股，也低于完整美股 universe；适合作为当前 19 个资产或抽样 QA 源，不适合作为全量源。[Futu quota](https://openapi.futunn.com/futu-api-doc/en/intro/authority.html)、[Tiger quota](https://quant.itigerup.com/openapi/zh/python/permission/historySubscribe.html)、[Longbridge quota](https://open.longbridge.com/docs/quote/pull/history-candlestick)
7. **不能把当前 SQLite 文件直接放到 SMB/NFS NAS share 上继续由 Mac 打开。** 当前 `KlineStore` 强制启用 WAL；SQLite 官方明确说 WAL 不支持网络文件系统，而且网络文件锁/同步错误可能造成损坏。[当前 store](../../src/kline/store.py)、[SQLite WAL](https://www.sqlite.org/wal.html)、[SQLite over network](https://www.sqlite.org/useovernet.html)
8. **MVP 的当前存储主选已经收敛：外接 APFS SSD 做 active SQLite，NAS 做一致性备份和冷层。** 2026-08-31 现场检查确认 `/Volumes/Phone SSD` 是 1 TB APFS SSD、约 895 GiB 可用；这足以承载 bounded MVP。未来需要多客户端/更大 universe 时，再在 NAS 本机运行 PostgreSQL 并由 datafeed 通过 TCP 访问。任何阶段都不允许 Mac 通过 SMB 直接打开 NAS 上的 `.db`。
9. **Daily v2 的 19 项已经核实，不再是未知项。** 它比 datafeed 的旧 17 项多 `ethereum` 和 `hype`；当前 Daily 只给 BTC/ETH/HYPE/WTI/Gold/Silver 请求 `4h/30m`，其余 13 项只请求 daily。新需求是把 `30m` 改成 `15m`，但“15m 只给这 6 项，还是扩到全部 19 项”仍需 Park 明确。[Daily v2 registry](</Users/wendy/Library/Application Support/ParkKlineDaily/app/product/data_core/market_regime_weekly_source.py>)、[Daily timeframe matrix](</Users/wendy/Library/Application Support/ParkKlineDaily/app/product/data_core/market_regime_daily_source.py>)
10. **本地空间的第一大现存问题不是 candle rows，而是无界 raw/runtime artifacts。** 只读快照中 live datafeed DB 为 157.7 MiB、38,989 根 candle，但 `raw_upstream_responses` 已占 147.9 MiB（主 DB 的 93.8%）；ParkMarketRegime runtime 已约 12 GB，未找到针对这些 K-line/runtime artifacts 的 prune/retention job。全市场化前必须先立 retention 合同。

### 推荐的目标架构

| 轨道 | 推荐主源 | 增量 | 回填/校准 | 数据库口径 |
|---|---|---|---|---|
| 全 A 股 | TuShare Pro（先过许可确认） | 历史分钟定时范围拉取；若必须盘中完整则购实时分钟并做日累计/范围补洞 | `stk_mins` 分证券分段；未来可升级到交易所许可源 | 原始未复权 `15m` + 原始 `1d`；`4h`、`1w` 派生；`adj_factor` 独立保存 |
| 全美股 | Massive（先过 non-display/存储许可） | `AM.*` 全市场分钟流，在内存聚合成 `15m`；或按 watermark 拉 custom bars | 全市场 minute/day flat files；每日 grouped aggregate 校准 | 未复权 `15m` + 未复权 `1d`；公司行动独立；`4h`、`1w` 派生 |
| Daily v2 crypto perps | Hyperliquid `BTC/ETH/HYPE`（保留现有 perp identity） | 原生 candle WebSocket 或每 4 小时 watermark 补取 | `candleSnapshot` 只保留最近 5,000 根；官方 S3 不提供 candles，必须从现在开始自行留存 | 原生 `15m/4h/1d/1w`；不能用 Binance spot 静默回填成同一 series |
| BTC/ETH spot（仅在 Park 明确改 identity 时） | Binance Spot | 每 4 小时按 watermark 拉闭合 K 线 | 官方 daily/monthly ZIP + checksum，自 2020 起批量 | Binance 原生 `15m/4h/1d/1w`，按单一 venue 标识 |
| Daily v2 其余 16 项 | 已核实的 v2 registry，逐项更换生产 source | 依市场日历 | 来源特定 | 保留当前 ETF/rate/index/futures 语义；禁止 Yahoo/腾讯/新浪静默 fallback |

**不建议马上买任何数据套餐。** 先让 Park 定义 universe、session、19 项的 `15m` 适用范围和用途（仅个人查看，还是回测/agent/未来产品），再把用途原文发给 TuShare/Massive 索取书面确认。

## 2. 现有 datafeed 的真实基线

- 当前 repo 已经有 source-aware 的 candle key、质量/来源收据，以及 `raw_timeframe` / `timeframe_origin` 语义；这些应该保留，而不是另起一套数据格式。[README](../../README.md)、[Kline schema](../../src/kline/models.py)
- 当前持久层是 SQLite + WAL，唯一键是 `source_id + ticker + asset_class + timeframe + timestamp`，还有一条 ticker/timeframe 索引。[store](../../src/kline/store.py)、[models](../../src/kline/models.py)
- 当前 A 股 adapter 只有 TuShare 日线/周线路径；周线实际上从日线聚合。腾讯/新浪仅绑定三个上海指数的日线和派生周线。[A-share adapter](../../src/kline/providers/ashare.py)、[Sina adapter](../../src/kline/providers/sina.py)
- 当前美股/指数/商品主要依赖 yfinance；`4h` 从 `1h` 派生，`1w` 从 `1d` 派生。yfinance 文档只承诺最近 60 日的 intraday，因此不能承担长期 `15m` 回填。[US adapter](../../src/kline/providers/us.py)、[yfinance download docs](https://ranaroussi.github.io/yfinance/reference/api/yfinance.download.html)
- 当前 Tiger adapter 只接了一个 COMEX futures 合约路径，只支持 `1m/5m/1d`，不是 A 股或美股全市场 adapter。[Tiger adapter](../../src/kline/providers/tiger.py)
- datafeed 当前 committed Phase 1 matrix 是较旧的 **17 个资产 / 39 个 cell**；它缺少 ETH 和 HYPE。[datafeed 17 项清单](../../ops/phase1_matrix.py)
- Daily v2 的实际 `WEEKLY_KEYS` 已明确是 **19 项**：`dxy, us2y, us10y, us2s10s, sp500, nasdaq, us_dividend, vix, bitcoin, ethereum, hype, shanghai, star50, china_dividend, nikkei, kospi, wti, gold, silver`。对应 registry 还明确：DXY/SP500/Nasdaq 使用 UUP/SPY/QQQ ETF，BTC/ETH/HYPE 是 Hyperliquid USDC 永续，WTI/Gold/Silver 是 continuous futures。[Daily v2 source registry](</Users/wendy/Library/Application Support/ParkKlineDaily/app/product/data_core/market_regime_weekly_source.py>)、[Daily v2 candle contract](</Users/wendy/Library/Application Support/ParkKlineDaily/app/product/data_core/market_regime_weekly_contract.py>)
- Daily v2 当前 timeframe contract 是 daily 全 19 项；只有 BTC/ETH/HYPE/WTI/Gold/Silver 额外请求 `4h` 和 `30m`。这次需求应被写成一次显式 migration：**删除 `30m`，新增 `15m`**，而不是继续兼容 30m。[Daily timeframe matrix](</Users/wendy/Library/Application Support/ParkKlineDaily/app/product/data_core/market_regime_daily_source.py>)

因此，`17 vs 19` 不是待 Park 补清单的问题，而是 **Daily v2 manifest 已前进、datafeed Phase 1 matrix 滞后**。后续 spec 应以 Daily v2 的 19 个 stable keys 为准，并单独决定 `15m` 是否仍只适用于 6 个 intraday assets。

### 2.1 Live local storage audit（2026-08-31 11:54 CST）

只读检查 launchd 和 SQLite 后的实际状态：

| 项目 | 观察值 | 含义 |
|---|---:|---|
| Live DB path | `/Users/wendy/datafeed/data/kline.db` | launchd `KLINE_DB_PATH` 指向这里，不是 repo 内的空样例 DB。[launchd plist](</Users/wendy/Library/LaunchAgents/com.wendy.datafeed.plist>) |
| Main DB | 165,330,944 bytes = **157.7 MiB** | 同时有约 6.7 MiB WAL；这是活跃文件，大小会变动。 |
| `klines` | **38,989 rows** | `klines` table + 两条索引合计 9,981,952 bytes，实测约 **256 bytes/candle row**。这支持后文的 200–350 B/row 规划区间。 |
| `raw_upstream_responses` | 548 rows / **147.9 MiB** | 平均约 276 KiB/response，占主 DB 93.8%；当前数据库膨胀主要来自完整 raw body，而不是 normalized candles。 |
| `source_observations` | 735 rows | 体量很小，receipt 不是当前空间问题。 |
| ParkMarketRegime runtime | **约 12 GB / 70,825 files** | 其中 intraday 目录约 64,183 files；在 app/scheduler 中未找到对这些 K-line/runtime artifacts 的 prune/retention 实现。 |
| NAS mount | `/Volumes/personal_folder` **当前未挂载** | 本轮无法验证容量、文件系统、网络性能、Docker/PostgreSQL 或 UPS；NAS 部署只能保持 `unverified`。 |
| External SSD | `/Volumes/Phone SSD`：1 TB APFS，约 **895 GiB available** | 当前适合作为 MVP active database volume；仍需 mount guard、断连失败语义、备份和受控切换。 |

审计使用 `sqlite3 -readonly ... count(*)` 和 `dbstat`，未写数据库。这个样本说明：在讨论把 DB 搬到 NAS 之前，必须先把 raw response 和 Daily runtime 的 retention 定义清楚；否则搬迁只会把无界增长换一个磁盘继续发生。

## 3. Universe 和时间级别必须先定义

### 3.1 “全 A 股”

中国上市公司协会 2026-07 月报给出境内上市公司 5,541 家，其中约 5,534 家有 A 股；这是本报告容量估算采用的基数。[官方月报](https://sp.capco.org.cn:82/file/202608/202607yuebao/202607yuebao.pdf)

实现口径应包括：

- SSE、SZSE、BSE；
- 当前上市、暂停、待上市、退市状态；
- 历史退市证券，避免幸存者偏差；
- 代码变更和上市/退市日期；
- 不含 B 股、ETF、基金、可转债，除非 Park 明确扩大范围。

TuShare `stock_basic` 有 `SSE/SZSE/BSE`、`L/D/P/G/UN`、`list_date/delist_date`，适合作为体验版 universe master。[TuShare stock_basic](https://tushare.pro/document/1?doc_id=25)

### 3.2 “全美股”不是一个自然唯一集合

至少要在以下两种口径中选一个：

- **公司股票口径（本报告基准）**：约 6,500 个 active common shares/ADRs，排除 ETF、基金、权证、优先股和 OTC；
- **全可交易 ticker 口径（容量上界）**：约 10,000+ active tickers，可能包含 ETF、OTC 和其他 security types。Massive 的 full-market snapshot 公开写的是 10,000+ actively traded tickers。[Massive full-market snapshot](https://massive.com/docs/rest/stocks)

Massive ticker master 可按 `type`、`market`、`exchange`、`active`、`date` 过滤，`active=false` 表示已退市，并提供 `delisted_utc`、CIK、FIGI 等字段。[All Tickers](https://massive.com/docs/rest/stocks/tickers/all-tickers)

### 3.3 `4h` 不是只写一个字符串

- A 股常规交易分为 09:30–11:30、13:00–15:00 两段，合计约 4 小时；16 根 `15m` 合成一个 session bar 后，OHLCV 往往与日线高度重合。[SSE trading hours](https://english.sse.com.cn/start/trading/schedule/)、[SZSE trading hours](https://www.szse.cn/English/services/trading/tradOverview/)
- 美股 core session 是 09:30–16:00 ET，共 6.5 小时；若用 session-relative `4h`，每天会出现一个完整 4h 和一个 2.5h closing stub。若丢掉“不完整 4h”，会永久丢失每日下午数据。[NYSE trading hours](https://www.nyse.com/trade/hours-calendars)
- 若按 fixed wall-clock 4h bucket，则必须确定 anchor、时区，以及是否包含美股 04:00–20:00 extended hours。

所以必须选择并版本化一种规则：

1. `regular_session_relative`：保留 closing stub，并显式标 `is_partial_session_bucket=true`；或
2. `extended_hours_fixed_4h`：使用 America/New_York 固定锚点；或
3. 对 A 股取消物理存储 `4h`，仅按兼容接口返回当日 session aggregate。

当前 datafeed 的“固定 4h + 丢弃不完整 bucket”规则不能原样推广到全美股。[现有 timeframe contract](../../README.md)

### 3.4 `1w` 应由已闭合 `1d` 派生

即使供应商返回 vendor-weekly，也建议把 `1d` 作为统一基础，按交易所时区和交易日历生成 completed week。这样 A 股、美股和 24/7 BTC 的周界线不会被供应商默认设置暗中改变。供应商原生周线只用作 reconciliation，不作为第二套事实源。

## 4. A 股数据源审查

### 4.1 推荐：TuShare Pro（体验版主源，许可待确认）

| 维度 | 一手证据与判断 |
|---|---|
| 时间级别 | `stk_mins` 原生返回 `1/5/15/30/60m`；无 `4h`，所以 `4h` 派生。`daily` 和 `weekly` 都有原生接口，但生产架构仍建议 `1d→1w`。[历史分钟](https://tushare.pro/document/2?doc_id=370)、[日线](https://tushare.pro/document/1?doc_id=27)、[周线](https://tushare.pro/document/2?doc_id=144) |
| 历史 | 历史分钟文档说超过 10 年，权限表标明自 2009；每次最多 8,000 行。[历史分钟](https://tushare.pro/document/2?doc_id=370)、[权限表](https://tushare.pro/document/1?doc_id=290) |
| Universe/退市 | `stock_basic` 一次最多 6,000 行，覆盖全市场 A 股，含 SSE/SZSE/BSE、上市/退市/暂停/待上市和上市/退市日期。[stock_basic](https://tushare.pro/document/1?doc_id=25) |
| 复权 | 日线接口是未复权；`adj_factor` 可按股票或交易日拉全历史，`pro_bar` 支持 qfq/hfq。qfq 以查询 `end_date` 为锚且使用“分红再投”口径，因此调整后历史会随锚点变化。[日线](https://tushare.pro/document/1?doc_id=27)、[复权因子](https://tushare.pro/document/2?doc_id=28)、[复权说明](https://tushare.pro/document/2?doc_id=146) |
| 频控/价格 | 历史分钟 ¥2,000/年、500 次/分钟、8,000 行/次；实时分钟 ¥1,000/月、500 次/分钟、一次最多 300 公司。普通 5,000 积分权限 ¥500/年、500 次/分钟；机构价为个人 10 倍。[权限与价格](https://tushare.pro/document/1?doc_id=290) |
| 批量/回填 | 日线可按 `trade_date` 一次拉全市场；分钟是按 symbol/time range，5,534 个 symbol 的一轮理论最低约 11 分钟，仅是频控下限，不含分页、网络、重试和服务端延迟。完整十多年回填会是数万请求，必须可断点续跑。 |
| 盘中更新 | 历史分钟是 EOD 产品；若要求盘中有完整 `15m`，要用单独的 `rt_min` / `rt_min_daily` 权限。仅每 4 小时取“最新一根”会漏掉期间的 15m bars。[实时分钟](https://tushare.pro/document/2?doc_id=374)、[当日分钟累计](https://tushare.pro/document/2?doc_id=457) |
| 许可 | 服务协议只授予个人、不可转让、非商业、可撤销、有期限的许可，并写明仅作个人查看使用。文档虽然提供本地 MySQL + cron 示例，也不能自动推导出策略回测、agent non-display 使用或未来对外产品已获许可。[服务协议](https://tushare.pro/document/1?doc_id=405)、[本地 MySQL 示例](https://tushare.pro/document/1?doc_id=231) |

**结论：** 技术上采用；合同上先把“个人 NAS 长期保存、生成指标、个人回测/agent 分析、不对外提供原始数据”原样提交 TuShare，拿书面确认。

### 4.2 官方交易所源（权威升级路线，不适合个人 MVP）

| 来源 | 能力 | 历史/批量 | 许可与成本 | 判断 |
|---|---|---|---|---|
| 上证所信息 | 原生日 K、分钟 K、证券基本信息；`15m/4h/1w` 需自行派生。[产品说明](https://www.sseinfo.com/services/assortment/historical/) | 按交易日落 `Day.csv` / `Minute.csv`；每日增量约 18:00 经 rsync。[接口结构](https://www.sseinfo.com/services/assortment/market/hqywwd/wdzsjk/c/10800481/files/43b75567ae224148bd576acf11d30bd3.pdf)、[每日数据](https://www.sseinfo.com/services/assortment/znsj/znfwnr/c/10767767/files/1e1427dfec484ab288fd67201b73e561.pdf) | 当前价表：每日数据 ¥8,000/月/用户；历史 L1 ¥30,000/年；单购日 K ¥10,000/年、分钟 K ¥20,000/年；行情使用需许可。[价格](https://www.sseinfo.com/services/cpfwjg/)、[授权声明](https://www.sseinfo.com/aboutus/authstatement/) | 技术上是沪市权威 bulk source，但只覆盖沪市且合同/成本高。 |
| 深交所/深证信 | 历史增强行情是全深市 3 秒快照、逐笔、委托队列、证券信息/状态，不是现成 K 线；所有档次需聚合。[产品](https://www.szsi.cn/cpfw/fwsq/hq/yw-2.htm) | 历史自 2008-01-01，盘后自动落客户本地服务器；L1 网络接入提供 TCP、C++/Java API、DBF。[L1 接入](https://www.szsi.cn/cpfw/fwsq/hq/hlhqfw.htm) | 非展示自用申请面向指定机构/机房，不应假设家庭 NAS 符合。[申请条件](https://www.szsi.cn/cpfw/fwsq/hq/sqlc-3.htm) | 权威但属于机构级 raw-feed 工程。 |
| 北交所 | 公开股票列表和行情页面不等于历史 K 线 API；任何机构或个人使用、发布、传播行情都需中证股转科技许可。[授权指南](https://www.bse.cn/application/guide.html) | 未找到公开历史 K 线 API 产品。 | 独立许可流程。 | “全 A 股”不能只买沪深两源；BSE 是单独合同。 |

### 4.3 Broker APIs（抽样/19 项备用，不是全量主源）

| 来源 | Vendor-returned 档次/历史 | Universe/调整 | 配额与结论 |
|---|---|---|---|
| Futu | vendor 返回 `15m/240m/1d/1w`；≤60m 近 8 年、日线近 20 年、日以上不限制；240m 的回溯范围未明确。[period enum](https://openapi.futunn.com/futu-api-doc/en/quote/quote.html)、[history K](https://openapi.futunn.com/futu-api-doc/en/quote/request-history-kline.html) | raw/qfq/hfq 和复权因子；静态信息有退市标记，但市场枚举没有 BSE。[rehab](https://openapi.futunn.com/futu-api-doc/en/quote/get-rehab.html)、[static info](https://openapi.futunn.com/futu-api-doc/en/quote/get-static-info.html) | 每 7 日 100/300/1,000/2,000 个历史证券，远低于 5,534；不能做全 A 股。[quota](https://openapi.futunn.com/futu-api-doc/en/intro/authority.html) |
| Tiger | Python/底层文档对 A 股 240m 支持有冲突；日/周和 intraday 历史范围也因市场而异。[stock bars](https://quant.itigerup.com/openapi/zh/python/operation/quotation/stock.html) | 默认前复权或 raw；当前 symbol/status 不能证明完整 point-in-time 退市 universe。 | 历史唯一证券额度低于全市场，K 线约 60 次/分钟；文档冲突本身就是生产风险。[quota](https://quant.itigerup.com/openapi/zh/python/permission/historySubscribe.html)、[rate limit](https://quant.itigerup.com/openapi/en/python/permission/requestLimit.html) |
| Longbridge/Longport | `15m/240m/1d/1w`；A 股日线自 1999-11，分钟自 2022-08。[periods](https://open.longbridge.com/docs/quote/objects)、[history](https://open.longbridge.com/docs/quote/pull/history-candlestick) | raw/qfq；CN board 没有 BSE；current security list 不是历史 universe。[security list](https://open.longbridge.com/docs/cli/market-data/security-list) | 每自然月 100–3,000 个唯一证券，60 次/30 秒；仍不足全市场。[history quota](https://open.longbridge.com/docs/quote/pull/history-candlestick) |

### 4.4 腾讯/新浪

当前 repo 的 Tencent/Sina 路径只服务 3 个明确上海指数，且只承诺 `1d` 和派生 `1w`。截至调研日，没有找到发布方的一手 API 文档、SLA、限流、历史范围、复权定义、point-in-time universe 或长期归档许可。新浪协议还明确限制未经书面许可的机器人/蜘蛛复制下载。[当前 Tencent adapter](../../src/kline/providers/ashare.py)、[当前 Sina adapter](../../src/kline/providers/sina.py)、[新浪财经协议](https://finance.sina.com.cn/roll/2021-05-12/doc-ikmxzfmm2033220.shtml)

**结论：** 不进入生产 source registry；最多保留为人工 spot-check，且不能把“网页能返回”写成“可靠且获授权”。

## 5. 美股数据源审查

### 5.1 推荐：Massive（技术主选，许可阻断）

| 维度 | 一手证据与判断 |
|---|---|
| 时间级别 | custom bars 用 `multiplier × timespan`，可请求 15-minute、4-hour、day、week；这些是 Massive 从 minute/day base aggregates 生成的 vendor aggregation，不是交易所原生的独立 4h/周线。[Custom Bars](https://massive.com/docs/rest/stocks/aggregates/custom-bars) |
| Coverage | 股票概览写明覆盖 19 个主要交易所、dark pools、FINRA facilities、OTC；full-market snapshot 为 10,000+ active tickers。[stocks overview](https://massive.com/docs/rest/stocks) |
| Universe/退市 | ticker master 每日更新，可按日期做 point-in-time 查询，`active=false`/`delisted_utc` 表示退市，历史记录可追溯至 2003-09-10。[All Tickers](https://massive.com/docs/rest/stocks/tickers/all-tickers) |
| 回填 | 全市场分钟和日 K 以压缩 CSV flat files 提供，次日约 11:00 ET 完成；分钟历史自 2003-09-10。2025 年全市场 1m ZIP 总量约 5.4 GB，可直接估算批量回填网络量。[Minute Aggregates](https://massive.com/docs/flat-files/stocks/minute-aggregates)、[Flat Files Quickstart](https://massive.com/docs/flat-files) |
| 盘中 | WebSocket `AM.*` 可订阅全部股票分钟 OHLCV；官方 FAQ 说除期权 quotes 外，不限制单连接 ticker 数，前提是消费端跟得上。[minute WebSocket](https://massive.com/docs/websocket/stocks/aggregates-per-minute)、[WebSocket FAQ](https://massive.com/knowledge-base/categories/faq) |
| 复权/公司行动 | REST 默认 split-adjusted，可用 `adjusted=false` 取 raw；不做 dividend adjustment。splits、dividends 和 ticker events 有独立端点。建议 canonical 一律 raw，再在本地版本化复权。[adjustment semantics](https://massive.com/knowledge-base/article/is-massives-stock-data-adjusted-for-splits-or-dividends)、[corporate actions](https://massive.com/docs/rest/stocks) |
| API/价格 | Individual：Free 2 年/5 calls-min；$29/月 5 年、$79/月 10 年、$199/月 20+ 年，付费档写明 unlimited API calls 和 flat files。[Individual pricing](https://massive.com/pricing?product=stocks) |
| 许可 | Individual 条款是个人、非商业、display-only；未经许可不得 non-display、构建策略/衍生作品或再分发，终止后要求删除。Business Stocks 当前 $2,499/月，但实际 non-display/存储范围仍要在合同里确认。[market-data terms](https://massive.com/terms/market_data_terms.pdf)、[business pricing](https://massive.com/business) |

**技术结论：** 它是唯一一个同时解决全市场 intraday、bulk backfill、退市 universe 和公司行动的自助候选。

**合同结论：** 在书面许可前，状态必须保持 `blocked_for_license`，不能因为 $29/$79 套餐页面写了 flat files 就开始建立永久策略数据库。

### 5.2 Alpaca（pilot / fallback）

| 维度 | 一手证据与判断 |
|---|---|
| 时间级别 | bars API 接受 `[1-59]Min`、`[1-23]Hour`、`1Day`、`1Week`；官方 FAQ 明确分钟/日是 base，小时由分钟、周由日聚合。[bars](https://docs.alpaca.markets/us/reference/stockbars)、[aggregation FAQ](https://docs.alpaca.markets/us/docs/market-data-faq) |
| Coverage/历史 | SIP 是全美交易所/100% reported volume；IEX 只是单一交易所。Basic 与 Algo Trader Plus 均标注历史自 2016。[plans](https://docs.alpaca.markets/us/docs/about-market-data-api) |
| Universe | `/v2/assets` 是当前可交易/数据消费 master，可包含 inactive；官方还建议每天更新，但没有承诺完整 point-in-time delisted universe。[assets](https://docs.alpaca.markets/us/reference/get-v2-assets-1)、[assets FAQ](https://docs.alpaca.markets/us/v1.1/docs/broker-api-faq) |
| 调整 | 默认 raw；可选 split、dividend、spin-off 或组合；`asof` 处理 FB→META 等 symbol mapping。[bars](https://docs.alpaca.markets/us/reference/stockbars) |
| 批量/频控 | 多 symbol endpoint 每页总计最多 10,000 bars；Basic 200 req/min，$99/月 Algo Trader Plus 10,000 req/min。没有公开的全市场 flat-file archive。[plans](https://docs.alpaca.markets/us/docs/about-market-data-api) |
| 权利 | 客户协议禁止未经书面同意复制、分发、销售或商业利用 market data；若不是单纯个人查看，也要先问清持久化/non-display 权限。[current agreement](https://files.alpaca.markets/disclosures/library/AcctAppMarginAndCustAgmt.pdf) |

**结论：** 可以做 50–200 个 symbol 的技术 pilot 或 Massive license 等待期验证；全 universe 多年分钟回填和退市 survivorship 控制不如 Massive。

### 5.3 Nasdaq Data Link / Nasdaq Cloud Data Service（不做主源）

- 传统 Data Link/Sharadar Equity Prices 是 premium daily dataset，适合 EOD、公司行动和 active/delisted reference；它不是 `15m` 全市场主源。Sharadar 公开资料写有 6,000+ active、10,000+ delisted companies，并提供 batch export。[Sharadar](https://data.nasdaq.com/databases/SF1)
- Nasdaq Cloud Data Service 的 Bars API 有 `15minute/1day/1week`，没有 `4h`；`15minute` 的公开 range matrix 只支持很短窗口，访问需 sales onboarding/OAuth。[Bars spec](https://github.com/Nasdaq/NasdaqCloudDataService-REST-API/blob/main/restapi/bars-all.md)、[onboarding](https://docs.data.nasdaq.com/docs/api-for-real-time-or-delayed-data)
- Data Link 文档站在 2026-08-31 退役，正好是本调研日期，形成额外迁移风险。[Data Link notice](https://docs.data.nasdaq.com/docs/getting-started)

**结论：** 可作为未来 daily/reference 二源，不值得为这次四档 K 线单独接入。

### 5.4 Databento（机构级备选，不是更简单的 Massive 替代）

Databento 提供 unadjusted `1m/1h/1d` OHLCV 和 point-in-time symbology；`15m/4h/1w` 都需本地派生。它有批量下载和独立公司行动/adjustment factors，数据治理质量高。[OHLCV schemas](https://databento.com/docs/schemas-and-data-formats/ohlcv)、[symbology](https://databento.com/docs/standards-and-conventions/symbology)、[adjustment factors](https://databento.com/docs/examples/adjustment-factors/applying-adjustment-factors)

但是其 100%-volume `EQUS.SUMMARY` 是 daily；intraday 完整 consolidated universe 需要组合多个 feed，而不是一个简单 dataset。[equities coverage](https://databento.com/equities) 因此它适合将来对 provenance 有机构级要求时评估，不是本次个人体验版首选。

### 5.5 Yahoo/yfinance（现有兼容源，退出生产）

- yfinance 支持 `15m/1h/1d/1wk`，无 `4h`，所以当前 repo 用 `1h→4h`；官方库文档说 intraday 不可超过最近 60 日。[download docs](https://ranaroussi.github.io/yfinance/reference/api/yfinance.download.html)
- 没有正式的 full active + delisted ticker master、稳定限流、SLA 或 bulk archive。
- yfinance README 明确写它未获 Yahoo 背书，只用于 research/education，并提醒 Yahoo Finance API 仅供 personal use；Yahoo Terms 禁止未经许可的自动抓取，并限制构建替代数据库/feed。[yfinance README](https://github.com/ranaroussi/yfinance/blob/main/README.md)、[Yahoo Terms](https://legal.yahoo.com/us/en/yahoo/terms/otos/index.html)

**结论：** 保留为短期兼容/人工核对，不再给任何“全量、定时、生产”任务当主源。

## 6. 跨市场 19 项：清单已核实，剩余的是语义冲突

Daily v2 的 19 项已经由 `WEEKLY_KEYS` 和 registry 核实；当前 `CONTEXT_4H_KEYS` 是 BTC、ETH、HYPE、WTI、Gold、Silver。真正未决的是新 `15m` 要沿用这 6 项的 intraday 边界，还是扩到全部 19 项。[Daily v2 source registry](</Users/wendy/Library/Application Support/ParkKlineDaily/app/product/data_core/market_regime_weekly_source.py>)

有几类不能直接满足“四个时间级别、OHLCV”这一统一合同：

1. `DGS2`、`DGS10`、`T10Y2Y` 是每日 yield/level observation，不是可交易品种，也没有 `15m/4h` OHLCV。Park 已决定把它们从 Candle MVP 排除；若未来仍需要，应进入独立 macro-level 数据轨，而不是转换成 synthetic candles。
2. S&P/Nasdaq/VIX/DXY/Nikkei/KOSPI 等指数未必有 volume；volume 应允许 `NULL/not_applicable`，不能用 `0` 冒充真实零成交量。当前 `Candle.volume` 必填，需要合同级 schema 决策。[当前 Candle](../../src/kline/models.py)
3. `GC=F/CL=F/SI=F` 是 Yahoo continuous-futures symbols。切到正式 futures source 后，供应商通常返回具体合约，如 `GCJ5`；必须定义主力/近月选择、roll date、价格拼接和 volume/open-interest 规则。Massive 的官方 futures API 是具体合约并提供 point-in-time contract master，不会替我们决定连续合约算法。[Futures aggregates](https://massive.com/docs/rest/futures/aggregates)、[Contracts](https://massive.com/docs/rest/futures/contracts)
4. Massive indices 支持 10,000+ 指数、分钟/日 aggregates 和 `15m/4h/day/week` custom bars，但 index aggregate 历史只到 2023-02-14；不能假设它自动覆盖并许可当前全部跨国 19 项。[indices API](https://massive.com/docs/rest/indices)
5. Daily v2 的 BTC/ETH/HYPE identity 是 **Hyperliquid USDC perpetuals**，不是 Binance spot。Hyperliquid `candleSnapshot` 和 candle WebSocket 原生支持 `15m/4h/1d/1w`，但 REST 只保留最近 5,000 根 candle：`15m` 约 52 天、`4h` 约 833 天。官方历史 S3 明确不提供 candles，因此若保留 perp identity，必须从现在起持续采集并自行归档；不能等几年后再完整回填。[Hyperliquid candle API](https://hyperliquid.gitbook.io/Hyperliquid-docs/for-developers/api/info-endpoint)、[candle WebSocket](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/websocket/subscriptions)、[historical data limits](https://hyperliquid.gitbook.io/hyperliquid-docs/historical-data)
6. Hyperliquid REST 按 IP 总计 1,200 weight/min，普通 `info` 请求 weight 20，`candleSnapshot` 还按每 60 items 增加 weight；三个 symbol 每 4 小时增量远低于该上限。公开 API 文档没有给出长期归档/再分发许可，外卖或团队共享仍需书面确认。[rate limits](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/rate-limits-and-user-limits)
7. Binance Spot 可作为 **不同 instrument identity** 的 BTC/ETH 交叉验证或在 Park 明确改为 spot 后的主源；`/api/v3/klines` 原生支持 `15m/4h/1d/1w`，每次最多 1,000 bars、weight 2，`exchangeInfo` 是当前 symbol/status master。[Binance klines](https://developers.binance.com/en/docs/catalog/core-trading-spot-trading/api/rest-api/market)、[Spot REST limits](https://developers.binance.com/en/docs/products/spot/rest-api)
8. Binance 官方还发布 daily/monthly K-line ZIP 和 checksum，helper 可批量下载全部 symbol/interval，自 2020-01-01 起默认回填；repo 的 MIT license 明确覆盖代码，但没有找到对原始 market data 商业再分发的清晰授权。[Binance public data](https://github.com/binance/binance-public-data)、[download helper](https://github.com/binance/binance-public-data/blob/master/python/README.md)

### 19 项 manifest 必须包含

| 字段 | 为什么不可省 |
|---|---|
| `instrument_id` | 稳定内部身份，不把 Yahoo/Binance/vendor symbol 当领域身份 |
| `display_name` / `asset_class` | 区分指数值、ETF、spot、具体期货合约、宏观 level |
| `provider_symbol` + `source_id` | 一项一源；禁止无声替换 |
| `market_timezone` + `calendar_id` | 生成 closed `4h/1d/1w` |
| `session_policy` | regular / extended / 24x7；4h anchor 和 closing stub |
| `volume_semantics` | traded volume / quote-derived / not_applicable |
| `adjustment_policy` | raw、split、total-return、futures roll |
| `required_timeframes` | 宏观序列可明确只做 `1d/1w`，而不是伪造 intraday |

## 7. 推荐的采集与派生合同

### 7.1 不要按四个 timeframe 重复抓四份事实

对股票推荐只持久化两个基础层：

- `15m_raw_unadjusted`；
- `1d_raw_unadjusted`。

然后：

- `15m → 4h`，使用版本化 session rule；
- `1d → 1w`，只发布 completed week；
- qfq/hfq/total-return 是 `raw + corporate_actions/adj_factor + as_of` 的可重算视图；
- 如查询性能需要，可物化 `4h/1w`，但必须记录 `derived_from_timeframe`、`aggregation_rule_version`、`source_id` 和 input range。

这既符合“只要 K 线、指标以后计算”的要求，也避免供应商四个档次互相不一致。

### 7.2 每 4 小时 heartbeat 的正确语义

每次运行不是“取最新一根”，而是：

1. 从 `(source, instrument, base_timeframe)` watermark 读取上次成功闭合 bar；
2. 从 watermark 前重叠 1–2 根开始取，到“当前已闭合 bar”为止；
3. 幂等 upsert，记录 source receipt、延迟、缺口、重复/乱序、entitlement/rate-limit 错误；
4. 只在基础层完整后物化 `4h/1w`；
5. 服务重启自动 catch-up，不依赖一次 cron 必须成功。

按市场拆节奏：

- BTC/24x7：固定每 4 小时拉最近区间，保留 1–2 根 overlap；
- A 股：盘中是否需要由 Park 决定；至少午间、收盘后和供应商 EOD 完成后 reconcile；
- 美股：若获 Massive streaming 权限，实时聚合、每 4 小时 checkpoint；次日 flat file 再做 authoritative reconciliation；
- daily/weekly：市场收盘并经过供应商 finalized window 后生成，不要在开盘中间发布“完整日线/周线”。

### 7.3 质量和安全边界

- source 失败保持 gap/blocked，禁止生成 synthetic bars；
- 不把 Yahoo/腾讯/新浪当自动 fallback；
- 不把 adjusted 与 unadjusted 写入同一个 series key；
- symbol rename/delist 不覆盖旧 identity；
- partial/live bars 与 closed bars 分开，数据库默认只服务 closed；
- source 修订时保留 revision/observed_at，避免悄悄改历史；
- API secret、broker credential 仍只在人控配置中，不进入数据库 receipt、日志或 NAS snapshot 的明文。

## 8. 行数与存储量级

以下是容量规划，不是对实际供应商数据完整性的断言。

### 8.1 假设

| 项目 | 基准假设 |
|---|---:|
| Active A 股 | 5,500（官方 2026-07 约 5,534，取整） |
| Active 美股公司股票 | 6,500（common shares/ADRs，排除 ETF/OTC）；上界另算 10,000 tickers |
| A 股交易日 / 15m bars | 244 日/年；16 bars/日 |
| 美股 regular-session 交易日 / 15m bars | 252 日/年；26 bars/日 |
| Materialized 4h | A 股 1/日；美股 2/日（第二根是 closing stub） |
| Weekly | 52/年 |
| 19 项 roster | 额外按最坏 24x7 计算，实际上会有重叠且多数不是 24x7 |

年增量（6,500 美股口径）：

- A 股：`5,500 × (3,904 15m + 244 4h + 244 1d + 52 1w)` ≈ **2,444 万 rows/年**；
- 美股：`6,500 × (6,552 15m + 504 4h + 252 1d + 52 1w)` ≈ **4,784 万 rows/年**；
- 合计约 **7,230 万 rows/年**，19 项的额外量小于约 70 万 rows/年。

### 8.2 两个 retention 场景

| 场景 | 保留规则 | 6,500 美股口径 | 10,000 ticker 上界 |
|---|---|---:|---:|
| A：个人研究 hot/warm | `15m+4h` 2 年；`1d+1w` 10 年 | ≈ **1.75 亿 rows** | ≈ **2.35 亿 rows** |
| B：深历史 | `15m+4h` 5 年；`1d+1w` 20 年 | ≈ **4.20 亿 rows** | ≈ **5.65 亿 rows** |

### 8.3 磁盘估算

当前 SQLite schema 使用多列 TEXT、7 个数值/时间字段和两条复合索引。[schema](../../src/kline/models.py) 在没有代表性数据 benchmark 前，按下面的规划范围：

| 介质/布局 | 规划假设 | 场景 A | 场景 B |
|---|---:|---:|---:|
| 当前风格 SQLite，含索引 | 200–350 bytes/row | **35–82 GB** | **84–198 GB** |
| 排序 + 压缩 Parquet 冷存储 | 40–80 bytes/row | **7–19 GB** | **17–45 GB** |

还必须另留：

- 至少 1 份一致性备份；
- WAL/checkpoint、索引重建/VACUUM 临时空间；
- source raw payload/flat files；
- 未来 universe 增长和 revisions。

因此设备规划应留 **2–3 倍 headroom**。若当前 `raw_upstream_responses.response_body` 对全量请求长期保存 JSON，它可能比 normalized candles 更大；生产版应只长期保留 manifest、checksum、request/receipt 和短 TTL/sampled raw payload，而不是永久复制每次完整响应。[当前 raw response schema](../../src/kline/models.py)

## 9. NAS / 数据库部署

### 9.1 明确禁止：Mac 上的 SQLite 直接打开 NAS SMB/NFS 文件

当前 store 每次连接执行 `PRAGMA journal_mode=WAL`。[store](../../src/kline/store.py) SQLite 官方明确说明：

- WAL 要求所有进程在同一 host，**不支持 network filesystem**；[WAL](https://www.sqlite.org/wal.html)
- 网络文件系统的 sync/locking 可能实现不正确，导致性能差、事务失败甚至数据库损坏；早期测试“看起来能用”不是安全证据；[SQLite over a network](https://www.sqlite.org/useovernet.html)
- 官方选择清单说，如果数据和发 SQL 的 application 被网络分开，应选 client/server database。[When to use SQLite](https://www.sqlite.org/whentouse.html)

所以 `KLINE_DB_PATH=/Volumes/NAS/.../kline.db` 对当前实现是 **NO-GO**。

### 9.2 三种部署方式的区别

| 方式 | 是否安全 | 是否节省本地空间 | 适用判断 |
|---|---|---|---|
| 外接 APFS SSD active SQLite + NAS 一致性备份 | **是，MVP 主选** | 是，active DB 不占内置盘 | `/Volumes/Phone SSD` 已现场验证为 APFS。必须设置稳定 mount path、启动前 mount guard、断连 fail-closed，并用 Online Backup API、`VACUUM INTO` 或 `sqlite3_rsync` 生成一致快照再传 NAS。[SQLite Backup API](https://www.sqlite.org/backup.html) |
| 在 NAS 本机运行 datafeed/SQLite，DB 在 NAS 本地 volume | **可以**，前提是所有 SQLite read/write 都在 NAS 同一 host | 是 | 由 NAS 上的单一服务通过 HTTP/API 对外；Mac 不经 SMB 直接开 DB。需要验证 NAS 容器/runtime、CPU/RAM、UPS 和监控。 |
| 在 NAS 运行 PostgreSQL/MariaDB 服务，datafeed 走 TCP | **推荐的目标形态** | 是 | 数据库引擎和文件都在 NAS 本机；网络上传的是 SQL/API，不是 SQLite file I/O。适合 1–5 亿行、分区、并发读取、在线备份。 |

### 9.3 推荐迁移顺序

1. **现在：** 不热搬活动 DB；先完成 SSD mount guard、SQLite 一致性备份、integrity check 和 rollback 合同。
2. **MVP：** 将 active SQLite 受控切到 APFS SSD，以约 500 A 股、20–30 美股和去除 Treasury 后的跨市场 roster 跑 30 天，实测 bytes/row、写入速率、回填速度、缺口率和供应商修订。
3. **备份/冷层：** NAS 挂载与能力验证后，保存一致性 SQLite backup、供应商 flat files 和压缩 Parquet；不把 live WAL database 放在 SMB share。
4. **扩容门槛：** 只有当 universe、并发或保留期超出 SQLite MVP 预算时，才在 NAS 本机部署 PostgreSQL，并通过现有 storage port 切换 adapter。
5. **切换：** 任何 storage cutover 都要双写/校验一个完整交易周，row counts、OHLCV checksum、最新 closed bar、delist/rename cases 全部一致后再切读流量。

## 10. 必须由 Park 决定或补证的事项

这些不是工程师可以替 Park 猜的偏好：

1. **19 项保留合同：** Daily v2 的 19 项已验明；请确认是否原样保留，还是替换/删除某些 rate-level、perpetual 或 continuous-futures identity。
2. **统一四档还是按资产适用：** 美债 yield/利差是否豁免 `15m/4h`；指数 volume 是否允许 `NULL`。
3. **全美股口径：** common shares/ADRs，还是 ETF/OTC/优先股/权证也要。
4. **历史保留：** 采用场景 A、B，还是别的 retention；是否保存 delisted 的完整 intraday。
5. **盘中 SLA：** “每 4 小时任务会补齐 15m”是否足够，还是 A/美股盘中每根 15m 都要在 15–20 分钟内出现。
6. **session：** 美股 regular-only 还是 extended；`4h` 采用 session-relative closing stub 还是 fixed wall-clock。
7. **价格口径：** canonical raw；是否需要 qfq、hfq、total return；调整后的值是 query-time 还是物化。
8. **Gold/WTI/Silver 身份：** spot、具体 futures，还是自定义 continuous futures；若 continuous，谁定义 roll rule。
9. **用途/授权：** 纯个人查看、个人回测/agent、团队共享、未来产品化分别对应不同数据合同。必须向 TuShare/Massive 书面确认。
10. **NAS 能力：** 厂牌/型号、CPU 架构、RAM、可用盘、RAID/文件系统、Docker/VM/PostgreSQL 支持、UPS、1/2.5/10GbE、外网和备份目标。

## 11. 建议的下一步（仍然不改代码）

1. Park 回答上面 10 个 decision gates；把已验明的 19 项 manifest（或 Park 明确批准的改版）固化成合同。
2. 向 TuShare 和 Massive 发送完全相同的用途说明，拿到“持久化、本地/NAS、指标、回测/agent、是否允许长期保留”的书面答复。
3. 只读检查 NAS 能力和当前实际数据库路径/大小/写入者；不要先搬文件。
4. 做一个有上限的 source spike：50 A 股 + 50 美股 + 19 项，覆盖 `15m/4h/1d/1w`、上市/退市、分拆/分红、market holiday、服务重启 catch-up。
5. 用 spike 实测后再写 spec/tickets；届时数据库选择和容量就有真实数据，而不是估算。

---

### 证据等级说明

- 本文只把官方文档、官方代码仓库、交易所/供应商条款和本 repo 代码当事实来源。
- 价格、权限和历史范围是 2026-08-31 的页面状态，采购前必须重新核对。
- “未找到公开许可/API 合同”表示证据缺口，不表示法律上必然禁止；在拿到书面授权前，生产判断保持 fail-closed。
