"""Force Charge / Force Discharge switches for Home Energy Manager.

These are only created when "Enable battery controls" is switched on in the
integration's options, which mirrors HEM's own default-off control permission.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import ACTION_CHARGE, ACTION_DISCHARGE, CONF_ENABLE_CONTROLS
from .coordinator import HemCoordinator, quick_action_matches
from .entity import HemEntity

_LOGGER = logging.getLogger(__name__)

# How long to trust our own optimistic state while the inverter is written to
# and re-read. Readback typically lands well inside this.
OPTIMISTIC_SECONDS = 180


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up the control switches if controls are enabled."""
    if not entry.options.get(CONF_ENABLE_CONTROLS, False):
        return

    coordinator: HemCoordinator = entry.runtime_data
    async_add_entities(
        [
            HemForceSwitch(coordinator, ACTION_CHARGE),
            HemForceSwitch(coordinator, ACTION_DISCHARGE),
        ]
    )


class HemForceSwitch(HemEntity, SwitchEntity):
    """Start or stop one direction of forced battery operation."""

    def __init__(self, coordinator: HemCoordinator, action: str) -> None:
        """Set up one direction."""
        super().__init__(coordinator, f"force_{action}")
        self._action = action
        self._attr_translation_key = f"force_{action}"
        self._attr_icon = (
            "mdi:battery-charging-high"
            if action == ACTION_CHARGE
            else "mdi:transmission-tower-export"
        )
        self._optimistic: bool | None = None
        self._optimistic_until: float = 0.0

    @property
    def is_on(self) -> bool | None:
        """Return whether HEM reports this force action as running."""
        if self._optimistic is not None and time.monotonic() < self._optimistic_until:
            return self._optimistic
        return quick_action_matches(self.status, self._action)

    @callback
    def _handle_coordinator_update(self) -> None:
        """Drop the optimistic value once HEM agrees, or once it has expired."""
        if self._optimistic is not None and (
            time.monotonic() >= self._optimistic_until
            or quick_action_matches(self.status, self._action) == self._optimistic
        ):
            self._optimistic = None
        super()._handle_coordinator_update()

    def _set_optimistic(self, value: bool) -> None:
        """Hold a value briefly: acceptance is not inverter confirmation."""
        self._optimistic = value
        # Monotonic, so a clock change cannot strand the switch on a stale value.
        self._optimistic_until = time.monotonic() + OPTIMISTIC_SECONDS
        self.async_write_ha_state()

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Start the force action for the configured duration."""
        minutes = self.coordinator.force_minutes
        await self.coordinator.async_force(self._action, minutes)
        _LOGGER.debug("Requested force %s for %s minutes", self._action, minutes)
        self._set_optimistic(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Stop the force action."""
        await self.coordinator.async_stop(self._action)
        self._set_optimistic(False)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose the duration that turning on would request."""
        return {"minutes": self.coordinator.force_minutes}
