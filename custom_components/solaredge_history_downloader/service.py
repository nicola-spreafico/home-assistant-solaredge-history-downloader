"""Implementation of the SolarEdge history update action."""

from __future__ import annotations

import asyncio
import logging
from datetime import date
from typing import Any

import voluptuous as vol
from homeassistant.components.recorder import get_instance
from homeassistant.components.sensor import ATTR_STATE_CLASS
from homeassistant.components.utility_meter.const import (
    ATTR_VALUE,
    SERVICE_CALIBRATE_METER,
)
from homeassistant.const import (
    ATTR_DEVICE_CLASS,
    ATTR_ENTITY_ID,
    ATTR_FRIENDLY_NAME,
    ATTR_UNIT_OF_MEASUREMENT,
)
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.util import dt as dt_util
from httpx import HTTPStatusError, RequestError

from .const import (
    CONF_API_KEY,
    CONF_CONFIRM_REPLACEMENT,
    CONF_GRANULARITY,
    CONF_SITE_ID,
    CONF_START_DATE,
    CONF_TARGET_ENTITY,
    CONF_TARGET_TYPE,
    CONFIRM_REPLACEMENT,
    DATA_CONFIG,
    DATA_LOCKS,
    DOMAIN,
    TARGET_TYPE_METER,
    TARGET_TYPE_SENSOR,
)
from .history import (
    DownloadGranularity,
    reconstruct_history,
    standard_statistic_rows,
    validate_granularity_for_meter,
)
from .meter import TargetMeter, resolve_target_meter
from .recorder import async_replace_history
from .solaredge_api import SolarEdgeDataError, SolarEdgeHistoryDownloader

_LOGGER = logging.getLogger(__name__)

SERVICE_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_API_KEY): vol.All(cv.string, vol.Length(min=1)),
        vol.Required(CONF_SITE_ID): vol.All(vol.Coerce(int), vol.Range(min=1)),
        vol.Required(CONF_TARGET_ENTITY): cv.entity_id,
        vol.Required(CONF_TARGET_TYPE): vol.In(
            [TARGET_TYPE_METER, TARGET_TYPE_SENSOR]
        ),
        vol.Required(CONF_GRANULARITY): vol.In(
            [granularity.value for granularity in DownloadGranularity]
        ),
        vol.Required(CONF_CONFIRM_REPLACEMENT): vol.In([CONFIRM_REPLACEMENT]),
    }
)

INSPECT_SERVICE_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_API_KEY): vol.All(cv.string, vol.Length(min=1)),
        vol.Required(CONF_SITE_ID): vol.All(vol.Coerce(int), vol.Range(min=1)),
        vol.Required(CONF_START_DATE): cv.date,
        vol.Required(CONF_GRANULARITY): vol.In(
            [granularity.value for granularity in DownloadGranularity]
        ),
    }
)


async def async_update_history(
    hass: HomeAssistant, call: ServiceCall
) -> ServiceResponse:
    """Download and atomically replace one utility meter's recorder history."""
    api_key, site_id = _resolve_credentials(hass, call)
    entity_id: str = call.data[CONF_TARGET_ENTITY]
    lock = _target_lock(hass, entity_id)
    if lock.locked():
        raise ServiceValidationError(
            f"A history update is already running for {entity_id}"
        )

    async with lock:
        target = resolve_target_meter(
            hass,
            entity_id,
            declared_meter=call.data[CONF_TARGET_TYPE] == TARGET_TYPE_METER,
        )
        write_states = _validate_recorder_target(hass, target)
        granularity = DownloadGranularity(call.data[CONF_GRANULARITY])
        try:
            validate_granularity_for_meter(granularity, target.cycle, target.offset)
        except ValueError as err:
            raise ServiceValidationError(str(err)) from err
        download = await _async_download(
            api_key=api_key,
            site_id=site_id,
            granularity=granularity,
        )
        points = reconstruct_history(
            download.intervals,
            target.cycle,
            dt_util.get_default_time_zone(),
            target.unit,
            target.offset,
        )
        state = hass.states.get(entity_id)
        if state is None:
            raise ServiceValidationError(
                f"Entity {entity_id} disappeared before history replacement"
            )

        try:
            replacement = await async_replace_history(
                hass,
                entity_id=entity_id,
                name=target.name,
                unit=target.unit,
                attributes=_recorded_attributes(dict(state.attributes)),
                points=points if write_states else [],
                stat_rows=standard_statistic_rows(points),
            )
        except Exception as err:
            raise HomeAssistantError(
                "Recorder history replacement failed; the database transaction "
                "was rolled back"
            ) from err
        calibrated_value: str | None = None
        if target.calibrate_domain is not None:
            calibrated_value = format(points[-1].state, "f")
            await hass.services.async_call(
                target.calibrate_domain,
                SERVICE_CALIBRATE_METER,
                {
                    ATTR_ENTITY_ID: entity_id,
                    ATTR_VALUE: calibrated_value,
                },
                blocking=True,
            )

    return {
        "status": "success",
        "target_entity": entity_id,
        "target_type": call.data[CONF_TARGET_TYPE],
        "site_id": download.site.site_id,
        "site_name": download.site.name,
        "site_timezone": str(download.site.timezone),
        "granularity": granularity.value,
        "source_start": download.site.start.isoformat(),
        "source_end": download.site.end.isoformat(),
        "api_requests": download.requests,
        "downloaded_points": len(download.intervals),
        "deleted_states": replacement.deleted_states,
        "deleted_short_term_statistics": (replacement.deleted_short_term_statistics),
        "deleted_long_term_statistics": replacement.deleted_long_term_statistics,
        "imported_states": replacement.imported_states,
        "imported_long_term_statistics": (replacement.imported_long_term_statistics),
        "calibrated_value": calibrated_value,
    }


