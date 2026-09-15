"""Sample payloads shared by the tests.

These mirror the shapes described in the HEM API docs. /api/snapshot has no
published field list, so SNAPSHOT is deliberately only a plausible subset:
tests that depend on exact snapshot keys say so locally.
"""

from __future__ import annotations

from typing import Any

from homeassistant.const import CONF_API_KEY, CONF_HOST, CONF_PORT, CONF_VERIFY_SSL

from custom_components.home_energy_manager.const import CONF_USE_SSL

HOST = "hem.local"
PORT = 7338
API_KEY = "test-api-key"

BASE_URL = f"http://{HOST}:{PORT}"
STATUS_URL = f"{BASE_URL}/api/control/status"
SNAPSHOT_URL = f"{BASE_URL}/api/snapshot"

ENTRY_DATA: dict[str, Any] = {
    CONF_HOST: HOST,
    CONF_PORT: PORT,
    CONF_API_KEY: API_KEY,
    CONF_USE_SSL: False,
    CONF_VERIFY_SSL: True,
}

STATUS: dict[str, Any] = {
    "summary": "Battery holding charge",
    "control_source": "eco",
    "control_phase": "active",
    "battery_soc": 55,
    "conditions": [
        {"code": "low_soc", "label": "Low state of charge"},
        {"code": "grid_export_limited", "label": "Grid export limited"},
    ],
    "automation": {"charging_mode": "eco_paused"},
}

SNAPSHOT: dict[str, Any] = {
    "battery_soc": 55,
    "battery_power": -1200,
    "grid_power": 340,
    "solar_power": 2100,
    "battery_temperature": 21.5,
}
