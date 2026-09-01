# 免费 source 3+3 端到端运行证据

## 最新复验

- 观察时间：2026-09-01T08:55:42+00:00
- 执行命令：`python -m ops.mvp_reliability --once --manifest configs/mvp_manifest.json --db <临时库> --lock <临时锁> --interval 14400`
- run_id：`mvp-20260901T085542Z`
- run status：`success`
- free profile hash：`9d21373cd123c2f69d36b552461a71f26a613afd02f6064184bd5953e5adca51`
- `technical_ready=30/30`；展示状态 `partial=30/30`（仅因 entitlement 未书面核实）
- 存储凭证：`candles=9,494`、`observations=30`、`quality=30`、`watermarks=30`、`transforms=12`
- 来源：A 股 `tencent_stock_free`；美股 `yahoo_finance_free`
- 请求顺序：日线、周线先于 15 分钟/1 小时/4 小时；日内失败不会阻断日/周落库，专项回归测试已覆盖。

- 观察时间：2026-09-01T08:41:21+00:00
- 执行命令：`python -m ops.mvp_reliability --once --manifest configs/mvp_manifest.json --db <临时库> --interval 14400`
- run_id：`mvp-20260901T084121Z`
- run status：`success`
- manifest：`mvp_universe_v1`
- free profile hash：`54f2b1bb8b1d61e254689bb3c8e594cf4c3b454ea378f77220050eee859dc17f`

## 落库事实

- 30/30 个 3+3 × `15m/1h/4h/1d/1w` cell 有真实闭合 K 线。
- `mvp_candles=9,494`、`source_observations=30`、`quality_receipts=30`、`watermarks=30`、`transform_receipts=12`。
- A 股三只的来源标识为 `tencent_stock_free`；美股三只的来源标识为 `yahoo_finance_free`。
- 每个 cell 的 `technical_status=ready`（30/30），展示状态为 `partial`，原因是免费个人 source 的 entitlement 尚未做书面核实；没有把它们标为 verified。
- `4h` 与 `1w` 均带 transform receipt；A 股 `4h` 使用会话规则，美股 `4h` 使用 Yahoo 15m 聚合。

## source 探测事实

- Yahoo Chart：AAPL/NVDA/TSLA 的 15m、1h、1d、1w 均返回非空 OHLCV。
- 腾讯财经：600519、300750、688981 的 15m/60m 均可返回；日线使用 Tencent qfq 接口。
- 同花顺 60 分钟/日线 fallback 在模拟失败切换测试中通过；百度当前网络返回 403，AKShare 当前 Eastmoney 路径遇到 SSL 错误，因此没有把它们标为主源。

## 边界

这些事实证明免费技术 source 已经能跑通 provider → quality → transform → watermark → SQLite → Dashboard 的链路；不证明任何 source 允许商业再分发。当前 profile 只用于本机个人研究，公网访问、数据销售和 NAS 迁移仍未开启。
