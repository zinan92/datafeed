# 3+3 Health Matrix 可靠性观察启动证据

- 观察时间：2026-09-01T06:03:27+00:00
- 持续 API：`http://127.0.0.1:18171/health-ui`
- 持续矩阵：`GET /api/mvp/health/matrix?scope=demo_3x3`
- launchd：`com.wendy.datafeed.mvp-api` 与 `com.wendy.datafeed.mvp-worker` 均为 running / KeepAlive
- 隔离数据库：`/Users/wendy/datafeed-runtime-issue-71/data/kline.db`（本地 SQLite；不触碰 resident DB、SSD 或 NAS）
- worker 间隔：14,400 秒（4 小时）
- manifest：`mvp_universe_v1`
- manifest hash：`bf276deacc6fc50606241d7d91ef1656725bc688ccfb3fec720865b510b49f25`
- 本次矩阵 response SHA-256：`1fbc2af6026dfb158ea693b78e72c33ac306727fd6cf46cfb4e8d9e9d25ff3f6`

## 首次真实运行

- run_id：`mvp-20260901T060237Z`
- terminal status：`partial`
- API 返回 30 个 cell（3 个 A 股 × 3 个美股 × 5 个时间级别）。
- 每个适用 cell 都是 `blocked`，原因 `entitlement_blocked`；没有任何 blocked/unavailable 被计入 ready。
- 五个时间级别的 coverage 均为 `applicable=6, ready=0, blocked=6`。
- worker 已计算下一次运行：`2026-09-01T10:02:37+00:00`。
- 数据源/持久化授权仍未核实，因此总体状态保持 `failed`，没有宣称 verified。

## 审计器首次观察

以 `2026-09-01T06:00:00+00:00` 到 `2026-09-01T06:10:00+00:00` 的短窗口运行审计：

- 总体 `blocked`，因为窗口远未满七个自然日。
- terminal receipt：`1/1`，当前短窗口为 100%。
- 最大静默间隔：`0.123` 小时；canonical duplicate keys：`0`；失败 run 推进 watermark：`0`。
- 七天门槛仍为 `blocked`；必须让 launchd 真实运行七天后再审计，不能用合成时间或回放数据代替。

## 浏览器证据

真实浏览器已打开持续页面并验证：中文标题、三组资产、30 个 cell、固定页内失败横幅；搜索、市场筛选、分组折叠和只读详情抽屉均可操作。截图已在本次运行的浏览器证据中采集；页面没有系统通知、重试按钮、来源切换或交易动作。

## 当前结论

可靠性观察已经开始，但 #71 尚未完成。满足七天窗口、95% terminal receipt、无八小时静默和故障/恢复/快照过期浏览器证据后，才可更新为 ready；任何 A/US 授权限制继续保持 blocked/partial。
