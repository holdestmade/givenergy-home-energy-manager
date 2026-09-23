"""Native battery pause for Home Energy Manager.

One select rather than three switches, because the modes are exclusive: HEM
pauses charging, discharging or both, for the duration set on the duration
number, and a stop restores the settings it captured before the pause. Only
created when "Enable battery controls" is on, like the force switches.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_ENABLE_CONTROLS, OPTIMISTIC_SECONDS, PAUSE_MODES, PAUSE_OFF
from .coordinator import HemCoordinator, pause_mode_of
from .entity import HemEntity

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up the pause select if controls are enabled."""
    if not entry.options.get(CONF_ENABLE_CONTROLS, False):
        return

    coordinator: HemCoordinator = entry.runtime_data
    async_add_entities([HemPauseSelect(coordinator)])


class HemPauseSelect(HemEntity, SelectEntity):
    """Pause battery charging, discharging or both, or stop a pause."""

    _attr_translation_key = "pause_mode"
    _attr_icon = "mdi:battery-lock"

    def __init__(self, coordinator: HemCoordinator) -> None:
        """Set up the select."""
        super().__init__(coordinator, "pause_mode")
        self._optimistic: str | None = None
        self._optimistic_until: float = 0.0

    @property
    def _supported_modes(self) -> list[str]:
        """Return the modes HEM says this inverter supports.

        HEM reports null while it cannot tell, which offers every mode and
        lets HEM refuse one it cannot do.
        """
        reported = self.coordinator.capabilities.get("pause_modes")
        if not isinstance(reported, list):
            return list(PAUSE_MODES)
        return [mode for mode in PAUSE_MODES if mode in reported]

    @property
    def options(self) -> list[str]:
        """Return Off plus the supported pause modes."""
        return [PAUSE_OFF, *self._supported_modes]

    @property
    def available(self) -> bool:
        """Return False when HEM says this inverter supports no pause mode."""
        return super().available and bool(self._supported_modes)

    @property
    def current_option(self) -> str | None:
        """Return the running pause mode, or Off."""
        if self._optimistic is not None and time.monotonic() < self._optimistic_until:
            return self._optimistic
        return pause_mode_of(self.status) or PAUSE_OFF

    @callback
    def _handle_coordinator_update(self) -> None:
        """Drop the optimistic value once HEM agrees, or once it has expired."""
        if self._optimistic is not None and (
            time.monotonic() >= self._optimistic_until
            or (pause_mode_of(self.status) or PAUSE_OFF) == self._optimistic
        ):
            self._optimistic = None
        super()._handle_coordinator_update()

    async def async_select_option(self, option: str) -> None:
        """Start a pause in this mode, or stop the running one."""
        if option == PAUSE_OFF:
            await self.coordinator.async_stop_pause()
        else:
            minutes = self.coordinator.force_minutes
            await self.coordinator.async_pause(option, minutes)
            _LOGGER.debug("Requested battery pause %s for %s minutes", option, minutes)
        # Acceptance is not inverter confirmation, but the UI must not snap
        # back while the inverter is written to and re-read.
        self._optimistic = option
        self._optimistic_until = time.monotonic() + OPTIMISTIC_SECONDS
        self.async_write_ha_state()

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose the duration that starting a pause would request."""
        return {"minutes": self.coordinator.force_minutes}
