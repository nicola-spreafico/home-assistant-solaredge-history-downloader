"""Resolve and validate a standard Home Assistant utility meter target."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from homeassistant.components.utility_meter.const import (
    CONF_METER_OFFSET,
    DATA_TARIFF_SENSORS,
    DATA_UTILITY,
)
from homeassistant.const import ATTR_UNIT_OF_MEASUREMENT
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError

from .history import MeterCycle


@dataclass(frozen=True, slots=True)
class TargetMeter:
    """Validated utility meter properties needed for reconstruction."""

    entity_id: str
    name: str
    unit: str
    cycle: MeterCycle
    offset: timedelta


def resolve_target_meter(hass: HomeAssistant, entity_id: str) -> TargetMeter:
    """Find a loaded standard utility_meter sensor by exact entity ID."""
    state = hass.states.get(entity_id)
    if state is None:
        raise ServiceValidationError(f"Entity {entity_id} does not exist")

    resolved: tuple[Any, dict[str, Any]] | None = None
    for meter_info in hass.data.get(DATA_UTILITY, {}).values():
        for sensor in meter_info.get(DATA_TARIFF_SENSORS, []):
            if sensor.entity_id == entity_id:
                resolved = sensor, meter_info
                break
        if resolved is not None:
            break
    if resolved is None:
        raise ServiceValidationError(
            f"Entity {entity_id} is not a loaded standard utility_meter sensor"
        )

    sensor, meter_info = resolved
    if getattr(sensor, "_tariff", None) is not None:
        raise ServiceValidationError(
            "Tariff utility meters are not supported because SolarEdge production "
            "history does not identify Home Assistant tariff periods"
        )

    period = getattr(sensor, "_period", None)
    cron_pattern = getattr(sensor, "_cron_pattern", None)
    if period is None and cron_pattern:
        raise ServiceValidationError(
            "Utility meters with a custom cron reset schedule are not supported"
        )
    try:
        cycle = MeterCycle(period) if period is not None else MeterCycle.LIFETIME
    except ValueError as err:
        raise ServiceValidationError(
            f"Unsupported utility meter cycle: {period!r}"
        ) from err

    offset = meter_info.get(CONF_METER_OFFSET, timedelta(0))
    if not isinstance(offset, timedelta):
        raise ServiceValidationError(
            f"Utility meter {entity_id} has an invalid reset offset"
        )

    unit = state.attributes.get(ATTR_UNIT_OF_MEASUREMENT)
    if unit not in {"Wh", "kWh", "MWh"}:
        raise ServiceValidationError(
            f"Utility meter {entity_id} must use Wh, kWh, or MWh"
        )

    return TargetMeter(
        entity_id=entity_id,
        name=state.name,
        unit=unit,
        cycle=cycle,
        offset=offset,
    )
