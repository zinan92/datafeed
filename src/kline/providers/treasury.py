"""Official U.S. Treasury daily par-yield levels and derived 2s10s spread."""

from __future__ import annotations

from csv import DictReader
from datetime import date, timedelta
from hashlib import sha256
from io import StringIO
import math
from typing import Any, Callable

import httpx

from kline.models import Candle, Timeframe, TimeframeTransform
from kline.providers.base import ProviderError


TREASURY_CSV_TEMPLATE = (
    "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/"
    "daily-treasury-rates.csv/{year}/all"
)
_TREASURY_COLUMNS = {"2 Yr": "US2Y", "10 Yr": "US10Y"}
_LEVEL_ALIASES = {
    "US2Y": "2 Yr",
    "DGS2": "2 Yr",
    "2Y": "2 Yr",
    "2 YR": "2 Yr",
    "US10Y": "10 Yr",
    "DGS10": "10 Yr",
    "10Y": "10 Yr",
    "10 YR": "10 Yr",
}
_SPREAD_ALIASES = {
    "US2S10S": "10 Yr-2 Yr",
    "T10Y2Y": "10 Yr-2 Yr",
    "2S10S": "10 Yr-2 Yr",
}
_MISSING_VALUES = frozenset({"", ".", "N/A", "NA", "NULL"})


class TreasuryCsvProvider:
    """Fetch official Treasury par-yield CSV rows as level candles.

    A provider instance is bound to either one official maturity column or the
    derived same-date 10Y-minus-2Y series.  It never consults FRED or another
    source when the official CSV is unavailable.
    """

    def __init__(
        self,
        *,
        timeout: float = 30,
        transport: httpx.AsyncBaseTransport | None = None,
        derived_spread: bool = False,
        today: Callable[[], date] | None = None,
    ) -> None:
        self._timeout = timeout
        self._transport = transport
        self._derived_spread = derived_spread
        self._today = today or date.today
        self.last_raw_response: dict[str, Any] | None = None
        self.timeframe_transform: TimeframeTransform | None = None
        self.source_identity: dict[str, Any] = {}

    def supported_timeframes(self) -> list[Timeframe]:
        return [Timeframe.DAY, Timeframe.WEEK]

    async def fetch(
        self,
        ticker: str,
        timeframe: Timeframe,
        *,
        start: str | None = None,
        end: str | None = None,
        limit: int = 500,
    ) -> list[Candle]:
        self.last_raw_response = None
        self.timeframe_transform = None
        self.source_identity = {}
        series = self._resolve_series(ticker)
        self.timeframe_transform = _treasury_transform(timeframe, series)
        self.source_identity = _source_identity(series, derived=self._derived_spread)
        if timeframe not in self.supported_timeframes():
            raise ProviderError(
                f"Treasury source does not support {timeframe.value}",
                suggestions=["Use timeframe=1d or timeframe=1w"],
            )

        self.last_raw_response = {
            "request_params": {
                "endpoint_template": TREASURY_CSV_TEMPLATE,
                "requested_ticker": ticker,
                "provider_symbol": series,
                "requested_timeframe": timeframe.value,
                "raw_timeframe": Timeframe.DAY.value,
                "years": [],
            },
            "response_body": {"responses": []},
            "status_code": None,
            "error": None,
        }

        try:
            requested_start = _parse_date(start, "start")
            requested_end = _parse_date(end, "end")
            today = self._today()
            completion_boundary = min(requested_end or today, today)
            if requested_start and requested_start >= completion_boundary:
                raise ProviderError("Treasury date range contains no closed observations")
            query_start = requested_start or (
                completion_boundary
                - timedelta(days=max(limit * (7 if timeframe == Timeframe.WEEK else 3), 365))
            )
            if query_start >= completion_boundary:
                raise ProviderError("Treasury date range is empty")
            csv_texts = await self._fetch_years(
                range(query_start.year, completion_boundary.year + 1)
            )
            candles = _parse_treasury_csv(
                csv_texts,
                series=series,
                derived_spread=self._derived_spread,
                start=requested_start,
                end=completion_boundary,
            )
            if timeframe == Timeframe.WEEK:
                candles = _aggregate_completed_level_weeks(
                    candles,
                    completion_boundary=completion_boundary,
                )
        except ProviderError as error:
            if self.last_raw_response is not None:
                self.last_raw_response["error"] = str(error)
            raise

        if not candles:
            error = ProviderError(
                f"Treasury returned no usable closed observations for {ticker} ({timeframe.value})",
                suggestions=["Check the requested range and official Treasury publication status"],
            )
            self.last_raw_response["error"] = str(error)
            raise error
        if limit and len(candles) > limit:
            candles = candles[-limit:]
        self.last_raw_response["request_params"]["public_row_count"] = len(candles)
        self.source_identity["observation_count"] = len(candles)
        return candles

    def _resolve_series(self, ticker: str) -> str:
        normalized = ticker.strip().upper()
        if self._derived_spread:
            series = _SPREAD_ALIASES.get(normalized, ticker.strip())
            if series != "10 Yr-2 Yr":
                raise ProviderError(
                    f"Unsupported Treasury spread symbol: {ticker}",
                    suggestions=["Use US2S10S or T10Y2Y"],
                )
            return series
        series = _LEVEL_ALIASES.get(normalized, ticker.strip())
        if series not in _TREASURY_COLUMNS:
            raise ProviderError(
                f"Unsupported Treasury maturity symbol: {ticker}",
                suggestions=["Use US2Y/DGS2 or US10Y/DGS10"],
            )
        return series

    async def _fetch_years(self, years: range) -> list[str]:
        raw = self.last_raw_response
        assert raw is not None
        texts: list[str] = []
        headers = {"Accept": "text/csv", "User-Agent": "Mozilla/5.0"}
        async with httpx.AsyncClient(
            timeout=self._timeout,
            transport=self._transport,
            follow_redirects=True,
            headers=headers,
        ) as client:
            for year in years:
                params = {
                    "_format": "csv",
                    "type": "daily_treasury_yield_curve",
                }
                url = TREASURY_CSV_TEMPLATE.format(year=year)
                raw["request_params"]["years"].append({"year": year, "params": params})
                try:
                    response = await client.get(url, params=params)
                    response.raise_for_status()
                except httpx.HTTPStatusError as exc:
                    raw["status_code"] = exc.response.status_code
                    raw["error"] = str(exc)
                    raise ProviderError(
                        f"Official Treasury CSV request failed for {year}: {exc}"
                    ) from exc
                except httpx.RequestError as exc:
                    raw["error"] = str(exc)
                    raise ProviderError(
                        f"Official Treasury CSV request failed for {year}: {exc}"
                    ) from exc
                raw["status_code"] = response.status_code
                text = response.text
                raw["response_body"]["responses"].append(
                    {
                        "year": year,
                        "status_code": response.status_code,
                        "body_sha256": sha256(text.encode("utf-8")).hexdigest(),
                        "body": text,
                    }
                )
                texts.append(text)
        return texts


