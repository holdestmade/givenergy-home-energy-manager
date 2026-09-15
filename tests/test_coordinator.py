"""Tests for the polling coordinator and its command dispatcher."""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import AsyncMock, Mock, call, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers.update_coordinator import UpdateFailed
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.home_energy_manager.api import (
    HemAuthError,
    HemConflictError,
    HemConnectionError,
    HemNotFoundError,
    HemPermissionError,
    HemRateLimitError,
    HemResponseError,
)
from custom_components.home_energy_manager.const import ACTION_CHARGE, ACTION_DISCHARGE
from custom_components.home_energy_manager.coordinator import (
    HemCoordinator,
    pick,
    quick_action_matches,
)

from .const import SNAPSHOT, STATUS


@pytest.fixture
def api() -> AsyncMock:
    """Return an API double that answers both reads."""
    api = AsyncMock()
    api.async_get_status.return_value = STATUS
    api.async_get_snapshot.return_value = SNAPSHOT
    # Sync on the real client, so it must not hand back a coroutine.
    api.new_idempotency_key = Mock(return_value="key-1")
    return api


@pytest.fixture
def coordinator(
    hass: HomeAssistant, config_entry: MockConfigEntry, api: AsyncMock
) -> HemCoordinator:
    """Return a coordinator wired to the API double."""
    return HemCoordinator(hass, config_entry, api)


# --- pick ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("paths", "expected"),
    [
        (("battery_soc",), 55),
        (("automation.charging_mode",), "eco_paused"),
        (("missing", "battery_soc"), 55),
        (("missing.deeper", "automation.charging_mode"), "eco_paused"),
        (("missing",), None),
        (("summary.nested",), None),
        (("automation.missing",), None),
    ],
)
def test_pick_walks_candidates_in_order(paths: tuple[str, ...], expected: Any) -> None:
    """The first path that resolves wins; the rest are fallbacks."""
    assert pick(STATUS, *paths) == expected


def test_pick_returns_the_default_when_nothing_resolves() -> None:
    """A missing key is not an error - snapshot has no published field list."""
    assert pick(STATUS, "nope", default="fallback") == "fallback"


@pytest.mark.parametrize("data", [None, [], "text", 42])
def test_pick_tolerates_a_non_mapping(data: Any) -> None:
    """A malformed payload must not raise on the way to a sensor."""
    assert pick(data, "battery_soc", default="fallback") == "fallback"


def test_pick_treats_an_explicit_null_as_missing() -> None:
    """HEM sends null for "not applicable", so fall through to the next path."""
    assert pick({"a": None, "b": 7}, "a", "b") == 7


# --- quick_action_matches --------------------------------------------------


@pytest.mark.parametrize(
    ("phase", "expected"),
    [
        ("pending", True),
        ("active", True),
        ("waiting", True),
        ("restricted", True),
        ("expired", False),
        ("finished", False),
        (None, False),
    ],
)
def test_quick_action_phase_decides_whether_it_is_running(
    phase: str | None, expected: bool
) -> None:
    """`expired` means the window has elapsed, so the switch is off."""
    status = {"quick_action": {"action": "force_charge", "phase": phase}}
    assert quick_action_matches(status, ACTION_CHARGE) is expected


def test_quick_action_must_match_the_direction() -> None:
    """A running discharge does not turn the charge switch on."""
    status = {"quick_action": {"action": "force_discharge", "phase": "active"}}
    assert quick_action_matches(status, ACTION_CHARGE) is False
    assert quick_action_matches(status, ACTION_DISCHARGE) is True


def test_control_source_is_the_fallback_when_there_is_no_quick_action() -> None:
    """A safety limiter can own control_source while the action still runs."""
    assert quick_action_matches(
        {"control_source": "force_charge", "control_phase": "active"}, ACTION_CHARGE
    )
    assert not quick_action_matches(
        {"control_source": "force_charge", "control_phase": "expired"}, ACTION_CHARGE
    )


def test_quick_action_wins_over_control_source() -> None:
    """quick_action is authoritative where both are present."""
    status = {
        "quick_action": {"action": "force_charge", "phase": "expired"},
        "control_source": "force_charge",
        "control_phase": "active",
    }
    assert quick_action_matches(status, ACTION_CHARGE) is False


@pytest.mark.parametrize("status", [None, "text", 42, {}, {"quick_action": "text"}])
def test_quick_action_tolerates_a_malformed_status(status: Any) -> None:
    """Anything unexpected reads as "not running" rather than raising."""
    assert quick_action_matches(status, ACTION_CHARGE) is False


# --- polling ---------------------------------------------------------------


async def test_update_returns_both_payloads(coordinator: HemCoordinator) -> None:
    """A healthy poll carries status and snapshot."""
    await coordinator.async_refresh()

    assert coordinator.last_update_success
    assert coordinator.data.status == STATUS
    assert coordinator.data.snapshot == SNAPSHOT


