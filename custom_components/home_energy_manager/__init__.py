"""The Home Energy Manager integration."""

from __future__ import annotations

import logging

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    CONF_API_KEY,
    CONF_HOST,
    CONF_PORT,
    CONF_VERIFY_SSL,
    Platform,
)
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.typing import ConfigType

from .api import HomeEnergyManagerApi
from .const import (
    ACTION_CHARGE,
    ACTION_DISCHARGE,
    ATTR_CONFIG_ENTRY_ID,
    ATTR_MINUTES,
    CONF_USE_SSL,
    DOMAIN,
    MAX_FORCE_MINUTES,
    MIN_FORCE_MINUTES,
    SERVICE_FORCE_CHARGE,
    SERVICE_FORCE_DISCHARGE,
    SERVICE_STOP_FORCE_CHARGE,
    SERVICE_STOP_FORCE_DISCHARGE,
)
from .coordinator import HemCoordinator

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.NUMBER,
    Platform.SENSOR,
    Platform.SWITCH,
]

_ENTRY_SCHEMA = {vol.Required(ATTR_CONFIG_ENTRY_ID): cv.string}
_MINUTES_SCHEMA = {
    vol.Required(ATTR_MINUTES): vol.All(
        vol.Coerce(int), vol.Range(min=MIN_FORCE_MINUTES, max=MAX_FORCE_MINUTES)
    )
}


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register services once, regardless of how many entries exist."""
    _async_register_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Home Energy Manager from a config entry."""
    session = async_get_clientsession(
        hass, verify_ssl=entry.data.get(CONF_VERIFY_SSL, True)
    )
    api = HomeEnergyManagerApi(
        session,
        host=entry.data[CONF_HOST],
        port=entry.data[CONF_PORT],
        api_key=entry.data[CONF_API_KEY],
        use_ssl=entry.data.get(CONF_USE_SSL, False),
    )

    coordinator = HemCoordinator(hass, entry, api)
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload when options change (poll interval, controls, snapshot polling)."""
    await hass.config_entries.async_reload(entry.entry_id)


def _async_register_services(hass: HomeAssistant) -> None:
    """Register the four control services."""
    if hass.services.has_service(DOMAIN, SERVICE_FORCE_CHARGE):
        return

    def _coordinator(call: ServiceCall) -> HemCoordinator:
        entry_id = call.data[ATTR_CONFIG_ENTRY_ID]
        entry = hass.config_entries.async_get_entry(entry_id)
        if entry is None or entry.domain != DOMAIN:
            raise ServiceValidationError(
                f"Unknown Home Energy Manager entry: {entry_id}"
            )
        coordinator = getattr(entry, "runtime_data", None)
        if coordinator is None:
            raise ServiceValidationError(f"Entry {entry.title} is not loaded")
        return coordinator

    async def _force_charge(call: ServiceCall) -> None:
        await _coordinator(call).async_force(ACTION_CHARGE, call.data[ATTR_MINUTES])

    async def _force_discharge(call: ServiceCall) -> None:
        await _coordinator(call).async_force(ACTION_DISCHARGE, call.data[ATTR_MINUTES])

    async def _stop_charge(call: ServiceCall) -> None:
        await _coordinator(call).async_stop(ACTION_CHARGE)

    async def _stop_discharge(call: ServiceCall) -> None:
        await _coordinator(call).async_stop(ACTION_DISCHARGE)

    hass.services.async_register(
        DOMAIN,
        SERVICE_FORCE_CHARGE,
        _force_charge,
        schema=vol.Schema({**_ENTRY_SCHEMA, **_MINUTES_SCHEMA}),
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_FORCE_DISCHARGE,
        _force_discharge,
        schema=vol.Schema({**_ENTRY_SCHEMA, **_MINUTES_SCHEMA}),
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_STOP_FORCE_CHARGE,
        _stop_charge,
        schema=vol.Schema(_ENTRY_SCHEMA),
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_STOP_FORCE_DISCHARGE,
        _stop_discharge,
        schema=vol.Schema(_ENTRY_SCHEMA),
    )
