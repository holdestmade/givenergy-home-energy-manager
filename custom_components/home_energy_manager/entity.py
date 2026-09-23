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


class ReportedDescription(Protocol):
    """The parts of an entity description that decide whether it exists."""

    key: str
    value_fn: Callable[[HemData], Any]


@callback
def async_add_entities_when_reported(
    entry: ConfigEntry,
    coordinator: HemCoordinator,
    descriptions: Iterable[ReportedDescription],
    factory: Callable[[Any], Entity],
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Add each entity the first time HEM reports a value for it.

    Used for everything HEM may simply not report: /api/snapshot has no
    published field list, and some status fields only exist on some inverter
    models. That cannot be decided once at setup: HEM answers {"ok": false}
    with no readings until it has its first inverter reading, which is exactly
    the state it is in when HEM and Home Assistant start together, and deciding
    then left every snapshot entity missing until the entry was reloaded.
    """
    pending = list(descriptions)

    @callback
    def _async_add_reported() -> None:
        if coordinator.data is None:
            return
        reported = [d for d in pending if d.value_fn(coordinator.data) is not None]
        if not reported:
            return
        for description in reported:
            pending.remove(description)
        async_add_entities(factory(description) for description in reported)

    _async_add_reported()
    if pending:
        entry.async_on_unload(coordinator.async_add_listener(_async_add_reported))


class HemEntity(CoordinatorEntity[HemCoordinator]):
    """Base entity: one HEM install is one device."""

    _attr_has_entity_name = True
    # Snapshot readings go unavailable once HEM's reading is older than its
    # own stale limit, rather than showing old power flows as current.
    _goes_stale = False

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
    def available(self) -> bool:
        """Return False when polling fails, or this reading has gone stale."""
        if not super().available:
            return False
        data = self.coordinator.data
        return not (self._goes_stale and data is not None and data.snapshot_stale)

    @property
    def status(self) -> dict:
        """Return the latest /api/control/status payload."""
        return self.coordinator.data.status if self.coordinator.data else {}

    @property
    def snapshot(self) -> dict:
        """Return the latest /api/snapshot payload."""
        return self.coordinator.data.snapshot if self.coordinator.data else {}
