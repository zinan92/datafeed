# Screening + Watchlist 双库健康面板运行证据 — 2026-09-03

## 部署合同

- Implementation issue / PR: #128 / #129
- Evidence issue: #130
- Merged build: `ded8741e2cf29cc710f65f4e468fc082d517462f`
- URL: `http://127.0.0.1:18172/health-ui?view=combined&dataset=watchlist`
- API: `GET /api/health/combined-matrix`
- launchd label: `com.wendy.datafeed.health-dashboard`
- Runtime: `/Users/wendy/datafeed-runtime-health-dashboard`
- PID at verification: `47065`
- Plist backup: `/Users/wendy/Library/LaunchAgents/com.wendy.datafeed.health-dashboard.plist.deployed-20260903.bak`

服务使用独立 port/runtime/label，并设置 `KLINE_READ_ONLY=true`。Screening 与 Market Data
Database 分别通过 `KLINE_DB_PATH` / `KLINE_MARKET_DB_PATH` 读取；SQLite 使用 `mode=ro`
和 `query_only`，写方法被显式拒绝。回滚方式为 bootout 此独立 label，并保留上述 plist
备份；无需重启 #115 或 resident 8100。

## 真实 API

2026-09-03 16:42 CST 对 launchd 常驻服务请求，HTTP 200，约 4.18 秒：

| 指标 | 实测 |
| --- | ---: |
| combined assets | 274 |
| combined cells | 1,370 |
| Screening assets | 216 |
| Watchlist assets | 58 |
| Watchlist cells | 290 |
| Watchlist daily technical-ready | 58/58 |
| Watchlist cross-market daily rows | 16/16 |
| Screening DB status | ready |
| Market Data DB status | ready |

Watchlist 日线状态为 55 个 `partial`、3 个 `stale`。`partial` 保留公开源 entitlement 未核验
事实；`stale` 是按市场日历算出的新鲜度结果。页面没有把技术可用伪造成授权 `ready`。
Watchlist 的 15m/1h/4h/1w 共 232 个 cell 均显式 `not_applicable`。

## 真实浏览器

Codex in-app browser 对永久 18172 服务的 DOM 验证：

- 页面总体状态：`部分`
- 覆盖元信息：`共 290 个单元格 · 清单 watchlist_universe_v1`
- 分组：A 股 20、美股/韩股 22、跨市场 16
- 日线卡：`100% · 58/58 有数据`
- 数据库卡：`Market Data 数据库 · 只读 SQLite · 正常`
- Screening 页面仍显示 1,080 cells / 100 A 股 / 100 美股 / 16 跨市场，数据集筛选隐藏

SPX 日线详情直接显示：

- source: `yahoo_finance_etf`
- provider symbol: `SPY`
- metadata: `identity_role=proxy`, `proxy_for=S&P 500 Index`
- row count: 500
- latest closed: `2026-09-02T00:00:00+00:00`
- quality: pass
- watermark run: `watchlist-20260903T080126Z-005`

## 只读证明

同一次真实 combined API 请求前后主数据库 SHA-256：

| Database | Before | After |
| --- | --- | --- |
| Screening | `5475fe65ff7ae83a254abeb6125833e776115604efc7870debe00c7e883cc045` | same |
| Market Data | `2b2d5bccf20beb35b5efc4252e5794e32dcadb3295696f150aa7668bc9370749` | same |

SQLite 在 WAL 模式下可能维护用于读锁的 `-shm`/空 `-wal` sidecar；本证据的只读定义是主
数据库、schema 和业务数据未改变，且 connection/query contract 拒绝写入。

## 隔离证明

验证后仍为：

| Service | PID | Build / runtime |
| --- | ---: | --- |
| `com.wendy.datafeed.mvp-api` (18171) | 878 | `3167f7d...` / issue-115 |
| `com.wendy.datafeed.mvp-worker` | 912 | `3167f7d...` / issue-115 |
| `com.wendy.datafeed` (8100) | 915 | `752698e...` / datafeed-runtime-live |

三个 PID、build 和工作目录与 #128 部署前一致；没有修改或重启 #115/18171/8100。

