# BYD Sealion 7 Comfort 2024 EU — owner notes

Field-tested observations from running this integration against a BYD Sealion 7 Comfort 2024 EU (Spain) over a 30-day window.  Useful reference for owners of the same trim and a starting point for other Sealion 7 variants.

## Discovered BYD endpoints not yet implemented in pyBYD

Catalogue of POST endpoints extracted from the official BYD overseas app
APK ([jkaberg/byd-react-app-reverse#1](https://github.com/jkaberg/byd-react-app-reverse/issues/1)
comment by `zwcall`).  None of these are wired into pyBYD as of writing —
parked here so future research has a starting point instead of an issue.

**Share Access / temporary key lending** (capability flagged in
`vehicleFunLearnInfo.bookingCar = 1`):

- `app/rental/vehicle/bind` — most likely entry point for granting
  someone temporary access to the car ("rental" is BYD's internal
  term for non-primary users).
- `control/appBindingVehicle` — primary owner binding.
- `vehicle/vehicleswitch/updatePermissionInfo` — likely the per-user
  permission mask (geofence, speed cap, time window).

**NFC digital key** (flagged via `nfcLearnInfo`, `nfcDigitalLearnInfo`,
`nfcUwbSwLearnInfo` all = 1 on this VIN):

- `nfc/v2/klist` — list paired keys (GET-equivalent, read-only).
- `nfc/app/v1/c3/pairing` + `pairingpassword` — pair a new key.
- `nfc/app/v1/c3/isPaired` / `isCanPass` / `preopeningCheck` — status
  checks.
- `nfc/app/v1/c3/changeOwnerDevice` — transfer ownership.
- `nfc/app/v1/delete` — revoke a key.

Schemas for the request bodies are not in the public source.  Two safe
discovery paths from here:

1. Decompile the BYD Android app (`com.byd.bydautolink`) with
   `apktool` + `jadx` and grep the Retrofit interfaces for the
   `@POST` definitions above; the parameter classes give the schema.
2. Call the read-only endpoints first (`nfc/v2/klist`,
   `app/rental/vehicle/list` if it exists) — the responses reveal the
   structure without changing any state on the car.

Write endpoints (`bind`, `pairing`, `updatePermissionInfo`,
`changeOwnerDevice`, `delete`) should not be hit blind — a malformed
payload could lock the VIN out of the cloud or grant access to the
wrong recipient.

## No partial / per-window open control

`OPEN_WINDOWS` (functionNo `10020005`) opens **all four windows together** to the same `~10 %` vent crack.  There is no remote command to:

- Open a single window (driver / passenger / either rear).
- Choose the opening percentage (only the fixed `~10 %` crack is exposed).
- Drop windows fully (BYD only exposes the vent crack as a remote operation).

This is confirmed not as a pyBYD limitation but as a hard cap of BYD's cloud API: the **official BYD mobile app cannot do it either** — fully dropping a window or operating individual ones is only available from the physical button switches inside the car.

`vehicleFunLearnInfo` on Sealion 7 Comfort 2024 EU shows a related capability flag — `openWindow499LearnInfo: 1` — whose payload remains undocumented as of 2026-05.  If future research uncovers what the "499" variant accepts (e.g. a percentage or a `windowId`), this is the place to plug a richer command.

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
