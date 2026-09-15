"""Tests for the config, reauth, reconfigure and options flows."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant import config_entries
from homeassistant.const import (
    CONF_API_KEY,
    CONF_HOST,
    CONF_PORT,
    CONF_SCAN_INTERVAL,
)
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
)

from custom_components.home_energy_manager.api import (
    HemAuthError,
    HemConnectionError,
)
from custom_components.home_energy_manager.const import (
    CONF_CONFIRM_TIMEOUT,
    CONF_DASHBOARD_PORT,
    CONF_FORCE_MINUTES,
    DOMAIN,
)

from .const import API_KEY, ENTRY_DATA, HOST, PORT, STATUS, STATUS_URL

VALIDATE = "custom_components.home_energy_manager.config_flow._async_validate"

USER_INPUT = {CONF_HOST: HOST, CONF_PORT: PORT, CONF_API_KEY: API_KEY}


async def test_user_flow_creates_an_entry(
    hass: HomeAssistant, mock_setup_entry: AsyncMock
) -> None:
    """A reachable HEM with a good key becomes an entry."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    with patch(VALIDATE):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], USER_INPUT
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == f"Home Energy Manager ({HOST})"
    assert result["data"][CONF_HOST] == HOST
    assert result["data"][CONF_PORT] == PORT
    assert result["result"].unique_id == f"{HOST}:{PORT}"


async def test_the_port_is_stored_as_an_int(
    hass: HomeAssistant, mock_setup_entry: AsyncMock
) -> None:
    """The number selector hands back a float, which would break the unique id."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )

    with patch(VALIDATE):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {**USER_INPUT, CONF_PORT: 7338.0}
        )

    assert result["data"][CONF_PORT] == 7338
    assert isinstance(result["data"][CONF_PORT], int)


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (HemAuthError("bad key"), "invalid_auth"),
        (HemConnectionError("unreachable"), "cannot_connect"),
        (RuntimeError("boom"), "unknown"),
    ],
)
async def test_user_flow_reports_errors_and_recovers(
    hass: HomeAssistant,
    mock_setup_entry: AsyncMock,
    error: Exception,
    expected: str,
) -> None:
    """Every failure shows a form the user can correct, not a dead end."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )

    with patch(VALIDATE, side_effect=error):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], USER_INPUT
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": expected}

    with patch(VALIDATE):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], USER_INPUT
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_an_unexpected_error_is_logged_with_its_traceback(
    hass: HomeAssistant,
    mock_setup_entry: AsyncMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The broad catch must not swallow the detail needed to debug it."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )

    with patch(VALIDATE, side_effect=RuntimeError("boom")):
        await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)

    assert "Unexpected error validating HEM" in caplog.text
    assert "RuntimeError: boom" in caplog.text