async def test_auth_failure_on_status_triggers_reauth(
    coordinator: HemCoordinator, api: AsyncMock
) -> None:
    """A dead key is a reconfiguration problem, not a poll failure."""
    api.async_get_status.side_effect = HemAuthError("bad key")

    with pytest.raises(ConfigEntryAuthFailed):
        await coordinator._async_update_data()


@pytest.mark.parametrize(
    "error", [HemRateLimitError("slow down", 30), HemResponseError("HTTP 500")]
)
async def test_status_failures_become_update_failed(
    coordinator: HemCoordinator, api: AsyncMock, error: Exception
) -> None:
    """Without status there is nothing to show, so the poll fails."""
    api.async_get_status.side_effect = error

    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()


@pytest.mark.parametrize(
    "error", [HemNotFoundError("no route"), HemPermissionError("no")]
)
async def test_missing_snapshot_endpoint_is_probed_only_once(
    coordinator: HemCoordinator, api: AsyncMock, error: Exception
) -> None:
    """An install without /api/snapshot must not be asked every cycle."""
    api.async_get_snapshot.side_effect = error

    await coordinator.async_refresh()
    await coordinator.async_refresh()

    assert coordinator.last_update_success
    assert coordinator.data.snapshot == {}
    assert api.async_get_snapshot.call_count == 1
    assert api.async_get_status.call_count == 2


async def test_auth_failure_on_snapshot_also_triggers_reauth(
    coordinator: HemCoordinator, api: AsyncMock
) -> None:
    """A key revoked between the two reads is still a key problem."""
    api.async_get_snapshot.side_effect = HemAuthError("bad key")

    with pytest.raises(ConfigEntryAuthFailed):
        await coordinator._async_update_data()


async def test_transient_snapshot_failure_holds_the_previous_snapshot(
    coordinator: HemCoordinator, api: AsyncMock, caplog: pytest.LogCaptureFixture
) -> None:
    """Sensors keep their last good value instead of blanking."""
    await coordinator.async_refresh()
    api.async_get_snapshot.side_effect = HemConnectionError("timeout")

    with caplog.at_level(logging.DEBUG):
        await coordinator.async_refresh()

    assert coordinator.last_update_success
    assert coordinator.data.snapshot == SNAPSHOT
    assert "holding the previous snapshot" in caplog.text
    # Still transient, so it is retried rather than disabled.
    assert api.async_get_snapshot.call_count == 2


@pytest.mark.parametrize(
    "error", [HemConflictError("No snapshot yet"), HemConnectionError("timeout")]
)
async def test_transient_snapshot_failure_on_the_first_poll_fails_the_refresh(
    coordinator: HemCoordinator, api: AsyncMock, error: Exception
) -> None:
    """The platforms choose their snapshot entities from the first poll.

    An empty snapshot there would leave every one of them missing until the
    entry is reloaded, so the refresh fails and setup retries instead.
    """
    api.async_get_snapshot.side_effect = error

    with pytest.raises(UpdateFailed) as caught:
        await coordinator._async_update_data()

    assert str(error) in str(caught.value)


async def test_snapshot_polling_can_be_switched_off(
    hass: HomeAssistant, config_entry: MockConfigEntry, api: AsyncMock
) -> None:
    """The option stops the request being made at all."""
    hass.config_entries.async_update_entry(
        config_entry, options={**config_entry.options, "poll_snapshot": False}
    )
    coordinator = HemCoordinator(hass, config_entry, api)

    await coordinator.async_refresh()

    assert coordinator.data.snapshot == {}
    api.async_get_snapshot.assert_not_called()


# --- commands --------------------------------------------------------------


@pytest.mark.parametrize(
    ("action", "method"),
    [
        (ACTION_CHARGE, "async_force_charge"),
        (ACTION_DISCHARGE, "async_force_discharge"),
    ],
)
async def test_force_calls_the_matching_endpoint(
    coordinator: HemCoordinator, api: AsyncMock, action: str, method: str
) -> None:
    """Direction picks the endpoint, and the key comes from the client."""
    getattr(api, method).return_value = {"command_id": "abc-123"}
    coordinator._confirm_timeout = 0

    assert await coordinator.async_force(action, 30) == "abc-123"

    getattr(api, method).assert_awaited_once_with(30, "key-1")
    assert coordinator.last_command_id == "abc-123"
    assert coordinator.last_command_action == f"force_{action}"
    assert coordinator.last_command_state == "accepted"


