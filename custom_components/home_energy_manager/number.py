"""Duration control for Home Energy Manager force actions.

This is a local setting, not an inverter register: it is the number of minutes
the switches ask for when they start a force action. It restores across
restarts and falls back to the value set in the integration's options.
"""

from __future__ import annotations

from homeassistant.components.number import NumberDeviceClass, NumberMode, RestoreNumber
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    CONF_ENABLE_CONTROLS,
    MAX_FORCE_MINUTES,
    MIN_FORCE_MINUTES,
)
from .coordinator import HemCoordinator
from .entity import HemEntity


def _clamp(value: float) -> int:
    """Return whole minutes inside the range HEM accepts.

    Home Assistant already bounds the number entity, but a value restored from
    an older install (or one written before these limits changed) arrives
    unchecked, and HEM rejects anything outside 1..1439 outright.
    """
    return max(MIN_FORCE_MINUTES, min(MAX_FORCE_MINUTES, int(value)))


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up the duration number if controls are enabled."""
    if not entry.options.get(CONF_ENABLE_CONTROLS, False):
        return

    coordinator: HemCoordinator = entry.runtime_data
    async_add_entities([HemForceMinutesNumber(coordinator)])


class HemForceMinutesNumber(HemEntity, RestoreNumber):
    """Minutes requested when a force action is started."""

    _attr_translation_key = "force_minutes"
    _attr_icon = "mdi:timer-cog-outline"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_device_class = NumberDeviceClass.DURATION
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES
    _attr_native_min_value = MIN_FORCE_MINUTES  # HEM rejects 0
    _attr_native_max_value = MAX_FORCE_MINUTES  # HEM rejects 1440+
    _attr_native_step = 1
    _attr_mode = NumberMode.BOX

    def __init__(self, coordinator: HemCoordinator) -> None:
        """Seed from the coordinator's current value."""
        super().__init__(coordinator, "force_minutes")
        self._attr_native_value = float(coordinator.force_minutes)

    async def async_added_to_hass(self) -> None:
        """Restore the last value the user set."""
        await super().async_added_to_hass()
        if (last := await self.async_get_last_number_data()) and last.native_value:
            minutes = _clamp(last.native_value)
            self._attr_native_value = float(minutes)
            self.coordinator.force_minutes = minutes

    async def async_set_native_value(self, value: float) -> None:
        """Store the new duration for the switches to use."""
        minutes = _clamp(value)
        self._attr_native_value = float(minutes)
        self.coordinator.force_minutes = minutes
        self.async_write_ha_state()