def _parse_date(value: str | None, field: str) -> date | None:
    if value is None:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError as exc:
        raise ProviderError(f"Treasury {field} date is invalid: {value}") from exc


def _source_identity(series: str, *, derived: bool) -> dict[str, Any]:
    if not derived:
        return {
            "source_id": "treasury_official_csv",
            "provider_symbol": series,
            "endpoint": TREASURY_CSV_TEMPLATE,
            "dataset": "daily_treasury_yield_curve",
            "series_kind": "rate_level",
            "unit": "percent",
        }
    return {
        "source_id": "treasury_official_csv_derived",
        "provider_symbol": series,
        "endpoint": TREASURY_CSV_TEMPLATE,
        "dataset": "daily_treasury_yield_curve",
        "series_kind": "spread",
        "unit": "basis_points",
        "derivation": {
            "rule": "same_date_10y_minus_2y",
            "scale": 100,
            "input_source": "treasury_official_csv",
            "input_columns": ["10 Yr", "2 Yr"],
        },
    }


def _treasury_transform(timeframe: Timeframe, series: str) -> TimeframeTransform:
    if timeframe == Timeframe.WEEK:
        return TimeframeTransform(
            raw_timeframe=Timeframe.DAY,
            timeframe_origin="aggregated",
            aggregation={
                "kind": "level_last_observation",
                "rule": "completed_iso_week_last_level",
                "input_timeframe": Timeframe.DAY.value,
                "bucket_timezone": "America/New_York",
                "input_source": {
                    "source_id": (
                        "treasury_official_csv_derived"
                        if series == "10 Yr-2 Yr"
                        else "treasury_official_csv"
                    ),
                    "provider_symbol": series,
                },
            },
        )
    return TimeframeTransform(
        raw_timeframe=Timeframe.DAY,
        timeframe_origin="native",
        aggregation={"kind": "none", "rule": "native_passthrough"},
    )


