# Charge & consumption analytics (charger vs battery + efficiency)

Optional Home-Assistant setup that shows, per period (week / month / year)
and per session:

- **kWh from the charger** — *exact*, measured by a smart wallbox/EVSE.
- **kWh into the battery** — *estimated* from the car's SoC (`ΔSoC × pack`).
- **Average charging efficiency** = battery ÷ charger (≈ **85–90 %** on AC).

It is **not** part of the integration code — it's a HA *package* (template
sensors + `utility_meter`) plus a Lovelace card, built on entities the
integration and your wallbox already expose. Anyone can reproduce it by
adapting the entity IDs below.

## What the integration provides (no setup)

- `sensor.byd_<model>_total_charge_energy` — lifetime kWh into the battery,
  `total_increasing`. Accumulated by the coordinator's **SoC-rise integrator**
  (`_track_total_charge`): on every poll *while charging* it adds
  `(SoC rise since last reading) × pack`. The anchor is persisted (survives
  restarts) and cleared when not charging (a driving SoC drop is never
  counted). This is the **battery side** of the efficiency.
  - Pack nameplate is `_DEFAULT_BATTERY_KWH` in `coordinator.py` (82.5 kWh for
    the Sealion 7 Comfort). Change it for other trims/models.
- `sensor.byd_<model>_charge_session_kwh_added` / `_soc_added` — per-session.

## Prerequisites

1. A **smart wallbox/EVSE** integration exposing a per-session energy counter
   in kWh (e.g. `sensor.evse_<ip>_charge_energy`). It may reset to 0 each
   session — `utility_meter` handles that (it sums across resets).
2. HA **packages** enabled. In `configuration.yaml` under `homeassistant:`:
   ```yaml
   homeassistant:
     packages: !include_dir_named packages
   ```

## The package

Drop this as `config/packages/byd_charge_analytics.yaml` and **replace the two
source entity IDs** (`SENSOR_WALLBOX_ENERGY`, `SENSOR_TOTAL_CHARGE_ENERGY`)
with yours, then restart HA (or reload templates + restart for the meters).

```yaml
# Replace:
#   sensor.evse_192_168_0_229_charge_energy  -> your wallbox per-session kWh
#   sensor.byd_sealion_7_total_charge_energy -> your byd_<model>_total_charge_energy
utility_meter:
  byd_charger_kwh_weekly:  { source: sensor.evse_192_168_0_229_charge_energy,  cycle: weekly,  name: BYD charger kWh weekly }
  byd_charger_kwh_monthly: { source: sensor.evse_192_168_0_229_charge_energy,  cycle: monthly, name: BYD charger kWh monthly }
  byd_charger_kwh_yearly:  { source: sensor.evse_192_168_0_229_charge_energy,  cycle: yearly,  name: BYD charger kWh yearly }
  byd_battery_kwh_weekly:  { source: sensor.byd_sealion_7_total_charge_energy, cycle: weekly,  name: BYD battery kWh weekly }
  byd_battery_kwh_monthly: { source: sensor.byd_sealion_7_total_charge_energy, cycle: monthly, name: BYD battery kWh monthly }
  byd_battery_kwh_yearly:  { source: sensor.byd_sealion_7_total_charge_energy, cycle: yearly,  name: BYD battery kWh yearly }

template:
  - sensor:
      - name: BYD charge session charger kWh
        unique_id: byd_charge_session_charger_kwh
        unit_of_measurement: kWh
        device_class: energy
        state_class: total
        state: "{{ states('sensor.evse_192_168_0_229_charge_energy') | float(0) }}"
      - name: BYD charge session battery kWh
        unique_id: byd_charge_session_battery_kwh
        unit_of_measurement: kWh
        device_class: energy
        state_class: total
        state: "{{ states('sensor.byd_sealion_7_charge_session_kwh_added') | float(0) }}"
      - name: BYD charge session efficiency
        unique_id: byd_charge_session_efficiency
        unit_of_measurement: "%"
        icon: mdi:transmission-tower-import
        state: >
          {% set c = states('sensor.evse_192_168_0_229_charge_energy') | float(0) %}
          {% set b = states('sensor.byd_sealion_7_charge_session_kwh_added') | float(0) %}
          {{ (b / c * 100) | round(1) if (c > 0.1 and b > 0) else none }}
      - name: BYD charge efficiency weekly
        unique_id: byd_charge_efficiency_weekly
        unit_of_measurement: "%"
        state_class: measurement
        icon: mdi:transmission-tower-import
        state: >
          {% set c = states('sensor.byd_charger_kwh_weekly') | float(0) %}
          {% set b = states('sensor.byd_battery_kwh_weekly') | float(0) %}
          {{ (b / c * 100) | round(1) if c > 0.1 else none }}
      - name: BYD charge efficiency monthly
        unique_id: byd_charge_efficiency_monthly
        unit_of_measurement: "%"
        state_class: measurement
        icon: mdi:transmission-tower-import
        state: >
          {% set c = states('sensor.byd_charger_kwh_monthly') | float(0) %}
          {% set b = states('sensor.byd_battery_kwh_monthly') | float(0) %}
          {{ (b / c * 100) | round(1) if c > 0.1 else none }}
      - name: BYD charge efficiency yearly
        unique_id: byd_charge_efficiency_yearly
        unit_of_measurement: "%"
        state_class: measurement
        icon: mdi:transmission-tower-import
        state: >
          {% set c = states('sensor.byd_charger_kwh_yearly') | float(0) %}
          {% set b = states('sensor.byd_battery_kwh_yearly') | float(0) %}
          {{ (b / c * 100) | round(1) if c > 0.1 else none }}
```

