# BYD Sealion 7 Comfort 2024 EU — owner notes

Field-tested observations from running this integration against a BYD Sealion 7 Comfort 2024 EU (Spain) over a 30-day window.  Useful reference for owners of the same trim and a starting point for other Sealion 7 variants.

## Skylight sensor reflects the interior sunroof curtain, not the glass

The glass panoramic roof is fixed on this trim — it does not open.  The interior **motorised sunshade curtain** does, and that is what the `binary_sensor.byd_*_skylight` sensor actually tracks (`off`=closed, `on`=open).

Live test on the Sealion 7 Comfort 2024 EU:

- Open the curtain manually from the headliner button → `skylight` flips to `on`.
- Press **Close windows** in HA → BYD's `CLOSE_WINDOWS` (functionNo 1026) closes the four side windows **and** the sunshade curtain, and `skylight` returns to `off`.

What the API does **not** expose:

- An "open curtain" command.  There is no `OPEN_SUNROOF` / `OPEN_SHADE` capability in this VIN's `cfFixedList`, so the curtain can only be opened from inside the car.
- Independent control of the curtain (close-only piggybacks on `CLOSE_WINDOWS`).

The friendly name for this sensor on our HA install has been renamed to **"Cortinilla techo"** to make the meaning explicit, and the `close_windows` button is labelled **"Cerrar ventanas + cortinilla"** to reflect what it actually does.

## Always-unknown entities (BYD cloud doesn't populate the field)

The following entities are permanently `unknown` because BYD's cloud doesn't populate the underlying field on this trim:

| Entity | Reason |
|---|---|
| `sensor.byd_*_power_battery_level` | Field absent from realtime payload — never observed in 30 days |
| `sensor.byd_*_gps_speed` | BYD returns `null` for `speed` in `/control/getGpsInfo` |

**Hide them** via Settings → Devices → BYD Vehicle device → entities tab → click the entity → toggle **Visible** off (or **Disabled**).

## Sentinel-mode entities (intermittent valid data)

These entities mostly read `unknown` because BYD reports a `-1` sentinel that pyBYD correctly normalises to `None`, but they **do receive real values** occasionally when the cloud delivers a fresh payload.  Leave them enabled.

| Entity | What you'll see |
|---|---|
| `sensor.byd_*_electronic_parking_brake` | Mostly `unknown`, occasional `0` when the cloud actually populates the EPB field |
| `sensor.byd_*_power_battery_connection` | Same pattern — `-1` sentinel most of the time, `0` when populated |

## Charge-context-only entities (correct when not charging)

These entities are `unknown` / `unavailable` **on purpose** when the car isn't actively charging:

| Entity | Available when |
|---|---|
| `sensor.byd_*_charge_curve` | Charging |
| `sensor.byd_*_charge_remaining_hours` / `_minutes` | Charging |
| `sensor.byd_*_charge_session_started_at` | Active session |
| `sensor.byd_*_charge_session_duration` / `_soc_added` / `_kwh_added` | Active session |
| `sensor.byd_*_hours_to_full` / `_minutes_to_full` / `_time_until_full` | Charging with a valid SoC trajectory |
| `button.byd_*_start_charging` | Cable plugged, schedule pending |
| `button.byd_*_stop_charging` | Actively charging |

## Optional-hardware entities (Sealion 7 Comfort doesn't have them)

These entities exist in the integration because other BYD trims have the hardware, but the Comfort EU doesn't:

| Entity | Notes |
|---|---|
| `binary_sensor.byd_*_sliding_door` + `_sliding_door_lock` | No sliding door on Sealion 7 |
| `binary_sensor.byd_*_forehold` | No frunk on Sealion 7 |
| `select.byd_*_rear_left_seat_heat` / `_rear_right_*` (heat & ventilation) | No rear heated/ventilated seats on Comfort trim |
| `binary_sensor.byd_*_third_row_*` | No third row |
| `switch.byd_*_battery_heat` | Battery heating not exposed by BYD for this trim — `batteryHeatState` always `0` |

All of these come with `entity_registry_enabled_default=False`, so they won't appear unless you've enabled them manually.

## Quick clean-up via WebSocket

If you want to hide the two always-unknown entities (`power_battery_level`, `gps_speed`) in one shot rather than via the UI:

```python
# Run from any python3 environment with the websockets module
import asyncio, json, websockets

HA_URL = "wss://ha.example.com/api/websocket"  # ← your HA URL
TOKEN = "<long-lived access token>"

async def main():
    async with websockets.connect(HA_URL, max_size=2**24) as ws:
        await ws.recv()
        await ws.send(json.dumps({"type": "auth", "access_token": TOKEN}))
        await ws.recv()
        for i, entity_id in enumerate([
            "sensor.byd_sealion_7_power_battery_level",
            "sensor.byd_sealion_7_gps_speed",
        ], start=1):
            await ws.send(json.dumps({
                "id": i,
                "type": "config/entity_registry/update",
                "entity_id": entity_id,
                "hidden_by": "user",
            }))
            print(await ws.recv())

asyncio.run(main())
```

To unhide later: Settings → Devices → BYD Vehicle → entities → filter "hidden" → toggle visibility back on.

## See also

- pyBYD API mapping discussion: [jkaberg/pyBYD#20](https://github.com/jkaberg/pyBYD/issues/20)
- Sealion 7 umbrella issue: [jkaberg/hass-byd-vehicle#115](https://github.com/jkaberg/hass-byd-vehicle/issues/115)
