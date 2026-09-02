# 美股 Yahoo-only 主源运行证据 — 2026-09-02

## 运行切换

- Build: `bbd93a96c4fe9fa81ff3b58694fbf644e76ed8a4` (PR #112)
- Observer API: `http://127.0.0.1:18171/health-ui`
- Observer DB: `/Users/wendy/datafeed-runtime-issue-71/data/kline.db`
- `com.wendy.datafeed.mvp-worker`: launchd `running`, 4 小时循环，批大小 10，请求间隔 0.5 秒。
- `APCA_API_KEY_ID`、`APCA_API_SECRET_KEY`、`ALPACA_API_KEY`、`ALPACA_SECRET_KEY` 均未配置；Alpaca 没有被假设为可用。

## Yahoo-only 首轮

切换后的首轮从 `2026-09-02T02:04:08Z` 到 `2026-09-02T02:16:44Z`，共 40 批、1,000 个 source observations（100 美股 × 5 个时间级别，另含同轮 A 股采集）：

| 指标 | 结果 |
| --- | ---: |
| 批次 | 40/40 |
| worker 汇总 `selected_total` | 400 |
| rate-limit errors | 0 |
| server errors | 0 |
| worker P95 latency | 1,023.2 ms |
| Sina attempts | 0 |
| Yahoo provider attempts | 100 美股五级别均有尝试 |

### 美股结果

- 15m、1h、4h：100/100 通过质量闸并有水位。
- 1d、1w：99/100 通过；DHR 的两格因 Yahoo 返回 `2026-09-01` 的非法 OHLC（`High < Open`）而 fail-closed。Yahoo 的 repair 响应仍保留该矛盾，系统没有自行修正价格。
- `BRK.B` 仍通过 `BRK-B` Yahoo ticker alias 获取。
- 旧新浪 receipt 仅保留为历史审计，不参与当前 source identity。

### A 股同轮现象

本轮在北京时间开盘后立即执行。15m/1h 的当前形成 bar 被质量闸拒绝，因而出现批次 partial；这不是源断开，也没有写入未闭合 K 线。`601989` 仍是已知例外：日/周水位停在 2025 年，15m/1h/4h 两个免费源都没有可用数据。

## 当前判断

- 美股 source policy 已符合要求：Yahoo Finance 主源，不调用新浪；Alpaca 等待明确 key 与 entitlement receipt 后再接入。
- 当前最大未解决项不是限流，而是 DHR 的上游非法 OHLC，以及固定四小时轮询在 A 股开盘时触发的 forming-bar partial。
- 旧的 100+100 完整股票覆盖结论不能直接沿用为 Yahoo-only 的“全绿”：Yahoo 当前真实覆盖是美股 intraday 100/100、粗粒度 99/100，DHR 两格明确失败。
- 交付仍是个人研究/本地 observer；resident 8100、NAS、Treasury 和 live trading 路径未改动。
