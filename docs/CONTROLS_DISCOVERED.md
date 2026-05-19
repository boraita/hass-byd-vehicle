# Discovered controls — not yet implemented in pyBYD

Capabilities registered in the BYD Sealion 7 Comfort EU's
`vehicle/vehicleswitch/getLatestConfig` response (`cfFixedList`) that
are **not** yet exposed as commands by pyBYD or this integration.

Source: capture from real VIN, dated 2026-05-19. May vary by trim /
region — the table only reflects what this VIN advertises.

| functionNo | code | functionName (zh) | Best-guess `commandType` | Notes |
|---|---|---|---|---|
| 1020 | `OPENTRUNK` | 解锁后备箱 (unlock trunk) | `OPENTRUNK` | Probably no params (analogous to `FINDCAR`) |
| 1021 | `CLOSETRUNK` | 关闭后备箱 (close trunk) | `CLOSETRUNK` | Probably no params |
| 1030 | `One-Tap Prep` | 一键备车 (one-tap prepare car) | `ONETAPPREP` (?) | Composite: A/C + seats + steering. Sub-codes `10300001` (A/C), `10300003` (Seats), `10300004` (Steering wheel heat) |
| 1031 | `One click shutdown` | 一键熄火 (one-tap shutdown) | `ONECLICKSHUTDOWN` (?) | Probably no params |
| 10100001 | (battery heat) | 行车预热 (driving preheat) | `BATTERYHEAT`? | Variant of existing BatteryHeat with mode flag |
| 10100002 | `CHARGINGHEATING` | 充电预热 (charging preheat) | `CHARGINGHEAT` (?) | Pre-warms battery for fast-charge |
| 1009 | `CPD` | 儿童遗留监测CPD (Child Presence Detection) | n/a (read-only?) | Likely no command, just a status flag |
| 1013 + 10130002 | `NFC` / `NFC key 3C` | 数字钥匙 / NFC钥匙 | n/a (pairing flow) | Probably uses `/nfc/app/v1/c3/*` endpoints, not remoteControl |

## Naming-pattern reasoning

Existing pyBYD commands map their `RemoteCommand` enum to the `commandType`
field on the wire. Pattern observation:

| code (Latest Config) | RemoteCommand value (wire) |
|---|---|
| `LOCKING` (1005) | `LOCKDOOR` |
| `UNLOCKING` (1006) | `OPENDOOR` |
| `AIR` (1001) + `A/C` (10300001) | `OPENAIR` / `CLOSEAIR` / `BOOKINGAIR` |
| `FINDCAR` (1007) | `FINDCAR` (exact) |
| `FLASHLIGHTNOWHISTLE` (1008) | `FLASHLIGHTNOWHISTLE` (exact) |
| `CLOSEWINDOWS` (1026) | `CLOSEWINDOW` (singular form) |
| `VENTILATIONHEATING` (1003 family) | `VENTILATIONHEATING` (exact) |
| `BATTERYHEATING` (1010 family) | `BATTERYHEAT` |

Trunk and One-Tap codes follow the "verb + noun" style of the others.
The best-guess values above match the `code` field directly. They will
need to be verified by:

1. MITM capture of the BYD app sending the equivalent command, **or**
2. Trial-and-error against the real cloud (a bad `commandType` returns
   error code 1001 / 1009 from `/control/remoteControl`)

## Implementation order recommended

1. **`TrunkCapability` in pyBYD** (PR #1) — simplest, no params, analogous
   to `FinderCapability`. Estimated 30 lines of code in
   `_capabilities/trunk.py`, 2-3 lines in `car.py`, 1 enum entry in
   `models/control.py`. Then a `button.open_trunk` / `button.close_trunk`
   in this integration.

2. **`OneTapCapability` in pyBYD** (PR #2) — more complex because the
   sub-codes hint at composite params (temp + which seats + steering).
   Worth capturing the BYD app's payload first to nail the schema.

3. **Pre-heat variants** (lower priority) — overlap with existing
   `BatteryHeat`. Possibly just extend the existing capability with a
   `mode` parameter (`driving` vs `charging`).

4. **CPD / NFC** — read-only and out-of-scope respectively.

## Why these aren't trivial to add today

Without a captured request body, we can't know:
- Whether the command takes `controlParams` at all
- The exact `commandPwd` shape vs other commands
- Whether the response uses `/remoteControlResult` polling or a different
  result endpoint
- How the `remote_mode` and `chair_type` fields apply (or don't)

The pattern *should* hold, but BYD has historically named-and-shamed
exceptions (e.g. `CLOSEWINDOW` is singular while the user-facing capability
is plural `CLOSEWINDOWS`).
