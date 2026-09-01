# datafeed

## 要去哪里
多资产 K 线数据服务:ticker+timeframe → 标准化 OHLCV + provenance(provider/fresh/synthetic 标记),A股/美股/加密/商品全覆盖;近期喂养 trading-system 与 tokenpulse,远期可作为独立 API 产品外卖。

## 现在在哪里(2026-09-01)
- V1 运行中:tushare/yahoo/binance 多源,ports-and-adapters,realtime strict 失败即显式报错、永不隐藏降级。
- `main@7f6b44d` 的 K 线 envelope 会同时返回 requested/selected source、provider、source mode、execution-venue、fresh 与 synthetic 身份；严格消费者不再从模糊 provider 名称推断来源。
- Alibaba Cloud 上的 trading-system Paper 通过 loopback datafeed 消费 Binance USD-M Futures 的 GOLD 最新/历史 K 线；Cloud preflight 已验证 execution-venue 身份、SQLite quick-check 与新鲜度。datafeed 只拥有行情合同，不拥有 scheduler、StrategyPlan 或交易命令权限。
- Park 2026-07-21 裁定为产品(非基础设施),上 Portfolio 板。
- `main@5c8b114` 已合并 MVP #44–#52、#66、#68、#69、#70：216 instrument manifest、独立 `mvp_*` storage/receipt/watermark schema、15m/1h/4h/1d/1w 时间级别、calendar/1h/4H/weekly quality seam、TuShare/授权 US gate、16 cross-market mapping、resumable run_once、独立 ≤4h worker/health、SSD/NAS backup/restore safeguards、只读 provider preflight receipts、中文 3+3×5 到全量 216×5 Health Matrix/API/UI 已就绪；A/US entitlement 和部分源覆盖仍 pending/partial，manifest 不会宣称 verified，resident DB/NAS 尚未切换。
- #66 preflight 真实证据：easy_tdx MacClient 的 3 个 A 股样本在 15m/1h/1d/1w 返回非零成交量；4h 因会话输入不完整明确 blocked。Yahoo 美股样本技术可取但持久化/派生/展示权利未核实，仍为 partial。证据见 `docs/research/provider-preflight-2026-09-01.*` 和 `provider-preflight-easy-tdx-2026-09-01.*`。
- 当前工作 frontier：#71 七天可靠性与 worker/备份运行证据（承接 #70 全量矩阵）；随后回到 #54 真实 30-day database acceptance。#70 的全量运行证据见 `docs/verification/health-matrix-full-runtime-2026-09-01.md`。

## 下一步
- #69 中文 3+3 Health Matrix、#70 全量 216×5 矩阵和交互已合并；现在由 #71 做七天可靠性与真实 worker/备份运行验收，通过后再回到 #54 真实 30-day database acceptance。
- 继续保持 provider/entitlement/quality 状态显式；不要把 preflight technical availability 当成持久化授权或 verified。
- 保持 canonical envelope 向后兼容并持续验证 Binance execution-venue 新鲜度；若走 API 外卖路线,先立商业化合同(对外发布风险轴归 Park)。
