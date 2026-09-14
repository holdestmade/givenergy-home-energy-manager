"""Stop buttons for Home Energy Manager.

A stop is always worth having as its own control: HEM allows a recovery stop
for an action this integration started even after battery-control permission
has been switched off, and a switch that already believes it is off cannot be
turned off again.
"""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import ACTION_CHARGE, ACTION_DISCHARGE, CONF_ENABLE_CONTROLS
from .coordinator import HemCoordinator
from .entity import HemEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up the stop buttons if controls are enabled."""
    if not entry.options.get(CONF_ENABLE_CONTROLS, False):
        return

    coordinator: HemCoordinator = entry.runtime_data
    async_add_entities(
        [
            HemStopButton(coordinator, ACTION_CHARGE),
            HemStopButton(coordinator, ACTION_DISCHARGE),
        ]
    )


class HemStopButton(HemEntity, ButtonEntity):
    """Stop one direction of forced battery operation."""

    def __init__(self, coordinator: HemCoordinator, action: str) -> None:
        """Set up one direction."""
        super().__init__(coordinator, f"stop_force_{action}")
        self._action = action
        self._attr_translation_key = f"stop_force_{action}"
        self._attr_icon = "mdi:stop-circle-outline"

    async def async_press(self) -> None:
        """Send the stop."""
        await self.coordinator.async_stop(self._action)
