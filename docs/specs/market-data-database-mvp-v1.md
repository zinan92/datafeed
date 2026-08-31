# Market Data Database MVP v1

## Outcome

Deliver a real, resumable, source-auditable K-line database MVP that can ingest and serve a fixed 100 A-share + 100 US-listed stock universe plus the approved 16 cross-market Candle Instruments on a four-hour heartbeat, while preserving the existing datafeed's fail-closed and provenance boundaries.

## Acceptance Criteria

- [ ] A machine-readable manifest validates exactly 216 MVP instruments: 100 A-share stocks, 100 US-listed company stocks, and 16 cross-market instruments, with no duplicate identity or unsupported security type.
- [ ] Every manifest cell declares source, provider symbol, required timeframe subset, calendar/timezone/session policy, volume semantics, adjustment basis, and aggregation-rule version; `not_applicable` and `blocked_for_entitlement` are explicit states.
- [ ] Authorized source adapters can persist closed `15m`, `4h`, `1d`, and `1w` rows with source/instrument identity and transform receipts; no MVP writes `30m` or Treasury K-lines, and no query-only derivation is counted as stored data.
- [ ] A four-hour scheduler performs watermark-based incremental catch-up with bounded retries, overlap, idempotent writes, and atomic run/quality/transform/watermark receipts; an interrupted run never advances its watermark.
- [ ] The active SQLite database is opened only on the verified APFS SSD after volume/mount checks; consistent backups are copied to NAS, and a clean restore drill passes without opening SQLite over SMB/NFS.
- [ ] Health output exposes run freshness, source/entitlement state, latest closed bar, gaps/duplicates, row counts, retention/backup age, and explicit failure states; raw payload retention is bounded and observable.
- [ ] A real-data 30-calendar-day MVP receipt proves coverage, freshness, gap/duplicate handling, re-run idempotency, company-action/identity cases, and backup restore; mocks or HTTP 200 alone cannot mark the MVP verified.

## In Scope

The 100×100 MVP manifests, the 16 non-Treasury cross-market instruments, source/identity and corporate-action metadata, four persisted timeframes, periodic ingestion, quality/transform/run receipts, bounded raw retention, SSD mount guard, NAS backup/cold layer, health reporting, and the 30-day real-data acceptance run.

## Forbidden Changes

Do not hot-move or delete the current resident database; do not open live SQLite WAL files over SMB/NFS; do not silently substitute ETFs for SPX/NDX; do not silently fall back between providers; do not fabricate missing/partial bars; do not write Treasury or `30m` rows into the MVP namespace; do not add indicators, trading commands, credentials, public redistribution, or full-market expansion.

## Problem Statement

The current datafeed is a request-driven K-line service with a small SQLite cache. It does not yet own a resumable, periodic ingestion lifecycle for a bounded multi-market universe. The existing store also retains complete raw upstream payloads without a bounded retention policy, and the current source registry is older than the deployed Daily asset contract.

Park wants a real, long-lived Market Data Database, but the first release must remain small enough to finish and observe end to end. The first release therefore uses a fixed MVP Universe rather than the full A-share and US-stock markets.

## Solution

Build a durable, source-aware ingestion capability around the existing datafeed ports and adapters.

The MVP Universe is:

- 100 A-share company stocks, selected for liquidity plus coverage of the approved hot-theme buckets;
- 100 US-listed operating-company stocks, selected for liquidity plus sector and identity edge cases;
- 16 non-Treasury cross-market instruments: SPX, NDX, DXY, SCHD, VIX, BTC, ETH, HYPE, Shanghai Composite, STAR 50, Shanghai Dividend, Nikkei 225, KOSPI, WTI, Gold, and Silver.

Treasury yield/curve series (`DGS2`, `DGS10`, `T10Y2Y`) are excluded from the Candle Instrument universe. The database persists all four requested timeframes: `15m` and `1d` base candles plus `4h` and completed `1w` candles derived under explicit versioned rules. It does not collect `30m`.

The first successful end-to-end run creates a versioned manifest and freezes its membership for 30 calendar days. New hot or liquid candidates remain in a reserve pool until a versioned refresh.

The active MVP database runs on the verified local APFS SSD. The NAS is a backup and cold-storage target; a live SQLite file must not be opened by the Mac from an SMB/NFS share. A future PostgreSQL-on-NAS deployment is an expansion path, not an MVP prerequisite.

