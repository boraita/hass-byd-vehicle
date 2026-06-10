"""BYD Vehicle integration."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Any

import voluptuous as vol
from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ConfigEntryNotReady, HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_time_interval
from pybyd import BydClient

from .const import (
    CONF_BASE_URL,
    CONF_CONTROL_PIN,
    CONF_COUNTRY_CODE,
    CONF_DEVICE_PROFILE,
    CONF_GPS_POLL_INTERVAL,
    CONF_LANGUAGE,
    CONF_POLL_INTERVAL,
    DEFAULT_COUNTRY,
    DEFAULT_GPS_POLL_INTERVAL,
    DEFAULT_POLL_INTERVAL,
    DOMAIN,
    MAX_GPS_POLL_INTERVAL,
    MAX_POLL_INTERVAL,
    MIN_GPS_POLL_INTERVAL,
    MIN_POLL_INTERVAL,
    PLATFORMS,
    get_country_connection_settings,
    get_country_connection_settings_by_code,
)
from .coordinator import BydApi, BydDataUpdateCoordinator, BydGpsUpdateCoordinator
from .device_fingerprint import async_generate_device_profile

_LOGGER = logging.getLogger(__name__)

# How often to refresh EnergyConsumption data in the background. The
# getEnergyConsumption endpoint is a single "cold" cloud read — it does NOT
# wake the car (unlike realtime polling), so a periodic refresh costs no
# 12V/HV battery. Energy stats change slowly, so a few hours is plenty.
ENERGY_REFRESH_INTERVAL = timedelta(hours=3)


def _sanitize_interval(value: int, default: int, min_value: int, max_value: int) -> int:
    """Clamp interval values so stale options cannot break scheduling."""
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(min_value, min(max_value, parsed))


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate config entries to latest schema."""
    _LOGGER.debug(
        "Migrating BYD config entry %s from version %s",
        entry.entry_id,
        entry.version,
    )

    if entry.version > 3:
        _LOGGER.error(
            "Cannot migrate BYD config entry %s from version %s",
            entry.entry_id,
            entry.version,
        )
        return False

    if entry.version < 2:
        options = dict(entry.options)

        options.pop("smart_gps_polling", None)
        options.pop("gps_active_interval", None)
        options.pop("gps_inactive_interval", None)

        options[CONF_POLL_INTERVAL] = _sanitize_interval(
            options.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL),
            DEFAULT_POLL_INTERVAL,
            MIN_POLL_INTERVAL,
            MAX_POLL_INTERVAL,
        )
        options[CONF_GPS_POLL_INTERVAL] = _sanitize_interval(
            options.get(CONF_GPS_POLL_INTERVAL, DEFAULT_GPS_POLL_INTERVAL),
            DEFAULT_GPS_POLL_INTERVAL,
            MIN_GPS_POLL_INTERVAL,
            MAX_GPS_POLL_INTERVAL,
        )

        hass.config_entries.async_update_entry(entry, options=options)

    if entry.version < 3:
        data = dict(entry.data)
        raw_country_code = data.get(CONF_COUNTRY_CODE)

        try:
            country_code, language, base_url = get_country_connection_settings_by_code(
                str(raw_country_code)
            )
        except (KeyError, AttributeError):
            country_code, language, base_url = get_country_connection_settings(
                DEFAULT_COUNTRY
            )
            _LOGGER.warning(
                (
                    "Entry %s had unknown country code %s; "
                    "defaulting to %s during migration"
                ),
                entry.entry_id,
                raw_country_code,
                DEFAULT_COUNTRY,
            )

        data[CONF_COUNTRY_CODE] = country_code
        data[CONF_LANGUAGE] = language
        data[CONF_BASE_URL] = base_url

        new_unique_id = entry.unique_id
        username = data.get("username")
        if isinstance(username, str) and username:
            new_unique_id = f"{username}@{base_url}"

        hass.config_entries.async_update_entry(
            entry,
            data=data,
            unique_id=new_unique_id,
        )

    _LOGGER.debug("Migration of BYD config entry %s complete", entry.entry_id)
    return True


