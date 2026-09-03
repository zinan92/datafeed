# Consumer Market Data Database cutover verification — 2026-09-03

Status: **partial — implementation and rollback verified; literal 69/69 live byte equality blocked by upstream nondeterminism**

## Scope and request denominator

The request set was enumerated from the installed Newsletter and Human K-line Review code before
implementation and posted to issue #134. Treasury is owner-excluded.

- Newsletter: 28 in-scope requests.
- Human Review: 34 primary requests plus 7 conditional 1h fallbacks.
- Total acceptance denominator: 69 requests.
- Three unsupported 4h requests are rejected before the query-backend seam, as before.

`ops/verify_consumer_cutover.py` records the exact consumer file SHA-256 values, uses the real
request policies and limits, and compares HTTP status, canonical response bytes, candle bytes and
candle count. Only `age_seconds`, top-level `instrument_id`, and the newly declared backend/proxy
metadata are excluded from the canonical byte digest.

## Real history backfill

The bounded one-shot runner used the canonical Watchlist lock and database. It did not alter either
scheduler.

| Observation | Result |
|---|---:|
| selected daily series | 10 |
| quality pass | 10/10 |
| promoted candles | 14,603 |
| blocked cells | 0 |
| rate-limit/entitlement failures | 0 |

Receipt: `/Users/wendy/park-data/market/consumer-cutover-backfill-134.json`.

UUP/DXY was subsequently held on the explicit legacy path because Yahoo intermittently omitted the
2026-08-28 row from otherwise identical history fetches. The service reports
`market_window_unverified`; no row was synthesized.

## Isolated same-build comparison

Two services ran from `e60857910f3858a5c931b4f97f9873e2e71e0284`:

- `127.0.0.1:18174`: `query_backend=legacy`
- `127.0.0.1:8100`: `query_backend=market_first`

The final closed-window lockstep comparison produced:

| Result | Count |
|---|---:|
| total requests | 69 |
| exact canonical matches | 61 |
| Market Data Database routes | 15 |
| Market-routed candle mismatches | **0** |
| explicit legacy routes | 51 |
| pre-query unsupported requests | 3 |
| legacy/upstream live mismatches | 8 |

All eight differences were on legacy/upstream routes: dated micro-contract 30m/1h, QQQ/SPY 30m,
WTI 30m, and one Tencent STAR50 request. Repeating the unchanged legacy service also changed these
responses; Yahoo revised returned bars between adjacent calls and Tencent intermittently returned an
empty response. These are not Market Data Database rows and the candidate exercised the same
provider code, source and request policy.

Evidence:

- `/Users/wendy/park-data/market/cutover-134-final-baseline.json`
- `/Users/wendy/park-data/market/cutover-134-final-candidate.json`
- `/Users/wendy/park-data/market/cutover-134-final-comparison.json`

Therefore the literal full-set 69/69 live byte criterion is not claimed as passed. The materially
switched set is 15/15 exact; the remaining eight differences demonstrate why two separate live
upstream fetches cannot be used as a deterministic byte oracle.

## Real consumer probes

With the candidate on 8100:

- Newsletter built its real `market-regime-daily-source-bundle-v2`: 31/31 ready (the product still
  requests its three owner-excluded Treasury panels), with 9 Market Data Database and 22 explicit
  legacy/upstream slots.
- Human Review `/api/overview?refresh=true` returned 16 assets / 48 timeframe tiles: 33 ready,
  1 insufficient-history and 14 unavailable; the 34 returned primary backend identities comprised
  6 Market Data Database and 28 explicit legacy/upstream routes.

No consumer repository file was changed.

## Cutover and rollback drill

The original plist was backed up byte-identically:

`f3b8d6aca99cca7b79c05e511fc5a404240ed3c31e87c8129d055edcb7461f1d`

The drill used only `launchctl bootout` then `launchctl bootstrap`:

1. legacy build `752698e` on 8100 → candidate `market_first` build `e608579`;
2. candidate → byte-identical rollback plist, restoring build `752698e` and a successful real SPY
   response;
3. rollback → candidate, restoring `market_first` and proving a Market DB SPY response plus an
   explicit legacy BTC 4h response.

The first bootstrap attempts immediately after `bootout` encountered launchd's transient I/O error
while the prior label was still terminating. The label and port were inspected before retrying; no
`kickstart -k` was used.

#115 API/worker, 18171, 18172 and the Human Review process retained their original PIDs/builds
throughout the drill.

## Automated validation

- Full suite after the final envelope change: 349 passed.
- Ruff on every changed Python file: passed.
- Gitleaks: no leaks.
- Whole-repository Ruff still reports the pre-existing unrelated unused `timedelta` import in
  `src/kline/providers/binance_usdm.py`; this issue did not change that provider.
