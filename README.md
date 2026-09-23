# Home Energy Manager — Home Assistant integration

[![hacs][hacs-badge]][hacs-url]
[![Validate](https://github.com/holdestmade/givenergy-home-energy-manager/actions/workflows/validate.yml/badge.svg)](https://github.com/holdestmade/givenergy-home-energy-manager/actions/workflows/validate.yml)
[![AI Assisted](https://img.shields.io/badge/AI--assisted-Claude-8A2BE2?logo=anthropic&logoColor=white)](#ai-disclosure)

A custom integration for [psylsph/home-energy-manager](https://github.com/psylsph/home-energy-manager),
talking to its authenticated integration API (default port **7338**, not the 7337 dashboard).

Requires Home Assistant **2024.12** or newer (uses `entry.runtime_data`, `_get_reauth_entry`
and `_get_reconfigure_entry`).

## AI Disclosure

This integration was developed with substantial assistance from AI (Anthropic's Claude).
Code was generated and iterated on through AI conversations, then reviewed, tested
and maintained by me on my own Home Assistant installation (HA 2026.x).
## Install

### HACS (recommended)

This repository is HACS-compatible but is not in the default HACS store, so add it as a
custom repository:

1. HACS → **⋮** (top right) → **Custom repositories**.
2. Repository: `https://github.com/holdestmade/givenergy-home-energy-manager`,
   Type: **Integration** → **Add**.
3. Find **GivEnergy Home Energy Manager** in HACS, **Download**, then **restart Home
   Assistant**.

Or use the My Home Assistant link:

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=holdestmade&repository=givenergy-home-energy-manager&category=integration)

### Manual

Copy `custom_components/home_energy_manager/` into your HA `config/custom_components/`
directory and restart.

### Then

**Settings → Devices & services → Add integration → Home Energy Manager**.

[hacs-badge]: https://img.shields.io/badge/HACS-Custom-41BDF5.svg
[hacs-url]: https://github.com/hacs/integration

## Before you start

In HEM: **Settings → Remote / Mobile Network Access → Authenticated API**

1. **Generate API key** — it is shown once, copy it.
2. Set the **listen address**. A fresh install listens on `127.0.0.1` only, which HA
   cannot reach unless HA runs on the same machine. Set a LAN address (or put HEM behind
   a reverse proxy on your trusted network) and **Apply network settings**.
3. For the switches/buttons/services to work, also enable **Allow battery control
   through the authenticated API**. It is off by default, including after upgrades.
   Reading works without it — a working sensor list is *not* proof that control is on.

Bearer tokens are not encrypted over plain HTTP, so keep this on your LAN or behind a VPN.

## Config flow

Host, API port, API key, plus optional HTTPS/verify toggles for a reverse-proxy setup.
The host can be pasted as a URL (even the dashboard's): only its host name is kept, and
`https://` turns HTTPS on; the API port always comes from its own field.
Setup is validated with a `GET /api/control/status`. Reauth (HEM regenerates keys in
place) and reconfigure (moved host/port) are both supported.

## Options flow

| Option | Default | Notes |
|---|---|---|
| Polling interval | 30 s | HEM rate-limits reads; 10 s minimum here |
| Poll `/api/snapshot` | on | Turn off to poll status only |
| Enable battery controls | **off** | Creates the switches, battery pause, stop buttons and duration number |
| Command readback timeout | 120 s | 0 disables `/api/commands/{id}` polling |
| Dashboard port | 7337 | Only used for the device's "Visit" link |

Changing options reloads the entry. How long a force action or pause lasts is set on the
device's **Force action duration** control (1–1439 min, starts at 60, remembered across
restarts). It used to be an option as well, but the control's remembered value always
won, so changing the option did nothing after the first setup.

## Entities

**Status sensors** (all flat, no packed attributes): summary, mode, activity, control
source, control phase, window remaining, quick action, quick action ends, charge /
export / demand-discharge schedule, charging mode, Cosy / Agile / Adaptive Charge phase,
condition count, condition codes, connection, observed at, reading age, stale after,
reserve SOC, target SOC, raw charge and discharge rates, calibration and maintenance
phase, last command state, and battery power cutoff (three-phase models only, so it
only appears where HEM reports it).

**Binary sensors**: inverter connection, stale reading, status unavailable, conditions
present, force charge active, force discharge active, battery pause active, charge
schedule active, export schedule active, battery charging, grid online.

**Snapshot sensors**: battery %, battery state (idle / charging / discharging), solar /
battery / grid / home power, grid import and export power, battery charge and discharge
power, battery and inverter temperature, grid voltage and frequency, today's solar,
import, export, charge, discharge and consumption energy (`total_increasing`, so they
drop into the Energy dashboard), snapshot age.

**Controls** (when enabled): `switch.force_charge`, `switch.force_discharge`,
`select.battery_pause` (off / pause charging / pause discharging / pause both), three
stop buttons (force charge, force discharge, battery pause), and
`number.force_action_duration`.

**Services**: `home_energy_manager.force_charge` / `.force_discharge` (with `minutes`),
`.stop_force_charge`, `.stop_force_discharge`, `.pause_battery` (with `mode` —
`charge`, `discharge` or `both` — and `minutes`), `.stop_battery_pause`. Services take
their own duration; the switches and the pause select use the duration control.

## About the snapshot sensors

HEM documents `/api/control/status` field by field, but `/api/snapshot` is only described
as "power flows, state of charge, temperatures, grid readings and today's energy
counters" — no published key list. So each snapshot sensor tries several candidate key
names (`battery_soc`, `soc`, `battery.soc`, …) and the **entity is only created once a key
actually resolves**.

The key names below are confirmed against a diagnostics download from HEM and are tried
first; the older guesses are kept behind them as fallbacks for other HEM builds.

| Sensor | Confirmed key |
|---|---|
| Battery | `soc` |
| Solar / battery / grid / home power | `solar_power`, `battery_power`, `grid_power`, `home_power` |
| Solar energy today | `today_solar_kwh` |
| Grid import / export today | `today_import_kwh`, `today_export_kwh` |
| Battery charge / discharge today | `today_charge_kwh`, `today_discharge_kwh` |
| Home consumption today | `today_consumption_kwh` |
| Charge / discharge rate (raw) | `limits.charge_rate_raw`, `limits.discharge_rate_raw` |

If something you expect is still missing:

1. Download **Diagnostics** from the device page — it includes the raw snapshot payload
   and a sorted `snapshot_keys` list.
2. Add the real key name to the matching `pick(...)` candidate list in `sensor.py`.

Same caveat for units: the power sensors assume watts and the energy counters kWh. If
your install reports kW, change `native_unit_of_measurement` on those descriptions.

The power sensors keep HEM's own signs, which follow GivTCP: **battery power is
positive when discharging** and negative when charging, and **grid power is positive
when exporting** and negative when importing. Home Assistant's convention for grid power
is the opposite (positive = import), so if you feed `grid_power` to something that
expects HA's convention, use the split sensors instead: *Grid import power*, *Grid
export power*, *Battery charge power* and *Battery discharge power* are each the
positive side of their flow and zero otherwise, which is what energy flow cards expect.

HEM keeps answering with its last reading after it loses the inverter, with a growing
`age_seconds`. Once that age passes the `stale_after_seconds` HEM reports (three of its
polls, at least 60 s), every snapshot reading goes **unavailable** rather than showing
old power flows as current, and comes back with the next fresh reading. *Snapshot age*
stays available, counting up, so you can see why. The age is HEM's own figure plus the
time since it answered, measured on this machine's monotonic clock, so a clock
difference between the two machines does not count as age.

Until HEM has its first inverter reading (just after HEM starts, or while it is still
connecting to the inverter), `/api/snapshot` answers `{"ok": false}` with no readings.
The entry loads anyway, so the status sensors — including *Inverter online* — are
available straight away, and each snapshot entity is added the first time its key
appears. A timeout or a 5xx later on is treated as transient: the previous readings are
held and the next poll retries. If `/api/snapshot` answers 403 or 404, snapshot polling
switches itself off for the rest of that entry's life (reload the entry to probe again)
and the integration carries on with the status sensors.

## How commands behave

A POST returning 200 means HEM **accepted and queued** it, not that the inverter applied
it. Each command gets a fresh `Idempotency-Key`. If HEM does not answer in time the
command may still have been queued, so it is retried once with the **same** key, which
makes HEM replay its original answer rather than queue a second command (a second
command would replace the first one's restore point). The integration then polls
`GET /api/commands/{id}` in the background until `readback_confirmed` (or `failed` /
`expired` / `unknown`, which are logged as warnings). `sensor.last_command_state` shows
where that got to, with the command id as an attribute. Only the newest command is
tracked: sending another one before the previous is confirmed drops the previous one's
polling, so the sensor never reports an older command's outcome against the new id.

The switches and the pause select hold an optimistic state for up to 3 minutes and then
defer to whatever HEM reports. Commands are serialised per entry so HA never fires two
starts at once — HEM refuses opposite directions, and a second start replaces the
restore point. Only one battery action runs at a time: HEM refuses a pause while a
force action runs, and vice versa, so stop one before starting the other.

**Battery pause** is the inverter's native pause (HEM 0.83.3+): pause charging,
discharging or both for the set duration; stopping restores the settings HEM captured
before the pause.

**What the inverter supports** comes from HEM's `control_capabilities`. When HEM says an
action is unsupported, its switch goes unavailable, the pause select only offers the
supported modes, and the services refuse it with a message instead of sending it. While
HEM cannot tell yet (it reports `null` before its first reading, or for an unknown
model) everything stays available and HEM decides. The stop buttons and stop services
are never restricted: a stop is how a running action gets undone.

Errors are reported in plain language, including HEM's own reason: for example
*HEM refused to start Force Charge: enable 'Allow battery control through the
authenticated API' in Home Energy Manager settings*. If HEM rate-limits the status
poll, Home Assistant 2025.12 and later wait the `Retry-After` HEM asks for.

Known behaviours inherited from HEM's Quick Actions (not bugs in this integration):
stop can restore a pre-action non-Eco mode; a charge window's previous schedule is not
restored at expiry; and this API is not an emergency stop.

## Example automation

```yaml
automation:
  - alias: Force charge on cheap overnight rate
    triggers:
      - trigger: time
        at: "00:30:00"
    conditions:
      - condition: numeric_state
        # Entity ids carry the device name, e.g. home_energy_manager_192_168_1_5.
        entity_id: sensor.home_energy_manager_192_168_1_5_battery
        below: 30
    actions:
      - action: home_energy_manager.force_charge
        data:
          # Pick the entry in the automation editor, or paste its entry id.
          config_entry_id: 0123456789abcdef0123456789abcdef
          minutes: 180

  - alias: Keep the battery for the evening peak
    triggers:
      - trigger: time
        at: "13:00:00"
    actions:
      - action: home_energy_manager.pause_battery
        data:
          config_entry_id: 0123456789abcdef0123456789abcdef
          mode: discharge
          minutes: 240
```

## Development

The test suite runs against a real Home Assistant, supplied by
`pytest-homeassistant-custom-component`, which pins the Home Assistant, pytest
and pytest-asyncio versions that belong together. The current pin is Home
Assistant 2026.6.4, which needs Python 3.14.2 or newer:

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements_test.txt
pytest
```

Coverage, as CI reports it:

```bash
pytest --cov=custom_components.home_energy_manager --cov-report=term-missing
```

Linting matches the parts of Home Assistant core's own config that apply to a
custom integration:

```bash
ruff check . && ruff format --check .
```

Bump the pin in `requirements_test.txt` to test against a newer Home Assistant.

## License

[MIT](LICENSE).
