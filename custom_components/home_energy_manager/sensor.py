"""Sensors for Home Energy Manager.

Everything is exposed as its own flat sensor rather than packed into
attributes, so each value is separately templatable, recordable and
history-graphable.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    PERCENTAGE,
    EntityCategory,
    UnitOfElectricPotential,
    UnitOfEnergy,
    UnitOfFrequency,
    UnitOfPower,
    UnitOfTemperature,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .coordinator import HemCoordinator, HemData, pick
from .entity import HemEntity

_LOGGER = logging.getLogger(__name__)

MAX_STATE_LENGTH = 255

# Words that must not be sentence-cased into "Soc", "Pv" and so on.
ACRONYMS = {
    "soc": "SOC",
    "pv": "PV",
    "ct": "CT",
    "hem": "HEM",
    "ac": "AC",
    "dc": "DC",
    "ev": "EV",
}


def _as_float(value: Any) -> float | None:
    """Coerce API numbers (which may arrive as strings) to float."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_text(value: Any) -> str | None:
    """Coerce to a string that fits in a state, unchanged.

    Used only where HEM already returns human-facing prose.
    """
    if value is None:
        return None
    return str(value)[:MAX_STATE_LENGTH]


def _pretty(value: Any) -> str | None:
    """Format an API token as sentence case, matching HA's own states.

    HEM returns machine tokens like `connected`, `eco_paused` and
    `readback_confirmed`, while Home Assistant renders its binary sensors as
    `On`, `Off` and `Connected`. Passing every enum-ish value through here makes
    the whole device read consistently: `Connected`, `Eco paused`,
    `Readback confirmed`, `Off`.

    Unrecognised values are handled gracefully, which the HEM docs specifically
    ask for. That is why this is preferred over SensorDeviceClass.ENUM, which
    logs an error whenever the API returns a value outside a fixed options list.

    Note: automations that matched the old raw states (state == 'eco') need
    updating to the formatted form (state == 'Eco'), or should use the binary
    sensors instead, which are unaffected.
    """
    if value is None:
        return None
    text = str(value).replace("_", " ").replace("-", " ").strip()
    if not text:
        return None
    words = [ACRONYMS.get(word.lower(), word) for word in text.split()]
    joined = " ".join(words)
    # Capitalise the first character only; anything already upper (an acronym,
    # or a proper noun from the API) is left as it is.
    return (joined[0].upper() + joined[1:])[:MAX_STATE_LENGTH]


def _as_timestamp(value: Any) -> datetime | None:
    """Parse an RFC 3339 timestamp, tolerating null."""
    if not value:
        return None
    return dt_util.parse_datetime(str(value))


@dataclass(frozen=True, kw_only=True)
class HemSensorDescription(SensorEntityDescription):
    """Sensor description with a value function."""

    value_fn: Callable[[HemData], Any]
    # Snapshot sensors are only created when their key actually resolves,
    # because /api/snapshot has no published field list.
    from_snapshot: bool = False


# --- /api/control/status ----------------------------------------------------

