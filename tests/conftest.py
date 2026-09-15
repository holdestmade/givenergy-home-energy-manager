"""Shared fixtures."""

from __future__ import annotations

from collections.abc import Generator
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.home_energy_manager.const import DOMAIN

from .const import ENTRY_DATA, HOST, PORT, SNAPSHOT, SNAPSHOT_URL, STATUS, STATUS_URL


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(
    enable_custom_integrations: None,
) -> Generator[None]:
    """Let Home Assistant load this integration from custom_components."""
    yield


@pytest.fixture
def mock_setup_entry() -> Generator[AsyncMock]:
    """Stop config flow tests from standing up the whole integration."""
    with patch(
        "custom_components.home_energy_manager.async_setup_entry",
        return_value=True,
    ) as mock:
        yield mock


@pytest.fixture
def config_entry(hass: HomeAssistant) -> MockConfigEntry:
    """Return an entry added to hass, with controls and snapshot polling on."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=f"Home Energy Manager ({HOST})",
        data=ENTRY_DATA,
        options={"enable_controls": True, "poll_snapshot": True},
        unique_id=f"{HOST}:{PORT}",
    )
    entry.add_to_hass(hass)
    return entry


@pytest.fixture
def mock_hem(aioclient_mock: AiohttpClientMocker) -> AiohttpClientMocker:
    """Answer the two read endpoints with healthy payloads."""
    aioclient_mock.get(STATUS_URL, json=STATUS)
    aioclient_mock.get(SNAPSHOT_URL, json=SNAPSHOT)
    return aioclient_mock


@pytest.fixture
async def init_integration(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_hem: AiohttpClientMocker,
) -> MockConfigEntry:
    """Set the integration up against a healthy HEM."""
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    return config_entry