def _parse_treasury_csv(
    texts: list[str],
    *,
    series: str,
    derived_spread: bool,
    start: date | None,
    end: date,
) -> list[Candle]:
    values_by_date: dict[date, float] = {}
    required = ["2 Yr", "10 Yr"] if derived_spread else [series]
    for text in texts:
        reader = DictReader(StringIO(text))
        headers = set(reader.fieldnames or ())
        missing_headers = [column for column in required if column not in headers]
        if "Date" not in headers or missing_headers:
            raise ProviderError(
                "Official Treasury CSV schema is missing required columns: "
                + ", ".join([*(["Date"] if "Date" not in headers else []), *missing_headers])
            )
        for row in reader:
            raw_date = str(row.get("Date", "")).strip()
            if not raw_date:
                raise ProviderError("Official Treasury CSV row has a missing Date")
            try:
                observation_date = date.fromisoformat(raw_date)
            except ValueError:
                try:
                    month, day, year = (int(part) for part in raw_date.split("/"))
                    observation_date = date(year, month, day)
                except (TypeError, ValueError) as exc:
                    raise ProviderError(f"Official Treasury date is malformed: {raw_date}") from exc
            if observation_date < (start or date.min) or observation_date >= end:
                continue
            if derived_spread:
                two_year = _parse_numeric(row.get("2 Yr"), "2 Yr", observation_date)
                ten_year = _parse_numeric(row.get("10 Yr"), "10 Yr", observation_date)
                if two_year is None or ten_year is None:
                    raise ProviderError(
                        "Official Treasury required same-date input is missing: "
                        f"{observation_date}"
                    )
                value = (ten_year - two_year) * 100
            else:
                value = _parse_numeric(row.get(series), series, observation_date)
                if value is None:
                    raise ProviderError(
                        "Official Treasury required observation is missing: "
                        f"{series} {observation_date}"
                    )
            if observation_date in values_by_date:
                raise ProviderError(
                    f"Official Treasury CSV returned duplicate date: {observation_date}"
                )
            values_by_date[observation_date] = value

    return [
        Candle(
            timestamp=observation_date.isoformat(),
            open=value,
            high=value,
            low=value,
            close=value,
            volume=0,
        )
        for observation_date, value in sorted(values_by_date.items())
    ]


def _parse_numeric(raw: Any, column: str, observation_date: date) -> float | None:
    text = "" if raw is None else str(raw).strip()
    if text.upper() in _MISSING_VALUES:
        return None
    try:
        value = float(text)
    except (TypeError, ValueError) as exc:
        raise ProviderError(
            f"Official Treasury value is malformed: {column} {observation_date}"
        ) from exc
    if not math.isfinite(value):
        raise ProviderError(f"Official Treasury value is non-finite: {column} {observation_date}")
    return value


def _aggregate_completed_level_weeks(
    candles: list[Candle], *, completion_boundary: date
) -> list[Candle]:
    groups: dict[tuple[int, int], list[Candle]] = {}
    for candle in candles:
        observation_date = date.fromisoformat(candle.timestamp)
        iso = observation_date.isocalendar()
        week_end = date.fromisocalendar(iso.year, iso.week, 5)
        if week_end >= completion_boundary:
            continue
        groups.setdefault((int(iso.year), int(iso.week)), []).append(candle)

    output: list[Candle] = []
    for rows in groups.values():
        rows.sort(key=lambda item: item.timestamp)
        value = rows[-1].close
        output.append(
            Candle(
                timestamp=rows[-1].timestamp,
                open=value,
                high=value,
                low=value,
                close=value,
                volume=0,
            )
        )
    return sorted(output, key=lambda item: item.timestamp)
