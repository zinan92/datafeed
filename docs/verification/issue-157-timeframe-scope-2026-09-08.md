# Issue #157 timeframe scope verification

## 本地代码验证

```text
PYTHONPATH=src python3 -m pytest -q tests/test_health_matrix.py tests/test_mvp_reliability.py tests/test_ingestion.py tests/test_mvp_worker.py
45 passed
```

The scope overlay keeps all 216 manifest identities and changes the 200 A-share/US stock
identities to required `1d + 4h`; their `15m`, `1h`, and `1w` cells are explicit
`not_applicable`. The worker entrypoint accepts `--scope screening` and passes only `1d + 4h`
to the existing atomic worker.

## Combined-matrix snapshots

Before snapshot, taken from the issue's 2026-09-07 observation:

```json
{"status":"failed","screening":{"unavailable":{"15m":200,"1h":200,"4h":200},"workers":{"screening":{"status":"idle","last_attempt_at":null}}},"note":"issue observation; 1d coverage 18%"}
```

After contract snapshot, produced by the local matrix regression fixture (no service restart and
no production database write):

```json
{"screening_stock_cells":{"15m":{"applicable":0,"not_applicable":200},"1h":{"applicable":0,"not_applicable":200},"4h":{"applicable":200},"1d":{"applicable":200},"1w":{"applicable":0,"not_applicable":200}},"worker_timeframes":["1d","4h"],"live_status":"待 owner 启动"}
```

The after snapshot is a contract-level local fixture, not live coverage evidence. The owner must
run the commands in `ops/issue-157-screening-schedule-runbook.md` after #155 deployment and attach
the real combined-matrix output. Until then, `last_success_at`, 4h >= 90%, 1d >= 90%, and live
candle compatibility remain unverified.

`601989` was removed from the checked-in manifest and replaced by `600150` / `600150.SH`.
