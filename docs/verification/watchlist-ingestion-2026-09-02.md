# Watchlist Universe 持久化运行证据 — 2026-09-02

## 合同与边界

- Issue: #120 v2；Owner 最终确认 42 项全部保留。
- Manifest: `configs/watchlist_manifest.json`
- Manifest version/hash: `watchlist_universe_v1` /
  `b240ba464cf3fbef019353a6e13ed993344ba3311e8a602111cef819809ed24e`
- Target: `/Users/wendy/park-data/market/kline.db`
- Lock: `/Users/wendy/park-data/market/watchlist-worker.lock`
- Runtime implementation: `349c7761a95dd95e62868984f4c73690f279b79e`
- 只跑 `1d`；不调用 Screening free-source profile，不打开 issue-71 DB，不切 8100。

运行前使用 SQLite online backup 创建了
`/Users/wendy/park-data/market/kline.pre-watchlist-120-20260902.db`，`PRAGMA quick_check=ok`。
开发期的两轮 scoped Watchlist 数据被清除后，从该 commit 重新执行完整首轮；删除范围仅限
`manifest_version=watchlist_universe_v1`，最终结果已重新写回，原 313,629 条非 Watchlist
candle 未改变。

## 最终真实全轮

命令：

```bash
PYTHONPATH=src python3 -m ops.watchlist_seed \
  --manifest configs/watchlist_manifest.json \
  --db /Users/wendy/park-data/market/kline.db \
  --lock /Users/wendy/park-data/market/watchlist-worker.lock \
  --batch-size 10 --request-interval 2 \
  --max-retries 2 --retry-backoff 5 --provider-timeout 45
```

Run IDs: `watchlist-20260902T142844Z-001` 到
`watchlist-20260902T143119Z-005`。

| 指标 | 实测 |
| --- | ---: |
| 总标的 | 42 |
| 当前轮 ready | 42 |
| 当前轮 failed/partial | 0 |
| 持久化 candle | 19,808 |
| source observations | 42 |
| quality receipts | 42 |
| watermarks | 42 |
| duplicate keys | 0 |
| future timestamps | 0 |
| 051505 rows | 0 |
| SQLite quick check | ok |

按资产类型：A 股个股 16 项 / 7,014 行；A 股 ETF 4 项 / 1,905 行；美股及韩股
22 项 / 10,889 行。`000660.KS` 通过 Yahoo 成功持久化 500 行，最新日线为
2026-09-01。新股 `688836` 保留真实 11 行、`688825` 保留真实 28 行，没有补造历史。

## A 股免费源压力读数

20 个 A 股/ETF cell 共记录 21 次 Tencent provider attempt：20 次 HTTP 200；
`159510` 首次出现一次无 HTTP status 的空 transport error，随后重试成功。按最终 runner
分类，该次为 `other_error=1`；它不是 429/403/5xx/timeout。

| 错误/延迟 | 实测 |
| --- | ---: |
| 429 | 0 |
| 403 | 0 |
| 5xx | 0 |
| timeout | 0 |
| other transient | 1（重试成功） |
| A-side P95 latency | 5,369.5 ms |

## Fail-closed 证明

- 审查发现旧 candle 可能把下一轮失败漂绿后，runner 已改为分别输出当前轮状态与
  `available_in_store`；“先成功、下一轮 provider 失败”的测试确认总状态为 partial，
  失败原因保留，即使库里已有旧 candle。
- 专属 lock 现在覆盖完整 42 项轮次；第二个完整调用在 lock 被占用时立即失败，且不写库。
- 真实验证中曾遇到 NVDA/AMAT/LRCX 的短暂 Yahoo provider error；该轮被诚实标为 partial。
  放慢请求后最终完整轮 42/42 success，没有把历史覆盖当成当轮成功。
- `configs/mvp_manifest.json`、`ops/mvp_stock_seed.py`、`TencentIndexProvider` 和
  `AShareFreeProvider` 相对 `origin/main` 无改动；#115 worker 仍在 issue-115 worktree 运行。
