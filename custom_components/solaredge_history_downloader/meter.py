"""Resolve and validate the target sensor for history replacement.

Any integration's utility meter is supported generically: if the target's
entity object subclasses the core ``UtilityMeterSensor`` (or exposes the same
attributes), its cycle, tariff, and cron settings are read from the live
entity. Any other sensor is accepted as a plain cumulative (lifetime) total.

The caller declares the expected target type; the declaration is checked
against what is actually loaded, so a mismatch blocks the replacement instead
of silently writing history in the wrong shape.
"""

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
from homeassistant.helpers.entity import entity_sources
from homeassistant.helpers.entity_platform import async_get_platforms

from .history import MeterCycle

SUPPORTED_UNITS = {"Wh", "kWh", "MWh"}

SERVICE_CALIBRATE = "calibrate"


@dataclass(frozen=True, slots=True)
class TargetMeter:
    """Validated target sensor properties needed for reconstruction."""

    entity_id: str
    name: str
    unit: str
    cycle: MeterCycle
    offset: timedelta
    calibrate_domain: str | None


def resolve_target_meter(
    hass: HomeAssistant, entity_id: str, *, declared_meter: bool
) -> TargetMeter:
    """Resolve the target sensor, enforcing the declared target type."""
    state = hass.states.get(entity_id)
    if state is None:
        raise ServiceValidationError(f"Entity {entity_id} does not exist")

    unit = state.attributes.get(ATTR_UNIT_OF_MEASUREMENT)
    if unit not in SUPPORTED_UNITS:
        raise ServiceValidationError(
            f"Target sensor {entity_id} must use Wh, kWh, or MWh"
        )

    integration, sensor = _find_entity_object(hass, entity_id)
    is_meter = sensor is not None and _is_meter_like(sensor)
    if declared_meter and not is_meter:
        raise ServiceValidationError(
            f"Entity {entity_id} was declared a utility meter but no loaded "
            "utility meter entity was found for it; use target_type: sensor "
            "if it is a plain cumulative sensor"
        )
    if not declared_meter and is_meter:
        raise ServiceValidationError(
            f"Entity {entity_id} was declared a plain sensor but it is a "
            "utility meter; use target_type: meter so its history is "
            "reconstructed per reset cycle"
        )

    if is_meter:
        cycle = _meter_cycle(entity_id, sensor)
        offset = _meter_offset(hass, entity_id)
        calibrate_domain = (
            integration
            if hass.services.has_service(integration, SERVICE_CALIBRATE)
            else None
        )
    else:
        # Plain sensor: its history is a cumulative total that never resets,
        # and no meter calibration is attempted.
        cycle = MeterCycle.LIFETIME
        offset = timedelta(0)
        calibrate_domain = None

    return TargetMeter(
        entity_id=entity_id,
        name=state.name,
        unit=unit,
        cycle=cycle,
        offset=offset,
        calibrate_domain=calibrate_domain,
    )


def _find_entity_object(
    hass: HomeAssistant, entity_id: str
) -> tuple[str | None, Any | None]:
    """Return the owning integration name and live entity object, if any."""
    source = entity_sources(hass).get(entity_id)
    if source is None:
        return None, None
    integration = source["domain"]
    for platform in async_get_platforms(hass, integration):
        sensor = platform.entities.get(entity_id)
        if sensor is not None:
            return integration, sensor
    return integration, None


def _is_meter_like(sensor: Any) -> bool:
    """Duck-type any utility meter implementation, core or custom."""
    return hasattr(sensor, "_period") and hasattr(sensor, "_cron_pattern")


def _meter_cycle(entity_id: str, sensor: Any) -> MeterCycle:
    if getattr(sensor, "_tariff", None) is not None:
        raise ServiceValidationError(
            "Tariff utility meters are not supported because SolarEdge production "
            "history does not identify Home Assistant tariff periods"
        )
    period = getattr(sensor, "_period", None)
    if period is None:
        if getattr(sensor, "_cron_pattern", None):
            raise ServiceValidationError(
                "Utility meters with a custom cron reset schedule are not supported"
            )
        return MeterCycle.LIFETIME
    try:
        return MeterCycle(period)
    except ValueError as err:
        raise ServiceValidationError(
            f"Utility meter {entity_id} has an unsupported cycle: {period!r}"
        ) from err


def _meter_offset(hass: HomeAssistant, entity_id: str) -> timedelta:
    """Read the reset offset when the standard integration exposes it."""
    for meter_info in hass.data.get(DATA_UTILITY, {}).values():
        for sensor in meter_info.get(DATA_TARIFF_SENSORS, []):
            if sensor.entity_id == entity_id:
                offset = meter_info.get(CONF_METER_OFFSET, timedelta(0))
                if not isinstance(offset, timedelta):
                    raise ServiceValidationError(
                        f"Utility meter {entity_id} has an invalid reset offset"
                    )
                return offset
    return timedelta(0)
