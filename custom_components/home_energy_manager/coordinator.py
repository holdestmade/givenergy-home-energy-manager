"""Polling coordinator and command dispatcher for Home Energy Manager."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import timedelta
from functools import partial
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_SCAN_INTERVAL
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import (
    HemAuthError,
    HemConflictError,
    HemConnectionError,
    HemError,
    HemNotFoundError,
    HemPermissionError,
    HemRateLimitError,
    HomeEnergyManagerApi,
)
from .const import (
    ACTION_CHARGE,
    COMMAND_POLL_INTERVAL,
    CONF_CONFIRM_TIMEOUT,
    CONF_FORCE_MINUTES,
    CONF_POLL_SNAPSHOT,
    DEFAULT_CONFIRM_TIMEOUT,
    DEFAULT_FORCE_MINUTES,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_STALE_AFTER,
    DOMAIN,
    PAUSE_MODES,
)

_LOGGER = logging.getLogger(__name__)

# Command states that mean "stop polling".
TERMINAL_STATES = {"readback_confirmed", "failed", "expired", "unknown"}

# Quick action phases that mean the action is still in force. `expired` means
# the window has elapsed and `restoring` that a stop is putting things back.
RUNNING_PHASES = ("pending", "active", "waiting", "restricted")

# HEM reports a running pause's mode as the raw register value it wrote.
PAUSE_MODE_BY_REGISTER = {1: "charge", 2: "discharge", 3: "both"}

# How each command reads in an error message ("HEM could not ...").
COMMAND_PHRASES = {
    "force_charge": "start Force Charge",
    "force_discharge": "start Force Discharge",
    "stop_force_charge": "stop Force Charge",
    "stop_force_discharge": "stop Force Discharge",
    "pause_charge": "pause battery charging",
    "pause_discharge": "pause battery discharging",
    "pause_both": "pause battery charging and discharging",
    "stop_pause": "stop the battery pause",
}


def pick(data: dict[str, Any] | None, *paths: str, default: Any = None) -> Any:
    """Return the first value found at any of the dotted candidate paths.

    /api/control/status has a documented shape, but /api/snapshot is described
    only as "power flows, state of charge, temperatures, grid readings and
    today's energy counters" without a published field list. Every snapshot
    sensor therefore lists several plausible key names and takes the first that
    exists. Enable debug logging (or download diagnostics) to see the real
    payload, then trim the candidate lists in sensor.py if you want.
    """
    if not isinstance(data, dict):
        return default
    for path in paths:
        current: Any = data
        for part in path.split("."):
            if isinstance(current, dict) and part in current:
                current = current[part]
            else:
                current = None
                break
        if current is not None:
            return current
    return default


def quick_action_matches(status: dict[str, Any] | None, action: str) -> bool:
    """Return True when HEM reports an owned force action of this direction.

    `quick_action` is authoritative when present; `control_source` is the
    fallback because a safety limiter can take over `control_source` while the
    quick action is still the thing that is running.
    """
    if not isinstance(status, dict):
        return False
    wanted = f"force_{action}"
    quick = status.get("quick_action")
    if isinstance(quick, dict) and quick.get("action") == wanted:
        return quick.get("phase") in RUNNING_PHASES
    if status.get("control_source") == wanted:
        return status.get("control_phase") != "expired"
    return False


def pause_mode_of(status: dict[str, Any] | None) -> str | None:
    """Return the mode of a running native battery pause, or None.

    HEM reports the mode as its register value (1, 2 or 3); the spelled-out
    forms it accepts on the way in are recognised too, in case that changes.
    """
    if not isinstance(status, dict):
        return None
    quick = status.get("quick_action")
    if not isinstance(quick, dict) or quick.get("action") != "pause_mode":
        return None
    if quick.get("phase") not in RUNNING_PHASES:
        return None
    mode = quick.get("mode")
    if isinstance(mode, str):
        mode = mode.removeprefix("pause_")
        return mode if mode in PAUSE_MODES else None
    if isinstance(mode, int) and not isinstance(mode, bool):
        return PAUSE_MODE_BY_REGISTER.get(mode)
    return None


def _seconds(value: Any) -> float | None:
    """Coerce a JSON number of seconds, which may be null, to a float."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


@dataclass
class HemData:
    """One poll cycle's worth of data."""

    status: dict[str, Any] = field(default_factory=dict)
    snapshot: dict[str, Any] = field(default_factory=dict)
    # time.monotonic() when `snapshot` arrived. A snapshot held over from an
    # earlier poll keeps its original arrival time, so its age keeps growing.
    snapshot_received: float | None = None

    @property
    def snapshot_age(self) -> float | None:
        """Return how old the snapshot's reading is now, in seconds.

        HEM's own age_seconds at the time it answered, plus the time since, on
        the local monotonic clock: comparing HEM's observed_at with this
        machine's clock would count any clock skew between the two as age.
        """
        age = _seconds(self.snapshot.get("age_seconds"))
        if age is None or self.snapshot_received is None:
            return age
        return age + max(0.0, time.monotonic() - self.snapshot_received)

    @property
    def snapshot_stale(self) -> bool:
        """Return True when the snapshot is older than HEM's own stale limit.

        HEM keeps answering with its last reading after it loses the inverter,
        so without this the power sensors would keep showing old flows as if
        they were current.
        """
        age = self.snapshot_age
        if age is None:
            return False
        limit = _seconds(self.status.get("stale_after_seconds")) or DEFAULT_STALE_AFTER
        return age > limit


