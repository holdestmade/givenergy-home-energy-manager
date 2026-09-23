"""Stop buttons for Home Energy Manager.

A stop is always worth having as its own control: HEM allows a recovery stop
for an action this integration started even after battery-control permission
has been switched off, and a switch that already believes it is off cannot be
turned off again. For the same reason these are never made unavailable on
capability grounds.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

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
            HemStopButton(
                coordinator,
                f"stop_force_{ACTION_CHARGE}",
                lambda: coordinator.async_stop(ACTION_CHARGE),
            ),
            HemStopButton(
                coordinator,
                f"stop_force_{ACTION_DISCHARGE}",
                lambda: coordinator.async_stop(ACTION_DISCHARGE),
            ),
            HemStopButton(
                coordinator, "stop_pause", lambda: coordinator.async_stop_pause()
            ),
        ]
    )


class HemStopButton(HemEntity, ButtonEntity):
    """Stop one kind of forced or paused battery operation."""

    _attr_icon = "mdi:stop-circle-outline"

    def __init__(
        self,
        coordinator: HemCoordinator,
        key: str,
        stop: Callable[[], Awaitable[str | None]],
    ) -> None:
        """Set up one stop."""
        super().__init__(coordinator, key)
        self._attr_translation_key = key
        self._stop = stop

    async def async_press(self) -> None:
        """Send the stop."""
        await self._stop()
