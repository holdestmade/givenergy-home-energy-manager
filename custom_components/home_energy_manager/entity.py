"""Shared base entity for Home Energy Manager."""

from __future__ import annotations

from homeassistant.const import CONF_HOST
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    CONF_DASHBOARD_PORT,
    DEFAULT_DASHBOARD_PORT,
    DOMAIN,
    MANUFACTURER,
    MODEL,
)
from .coordinator import HemCoordinator


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
        host = entry.data[CONF_HOST]
        dashboard_port = entry.options.get(CONF_DASHBOARD_PORT, DEFAULT_DASHBOARD_PORT)

        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            manufacturer=MANUFACTURER,
            model=MODEL,
            configuration_url=f"http://{host}:{dashboard_port}",
        )

    @property
    def status(self) -> dict:
        """Return the latest /api/control/status payload."""
        return self.coordinator.data.status if self.coordinator.data else {}

    @property
    def snapshot(self) -> dict:
        """Return the latest /api/snapshot payload."""
        return self.coordinator.data.snapshot if self.coordinator.data else {}
