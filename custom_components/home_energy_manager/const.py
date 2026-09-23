"""Constants for the Home Energy Manager integration."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "home_energy_manager"

# --- config entry data keys -------------------------------------------------
CONF_USE_SSL: Final = "use_ssl"

# --- options keys -----------------------------------------------------------
CONF_DASHBOARD_PORT: Final = "dashboard_port"
CONF_ENABLE_CONTROLS: Final = "enable_controls"
CONF_POLL_SNAPSHOT: Final = "poll_snapshot"
# No longer offered: the duration number is the one control for it. Entries
# that saved it still seed the number from it the first time it is created.
CONF_FORCE_MINUTES: Final = "force_minutes"
CONF_CONFIRM_TIMEOUT: Final = "confirm_timeout"

# --- defaults ---------------------------------------------------------------
DEFAULT_PORT: Final = 7338  # the separate authenticated API server
DEFAULT_DASHBOARD_PORT: Final = 7337  # the main HEM web UI, used for the device link
DEFAULT_SCAN_INTERVAL: Final = 30
MIN_SCAN_INTERVAL: Final = 10
MAX_SCAN_INTERVAL: Final = 900

DEFAULT_FORCE_MINUTES: Final = 60
MIN_FORCE_MINUTES: Final = 1
MAX_FORCE_MINUTES: Final = 1439  # HEM rejects anything outside 1..1439

DEFAULT_CONFIRM_TIMEOUT: Final = 120  # seconds spent polling /api/commands/{id}
MIN_CONFIRM_TIMEOUT: Final = 0  # 0 disables readback confirmation
MAX_CONFIRM_TIMEOUT: Final = 600
COMMAND_POLL_INTERVAL: Final = 5  # HEM docs recommend 5-10s; faster does not help

# How long a switch or select trusts its own commanded value while the
# inverter is written to and re-read. Readback typically lands well inside it.
OPTIMISTIC_SECONDS: Final = 180

# HEM's own floor for stale_after_seconds (three polls, at least 60 s), used
# when the status does not say.
DEFAULT_STALE_AFTER: Final = 60

# --- actions ----------------------------------------------------------------
ACTION_CHARGE: Final = "charge"
ACTION_DISCHARGE: Final = "discharge"

# Native battery pause modes, in HEM's own spelling, and the select's "none".
PAUSE_OFF: Final = "off"
PAUSE_MODES: Final = ("charge", "discharge", "both")

# --- services ---------------------------------------------------------------
SERVICE_FORCE_CHARGE: Final = "force_charge"
SERVICE_FORCE_DISCHARGE: Final = "force_discharge"
SERVICE_STOP_FORCE_CHARGE: Final = "stop_force_charge"
SERVICE_STOP_FORCE_DISCHARGE: Final = "stop_force_discharge"
SERVICE_PAUSE_BATTERY: Final = "pause_battery"
SERVICE_STOP_BATTERY_PAUSE: Final = "stop_battery_pause"

ATTR_CONFIG_ENTRY_ID: Final = "config_entry_id"
ATTR_MINUTES: Final = "minutes"
ATTR_MODE: Final = "mode"

MANUFACTURER: Final = "psylsph"
MODEL: Final = "Home Energy Manager (GivEnergy)"