def _apply_poll_intervals_from_options(
    entry: ConfigEntry,
    entry_data: dict[str, Any],
) -> None:
    """Apply poll intervals from entry options to all coordinators."""
    poll_interval = _sanitize_interval(
        entry.options.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL),
        DEFAULT_POLL_INTERVAL,
        MIN_POLL_INTERVAL,
        MAX_POLL_INTERVAL,
    )
    gps_interval = _sanitize_interval(
        entry.options.get(CONF_GPS_POLL_INTERVAL, DEFAULT_GPS_POLL_INTERVAL),
        DEFAULT_GPS_POLL_INTERVAL,
        MIN_GPS_POLL_INTERVAL,
        MAX_GPS_POLL_INTERVAL,
    )

    for coordinator in entry_data.get("coordinators", {}).values():
        coordinator.set_poll_interval(poll_interval)
    for gps_coordinator in entry_data.get("gps_coordinators", {}).values():
        gps_coordinator.set_poll_interval(gps_interval)


async def _async_handle_entry_update(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle config entry option updates."""
    entry_data = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if entry_data is None:
        return

    previous_options = entry_data.get("options_snapshot", {})
    current_options = dict(entry.options)
    entry_data["options_snapshot"] = current_options

    changed_keys = {
        key
        for key in set(previous_options) | set(current_options)
        if previous_options.get(key) != current_options.get(key)
    }
    poll_keys = {CONF_POLL_INTERVAL, CONF_GPS_POLL_INTERVAL}

    if changed_keys and changed_keys.issubset(poll_keys):
        _apply_poll_intervals_from_options(entry, entry_data)
        return

    await hass.config_entries.async_reload(entry.entry_id)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up BYD Vehicle from a config entry."""
    _LOGGER.debug("Setting up BYD config entry %s", entry.entry_id)
    hass.data.setdefault(DOMAIN, {})

    # Dismiss any stale PIN-invalid notification from a prior run.
    notification_id = f"{DOMAIN}_{entry.entry_id}_pin_invalid"
    persistent_notification.async_dismiss(hass, notification_id)

    # Ensure a device fingerprint exists (backfill for pre-existing entries)
    if CONF_DEVICE_PROFILE not in entry.data:
        hass.config_entries.async_update_entry(
            entry,
            data={
                **entry.data,
                CONF_DEVICE_PROFILE: await async_generate_device_profile(hass),
            },
        )

    session = async_get_clientsession(hass)
    api = BydApi(hass, entry, session)

    poll_interval = _sanitize_interval(
        entry.options.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL),
        DEFAULT_POLL_INTERVAL,
        MIN_POLL_INTERVAL,
        MAX_POLL_INTERVAL,
    )
    gps_interval = _sanitize_interval(
        entry.options.get(CONF_GPS_POLL_INTERVAL, DEFAULT_GPS_POLL_INTERVAL),
        DEFAULT_GPS_POLL_INTERVAL,
        MIN_GPS_POLL_INTERVAL,
        MAX_GPS_POLL_INTERVAL,
    )

    async def _fetch_vehicles(client: BydClient) -> list:
        return await client.get_vehicles()

    vehicles = await api.async_call(_fetch_vehicles)
    if not vehicles:
        raise ConfigEntryNotReady("No vehicles available for this account")

    _LOGGER.debug(
        "Discovered %s BYD vehicle(s) for entry %s",
        len(vehicles),
        entry.entry_id,
    )

    # Verify command access when a control PIN is configured.
    if entry.data.get(CONF_CONTROL_PIN):
        pin_ok = await api.async_verify_commands(vehicles[0].vin)
        if not pin_ok:
            persistent_notification.async_create(
                hass,
                (
                    "The Control PIN is incorrect or cloud control is "
                    "temporarily locked. Remote control actions are disabled. "
                    "Please reconfigure the integration to update your "
                    "Control PIN."
                ),
                title="BYD Vehicle: Command PIN invalid",
                notification_id=notification_id,
            )

    coordinators: dict[str, BydDataUpdateCoordinator] = {}
    gps_coordinators: dict[str, BydGpsUpdateCoordinator] = {}

    for vehicle in vehicles:
        vin = vehicle.vin
        telemetry_coordinator = BydDataUpdateCoordinator(
            hass,
            api,
            vehicle,
            vin,
            poll_interval,
        )
        gps_coordinator = BydGpsUpdateCoordinator(
            hass,
            api,
            vehicle,
            vin,
            gps_interval,
            telemetry_coordinator=telemetry_coordinator,
        )
        coordinators[vin] = telemetry_coordinator
        gps_coordinators[vin] = gps_coordinator

    # Wire MQTT push early so vehicleInfo messages arriving during the
    # first refresh are dispatched to coordinators instead of being dropped.
    api.register_coordinators(coordinators, gps_coordinators)

    try:
        # Bind BydCar instances up front (capability fetch) so the GPS
        # coordinators, which borrow the telemetry coordinator's car, can
        # run their first refresh in parallel with telemetry instead of
        # racing it.  With a sleeping car each trigger+poll cycle can take
        # ~25s (MQTT wait + HTTP poll fallback); running telemetry and GPS
        # concurrently roughly halves worst-case setup time.
        _LOGGER.debug("Binding BYD car instances before first refresh")
        for coordinator in coordinators.values():
            await coordinator.async_ensure_car()

        _LOGGER.debug("Running first refresh for telemetry + GPS in parallel")
        await asyncio.gather(
            *(c.async_config_entry_first_refresh() for c in coordinators.values()),
            *(g.async_config_entry_first_refresh() for g in gps_coordinators.values()),
        )
    except Exception as exc:  # noqa: BLE001
        raise ConfigEntryNotReady from exc

    # One-shot energy fetch so the EnergyConsumption-backed sensors are
    # populated at startup. Subsequent refreshes are user-driven via the
    # ``Fetch energy data`` button or ``byd_vehicle.fetch_energy`` service
    # — energy data changes slowly and the cloud rate-limits the endpoint.
    _LOGGER.debug("Running initial energy fetch for BYD coordinators")
    for vin, coordinator in coordinators.items():
        try:
            await coordinator.async_fetch_energy()
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug(
                "Initial energy fetch failed (will populate on next press): "
                "vin=%s error=%s",
                vin,
                exc,
            )

    # Periodic background refresh of EnergyConsumption data. getEnergyConsumption
    # is a single cold cloud read that does NOT wake the car, so this is safe to
    # run on a timer regardless of sleep/polling state. The handle is registered
    # via async_on_unload so it is cancelled cleanly when the entry unloads.
    async def _async_refresh_energy(_now: Any) -> None:
        for refresh_vin, refresh_coordinator in coordinators.items():
            try:
                await refresh_coordinator.async_fetch_energy()
            except Exception as exc:  # noqa: BLE001
                _LOGGER.debug(
                    "Periodic energy refresh failed: vin=%s error=%s",
                    refresh_vin,
                    exc,
                )

    entry.async_on_unload(
        async_track_time_interval(hass, _async_refresh_energy, ENERGY_REFRESH_INTERVAL)
    )

    # One-shot charging fetch so the homePage-backed sensors are
    # populated at startup. Pulls live state AND schedule from one
    # /control/smartCharge/homePage call — MQTT updates the live state
    # afterwards, but the schedule is HTTP-only so without this the
    # schedule sensors stay unavailable until the user presses the
    # ``Fetch charging`` button.
    _LOGGER.debug("Running initial charging fetch for BYD coordinators")
    for vin, coordinator in coordinators.items():
        try:
            await coordinator.async_fetch_charging()
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug(
                "Initial charging fetch failed "
                "(will populate on next press): vin=%s error=%s",
                vin,
                exc,
            )

    hass.data[DOMAIN][entry.entry_id] = {
        "api": api,
        "coordinators": coordinators,
        "gps_coordinators": gps_coordinators,
        "options_snapshot": dict(entry.options),
    }

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # --- Register domain services (once, on first entry) ---
    _async_register_services(hass)

    entry.async_on_unload(entry.add_update_listener(_async_handle_entry_update))
    _LOGGER.debug("BYD config entry %s setup complete", entry.entry_id)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    _LOGGER.debug("Unloading BYD config entry %s", entry.entry_id)
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        entry_data = hass.data[DOMAIN].pop(entry.entry_id, None)
        if entry_data and "api" in entry_data:
            await entry_data["api"].async_shutdown()
        _LOGGER.debug("Unloaded BYD config entry %s", entry.entry_id)
        # Unregister services when no entries remain.
        if not hass.data.get(DOMAIN):
            _async_unregister_services(hass)
    else:
        _LOGGER.debug("BYD config entry %s unload returned False", entry.entry_id)
    return unload_ok


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload config entry."""
    _LOGGER.debug("Reloading BYD config entry %s", entry.entry_id)
    await hass.config_entries.async_reload(entry.entry_id)


# ------------------------------------------------------------------
# Service helpers
# ------------------------------------------------------------------

_SERVICE_FETCH_REALTIME = "fetch_realtime"
_SERVICE_FETCH_GPS = "fetch_gps"
_SERVICE_FETCH_HVAC = "fetch_hvac"
_SERVICE_FETCH_CHARGING = "fetch_charging"
_SERVICE_FETCH_ENERGY = "fetch_energy"
_SERVICE_START_CHARGING = "start_charging"
_SERVICE_SAVE_CHARGING_SCHEDULE = "save_charging_schedule"
_SERVICE_REFRESH_FIRMWARE = "refresh_firmware_metadata"
_SERVICE_FORCE_POLL = "force_poll_now"
_SERVICE_SCHEDULE_CLIMATE = "schedule_climate"

# Repeat-mode → BYD ``chargeWay`` wire format.
_REPEAT_TO_CHARGE_WAY: dict[str, str] = {
    "single": "s",
    "every_day": "e",
    "weekdays": "0,1,2,3,4",
    "weekends": "5,6",
}
# Day-of-week token → BYD index (``0`` = Monday).
_WEEKDAY_INDEX: dict[str, int] = {
    "mon": 0,
    "tue": 1,
    "wed": 2,
    "thu": 3,
    "fri": 4,
    "sat": 5,
    "sun": 6,
}

_ALL_SERVICES = (
    _SERVICE_FETCH_REALTIME,
    _SERVICE_FETCH_GPS,
    _SERVICE_FETCH_HVAC,
    _SERVICE_FETCH_CHARGING,
    _SERVICE_FETCH_ENERGY,
    _SERVICE_START_CHARGING,
    _SERVICE_SAVE_CHARGING_SCHEDULE,
    _SERVICE_REFRESH_FIRMWARE,
    _SERVICE_FORCE_POLL,
    _SERVICE_SCHEDULE_CLIMATE,
)

# Schemas validate service input up front so handlers can assume shape:
# device_id is always a list[str], times are datetime.time, repeat is a
# known token.  Bad input surfaces as a proper validation error instead
# of a stray ValueError mid-handler.
_SERVICE_BASE_SCHEMA = vol.Schema(
    {vol.Required("device_id"): vol.All(cv.ensure_list, [cv.string])}
)

_SERVICE_SCHEDULE_CLIMATE_SCHEMA = _SERVICE_BASE_SCHEMA.extend(
    {
        vol.Optional("temperature", default=21.0): vol.All(
            vol.Coerce(float), vol.Range(min=16, max=30)
        ),
        vol.Optional("duration", default=20): vol.All(
            vol.Coerce(int), vol.Range(min=5, max=60)
        ),
        vol.Optional("booking_time"): cv.datetime,
    }
)

_SERVICE_SAVE_CHARGING_SCHEDULE_SCHEMA = _SERVICE_BASE_SCHEMA.extend(
    {
        vol.Required("start_time"): cv.time,
        vol.Optional("end_time"): cv.time,
        vol.Optional("until_full", default=True): cv.boolean,
        vol.Optional("repeat", default="every_day"): vol.In(
            (*_REPEAT_TO_CHARGE_WAY, "custom")
        ),
        vol.Optional("weekdays"): vol.All(cv.ensure_list, [vol.In(_WEEKDAY_INDEX)]),
        vol.Optional("enabled", default=True): cv.boolean,
    }
)


def _resolve_charge_way(call: ServiceCall) -> str:
    """Map service-call ``repeat`` (+ optional ``weekdays``) to ``chargeWay``.

    The schema already guarantees ``repeat`` and ``weekdays`` hold known
    tokens; only the custom-without-weekdays combination needs a check.
    """
    repeat: str = call.data["repeat"]
    if repeat == "custom":
        weekdays: list[str] = call.data.get("weekdays") or []
        if not weekdays:
            raise HomeAssistantError("weekdays must be set when repeat='custom'")
        indices = sorted({_WEEKDAY_INDEX[d] for d in weekdays})
        return ",".join(str(i) for i in indices)
    return _REPEAT_TO_CHARGE_WAY[repeat]


def _resolve_vins_from_call(
    hass: HomeAssistant,
    call: ServiceCall,
) -> list[tuple[str, str]]:
    """Resolve (entry_id, vin) pairs from device targets in a service call.

    Raises ``HomeAssistantError`` when no valid targets can be resolved.
    """
    device_ids: list[str] = call.data["device_id"]

    dev_reg = dr.async_get(hass)
    results: list[tuple[str, str]] = []

    for device_id in device_ids:
        device = dev_reg.async_get(device_id)
        if device is None:
            continue
        for identifier in device.identifiers:
            if identifier[0] == DOMAIN:
                vin = identifier[1]
                # Find which config entry owns this VIN.
                for entry_id, entry_data in hass.data.get(DOMAIN, {}).items():
                    coordinators = entry_data.get("coordinators", {})
                    if vin in coordinators:
                        results.append((entry_id, vin))
                        break

    if not results:
        raise HomeAssistantError("No BYD vehicle devices found for the given targets")
    return results


def _get_coordinators(
    hass: HomeAssistant,
    entry_id: str,
    vin: str,
) -> tuple[BydDataUpdateCoordinator, BydGpsUpdateCoordinator | None]:
    """Return (telemetry, gps) coordinators for an entry/vin pair."""
    entry_data: dict[str, Any] = hass.data[DOMAIN][entry_id]
    telemetry: BydDataUpdateCoordinator = entry_data["coordinators"][vin]
    gps: BydGpsUpdateCoordinator | None = entry_data.get("gps_coordinators", {}).get(
        vin
    )
    return telemetry, gps


def _async_register_services(hass: HomeAssistant) -> None:
    """Register domain services (idempotent — safe to call multiple times)."""

    if hass.services.has_service(DOMAIN, _SERVICE_FETCH_REALTIME):
        return  # Already registered.

    def _per_vehicle(
        action: Callable[
            [BydDataUpdateCoordinator, BydGpsUpdateCoordinator | None],
            Awaitable[Any],
        ],
    ) -> Callable[[ServiceCall], Awaitable[None]]:
        """Build a handler that runs *action* for every targeted vehicle."""

        async def _handler(call: ServiceCall) -> None:
            for entry_id, vin in _resolve_vins_from_call(hass, call):
                telemetry, gps = _get_coordinators(hass, entry_id, vin)
                await action(telemetry, gps)

        return _handler

    async def _do_fetch_gps(
        _telemetry: BydDataUpdateCoordinator,
        gps: BydGpsUpdateCoordinator | None,
    ) -> None:
        if gps is not None:
            await gps.async_fetch_gps()

    async def _do_force_poll(
        telemetry: BydDataUpdateCoordinator,
        gps: BydGpsUpdateCoordinator | None,
    ) -> None:
        await telemetry.async_force_poll_now()
        if gps is not None:
            await gps.async_fetch_gps()

    async def _handle_schedule_climate(call: ServiceCall) -> None:
        booking_time: datetime | None = call.data.get("booking_time")
        booking_time_iso = booking_time.isoformat() if booking_time else None
        for entry_id, vin in _resolve_vins_from_call(hass, call):
            telemetry, _ = _get_coordinators(hass, entry_id, vin)
            await telemetry.async_schedule_climate(
                temperature=call.data["temperature"],
                duration=call.data["duration"],
                booking_time_iso=booking_time_iso,
            )

    async def _handle_save_charging_schedule(call: ServiceCall) -> None:
        # Resolve targets first so the input-shape errors below fire on
        # the first device only — they're identical for every target.
        targets = _resolve_vins_from_call(hass, call)

        start_charge_time = call.data["start_time"].strftime("%H:%M")
        if call.data["until_full"]:
            end_charge_time = "full"
        else:
            end_time = call.data.get("end_time")
            if end_time is None:
                raise HomeAssistantError(
                    "end_time is required when until_full is false"
                )
            end_charge_time = end_time.strftime("%H:%M")
        charge_way = _resolve_charge_way(call)

        for entry_id, vin in targets:
            telemetry, _ = _get_coordinators(hass, entry_id, vin)
            await telemetry.async_save_charging_schedule(
                start_charge_time=start_charge_time,
                end_charge_time=end_charge_time,
                charge_way=charge_way,
                enabled=call.data["enabled"],
            )

    # Single source of truth: service name -> (handler, schema).  Order
    # mirrors _ALL_SERVICES, which _async_unregister_services iterates.
    services: dict[str, tuple[Callable[[ServiceCall], Awaitable[None]], vol.Schema]] = {
        _SERVICE_FETCH_REALTIME: (
            _per_vehicle(lambda t, _g: t.async_fetch_realtime()),
            _SERVICE_BASE_SCHEMA,
        ),
        _SERVICE_FETCH_GPS: (_per_vehicle(_do_fetch_gps), _SERVICE_BASE_SCHEMA),
        _SERVICE_FETCH_HVAC: (
            _per_vehicle(lambda t, _g: t.async_fetch_hvac()),
            _SERVICE_BASE_SCHEMA,
        ),
        _SERVICE_FETCH_CHARGING: (
            _per_vehicle(lambda t, _g: t.async_fetch_charging()),
            _SERVICE_BASE_SCHEMA,
        ),
        _SERVICE_FETCH_ENERGY: (
            _per_vehicle(lambda t, _g: t.async_fetch_energy()),
            _SERVICE_BASE_SCHEMA,
        ),
        _SERVICE_START_CHARGING: (
            _per_vehicle(lambda t, _g: t.async_start_charging()),
            _SERVICE_BASE_SCHEMA,
        ),
        _SERVICE_SAVE_CHARGING_SCHEDULE: (
            _handle_save_charging_schedule,
            _SERVICE_SAVE_CHARGING_SCHEDULE_SCHEMA,
        ),
        _SERVICE_REFRESH_FIRMWARE: (
            _per_vehicle(lambda t, _g: t.async_refresh_firmware_metadata()),
            _SERVICE_BASE_SCHEMA,
        ),
        _SERVICE_FORCE_POLL: (_per_vehicle(_do_force_poll), _SERVICE_BASE_SCHEMA),
        _SERVICE_SCHEDULE_CLIMATE: (
            _handle_schedule_climate,
            _SERVICE_SCHEDULE_CLIMATE_SCHEMA,
        ),
    }

    for service, (handler, schema) in services.items():
        hass.services.async_register(DOMAIN, service, handler, schema=schema)

    _LOGGER.debug("Registered %s domain services", len(services))


def _async_unregister_services(hass: HomeAssistant) -> None:
    """Remove domain services when the last config entry is unloaded."""
    for service in _ALL_SERVICES:
        hass.services.async_remove(DOMAIN, service)
    _LOGGER.debug("Unregistered %s domain services", len(_ALL_SERVICES))
