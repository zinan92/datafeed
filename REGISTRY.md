# datafeed

## 要去哪里
多资产 K 线数据服务:ticker+timeframe → 标准化 OHLCV + provenance(provider/fresh/synthetic 标记),A股/美股/加密/商品全覆盖;近期喂养 trading-system 与 tokenpulse,远期可作为独立 API 产品外卖。

## 现在在哪里(2026-08-31)
- V1 运行中:tushare/yahoo/binance 多源,ports-and-adapters,realtime strict 失败即显式报错、永不隐藏降级。
- `main@7f6b44d` 的 K 线 envelope 会同时返回 requested/selected source、provider、source mode、execution-venue、fresh 与 synthetic 身份；严格消费者不再从模糊 provider 名称推断来源。
- Alibaba Cloud 上的 trading-system Paper 通过 loopback datafeed 消费 Binance USD-M Futures 的 GOLD 最新/历史 K 线；Cloud preflight 已验证 execution-venue 身份、SQLite quick-check 与新鲜度。datafeed 只拥有行情合同，不拥有 scheduler、StrategyPlan 或交易命令权限。
- Park 2026-07-21 裁定为产品(非基础设施),上 Portfolio 板。
- `main@74e8810` 已合并 MVP #44–#50：216 instrument manifest、独立 `mvp_*` storage/receipt/watermark schema、calendar/4H/weekly quality seam、TuShare gate、授权 US seam、16 cross-market mapping、resumable run_once 已就绪；当前 A/US/index entitlement 仍 pending，manifest 不会宣称 verified，resident DB/NAS 尚未切换。
- 当前工作：#51 `codex/issue-51-mvp-worker`，实现独立 ≤4h worker、single-run lock、SSD guard callback 与 health/serving status；不改 resident launchd、不切 live DB。

## 下一步
- 完成 #51 MVP worker/health surface；随后按 #52→#54 依赖链推进 SSD/NAS/30-day acceptance。
- 保持 canonical envelope 向后兼容并持续验证 Binance execution-venue 新鲜度；若走 API 外卖路线,先立商业化合同(对外发布风险轴归 Park)。
