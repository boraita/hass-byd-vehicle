"""Sensors for BYD Vehicle."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from homeassistant.components.sensor import (
    RestoreSensor,
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    PERCENTAGE,
    EntityCategory,
    UnitOfEnergy,
    UnitOfLength,
    UnitOfPower,
    UnitOfPressure,
    UnitOfSpeed,
    UnitOfTemperature,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from pybyd.models.realtime import TirePressureUnit
from pybyd.models.vehicle import EnergyType, Vehicle

from .const import DOMAIN
from .coordinator import BydDataUpdateCoordinator
from .entity import BydVehicleEntity

# ---------------------------------------------------------------------------
# Simple presentation-level validators (pyBYD state engine handles deeper
# quality guards; these cover HA display edge-cases only).
# ---------------------------------------------------------------------------

FieldValidator = Callable[[Any, Any], Any]


def keep_previous_when_zero(previous: Any, current: Any) -> Any:
    """Return *previous* when *current* is zero or None.

    Prevents transient ``0 %`` SOC values from showing in the HA UI
    when the vehicle sends stale/invalid telemetry.
    """
    if current is None or current == 0:
        return previous
    return current


def _normalize_epoch(value: Any) -> datetime | None:
    """Ensure a pre-parsed BydTimestamp is UTC-aware, or return None."""
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value
    return None


def _normalize_gps_timestamp(
    gps_timestamp: datetime | None,
    realtime_timestamp: datetime | None,
) -> datetime | None:
    """Correct clean whole-hour GPS offsets relative to realtime.

    Some vehicles appear to report GPS timestamps with a timezone-style
    encoding bug that shifts the value by an exact number of whole hours.
    When the delta vs. realtime is within 5 minutes of a whole-hour
    offset, subtract that offset; otherwise preserve the original GPS
    timestamp to avoid over-correcting stale data.
    """
    if gps_timestamp is None or realtime_timestamp is None:
        return gps_timestamp

    delta = gps_timestamp - realtime_timestamp
    whole_hours = round(delta.total_seconds() / 3600)
    if whole_hours == 0:
        return gps_timestamp

    whole_hour_delta = timedelta(hours=whole_hours)
    if abs(delta - whole_hour_delta) > timedelta(minutes=5):
        return gps_timestamp

    return gps_timestamp - whole_hour_delta


@dataclass(frozen=True, kw_only=True)
class BydSensorDescription(SensorEntityDescription):
    """Describe a BYD sensor."""

    source: str = "realtime"
    attr_key: str | None = None
    value_fn: Callable[[Any], Any] | None = None
    unit_fn: Callable[[Any], str | None] | None = None
    state_attrs_fn: Callable[[Any], dict[str, Any]] | None = None
    validator_fn: FieldValidator | None = None
    available_fn: Callable[[Any], bool] | None = None
    """Optional predicate against the source object to mark the entity
    unavailable.  Returning ``False`` produces an HA "unavailable" state
    even when the source object exists (e.g. for the schedule-end-time
    sensor when the schedule is set to charge until full)."""


def _round_int_attr(attr: str) -> Callable[[Any], int | None]:
    """Create a converter that rounds a numeric attribute to an integer."""

    def _convert(obj: Any) -> int | None:
        value = getattr(obj, attr, None)
        if value is None:
            return None
        return int(round(float(value)))

    return _convert


_LEADING_NUMBER_RE = re.compile(r"^\s*(-?\d+(?:\.\d+)?)")


def _parse_numeric_string(attr: str) -> Callable[[Any], float | None]:
    """Create a converter that parses a string attribute to float.

    Returns *None* for sentinel strings like ``"--"`` or non-numeric values.
    The BYD API sends several energy-related fields as strings. Some are
    bare numbers (e.g. ``"29.6"``) while others include unit suffixes
    (e.g. ``"18.4kW·h/100km"``, ``"11.9度/百公里"``). The fallback regex
    extracts the leading numeric portion so both styles parse cleanly.
    """

    def _convert(obj: Any) -> float | None:
        value = getattr(obj, attr, None)
        if value is None or value == "--":
            return None
        try:
            return float(value)
        except (ValueError, TypeError):
            if isinstance(value, str):
                match = _LEADING_NUMBER_RE.match(value)
                if match:
                    try:
                        return float(match.group(1))
                    except ValueError:
                        pass
            return None

    return _convert


def _positive_float_attr(attr: str) -> Callable[[Any], float | None]:
    """Create a converter returning *None* for negative sentinel values.

    The BYD API uses ``-1`` as a "not available" marker for several
    numeric fields (e.g. ``oilEndurance``).
    """

    def _convert(obj: Any) -> float | None:
        value = getattr(obj, attr, None)
        if value is None or value < 0:
            return None
        return float(value)

    return _convert


def _attr_getter(name: str) -> Callable[[Any], Any]:
    """Return a callable that reads attribute *name* from a source object."""

    def _get(obj: Any) -> Any:
        if obj is None:
            return None
        return getattr(obj, name, None)

    return _get


_WEEKDAY_LABELS: tuple[str, ...] = (
    "Mon",
    "Tue",
    "Wed",
    "Thu",
    "Fri",
    "Sat",
    "Sun",
)


def _format_charge_way(value: str | None) -> str | None:
    """Render the BYD ``chargeWay`` token as a human-readable repeat label.

    * ``"s"`` → ``"Single"``
    * ``"e"`` → ``"Every day"``
    * ``"0,1,2,3,4"`` → ``"Weekdays"``
    * ``"5,6"`` → ``"Weekends"``
    * other comma-separated weekday indices → ``"Custom (Mon, Wed, Fri)"``

    Falls back to the raw string for unparseable values so the sensor
    surfaces the truth rather than swallowing unknown formats silently.
    """
    if value is None or not value:
        return None
    if value == "s":
        return "Single"
    if value == "e":
        return "Every day"
    if value == "0,1,2,3,4":
        return "Weekdays"
    if value == "5,6":
        return "Weekends"
    try:
        indices = sorted({int(p.strip()) for p in value.split(",") if p.strip()})
    except ValueError:
        return value
    names = [_WEEKDAY_LABELS[i] for i in indices if 0 <= i < len(_WEEKDAY_LABELS)]
    if not names:
        return value
    return f"Custom ({', '.join(names)})"


def _eq_consumption_value(snap: Any) -> Any:
    """Return the 'Last 50km equivalent consumption' value.

    Prefers ``realtime.eq_consumption`` (the raw MQTT
    ``nearestEnergyConsumption`` summary, populated every poll cycle).
    Falls back to the HTTP equivalent based on the vehicle's
    energyType — ``avg_ev_consumption`` at et=0, otherwise
    ``avg_eq_oil_consumption``.
    """
    if snap is None:
        return None
    rt = getattr(snap, "realtime", None)
    if rt is not None:
        v = getattr(rt, "eq_consumption", None)
        if v is not None:
            return v
    energy = getattr(snap, "energy", None)
    if energy is None:
        return None
    nearest = getattr(energy, "nearest_energy_consumption", None)
    if nearest is None:
        return None
    energy_type = getattr(getattr(snap, "vehicle", None), "energy_type", EnergyType.EV)
    if energy_type in (EnergyType.ICE, EnergyType.HYBRID):
        return getattr(nearest, "avg_eq_oil_consumption", None)
    return getattr(nearest, "avg_ev_consumption", None)


_CHARGING_STATE_LABELS: dict[int, str] = {
    0: "not_charging",
    1: "charging",
    9: "connected_waiting",
    15: "connected",
}


def _charging_state_to_label(charging: Any) -> str | None:
    """Map ``charging.charging_state`` numeric value to a user-facing label."""
    if charging is None:
        return None
    value = getattr(charging, "charging_state", None)
    if value is None:
        return "unknown"
    return _CHARGING_STATE_LABELS.get(value, "unknown")


def _charge_session_phase(snap: Any) -> str | None:
    """Derive a single user-facing phase from charging + realtime + schedule.

    Decision tree (first match wins):
        unplugged                  — connect_state == 0
        plugged_waiting_schedule   — wait_status == 1 AND charging_state != 1
        handshake_locked           — charging_state == 9
        charging                   — charging_state == 1
        charge_complete            — charging_state in (0, 15) AND soc == 100
        plugged_idle               — charging_state in (0, 15) AND plug present
        unknown                    — otherwise
    """
    if snap is None:
        return None
    charging = getattr(snap, "charging", None)
    realtime = getattr(snap, "realtime", None)
    if charging is None and realtime is None:
        return "unknown"
    connect_state = getattr(charging, "connect_state", None) if charging else None
    charging_state = getattr(charging, "charging_state", None) if charging else None
    wait_status = getattr(charging, "wait_status", None) if charging else None
    soc = (getattr(charging, "soc", None) if charging else None) or (
        getattr(realtime, "elec_percent", None) if realtime else None
    )

    if connect_state == 0:
        return "unplugged"
    if wait_status == 1 and charging_state != 1:
        return "plugged_waiting_schedule"
    if charging_state == 9:
        return "handshake_locked"
    if charging_state == 1:
        return "charging"
    if charging_state in (0, 15):
        if soc is not None and soc >= 100:
            return "charge_complete"
        return "plugged_idle"
    return "unknown"


def _eq_consumption_unit(snap: Any) -> Any:
    """Return the matching unit for ``_eq_consumption_value``."""
    if snap is None:
        return None
    rt = getattr(snap, "realtime", None)
    if rt is not None:
        u = getattr(rt, "eq_consumption_unit", None)
        if u:
            return u
    energy = getattr(snap, "energy", None)
    if energy is None:
        return None
    nearest = getattr(energy, "nearest_energy_consumption", None)
    if nearest is None:
        return None
    energy_type = getattr(getattr(snap, "vehicle", None), "energy_type", EnergyType.EV)
    if energy_type in (EnergyType.ICE, EnergyType.HYBRID):
        return getattr(nearest, "oil_unit", None) or None
    return getattr(nearest, "ev_unit", None) or None


def _prefer_rt_then_energy(
    rt_attr: str | None,
    *energy_path: str,
) -> Callable[[Any], Any]:
    """Return ``snap.realtime.<rt_attr>`` if set, else navigate ``snap.energy``.

    The realtime section is updated automatically every poll cycle while the
    energy section is on-demand (via ``Fetch energy data``). Reading from
    realtime first means merged sensors stay fresh between energy fetches;
    falling back to the energy section keeps them populated when realtime
    hasn't carried the value (or returned a sentinel).
    """

    def _convert(snap: Any) -> Any:
        if snap is None:
            return None
        if rt_attr is not None:
            rt = getattr(snap, "realtime", None)
            if rt is not None:
                value = getattr(rt, rt_attr, None)
                if value is not None:
                    return value
        cur: Any = getattr(snap, "energy", None)
        for part in energy_path:
            if cur is None:
                return None
            cur = getattr(cur, part, None)
        return cur

    return _convert


SENSOR_DESCRIPTIONS: tuple[BydSensorDescription, ...] = (
    # =============================================
    # Realtime: primary sensors (enabled by default)
    # =============================================
    BydSensorDescription(
        key="elec_percent",
        source="realtime",
        native_unit_of_measurement=PERCENTAGE,
        suggested_display_precision=0,
        device_class=SensorDeviceClass.BATTERY,
        state_class=SensorStateClass.MEASUREMENT,
        validator_fn=keep_previous_when_zero,
    ),
    BydSensorDescription(
        key="endurance_mileage",
        source="realtime",
        native_unit_of_measurement=UnitOfLength.KILOMETERS,
        suggested_display_precision=0,
        device_class=SensorDeviceClass.DISTANCE,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:map-marker-distance",
        value_fn=_round_int_attr("endurance_mileage"),
    ),
    BydSensorDescription(
        key="total_mileage",
        source="realtime",
        native_unit_of_measurement=UnitOfLength.KILOMETERS,
        suggested_display_precision=0,
        device_class=SensorDeviceClass.DISTANCE,
        state_class=SensorStateClass.TOTAL_INCREASING,
        icon="mdi:counter",
        value_fn=_round_int_attr("total_mileage"),
    ),
    BydSensorDescription(
        key="speed",
        source="realtime",
        native_unit_of_measurement=UnitOfSpeed.KILOMETERS_PER_HOUR,
        suggested_display_precision=0,
        device_class=SensorDeviceClass.SPEED,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    BydSensorDescription(
        key="temp_in_car",
        source="realtime",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        suggested_display_precision=0,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda obj: (
            int(round(obj.temp_in_car)) if obj.temp_in_car is not None else 0
        ),
    ),
    # Tire pressures – unit resolved dynamically from tire_press_unit;
    # kPa is the default because most BYD vehicles report tirePressUnit=3.
    BydSensorDescription(
        key="left_front_tire_pressure",
        source="realtime",
        native_unit_of_measurement=UnitOfPressure.KPA,
        device_class=SensorDeviceClass.PRESSURE,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:car-tire-alert",
    ),
    BydSensorDescription(
        key="right_front_tire_pressure",
        source="realtime",
        native_unit_of_measurement=UnitOfPressure.KPA,
        device_class=SensorDeviceClass.PRESSURE,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:car-tire-alert",
    ),
    BydSensorDescription(
        key="left_rear_tire_pressure",
        source="realtime",
        native_unit_of_measurement=UnitOfPressure.KPA,
        device_class=SensorDeviceClass.PRESSURE,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:car-tire-alert",
    ),
    BydSensorDescription(
        key="right_rear_tire_pressure",
        source="realtime",
        native_unit_of_measurement=UnitOfPressure.KPA,
        device_class=SensorDeviceClass.PRESSURE,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:car-tire-alert",
    ),
    BydSensorDescription(
        key="battery_power",
        attr_key="gl",
        source="realtime",
        native_unit_of_measurement=UnitOfPower.WATT,
        suggested_display_precision=0,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    # =============================================
    # HVAC: primary sensors (enabled by default)
    # =============================================
    BydSensorDescription(
        key="temp_out_car",
        source="hvac",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        suggested_display_precision=0,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_round_int_attr("temp_out_car"),
    ),
    BydSensorDescription(
        key="pm",
        source="hvac",
        native_unit_of_measurement="µg/m³",
        device_class=SensorDeviceClass.PM25,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    # ===========================================================
    # Realtime: disabled by default (diagnostic / secondary data)
    # ===========================================================
    # Alt battery / range fields
    BydSensorDescription(
        key="power_battery",
        source="realtime",
        native_unit_of_measurement=PERCENTAGE,
        device_class=SensorDeviceClass.BATTERY,
        state_class=SensorStateClass.MEASUREMENT,
        entity_registry_enabled_default=False,
        entity_category=EntityCategory.DIAGNOSTIC,
        validator_fn=keep_previous_when_zero,
    ),
    BydSensorDescription(
        key="ev_endurance",
        source="realtime",
        native_unit_of_measurement=UnitOfLength.KILOMETERS,
        suggested_display_precision=0,
        device_class=SensorDeviceClass.DISTANCE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_registry_enabled_default=False,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_round_int_attr("ev_endurance"),
    ),
    BydSensorDescription(
        key="endurance_mileage_v2",
        source="realtime",
        native_unit_of_measurement=UnitOfLength.KILOMETERS,
        suggested_display_precision=0,
        device_class=SensorDeviceClass.DISTANCE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_registry_enabled_default=False,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_round_int_attr("endurance_mileage_v2"),
    ),
    BydSensorDescription(
        key="total_mileage_v2",
        source="realtime",
        native_unit_of_measurement=UnitOfLength.KILOMETERS,
        suggested_display_precision=0,
        device_class=SensorDeviceClass.DISTANCE,
        state_class=SensorStateClass.TOTAL_INCREASING,
        entity_registry_enabled_default=False,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_round_int_attr("total_mileage_v2"),
    ),
    # Charging detail from the smart-charging endpoint.  The ``realtime``
    # payload reports ``chargingState=-1`` permanently on several VINs
    # (notably Sealion 7 EU), so we read the authoritative value from
    # ``/control/smartCharge/homePage`` instead.  Exposed as a readable
    # enum string instead of a raw int so the UI shows "charging" / etc.
    # instead of "1" / "15".  The numeric value is preserved in attributes.
    BydSensorDescription(
        key="charging_state",
        source="charging",
        icon="mdi:ev-station",
        device_class=SensorDeviceClass.ENUM,
        options=[
            "not_charging",
            "charging",
            "connected_waiting",
            "connected",
            "unknown",
        ],
        value_fn=_charging_state_to_label,
        state_attrs_fn=lambda c: (
            {"raw_value": c.charging_state} if c is not None else {}
        ),
        entity_registry_enabled_default=False,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    # High-level session phase: combines ``charging.charging_state`` +
    # ``connect_state`` + ``wait_status`` into a single user-facing enum
    # so dashboards/automations don't have to do the boolean algebra.
    BydSensorDescription(
        key="charge_session_phase",
        name="Charge session phase",
        source="snapshot",
        icon="mdi:ev-station",
        device_class=SensorDeviceClass.ENUM,
        options=[
            "unplugged",
            "plugged_idle",
            "plugged_waiting_schedule",
            "handshake_locked",
            "charging",
            "charge_complete",
            "unknown",
        ],
        value_fn=_charge_session_phase,
    ),
    # T-Box version (from vehicle metadata, refreshable via the
    # ``byd_vehicle.refresh_firmware_metadata`` service).  Exposed as a
    # diagnostic sensor so automations can trigger on its change.  The
    # cloud also fires the :event:`byd_vehicle_firmware_changed` HA event
    # on transition.
    BydSensorDescription(
        key="tbox_version",
        name="T-Box version",
        source="vehicle",
        attr_key="tbox_version",
        icon="mdi:chip",
        entity_registry_enabled_default=False,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    # Capabilities snapshot — exposes the cloud-reported
    # ``vehicleFunLearnInfo`` dict as state attributes.  The dict carries
    # ~46 flags (NFC, OTA, battery heating variants, third row, sentry,
    # etc.) that can change after an OTA — useful for automations that
    # condition on capabilities, and for diagnostics when a feature stops
    # working.  State value is the count of registered capabilities.
    BydSensorDescription(
        key="capabilities",
        name="Capabilities",
        source="vehicle",
        icon="mdi:format-list-checkbox",
        value_fn=lambda v: (
            sum(
                1
                for x in (v.raw.get("vehicleFunLearnInfo") or {}).values()
                if x and x != -1
            )
            if v is not None and isinstance(getattr(v, "raw", None), dict)
            else None
        ),
        state_attrs_fn=lambda v: (
            v.raw.get("vehicleFunLearnInfo") or {}
            if v is not None and isinstance(getattr(v, "raw", None), dict)
            else {}
        ),
        entity_registry_enabled_default=False,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    # Effective poll interval — diagnostic visibility of the adaptive
    # polling decision.  Value is the *current* update_interval the
    # coordinator is using (may be 1×/2×/4×/8× the user-configured base).
    BydSensorDescription(
        key="effective_poll_interval",
        name="Effective poll interval",
        source="coordinator",
        attr_key="effective_poll_interval_seconds",
        native_unit_of_measurement=UnitOfTime.SECONDS,
        icon="mdi:timer-sync",
        entity_registry_enabled_default=False,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    # Trip tracking — baseline captured on power-ON (persisted across HA
    # restarts via Store) plus live distance.  The ON→OFF transition also
    # fires the :event:`byd_vehicle_power_changed` HA event with a full
    # trip summary for automations.
    BydSensorDescription(
        key="trip_started_at",
        name="Trip started at",
        source="coordinator",
        attr_key="trip_started_at",
        device_class=SensorDeviceClass.TIMESTAMP,
        icon="mdi:car-clock",
        entity_registry_enabled_default=False,
    ),
    BydSensorDescription(
        key="trip_start_soc",
        name="Trip start SoC",
        source="coordinator",
        attr_key="trip_start_soc",
        native_unit_of_measurement=PERCENTAGE,
        icon="mdi:battery-high",
        entity_registry_enabled_default=False,
    ),
    BydSensorDescription(
        key="trip_distance",
        name="Trip distance",
        source="coordinator",
        attr_key="trip_distance_km",
        native_unit_of_measurement=UnitOfLength.KILOMETERS,
        device_class=SensorDeviceClass.DISTANCE,
        icon="mdi:map-marker-distance",
        entity_registry_enabled_default=False,
    ),
    # State value is the last trip's distance; the full summary (start/end
    # SoC and odometer, duration, soc_used) rides along as attributes.
    BydSensorDescription(
        key="last_trip_distance",
        name="Last trip distance",
        source="coordinator",
        value_fn=lambda c: (c.last_trip or {}).get("distance_km"),
        state_attrs_fn=lambda c: c.last_trip or {},
        native_unit_of_measurement=UnitOfLength.KILOMETERS,
        device_class=SensorDeviceClass.DISTANCE,
        icon="mdi:map-marker-path",
        entity_registry_enabled_default=False,
    ),
    # Timestamp of the latest charging session start (i.e. the last time
    # ``charging.charging_state`` transitioned to 1).  Cleared on plug-out.
    # Useful for "how long has this charge been running" automations.
    BydSensorDescription(
        key="charge_session_started_at",
        name="Charge session started at",
        source="coordinator",
        attr_key="charge_session_started_at",
        device_class=SensorDeviceClass.TIMESTAMP,
        icon="mdi:clock-start",
        entity_registry_enabled_default=False,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    BydSensorDescription(
        key="charge_session_duration",
        name="Charge session duration",
        source="coordinator",
        attr_key="charge_session_duration_minutes",
        native_unit_of_measurement=UnitOfTime.MINUTES,
        icon="mdi:timer",
        entity_registry_enabled_default=False,
    ),
    BydSensorDescription(
        key="charge_session_soc_added",
        name="Charge session SoC added",
        source="coordinator",
        attr_key="charge_session_soc_added",
        native_unit_of_measurement=PERCENTAGE,
        icon="mdi:battery-arrow-up",
        entity_registry_enabled_default=False,
    ),
    # Energy delivered to the battery this session, derived from the SoC
    # delta times the Sealion 7 Comfort nameplate capacity (82.5 kWh).
    # Works for any charging source — V2C, public AC, public DC — since
    # the cloud reports the SoC field regardless of where the energy
    # came from.  Coarse: rounding error is in the few-hundred-Wh range.
    BydSensorDescription(
        key="charge_session_kwh_added",
        name="Charge session kWh added",
        source="coordinator",
        attr_key="charge_session_kwh_added",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL,
        icon="mdi:lightning-bolt-circle",
        entity_registry_enabled_default=False,
    ),
    BydSensorDescription(
        key="time_until_full",
        name="Time until full",
        source="coordinator",
        attr_key="time_until_full_minutes",
        native_unit_of_measurement=UnitOfTime.MINUTES,
        icon="mdi:battery-clock",
        entity_registry_enabled_default=False,
    ),
    BydSensorDescription(
        key="last_mqtt_push_at",
        name="Last MQTT push at",
        source="coordinator",
        attr_key="last_mqtt_push_at",
        device_class=SensorDeviceClass.TIMESTAMP,
        icon="mdi:cloud-arrow-down",
        entity_registry_enabled_default=False,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    BydSensorDescription(
        key="last_successful_fetch_at",
        name="Last successful fetch at",
        source="coordinator",
        attr_key="last_successful_fetch_at",
        device_class=SensorDeviceClass.TIMESTAMP,
        icon="mdi:cloud-check",
        entity_registry_enabled_default=False,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    # NOTE: ``charge_state`` (from ``realtime``) and ``charging_state`` (from
    # the smart-charging endpoint) report the same numeric value on most
    # VINs, which makes them look duplicated in the UI.  We keep
    # ``charging_state`` as the authoritative source (it's reliable on
    # Sealion 7 EU, see PR_PLAN.md) and drop the ``realtime`` variant.
    # Legacy registry entry cleaned up via
    # ``_LEGACY_SENSOR_UNIQUE_ID_REMOVALS`` below.
    BydSensorDescription(
        key="wait_status",
        source="realtime",
        icon="mdi:timer-sand",
        entity_registry_enabled_default=False,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    BydSensorDescription(
        key="full_hour",
        source="realtime",
        native_unit_of_measurement=UnitOfTime.HOURS,
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:clock-outline",
        entity_registry_enabled_default=False,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    BydSensorDescription(
        key="full_minute",
        source="realtime",
        native_unit_of_measurement=UnitOfTime.MINUTES,
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:clock-outline",
        entity_registry_enabled_default=False,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    BydSensorDescription(
        key="remaining_hours",
        source="realtime",
        native_unit_of_measurement=UnitOfTime.HOURS,
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:clock-outline",
        entity_registry_enabled_default=False,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    BydSensorDescription(
        key="remaining_minutes",
        source="realtime",
        native_unit_of_measurement=UnitOfTime.MINUTES,
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:clock-outline",
        entity_registry_enabled_default=False,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    # Combined ``remaining_hours``/``remaining_minutes`` realtime fields,
    # rendered as a single ``HH:MM`` string.  Only populates while
    # actively charging — both source fields are ``-1`` (sentinel)
    # otherwise, so the sensor stays unavailable instead of showing
    # ``00:00``.  ``full_hour``/``full_minute`` always read ``-1`` even
    # mid-charge per the active-charging capture, so they're unused.
    BydSensorDescription(
        key="charge_remaining_time",
        source="realtime",
        # No ``available_fn``: report ``"00:00"`` when not charging so the
        # sensor stays in a meaningful state instead of jumping to
        # ``unavailable``.  ``remaining_hours``/``remaining_minutes`` are
        # ``-1`` (normalised to ``None``) when no charge session is active.
        value_fn=lambda obj: (
            f"{obj.remaining_hours:02d}:{obj.remaining_minutes:02d}"
            if obj is not None
            and obj.remaining_hours is not None
            and obj.remaining_minutes is not None
            else "00:00"
        ),
        icon="mdi:battery-clock",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    # NOTE: ``total_power`` is reported as ``0.0`` permanently on Sealion 7
    # EU — the BYD cloud does not populate this field for this VIN.  The
    # useful instantaneous-power sensor is ``battery_power`` (reads
    # ``realtime.gl`` instead).  Descriptor omitted; orphan cleanup.
    BydSensorDescription(
        key="nearest_energy_consumption",
        source="realtime",
        icon="mdi:lightning-bolt",
        state_class=SensorStateClass.MEASUREMENT,
        entity_registry_enabled_default=False,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_parse_numeric_string("nearest_energy_consumption"),
    ),
    BydSensorDescription(
        key="recent_50km_energy",
        source="realtime",
        icon="mdi:lightning-bolt",
        state_class=SensorStateClass.MEASUREMENT,
        entity_registry_enabled_default=False,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_parse_numeric_string("recent_50km_energy"),
    ),
    # Fuel (hybrid vehicles)
    BydSensorDescription(
        key="oil_endurance",
        source="realtime",
        native_unit_of_measurement=UnitOfLength.KILOMETERS,
        device_class=SensorDeviceClass.DISTANCE,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:gas-station",
        entity_registry_enabled_default=True,
        value_fn=_round_int_attr("oil_endurance"),
    ),
    BydSensorDescription(
        key="oil_percent",
        source="realtime",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:gas-station",
        entity_registry_enabled_default=True,
    ),
    BydSensorDescription(
        key="total_oil",
        source="realtime",
        icon="mdi:gas-station",
        entity_registry_enabled_default=False,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    # System indicators
    BydSensorDescription(
        key="engine_status",
        source="realtime",
        icon="mdi:engine",
        entity_registry_enabled_default=False,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    BydSensorDescription(
        key="epb",
        source="realtime",
        icon="mdi:car-brake-parking",
        entity_registry_enabled_default=False,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    BydSensorDescription(
        key="ect_value",
        source="realtime",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:coolant-temperature",
        entity_registry_enabled_default=False,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    # =========================================
    # HVAC: standalone sensors (not climate)
    # =========================================
    BydSensorDescription(
        key="refrigerator_state",
        source="hvac",
        icon="mdi:fridge",
        entity_registry_enabled_default=False,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    BydSensorDescription(
        key="refrigerator_door_state",
        source="hvac",
        icon="mdi:fridge",
        entity_registry_enabled_default=False,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    # ==========================================
    # Realtime: additional diagnostic sensors
    #   (disabled by default — raw / unparsed)
    # ==========================================
    BydSensorDescription(
        key="total_energy",
        source="realtime",
        icon="mdi:flash",
        entity_registry_enabled_default=False,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_parse_numeric_string("total_energy"),
    ),
    BydSensorDescription(
        key="nearest_energy_consumption_unit",
        source="realtime",
        icon="mdi:lightning-bolt",
        entity_registry_enabled_default=False,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    BydSensorDescription(
        key="endurance_mileage_v2_unit",
        source="realtime",
        icon="mdi:map-marker-distance",
        entity_registry_enabled_default=False,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    BydSensorDescription(
        key="total_mileage_v2_unit",
        source="realtime",
        icon="mdi:counter",
        entity_registry_enabled_default=False,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    # Charge rate
    BydSensorDescription(
        key="rate",
        source="realtime",
        icon="mdi:ev-station",
        state_class=SensorStateClass.MEASUREMENT,
        entity_registry_enabled_default=False,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    # Energy consumption strings
    BydSensorDescription(
        key="energy_consumption",
        source="realtime",
        icon="mdi:lightning-bolt",
        state_class=SensorStateClass.MEASUREMENT,
        entity_registry_enabled_default=False,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_parse_numeric_string("energy_consumption"),
    ),
    BydSensorDescription(
        key="total_consumption",
        source="realtime",
        icon="mdi:lightning-bolt",
        state_class=SensorStateClass.MEASUREMENT,
        entity_registry_enabled_default=False,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_parse_numeric_string("total_consumption"),
    ),
    BydSensorDescription(
        key="total_consumption_en",
        source="realtime",
        icon="mdi:lightning-bolt",
        state_class=SensorStateClass.MEASUREMENT,
        entity_registry_enabled_default=False,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_parse_numeric_string("total_consumption_en"),
    ),
    # Warning indicators (as numeric sensors)
    BydSensorDescription(
        key="ok_light",
        source="realtime",
        icon="mdi:check-circle",
        entity_registry_enabled_default=False,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    BydSensorDescription(
        key="power_battery_connection",
        source="realtime",
        icon="mdi:battery-alert",
        entity_registry_enabled_default=False,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    BydSensorDescription(
        key="ins",
        source="realtime",
        icon="mdi:shield-car",
        entity_registry_enabled_default=False,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    # Misc
    BydSensorDescription(
        key="repair_mode_switch",
        source="realtime",
        icon="mdi:wrench",
        entity_registry_enabled_default=False,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    BydSensorDescription(
        key="vehicle_time_zone",
        source="realtime",
        icon="mdi:clock-outline",
        entity_registry_enabled_default=False,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    # ==========================================
    # Last updated timestamp
    # ==========================================
    BydSensorDescription(
        key="last_updated",
        source="realtime",
        device_class=SensorDeviceClass.TIMESTAMP,
        icon="mdi:clock-outline",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    BydSensorDescription(
        key="gps_last_updated",
        source="gps",
        device_class=SensorDeviceClass.TIMESTAMP,
        icon="mdi:crosshairs-gps",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    # ==========================================
    # Merged hybrid leg averages (realtime + energy endpoints carry
    # the same value at different update cadences). Each merged sensor
    # prefers the realtime field — updated every poll — and falls back
    # to the energy-endpoint field — refreshed on Fetch energy data.
    # ==========================================
    BydSensorDescription(
        key="last_50km_avg_ev_consumption",
        source="snapshot",
        value_fn=_prefer_rt_then_energy(
            "energy_consumption_ev",
            "nearest_energy_consumption",
            "avg_ev_consumption",
        ),
        unit_fn=_prefer_rt_then_energy(
            "energy_consumption_ev_unit",
            "nearest_energy_consumption",
            "ev_unit",
        ),
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:lightning-bolt",
        entity_registry_enabled_default=False,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    BydSensorDescription(
        key="last_50km_avg_fuel_consumption",
        source="snapshot",
        value_fn=_prefer_rt_then_energy(
            "energy_consumption_fuel",
            "nearest_energy_consumption",
            "avg_oil_consumption",
        ),
        unit_fn=_prefer_rt_then_energy(
            "energy_consumption_fuel_unit",
            "nearest_energy_consumption",
            "oil_unit",
        ),
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:gas-station",
        entity_registry_enabled_default=False,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    BydSensorDescription(
        key="lifetime_avg_ev_consumption",
        source="snapshot",
        value_fn=_prefer_rt_then_energy(
            "total_consumption_en_ev",
            "cumulative_energy_consumption",
            "avg_ev_consumption",
        ),
        unit_fn=_prefer_rt_then_energy(
            "total_consumption_en_ev_unit",
            "cumulative_energy_consumption",
            "ev_unit",
        ),
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:lightning-bolt",
        entity_registry_enabled_default=False,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    BydSensorDescription(
        key="lifetime_avg_fuel_consumption",
        source="snapshot",
        value_fn=_prefer_rt_then_energy(
            "total_consumption_en_fuel",
            "cumulative_energy_consumption",
            "avg_oil_consumption",
        ),
        unit_fn=_prefer_rt_then_energy(
            "total_consumption_en_fuel_unit",
            "cumulative_energy_consumption",
            "oil_unit",
        ),
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:gas-station",
        entity_registry_enabled_default=False,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    # ==========================================
    # EnergyConsumption (getEnergyConsumption only)
    # ==========================================
    BydSensorDescription(
        key="energy_cumulative_total_mileage",
        source="energy_cumulative",
        attr_key="total_mileage",
        native_unit_of_measurement=UnitOfLength.KILOMETERS,
        device_class=SensorDeviceClass.DISTANCE,
        state_class=SensorStateClass.TOTAL_INCREASING,
        icon="mdi:counter",
        entity_registry_enabled_default=False,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    BydSensorDescription(
        key="energy_last_50km_ev_consumption",
        source="energy_nearest",
        attr_key="ev_consumption",
        unit_fn=_attr_getter("ev_value_unit"),
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:lightning-bolt",
        entity_registry_enabled_default=False,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    BydSensorDescription(
        key="energy_last_50km_oil_consumption",
        source="energy_nearest",
        attr_key="oil_consumption",
        unit_fn=_attr_getter("oil_value_unit"),
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:gas-station",
        entity_registry_enabled_default=False,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    BydSensorDescription(
        key="last_50km_avg_eq_consumption",
        source="snapshot",
        value_fn=_eq_consumption_value,
        unit_fn=_eq_consumption_unit,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:fuel",
        entity_registry_enabled_default=False,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    BydSensorDescription(
        key="energy_last_50km_drive_distribution",
        source="energy_nearest",
        attr_key="drive_distribution",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:steering",
        entity_registry_enabled_default=False,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    BydSensorDescription(
        key="energy_last_50km_elect_distribution",
        source="energy_nearest",
        attr_key="elect_distribution",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:lightning-bolt",
        entity_registry_enabled_default=False,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    BydSensorDescription(
        key="energy_last_50km_air_distribution",
        source="energy_nearest",
        attr_key="air_distribution",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:air-conditioner",
        entity_registry_enabled_default=False,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    BydSensorDescription(
        key="energy_last_50km_other_distribution",
        source="energy_nearest",
        attr_key="other_distribution",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:dots-horizontal",
        entity_registry_enabled_default=False,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    # ==========================================
    # EnergyConsumption — 7-day graph series.
    # Exposes today's value (last element) as the sensor state and
    # the full series as ``daily_values`` extra state attribute.
    # ==========================================
    BydSensorDescription(
        key="energy_self_graph_today",
        source="energy_self_graph",
        value_fn=lambda obj: (
            obj.energy_consumption[-1]
            if obj is not None and obj.energy_consumption
            else None
        ),
        unit_fn=_attr_getter("energy_consumption_unit"),
        state_attrs_fn=lambda obj: (
            {
                "daily_values": list(obj.energy_consumption),
            }
            if obj is not None and obj.energy_consumption
            else {}
        ),
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:chart-line",
        entity_registry_enabled_default=False,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    BydSensorDescription(
        key="energy_auto_model_graph_today",
        source="energy_auto_model_graph",
        value_fn=lambda obj: (
            obj.energy_consumption[-1]
            if obj is not None and obj.energy_consumption
            else None
        ),
        unit_fn=_attr_getter("energy_consumption_unit"),
        state_attrs_fn=lambda obj: (
            {
                "daily_values": list(obj.energy_consumption),
            }
            if obj is not None and obj.energy_consumption
            else {}
        ),
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:chart-line",
        entity_registry_enabled_default=False,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    # --- Smart-charging schedule (sourced from /control/smartCharge/homePage) ---
    BydSensorDescription(
        key="scheduled_charge_start_time",
        source="charging_schedule_charge",
        value_fn=lambda obj: (
            obj.start_time.strftime("%H:%M")
            if obj is not None and obj.start_time is not None
            else None
        ),
        icon="mdi:calendar-clock",
        entity_registry_enabled_default=False,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    BydSensorDescription(
        key="scheduled_charge_end_time",
        source="charging_schedule_charge",
        # ``charge_until_full`` flags the wire sentinel ``"full"`` —
        # there's no clock-time end, so surface as unavailable rather
        # than showing a misleading value.
        available_fn=lambda obj: (
            obj is not None and obj.end_time is not None and not obj.charge_until_full
        ),
        value_fn=lambda obj: (
            obj.end_time.strftime("%H:%M")
            if obj is not None and obj.end_time is not None
            else None
        ),
        icon="mdi:calendar-clock",
        entity_registry_enabled_default=False,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    BydSensorDescription(
        key="scheduled_charge_repeat",
        source="charging_schedule_charge",
        value_fn=lambda obj: (
            _format_charge_way(obj.charge_way) if obj is not None else None
        ),
        icon="mdi:calendar-refresh",
        entity_registry_enabled_default=False,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    # =============================================
    # Realtime: previously-unparsed extras
    # =============================================
    # HVAC target temperature the user set inside the car.  Useful for
    # automations that want to detect manual climate changes vs. our
    # remote-control invocations.
    BydSensorDescription(
        key="main_setting_temp",
        name="HVAC target temperature",
        source="realtime",
        attr_key="main_setting_temp_new",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:thermostat",
        entity_registry_enabled_default=False,
    ),
    # Air recirculation mode (external = fresh air, internal = recirculate).
    # The HVAC component already exposes this indirectly but having it as a
    # standalone sensor lets automations key off it (e.g. switch to
    # recirculate when AQI drops outside).
    BydSensorDescription(
        key="air_run_state",
        name="Air recirculation",
        source="realtime",
        value_fn=lambda obj: (
            {1: "external", 2: "internal"}.get(
                getattr(getattr(obj, "air_run_state", None), "value", None)
            )
            if obj is not None
            else None
        ),
        device_class=SensorDeviceClass.ENUM,
        options=["external", "internal"],
        icon="mdi:air-filter",
        entity_registry_enabled_default=False,
    ),
    # Battery preheating state during DC charging.  Distinct from
    # `battery_heating` binary which only reports on/off — this exposes
    # the more granular state code reported by the car.
    BydSensorDescription(
        key="charge_heat_state",
        name="Charge heat state",
        source="realtime",
        attr_key="charge_heat_state",
        icon="mdi:radiator",
        entity_registry_enabled_default=False,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    # Equivalent consumption — BYD's combined kWh/100km figure that
    # mixes electric + (for PHEVs) fuel into a single value.  Already
    # parsed by pyBYD; just wasn't surfaced as a sensor.
    BydSensorDescription(
        key="eq_consumption_raw",
        name="Equivalent consumption (raw)",
        source="realtime",
        attr_key="eq_consumption",
        icon="mdi:lightning-bolt-outline",
        entity_registry_enabled_default=False,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
)


_SENSOR_UNIQUE_ID_MIGRATIONS: dict[str, str] = {
    # Sensors that previously read from ``realtime`` and now source their
    # value from the smart-charging endpoint.  Map legacy suffixes to the new
    # ones so existing users keep their entity_id, history and dashboards.
    "realtime_charging_state": "charging_charging_state",
}

_LEGACY_SENSOR_UNIQUE_ID_REMOVALS: frozenset[str] = frozenset(
    {
        # Sensors whose descriptor was retired in favour of a binary_sensor
        # counterpart that uses the proper device class (problem, plug, …).
        # The entries linger in registry from older versions and only show
        # ``unavailable`` — drop them at setup so the UI stays clean.
        "realtime_left_front_tire_status",
        "realtime_right_front_tire_status",
        "realtime_left_rear_tire_status",
        "realtime_right_rear_tire_status",
        "realtime_tirepressure_system",
        "realtime_power_system",
        # Descriptors that no longer exist in any platform (their realtime
        # field is redundant with other entities).
        "realtime_power_gear",
        "realtime_booking_charge_state",
        # Redundant with ``charging.charging_state`` (the authoritative
        # source on Sealion 7 EU — see PR_PLAN.md).  The realtime variant
        # reports the same number, just sourced from a less reliable field.
        "realtime_charge_state",
        # ``total_power`` raw field is permanently ``0.0`` on Sealion 7 EU;
        # use ``battery_power`` (reads ``gl``) for the real instantaneous
        # power instead.
        "realtime_total_power",
    }
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up BYD sensors from a config entry."""
    data = hass.data[DOMAIN][entry.entry_id]
    coordinators: dict[str, BydDataUpdateCoordinator] = data["coordinators"]
    gps_coordinators = data.get("gps_coordinators", {})

    registry = er.async_get(hass)

    if _SENSOR_UNIQUE_ID_MIGRATIONS:
        for vin in coordinators:
            for old_suffix, new_suffix in _SENSOR_UNIQUE_ID_MIGRATIONS.items():
                old_uid = f"{vin}_{old_suffix}"
                new_uid = f"{vin}_{new_suffix}"
                existing = registry.async_get_entity_id("sensor", DOMAIN, old_uid)
                if existing and not registry.async_get_entity_id(
                    "sensor", DOMAIN, new_uid
                ):
                    registry.async_update_entity(existing, new_unique_id=new_uid)

    for vin in coordinators:
        for suffix in _LEGACY_SENSOR_UNIQUE_ID_REMOVALS:
            stale = registry.async_get_entity_id("sensor", DOMAIN, f"{vin}_{suffix}")
            if stale:
                registry.async_remove(stale)

    entities: list[SensorEntity] = []
    for vin, coordinator in coordinators.items():
        vehicle = coordinator.vehicle
        distribution_supported = coordinator.energy_distribution_supported
        for description in SENSOR_DESCRIPTIONS:
            if description.key == "gps_last_updated":
                gps_coordinator = gps_coordinators.get(vin)
                if gps_coordinator is not None:
                    entities.append(
                        BydSensor(gps_coordinator, vin, vehicle, description)
                    )
                continue
            # Skip per-leg distribution sensors on VINs where the cloud
            # leaves the four ``*Distribution`` fields fixed at ``"--"`` —
            # they would always report ``unknown`` and only clutter the UI.
            if (
                distribution_supported is False
                and description.source == "energy_nearest"
                and description.attr_key
                and description.attr_key.endswith("_distribution")
            ):
                stale_uid = f"{vin}_{description.source}_{description.key}"
                stale = registry.async_get_entity_id("sensor", DOMAIN, stale_uid)
                if stale:
                    registry.async_remove(stale)
                continue
            entities.append(BydSensor(coordinator, vin, vehicle, description))

    async_add_entities(entities)