STATUS_SENSORS: tuple[HemSensorDescription, ...] = (
    HemSensorDescription(
        key="summary",
        translation_key="summary",
        icon="mdi:text-short",
        # Left verbatim: already a human sentence, and the HEM docs warn not to
        # parse its wording.
        value_fn=lambda d: _as_text(d.status.get("summary")),
    ),
    HemSensorDescription(
        key="mode",
        translation_key="mode",
        icon="mdi:home-lightning-bolt-outline",
        value_fn=lambda d: _pretty(d.status.get("mode")),
    ),
    HemSensorDescription(
        key="activity",
        translation_key="activity",
        icon="mdi:battery-sync-outline",
        value_fn=lambda d: _pretty(d.status.get("activity")),
    ),
    HemSensorDescription(
        key="control_source",
        translation_key="control_source",
        icon="mdi:account-cog-outline",
        value_fn=lambda d: _pretty(d.status.get("control_source")),
    ),
    HemSensorDescription(
        key="control_phase",
        translation_key="control_phase",
        icon="mdi:progress-clock",
        value_fn=lambda d: _pretty(d.status.get("control_phase")),
    ),
    HemSensorDescription(
        key="remaining_minutes",
        translation_key="remaining_minutes",
        icon="mdi:timer-sand",
        native_unit_of_measurement=UnitOfTime.MINUTES,
        state_class=SensorStateClass.MEASUREMENT,
        # Window time left, NOT time until the battery is full or empty.
        value_fn=lambda d: _as_float(d.status.get("remaining_minutes")),
    ),
    HemSensorDescription(
        key="quick_action",
        translation_key="quick_action",
        icon="mdi:flash-outline",
        value_fn=lambda d: _pretty(pick(d.status, "quick_action.action") or "none"),
    ),
    HemSensorDescription(
        key="quick_action_ends_at",
        translation_key="quick_action_ends_at",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda d: _as_timestamp(pick(d.status, "quick_action.window_ends_at")),
    ),
    HemSensorDescription(
        key="schedule_charge",
        translation_key="schedule_charge",
        icon="mdi:calendar-arrow-right",
        value_fn=lambda d: _pretty(pick(d.status, "schedules.charge")),
    ),
    HemSensorDescription(
        key="schedule_export",
        translation_key="schedule_export",
        icon="mdi:calendar-export",
        value_fn=lambda d: _pretty(pick(d.status, "schedules.export")),
    ),
    HemSensorDescription(
        key="schedule_demand_discharge",
        translation_key="schedule_demand_discharge",
        icon="mdi:calendar-arrow-left",
        value_fn=lambda d: _pretty(pick(d.status, "schedules.demand_discharge")),
    ),
    HemSensorDescription(
        key="charging_mode",
        translation_key="charging_mode",
        icon="mdi:tune-variant",
        value_fn=lambda d: _pretty(pick(d.status, "automation.charging_mode")),
    ),
    HemSensorDescription(
        key="condition_count",
        translation_key="condition_count",
        icon="mdi:alert-circle-outline",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: len(d.status.get("conditions") or []),
    ),
    HemSensorDescription(
        key="conditions",
        translation_key="conditions",
        icon="mdi:format-list-bulleted",
        # Joins the human labels, which HEM already supplies properly cased.
        # The raw codes live on their own sensor below.
        value_fn=lambda d: _as_text(
            ", ".join(
                str(c.get("label"))
                for c in (d.status.get("conditions") or [])
                if isinstance(c, dict) and c.get("label")
            )
            or "None"
        ),
    ),
    HemSensorDescription(
        key="condition_codes",
        translation_key="condition_codes",
        icon="mdi:code-braces",
        entity_category=EntityCategory.DIAGNOSTIC,
        # Deliberately left raw and lowercase. Codes are the stable thing to
        # write automations against, so they must not be reformatted.
        value_fn=lambda d: _as_text(
            ", ".join(
                str(c.get("code"))
                for c in (d.status.get("conditions") or [])
                if isinstance(c, dict) and c.get("code")
            )
            or "none"
        ),
    ),
    HemSensorDescription(
        key="connection",
        translation_key="connection",
        icon="mdi:lan-connect",
        value_fn=lambda d: _pretty(d.status.get("connection")),
    ),
    HemSensorDescription(
        key="observed_at",
        translation_key="observed_at",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda d: _as_timestamp(d.status.get("observed_at")),
    ),
    HemSensorDescription(
        key="age_seconds",
        translation_key="age_seconds",
        icon="mdi:clock-alert-outline",
        native_unit_of_measurement=UnitOfTime.SECONDS,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: _as_float(d.status.get("age_seconds")),
    ),
    HemSensorDescription(
        key="stale_after_seconds",
        translation_key="stale_after_seconds",
        icon="mdi:clock-outline",
        native_unit_of_measurement=UnitOfTime.SECONDS,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: _as_float(d.status.get("stale_after_seconds")),
    ),
    # --- limits block -------------------------------------------------------
    HemSensorDescription(
        key="reserve_soc",
        translation_key="reserve_soc",
        icon="mdi:battery-arrow-down-outline",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: _as_float(
            pick(
                d.status,
                "limits.reserve_soc",
                "limits.reserve",
                "limits.battery_reserve",
            )
        ),
    ),
    HemSensorDescription(
        key="target_soc",
        translation_key="target_soc",
        icon="mdi:battery-arrow-up-outline",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: _as_float(
            pick(d.status, "limits.target_soc", "limits.charge_target", "limits.target")
        ),
    ),
    HemSensorDescription(
        key="charge_rate",
        translation_key="charge_rate",
        icon="mdi:speedometer",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        # Raw register value: not watts and not a universally scaled percentage.
        value_fn=lambda d: _as_float(
            pick(
                d.status,
                "limits.charge_rate_raw",
                "limits.charge_rate",
                "limits.charge",
            )
        ),
    ),
    HemSensorDescription(
        key="discharge_rate",
        translation_key="discharge_rate",
        icon="mdi:speedometer-slow",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: _as_float(
            pick(
                d.status,
                "limits.discharge_rate_raw",
                "limits.discharge_rate",
                "limits.discharge",
            )
        ),
    ),
)


