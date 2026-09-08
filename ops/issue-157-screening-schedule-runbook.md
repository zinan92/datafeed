# Issue #157 Screening schedule runbook

本票只提供代码和可复现启动命令；本轮不重启服务、不修改
`~/Library/LaunchAgents/*.plist`。#155 owner 将 canonical checkout 收敛完成后，按本 runbook
启动 Screening 大级别调度。

## 目标

- Screening manifest 使用 `1d + 4h`；股票标的的 `15m/1h/1w` 为
  `not_applicable`，不计入健康 ratio。
- 复用 `ops.mvp_reliability` 的同一个四小时 worker 和 lock；Watchlist 的独立日更任务不变。
- 数据库、runtime 和代码路径必须来自 #155 部署后的 canonical checkout。

## Owner 启动

```bash
cd /Users/wendy/datafeed-runtime
PYTHONPATH=src python3 -m ops.mvp_reliability \
  --scope screening \
  --manifest configs/mvp_manifest.json \
  --db /Users/wendy/park-data/market/kline.db \
  --lock /Users/wendy/park-data/market/mvp-worker.lock \
  --interval 14400
```

首次启动前先执行一次 `--once` 并保存 JSON receipt；确认 `receipt.requested_cells` 中只有
`1d`、`4h` 的可运行 cell，再以同样参数去掉 `--once` 常驻运行。实际启动状态在本票报告中标记为
“待 owner 启动”。

## 验收命令

```bash
curl -s http://127.0.0.1:8100/api/health/combined-matrix > /tmp/issue-157-after.json
python3 - <<'PY'
import json
payload = json.load(open('/tmp/issue-157-after.json'))
screening = [c for c in payload['cells'] if c['dataset'] == 'screening']
for tf in ('15m', '1h'):
    cells = [c for c in screening if c['timeframe'] == tf]
    print(tf, {'applicable': sum(c['applicability'] == 'applicable' for c in cells),
               'not_applicable': sum(c['applicability'] == 'not_applicable' for c in cells)})
print('status', payload['status'])
print('screening_last_success_at', payload['workers']['screening']['last_success_at'])
print('screening_4h_ratio', payload['coverage']['4h']['ratio'])
print('screening_1d_ratio', payload['coverage']['1d']['ratio'])
PY
```

要求 owner 保存启动后的 combined-matrix JSON 和 worker receipt。`last_success_at`、4h >= 0.90、
1d >= 0.90 以及原始 15m/1h `/api/candles` 查询均需以真实运行结果验收；本票未代 owner 启动，
因此不提前宣称这些 live 条件已通过。