_TIRE_PRESSURE_KEYS = {
    "left_front_tire_pressure",
    "right_front_tire_pressure",
    "left_rear_tire_pressure",
    "right_rear_tire_pressure",
}

_TIRE_UNIT_MAP = {
    TirePressureUnit.BAR: UnitOfPressure.BAR,
    TirePressureUnit.PSI: UnitOfPressure.PSI,
    TirePressureUnit.KPA: UnitOfPressure.KPA,
}


class BydSensor(BydVehicleEntity, RestoreSensor):
    """Representation of a BYD vehicle sensor.

    All state is read from ``VehicleSnapshot`` sections via the
    base-class ``_get_source_obj()`` helper. The ``_last_native_value``
    cache (used by ``keep_previous_when_zero`` and friends) survives
    restarts and config-entry reloads via :class:`RestoreSensor`, so a
    transient sentinel payload right after a reload doesn't flip a
    previously-known value (e.g. battery 98 %) back to ``unknown``.
    """

    _attr_has_entity_name = True
    entity_description: BydSensorDescription

    def __init__(
        self,
        coordinator: BydDataUpdateCoordinator,
        vin: str,
        vehicle: Vehicle,
        description: BydSensorDescription,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_translation_key = description.key
        self._vin = vin
        self._vehicle = vehicle
        self._attr_unique_id = f"{vin}_{description.source}_{description.key}"
        self._last_native_value: Any | None = None

        # Auto-disable sensors that return no data on first fetch.
        if description.entity_registry_enabled_default is not False:
            if self._resolve_validated_value() is None:
                self._attr_entity_registry_enabled_default = False

    async def async_added_to_hass(self) -> None:
        """Restore last known native value before the coordinator binds.

        The restored value seeds ``_last_native_value`` so that the first
        post-restart coordinator update can still fall through to the
        previous value when the cloud returns a sentinel payload.
        """
        await super().async_added_to_hass()
        if self.entity_description.validator_fn is None:
            return
        last = await self.async_get_last_sensor_data()
        if last is None:
            return
        restored = last.native_value
        if restored is None:
            return
        self._last_native_value = restored

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _resolve_value(self) -> Any:
        """Extract the current value using the description's extraction logic."""
        key = self.entity_description.key

        # Timestamp sensors use the snapshot section's timestamp attribute.
        if key == "last_updated":
            realtime = self._get_realtime()
            if realtime is None:
                return None
            return _normalize_epoch(getattr(realtime, "timestamp", None))

        if key == "gps_last_updated":
            gps = self._get_gps()
            if gps is None:
                return None
            gps_timestamp = _normalize_epoch(getattr(gps, "gps_timestamp", None))
            realtime = self._get_realtime()
            realtime_timestamp = _normalize_epoch(getattr(realtime, "timestamp", None))
            return _normalize_gps_timestamp(gps_timestamp, realtime_timestamp)

        obj = self._get_source_obj(self.entity_description.source)
        if obj is None:
            return None

        if self.entity_description.value_fn is not None:
            return self.entity_description.value_fn(obj)

        attr = self.entity_description.attr_key or key
        value = getattr(obj, attr, None)
        enum_value = getattr(value, "value", None)
        if isinstance(enum_value, int):
            return enum_value
        return value

    def _resolve_validated_value(self) -> Any:
        """Resolve sensor value and apply optional per-entity validation."""
        value = self._resolve_value()
        validator = self.entity_description.validator_fn
        if validator is not None:
            value = validator(self._last_native_value, value)
        if value is not None:
            self._last_native_value = value
        return value

    # ------------------------------------------------------------------
    # Entity properties
    # ------------------------------------------------------------------

    @property
    def available(self) -> bool:
        """Return True when the coordinator has data for this source."""
        if self.entity_description.key in ("last_updated", "gps_last_updated"):
            return super().available and self._resolve_value() is not None
        if not super().available:
            return False
        obj = self._get_source_obj(self.entity_description.source)
        if obj is None:
            return False
        if self.entity_description.available_fn is not None:
            return bool(self.entity_description.available_fn(obj))
        return True

    @property
    def native_unit_of_measurement(self) -> str | None:
        """Return the unit; tyres + per-leg energy fields resolve dynamically."""
        desc_unit = self.entity_description.native_unit_of_measurement
        if self.entity_description.unit_fn is not None:
            obj = self._get_source_obj(self.entity_description.source)
            if obj is not None:
                dynamic_unit = self.entity_description.unit_fn(obj)
                if dynamic_unit:
                    return dynamic_unit
            return desc_unit
        if self.entity_description.key not in _TIRE_PRESSURE_KEYS:
            return desc_unit
        obj = self._get_source_obj(self.entity_description.source)
        if obj is not None:
            api_unit = getattr(obj, "tire_press_unit", None)
            if api_unit is not None:
                return _TIRE_UNIT_MAP.get(api_unit, desc_unit)
        return desc_unit

    @property
    def native_value(self) -> Any:
        """Return the sensor value."""
        return self._resolve_validated_value()

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Merge VIN with description-supplied dynamic attributes."""
        attrs = super().extra_state_attributes
        if self.entity_description.state_attrs_fn is not None:
            obj = self._get_source_obj(self.entity_description.source)
            if obj is not None:
                extra = self.entity_description.state_attrs_fn(obj)
                if extra:
                    attrs = {**attrs, **extra}
        return attrs
