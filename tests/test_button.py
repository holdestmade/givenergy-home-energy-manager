"""Tests for the stop buttons."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.components.button import DOMAIN as BUTTON_DOMAIN
from homeassistant.components.button import SERVICE_PRESS
from homeassistant.const import ATTR_ENTITY_ID, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.home_energy_manager.const import ACTION_CHARGE, ACTION_DISCHARGE

CHARGE = "button.home_energy_manager_hem_local_stop_force_charge"
DISCHARGE = "button.home_energy_manager_hem_local_stop_force_discharge"
PAUSE = "button.home_energy_manager_hem_local_stop_battery_pause"


@pytest.mark.parametrize(
    ("entity_id", "action"), [(CHARGE, ACTION_CHARGE), (DISCHARGE, ACTION_DISCHARGE)]
)
async def test_pressing_sends_a_stop(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    entity_id: str,
    action: str,
) -> None:
    """A stop is worth its own control: a switch already off cannot be turned off."""
    with patch.object(init_integration.runtime_data, "async_stop", AsyncMock()) as stop:
        await hass.services.async_call(
            BUTTON_DOMAIN, SERVICE_PRESS, {ATTR_ENTITY_ID: entity_id}, blocking=True
        )

    stop.assert_awaited_once_with(action)


async def test_pressing_stop_battery_pause_stops_the_pause(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """The pause has its own stop, for the same recovery reasons."""
    with patch.object(
        init_integration.runtime_data, "async_stop_pause", AsyncMock()
    ) as stop:
        await hass.services.async_call(
            BUTTON_DOMAIN, SERVICE_PRESS, {ATTR_ENTITY_ID: PAUSE}, blocking=True
        )

    stop.assert_awaited_once_with()


async def test_stops_stay_available_whatever_hem_says_is_supported(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """A stop is how a running action gets undone, so it is never gated."""
    coordinator = init_integration.runtime_data
    coordinator.capabilities = {
        "force_charge": False,
        "force_discharge": False,
        "pause_modes": [],
    }
    coordinator.async_update_listeners()
    await hass.async_block_till_done()

    for entity_id in (CHARGE, DISCHARGE, PAUSE):
        assert hass.states.get(entity_id).state != STATE_UNAVAILABLE


async def test_buttons_are_absent_when_controls_are_off(
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
    assert hass.states.get(PAUSE) is None
