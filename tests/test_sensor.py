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


async def test_snapshot_sensors_are_added_once_their_key_turns_up(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    aioclient_mock: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """HEM has no reading when it has just started, so wait for the keys.

    Deciding once at setup left every snapshot sensor missing until a reload.
    """
    from .const import SNAPSHOT, SNAPSHOT_URL, STATUS, STATUS_URL

    aioclient_mock.get(STATUS_URL, json=STATUS)
    aioclient_mock.get(
        SNAPSHOT_URL, json={"ok": False, "error": "No inverter data available yet"}
    )
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    battery = "sensor.home_energy_manager_hem_local_battery"
    assert hass.states.get(battery) is None

    aioclient_mock.clear_requests()
    aioclient_mock.get(STATUS_URL, json=STATUS)
    aioclient_mock.get(SNAPSHOT_URL, json=SNAPSHOT)
    await config_entry.runtime_data.async_refresh()
    await hass.async_block_till_done()

    assert hass.states.get(battery).state == "55"
    # Keys HEM still does not report stay absent rather than stuck on unknown.
    assert hass.states.get("sensor.home_energy_manager_hem_local_home_power") is None

    # A later poll must not add the same entity again, which Home Assistant
    # would reject as a duplicate unique id.
    await config_entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert "already exists" not in caplog.text


@pytest.mark.parametrize(("value", "expected"), [(56, 56), (56.0, 56), (None, None)])
def test_window_remaining_is_whole_minutes(value: Any, expected: int | None) -> None:
    """HEM rounds the window up to whole minutes, so do not show "56.0"."""
    from custom_components.home_energy_manager.coordinator import HemData
    from custom_components.home_energy_manager.sensor import STATUS_SENSORS

    description = next(d for d in STATUS_SENSORS if d.key == "remaining_minutes")
    result = description.value_fn(HemData(status={"remaining_minutes": value}))

    assert result == expected
    assert result is None or isinstance(result, int)


# --- sensors added from data HEM already sends --------------------------------

# Shaped like HEM v0.84.0's real /api/snapshot and /api/control/status.
FULL_SNAPSHOT: dict[str, Any] = {
    "ok": True,
    "soc": 64,
    "battery_state": "charging",
    "solar_power": 2100,
    "battery_power": -1500,
    "grid_power": 300,
    "home_power": 900,
    "grid_online": True,
    "age_seconds": 4,
}
FULL_STATUS: dict[str, Any] = {
    "ok": True,
    "summary": "Eco — idle",
    "stale_after_seconds": 60,
    "automation": {
        "charging_mode": "adaptive",
        "cosy": {"enabled": False, "active": False, "phase": "off"},
        "agile": {"enabled": True, "active": False, "phase": "waiting"},
        "adaptive": {"enabled": True, "phase": "charging", "period": 2},
    },
    "calibration": {"supported": True, "stage": 0, "phase": "off"},
    "maintenance": {"mode": 0, "phase": "standby"},
    "limits": {"reserve_soc": 4, "battery_power_cutoff_percent": None},
}
PREFIX = "sensor.home_energy_manager_hem_local_"


async def _setup(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    aioclient_mock: Any,
    status: dict[str, Any],
    snapshot: dict[str, Any],
) -> None:
    from .const import SNAPSHOT_URL, STATUS_URL

    aioclient_mock.get(STATUS_URL, json=status)
    aioclient_mock.get(SNAPSHOT_URL, json=snapshot)
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()


async def test_the_new_sensors_read_the_real_payload_shapes(
    hass: HomeAssistant, config_entry: MockConfigEntry, aioclient_mock: Any
) -> None:
    """Every new sensor, end to end from HEM's actual field names."""
    await _setup(hass, config_entry, aioclient_mock, FULL_STATUS, FULL_SNAPSHOT)

    expected = {
        "battery_state": "Charging",
        "cosy_phase": "Off",
        "agile_phase": "Waiting",
        "adaptive_charge_phase": "Charging",
        "calibration_phase": "Off",
        "maintenance_phase": "Standby",
        # HEM: grid +300 W is exporting; battery -1500 W is charging.
        "grid_import_power": "0.0",
        "grid_export_power": "300.0",
        "battery_charge_power": "1500.0",
        "battery_discharge_power": "0.0",
    }
    for suffix, state in expected.items():
        assert hass.states.get(f"{PREFIX}{suffix}").state == state, suffix


@pytest.mark.parametrize(
    ("grid", "battery", "imported", "exported", "charging", "discharging"),
    [
        (-2400, 800, 2400.0, 0.0, 0.0, 800.0),
        (0, 0, 0.0, 0.0, 0.0, 0.0),
        ("-12.5", "3", 12.5, 0.0, 0.0, 3.0),
    ],
)
def test_the_split_power_sensors_only_ever_read_positive(
    grid: Any,
    battery: Any,
    imported: float,
    exported: float,
    charging: float,
    discharging: float,
) -> None:
    """HA's grid convention is import-positive; HEM's is export-positive."""
    from custom_components.home_energy_manager.coordinator import HemData
    from custom_components.home_energy_manager.sensor import SNAPSHOT_SENSORS

    data = HemData(snapshot={"grid_power": grid, "battery_power": battery})
    value = {d.key: d.value_fn(data) for d in SNAPSHOT_SENSORS}

    assert value["grid_import_power"] == imported
    assert value["grid_export_power"] == exported
    assert value["battery_charge_power"] == charging
    assert value["battery_discharge_power"] == discharging


def test_the_split_power_sensors_need_their_source() -> None:
    """No grid reading means no split sensor either, not a 0 W one."""
    from custom_components.home_energy_manager.coordinator import HemData
    from custom_components.home_energy_manager.sensor import SNAPSHOT_SENSORS

    data = HemData(snapshot={"soc": 50})
    for key in ("grid_import_power", "grid_export_power", "battery_charge_power"):
        description = next(d for d in SNAPSHOT_SENSORS if d.key == key)
        assert description.value_fn(data) is None


async def test_the_power_cutoff_only_exists_where_hem_reports_it(
    hass: HomeAssistant, config_entry: MockConfigEntry, aioclient_mock: Any
) -> None:
    """Three-phase models only: elsewhere HEM sends null, so no entity."""
    from .const import SNAPSHOT_URL, STATUS_URL

    await _setup(hass, config_entry, aioclient_mock, FULL_STATUS, FULL_SNAPSHOT)
    assert hass.states.get(f"{PREFIX}battery_power_cutoff") is None

    aioclient_mock.clear_requests()
    aioclient_mock.get(
        STATUS_URL,
        json={
            **FULL_STATUS,
            "limits": {"reserve_soc": 4, "battery_power_cutoff_percent": 20},
        },
    )
    aioclient_mock.get(SNAPSHOT_URL, json=FULL_SNAPSHOT)
    await config_entry.runtime_data.async_refresh()
    await hass.async_block_till_done()

    assert hass.states.get(f"{PREFIX}battery_power_cutoff").state == "20"


async def test_stale_snapshot_readings_go_unavailable(
    hass: HomeAssistant, config_entry: MockConfigEntry, aioclient_mock: Any
) -> None:
    """HEM keeps serving its last reading after it loses the inverter.

    Showing ten-minute-old power flows as current would be wrong, but the
    snapshot age sensor is the one that says why, so it stays.
    """
    from homeassistant.const import STATE_UNAVAILABLE

    await _setup(
        hass,
        config_entry,
        aioclient_mock,
        FULL_STATUS,
        {**FULL_SNAPSHOT, "age_seconds": 600},
    )

    for suffix in ("battery", "solar_power", "grid_import_power", "battery_state"):
        assert hass.states.get(f"{PREFIX}{suffix}").state == STATE_UNAVAILABLE, suffix
    assert int(hass.states.get(f"{PREFIX}snapshot_age").state) >= 600
    # Status sensors are not affected.
    assert hass.states.get(f"{PREFIX}summary").state == "Eco — idle"


async def test_a_fresh_reading_brings_them_back(
    hass: HomeAssistant, config_entry: MockConfigEntry, aioclient_mock: Any
) -> None:
    """Staleness is judged on every update, not once."""
    from .const import SNAPSHOT_URL, STATUS_URL

    await _setup(
        hass,
        config_entry,
        aioclient_mock,
        FULL_STATUS,
        {**FULL_SNAPSHOT, "age_seconds": 600},
    )
    aioclient_mock.clear_requests()
    aioclient_mock.get(STATUS_URL, json=FULL_STATUS)
    aioclient_mock.get(SNAPSHOT_URL, json=FULL_SNAPSHOT)
    await config_entry.runtime_data.async_refresh()
    await hass.async_block_till_done()

    assert hass.states.get(f"{PREFIX}battery").state == "64"