## User Stories

1. As Park, I want one versioned MVP Universe, so that a database failure cannot be confused with a silent stock-list change.
2. As Park, I want exactly 100 A-share members, so that the first full run is bounded and affordable.
3. As Park, I want exactly 100 US-listed company members, so that the US adapter is exercised without attempting the entire market.
4. As Park, I want the A-share members to cover high-liquidity hot themes, so that the MVP remains useful for current research.
5. As Park, I want user-named anchors such as 长鑫科技, 宇树科技, 贵州茅台, 中银证券, 中际旭创, 天孚通信, 新易盛, and 佰维存储 represented by verified tickers, so that colloquial names do not become incorrect database identities.
6. As Park, I want SPX and NDX stored as actual index identities, so that SPY and QQQ are not silently substituted for the requested indices.
7. As Park, I want the non-Treasury cross-market roster preserved, so that the existing research context remains available while Treasury level series stay out of the candle database.
8. As Park, I want `30m` removed from the MVP contract, so that the system has one explicit intraday cadence rather than a legacy duplicate.
9. As Park, I want each instrument to have a stable internal identity independent of provider ticker spelling, so that aliases and exchange changes do not split its history.
10. As Park, I want provider symbols and source IDs stored separately, so that `BRK.B`, `BRK/B`, and similar variants cannot collide.
11. As Park, I want security type and share class recorded, so that common stock, ADR, and multiple classes are not blended.
12. As Park, I want ticker aliases and venue history to be effective-dated, so that `FB → META` and venue moves remain queryable.
13. As Park, I want corporate actions recorded independently from candles, so that splits, dividends, spin-offs, and ADR ratios can be reconciled.
14. As Park, I want raw/unadjusted candles distinguished from adjusted views, so that price history is never silently mixed.
15. As Park, I want the A-share source to use my renewed/authorized TuShare entitlement, so that the MVP does not depend on an expired token.
16. As Park, I want the US source to pass an explicit entitlement and storage-use gate, so that technical API access is not mistaken for permission to build a persistent database.
17. As Park, I want every source request to record its source, provider symbol, request window, response identity, and policy, so that a bad bar can be traced to the upstream observation.
18. As Park, I want the scheduler to run at least every four hours, so that 24/7 assets do not become stale.
19. As Park, I want the scheduler to respect A-share and US market calendars, so that closed markets are not reported as failed requests.
20. As Park, I want each run to begin from a persisted watermark with a small overlap, so that restarts and transient failures can catch up without duplicate rows.
21. As Park, I want only closed bars promoted to the durable serving layer, so that a still-forming candle is not mistaken for a completed observation.
22. As Park, I want `15m → 4h` aggregation to use an explicit session/bucket rule, so that partial sessions and lunch breaks are visible rather than silently dropped.
23. As Park, I want `1d → 1w` aggregation to publish completed weeks only, so that the weekly series is reproducible across providers.
24. As Park, I want derived candles to carry the input timeframe, source identity, aggregation-rule version, and input range, so that transformations are auditable.
25. As Park, I want an upstream gap, malformed row, or stale response to remain blocked or unavailable, so that the system never fabricates a candle.
26. As Park, I want retries to be bounded and idempotent, so that a provider outage does not create duplicate or contradictory data.
27. As Park, I want the database to keep durable receipts without permanently storing every large raw response, so that storage growth is bounded.
28. As Park, I want active SQLite on the APFS SSD, so that the MVP avoids the corruption risks of SQLite over SMB/NFS.
29. As Park, I want consistent database backups on the NAS, so that the active database can be restored without copying a live WAL file.
30. As Park, I want a restore drill before declaring the MVP ready, so that a backup file is proven usable rather than merely present.
31. As Park, I want the service to fail closed when the SSD mount is absent, so that launchd cannot silently recreate a database on the internal disk.
32. As Park, I want the NAS target to be health-checked before backup promotion, so that an unavailable share does not produce a false backup receipt.
33. As Park, I want the first run to persist a manifest hash and effective timestamp, so that later reports can identify the exact universe used.
34. As Park, I want the first 30 calendar days to keep membership fixed, so that freshness and coverage can be measured against a stable denominator.
35. As Park, I want new hot names to enter a reserve pool, so that they can be considered without mutating the active universe.
36. As Park, I want universe changes to create a new manifest version, so that history is not rewritten in place.
37. As Park, I want a health view to show last successful run, next due time, source failures, row counts, latest closed bar, and backup age, so that I can operate the database without reading logs.
38. As Park, I want the API to distinguish `ready`, `partial`, `blocked`, `stale`, and `not_applicable`, so that unsupported timeframes are not presented as provider outages.
39. As Park, I want the 16 Candle cross-market instruments and the three excluded legacy Treasury aliases mapped explicitly, so that downstream Daily consumers can migrate without hidden ETF substitutions.
40. As Park, I want no trading commands, broker credentials, or real-money paths in this capability, so that the Market Data Database remains a read-only/Paper boundary.
41. As Park, I want the implementation to preserve the existing datafeed source/provenance envelope, so that current consumers do not lose source identity.
42. As Park, I want a bounded real-data pilot before any full-market expansion, so that capacity, provider behavior, and licensing are measured rather than guessed.