## Dashboard card

```yaml
type: vertical-stack
cards:
  - type: gauge
    name: Average charging efficiency (month)
    entity: sensor.byd_charge_efficiency_monthly
    unit: "%"
    min: 70
    max: 100
    needle: true
    severity: { green: 85, yellow: 80, red: 70 }
  - type: entities
    title: Charging — charger vs battery
    entities:
      - { type: section, label: From charger (kWh) }
      - sensor.byd_charger_kwh_weekly
      - sensor.byd_charger_kwh_monthly
      - sensor.byd_charger_kwh_yearly
      - { type: section, label: Into battery (kWh) }
      - sensor.byd_battery_kwh_weekly
      - sensor.byd_battery_kwh_monthly
      - sensor.byd_battery_kwh_yearly
      - { type: section, label: Efficiency (%) }
      - sensor.byd_charge_efficiency_weekly
      - sensor.byd_charge_efficiency_monthly
      - sensor.byd_charge_efficiency_yearly
```

## How to read it / gotchas

- **Average efficiency ≈ 85–90 %** is normal for AC charging (onboard-charger
  losses). The **period (week/month/year)** figures are the trustworthy ones;
  the **live per-session** efficiency is noisy because cloud SoC is integer
  (a 1 % step = ~0.8 kWh) and a single session is small.
- **Why not ~100 %?** It used to read ~100 % because the *battery side* (SoC ×
  pack) ≈ the AC delivered. That's only meaningful as an efficiency when the
  battery side actually captures **all** the charging — which is why the
  integration uses the restart-safe SoC-rise integrator. If you see ~100 % or
  a wildly low number, the battery counter probably missed charges (see below).
- **Wallbox per-session resets:** the EVSE counter often goes 0→N→0 each
  session. `utility_meter` sums across resets, so use it (don't read the raw
  per-session value for period totals). A day with several plug-ins =
  several resets = the meter still sums them all.
- **Meters reset on their cycle** (weekly = Monday, monthly = 1st, yearly =
  Jan 1). After enabling, the first partial period is incomplete; the figure
  is meaningful from the next full cycle. To clean a desynced meter sooner,
  reset it via Developer Tools.
- **Charge not counted?** The battery side only accumulates *while the cloud
  reports charging* (`charging.charging_state == 1`). If the car was polled
  rarely (e.g. `disable_polling` on) or offline during a charge, the SoC rise
  may be missed. Public/DC charges away from a smart wallbox have no charger
  figure at all (only the battery side via SoC).
- **Pack capacity:** the battery estimate uses 82.5 kWh (Sealion 7 Comfort
  nameplate). For other models edit `_DEFAULT_BATTERY_KWH` in `coordinator.py`.
