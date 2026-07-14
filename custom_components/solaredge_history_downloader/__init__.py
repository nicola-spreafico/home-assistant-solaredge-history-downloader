"""SolarEdge History Downloader integration."""

from __future__ import annotations

import voluptuous as vol
from homeassistant.core import HomeAssistant, SupportsResponse
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType

from .const import (
    CONF_API_KEY,
    DATA_CONFIG,
    DOMAIN,
    SERVICE_INSPECT_HISTORY,
    SERVICE_UPDATE_HISTORY,
)
from .service import (
    INSPECT_SERVICE_SCHEMA,
    SERVICE_SCHEMA,
    async_inspect_history,
    async_update_history,
)

CONFIG_SCHEMA = vol.Schema(
    {
        vol.Optional(DOMAIN): vol.Any(
            None,
            vol.Schema(
                {
                    vol.Optional(CONF_API_KEY): cv.string,
                }
            ),
        )
    },
    extra=vol.ALLOW_EXTRA,
)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the integration and register its action."""
    hass.data.setdefault(DOMAIN, {})[DATA_CONFIG] = config.get(DOMAIN) or {}

    async def async_handle_update_history(call):
        return await async_update_history(hass, call)

    async def async_handle_inspect_history(call):
        return await async_inspect_history(hass, call)

    hass.services.async_register(
        DOMAIN,
        SERVICE_UPDATE_HISTORY,
        async_handle_update_history,
        schema=SERVICE_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_INSPECT_HISTORY,
        async_handle_inspect_history,
        schema=INSPECT_SERVICE_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    return True
