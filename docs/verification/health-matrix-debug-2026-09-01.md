# Health Dashboard 全红问题调试记录

## 现象

2026-09-01 15:49（截图）页面的矩阵全部为红色，覆盖率为 0%。

## 反馈回路实测

- `GET http://127.0.0.1:18171/api/mvp/health/matrix?scope=demo_3x3`：30 个 cell 全部 `blocked`，没有 `ready`。
- 全量接口：1,080 个 cell，其中 1,057 个 applicable cell 为 `blocked`，23 个为 `not_applicable`，`ready=0`。
- 隔离数据库 `datafeed-runtime-issue-71/data/kline.db`：2 个 `partial` terminal run，`mvp_candles=0`、`mvp_source_observations=0`、`mvp_quality_receipts=0`、`mvp_watermarks=0`。
- 两次 worker run：`mvp-20260901T060237Z`、`mvp-20260901T080012Z`；均未调用 provider，因为 manifest entitlement gate 在 provider 前阻断。
- manifest 中 3 个 A 股使用 `tushare_pro`、3 个美股使用 `us_authorized_pending`，六个资产均为 `source_status=blocked_for_entitlement`，required timeframes 为空、blocked timeframes 覆盖五个级别。
- 现有 resident 数据库另有 57,694 条旧 `legacy klines`，但没有 `mvp_*` 表/receipt，且标的和时间级别不等于当前 3+3 MVP 序列；它们不能被重新标成当前矩阵成功。

## 根因

这是 fail-closed 授权闸门的真实结果，不是浏览器缓存、API 读取错库或前端状态推断：没有 source entitlement receipt，就不能请求/持久化 A 股和美股数据。当前 worker 只运行批准的 3+3，因此没有可成功的公共 BTC/指数任务。

## 修复

- #79 已合并：首屏现在把总体状态显示为“授权阻塞”，横幅明确写“尚无可展示数据；不是采集程序崩溃”，覆盖卡片分别显示 `阻塞` 与 `失败` 数量。
- 机器状态、source/provider、entitlement 和 `status_reason` 仍可在只读详情抽屉中追踪。
- launchd/API/worker 仍只监听 `127.0.0.1:18171`，使用隔离本地库；没有改动现有 8100 服务、resident 数据库、SSD 或 NAS。

## 解除阻塞所需动作

需要 operator 提供可审计的 A 股/美股数据源授权（计划、时间级别、持久化/派生/展示权限和证据引用），再运行 source preflight 并写入 entitlement receipt。仅仅 API 能返回数据，不能直接解除闸门；在此之前页面保持 blocked 是正确状态。