## Implementation Decisions

- Reuse the existing MarketDataPort/adapter registry and source-aware candle identity, but do not call API-private fetch helpers from the scheduler. Add one high-level ingestion orchestration seam (conceptually `run_once(plan, clock) -> RunReceipt`) backed by separate source and storage ports; it owns scheduling, watermarking, quality promotion, persistence, and receipts.
- Keep the normalized storage key source-aware and instrument-aware. Do not use display ticker as the only identity.
- Add a versioned instrument/universe manifest model covering stable instrument ID, display symbol, provider symbol, source ID, asset/security type, share class, exchange/venue validity, ticker aliases, corporate actions, adjustment basis, and manifest version.
- Add a machine-readable manifest for all 216 MVP instruments (100 A-share, 100 US-listed, and 16 cross-market) with `required_timeframes` as an explicit subset of `{15m, 4h, 1d, 1w}`, plus source, calendar, timezone, session, volume, adjustment, and aggregation-rule fields. `not_applicable` is a legal status and must not be disguised as source failure. Markdown research files are evidence, not runtime input.
- Persist raw/unadjusted `15m` and `1d` company-stock candles, and persist the derived `4h` and completed `1w` rows in the serving database. Every derived row must have a transformation receipt containing input timeframe, source identity, aggregation-rule version, and input range; query-time calculation alone does not count as MVP database storage.
- Treat `volume` as source-semantic: traded volume, quote-derived volume, or not applicable. Do not encode unavailable index volume as a false zero.
- Allow `volume` to be NULL when `volume_semantics=not_applicable`; never use a false zero for an index without traded volume.
- Use explicit market calendars and timezone metadata per instrument. Closed-bar eligibility must be deterministic and testable.
- Use a four-hour heartbeat with persisted watermarks and one-to-two-bar overlap. A run fetches the missing closed range, upserts idempotently, records observations, and then promotes derived periods.
- Define provider worker/thread boundaries, concurrency limits, retry/backoff, and per-source rate budgets so synchronous provider SDKs cannot block the async service or violate upstream limits.
- Keep daily/weekly finalization tied to the provider's finalized session window. Do not publish an in-progress daily or weekly bar as complete.
- Keep provider fallback explicit. A failed source is blocked/unavailable unless the caller or manifest names an allowed fallback; no Yahoo/Tencent/Sina silent substitution is permitted.
- Treat 688825 and 688836 as deliberate new-listing cases. They may carry a typed `new_listing_exception` while their available history is shorter than the normal window; missing history is not filled with zeros or synthetic rows.
- Use TuShare Pro as the A-share adapter after an operator supplies a current entitlement. The A-share MVP manifest is the exact 100-candidate list captured in the approved universe artifact, subject to the first-run liquidity/status gate.
- Use a licensed/authorized US market-data adapter selected by the source-entitlement ticket. Massive is the technical whole-market reference; Alpaca is a bounded pilot alternative. Yahoo/yfinance is not the production authority.
- Record an entitlement receipt for every production source, including source ID, allowed history, timeframe permissions, persistence rights, derived/non-display use, validity period, and evidence reference. Missing entitlement is a typed `blocked_for_entitlement` state.
- Treat the exact 100×100 candidate lists as candidate manifests until the first real, timestamped liquidity snapshot passes. A failed candidate is replaced only from a pre-screened reserve and produces a new manifest version.
- Freeze membership for 30 calendar days after the first successful end-to-end run. After the freeze, refresh by version using a documented rolling liquidity window and theme quotas; never rotate daily.
- Run active SQLite only on a local filesystem owned by the host running the database. For the MVP, target the verified APFS SSD. Back up through SQLite's consistency-preserving backup API or equivalent, then copy the completed backup to NAS.
- Do not place the live SQLite WAL database on an SMB/NFS mount. If future scale requires a database server, run PostgreSQL on the NAS host and access it over TCP; do not turn an SMB file path into a pseudo-server.
- Persist durable run/quality/transform/backup receipts and keep large raw payloads under explicit TTL/sampling or cold-archive policy. Raw response retention must not be unbounded.
- Make each ingestion attempt atomic at the contract level: candle upserts, quality receipt, transform receipt, and watermark advancement must all succeed before a run is `success`; an interruption must not advance the watermark.
- Add an SSD mount guard that validates the target volume UUID, filesystem, and mount state before creating or opening the database. Failure exits without falling back to the internal disk.
- Keep legacy Treasury and `30m` rows readable in an isolated legacy namespace if retained, but stop MVP writes and reject them from the new manifest; do not delete them as part of this MVP.
- Keep launchd integration separate from the current resident service until the new MVP has passed a controlled cutover and rollback drill. No live DB hot move is part of the first implementation ticket.
- Keep indicators (MACD, RSI, moving averages), research prose, news, fundamentals, options, broker execution, and public data redistribution outside this K-line MVP.

