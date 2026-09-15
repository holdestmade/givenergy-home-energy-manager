"""Tests for the sensor helpers and platform."""

from __future__ import annotations

from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.home_energy_manager.sensor import (
    MAX_STATE_LENGTH,
    _as_float,
    _as_int,
    _as_text,
    _join_conditions,
    _pretty,
)

# --- coercion --------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [(1.5, 1.5), ("2.5", 2.5), (3, 3.0), ("-4", -4.0), (0, 0.0)],
)
def test_as_float_accepts_numbers_and_numeric_strings(
    value: Any, expected: float
) -> None:
    """HEM sometimes quotes its numbers."""
    assert _as_float(value) == expected


@pytest.mark.parametrize("value", [None, "abc", "", [], {}, True, False])
def test_as_float_rejects_everything_else(value: Any) -> None:
    """A bool is an int in Python, but never a meaningful reading here."""
    assert _as_float(value) is None


@pytest.mark.parametrize(
    ("value", "expected"), [(55, 55), (55.4, 55), (55.6, 56), ("55", 55), (-0.2, 0)]
)
def test_as_int_rounds_to_whole_numbers(value: Any, expected: int) -> None:
    """Percentages and counters are integral, so they must not render as 55.0."""
    assert _as_int(value) == expected


def test_as_int_passes_none_through() -> None:
    """A missing reading stays missing rather than becoming zero."""
    assert _as_int(None) is None


def test_as_text_truncates_to_the_state_limit() -> None:
    """Home Assistant rejects a state longer than 255 characters."""
    assert len(_as_text("x" * 500)) == MAX_STATE_LENGTH
    assert _as_text(None) is None
    assert _as_text(12) == "12"


# --- _pretty ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("connected", "Connected"),
        ("eco_paused", "Eco paused"),
        ("readback_confirmed", "Readback confirmed"),
        ("force-charge", "Force charge"),
        ("off", "Off"),
    ],
)
def test_pretty_sentence_cases_api_tokens(value: str, expected: str) -> None:
    """Underscores and hyphens both become spaces."""
    assert _pretty(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("battery_soc", "Battery SOC"),
        ("soc", "SOC"),
        ("pv_power", "PV power"),
        ("ct_reading", "CT reading"),
        ("hem", "HEM"),
        ("ac_dc_ev", "AC DC EV"),
    ],
)
def test_pretty_keeps_acronyms_upper(value: str, expected: str) -> None:
    """Sentence casing must not turn SOC into "Soc"."""
    assert _pretty(value) == expected


def test_pretty_leaves_existing_capitals_alone() -> None:
    """A proper noun from the API should survive untouched."""
    assert _pretty("GivEnergy_inverter") == "GivEnergy inverter"


@pytest.mark.parametrize("value", [None, "", "   ", "___", "-_-"])
def test_pretty_returns_none_for_nothing_to_show(value: Any) -> None:
    """Separators alone are not a state."""
    assert _pretty(value) is None


def test_pretty_handles_an_unrecognised_value() -> None:
    """The HEM docs ask for graceful handling of unknown enum values."""
    assert _pretty("some_brand_new_phase") == "Some brand new phase"


def test_pretty_truncates_to_the_state_limit() -> None:
    """Even a pathological value has to fit in a state."""
    assert len(_pretty("word_" * 200)) == MAX_STATE_LENGTH


def test_pretty_coerces_non_strings() -> None:
    """Numbers and bools arrive here too."""
    assert _pretty(42) == "42"
    assert _pretty(True) == "True"


# --- _join_conditions ------------------------------------------------------


def test_join_conditions_joins_the_requested_field() -> None:
    """Labels and codes come from the same list, formatted differently."""
    conditions = [
        {"code": "low_soc", "label": "Low state of charge"},
        {"code": "grid_export_limited", "label": "Grid export limited"},
    ]

    assert (
        _join_conditions(conditions, "label", "None")
        == "Low state of charge, Grid export limited"
    )
    assert (
        _join_conditions(conditions, "code", "none") == "low_soc, grid_export_limited"
    )


@pytest.mark.parametrize("conditions", [None, [], [{}], [{"code": "x"}]])
def test_join_conditions_falls_back_to_the_placeholder(conditions: Any) -> None:
    """An empty result reads as the caller's placeholder, not an empty string."""
    assert _join_conditions(conditions, "label", "None") == "None"


def test_join_conditions_drops_ragged_entries() -> None:
    """A malformed entry should disappear, not break the sensor."""
    conditions = ["junk", None, 42, {"label": ""}, {"label": "Real"}]

    assert _join_conditions(conditions, "label", "None") == "Real"


def test_join_conditions_stringifies_non_string_fields() -> None:
    """The value only has to be renderable."""
    assert _join_conditions([{"label": 7}], "label", "None") == "7"


# --- platform --------------------------------------------------------------


async def test_status_sensors_are_created(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """Status sensors do not depend on the snapshot payload."""
    assert hass.states.get("sensor.home_energy_manager_hem_local_summary") is not None


async def test_conditions_sensors_render_both_forms(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """Labels are prose, codes stay raw for automations."""
    labels = hass.states.get("sensor.home_energy_manager_hem_local_conditions")
    codes = hass.states.get("sensor.home_energy_manager_hem_local_condition_codes")

    assert labels.state == "Low state of charge, Grid export limited"
    assert codes.state == "low_soc, grid_export_limited"


async def test_snapshot_sensors_without_a_matching_key_are_skipped(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    aioclient_mock: Any,
) -> None:
    """/api/snapshot has no published field list, so absent keys mean no entity."""
    from .const import SNAPSHOT_URL, STATUS, STATUS_URL

    aioclient_mock.get(STATUS_URL, json=STATUS)
    aioclient_mock.get(SNAPSHOT_URL, json={})

    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    assert hass.states.get("sensor.home_energy_manager_hem_local_solar_power") is None
    # Status sensors are unaffected.
    assert hass.states.get("sensor.home_energy_manager_hem_local_summary") is not None
