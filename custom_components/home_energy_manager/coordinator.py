"""Polling coordinator and command dispatcher for Home Energy Manager."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_SCAN_INTERVAL
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import (
    HemAuthError,
    HemConflictError,
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
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

# Command states that mean "stop polling".
TERMINAL_STATES = {"readback_confirmed", "failed", "expired", "unknown"}


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
        # `expired` means the window has elapsed, so it is no longer "on".
        return quick.get("phase") in ("pending", "active", "waiting", "restricted")
    if status.get("control_source") == wanted:
        return status.get("control_phase") != "expired"
    return False


@dataclass
class HemData:
    """One poll cycle's worth of data."""

    status: dict[str, Any] = field(default_factory=dict)
    snapshot: dict[str, Any] = field(default_factory=dict)


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

        # Duration used by the switches; the number entity writes to this.
        self.force_minutes: int = options.get(CONF_FORCE_MINUTES, DEFAULT_FORCE_MINUTES)

        # Last command bookkeeping, surfaced as diagnostic sensors.
        self.last_command_id: str | None = None
        self.last_command_state: str | None = None
        self.last_command_action: str | None = None

        # Snapshot support is probed once; a 404/403 there must not kill polling.
        self._snapshot_available = True
        self._command_lock = asyncio.Lock()

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
            raise UpdateFailed(f"Rate limited by HEM: {err}") from err
        except HemError as err:
            raise UpdateFailed(str(err)) from err

        snapshot: dict[str, Any] = {}
        if self._poll_snapshot and self._snapshot_available:
            try:
                snapshot = await self.api.async_get_snapshot()
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
                # Transient: a timeout, a rate limit or a 5xx. Hold the last
                # good snapshot rather than blanking every snapshot sensor, and
                # try again on the next poll.
                if self.data is not None:
                    snapshot = self.data.snapshot
                    _LOGGER.debug(
                        "Snapshot fetch failed, holding the previous snapshot "
                        "until the next poll: %s",
                        err,
                    )
                else:
                    _LOGGER.debug(
                        "Snapshot fetch failed with nothing to fall back on, "
                        "retrying next poll: %s",
                        err,
                    )

        _LOGGER.debug("HEM status=%s snapshot=%s", status, snapshot)
        return HemData(status=status, snapshot=snapshot)

    # --- control -------------------------------------------------------------

    async def async_force(self, action: str, minutes: int) -> str | None:
        """Start a force action and return its command id.

        One command at a time per entry: HEM refuses opposite-direction actions
        and a second start replaces the restore point, so serialising here
        avoids generating that situation from Home Assistant itself.
        """
        async with self._command_lock:
            key = self.api.new_idempotency_key()
            try:
                if action == ACTION_CHARGE:
                    result = await self.api.async_force_charge(minutes, key)
                else:
                    result = await self.api.async_force_discharge(minutes, key)
            except HemPermissionError as err:
                raise HomeAssistantError(
                    "HEM refused the command: enable 'Allow battery control through "
                    f"the authenticated API' in Home Energy Manager settings ({err})"
                ) from err
            except HemConflictError as err:
                raise HomeAssistantError(
                    f"HEM rejected the command: {err}. Stop the opposite action first, "
                    "or wait for a snapshot to become available."
                ) from err
            except HemError as err:
                raise HomeAssistantError(f"Force {action} failed: {err}") from err

            return await self._async_track(result, f"force_{action}")

    async def async_stop(self, action: str) -> str | None:
        """Stop a force action and return its command id."""
        async with self._command_lock:
            key = self.api.new_idempotency_key()
            try:
                if action == ACTION_CHARGE:
                    result = await self.api.async_stop_force_charge(key)
                else:
                    result = await self.api.async_stop_force_discharge(key)
            except HemError as err:
                raise HomeAssistantError(f"Stop force {action} failed: {err}") from err

            return await self._async_track(result, f"stop_force_{action}")

    async def _async_track(self, result: dict[str, Any], label: str) -> str | None:
        """Record the accepted command and start readback confirmation."""
        command_id = result.get("command_id")
        self.last_command_id = command_id
        self.last_command_action = label
        self.last_command_state = "accepted"
        _LOGGER.debug("HEM accepted %s as command %s", label, command_id)

        if command_id and self._confirm_timeout > 0:
            self.entry.async_create_background_task(
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
