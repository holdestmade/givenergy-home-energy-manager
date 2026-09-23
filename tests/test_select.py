"""Tests for the native battery pause select."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.components.select import (
    ATTR_OPTION,
    ATTR_OPTIONS,
    SERVICE_SELECT_OPTION,
)
from homeassistant.components.select import DOMAIN as SELECT_DOMAIN
from homeassistant.const import ATTR_ENTITY_ID, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.home_energy_manager.const import DOMAIN

from .const import BASE_URL, ENTRY_DATA, HOST, PORT, SNAPSHOT, SNAPSHOT_URL, STATUS_URL

ENTITY_ID = "select.home_energy_manager_hem_local_battery_pause"
PAUSE_URL = f"{BASE_URL}/api/control/pause-mode"

IDLE: dict[str, Any] = {"summary": "Idle", "control_source": "inverter"}
PAUSED_BOTH: dict[str, Any] = {
    "summary": "Battery Pause",
    "quick_action": {"action": "pause_mode", "mode": 3, "phase": "active"},
}


@pytest.fixture
def status_payload() -> dict[str, Any]:
    """Return the status payload HEM answers with. Override this per test."""
    return IDLE


@pytest.fixture
async def setup_select(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    status_payload: dict[str, Any],
) -> MockConfigEntry:
    """Set up with readback confirmation off, so no background task runs."""
    aioclient_mock.get(STATUS_URL, json=status_payload)
    aioclient_mock.get(SNAPSHOT_URL, json=SNAPSHOT)
    aioclient_mock.post(PAUSE_URL, json={"ok": True, "command_id": "p-1"})
    aioclient_mock.post(f"{PAUSE_URL}/stop", json={"ok": True, "command_id": "s-1"})

    entry = MockConfigEntry(
        domain=DOMAIN,
        title=f"Home Energy Manager ({HOST})",
        data=ENTRY_DATA,
        options={"enable_controls": True, "confirm_timeout": 0},
        unique_id=f"{HOST}:{PORT}",
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def _select(hass: HomeAssistant, option: str) -> None:
    await hass.services.async_call(
        SELECT_DOMAIN,
        SERVICE_SELECT_OPTION,
        {ATTR_ENTITY_ID: ENTITY_ID, ATTR_OPTION: option},
        blocking=True,
    )
    await hass.async_block_till_done()


def _posts(aioclient_mock: AiohttpClientMocker) -> list[tuple[str, Any]]:
    return [(str(c[1]), c[2]) for c in aioclient_mock.mock_calls if c[0] == "POST"]


async def test_idle_reads_as_off(
    hass: HomeAssistant, setup_select: MockConfigEntry
) -> None:
    """No pause running, and every mode offered while HEM has not said."""
    state = hass.states.get(ENTITY_ID)

    assert state.state == "off"
    assert state.attributes[ATTR_OPTIONS] == ["off", "charge", "discharge", "both"]


@pytest.mark.parametrize("status_payload", [PAUSED_BOTH])
async def test_a_running_pause_shows_its_mode(
    hass: HomeAssistant, setup_select: MockConfigEntry
) -> None:
    """HEM's register value 3 is "both"."""
    assert hass.states.get(ENTITY_ID).state == "both"


async def test_choosing_a_mode_starts_a_pause_for_the_duration(
    hass: HomeAssistant,
    setup_select: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """The duration number's value is what gets requested, and it shows at once."""
    setup_select.runtime_data.force_minutes = 25

    await _select(hass, "discharge")

    assert _posts(aioclient_mock) == [(PAUSE_URL, {"mode": "discharge", "minutes": 25})]
    assert hass.states.get(ENTITY_ID).state == "discharge"
    assert setup_select.runtime_data.last_command_action == "pause_discharge"


@pytest.mark.parametrize("status_payload", [PAUSED_BOTH])
async def test_choosing_off_stops_the_pause(
    hass: HomeAssistant,
    setup_select: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Off is a stop, which restores what HEM captured before the pause."""
    await _select(hass, "off")

    assert _posts(aioclient_mock) == [(f"{PAUSE_URL}/stop", None)]
    assert hass.states.get(ENTITY_ID).state == "off"


async def test_the_optimistic_mode_expires(
    hass: HomeAssistant, setup_select: MockConfigEntry
) -> None:
    """A pause HEM never applies must not strand the select on it."""
    await _select(hass, "charge")
    entity = hass.data[SELECT_DOMAIN].get_entity(ENTITY_ID)

    with patch(
        "custom_components.home_energy_manager.select.time.monotonic",
        return_value=entity._optimistic_until + 1,
    ):
        await setup_select.runtime_data.async_refresh()
        await hass.async_block_till_done()

    assert hass.states.get(ENTITY_ID).state == "off"


@pytest.mark.parametrize(
    "status_payload",
    [{**IDLE, "control_capabilities": {"pause_modes": ["charge", "both"]}}],
)
async def test_only_supported_modes_are_offered(
    hass: HomeAssistant, setup_select: MockConfigEntry
) -> None:
    """Once HEM says which modes this inverter supports, offer just those."""
    assert hass.states.get(ENTITY_ID).attributes[ATTR_OPTIONS] == [
        "off",
        "charge",
        "both",
    ]


@pytest.mark.parametrize(
    "status_payload", [{**IDLE, "control_capabilities": {"pause_modes": []}}]
)
async def test_an_inverter_without_pause_makes_it_unavailable(
    hass: HomeAssistant, setup_select: MockConfigEntry
) -> None:
    """No supported mode means nothing to choose."""
    assert hass.states.get(ENTITY_ID).state == STATE_UNAVAILABLE


async def test_a_refused_pause_reports_hem_reason(
    hass: HomeAssistant,
    setup_select: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """A force action running blocks a pause; the message must say so."""
    aioclient_mock.clear_requests()
    aioclient_mock.get(STATUS_URL, json=IDLE)
    aioclient_mock.post(
        PAUSE_URL,
        status=409,
        json={
            "ok": False,
            "code": "control_conflict",
            "error": "Another battery control action is active",
        },
    )

    with pytest.raises(HomeAssistantError) as caught:
        await _select(hass, "charge")

    assert str(caught.value) == (
        "HEM refused to pause battery charging because another battery action is "
        "running, the same action already is, or HEM has no inverter reading yet: "
        "Another battery control action is active"
    )
    assert hass.states.get(ENTITY_ID).state == "off"


async def test_the_select_is_absent_when_controls_are_off(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_hem: AiohttpClientMocker
) -> None:
    """Controls are opt-in."""
    hass.config_entries.async_update_entry(
        config_entry, options={"enable_controls": False}
    )

    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    assert hass.states.get(ENTITY_ID) is None
