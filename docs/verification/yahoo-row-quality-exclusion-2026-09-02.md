# Yahoo 坏行隔离运行证据 — 2026-09-02

## 范围

- Issue: #118
- Branch: `codex/issue-118-quality-row-exclusion`
- Source: 免费 Yahoo / `src/kline/providers/us.py`
- 验证方式：直接调用 feature branch 的 `USStockProvider.fetch(..., timeframe=1d, limit=1)`；
  不写 observer DB，不切换 #115 的 launchd 服务，不触碰授权源或其他 provider。

## 真实 Yahoo 探针

2026-09-02（Asia/Shanghai）对 issue 中三个已知标的执行真实上游请求：

| 标的 | 请求结果 | 最新返回日线 | 排除记录 |
| --- | --- | --- | --- |
| QCOM | success | 2026-09-01 | 1 行：2021-09-01 `non_finite_ohlcv` |
| 000660.KS | success | 2026-09-01 | 3 行：2023-02-02、2023-02-09、2024-10-14 `ohlc_invariant` |
| DHR | success | 2026-09-01 | 本次上游响应已无坏行，0 行排除 |

QCOM 与 000660.KS 都返回了 Yahoo 当前正常的原始 K 线，同时在 `source_identity` 中带有
`quality_flags=["invalid_row_excluded"]`、排除数量、时间戳和逐行原因。DHR 在此前运行证据中
曾返回 2026-09-01 的 `High < Open`，但本次 Yahoo 响应已恢复为合法 OHLC；这说明上游数据会
漂移，因此 DHR 的旧故障形状由离线回归 fixture 固定，不把本次无排除误写成代码未生效。

## 防伪造与失败边界

- 返回列表只包含上游原始正常行；被排除时间戳不会以插值、前值或合成 OHLC 重新出现。
- repair 路径保持原样；排除发生在 repair 后仍不合格的行。
- 若窗口内所有行都被排除，provider 返回 `ProviderError`，raw receipt 的 error 为
  `all_rows_failed_quality_validation`，不会返回 empty-but-200。
- 排除明细随 `source_identity` 进入现有 FetchReceipt / ingestion source observation policy，
  因而 operator 可按 instrument 统计覆盖损失；本 issue 不改健康面板 UI。

## 自动化验证

- `tests/test_timeframe_contract.py`: QCOM non-finite、000660.KS/DHR invariant、负成交量、
  全部坏行、重复请求稳定性与不造数。
- provider + port + ingestion + live API 专项：49 passed。
- 完整套件与最终 lint 结果在 PR Validation 中记录。