# --- /api/snapshot ----------------------------------------------------------
# The snapshot schema is not formally published, so each sensor lists candidate
# key names and the first that exists wins. Entities are only created for keys
# that actually resolve at setup - check the debug log or the diagnostics
# download to see the real payload from your install.

SNAPSHOT_SENSORS: tuple[HemSensorDescription, ...] = (
    HemSensorDescription(
        key="battery_soc",
        translation_key="battery_soc",
        from_snapshot=True,
        device_class=SensorDeviceClass.BATTERY,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: _as_float(
            pick(
                d.snapshot,
                "battery_soc",
                "soc",
                "battery.soc",
                "battery_percent",
                "battery.state_of_charge",
                "state_of_charge",
            )
        ),
    ),
    HemSensorDescription(
        key="solar_power",
        translation_key="solar_power",
        from_snapshot=True,
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: _as_float(
            pick(
                d.snapshot,
                "solar_power",
                "pv_power",
                "power.solar",
                "solar.power",
                "solar_w",
            )
        ),
    ),
    HemSensorDescription(
        key="battery_power",
        translation_key="battery_power",
        from_snapshot=True,
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: _as_float(
            pick(
                d.snapshot,
                "battery_power",
                "power.battery",
                "battery.power",
                "battery_w",
            )
        ),
    ),
    HemSensorDescription(
        key="grid_power",
        translation_key="grid_power",
        from_snapshot=True,
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: _as_float(
            pick(d.snapshot, "grid_power", "power.grid", "grid.power", "grid_w")
        ),
    ),
    HemSensorDescription(
        key="home_power",
        translation_key="home_power",
        from_snapshot=True,
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: _as_float(
            pick(
                d.snapshot,
                "home_power",
                "load_power",
                "consumption_power",
                "power.home",
                "power.load",
                "home.power",
                "load_w",
            )
        ),
    ),
    HemSensorDescription(
        key="battery_temperature",
        translation_key="battery_temperature",
        from_snapshot=True,
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: _as_float(
            pick(
                d.snapshot,
                "battery_temperature",
                "battery_temp",
                "battery.temperature",
                "temperatures.battery",
            )
        ),
    ),
    HemSensorDescription(
        key="inverter_temperature",
        translation_key="inverter_temperature",
        from_snapshot=True,
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: _as_float(
            pick(
                d.snapshot,
                "inverter_temperature",
                "inverter_temp",
                "inverter.temperature",
                "temperatures.inverter",
            )
        ),
    ),
    HemSensorDescription(
        key="grid_voltage",
        translation_key="grid_voltage",
        from_snapshot=True,
        device_class=SensorDeviceClass.VOLTAGE,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: _as_float(
            pick(d.snapshot, "grid_voltage", "grid.voltage", "voltage.grid")
        ),
    ),
    HemSensorDescription(
        key="grid_frequency",
        translation_key="grid_frequency",
        from_snapshot=True,
        device_class=SensorDeviceClass.FREQUENCY,
        native_unit_of_measurement=UnitOfFrequency.HERTZ,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: _as_float(
            pick(d.snapshot, "grid_frequency", "grid.frequency", "frequency.grid")
        ),
    ),
    # --- today's counters: TOTAL_INCREASING so they work in the Energy dashboard
    HemSensorDescription(
        key="today_solar_energy",
        translation_key="today_solar_energy",
        from_snapshot=True,
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda d: _as_float(
            pick(
                d.snapshot,
                "today_solar_kwh",
                "today.solar_energy",
                "today.solar",
                "energy_today.solar",
                "today_solar_energy",
                "solar_energy_today",
            )
        ),
    ),
    HemSensorDescription(
        key="today_grid_import",
        translation_key="today_grid_import",
        from_snapshot=True,
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda d: _as_float(
            pick(
                d.snapshot,
                "today_import_kwh",
                "today.grid_import",
                "today.import",
                "energy_today.grid_import",
                "today_grid_import",
                "grid_import_today",
            )
        ),
    ),
    HemSensorDescription(
        key="today_grid_export",
        translation_key="today_grid_export",
        from_snapshot=True,
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda d: _as_float(
            pick(
                d.snapshot,
                "today_export_kwh",
                "today.grid_export",
                "today.export",
                "energy_today.grid_export",
                "today_grid_export",
                "grid_export_today",
            )
        ),
    ),
    HemSensorDescription(
        key="today_battery_charge",
        translation_key="today_battery_charge",
        from_snapshot=True,
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda d: _as_float(
            pick(
                d.snapshot,
                "today_charge_kwh",
                "today.battery_charge",
                "today.charge",
                "energy_today.battery_charge",
                "today_battery_charge",
                "battery_charge_today",
            )
        ),
    ),
    HemSensorDescription(
        key="today_battery_discharge",
        translation_key="today_battery_discharge",
        from_snapshot=True,
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda d: _as_float(
            pick(
                d.snapshot,
                "today_discharge_kwh",
                "today.battery_discharge",
                "today.discharge",
                "energy_today.battery_discharge",
                "today_battery_discharge",
                "battery_discharge_today",
            )
        ),
    ),
    HemSensorDescription(
        key="today_home_consumption",
        translation_key="today_home_consumption",
        from_snapshot=True,
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda d: _as_float(
            pick(
                d.snapshot,
                "today_consumption_kwh",
                "today.home_consumption",
                "today.consumption",
                "today.load",
                "energy_today.home_consumption",
                "today_home_consumption",
                "home_consumption_today",
            )
        ),
    ),
    HemSensorDescription(
        key="snapshot_age",
        translation_key="snapshot_age",
        from_snapshot=True,
        icon="mdi:clock-alert-outline",
        native_unit_of_measurement=UnitOfTime.SECONDS,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: _as_float(d.snapshot.get("age_seconds")),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up the sensors."""
    coordinator: HemCoordinator = entry.runtime_data
    data = coordinator.data

    entities: list[SensorEntity] = [
        HemSensor(coordinator, description) for description in STATUS_SENSORS
    ]

    missing: list[str] = []
    for description in SNAPSHOT_SENSORS:
        if description.value_fn(data) is None:
            missing.append(description.key)
            continue
        entities.append(HemSensor(coordinator, description))

    if missing:
        _LOGGER.debug(
            "Skipping snapshot sensors with no matching key: %s. "
            "Snapshot keys seen: %s",
            ", ".join(missing),
            sorted(data.snapshot) if data.snapshot else "(none)",
        )

    entities.append(HemLastCommandSensor(coordinator))
    async_add_entities(entities)


class HemSensor(HemEntity, SensorEntity):
    """A single value from status or snapshot."""

    entity_description: HemSensorDescription

    def __init__(
        self, coordinator: HemCoordinator, description: HemSensorDescription
    ) -> None:
        """Store the description."""
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> Any:
        """Return the current value."""
        if self.coordinator.data is None:
            return None
        return self.entity_description.value_fn(self.coordinator.data)


class HemLastCommandSensor(HemEntity, SensorEntity):
    """Lifecycle state of the most recent command this integration sent."""

    _attr_translation_key = "last_command_state"
    _attr_icon = "mdi:cursor-default-click-outline"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: HemCoordinator) -> None:
        """Set up the diagnostic sensor."""
        super().__init__(coordinator, "last_command_state")

    @property
    def native_value(self) -> str | None:
        """Return Accepted / Queued / Dispatched / Readback confirmed / Failed."""
        return _pretty(self.coordinator.last_command_state)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose the id and action so a failed command can be traced in HEM."""
        return {
            "command_id": self.coordinator.last_command_id,
            # Raw token, so it matches HEM's own logs.
            "action": self.coordinator.last_command_action,
        }
