# Home Energy Manager — Home Assistant integration

A custom integration for [psylsph/home-energy-manager](https://github.com/psylsph/home-energy-manager),
talking to its authenticated integration API (default port **7338**, not the 7337 dashboard).

Requires Home Assistant **2024.12** or newer (uses `entry.runtime_data`, `_get_reauth_entry`
and `_get_reconfigure_entry`).

## Install

Copy `custom_components/home_energy_manager/` into your HA `config/custom_components/`
directory and restart. Then **Settings → Devices & services → Add integration →
Home Energy Manager**.

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
Setup is validated with a `GET /api/control/status`. Reauth (HEM regenerates keys in
place) and reconfigure (moved host/port) are both supported.

## Options flow

| Option | Default | Notes |
|---|---|---|
| Polling interval | 30 s | HEM rate-limits reads; 10 s minimum here |
| Poll `/api/snapshot` | on | Turn off to poll status only |
| Enable battery controls | **off** | Creates the switches, stop buttons and duration number |
| Default force duration | 60 min | Seeds the duration number (1–1439) |
| Command readback timeout | 120 s | 0 disables `/api/commands/{id}` polling |
| Dashboard port | 7337 | Only used for the device's "Visit" link |

Changing options reloads the entry.

## Entities

**Status sensors** (all flat, no packed attributes): summary, mode, activity, control
source, control phase, window remaining, quick action, quick action ends, charge /
export / demand-discharge schedule, charging mode, condition count, condition codes,
connection, observed at, reading age, stale after, reserve SOC, target SOC, raw charge
and discharge rates, last command state.

**Binary sensors**: inverter connection, stale reading, status unavailable, conditions
present, force charge active, force discharge active, charge schedule active, export
schedule active, battery charging.

**Snapshot sensors**: battery %, solar / battery / grid / home power, battery and
inverter temperature, grid voltage and frequency, today's solar, import, export, charge,
discharge and consumption energy (`total_increasing`, so they drop into the Energy
dashboard), snapshot age.

**Controls** (when enabled): `switch.force_charge`, `switch.force_discharge`,
two stop buttons, and `number.force_action_duration`.

**Services**: `home_energy_manager.force_charge` / `.force_discharge` (with `minutes`),
`.stop_force_charge`, `.stop_force_discharge`.

## About the snapshot sensors

HEM documents `/api/control/status` field by field, but `/api/snapshot` is only described
as "power flows, state of charge, temperatures, grid readings and today's energy
counters" — no published key list. So each snapshot sensor tries several candidate key
names (`battery_soc`, `soc`, `battery.soc`, …) and the **entity is only created if a key
actually resolves** during setup.

If something you expect is missing:

1. Download **Diagnostics** from the device page — it includes the raw snapshot payload
   and a sorted `snapshot_keys` list.
2. Add the real key name to the matching `pick(...)` candidate list in `sensor.py`.

Same caveat for units: the power sensors assume watts and the energy counters kWh. If
your install reports kW, change `native_unit_of_measurement` on those descriptions.

## How commands behave

A POST returning 200 means HEM **accepted and queued** it, not that the inverter applied
it. Each command gets a fresh `Idempotency-Key`, and the integration then polls
`GET /api/commands/{id}` in the background until `readback_confirmed` (or `failed` /
`expired` / `unknown`, which are logged as warnings). `sensor.last_command_state` shows
where that got to, with the command id as an attribute.

The switches hold an optimistic state for up to 3 minutes and then defer to whatever HEM
reports. Commands are serialised per entry so HA never fires two starts at once — HEM
refuses opposite directions, and a second start replaces the restore point.

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
        entity_id: sensor.home_energy_manager_battery
        below: 30
    actions:
      - action: home_energy_manager.force_charge
        data:
          config_entry_id: !input hem_entry   # or paste the entry id
          minutes: 180
```
