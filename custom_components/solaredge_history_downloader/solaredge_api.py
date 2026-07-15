"""SolarEdge Monitoring API history downloader."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .history import DownloadGranularity, EnergyInterval, aggregate_intervals

_HOURLY_MAX_DATE_DELTA_DAYS = 13


class SolarEdgeDataError(ValueError):
    """Raised when SolarEdge returns an incomplete or inconsistent payload."""


class MonitoringClient(Protocol):
    """Subset of the solaredge client used by this integration."""

    async def get_site_details(self, site_id: int) -> dict[str, Any]: ...

    async def get_site_data(self, site_ids: list[int]) -> dict[str, Any]: ...

    async def get_energy(
        self,
        site_ids: list[int],
        start_date: datetime,
        end_date: datetime,
        time_unit: str,
    ) -> dict[str, Any]: ...


ClientFactory = Callable[..., AbstractAsyncContextManager[MonitoringClient]]


class SyncMonitoringClient:
    """Adapt the synchronous solaredge client for async use."""

    def __init__(
        self,
        api_key: str,
        *,
        client_factory: Callable[[str], Any] | None = None,
    ) -> None:
        if client_factory is None:
            from solaredge import MonitoringClient

            client_factory = MonitoringClient
        self._client = client_factory(api_key)

    async def __aenter__(self) -> SyncMonitoringClient:
        return self

    async def __aexit__(self, *_: Any) -> None:
        return None

    async def get_site_details(self, site_id: int) -> dict[str, Any]:
        return await asyncio.to_thread(self._client.get_details, site_id)

    async def get_site_data(self, site_ids: list[int]) -> dict[str, Any]:
        return await asyncio.to_thread(self._client.get_data_period, site_ids[0])

    async def get_energy(
        self,
        site_ids: list[int],
        start_date: datetime,
        end_date: datetime,
        time_unit: str,
    ) -> dict[str, Any]:
        return await asyncio.to_thread(
            self._client.get_energy,
            site_ids[0],
            start_date.date().isoformat(),
            end_date.date().isoformat(),
            time_unit,
        )


@dataclass(frozen=True, slots=True)
class SiteDataPeriod:
    """Validated SolarEdge site metadata used for a download."""

    site_id: int
    name: str
    timezone: ZoneInfo
    start: date
    end: date


@dataclass(frozen=True, slots=True)
class DownloadResult:
    """Complete in-memory SolarEdge history ready for reconstruction."""

    site: SiteDataPeriod
    intervals: list[EnergyInterval]
    requests: int


class SolarEdgeHistoryDownloader:
    """Validate a site and download all requested production history."""

    def __init__(
        self,
        api_key: str,
        site_id: int,
        *,
        client_factory: ClientFactory | None = None,
    ) -> None:
        self._api_key = api_key
        self._site_id = site_id
        self._client_factory = client_factory

    async def async_download(
        self,
        granularity: DownloadGranularity,
        *,
        start_date: date | None = None,
    ) -> DownloadResult:
        """Validate credentials/site and download the complete data period."""
        client_factory = self._client_factory or _default_client_factory()
        client = await asyncio.to_thread(client_factory, self._api_key)
        async with client:
            details_payload = await client.get_site_details(self._site_id)
            period_payload = await client.get_site_data([self._site_id])
            site = _parse_site(self._site_id, details_payload, period_payload)
            download_start = start_date or site.start
            if download_start > site.end:
                raise SolarEdgeDataError(
                    "Requested start date is after the SolarEdge data period end"
                )

            time_unit, max_days = _api_time_unit(granularity)
            values: dict[datetime, Decimal] = {}
            request_count = 0
            for window_start, window_end in _date_windows(
                download_start, site.end, max_days
            ):
                response = await client.get_energy(
                    [self._site_id],
                    datetime.combine(window_start, time.min),
                    datetime.combine(window_end, time.min),
                    time_unit=time_unit,
                )
                request_count += 1
                for timestamp, watt_hours in _parse_energy_values(
                    response, site.timezone
                ):
                    if timestamp in values and values[timestamp] != watt_hours:
                        raise SolarEdgeDataError(
                            "SolarEdge returned conflicting values for "
                            f"{timestamp.isoformat()}"
                        )
                    values[timestamp] = watt_hours

        if not values:
            raise SolarEdgeDataError(
                "SolarEdge returned no production values for the site data period"
            )

        source_intervals = [
            EnergyInterval(
                start=timestamp,
                end=min(
                    _interval_end(timestamp, time_unit),
                    _download_end(site.end, site.timezone),
                ),
                watt_hours=watt_hours,
            )
            for timestamp, watt_hours in sorted(values.items())
        ]
        return DownloadResult(
            site=site,
            intervals=aggregate_intervals(source_intervals, granularity, site.timezone),
            requests=request_count,
        )


def _default_client_factory() -> ClientFactory:
    try:
        from solaredge import AsyncMonitoringClient
    except ImportError:
        try:
            from solaredge import MonitoringClient
        except ImportError as sync_err:
            raise SolarEdgeDataError(
                f"Unable to import a supported solaredge client: {sync_err}"
            ) from sync_err
        return lambda api_key: SyncMonitoringClient(
            api_key, client_factory=MonitoringClient
        )
    return AsyncMonitoringClient


def _parse_site(
    site_id: int,
    details_payload: dict[str, Any],
    period_payload: dict[str, Any],
) -> SiteDataPeriod:
    details = details_payload.get("details")
    if not isinstance(details, dict):
        raise SolarEdgeDataError("SolarEdge site details are missing")
    returned_id = details.get("id")
    if returned_id is not None and str(returned_id) != str(site_id):
        raise SolarEdgeDataError(
            f"SolarEdge returned site {returned_id!r} instead of {site_id}"
        )

    timezone_name = _nested_value(details, "location", "timeZone")
    if not isinstance(timezone_name, str) or not timezone_name:
        raise SolarEdgeDataError("SolarEdge site timezone is missing")
    try:
        timezone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as err:
        raise SolarEdgeDataError(
            f"SolarEdge returned an unknown timezone: {timezone_name}"
        ) from err

    period = _find_site_period(site_id, period_payload)
    start = _parse_date(period.get("startDate"), "startDate")
    end = _parse_date(period.get("endDate"), "endDate")
    if end < start:
        raise SolarEdgeDataError("SolarEdge site data period ends before it starts")

    return SiteDataPeriod(
        site_id=site_id,
        name=str(details.get("name") or site_id),
        timezone=timezone,
        start=start,
        end=end,
    )


def _find_site_period(site_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    data_period = payload.get("dataPeriod")
    if not isinstance(data_period, dict):
        raise SolarEdgeDataError("SolarEdge site data period is missing")

    periods = data_period.get("list")
    if isinstance(periods, list):
        for period in periods:
            if not isinstance(period, dict):
                continue
            returned_id = period.get("id")
            if returned_id is None or str(returned_id) == str(site_id):
                return period
        raise SolarEdgeDataError(
            f"SolarEdge data period does not contain site {site_id}"
        )
    if "startDate" in data_period and "endDate" in data_period:
        return data_period
    raise SolarEdgeDataError("SolarEdge site data period is incomplete")


def _parse_energy_values(
    payload: dict[str, Any], timezone: ZoneInfo
) -> list[tuple[datetime, Decimal]]:
    energy = payload.get("energy")
    if not isinstance(energy, dict) or not isinstance(energy.get("values"), list):
        raise SolarEdgeDataError("SolarEdge energy values are missing")
    multiplier = _watt_hour_multiplier(energy.get("unit"))
    result: list[tuple[datetime, Decimal]] = []

    for item in energy["values"]:
        if not isinstance(item, dict) or item.get("value") is None:
            continue
        timestamp = _parse_timestamp(item.get("date"), timezone)
        try:
            value = Decimal(str(item["value"])) * multiplier
        except (InvalidOperation, ValueError) as err:
            raise SolarEdgeDataError(
                f"Invalid SolarEdge energy value: {item.get('value')!r}"
            ) from err
        if not value.is_finite() or value < 0:
            raise SolarEdgeDataError(
                f"Invalid SolarEdge energy value: {item.get('value')!r}"
            )
        result.append((timestamp, value))
    return result


def _parse_timestamp(value: Any, timezone: ZoneInfo) -> datetime:
    if not isinstance(value, str):
        raise SolarEdgeDataError(f"Invalid SolarEdge timestamp: {value!r}")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as err:
        raise SolarEdgeDataError(f"Invalid SolarEdge timestamp: {value!r}") from err
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone)
    return parsed.astimezone(timezone)


def _parse_date(value: Any, field: str) -> date:
    if not isinstance(value, str):
        raise SolarEdgeDataError(f"SolarEdge {field} is missing")
    try:
        return date.fromisoformat(value[:10])
    except ValueError as err:
        raise SolarEdgeDataError(f"Invalid SolarEdge {field}: {value!r}") from err


def _nested_value(value: dict[str, Any], *keys: str) -> Any:
    current: Any = value
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _api_time_unit(
    granularity: DownloadGranularity,
) -> tuple[str, int | None]:
    if granularity is DownloadGranularity.HOURLY:
        return "HOUR", _HOURLY_MAX_DATE_DELTA_DAYS
    if granularity is DownloadGranularity.DAILY:
        return "DAY", 365
    if granularity is DownloadGranularity.MONTHLY:
        return "MONTH", None
    return "YEAR", None


def _date_windows(
    start: date, end: date, max_days: int | None
) -> Iterator[tuple[date, date]]:
    if max_days is None:
        yield start, end
        return

    cursor = start
    while cursor <= end:
        window_end = min(cursor + timedelta(days=max_days), end)
        yield cursor, window_end
        if window_end == end:
            return
        cursor = window_end


def _watt_hour_multiplier(unit: Any) -> Decimal:
    multipliers = {
        "Wh": Decimal(1),
        "kWh": Decimal(1000),
        "MWh": Decimal(1_000_000),
    }
    try:
        return multipliers[unit]
    except (KeyError, TypeError) as err:
        raise SolarEdgeDataError(
            f"Unsupported SolarEdge energy unit: {unit!r}"
        ) from err


def _interval_end(start: datetime, time_unit: str) -> datetime:
    if time_unit == "HOUR":
        return start + timedelta(hours=1)
    if time_unit == "DAY":
        return datetime.combine(
            start.date() + timedelta(days=1), time.min, start.tzinfo
        )
    if time_unit == "MONTH":
        year_delta, month = divmod(start.month, 12)
        return datetime(start.year + year_delta, month + 1, 1, tzinfo=start.tzinfo)
    if time_unit == "YEAR":
        return datetime(start.year + 1, 1, 1, tzinfo=start.tzinfo)
    raise SolarEdgeDataError(f"Unsupported SolarEdge time unit: {time_unit}")


def _download_end(end: date, timezone: ZoneInfo) -> datetime:
    period_end = datetime.combine(end + timedelta(days=1), time.min, timezone)
    now = datetime.now(timezone)
    return min(period_end, now)
