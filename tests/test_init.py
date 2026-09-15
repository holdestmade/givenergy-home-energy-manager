"""Tests for setup, unload and the control services."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
import voluptuous as vol
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_API_KEY, CONF_HOST
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.home_energy_manager.const import (
    ACTION_CHARGE,
    ACTION_DISCHARGE,
    ATTR_CONFIG_ENTRY_ID,
    ATTR_MINUTES,
    DOMAIN,
    SERVICE_FORCE_CHARGE,
    SERVICE_FORCE_DISCHARGE,
    SERVICE_STOP_FORCE_CHARGE,
    SERVICE_STOP_FORCE_DISCHARGE,
)

from .const import SNAPSHOT_URL, STATUS, STATUS_URL


async def test_setup_and_unload(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """A healthy HEM loads and unloads cleanly."""
    assert init_integration.state is ConfigEntryState.LOADED

    assert await hass.config_entries.async_unload(init_integration.entry_id)
    await hass.async_block_till_done()

    assert init_integration.state is ConfigEntryState.NOT_LOADED


async def test_all_entities_share_one_device(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """One HEM install is one device, linked to its dashboard."""
    devices = dr.async_entries_for_config_entry(
        dr.async_get(hass), init_integration.entry_id
    )

    assert len(devices) == 1
    # The dashboard runs on its own port, not the API port.
    assert devices[0].configuration_url == "http://hem.local:7337"


async def test_a_bad_key_starts_reauth(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """401 at setup is a configuration problem, not a retry."""
    aioclient_mock.get(STATUS_URL, status=401, json={"error": "bad key"})

    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    assert config_entry.state is ConfigEntryState.SETUP_ERROR
    assert any(
        flow["context"]["source"] == "reauth"
        for flow in hass.config_entries.flow.async_progress()
    )


async def test_an_unreachable_hem_is_retried(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """A HEM that is merely down should come back on its own."""
    aioclient_mock.get(STATUS_URL, exc=TimeoutError())

    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    assert config_entry.state is ConfigEntryState.SETUP_RETRY


async def test_a_hem_with_no_snapshot_yet_is_retried(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Snapshot entities are chosen at setup, so wait until HEM can supply one."""
    aioclient_mock.get(STATUS_URL, json=STATUS)
    aioclient_mock.get(SNAPSHOT_URL, status=409, json={"error": "No snapshot yet"})

    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    assert config_entry.state is ConfigEntryState.SETUP_RETRY
    assert "No snapshot yet" in (config_entry.reason or "")


async def test_changing_options_reloads_the_entry(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """Poll interval and control options only take effect on reload."""
    with patch(
        "custom_components.home_energy_manager.async_setup_entry", return_value=True
    ) as setup:
        hass.config_entries.async_update_entry(
            init_integration,
            options={**init_integration.options, "poll_snapshot": False},
        )
        await hass.async_block_till_done()

    assert setup.call_count == 1


# --- services --------------------------------------------------------------


@pytest.mark.parametrize(
    ("service", "action"),
    [
        (SERVICE_FORCE_CHARGE, ACTION_CHARGE),
        (SERVICE_FORCE_DISCHARGE, ACTION_DISCHARGE),
    ],
)
async def test_force_services_reach_the_coordinator(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    service: str,
    action: str,
) -> None:
    """The service takes its own duration rather than the number entity's."""
    with patch.object(
        init_integration.runtime_data, "async_force", AsyncMock()
    ) as force:
        await hass.services.async_call(
            DOMAIN,
            service,
            {
                ATTR_CONFIG_ENTRY_ID: init_integration.entry_id,
                ATTR_MINUTES: 15,
            },
            blocking=True,
        )

    force.assert_awaited_once_with(action, 15)


@pytest.mark.parametrize(
    ("service", "action"),
    [
        (SERVICE_STOP_FORCE_CHARGE, ACTION_CHARGE),
        (SERVICE_STOP_FORCE_DISCHARGE, ACTION_DISCHARGE),
    ],
)
async def test_stop_services_reach_the_coordinator(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    service: str,
    action: str,
) -> None:
    """A stop needs no duration."""
    with patch.object(init_integration.runtime_data, "async_stop", AsyncMock()) as stop:
        await hass.services.async_call(
            DOMAIN,
            service,
            {ATTR_CONFIG_ENTRY_ID: init_integration.entry_id},
            blocking=True,
        )

    stop.assert_awaited_once_with(action)


@pytest.mark.parametrize("minutes", [0, -1, 1440, 99999])
async def test_the_service_schema_enforces_the_api_range(
    hass: HomeAssistant, init_integration: MockConfigEntry, minutes: int
) -> None:
    """HEM rejects anything outside 1..1439, so refuse it before the call."""
    with pytest.raises(vol.Invalid):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_FORCE_CHARGE,
            {
                ATTR_CONFIG_ENTRY_ID: init_integration.entry_id,
                ATTR_MINUTES: minutes,
            },
            blocking=True,
        )


async def test_an_unknown_entry_id_is_reported_to_the_user(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """A stale entry id in an automation should say so, not raise a traceback."""
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_STOP_FORCE_CHARGE,
            {ATTR_CONFIG_ENTRY_ID: "does-not-exist"},
            blocking=True,
        )


async def test_an_unloaded_entry_is_reported_to_the_user(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """Calling a service against an unloaded entry is a user error."""
    await hass.config_entries.async_unload(init_integration.entry_id)
    await hass.async_block_till_done()

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_STOP_FORCE_CHARGE,
            {ATTR_CONFIG_ENTRY_ID: init_integration.entry_id},
            blocking=True,
        )


# --- diagnostics -----------------------------------------------------------


async def test_diagnostics_redact_the_key_and_host(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
) -> None:
    """Diagnostics get pasted into issues, so the key must never appear."""
    from custom_components.home_energy_manager.diagnostics import (
        async_get_config_entry_diagnostics,
    )

    result = await async_get_config_entry_diagnostics(hass, init_integration)

    assert result["entry"]["data"][CONF_API_KEY] == "**REDACTED**"
    assert result["entry"]["data"][CONF_HOST] == "**REDACTED**"
    assert result["status"] == STATUS
    # The snapshot key list is the point of the download.
    assert "battery_soc" in result["snapshot_keys"]
