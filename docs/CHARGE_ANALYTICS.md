# Charge & consumption analytics (charger vs battery + efficiency)

Optional Home-Assistant setup that shows, per period (week / month / year)
and per session:

- **kWh from the charger** — *exact*, measured by a smart wallbox/EVSE.
- **kWh into the battery** — *estimated* by integrating the battery charge
  power over time (`Σ power × Δt`, finer than 1 % SoC steps).
- **Average charging efficiency** = battery ÷ charger (≈ **85–90 %** on AC).

It is **not** part of the integration code — it's a HA *package* (template
sensors + `utility_meter`) plus a Lovelace card, built on entities the
integration and your wallbox already expose. Anyone can reproduce it by
adapting the entity IDs below.

## What the integration provides (no setup)

- `sensor.byd_<model>_total_charge_energy_integrated` — lifetime kWh into the
  battery, `total_increasing`, from **power integration** (`_track_charge_
  power_integral`): while charging it accumulates `power(kW) × Δt(h)`
  (trapezoidal) from `realtime.gl`. **Higher resolution** than 1 % SoC steps —
  this is the preferred **battery side** of the efficiency. Gaps > 0.5 h are
  skipped (lost samples); counter persisted. *(Enable it — disabled by
  default.)*
- `sensor.byd_<model>_total_charge_energy` — coarser fallback: lifetime kWh
  via the **SoC-rise integrator** (`(SoC rise) × pack` while charging, anchor
  persisted). Good enough for the HA Energy dashboard; less precise on short
  charges. Pack nameplate `_DEFAULT_BATTERY_KWH` in `coordinator.py` (82.5 kWh
  Sealion 7 Comfort — change per model).
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
#   sensor.evse_192_168_0_229_charge_energy             -> your wallbox per-session kWh
#   sensor.byd_sealion_7_total_charge_energy_integrated -> your byd_<model>_total_charge_energy_integrated
#   (the battery side uses the power-integrated counter; enable that sensor)
utility_meter:
  byd_charger_kwh_weekly:  { source: sensor.evse_192_168_0_229_charge_energy,  cycle: weekly,  name: BYD charger kWh weekly }
  byd_charger_kwh_monthly: { source: sensor.evse_192_168_0_229_charge_energy,  cycle: monthly, name: BYD charger kWh monthly }
  byd_charger_kwh_yearly:  { source: sensor.evse_192_168_0_229_charge_energy,  cycle: yearly,  name: BYD charger kWh yearly }
  byd_battery_kwh_weekly:  { source: sensor.byd_sealion_7_total_charge_energy_integrated, cycle: weekly,  name: BYD battery kWh weekly }
  byd_battery_kwh_monthly: { source: sensor.byd_sealion_7_total_charge_energy_integrated, cycle: monthly, name: BYD battery kWh monthly }
  byd_battery_kwh_yearly:  { source: sensor.byd_sealion_7_total_charge_energy_integrated, cycle: yearly,  name: BYD battery kWh yearly }

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
    # Plateau is ~92–94 % (≥15 A); green from 90, amber when low-amp charging
    # drags it down, red below ~80 %.
    severity: { green: 90, yellow: 80, red: 70 }
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

## Reference: measured AC→battery efficiency curve (Sealion 7)

Bench-measured on a real Sealion 7 (82.5 kWh pack) with a 1-phase V2C
wallbox (~222 V), AC only, SoC 36→51 %. Efficiency = battery power (DC, from
car telemetry) ÷ charger power (AC, from wallbox). Use this to sanity-check
what the period gauges report:

| Set | Real A | Charger (AC) | Battery (DC) | Loss | Efficiency |
|----:|-------:|-------------:|-------------:|-----:|-----------:|
|  6 A |  5.7 A | 1.265 kW | 1.026 kW | 0.239 kW | 81.1 % |
| 10 A |  9.3 A | 2.066 kW | 1.824 kW | 0.242 kW | 88.3 % |
| 11 A | 10.8 A | 2.399 kW | 2.231 kW | 0.168 kW | 93.0 % |
| 12 A | 11.8 A | 2.619 kW | 2.460 kW | 0.159 kW | 93.9 % |
| 15 A | 15.8 A | 3.513 kW | 3.318 kW | 0.196 kW | 94.4 % |
| 16 A | 15.8 A | 3.518 kW | 3.255 kW | 0.263 kW | 92.5 % |
| 20 A | 19.9 A | 4.427 kW | 4.126 kW | 0.302 kW | 93.2 % |
| 21 A | 21.5 A | 4.767 kW | 4.527 kW | 0.241 kW | 95.0 % |
| 25 A | 25.0 A | 5.539 kW | 5.157 kW | 0.382 kW | 93.1 % |
| 30 A | 29.2 A | 6.480 kW | 6.017 kW | 0.463 kW | 92.8 % |
| 31 A | 31.0 A | 6.882 kW | 6.418 kW | 0.464 kW | 93.1 % |

*(Abridged from a 23-point sweep, 6→31 A.)*

- **Plateau ~92.5–94 %** from 11 A to 31 A — no drop at the top of the range.
- Efficiency falls **only at very low current**: ~88 % @ 10 A, ~81 % @ 6 A —
  the charger's fixed overhead (electronics/BMS/cooling, ~0.16–0.25 kW)
  dominates when charging power is small.
- **Practical takeaway:** charge at **≥ 15 A for ~93 %**; avoid < 10 A.
- *Precision note:* the DC figure is car telemetry (~±0.07 kW), so ±2–3 % at
  low power; the 16–31 A points are very consistent.

So the period gauges should read **~92–94 %** for normal home charging and
dip toward ~85–88 % only if a lot of the energy went in at low amperage.

## How to read it / gotchas

- **Average efficiency ≈ 92–94 %** is normal for AC home charging at a decent
  amperage (≥ 15 A); it dips to ~85–88 % only when much of the charge went in
  at low current (see the reference curve above). The **period
  (week/month/year)** figures are the trustworthy ones.
- **Battery side = power integration** (`total_charge_energy_integrated`,
  `Σ power × Δt` while charging) — finer than 1 % SoC steps (a 1 % step ≈
  0.8 kWh, too coarse for a per-charge comparison). Accuracy depends on the
  poll cadence during charging: more frequent samples = a better integral.
  The SoC-based `total_charge_energy` remains as a coarser fallback.
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
