"""Pure helpers for converting SolarEdge energy into utility meter history."""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from enum import StrEnum
from zoneinfo import ZoneInfo


class DownloadGranularity(StrEnum):
    """Granularity exposed by the update_history action."""

    HOURLY = "hourly"
    DAILY = "daily"
    MONTHLY = "monthly"
    ANNUAL = "annual"
    LIFETIME = "lifetime"


class MeterCycle(StrEnum):
    """Utility meter cycles supported by historical reconstruction."""

    QUARTER_HOURLY = "quarter-hourly"
    HOURLY = "hourly"
    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"
    BIMONTHLY = "bimonthly"
    QUARTERLY = "quarterly"
    YEARLY = "yearly"
    LIFETIME = "lifetime"


@dataclass(frozen=True, slots=True)
class EnergyInterval:
    """Energy produced during a half-open time interval."""

    start: datetime
    end: datetime
    watt_hours: Decimal

    def __post_init__(self) -> None:
        if self.start.tzinfo is None or self.end.tzinfo is None:
            raise ValueError("Energy interval timestamps must be timezone-aware")
        if self.end <= self.start:
            raise ValueError("Energy interval end must be after its start")
        if not math.isfinite(float(self.watt_hours)) or self.watt_hours < 0:
            raise ValueError("Energy interval value must be finite and non-negative")


@dataclass(frozen=True, slots=True)
class HistoryPoint:
    """One reconstructed raw-state and long-term-statistics point."""

    start: datetime
    end: datetime
    interval_value: Decimal
    state: Decimal
    sum: Decimal


@dataclass(frozen=True, slots=True)
class StatRow:
    """One long-term-statistics row ready for the recorder."""

    start: datetime
    state: Decimal
    sum: Decimal


def aggregate_intervals(
    intervals: list[EnergyInterval],
    granularity: DownloadGranularity,
    timezone: ZoneInfo,
) -> list[EnergyInterval]:
    """Aggregate ordered source intervals to the requested calendar granularity."""
    if not intervals:
        raise ValueError("SolarEdge returned no usable energy values")

    ordered = sorted(intervals, key=lambda interval: interval.start)
    _validate_non_overlapping(ordered)
    if granularity is DownloadGranularity.HOURLY:
        return ordered
    if granularity is DownloadGranularity.LIFETIME:
        return [
            EnergyInterval(
                start=ordered[0].start,
                end=ordered[-1].end,
                watt_hours=sum(
                    (interval.watt_hours for interval in ordered), Decimal(0)
                ),
            )
        ]

    grouped: dict[tuple[int, ...], list[EnergyInterval]] = defaultdict(list)
    for interval in ordered:
        local_start = interval.start.astimezone(timezone)
        grouped[_granularity_key(local_start, granularity)].append(interval)

    aggregated: list[EnergyInterval] = []
    for key in sorted(grouped):
        group = grouped[key]
        period_start, period_end = _granularity_bounds(key, granularity, timezone)
        aggregated.append(
            EnergyInterval(
                start=period_start,
                end=min(period_end, max(interval.end for interval in group)),
                watt_hours=sum((interval.watt_hours for interval in group), Decimal(0)),
            )
        )
    return aggregated


def reconstruct_history(
    intervals: list[EnergyInterval],
    meter_cycle: MeterCycle,
    timezone: ZoneInfo,
    unit: str,
    offset: timedelta = timedelta(0),
) -> list[HistoryPoint]:
    """Build utility-meter state and monotonically increasing statistic sum values."""
    if not intervals:
        raise ValueError("Cannot reconstruct an empty history")
    if offset < timedelta(0):
        raise ValueError("Utility meter offset cannot be negative")

    divisor = _unit_divisor(unit)
    ordered = sorted(intervals, key=lambda interval: interval.start)
    _validate_non_overlapping(ordered)
    cycle_totals: dict[tuple[int, ...], Decimal] = defaultdict(Decimal)
    total = Decimal(0)
    result: list[HistoryPoint] = []

    for interval in ordered:
        value = interval.watt_hours / divisor
        cycle_key = _interval_cycle_key(interval, meter_cycle, timezone, offset)
        cycle_totals[cycle_key] += value
        total += value
        result.append(
            HistoryPoint(
                start=interval.start,
                end=interval.end,
                interval_value=value,
                state=cycle_totals[cycle_key],
                sum=total,
            )
        )

    return result


def _interval_cycle_key(
    interval: EnergyInterval,
    cycle: MeterCycle,
    timezone: ZoneInfo,
    offset: timedelta,
) -> tuple[int, ...]:
    start_key = _meter_cycle_key(interval.start.astimezone(timezone) - offset, cycle)
    end_key = _meter_cycle_key(
        (interval.end - timedelta(microseconds=1)).astimezone(timezone) - offset,
        cycle,
    )
    if start_key != end_key:
        raise ValueError("An energy interval crosses a utility meter reset boundary")
    return start_key


def statistics_start(point: HistoryPoint) -> datetime:
    """Return an aware UTC, top-of-hour timestamp accepted by HA statistics."""
    return (
        (point.end - timedelta(microseconds=1))
        .astimezone(UTC)
        .replace(minute=0, second=0, microsecond=0)
    )


