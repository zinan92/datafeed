# datafeed

## 要去哪里
多资产 K 线数据服务:ticker+timeframe → 标准化 OHLCV + provenance(provider/fresh/synthetic 标记),A股/美股/加密/商品全覆盖;近期喂养 trading-system 与 tokenpulse,远期可作为独立 API 产品外卖。

## 现在在哪里(2026-08-31)
- V1 运行中:tushare/yahoo/binance 多源,ports-and-adapters,realtime strict 失败即显式报错、永不隐藏降级。
- `main@7f6b44d` 的 K 线 envelope 会同时返回 requested/selected source、provider、source mode、execution-venue、fresh 与 synthetic 身份；严格消费者不再从模糊 provider 名称推断来源。
- Alibaba Cloud 上的 trading-system Paper 通过 loopback datafeed 消费 Binance USD-M Futures 的 GOLD 最新/历史 K 线；Cloud preflight 已验证 execution-venue 身份、SQLite quick-check 与新鲜度。datafeed 只拥有行情合同，不拥有 scheduler、StrategyPlan 或交易命令权限。
- Park 2026-07-21 裁定为产品(非基础设施),上 Portfolio 板。
- `main@5c1a5dd` 已合并 MVP #44：机器可读的 100 A 股 + 100 美股 + 16 跨市场 manifest、reserve/quarantine、source/timeframe/identity 校验与激活闸门已就绪；当前仍是 `candidate`，TuShare/美国源 entitlement 未提供前不会宣称 verified。
- 当前工作：#45 `codex/issue-45-storage-receipts`，实现独立 `mvp_*` storage schema 与原子 run/receipt/watermark 写入；resident DB、NAS、provider、scheduler 尚未改动。

## 下一步
- 完成 #45 StoragePort 与 SQLite receipt/rollback contract；随后按 #46→#54 依赖链推进 calendar/adapter/orchestrator/scheduler/SSD-NAS/30-day acceptance。
- 保持 canonical envelope 向后兼容并持续验证 Binance execution-venue 新鲜度；若走 API 外卖路线,先立商业化合同(对外发布风险轴归 Park)。