@pytest.mark.parametrize(
    ("action", "method"),
    [
        (ACTION_CHARGE, "async_stop_force_charge"),
        (ACTION_DISCHARGE, "async_stop_force_discharge"),
    ],
)
async def test_stop_calls_the_matching_endpoint(
    coordinator: HemCoordinator, api: AsyncMock, action: str, method: str
) -> None:
    """A stop is tracked under its own label."""
    getattr(api, method).return_value = {"command_id": "def-456"}
    coordinator._confirm_timeout = 0

    assert await coordinator.async_stop(action) == "def-456"

    getattr(api, method).assert_awaited_once_with("key-1")
    assert coordinator.last_command_action == f"stop_force_{action}"


async def test_permission_error_explains_the_hem_toggle(
    coordinator: HemCoordinator, api: AsyncMock
) -> None:
    """A 403 means the user has to enable API control in HEM itself."""
    api.async_force_charge.side_effect = HemPermissionError("forbidden")

    with pytest.raises(HomeAssistantError) as caught:
        await coordinator.async_force(ACTION_CHARGE, 30)

    assert "Allow battery control" in str(caught.value)


async def test_conflict_error_surfaces_the_running_command(
    coordinator: HemCoordinator, api: AsyncMock
) -> None:
    """The id folded into the API message has to reach the user."""
    api.async_force_charge.side_effect = HemConflictError("already running", "abc-123")

    with pytest.raises(HomeAssistantError) as caught:
        await coordinator.async_force(ACTION_CHARGE, 30)

    assert "abc-123" in str(caught.value)
    assert "Stop the opposite action first" in str(caught.value)


async def test_other_start_failures_are_wrapped(
    coordinator: HemCoordinator, api: AsyncMock
) -> None:
    """Anything else still reaches the user as a Home Assistant error."""
    api.async_force_charge.side_effect = HemConnectionError("unreachable")

    with pytest.raises(HomeAssistantError) as caught:
        await coordinator.async_force(ACTION_CHARGE, 30)

    assert "Force charge failed" in str(caught.value)


async def test_other_stop_failures_are_wrapped(
    coordinator: HemCoordinator, api: AsyncMock
) -> None:
    """A stop that cannot be delivered is reported the same way."""
    api.async_stop_force_charge.side_effect = HemConnectionError("unreachable")

    with pytest.raises(HomeAssistantError) as caught:
        await coordinator.async_stop(ACTION_CHARGE)

    assert "Stop force charge failed" in str(caught.value)


async def test_a_command_without_confirmation_just_refreshes(
    coordinator: HemCoordinator, api: AsyncMock
) -> None:
    """With readback confirmation off, fall back to the next poll."""
    api.async_force_charge.return_value = {"command_id": "abc-123"}
    coordinator._confirm_timeout = 0

    with patch.object(coordinator, "async_request_refresh") as refresh:
        await coordinator.async_force(ACTION_CHARGE, 30)

    refresh.assert_called_once()


async def test_a_command_with_confirmation_is_tracked_in_the_background(
    coordinator: HemCoordinator, api: AsyncMock, config_entry: MockConfigEntry
) -> None:
    """Readback polling must not block the service call that started it."""
    api.async_force_charge.return_value = {"command_id": "abc-123"}
    coordinator._confirm_timeout = 120

    with patch.object(config_entry, "async_create_background_task") as task:
        await coordinator.async_force(ACTION_CHARGE, 30)

    assert task.call_count == 1
    assert task.call_args.kwargs["name"].endswith("confirm abc-123")
    # Close the coroutine the mock never awaited.
    task.call_args[0][1].close()


async def test_a_command_hem_does_not_id_is_not_tracked(
    coordinator: HemCoordinator, api: AsyncMock, config_entry: MockConfigEntry
) -> None:
    """With no command id there is nothing to poll for."""
    api.async_force_charge.return_value = {}
    coordinator._confirm_timeout = 120

    with (
        patch.object(config_entry, "async_create_background_task") as task,
        patch.object(coordinator, "async_request_refresh") as refresh,
    ):
        assert await coordinator.async_force(ACTION_CHARGE, 30) is None

    task.assert_not_called()
    refresh.assert_called_once()