def standard_statistic_rows(points: list[HistoryPoint]) -> list[StatRow]:
    """Build one statistics row per reconstructed point for a standard meter."""
    return [
        StatRow(start=statistics_start(point), state=point.state, sum=point.sum)
        for point in points
    ]


def validate_granularity_for_meter(
    granularity: DownloadGranularity,
    cycle: MeterCycle,
    offset: timedelta,
) -> None:
    """Reject source buckets too coarse to reconstruct the meter truthfully."""
    granularity_rank = {
        DownloadGranularity.HOURLY: 0,
        DownloadGranularity.DAILY: 1,
        DownloadGranularity.MONTHLY: 2,
        DownloadGranularity.ANNUAL: 3,
        DownloadGranularity.LIFETIME: 4,
    }[granularity]
    cycle_rank = {
        MeterCycle.QUARTER_HOURLY: -1,
        MeterCycle.HOURLY: 0,
        MeterCycle.DAILY: 1,
        MeterCycle.WEEKLY: 1,
        MeterCycle.MONTHLY: 2,
        MeterCycle.BIMONTHLY: 2,
        MeterCycle.QUARTERLY: 2,
        MeterCycle.YEARLY: 3,
        MeterCycle.LIFETIME: 4,
    }[cycle]
    if granularity_rank > cycle_rank:
        raise ValueError(
            f"{granularity.value} data is too coarse for a {cycle.value} meter"
        )

    offset_seconds = offset.total_seconds()
    if granularity is DownloadGranularity.HOURLY:
        alignment_seconds = 3600
    elif granularity is DownloadGranularity.DAILY:
        alignment_seconds = 86400
    else:
        alignment_seconds = None
    if alignment_seconds is not None and offset_seconds % alignment_seconds:
        raise ValueError(f"The meter offset is not aligned to {granularity.value} data")
    if alignment_seconds is None and offset:
        raise ValueError(
            f"{granularity.value} data cannot reconstruct a meter with an offset"
        )


def _validate_non_overlapping(intervals: list[EnergyInterval]) -> None:
    for previous, current in zip(intervals, intervals[1:], strict=False):
        if current.start < previous.end:
            raise ValueError("Energy intervals overlap")


def _granularity_key(
    value: datetime, granularity: DownloadGranularity
) -> tuple[int, ...]:
    if granularity is DownloadGranularity.DAILY:
        return (value.year, value.month, value.day)
    if granularity is DownloadGranularity.MONTHLY:
        return (value.year, value.month)
    if granularity is DownloadGranularity.ANNUAL:
        return (value.year,)
    raise ValueError(f"Unsupported aggregation granularity: {granularity}")


def _granularity_bounds(
    key: tuple[int, ...], granularity: DownloadGranularity, timezone: ZoneInfo
) -> tuple[datetime, datetime]:
    if granularity is DownloadGranularity.DAILY:
        start = datetime.combine(
            datetime(key[0], key[1], key[2]).date(), time.min, timezone
        )
        return start, datetime.combine(
            (start.date() + timedelta(days=1)), time.min, timezone
        )
    if granularity is DownloadGranularity.MONTHLY:
        start = datetime(key[0], key[1], 1, tzinfo=timezone)
        next_year, next_month = divmod(start.month, 12)
        return start, datetime(
            start.year + next_year, next_month + 1, 1, tzinfo=timezone
        )
    if granularity is DownloadGranularity.ANNUAL:
        start = datetime(key[0], 1, 1, tzinfo=timezone)
        return start, datetime(start.year + 1, 1, 1, tzinfo=timezone)
    raise ValueError(f"Unsupported aggregation granularity: {granularity}")


def _meter_cycle_key(value: datetime, cycle: MeterCycle) -> tuple[int, ...]:
    if cycle is MeterCycle.QUARTER_HOURLY:
        return (value.year, value.month, value.day, value.hour, value.minute // 15)
    if cycle is MeterCycle.HOURLY:
        return (value.year, value.month, value.day, value.hour, value.fold)
    if cycle is MeterCycle.DAILY:
        return (value.year, value.month, value.day)
    if cycle is MeterCycle.WEEKLY:
        iso_year, iso_week, _ = value.isocalendar()
        return (iso_year, iso_week)
    if cycle is MeterCycle.MONTHLY:
        return (value.year, value.month)
    if cycle is MeterCycle.BIMONTHLY:
        return (value.year, (value.month - 1) // 2)
    if cycle is MeterCycle.QUARTERLY:
        return (value.year, (value.month - 1) // 3)
    if cycle is MeterCycle.YEARLY:
        return (value.year,)
    if cycle is MeterCycle.LIFETIME:
        return ()
    raise ValueError(f"Unsupported utility meter cycle: {cycle}")


def _unit_divisor(unit: str) -> Decimal:
    divisors = {
        "Wh": Decimal(1),
        "kWh": Decimal(1000),
        "MWh": Decimal(1_000_000),
    }
    try:
        return divisors[unit]
    except KeyError as err:
        raise ValueError(
            f"Unsupported target energy unit {unit!r}; expected Wh, kWh, or MWh"
        ) from err
