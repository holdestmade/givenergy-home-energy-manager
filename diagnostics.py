"""Diagnostics for Home Energy Manager.

Download this from the device page to see the exact /api/snapshot payload your
install returns - handy if a snapshot sensor is missing because its key name
differs from the candidates in sensor.py.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_API_KEY, CONF_HOST
from homeassistant.core import HomeAssistant

from .coordinator import HemCoordinator

TO_REDACT = {CONF_API_KEY, CONF_HOST}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator: HemCoordinator = entry.runtime_data
    data = coordinator.data

    return {
        "entry": {
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": dict(entry.options),
        },
        "last_command": {
            "id": coordinator.last_command_id,
            "action": coordinator.last_command_action,
            "state": coordinator.last_command_state,
        },
        "status": data.status if data else {},
        "snapshot": data.snapshot if data else {},
        "snapshot_keys": sorted(data.snapshot) if data and data.snapshot else [],
    }
