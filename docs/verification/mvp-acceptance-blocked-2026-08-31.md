# Market Data Database MVP Acceptance

- Status: **blocked**
- Generated: `2026-08-31T21:30:00+00:00`
- Manifest: `mvp_universe_v1`
- Manifest hash: `fe0731ae0ea3e343400f6aff74249c91b00e7a31de2cbf6ab3ff98e5dcbc0173`
- Evidence kind: `not_supplied`
- Window: `not started` → `not started`

## Criteria

- `active_manifest`: **blocked** — {"observed": "candidate", "status": "blocked"}
- `entitlement`: **blocked** — {"blocked_instruments": 203, "status": "blocked"}
- `evidence_kind`: **blocked** — {"observed": "not_supplied", "status": "blocked"}
- `thirty_day_window`: **blocked** — {"end": null, "start": null, "status": "blocked"}
- `real_coverage`: **blocked** — {"health_status": null, "status": "blocked"}
- `restore`: **blocked** — {"observed": null, "status": "blocked"}
- `membership_freeze`: **blocked** — {"status": "blocked"}

## Blockers

- `manifest_not_active`: manifest must be activated by a real successful run
- `entitlement_blocked`: one or more manifest sources lack valid persistence entitlement
- `real_evidence_required`: mock/parser/HTTP-200 evidence cannot verify the MVP
- `window_not_started`: active manifest effective_at is missing
- `coverage_not_verified`: health must show a successful real-data run
- `restore_not_verified`: clean NAS restore with matching manifest hash is required
- `freeze_policy_mismatch`: active manifest must freeze membership for 30 days
