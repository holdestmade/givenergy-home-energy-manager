"""Tests for the polling coordinator and its command dispatcher."""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
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
from custom_components.home_energy_manager.const import (
    ACTION_CHARGE,
    ACTION_DISCHARGE,
    DOMAIN,
)
from custom_components.home_energy_manager.coordinator import (
    HemCoordinator,
    HemData,
    pause_mode_of,
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
async def coordinator(
    hass: HomeAssistant, config_entry: MockConfigEntry, api: AsyncMock
) -> AsyncGenerator[HemCoordinator]:
    """Return a coordinator wired to the API double.

    Nothing unloads this entry, so shut the coordinator down here: a refresh
    request leaves its debouncer timer behind otherwise, which newer Home
    Assistant test harnesses fail as a lingering timer.
    """
    coordinator = HemCoordinator(hass, config_entry, api)
    yield coordinator
    await coordinator.async_shutdown()


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
async def test_transient_snapshot_failure_on_the_first_poll_is_not_fatal(
    coordinator: HemCoordinator, api: AsyncMock, error: Exception
) -> None:
    """With nothing to hold yet, the snapshot is just empty for now.

    The status is still worth showing, and the platforms add each snapshot
    entity once its key turns up, so there is no reason to fail the poll.
    """
    api.async_get_snapshot.side_effect = error

    await coordinator.async_refresh()

    assert coordinator.last_update_success
    assert coordinator.data.status == STATUS
    assert coordinator.data.snapshot == {}


# What HEM actually answers before its first inverter reading: a 200, not a 409.
NO_READING_YET: dict[str, Any] = {
    "ok": False,
    "error": "No inverter data available yet",
    "observed_at": None,
    "age_seconds": None,
}


async def test_a_snapshot_hem_does_not_have_yet_is_treated_as_none(
    coordinator: HemCoordinator, api: AsyncMock
) -> None:
    """ok:false carries no readings, so it must not become the snapshot."""
    api.async_get_snapshot.return_value = NO_READING_YET

    await coordinator.async_refresh()

    assert coordinator.last_update_success
    assert coordinator.data.snapshot == {}

    # Once HEM has a reading, it is used as normal.
    api.async_get_snapshot.return_value = SNAPSHOT
    await coordinator.async_refresh()

    assert coordinator.data.snapshot == SNAPSHOT


async def test_a_later_snapshot_without_readings_holds_the_previous_one(
    coordinator: HemCoordinator, api: AsyncMock
) -> None:
    """ok:false after good data is held over like any other transient failure."""
    await coordinator.async_refresh()
    api.async_get_snapshot.return_value = NO_READING_YET

    await coordinator.async_refresh()

    assert coordinator.data.snapshot == SNAPSHOT


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


@pytest.mark.parametrize(
    ("error", "key"),
    [
        (
            HemPermissionError("External battery control is disabled"),
            "control_disabled",
        ),
        (HemConflictError("already running", "abc-123"), "command_conflict"),
        (HemResponseError("HTTP 422: unsupported_control"), "command_failed"),
    ],
)
async def test_start_failures_are_translated(
    coordinator: HemCoordinator, api: AsyncMock, error: Exception, key: str
) -> None:
    """Each failure gets its own message, carrying HEM's own words.

    The command id HEM folds into a conflict is the user's only handle on the
    command already running, so it has to survive into the message.
    """
    api.async_force_charge.side_effect = error

    with pytest.raises(HomeAssistantError) as caught:
        await coordinator.async_force(ACTION_CHARGE, 30)

    assert caught.value.translation_domain == DOMAIN
    assert caught.value.translation_key == key
    assert caught.value.translation_placeholders == {
        "command": "start Force Charge",
        "error": str(error),
    }


async def test_stop_failures_are_translated(
    coordinator: HemCoordinator, api: AsyncMock
) -> None:
    """A stop that cannot be delivered is reported the same way."""
    api.async_stop_force_charge.side_effect = HemResponseError("HTTP 500: boom")

    with pytest.raises(HomeAssistantError) as caught:
        await coordinator.async_stop(ACTION_CHARGE)

    assert caught.value.translation_key == "command_failed"
    assert caught.value.translation_placeholders["command"] == "stop Force Charge"


@pytest.mark.parametrize(
    ("send", "method", "args"),
    [
        (lambda c: c.async_force(ACTION_CHARGE, 30), "async_force_charge", (30,)),
        (lambda c: c.async_stop(ACTION_DISCHARGE), "async_stop_force_discharge", ()),
    ],
    ids=["start", "stop"],
)
async def test_a_lost_reply_is_retried_with_the_same_key(
    coordinator: HemCoordinator, api: AsyncMock, send: Any, method: str, args: tuple
) -> None:
    """HEM may have queued it, so only a same-key retry is safe.

    A fresh key would be a second command replacing the first's restore point;
    the same key makes HEM replay its original answer.
    """
    getattr(api, method).side_effect = [
        HemConnectionError("timeout"),
        {"command_id": "abc-123"},
    ]
    coordinator._confirm_timeout = 0

    assert await send(coordinator) == "abc-123"

    assert getattr(api, method).await_args_list == [
        call(*args, "key-1"),
        call(*args, "key-1"),
    ]
    api.new_idempotency_key.assert_called_once()
    assert coordinator.last_command_id == "abc-123"


async def test_a_retry_that_finds_the_first_attempt_in_progress_follows_it(
    coordinator: HemCoordinator, api: AsyncMock
) -> None:
    """HEM answers 409 with the command id while the key is still in flight."""
    api.async_force_charge.side_effect = [
        HemConnectionError("timeout"),
        HemConflictError(
            "A request with this Idempotency-Key is still in progress", "abc-123"
        ),
    ]
    coordinator._confirm_timeout = 0

    assert await coordinator.async_force(ACTION_CHARGE, 30) == "abc-123"
    assert coordinator.last_command_id == "abc-123"


async def test_a_command_that_cannot_be_delivered_is_retried_only_once(
    coordinator: HemCoordinator, api: AsyncMock
) -> None:
    """A HEM that is really down is reported, not hammered."""
    api.async_force_charge.side_effect = HemConnectionError("unreachable")

    with pytest.raises(HomeAssistantError) as caught:
        await coordinator.async_force(ACTION_CHARGE, 30)

    assert caught.value.translation_key == "command_failed"
    assert api.async_force_charge.await_count == 2


async def test_a_conflict_on_the_first_attempt_is_not_mistaken_for_a_retry(
    coordinator: HemCoordinator, api: AsyncMock
) -> None:
    """Only a retry's 409 means "yours is in progress"; a first 409 is a refusal."""
    api.async_force_charge.side_effect = HemConflictError("already running", "old-1")

    with pytest.raises(HomeAssistantError):
        await coordinator.async_force(ACTION_CHARGE, 30)

    assert api.async_force_charge.await_count == 1
    assert coordinator.last_command_id is None


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


# --- rate limits -----------------------------------------------------------


async def test_a_rate_limit_passes_hem_retry_after_on(
    coordinator: HemCoordinator, api: AsyncMock
) -> None:
    """Home Assistant 2025.12+ waits the advertised time before polling again."""
    api.async_get_status.side_effect = HemRateLimitError("Too many requests", 42)

    with pytest.raises(UpdateFailed) as caught:
        await coordinator._async_update_data()

    assert caught.value.retry_after == 42


# --- capabilities ----------------------------------------------------------


async def test_capabilities_keep_the_last_definite_answer(
    coordinator: HemCoordinator, api: AsyncMock
) -> None:
    """HEM reporting null means "cannot tell right now", not "unsupported"."""
    api.async_get_status.return_value = {
        **STATUS,
        "control_capabilities": {
            "force_charge": True,
            "force_discharge": False,
            "pause_modes": ["charge"],
        },
    }
    await coordinator.async_refresh()

    api.async_get_status.return_value = {
        **STATUS,
        "control_capabilities": {
            "force_charge": None,
            "force_discharge": None,
            "pause_modes": None,
        },
    }
    await coordinator.async_refresh()

    assert coordinator.capabilities == {
        "force_charge": True,
        "force_discharge": False,
        "pause_modes": ["charge"],
    }


async def test_a_start_hem_says_is_unsupported_is_refused_up_front(
    coordinator: HemCoordinator, api: AsyncMock
) -> None:
    """Nothing is sent; the user is told why."""
    coordinator.capabilities = {"force_discharge": False}

    with pytest.raises(HomeAssistantError) as caught:
        await coordinator.async_force(ACTION_DISCHARGE, 30)

    assert caught.value.translation_key == "command_unsupported"
    api.async_force_discharge.assert_not_called()


async def test_an_unknown_capability_lets_hem_decide(
    coordinator: HemCoordinator, api: AsyncMock
) -> None:
    """HEM reports null until it can tell, which must not block anything."""
    api.async_force_charge.return_value = {"command_id": "abc-123"}
    coordinator._confirm_timeout = 0

    assert await coordinator.async_force(ACTION_CHARGE, 30) == "abc-123"


async def test_a_stop_is_never_refused_on_capability(
    coordinator: HemCoordinator, api: AsyncMock
) -> None:
    """A stop is how a running action gets undone, whatever HEM now says."""
    coordinator.capabilities = {"force_charge": False, "pause_modes": []}
    api.async_stop_force_charge.return_value = {"command_id": "a"}
    api.async_stop_pause.return_value = {"command_id": "b"}
    coordinator._confirm_timeout = 0

    assert await coordinator.async_stop(ACTION_CHARGE) == "a"
    assert await coordinator.async_stop_pause() == "b"


# --- battery pause ---------------------------------------------------------


@pytest.mark.parametrize("mode", ["charge", "discharge", "both"])
async def test_pause_sends_its_mode_and_duration(
    coordinator: HemCoordinator, api: AsyncMock, mode: str
) -> None:
    """The mode goes over in HEM's own spelling, tracked under HEM's action name."""
    api.async_pause.return_value = {"command_id": "p-1"}
    coordinator._confirm_timeout = 0

    assert await coordinator.async_pause(mode, 45) == "p-1"

    api.async_pause.assert_awaited_once_with(mode, 45, "key-1")
    assert coordinator.last_command_action == f"pause_{mode}"


async def test_stop_pause_is_tracked_under_its_own_label(
    coordinator: HemCoordinator, api: AsyncMock
) -> None:
    """A stop needs no mode: HEM restores whatever it captured."""
    api.async_stop_pause.return_value = {"command_id": "s-1"}
    coordinator._confirm_timeout = 0

    assert await coordinator.async_stop_pause() == "s-1"

    api.async_stop_pause.assert_awaited_once_with("key-1")
    assert coordinator.last_command_action == "stop_pause"


async def test_a_pause_mode_hem_does_not_list_is_refused(
    coordinator: HemCoordinator, api: AsyncMock
) -> None:
    """Only the modes in pause_modes are supported once HEM has said."""
    coordinator.capabilities = {"pause_modes": ["charge"]}

    with pytest.raises(HomeAssistantError) as caught:
        await coordinator.async_pause("both", 30)

    assert caught.value.translation_placeholders == {
        "command": "pause battery charging and discharging"
    }
    api.async_pause.assert_not_called()


async def test_a_pause_conflict_is_explained(
    coordinator: HemCoordinator, api: AsyncMock
) -> None:
    """HEM refuses a pause while a force action (or another pause) runs."""
    api.async_pause.side_effect = HemConflictError(
        "Another battery control action is active"
    )

    with pytest.raises(HomeAssistantError) as caught:
        await coordinator.async_pause("charge", 30)

    assert caught.value.translation_key == "command_conflict"
    assert caught.value.translation_placeholders["command"] == "pause battery charging"


@pytest.mark.parametrize(
    ("quick_action", "expected"),
    [
        ({"action": "pause_mode", "mode": 1, "phase": "active"}, "charge"),
        ({"action": "pause_mode", "mode": 2, "phase": "pending"}, "discharge"),
        ({"action": "pause_mode", "mode": 3, "phase": "active"}, "both"),
        ({"action": "pause_mode", "mode": "both", "phase": "active"}, "both"),
        ({"action": "pause_mode", "mode": "pause_charge", "phase": "active"}, "charge"),
        # A stop is putting things back, or the window has run out.
        ({"action": "pause_mode", "mode": 1, "phase": "restoring"}, None),
        ({"action": "pause_mode", "mode": 1, "phase": "expired"}, None),
        # Not a pause, or not one we can name.
        ({"action": "force_charge", "phase": "active"}, None),
        ({"action": "pause_mode", "mode": 9, "phase": "active"}, None),
        ({"action": "pause_mode", "mode": True, "phase": "active"}, None),
        ({"action": "pause_mode", "phase": "active"}, None),
        (None, None),
        ("text", None),
    ],
)
def test_pause_mode_of_reads_hem_register_values(
    quick_action: Any, expected: str | None
) -> None:
    """HEM reports a running pause's mode as the register value it wrote."""
    assert pause_mode_of({"quick_action": quick_action}) == expected


@pytest.mark.parametrize("status", [None, "text", {}])
def test_pause_mode_of_tolerates_a_malformed_status(status: Any) -> None:
    """Anything unexpected reads as "no pause" rather than raising."""
    assert pause_mode_of(status) is None


# --- snapshot age ----------------------------------------------------------

MONOTONIC = "custom_components.home_energy_manager.coordinator.time.monotonic"


def test_snapshot_age_counts_on_from_what_hem_reported() -> None:
    """HEM's age at answer time plus local time since, never wall clocks."""
    data = HemData(
        status={"stale_after_seconds": 60},
        snapshot={"age_seconds": 4},
        snapshot_received=1000.0,
    )

    with patch(MONOTONIC, return_value=1010.0):
        assert data.snapshot_age == 14
        assert not data.snapshot_stale
    with patch(MONOTONIC, return_value=1057.0):
        assert data.snapshot_age == 61
        assert data.snapshot_stale


def test_hem_can_already_report_a_stale_reading() -> None:
    """HEM keeps serving its last reading after it loses the inverter."""
    data = HemData(
        status={"stale_after_seconds": 90},
        snapshot={"age_seconds": 600},
        snapshot_received=1000.0,
    )

    with patch(MONOTONIC, return_value=1000.0):
        assert data.snapshot_stale


@pytest.mark.parametrize(
    "data",
    [
        HemData(),
        HemData(snapshot={"soc": 55}, snapshot_received=0.0),
        HemData(snapshot={"age_seconds": None}, snapshot_received=0.0),
    ],
)
def test_an_unknown_age_is_not_stale(data: HemData) -> None:
    """Without an age there is nothing to judge, so keep the readings."""
    assert not data.snapshot_stale


def test_the_stale_limit_falls_back_to_hem_minimum() -> None:
    """HEM never goes below 60 s, so use that when the status does not say."""
    data = HemData(snapshot={"age_seconds": 61}, snapshot_received=None)

    assert data.snapshot_stale


async def test_a_held_snapshot_keeps_ageing(
    coordinator: HemCoordinator, api: AsyncMock
) -> None:
    """A snapshot held through a failed fetch keeps its original arrival time."""
    api.async_get_snapshot.return_value = {**SNAPSHOT, "age_seconds": 5}
    with patch(MONOTONIC, return_value=1000.0):
        await coordinator.async_refresh()

    api.async_get_snapshot.side_effect = HemConnectionError("timeout")
    with patch(MONOTONIC, return_value=1100.0):
        await coordinator.async_refresh()
        assert coordinator.data.snapshot_received == 1000.0
        assert coordinator.data.snapshot_age == 105
        assert coordinator.data.snapshot_stale
