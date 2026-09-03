# Yahoo post-close 日线边界证据 — 2026-09-03

## 原始差分

北京时间 2026-09-03 09:22（纽约 2026-09-02 21:22）直接比较：

- raw Yahoo QCOM 已包含 `2026-09-02`；
- 合并前 `USStockProvider.fetch()` 只返回到 `2026-09-01`；
- resident 8100 同样只返回到 `2026-09-01`。

因此缺口来自 provider 的 calendar-midnight cutoff，不是 Yahoo 尚未发布数据。

## 确定性边界

- America/New_York 17:59：当前交易日排除。
- America/New_York 18:00、18:01：只有上游实际提供时才允许当前交易日。
- date-only `end=2026-09-02`：仍只允许到 2026-09-01。
- timestamp `end=2026-09-02T23:15:00Z`：转换为纽约 19:15，允许已闭合的
  2026-09-02。
- timestamp end 自身在纽约 17:59/18:00/18:01 的测试分别得到前一日/当日/当日；即使
  验证时钟已经到次日，17:59 的显式上界也不能泄漏当日 session。
- `.KS` 使用 Asia/Seoul；北京时间 07:15 对应首尔早晨时，只允许前一已闭合交易日。
- 周末保留最近市场交易日；上游缺少当前行时 repair 后仍不造数。

## 真实 production-shaped probe

在 feature branch 使用与 IngestionOrchestrator 相同的 aware timestamp start/end：

| ticker | status | latest daily | synthetic/exclusion |
| --- | --- | --- | --- |
| QCOM | success | 2026-09-02 | 无补造；本窗口无排除 |
| AAPL | success | 2026-09-02 | 无补造；本窗口无排除 |
| 000660.KS | success | 2026-09-02 | Seoul timezone；本窗口无排除 |

测试覆盖 before/at/after、date-only end、timestamp end、Seoul、weekend/no-row、weekly 与
#118 行隔离回归。验证期间未修改或重启 resident 8100、#115 API/worker 或任何数据库。
