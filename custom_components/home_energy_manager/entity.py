"""Shared base entity for Home Energy Manager."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any, Protocol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST
from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import build_url
from .const import (
    CONF_DASHBOARD_PORT,
    CONF_USE_SSL,
    DEFAULT_DASHBOARD_PORT,
    DOMAIN,
    MANUFACTURER,
    MODEL,
)
from .coordinator import HemCoordinator, HemData


class SnapshotDescription(Protocol):
    """The parts of an entity description that decide whether it exists."""

    key: str
    value_fn: Callable[[HemData], Any]


@callback
def async_add_snapshot_entities(
    entry: ConfigEntry,
    coordinator: HemCoordinator,
    descriptions: Iterable[SnapshotDescription],
    factory: Callable[[Any], Entity],
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Add each snapshot-backed entity the first time its key resolves.

    /api/snapshot has no published field list, so an entity is only created
    once HEM actually reports its key. That cannot be decided once at setup:
    HEM answers {"ok": false} with no readings until it has its first
    inverter reading, which is exactly the state it is in when HEM and Home
    Assistant start together, and deciding then left every snapshot entity
    missing until the entry was reloaded.
    """
    pending = list(descriptions)

    @callback
    def _async_add_resolved() -> None:
        if coordinator.data is None:
            return
        resolved = [d for d in pending if d.value_fn(coordinator.data) is not None]
        if not resolved:
            return
        for description in resolved:
            pending.remove(description)
        async_add_entities(factory(description) for description in resolved)

    _async_add_resolved()
    if pending:
        entry.async_on_unload(coordinator.async_add_listener(_async_add_resolved))


class HemEntity(CoordinatorEntity[HemCoordinator]):
    """Base entity: one HEM install is one device."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: HemCoordinator, key: str) -> None:
        """Bind the entity to the entry's device."""
        super().__init__(coordinator)
        entry = coordinator.entry
        self._attr_unique_id = f"{entry.entry_id}_{key}"

        # The control API lives on its own port; link the device to the main
        # dashboard instead, since that is the page a user actually wants.
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            manufacturer=MANUFACTURER,
            model=MODEL,
            configuration_url=build_url(
                entry.data[CONF_HOST],
                entry.options.get(CONF_DASHBOARD_PORT, DEFAULT_DASHBOARD_PORT),
                entry.data.get(CONF_USE_SSL, False),
            ),
        )

    @property
    def status(self) -> dict:
        """Return the latest /api/control/status payload."""
        return self.coordinator.data.status if self.coordinator.data else {}

    @property
    def snapshot(self) -> dict:
        """Return the latest /api/snapshot payload."""
        return self.coordinator.data.snapshot if self.coordinator.data else {}
