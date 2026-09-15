"""Tests for the force-duration number."""

from __future__ import annotations

from typing import Any

import pytest
from homeassistant.components.number import (
    ATTR_VALUE,
    SERVICE_SET_VALUE,
)
from homeassistant.components.number import (
    DOMAIN as NUMBER_DOMAIN,
)
from homeassistant.const import ATTR_ENTITY_ID, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant, State
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    mock_restore_cache_with_extra_data,
)

from custom_components.home_energy_manager.const import (
    MAX_FORCE_MINUTES,
    MIN_FORCE_MINUTES,
)
from custom_components.home_energy_manager.number import _clamp

ENTITY_ID = "number.home_energy_manager_hem_local_force_action_duration"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (60, 60),
        (MIN_FORCE_MINUTES, MIN_FORCE_MINUTES),
        (MAX_FORCE_MINUTES, MAX_FORCE_MINUTES),
        (0, MIN_FORCE_MINUTES),
        (-30, MIN_FORCE_MINUTES),
        (MAX_FORCE_MINUTES + 1, MAX_FORCE_MINUTES),
        (99999, MAX_FORCE_MINUTES),
    ],
)
def test_clamp_holds_the_range_hem_accepts(value: float, expected: int) -> None:
    """HEM rejects anything outside 1..1439 outright."""
    assert _clamp(value) == expected


@pytest.mark.parametrize(("value", "expected"), [(60.7, 60), (1.9, 1), (1439.9, 1439)])
def test_clamp_returns_whole_minutes(value: float, expected: int) -> None:
    """The API takes an integer number of minutes."""
    result = _clamp(value)

    assert result == expected
    assert isinstance(result, int)


async def test_setting_the_value_updates_the_coordinator(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """The switches read their duration straight off the coordinator."""
    await hass.services.async_call(
        NUMBER_DOMAIN,
        SERVICE_SET_VALUE,
        {ATTR_ENTITY_ID: ENTITY_ID, ATTR_VALUE: 90},
        blocking=True,
    )

    assert hass.states.get(ENTITY_ID).state == "90.0"
    assert init_integration.runtime_data.force_minutes == 90


async def test_the_entity_advertises_the_api_limits(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """Home Assistant should refuse an out-of-range value before HEM has to."""
    state = hass.states.get(ENTITY_ID)

    assert state.attributes["min"] == MIN_FORCE_MINUTES
    assert state.attributes["max"] == MAX_FORCE_MINUTES


@pytest.mark.parametrize(
    ("restored", "expected"),
    [("30", 30), ("99999", MAX_FORCE_MINUTES), ("0.5", MIN_FORCE_MINUTES)],
)
async def test_a_restored_value_is_clamped(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_hem: Any,
    restored: str,
    expected: int,
) -> None:
    """A value saved before these limits existed must not reach HEM unchecked."""
    mock_restore_cache_with_extra_data(
        hass,
        (
            (
                State(ENTITY_ID, restored),
                {
                    "native_max_value": 99999,
                    "native_min_value": 0,
                    "native_step": 1,
                    "native_unit_of_measurement": "min",
                    "native_value": float(restored),
                },
            ),
        ),
    )

    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    assert config_entry.runtime_data.force_minutes == expected
    assert hass.states.get(ENTITY_ID).state == str(float(expected))


async def test_the_number_is_absent_when_controls_are_off(
    hass: HomeAssistant, mock_hem: Any
) -> None:
    """Controls are opt-in, mirroring HEM's own default-off permission."""
    from custom_components.home_energy_manager.const import DOMAIN

    from .const import ENTRY_DATA

    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Home Energy Manager (hem.local)",
        data=ENTRY_DATA,
        options={"enable_controls": False},
    )
    entry.add_to_hass(hass)

    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert hass.states.get(ENTITY_ID) in (None, STATE_UNAVAILABLE)
