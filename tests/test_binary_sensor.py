"""Tests for the binary sensors.

The value functions carry the integration's judgement calls about what counts
as a problem, so they are worth pinning down directly.
"""

from __future__ import annotations

from typing import Any

import pytest
from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.home_energy_manager.binary_sensor import BINARY_SENSORS
from custom_components.home_energy_manager.coordinator import HemData

BY_KEY = {description.key: description for description in BINARY_SENSORS}


def _value(key: str, status: dict[str, Any], snapshot: dict[str, Any] | None = None):
    """Run one description's value function over a payload."""
    return BY_KEY[key].value_fn(HemData(status=status, snapshot=snapshot or {}))


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ({"connection": "connected"}, True),
        ({"connection": "disconnected"}, False),
        ({}, False),
    ],
)
def test_connected(status: dict[str, Any], expected: bool) -> None:
    """Link state comes from the connection field alone."""
    assert _value("connected", status) is expected


@pytest.mark.parametrize(
    ("status", "expected"),
    [({"stale": True}, True), ({"stale": False}, False), ({}, False)],
)
def test_stale_is_about_reading_age(status: dict[str, Any], expected: bool) -> None:
    """Staleness is separate from the link being up."""
    assert _value("stale", status) is expected


@pytest.mark.parametrize(
    ("status", "expected"),
    [({"ok": True}, False), ({"ok": False}, True), ({}, True)],
)
def test_status_unavailable_defaults_to_a_problem(
    status: dict[str, Any], expected: bool
) -> None:
    """A payload with no ok flag is not evidence that everything is fine."""
    assert _value("status_unavailable", status) is expected


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ({"ok": True, "conditions": [{"code": "low_soc"}]}, True),
        ({"ok": True, "conditions": []}, False),
        # An empty list in an unusable response is not a clean bill of health.
        ({"ok": False, "conditions": []}, False),
        ({"ok": False, "conditions": [{"code": "low_soc"}]}, False),
        ({}, False),
    ],
)
def test_has_conditions_requires_a_usable_status(
    status: dict[str, Any], expected: bool
) -> None:
    """Conditions are only reported when the status itself is trustworthy."""
    assert _value("has_conditions", status) is expected


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ({"schedules": {"charge": "active"}}, True),
        ({"schedules": {"charge": "idle"}}, False),
        ({"schedules": {}}, False),
        ({}, False),
    ],
)
def test_charge_schedule(status: dict[str, Any], expected: bool) -> None:
    """Schedules live under their own nested object."""
    assert _value("charge_schedule_active", status) is expected


@pytest.mark.parametrize(
    ("status", "expected"),
    [({"activity": "charging"}, True), ({"activity": "idle"}, False), ({}, False)],
)
def test_charging_reports_observed_activity(
    status: dict[str, Any], expected: bool
) -> None:
    """This is what the battery is doing, not what was asked of it."""
    assert _value("charging", status) is expected


@pytest.mark.parametrize(
    ("snapshot", "expected"),
    [
        ({"grid_online": True}, True),
        ({"grid_online": False}, False),
        ({"grid_online": 1}, True),
        # Absent means unknown: claiming an outage would be worse.
        ({}, None),
        ({"grid_online": None}, None),
    ],
)
def test_grid_online_is_unknown_rather_than_false(
    snapshot: dict[str, Any], expected: bool | None
) -> None:
    """A missing snapshot key must not look like a power cut."""
    assert _value("grid_online", {}, snapshot) is expected


async def test_snapshot_backed_sensor_is_skipped_without_its_key(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """No key means no entity, rather than an entity stuck on unknown."""
    from .const import SNAPSHOT_URL, STATUS, STATUS_URL

    aioclient_mock.get(STATUS_URL, json=STATUS)
    aioclient_mock.get(SNAPSHOT_URL, json={"battery_soc": 55})

    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    assert (
        hass.states.get("binary_sensor.home_energy_manager_hem_local_grid_online")
        is None
    )


async def test_binary_sensors_reflect_the_payload(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """End to end: the payload reaches the states."""
    from .const import SNAPSHOT_URL, STATUS_URL

    aioclient_mock.get(
        STATUS_URL,
        json={
            "ok": True,
            "connection": "connected",
            "activity": "charging",
            "conditions": [{"code": "low_soc", "label": "Low state of charge"}],
        },
    )
    aioclient_mock.get(SNAPSHOT_URL, json={"grid_online": True})

    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    states = hass.states
    prefix = "binary_sensor.home_energy_manager_hem_local_"
    assert states.get(f"{prefix}inverter_online").state == STATE_ON
    assert states.get(f"{prefix}battery_charging").state == STATE_ON
    assert states.get(f"{prefix}conditions_present").state == STATE_ON
    assert states.get(f"{prefix}status_unavailable").state == STATE_OFF
    assert states.get(f"{prefix}grid_online").state == STATE_ON
    assert states.get(f"{prefix}force_charge_active").state == STATE_OFF
    assert states.get(f"{prefix}stale_reading").state != STATE_UNKNOWN