async def test_the_same_host_and_port_cannot_be_added_twice(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    """One HEM install is one entry."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )

    with patch(VALIDATE):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], USER_INPUT
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


# --- reauth ----------------------------------------------------------------


async def test_reauth_replaces_the_key(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_setup_entry: AsyncMock
) -> None:
    """HEM shows a regenerated key only once, so reauth takes a new one."""
    result = await config_entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"

    with patch(VALIDATE):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_API_KEY: "new-key"}
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert config_entry.data[CONF_API_KEY] == "new-key"
    # The address is untouched.
    assert config_entry.data[CONF_HOST] == HOST


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (HemAuthError("still bad"), "invalid_auth"),
        (HemConnectionError("unreachable"), "cannot_connect"),
    ],
)
async def test_reauth_reports_errors(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    error: Exception,
    expected: str,
) -> None:
    """A replacement key that also fails keeps the form up."""
    result = await config_entry.start_reauth_flow(hass)

    with patch(VALIDATE, side_effect=error):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_API_KEY: "new-key"}
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": expected}
    assert config_entry.data[CONF_API_KEY] == API_KEY


# --- reconfigure -----------------------------------------------------------


async def test_reconfigure_shows_its_form(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    """The step opens on a form seeded with the entry's current address."""
    result = await config_entry.start_reconfigure_flow(hass)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reconfigure"


@pytest.mark.parametrize(
    "changed",
    [
        {CONF_PORT: 9999},
        {CONF_HOST: "other.local"},
        {CONF_HOST: "new.local", CONF_PORT: 9999},
    ],
)
async def test_reconfigure_moves_hem_to_a_new_address(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_setup_entry: AsyncMock,
    changed: dict[str, Any],
) -> None:
    """Moving HEM is what this step exists to do.

    The unique id is the address, so a move necessarily changes it and the
    entry has to follow - otherwise the next setup reads as a different device.
    """
    result = await config_entry.start_reconfigure_flow(hass)

    with patch(VALIDATE):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {**USER_INPUT, **changed}
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"

    expected = {**USER_INPUT, **changed}
    assert config_entry.data[CONF_HOST] == expected[CONF_HOST]
    assert config_entry.data[CONF_PORT] == expected[CONF_PORT]
    assert config_entry.unique_id == f"{expected[CONF_HOST]}:{expected[CONF_PORT]}"


async def test_reconfigure_keeps_the_api_key(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_setup_entry: AsyncMock
) -> None:
    """A move is not a reauth; the key the user already has still applies."""
    result = await config_entry.start_reconfigure_flow(hass)

    with patch(VALIDATE):
        await hass.config_entries.flow.async_configure(
            result["flow_id"], {**USER_INPUT, CONF_PORT: 9999}
        )

    assert config_entry.data[CONF_API_KEY] == API_KEY


async def test_reconfigure_refuses_an_address_another_entry_owns(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_setup_entry: AsyncMock
) -> None:
    """Two entries on one address would fight over the same HEM."""
    other = MockConfigEntry(
        domain=DOMAIN,
        title="Home Energy Manager (other.local)",
        data={**ENTRY_DATA, CONF_HOST: "other.local"},
        unique_id=f"other.local:{PORT}",
    )
    other.add_to_hass(hass)

    result = await config_entry.start_reconfigure_flow(hass)

    with patch(VALIDATE):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {**USER_INPUT, CONF_HOST: "other.local"}
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "wrong_device"
    # The entry the flow was launched from is left alone.
    assert config_entry.data[CONF_HOST] == HOST
    assert config_entry.unique_id == f"{HOST}:{PORT}"


async def test_reconfigure_accepts_the_same_address_again(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_setup_entry: AsyncMock
) -> None:
    """Re-submitting unchanged revalidates; an entry cannot clash with itself."""
    result = await config_entry.start_reconfigure_flow(hass)

    with patch(VALIDATE):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], USER_INPUT
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert config_entry.unique_id == f"{HOST}:{PORT}"


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (HemConnectionError("unreachable"), "cannot_connect"),
        (HemAuthError("bad key"), "invalid_auth"),
    ],
)
async def test_reconfigure_reports_a_bad_address(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    error: Exception,
    expected: str,
) -> None:
    """An unreachable or unauthenticated new address keeps the form up."""
    result = await config_entry.start_reconfigure_flow(hass)

    with patch(VALIDATE, side_effect=error):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {**USER_INPUT, CONF_PORT: 9999}
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": expected}
    assert config_entry.data[CONF_PORT] == PORT


# --- options ---------------------------------------------------------------


async def test_options_are_stored_as_ints(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_setup_entry: AsyncMock
) -> None:
    """Number selectors hand back floats; ints keep comparisons clean."""
    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    assert result["step_id"] == "init"

    user_input: dict[str, Any] = {
        CONF_SCAN_INTERVAL: 60.0,
        "poll_snapshot": True,
        "enable_controls": True,
        CONF_FORCE_MINUTES: 90.0,
        CONF_CONFIRM_TIMEOUT: 30.0,
        CONF_DASHBOARD_PORT: 7337.0,
    }
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    for key in (
        CONF_SCAN_INTERVAL,
        CONF_FORCE_MINUTES,
        CONF_CONFIRM_TIMEOUT,
        CONF_DASHBOARD_PORT,
    ):
        assert isinstance(config_entry.options[key], int)
    assert config_entry.options[CONF_SCAN_INTERVAL] == 60


# --- validation ------------------------------------------------------------


async def test_validate_proves_the_key_by_reading_status(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A successful read is the check; nothing is written during setup."""
    from custom_components.home_energy_manager.config_flow import _async_validate

    aioclient_mock.get(STATUS_URL, json=STATUS)

    await _async_validate(hass, {**ENTRY_DATA})

    assert aioclient_mock.call_count == 1


async def test_validate_propagates_a_rejected_key(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """The flow relies on the typed error to pick its message."""
    from custom_components.home_energy_manager.config_flow import _async_validate

    aioclient_mock.get(STATUS_URL, status=401, json={"error": "bad key"})

    with pytest.raises(HemAuthError):
        await _async_validate(hass, {**ENTRY_DATA})
