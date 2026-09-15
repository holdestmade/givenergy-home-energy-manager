"""Tests for the force switches, especially their optimistic state."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.components.switch import DOMAIN as SWITCH_DOMAIN
from homeassistant.const import (
    ATTR_ENTITY_ID,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
    STATE_OFF,
    STATE_ON,
)
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.home_energy_manager.const import DOMAIN
from custom_components.home_energy_manager.switch import OPTIMISTIC_SECONDS

from .const import BASE_URL, ENTRY_DATA, HOST, PORT, SNAPSHOT, SNAPSHOT_URL, STATUS_URL

CHARGE = "switch.home_energy_manager_hem_local_force_charge"
DISCHARGE = "switch.home_energy_manager_hem_local_force_discharge"

IDLE_STATUS: dict[str, Any] = {"summary": "Idle", "control_source": "eco"}
CHARGING_STATUS: dict[str, Any] = {
    "summary": "Force charging",
    "quick_action": {"action": "force_charge", "phase": "active"},
}


@pytest.fixture
def status_payload() -> dict[str, Any]:
    """Return the status payload HEM answers with. Override this per test."""
    return IDLE_STATUS


@pytest.fixture
async def setup_switches(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    status_payload: dict[str, Any],
) -> MockConfigEntry:
    """Set up with readback confirmation off, so no background task runs."""
    aioclient_mock.get(STATUS_URL, json=status_payload)
    aioclient_mock.get(SNAPSHOT_URL, json=SNAPSHOT)
    aioclient_mock.post(
        f"{BASE_URL}/api/control/force-charge", json={"command_id": "abc-123"}
    )
    aioclient_mock.post(
        f"{BASE_URL}/api/control/force-charge/stop", json={"command_id": "def-456"}
    )
    aioclient_mock.post(
        f"{BASE_URL}/api/control/force-discharge", json={"command_id": "ghi-789"}
    )

    entry = MockConfigEntry(
        domain=DOMAIN,
        title=f"Home Energy Manager ({HOST})",
        data=ENTRY_DATA,
        options={
            "enable_controls": True,
            "poll_snapshot": True,
            "confirm_timeout": 0,
            "force_minutes": 45,
        },
        unique_id=f"{HOST}:{PORT}",
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def _turn(hass: HomeAssistant, service: str, entity_id: str) -> None:
    """Call a switch service and let it settle."""
    await hass.services.async_call(
        SWITCH_DOMAIN, service, {ATTR_ENTITY_ID: entity_id}, blocking=True
    )
    await hass.async_block_till_done()


async def test_state_follows_hem(
    hass: HomeAssistant, setup_switches: MockConfigEntry
) -> None:
    """With no optimistic value the switch is whatever HEM reports."""
    assert hass.states.get(CHARGE).state == STATE_OFF
    assert hass.states.get(DISCHARGE).state == STATE_OFF


@pytest.mark.parametrize("status_payload", [CHARGING_STATUS])
async def test_a_running_action_reads_as_on(
    hass: HomeAssistant, setup_switches: MockConfigEntry
) -> None:
    """A force charge owned by HEM turns only the charge switch on."""
    assert hass.states.get(CHARGE).state == STATE_ON
    assert hass.states.get(DISCHARGE).state == STATE_OFF


async def test_turning_on_sends_the_configured_duration(
    hass: HomeAssistant,
    setup_switches: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """The number entity's value is what gets requested."""
    await _turn(hass, SERVICE_TURN_ON, CHARGE)

    post = next(call for call in aioclient_mock.mock_calls if call[0] == "POST")
    assert post[2] == {"minutes": 45}


async def test_turning_on_is_optimistic(
    hass: HomeAssistant, setup_switches: MockConfigEntry
) -> None:
    """Acceptance is not confirmation, but the UI must not snap back."""
    await _turn(hass, SERVICE_TURN_ON, CHARGE)

    # HEM still reports idle, yet the switch shows on.
    assert hass.states.get(CHARGE).state == STATE_ON


async def test_the_optimistic_value_survives_a_poll_that_disagrees(
    hass: HomeAssistant, setup_switches: MockConfigEntry
) -> None:
    """The inverter is still being written to, so HEM lagging is expected."""
    await _turn(hass, SERVICE_TURN_ON, CHARGE)

    await setup_switches.runtime_data.async_refresh()
    await hass.async_block_till_done()

    assert hass.states.get(CHARGE).state == STATE_ON


async def test_the_optimistic_value_is_dropped_once_hem_agrees(
    hass: HomeAssistant,
    setup_switches: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Once readback lands the switch goes back to following HEM."""
    await _turn(hass, SERVICE_TURN_ON, CHARGE)

    aioclient_mock.clear_requests()
    aioclient_mock.get(STATUS_URL, json=CHARGING_STATUS)
    aioclient_mock.get(SNAPSHOT_URL, json=SNAPSHOT)
    await setup_switches.runtime_data.async_refresh()
    await hass.async_block_till_done()

    entity = hass.data["switch"].get_entity(CHARGE)
    assert entity._optimistic is None
    assert hass.states.get(CHARGE).state == STATE_ON


async def test_the_optimistic_value_expires(
    hass: HomeAssistant, setup_switches: MockConfigEntry
) -> None:
    """A command HEM never applies must not strand the switch on."""
    await _turn(hass, SERVICE_TURN_ON, CHARGE)
    assert hass.states.get(CHARGE).state == STATE_ON

    entity = hass.data["switch"].get_entity(CHARGE)
    with patch(
        "custom_components.home_energy_manager.switch.time.monotonic",
        return_value=entity._optimistic_until + 1,
    ):
        await setup_switches.runtime_data.async_refresh()
        await hass.async_block_till_done()

    assert entity._optimistic is None
    assert hass.states.get(CHARGE).state == STATE_OFF


async def test_the_optimistic_window_is_bounded(
    hass: HomeAssistant, setup_switches: MockConfigEntry
) -> None:
    """The hold is a fixed budget measured from the moment of the command."""
    with patch(
        "custom_components.home_energy_manager.switch.time.monotonic",
        return_value=1000.0,
    ):
        await _turn(hass, SERVICE_TURN_ON, CHARGE)

    entity = hass.data["switch"].get_entity(CHARGE)
    assert entity._optimistic_until == 1000.0 + OPTIMISTIC_SECONDS


@pytest.mark.parametrize("status_payload", [CHARGING_STATUS])
async def test_turning_off_is_optimistic_too(
    hass: HomeAssistant, setup_switches: MockConfigEntry
) -> None:
    """A stop shows immediately even while HEM still reports the action."""
    assert hass.states.get(CHARGE).state == STATE_ON

    await _turn(hass, SERVICE_TURN_OFF, CHARGE)

    assert hass.states.get(CHARGE).state == STATE_OFF


async def test_the_duration_is_exposed_as_an_attribute(
    hass: HomeAssistant, setup_switches: MockConfigEntry
) -> None:
    """Templates should be able to see what turning on would ask for."""
    assert hass.states.get(CHARGE).attributes["minutes"] == 45


async def test_switches_are_absent_when_controls_are_off(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_hem: AiohttpClientMocker
) -> None:
    """Controls are opt-in."""
    hass.config_entries.async_update_entry(
        config_entry, options={"enable_controls": False}
    )

    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    assert hass.states.get(CHARGE) is None
    assert hass.states.get(DISCHARGE) is None