class HemCoordinator(DataUpdateCoordinator[HemData]):
    """Poll HEM and serialise control commands for one config entry."""

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, api: HomeEnergyManagerApi
    ) -> None:
        """Set up the coordinator from the entry's options."""
        self.api = api
        self.entry = entry

        options = entry.options
        self._poll_snapshot: bool = options.get(CONF_POLL_SNAPSHOT, True)
        self._confirm_timeout: int = options.get(
            CONF_CONFIRM_TIMEOUT, DEFAULT_CONFIRM_TIMEOUT
        )

        # Duration used by the switches and the pause select; the number
        # entity writes to this.
        self.force_minutes: int = options.get(CONF_FORCE_MINUTES, DEFAULT_FORCE_MINUTES)

        # What this inverter supports, from status.control_capabilities. HEM
        # reports null while it cannot tell (no reading yet, unknown model),
        # so the last definite answer is kept rather than flapping to unknown
        # whenever the connection drops. Absent means unknown, which allows.
        self.capabilities: dict[str, Any] = {}

        # Last command bookkeeping, surfaced as diagnostic sensors.
        self.last_command_id: str | None = None
        self.last_command_state: str | None = None
        self.last_command_action: str | None = None

        # Snapshot support is probed once; a 404/403 there must not kill polling.
        self._snapshot_available = True
        self._command_lock = asyncio.Lock()
        # Readback confirmation of the newest command, if still running.
        self._confirm_task: asyncio.Task[None] | None = None

        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            config_entry=entry,
            update_interval=timedelta(
                seconds=options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
            ),
        )

    async def _async_update_data(self) -> HemData:
        """Fetch status and (optionally) snapshot."""
        try:
            status = await self.api.async_get_status()
        except HemAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except HemRateLimitError as err:
            failed = UpdateFailed(f"Rate limited by HEM: {err}")
            # Home Assistant 2025.12+ waits this long before the next poll;
            # older versions ignore the attribute and keep their interval.
            failed.retry_after = err.retry_after
            raise failed from err
        except HemError as err:
            raise UpdateFailed(str(err)) from err

        capabilities = status.get("control_capabilities")
        if isinstance(capabilities, dict):
            self.capabilities.update(
                (name, value)
                for name, value in capabilities.items()
                if value is not None
            )

        snapshot: dict[str, Any] = {}
        received: float | None = None
        if self._poll_snapshot and self._snapshot_available:
            # Hold the last good snapshot through anything transient rather
            # than blanking every snapshot sensor. With no previous one the
            # snapshot entities simply do not exist yet: the platforms add
            # each one the first time its key resolves.
            previous = self.data if self.data is not None else HemData()
            try:
                fetched = await self.api.async_get_snapshot()
            except HemAuthError as err:
                raise ConfigEntryAuthFailed(str(err)) from err
            except (HemNotFoundError, HemPermissionError) as err:
                # This install does not serve /api/snapshot at all, so stop
                # asking: retrying every cycle would only add load and log noise.
                _LOGGER.warning(
                    "HEM does not serve /api/snapshot (%s); continuing on status "
                    "alone. Reload the entry to probe again",
                    err,
                )
                self._snapshot_available = False
            except HemError as err:
                # Transient: a timeout, a rate limit or a 5xx.
                snapshot, received = previous.snapshot, previous.snapshot_received
                _LOGGER.debug(
                    "Snapshot fetch failed, holding the previous snapshot "
                    "until the next poll: %s",
                    err,
                )
            else:
                if fetched.get("ok") is False:
                    # HEM answers 200 {"ok": false, "error": ...} with no
                    # readings at all until it has its first inverter reading.
                    snapshot, received = previous.snapshot, previous.snapshot_received
                    _LOGGER.debug(
                        "HEM has no snapshot yet (%s), trying again next poll",
                        fetched.get("error"),
                    )
                else:
                    snapshot, received = fetched, time.monotonic()

        _LOGGER.debug("HEM status=%s snapshot=%s", status, snapshot)
        return HemData(status=status, snapshot=snapshot, snapshot_received=received)

    # --- control -------------------------------------------------------------

    async def async_force(self, action: str, minutes: int) -> str | None:
        """Start a force action and return its command id.

        One command at a time per entry: HEM refuses opposite-direction actions
        and a second start replaces the restore point, so serialising here
        avoids generating that situation from Home Assistant itself.
        """
        label = f"force_{action}"
        if self.capabilities.get(label) is False:
            raise _unsupported(label)
        start = (
            self.api.async_force_charge
            if action == ACTION_CHARGE
            else self.api.async_force_discharge
        )
        return await self._async_command(partial(start, minutes), label)

    async def async_stop(self, action: str) -> str | None:
        """Stop a force action and return its command id.

        Never refused on capability grounds: a stop is how an action already
        running gets undone, whatever HEM now says about the inverter.
        """
        stop = (
            self.api.async_stop_force_charge
            if action == ACTION_CHARGE
            else self.api.async_stop_force_discharge
        )
        return await self._async_command(stop, f"stop_force_{action}")

    async def async_pause(self, mode: str, minutes: int) -> str | None:
        """Start a native battery pause and return its command id."""
        label = f"pause_{mode}"
        supported = self.capabilities.get("pause_modes")
        if isinstance(supported, list) and mode not in supported:
            raise _unsupported(label)
        return await self._async_command(
            partial(self.api.async_pause, mode, minutes), label
        )

    async def async_stop_pause(self) -> str | None:
        """Stop a native battery pause and return its command id."""
        return await self._async_command(self.api.async_stop_pause, "stop_pause")

    async def _async_command(
        self, send: Callable[[str], Awaitable[dict[str, Any]]], label: str
    ) -> str | None:
        """Send one command, translate its failure, and track it."""
        placeholders = {"command": COMMAND_PHRASES.get(label, label)}
        async with self._command_lock:
            try:
                result = await self._async_send(send)
            except HemPermissionError as err:
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="control_disabled",
                    translation_placeholders={**placeholders, "error": str(err)},
                ) from err
            except HemConflictError as err:
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="command_conflict",
                    translation_placeholders={**placeholders, "error": str(err)},
                ) from err
            except HemError as err:
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="command_failed",
                    translation_placeholders={**placeholders, "error": str(err)},
                ) from err

            return await self._async_track(result, label)

    async def _async_send(
        self, send: Callable[[str], Awaitable[dict[str, Any]]]
    ) -> dict[str, Any]:
        """Send one command, retrying once with the same key if the reply is lost.

        HEM can queue a command and still fail to answer in time, so a timeout
        does not mean it was refused. Sending it again under a fresh key would
        be a second command that replaces the first one's restore point; the
        HEM docs say to retry with the *same* key instead, which replays the
        original answer rather than queuing anything.
        """
        key = self.api.new_idempotency_key()
        try:
            return await send(key)
        except HemConnectionError as err:
            _LOGGER.debug("No answer to command (%s), retrying with its key", err)
        try:
            return await send(key)
        except HemConflictError as err:
            # The first attempt is still being processed under this key, so
            # that command is the one to follow.
            if err.command_id:
                return {"command_id": err.command_id}
            raise

    async def _async_track(self, result: dict[str, Any], label: str) -> str | None:
        """Record the accepted command and start readback confirmation."""
        # Only the newest command is reported, so a confirmation still running
        # for the previous one must not write its lifecycle over this one.
        if self._confirm_task is not None and not self._confirm_task.done():
            _LOGGER.debug(
                "Command %s superseded before it was confirmed, dropping it",
                self.last_command_id,
            )
            self._confirm_task.cancel()
        self._confirm_task = None

        command_id = result.get("command_id")
        self.last_command_id = command_id
        self.last_command_action = label
        self.last_command_state = "accepted"
        _LOGGER.debug("HEM accepted %s as command %s", label, command_id)

        if command_id and self._confirm_timeout > 0:
            self._confirm_task = self.entry.async_create_background_task(
                self.hass,
                self._async_confirm(command_id),
                name=f"{DOMAIN} confirm {command_id}",
            )
        else:
            await self.async_request_refresh()
        return command_id

    async def _async_confirm(self, command_id: str) -> None:
        """Poll a command id until it reaches a terminal state.

        An HTTP acknowledgement only means "queued". Only readback_confirmed
        means the inverter applied it; unknown means go and look.
        """
        deadline = self.hass.loop.time() + self._confirm_timeout
        while self.hass.loop.time() < deadline:
            await asyncio.sleep(COMMAND_POLL_INTERVAL)
            try:
                record = await self.api.async_get_command(command_id)
            except (HemAuthError, HemNotFoundError) as err:
                # Neither recovers by waiting: the key is gone, or HEM has
                # forgotten the command. The next poll will show the truth.
                _LOGGER.warning("Stopped tracking command %s: %s", command_id, err)
                break
            except HemError as err:
                _LOGGER.debug("Command %s poll failed: %s", command_id, err)
                continue

            state = pick(record, "data.state", "state")
            if state and state != self.last_command_state:
                self.last_command_state = state
                self.async_update_listeners()

            if state in TERMINAL_STATES:
                if state != "readback_confirmed":
                    _LOGGER.warning(
                        "HEM command %s finished as '%s' - check the inverter",
                        command_id,
                        state,
                    )
                break
        else:
            _LOGGER.warning(
                "Gave up waiting for HEM command %s after %ss",
                command_id,
                self._confirm_timeout,
            )

        await self.async_request_refresh()


def _unsupported(label: str) -> HomeAssistantError:
    """Return the error for a command HEM says this inverter cannot do."""
    return HomeAssistantError(
        translation_domain=DOMAIN,
        translation_key="command_unsupported",
        translation_placeholders={"command": COMMAND_PHRASES.get(label, label)},
    )
