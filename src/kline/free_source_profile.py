"""Runtime overlay for the no-membership personal research profile.

The checked-in manifest keeps the paid/authorized source contracts explicit.
This overlay preserves canonical instrument identities while selecting the
free technical adapters requested for a private local MVP.  It is not a
commercial redistribution or entitlement claim.
"""

from __future__ import annotations

from dataclasses import replace

from kline.mvp_manifest import ALLOWED_TIMEFRAMES, MvpManifest


FREE_SOURCE_PROFILE = "free_personal_v1"
SCREENING_TIMEFRAMES = ("1d", "4h")
SCREENING_NOT_APPLICABLE_TIMEFRAMES = tuple(
    timeframe for timeframe in ALLOWED_TIMEFRAMES if timeframe not in SCREENING_TIMEFRAMES
)


def apply_free_source_profile(manifest: MvpManifest) -> MvpManifest:
    """Return a validated same-universe manifest using free technical sources."""

    instruments = []
    for item in manifest.instruments:
        if item.universe == "a_share":
            instruments.append(
                replace(
                    item,
                    source_id="tencent_stock_free",
                    required_timeframes=ALLOWED_TIMEFRAMES,
                    not_applicable_timeframes=(),
                    blocked_timeframes=(),
                    source_status="configured",
                    adjustment_basis="qfq",
                    metadata={**item.metadata, "source_profile": FREE_SOURCE_PROFILE},
                )
            )
        elif item.universe == "us_stock":
            instruments.append(
                replace(
                    item,
                    source_id="yahoo_finance_free",
                    required_timeframes=ALLOWED_TIMEFRAMES,
                    not_applicable_timeframes=(),
                    blocked_timeframes=(),
                    source_status="configured",
                    metadata={**item.metadata, "source_profile": FREE_SOURCE_PROFILE},
                )
            )
        else:
            instruments.append(item)
    return replace(manifest, instruments=tuple(instruments))


def apply_screening_scope(manifest: MvpManifest) -> MvpManifest:
    """Apply the Screening health/worker contract without changing query scope."""

    scoped = apply_free_source_profile(manifest)
    instruments = []
    for item in scoped.instruments:
        if item.universe in {"a_share", "us_stock"}:
            instruments.append(
                replace(
                    item,
                    required_timeframes=SCREENING_TIMEFRAMES,
                    not_applicable_timeframes=SCREENING_NOT_APPLICABLE_TIMEFRAMES,
                )
            )
        else:
            instruments.append(item)
    return replace(scoped, instruments=tuple(instruments))
