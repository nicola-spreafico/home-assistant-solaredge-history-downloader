"""Tests for SolarEdge history conversion."""

from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from custom_components.solaredge_history_downloader.history import (
    DownloadGranularity,
    EnergyInterval,
    MeterCycle,
    aggregate_intervals,
    reconstruct_history,
    statistics_start,
    validate_granularity_for_meter,
)

ROME = ZoneInfo("Europe/Rome")


def _daily(day: int, value: str, *, month: int = 1) -> EnergyInterval:
    start = datetime(2024, month, day, tzinfo=ROME)
    return EnergyInterval(start, start + timedelta(days=1), Decimal(value))


def test_monthly_aggregation_uses_calendar_boundaries() -> None:
    intervals = [_daily(30, "1000"), _daily(31, "2000"), _daily(1, "3000", month=2)]

    result = aggregate_intervals(intervals, DownloadGranularity.MONTHLY, ROME)

    actual = [
        (point.start.month, point.end.month, point.watt_hours) for point in result
    ]
    assert actual == [
        (1, 2, Decimal("3000")),
        (2, 2, Decimal("3000")),
    ]


def test_daily_meter_state_resets_while_sum_remains_monotonic() -> None:
    intervals = [_daily(1, "1000"), _daily(2, "2000")]

    result = reconstruct_history(intervals, MeterCycle.DAILY, ROME, "kWh")

    assert [point.state for point in result] == [Decimal("1"), Decimal("2")]
    assert [point.sum for point in result] == [Decimal("1"), Decimal("3")]


def test_monthly_meter_accumulates_points_inside_cycle() -> None:
    intervals = [_daily(1, "1000"), _daily(2, "2000"), _daily(1, "4000", month=2)]

    result = reconstruct_history(intervals, MeterCycle.MONTHLY, ROME, "kWh")

    assert [point.state for point in result] == [
        Decimal("1"),
        Decimal("3"),
        Decimal("4"),
    ]
    assert result[-1].sum == Decimal("7")


def test_lifetime_meter_never_resets() -> None:
    intervals = [_daily(1, "1000"), _daily(2, "2000")]

    result = reconstruct_history(intervals, MeterCycle.LIFETIME, ROME, "kWh")

    assert [point.state for point in result] == [Decimal("1"), Decimal("3")]
    assert result[-1].sum == Decimal("3")


def test_lifetime_produces_one_point() -> None:
    intervals = [_daily(1, "1000"), _daily(2, "2000")]

    result = aggregate_intervals(intervals, DownloadGranularity.LIFETIME, ROME)

    assert len(result) == 1
    assert result[0].watt_hours == Decimal("3000")
    assert result[0].start == intervals[0].start
    assert result[0].end == intervals[-1].end


def test_partial_month_keeps_actual_end() -> None:
    start = datetime(2024, 2, 1, tzinfo=ROME)
    partial = EnergyInterval(
        start=start,
        end=start + timedelta(days=12, hours=10),
        watt_hours=Decimal("5000"),
    )

    result = aggregate_intervals([partial], DownloadGranularity.MONTHLY, ROME)

    assert result[0].end == partial.end


def test_statistics_timestamp_is_utc_hour_aligned() -> None:
    interval = _daily(1, "1000")
    point = reconstruct_history([interval], MeterCycle.MONTHLY, ROME, "kWh")[0]

    result = statistics_start(point)

    assert result.utcoffset() == timedelta(0)
    assert (result.minute, result.second, result.microsecond) == (0, 0, 0)
    assert result == datetime(2024, 1, 1, 22, tzinfo=ZoneInfo("UTC"))


def test_granularity_must_not_be_coarser_than_meter_cycle() -> None:
    with pytest.raises(ValueError, match="too coarse"):
        validate_granularity_for_meter(
            DownloadGranularity.ANNUAL,
            MeterCycle.MONTHLY,
            timedelta(0),
        )


def test_finer_granularity_is_allowed() -> None:
    validate_granularity_for_meter(
        DownloadGranularity.HOURLY,
        MeterCycle.MONTHLY,
        timedelta(0),
    )


def test_coarse_bucket_cannot_reconstruct_offset_cycle() -> None:
    with pytest.raises(ValueError, match="offset"):
        validate_granularity_for_meter(
            DownloadGranularity.MONTHLY,
            MeterCycle.MONTHLY,
            timedelta(days=1),
        )


def test_interval_crossing_meter_reset_is_rejected() -> None:
    start = datetime(2024, 1, 31, 23, tzinfo=ROME)
    crossing = EnergyInterval(
        start=start,
        end=start + timedelta(hours=2),
        watt_hours=Decimal("1000"),
    )

    with pytest.raises(ValueError, match="reset boundary"):
        reconstruct_history([crossing], MeterCycle.MONTHLY, ROME, "kWh")


def test_overlapping_source_intervals_are_rejected() -> None:
    first = _daily(1, "1000")
    overlapping = EnergyInterval(
        first.start + timedelta(hours=1), first.end, Decimal("500")
    )

    with pytest.raises(ValueError, match="overlap"):
        aggregate_intervals([first, overlapping], DownloadGranularity.LIFETIME, ROME)