async def test_a_newer_command_stops_tracking_the_previous_one(
    hass: HomeAssistant, coordinator: HemCoordinator, api: AsyncMock
) -> None:
    """Only the newest command is reported, so an older one must not overwrite it.

    Without the cancel, the stop's readback_confirmed would be replaced by the
    start's late "failed", against the stop's command id.
    """
    api.async_force_charge.return_value = {"command_id": "abc-123"}
    api.async_stop_force_charge.return_value = {"command_id": "def-456"}
    replies: dict[str, list[Any]] = {
        "abc-123": [HemConnectionError("blip"), {"state": "failed"}],
        "def-456": [{"state": "readback_confirmed"}],
    }

    def poll(command_id: str) -> dict[str, Any]:
        reply = replies[command_id].pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    api.async_get_command.side_effect = poll
    coordinator._confirm_timeout = 30

    with patch(
        "custom_components.home_energy_manager.coordinator.COMMAND_POLL_INTERVAL", 0
    ):
        await coordinator.async_force(ACTION_CHARGE, 30)
        first = coordinator._confirm_task
        # The stop lands while the start's confirmation is waiting to poll.
        await coordinator.async_stop(ACTION_CHARGE)
        await hass.async_block_till_done(wait_background_tasks=True)

    assert first is not None and first.cancelled()
    assert coordinator.last_command_id == "def-456"
    assert coordinator.last_command_state == "readback_confirmed"
    # The superseded command was never polled, let alone allowed to report.
    assert api.async_get_command.await_args_list == [call("def-456")]


async def test_a_command_without_an_id_still_supersedes_the_previous_one(
    hass: HomeAssistant, coordinator: HemCoordinator, api: AsyncMock
) -> None:
    """Nothing to track for the new command, but the old one is still stale."""
    api.async_force_charge.return_value = {"command_id": "abc-123"}
    api.async_stop_force_charge.return_value = {}
    api.async_get_command.return_value = {"state": "failed"}
    coordinator._confirm_timeout = 30

    with patch(
        "custom_components.home_energy_manager.coordinator.COMMAND_POLL_INTERVAL", 0
    ):
        await coordinator.async_force(ACTION_CHARGE, 30)
        first = coordinator._confirm_task
        await coordinator.async_stop(ACTION_CHARGE)
        await hass.async_block_till_done(wait_background_tasks=True)

    assert first is not None and first.cancelled()
    assert coordinator.last_command_id is None
    assert coordinator.last_command_state == "accepted"
    api.async_get_command.assert_not_awaited()


# --- readback confirmation -------------------------------------------------


async def test_confirmation_records_a_terminal_state(
    coordinator: HemCoordinator, api: AsyncMock
) -> None:
    """readback_confirmed is the only state that means the inverter applied it."""
    api.async_get_command.return_value = {"data": {"state": "readback_confirmed"}}
    coordinator._confirm_timeout = 30

    with patch(
        "custom_components.home_energy_manager.coordinator.COMMAND_POLL_INTERVAL", 0
    ):
        await coordinator._async_confirm("abc-123")

    assert coordinator.last_command_state == "readback_confirmed"


async def test_confirmation_warns_on_an_unhappy_terminal_state(
    coordinator: HemCoordinator, api: AsyncMock, caplog: pytest.LogCaptureFixture
) -> None:
    """A failed command has to be loud - the inverter may disagree."""
    api.async_get_command.return_value = {"state": "failed"}
    coordinator._confirm_timeout = 30

    with patch(
        "custom_components.home_energy_manager.coordinator.COMMAND_POLL_INTERVAL", 0
    ):
        await coordinator._async_confirm("abc-123")

    assert coordinator.last_command_state == "failed"
    assert "check the inverter" in caplog.text


@pytest.mark.parametrize(
    "error", [HemAuthError("key revoked"), HemNotFoundError("forgotten")]
)
async def test_confirmation_stops_on_errors_that_waiting_cannot_fix(
    coordinator: HemCoordinator,
    api: AsyncMock,
    caplog: pytest.LogCaptureFixture,
    error: Exception,
) -> None:
    """Neither recovers by polling again, so stop tracking and say why."""
    api.async_get_command.side_effect = error
    coordinator._confirm_timeout = 30

    with patch(
        "custom_components.home_energy_manager.coordinator.COMMAND_POLL_INTERVAL", 0
    ):
        await coordinator._async_confirm("abc-123")

    assert "Stopped tracking command abc-123" in caplog.text
    assert str(error) in caplog.text


async def test_confirmation_keeps_polling_through_a_transient_error(
    coordinator: HemCoordinator, api: AsyncMock
) -> None:
    """A blip mid-confirmation should not abandon the command."""
    api.async_get_command.side_effect = [
        HemConnectionError("blip"),
        {"state": "readback_confirmed"},
    ]
    coordinator._confirm_timeout = 30

    with patch(
        "custom_components.home_energy_manager.coordinator.COMMAND_POLL_INTERVAL", 0
    ):
        await coordinator._async_confirm("abc-123")

    assert api.async_get_command.call_count == 2
    assert coordinator.last_command_state == "readback_confirmed"


async def test_confirmation_gives_up_at_the_deadline(
    coordinator: HemCoordinator, api: AsyncMock, caplog: pytest.LogCaptureFixture
) -> None:
    """An expired budget is reported rather than waited on forever."""
    coordinator._confirm_timeout = 0

    await coordinator._async_confirm("abc-123")

    assert "Gave up waiting for HEM command abc-123" in caplog.text
