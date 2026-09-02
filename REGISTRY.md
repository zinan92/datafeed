# datafeed

## 要去哪里
多资产 K 线数据服务:ticker+timeframe → 标准化 OHLCV + provenance(provider/fresh/synthetic 标记),A股/美股/加密/商品全覆盖;近期喂养 trading-system 与 tokenpulse,远期可作为独立 API 产品外卖。

## 现在在哪里(2026-09-01)
- V1 运行中:tushare/yahoo/binance 多源,ports-and-adapters,realtime strict 失败即显式报错、永不隐藏降级。
- `main@1699575` 的 K 线 envelope 会同时返回 requested/selected source、provider、source mode、execution-venue、fresh 与 synthetic 身份；严格消费者不再从模糊 provider 名称推断来源。
- Alibaba Cloud 上的 trading-system Paper 通过 loopback datafeed 消费 Binance USD-M Futures 的 GOLD 最新/历史 K 线；Cloud preflight 已验证 execution-venue 身份、SQLite quick-check 与新鲜度。datafeed 只拥有行情合同，不拥有 scheduler、StrategyPlan 或交易命令权限。
- Park 2026-07-21 裁定为产品(非基础设施),上 Portfolio 板。
- `main@bbd93a9` 已合并 MVP #44–#52、#66、#68、#69、#70、#71 runner、#77 audit 修复、#79 状态文案修复、#81 免费源路由、#83 混合状态文案修复、#85 剩余 97+97 批处理/限流观测、#87 美股粗粒度源调整、#89 分阶段恢复、#91 Yahoo 点号 ticker 兼容、#93 空响应终止重试、#95 fallback 404 终止重试、#97 水位倒退保护、#99 provider 硬超时、#101 免费源 HTTP 超时上限、#103 Yahoo 历史请求线程化、#105 Yahoo 修复请求线程化、#107 Yahoo ISO intraday 窗口兼容、#111 Yahoo 美股全时间级别主源：216 instrument manifest、独立 `mvp_*` storage/receipt/watermark schema、15m/1h/4h/1d/1w 时间级别、calendar/1h/4H/weekly quality seam、TuShare/授权 US gate、个人无会员运行 profile（A 股 Tencent→Tonghuashun、美国 Yahoo 全时间级别；不调用新浪；Alpaca 仅待显式凭证/权限）、Yahoo 点号代码（BRK.B→BRK-B）、空响应 fail-closed、水位不倒退、provider 硬超时/15 秒源超时上限、Yahoo 同步请求线程化、16 cross-market mapping、resumable run_once、独立 ≤4h worker/health、SSD/NAS backup/restore safeguards、只读 provider preflight receipts、中文 3+3×5 到全量 216×5 Health Matrix/API/UI、3+3 四小时可靠性 runner/audit、隔离 observer DB 批量 seeder 与 rate-limit/P95 receipts、按周期阶段恢复已就绪；首轮完整 100+100 周期已真实落库，Yahoo-only 首轮 40/40 已真实运行，美股 15m/1h/4h 100/100、1d/1w 99/100（DHR 非法 OHLC 明确失败），Sina attempts=0；免费源技术数据已真实落库但 entitlement 仍显式 `partial/unverified`，七天可靠性门槛尚未完成，resident DB/NAS 尚未切换。证据见 `docs/verification/us-yahoo-primary-2026-09-02.md`。
- #66 preflight 真实证据：easy_tdx MacClient 的 3 个 A 股样本在 15m/1h/1d/1w 返回非零成交量；4h 因会话输入不完整明确 blocked。Yahoo 美股样本技术可取但持久化/派生/展示权利未核实，仍为 partial。证据见 `docs/research/provider-preflight-2026-09-01.*` 和 `provider-preflight-easy-tdx-2026-09-01.*`。
- 当前工作 frontier：#71 七天可靠性与 worker/备份运行证据（Yahoo-only 首轮已完成，尚未通过七天门槛；DHR 两个粗粒度格需保留 fail-closed；承接 #70 全量矩阵）。全红问题的根因和修复见 `docs/verification/health-matrix-debug-2026-09-01.md`，免费源复验见 `docs/verification/free-source-3x3-runtime-2026-09-01.md`；历史 100+100 seed 证据见 `docs/verification/full-stock-seed-rate-limit-2026-09-01.md`，Yahoo-only 当前证据见 `docs/verification/us-yahoo-primary-2026-09-02.md`；随后回到 #54 真实 30-day database acceptance。#70 的全量运行证据见 `docs/verification/health-matrix-full-runtime-2026-09-01.md`，#71 启动证据见 `docs/verification/health-matrix-reliability-2026-09-01.md`。
- 观察服务：`http://127.0.0.1:18171/health-ui` 由 `com.wendy.datafeed.mvp-api` KeepAlive 托管；`com.wendy.datafeed.mvp-worker` 每 4 小时写入 `/Users/wendy/datafeed-runtime-issue-71/data/kline.db`。这是隔离的可靠性观察库，不替换现有 8100 服务、resident DB、SSD 或 NAS。

## 下一步
- #69 中文 3+3 Health Matrix、#70 全量 216×5 矩阵和交互、#71 可靠性 runner、#81 免费 source 路由、#83 混合状态文案、#85 剩余 97+97 批处理器、#87 美股粗粒度源调整、#89 分阶段恢复、#91 Yahoo 点号 ticker 兼容、#93 空响应终止重试、#95 fallback 404 终止重试、#97 水位倒退保护、#99 provider 硬超时、#101 免费源 HTTP 超时上限、#103 Yahoo 历史请求线程化、#105 Yahoo 修复请求线程化、#107 Yahoo ISO intraday 窗口兼容、#111 Yahoo 美股全时间级别主源已合并；真实全量股票 seed 首轮已完成，Yahoo-only 首轮已验证 100 只美股五级别，DHR 两个粗粒度格按 fail-closed 记录，A 股开盘 forming-bar partial 按规则记录，601989 仍是已知缺口；全量常驻 worker 已切换为 100+100、4 小时刷新并保持 running。美股五级别统一 Yahoo，A 股继续 Tencent→Tonghuashun；日/周优先、日内失败降级、请求间隔/重试退避、P95/限流统计、水位不倒退、provider 超时和同步请求可取消已锁定。#71 七天验收仍开放且 blocked，#54 真实 30-day database acceptance 仍未开始。
- 继续保持 provider/entitlement/quality 状态显式；不要把 preflight technical availability 当成持久化授权或 verified。
- 保持 canonical envelope 向后兼容并持续验证 Binance execution-venue 新鲜度；若走 API 外卖路线,先立商业化合同(对外发布风险轴归 Park)。