## Testing Decisions

- Tests must exercise external behavior at the highest available seam: manifest validation, source selection, closed-bar promotion, idempotent re-run, quality blocking, derived-timeframe receipts, backup/restore, and health reporting.
- Unit tests cover identity normalization, manifest rules, calendar/bucket aggregation, watermark overlap, retention policy, and error classification.
- Provider contract tests use recorded response shapes only for deterministic parsing; they must not claim live availability or licensing.
- Bounded integration tests use real, authorized data for representative A-share/US instruments and the cross-market roster. A successful mock or HTTP 200 alone is not acceptance.
- Scheduler tests cover first run, restart catch-up, transient failure, provider rate limit, market-closed window, duplicate overlap, and a run exceeding its four-hour interval.
- Database tests cover source/instrument/timeframe uniqueness, raw-vs-adjusted separation, partial writes, WAL checkpoint/backup, restore into a clean database, and absent-SSD mount fail-closed behavior.
- Acceptance requires a real 30-day MVP observation receipt with coverage/freshness/gap/duplicate counts, latest closed bars, source identities, manifest hash, and a successful restore drill.
- Existing datafeed provider, provenance, store, timeframe, health, and live API tests are prior art; new tests should extend their public contracts rather than assert private implementation details.

## Out of Scope

- Full 5,000+ A-share coverage or all US-listed securities.
- Dynamic daily universe rotation, automatic “hot stock” recommendations, or performance-based selection.
- Treasury yield/curve K-lines (`DGS2`, `DGS10`, `T10Y2Y`) in this candle database.
- `30m` candles.
- Silent fallback between Yahoo, Tencent, Sina, TuShare, Massive, Alpaca, or other providers.
- Persisting MACD, RSI, moving averages, news, research prose, fundamentals, options, or capital-flow data as part of this K-line MVP.
- Public API resale, market-data redistribution, or team/commercial sharing before provider contracts explicitly allow it.
- Real-money trading, broker order APIs, credentials, live execution paths, or strategy authorization.
- Directly opening an SQLite file from an SMB/NFS NAS share.
- Moving or deleting the current resident database before a separate backup/cutover/rollback contract passes.

## Further Notes

- The old A-share repo is a theme and candidate reference, not the canonical Market Data Database. Its current checkout has theme documentation/configuration but no verified active database to adopt.
- The exact candidate lists and their source evidence are maintained as research artifacts; the implementation contract must re-check listing status and liquidity at the first real run.
- The earlier 30-stock US research artifact is superseded by the 100-stock candidate manifest and must not be used as runtime input.
- The user's current storage statement is sufficient for capacity planning, but NAS mount path, filesystem, Docker/PostgreSQL support, UPS, and network reliability remain deployment facts to verify before moving cold storage or the database server.
- The current datafeed DB has a small normalized candle footprint but disproportionately large raw response bodies. The MVP must make retention observable from the first run.
- Any source entitlement, token renewal, NAS login, or purchase is a user-controlled gate. The implementation must expose a typed blocked state rather than pretending the capability is verified.
