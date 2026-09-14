"""Binary sensors for Home Energy Manager."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import ACTION_CHARGE, ACTION_DISCHARGE
from .coordinator import HemCoordinator, HemData, pick, quick_action_matches
from .entity import HemEntity


@dataclass(frozen=True, kw_only=True)
class HemBinarySensorDescription(BinarySensorEntityDescription):
    """Binary sensor description with a value function."""

    value_fn: Callable[[HemData], bool | None]
    # Snapshot-backed sensors are only created when their key actually resolves,
    # matching how the snapshot sensors in sensor.py are handled.
    from_snapshot: bool = False


BINARY_SENSORS: tuple[HemBinarySensorDescription, ...] = (
    HemBinarySensorDescription(
        key="connected",
        translation_key="connected",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        value_fn=lambda d: d.status.get("connection") == "connected",
    ),
    HemBinarySensorDescription(
        key="stale",
        translation_key="stale",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        # `stale` is about reading age, not the link state - they are separate.
        value_fn=lambda d: bool(d.status.get("stale")),
    ),
    HemBinarySensorDescription(
        key="status_unavailable",
        translation_key="status_unavailable",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        # ok:false means "do not treat this as current battery operation".
        value_fn=lambda d: not d.status.get("ok", False),
    ),
    HemBinarySensorDescription(
        key="has_conditions",
        translation_key="has_conditions",
        device_class=BinarySensorDeviceClass.PROBLEM,
        # An empty list in an ok:false response is not a clean bill of health,
        # so only report conditions when the status itself is usable.
        value_fn=lambda d: (
            bool(d.status.get("ok")) and bool(d.status.get("conditions"))
        ),
    ),
    HemBinarySensorDescription(
        key="force_charge_active",
        translation_key="force_charge_active",
        icon="mdi:battery-charging-high",
        value_fn=lambda d: quick_action_matches(d.status, ACTION_CHARGE),
    ),
    HemBinarySensorDescription(
        key="force_discharge_active",
        translation_key="force_discharge_active",
        icon="mdi:transmission-tower-export",
        value_fn=lambda d: quick_action_matches(d.status, ACTION_DISCHARGE),
    ),
    HemBinarySensorDescription(
        key="charge_schedule_active",
        translation_key="charge_schedule_active",
        icon="mdi:calendar-clock",
        value_fn=lambda d: pick(d.status, "schedules.charge") == "active",
    ),
    HemBinarySensorDescription(
        key="export_schedule_active",
        translation_key="export_schedule_active",
        icon="mdi:calendar-export",
        value_fn=lambda d: pick(d.status, "schedules.export") == "active",
    ),
    HemBinarySensorDescription(
        key="charging",
        translation_key="charging",
        device_class=BinarySensorDeviceClass.BATTERY_CHARGING,
        # Observed activity, not requested action.
        value_fn=lambda d: d.status.get("activity") == "charging",
    ),
    HemBinarySensorDescription(
        key="grid_online",
        translation_key="grid_online",
        from_snapshot=True,
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        # Deliberately None rather than False when the key is absent: claiming a
        # grid outage because the snapshot simply has not arrived would be worse
        # than reporting nothing. `grid_online` is about the supply, which is
        # not the same thing as the `connected` sensor's link to the inverter.
        value_fn=lambda d: (
            None
            if d.snapshot.get("grid_online") is None
            else bool(d.snapshot["grid_online"])
        ),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up the binary sensors."""
    coordinator: HemCoordinator = entry.runtime_data
    async_add_entities(
        HemBinarySensor(coordinator, description)
        for description in BINARY_SENSORS
        if not description.from_snapshot
        or description.value_fn(coordinator.data) is not None
    )


class HemBinarySensor(HemEntity, BinarySensorEntity):
    """A single boolean derived from the status or snapshot payload."""

    entity_description: HemBinarySensorDescription

    def __init__(
        self, coordinator: HemCoordinator, description: HemBinarySensorDescription
    ) -> None:
        """Store the description."""
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def is_on(self) -> bool | None:
        """Return the current state."""
        if self.coordinator.data is None:
            return None
        return self.entity_description.value_fn(self.coordinator.data)
