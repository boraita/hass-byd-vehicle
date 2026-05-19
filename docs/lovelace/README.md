# Lovelace presets

Ready-to-paste card configs for the BYD Vehicle integration. Tested with
Sealion 7 Comfort 2024 EU (`hass-byd-vehicle 0.0.80-sealion.6`+).

Drop into **Settings → Dashboards → Edit → Raw configuration editor** or
the relevant view. Each card stands alone; mix and match.

> All entity IDs follow the pattern `sensor.byd_sealion_7_<key>` — replace
> the `sealion_7` slug with whatever your vehicle's auto-alias produced
> in HA. You can find your slugs under Settings → Devices & Services →
> BYD Vehicle → click your car.

## Cards

- [`car_summary.yaml`](./car_summary.yaml) — at-a-glance: SoC, range,
  location, online, plug, doors, charge session phase.
- [`charging.yaml`](./charging.yaml) — full charging dashboard: schedule,
  session metrics (duration, soc added, time until full), V2C status if
  you have a separate wallbox integration.
- [`climate.yaml`](./climate.yaml) — climate control + seat heating
  selects + cabin/exterior temp + tire pressures.
- [`diagnostics.yaml`](./diagnostics.yaml) — health: cloud responsive,
  last MQTT push, last successful fetch, effective poll interval, T-Box
  version.

## Events you can use in automations

- `byd_vehicle_phase_changed` — fires whenever the charge session phase
  transitions (unplugged → plugged_idle → handshake_locked → charging →
  charge_complete). Payload: `{vin, previous_phase, new_phase}`.
- `byd_vehicle_firmware_changed` — fires when `tbox_version` transitions
  (will be repointed to `config_version` once pyBYD#20 exposes it).
- `byd_vehicle_command_lifecycle` — fires on every remote-control command
  with status (queued, settled, failed).

## Suggested automation

```yaml
alias: "BYD: Notify when charging starts"
trigger:
  - platform: event
    event_type: byd_vehicle_phase_changed
    event_data:
      new_phase: charging
action:
  - service: notify.mobile_app_<your_phone>
    data:
      title: "BYD started charging"
      message: |
        Phase: {{ trigger.event.data.new_phase }}
        SoC: {{ states('sensor.byd_sealion_7_battery_level') }}%
        ETA full: {{ states('sensor.byd_sealion_7_time_until_full') }} min
mode: single
```
