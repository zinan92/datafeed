# 全量 Health Matrix 运行证据

- 观察时间：2026-09-01T05:49:17+00:00
- 运行地址：`http://127.0.0.1:18170/health-ui`
- 运行方式：隔离临时 SQLite；未连接 resident database、NAS 或公网监听
- API：`GET /api/mvp/health/matrix`
- manifest：`mvp_universe_v1`
- manifest hash：`bf276deacc6fc50606241d7d91ef1656725bc688ccfb3fec720865b510b49f25`
- response SHA-256：`24006e5017ceaf0dd065bc3f3085f735a3f1dc9197a3a0c34014453fcdcb66cd`

## API 实测

- 返回 `scope=full_216`，资产数 216：A 股 100、美股 100、跨市场 16。
- 返回 1,080 个 cell（216 × `15m/1h/4h/1d/1w`）；没有 `30m`。
- 每个时间级别的 `applicable + not_applicable` 等于 216；适用 cell 的状态计数和等于 applicable。
- 当前隔离库没有采集 run，且 manifest 中 A/美股 entitlement 仍为 blocked；因此当前总体状态为 `failed`，不是 verified/ready。
- 五个级别的 blocked/applicable 计数分别为：`15m 208/208`、`1h 209/209`、`4h 208/208`、`1d 216/216`、`1w 216/216`；不适用分别为 8、7、8、0、0。
- 数据库路径与 SSD volume 在响应中均为 redacted；NAS backup 为 pending，未伪造成功。

## 浏览器实测

- 页面标题为“资产 × 时间级别健康矩阵”，首屏顺序为覆盖概览 → 全量矩阵 → 最近一次运行 → 基础设施。
- 真实页面 DOM 显示三组：A 股 100、美股 100、跨市场 16。
- 搜索“Tesla”后只显示 Tesla 资产；市场筛选“跨市场”只显示跨市场分组。
- 点击“收起 · 美股（100 个资产）”后矩阵只保留分组行，点击展开恢复资产行。
- 点击真实 cell 后打开只读详情抽屉，显示来源标识等证据字段；没有交易控件、来源切换或系统通知。
- 页面脚本包含 30 秒轮询、10 秒 AbortController 超时、`cache: no-store`、API 失联快照冻结和 900 秒过期处理。

## 未解决事项

本证据证明全量 API/UI 从真实隔离服务读取了运行时 manifest 和存储事实，不证明数据源已取得持久化授权，也不证明采集 worker 已完成 7 天可靠性。后续 #71 负责实际 worker/备份运行与 7 天门槛。