async def async_inspect_history(
    hass: HomeAssistant, call: ServiceCall
) -> ServiceResponse:
    """Download history for inspection without modifying recorder data."""
    api_key, site_id = _resolve_credentials(hass, call)
    granularity = DownloadGranularity(call.data[CONF_GRANULARITY])
    requested_start: date = call.data[CONF_START_DATE]
    download = await _async_download(
        api_key=api_key,
        site_id=site_id,
        granularity=granularity,
        start_date=requested_start,
    )
    return {
        "status": "success",
        "site_id": download.site.site_id,
        "site_name": download.site.name,
        "site_timezone": str(download.site.timezone),
        "granularity": granularity.value,
        "requested_start": requested_start.isoformat(),
        "data_period_start": download.site.start.isoformat(),
        "data_period_end": download.site.end.isoformat(),
        "source_start": download.intervals[0].start.date().isoformat(),
        "source_end": download.intervals[-1].end.date().isoformat(),
        "api_requests": download.requests,
        "downloaded_points": len(download.intervals),
    }


def _resolve_credentials(hass: HomeAssistant, call: ServiceCall) -> tuple[str, int]:
    """Use an explicit site ID and an action or YAML-resolved API key."""
    defaults = hass.data.get(DOMAIN, {}).get(DATA_CONFIG, {})
    api_key = call.data.get(CONF_API_KEY) or defaults.get(CONF_API_KEY)
    site_id = call.data[CONF_SITE_ID]
    if not api_key:
        raise ServiceValidationError(
            "SolarEdge API key is required by the action or integration YAML"
        )
    return api_key, int(site_id)


async def _async_download(
    *,
    api_key: str,
    site_id: int,
    granularity: DownloadGranularity,
    start_date: date | None = None,
):
    try:
        return await SolarEdgeHistoryDownloader(api_key, site_id).async_download(
            granularity, start_date=start_date
        )
    except HTTPStatusError as err:
        status = err.response.status_code
        if status in {401, 403}:
            message = "SolarEdge rejected the API key or the key cannot access the site"
        elif status == 404:
            message = f"SolarEdge site {site_id} does not exist or is not accessible"
        elif status == 429:
            message = "SolarEdge API request limit reached; retry later"
        else:
            message = f"SolarEdge API returned HTTP status {status}"
            detail = " ".join(err.response.text.split())
            if detail:
                message = f"{message}: {detail[:500]}"
        raise ServiceValidationError(message) from err
    except RequestError as err:
        raise HomeAssistantError("Unable to reach the SolarEdge API") from err
    except SolarEdgeDataError as err:
        raise HomeAssistantError(str(err)) from err
    except Exception as err:
        _LOGGER.error("Unexpected SolarEdge download failure (%s)", type(err).__name__)
        raise HomeAssistantError("Unexpected SolarEdge download failure") from err


def _validate_recorder_target(hass: HomeAssistant, target: TargetMeter) -> bool:
    """Check recorder readiness; return whether raw states should be written.

    Recorder-excluded targets (for example meters that keep only long-term
    statistics) get statistics replaced without importing raw states.
    """
    recorder = get_instance(hass)
    if not recorder.is_running or not recorder.async_db_ready.done():
        raise ServiceValidationError("Home Assistant recorder is not ready")
    return recorder.entity_filter is None or recorder.entity_filter(target.entity_id)


def _recorded_attributes(attributes: dict[str, Any]) -> dict[str, Any]:
    retained = {
        ATTR_DEVICE_CLASS,
        ATTR_FRIENDLY_NAME,
        ATTR_STATE_CLASS,
        ATTR_UNIT_OF_MEASUREMENT,
    }
    return {key: value for key, value in attributes.items() if key in retained}


def _target_lock(hass: HomeAssistant, entity_id: str) -> asyncio.Lock:
    domain_data = hass.data.setdefault(DOMAIN, {})
    locks: dict[str, asyncio.Lock] = domain_data.setdefault(DATA_LOCKS, {})
    return locks.setdefault(entity_id, asyncio.Lock())
