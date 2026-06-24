"""Data coordinators for BYD Vehicle."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import perf_counter
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    UpdateFailed,
)
from homeassistant.util import dt as dt_util
from pybyd import (
    BydApiError,
    BydAuthenticationError,
    BydCar,
    BydClient,
    BydControlPasswordError,
    BydDataUnavailableError,
    BydEndpointNotSupportedError,
    BydRateLimitError,
    BydRemoteControlError,
    BydServiceBusyError,
    BydSessionExpiredError,
    BydTransportError,
    CommandAckEvent,
    CommandLifecycleEvent,
    VehicleSnapshot,
)
from pybyd.config import BydConfig, DeviceProfile
from pybyd.models.realtime import PowerGear
from pybyd.models.vehicle import Vehicle

from . import _logic
from .const import (
    CONF_BASE_URL,
    CONF_CONTROL_PIN,
    CONF_COUNTRY_CODE,
    CONF_DEBUG_DUMPS,
    CONF_DEVICE_PROFILE,
    CONF_LANGUAGE,
    DEFAULT_DEBUG_DUMPS,
    DEFAULT_LANGUAGE,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

_HA_EVENT_COMMAND_LIFECYCLE: str = f"{DOMAIN}_command_lifecycle"
# Fired on every MQTT push (event-bus hook so automations can react without
# polling).  Payload: {vin, event, summary}.
_HA_EVENT_PUSH: str = f"{DOMAIN}_push"
# Fired when the vehicle's on/off (power) state transitions.
# Payload: {vin, is_on, state, timestamp, trip_start|trip}.
_HA_EVENT_POWER_CHANGED: str = f"{DOMAIN}_power_changed"
# Fired when the MQTT push channel (re)connects to the BYD cloud.
# Payload: {vin}.  The "service is back up" signal.
_HA_EVENT_CLOUD_ONLINE: str = f"{DOMAIN}_cloud_online"
# Fired when the odometer jumped between two snapshots without us ever seeing
# a power-on cycle — i.e. the car was driven while we were blind (cloud
# blackout or a long sleep-gap between polls). An INFERRED, possibly-aggregate
# trip so downstream consumers don't lose it. Payload below in
# _maybe_detect_offline_drive.
_HA_EVENT_OFFLINE_DRIVE: str = f"{DOMAIN}_offline_drive"
# A drive entirely between two snapshots must move at least this far to be
# inferred (filters odometer jitter); jumps beyond the max are treated as
# sentinel garbage and ignored.
_MIN_OFFLINE_DRIVE_KM: float = 1.0
_MAX_OFFLINE_DRIVE_KM: float = 2000.0
# Below this distance an ON→OFF cycle is a park/maneuver, not a trip — don't
# record it (0 km hops with 0 % SoC were ~27% of recorded "trips").
_MIN_TRIP_KM: float = 1.0
# Below this distance the integer-SoC resolution can't measure energy: a 1 km
# trip crossing a 1 % boundary reads as 0.82 kWh = 82 kWh/100km (or 0). Record
# the distance but leave energy/efficiency unknown rather than publish garbage.
_MIN_ENERGY_TRIP_KM: float = 3.0
# A SoC rise larger than this (%) between samples means a charge happened —
# reset the efficiency window (efficiency measured across a charge is bogus).
_EFFICIENCY_SOC_RISE_RESET = 2.0
# Cap (hours) on the gap between two charge-power samples that we integrate.
# A longer gap = lost samples (polling paused/outage) — skip it rather than
# extrapolate a stale power rectangle across it.
_CHARGE_POWER_MAX_GAP_H = 0.5

# Trip snapshots persist across HA restarts so a restart mid-trip does
# not lose the power-on baseline (SoC/odometer at trip start).
_TRIP_STORE_VERSION = 1
_TRIP_STORE_SAVE_DELAY_SECONDS = 2.0

# Cap the persisted trip history feeding the aggregate-stats sensors.
_TRIP_HISTORY_MAX = 300

# Battery capacity defaults to Sealion 7 Comfort nameplate (82.5 kWh).
# Wrong for other trims/models; swap when pyBYD exposes per-model capacity.
_DEFAULT_BATTERY_KWH: float = 82.5

_AUTH_ERRORS = (BydAuthenticationError, BydSessionExpiredError)
_RECOVERABLE_ERRORS = (
    BydApiError,
    BydTransportError,
    BydRateLimitError,
    BydEndpointNotSupportedError,
)

# Per-event whitelist of fields safe to include in the MQTT diagnostic
# samples surfaced through ``sensor.byd_*_mqtt_event_log``.  Anything not
# listed here is dropped — keeps personal data (location, VIN tail, etc.)
# out of the sensor history while still giving enough signal to diagnose
# command results and state-machine transitions.
_MQTT_SAMPLE_FIELDS: dict[str, tuple[str, ...]] = {
    "remoteControl": ("res", "requestSerial"),
    "vehicleInfo": (
        "chargingState",
        "chargeState",
        "connectState",
        "elecPercent",
        "vehicleState",
        "onlineState",
    ),
}


def _extract_mqtt_sample_fields(event: str, data: Any) -> dict[str, Any]:
    """Pick the whitelisted summary fields for an MQTT event payload."""
    allowed = _MQTT_SAMPLE_FIELDS.get(event)
    if not allowed or not isinstance(data, dict):
        return {}
    return {key: data[key] for key in allowed if key in data}


class BydApi:
    """Thin wrapper around the pybyd client.

    Manages client lifecycle, exception translation, MQTT callback wiring,
    and debug dump writing.
    """

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, session: Any) -> None:
        self._hass = hass
        self._entry = entry
        self._http_session = session
        time_zone = hass.config.time_zone or "UTC"
        device = DeviceProfile(**entry.data[CONF_DEVICE_PROFILE])
        self._config = BydConfig(
            username=entry.data["username"],
            password=entry.data["password"],
            base_url=entry.data[CONF_BASE_URL],
            country_code=entry.data.get(CONF_COUNTRY_CODE, "NL"),
            language=entry.data.get(CONF_LANGUAGE, DEFAULT_LANGUAGE),
            time_zone=time_zone,
            device=device,
            control_pin=entry.data.get(CONF_CONTROL_PIN) or None,
            # Default is 10s; with a sleeping car every trigger+poll waits
            # the full window before the HTTP fallback, so cold-start pays
            # it twice (realtime + GPS).  5s keeps the fast path for awake
            # cars and fails over sooner otherwise.
            mqtt_timeout=5.0,
        )
        self._client: BydClient | None = None
        self._commands_enabled: bool = False
        self._commands_failed_reason: str | None = None
        self._verified_vin: str | None = None
        self._debug_dumps_enabled = entry.options.get(
            CONF_DEBUG_DUMPS,
            DEFAULT_DEBUG_DUMPS,
        )
        self._debug_dump_dir = Path(hass.config.path(".storage/byd_vehicle_debug"))
        self._coordinators: dict[str, BydDataUpdateCoordinator] = {}
        self._gps_coordinators: dict[str, BydGpsUpdateCoordinator] = {}
        self._mqtt_event_counters: dict[str, int] = {}
        self._mqtt_event_samples: list[dict[str, Any]] = []
        _LOGGER.debug(
            "BYD API initialized: entry_id=%s, region=%s, language=%s",
            entry.entry_id,
            entry.data[CONF_BASE_URL],
            entry.data.get(CONF_LANGUAGE, DEFAULT_LANGUAGE),
        )

    # ------------------------------------------------------------------
    # Debug dumps
    # ------------------------------------------------------------------

    def _write_debug_dump(self, category: str, payload: dict[str, Any]) -> None:
        if not self._debug_dumps_enabled:
            return
        try:
            self._debug_dump_dir.mkdir(parents=True, exist_ok=True)
            timestamp = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%S%fZ")
            file_path = self._debug_dump_dir / f"{timestamp}_{category}.json"
            file_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2, default=str),
                encoding="utf-8",
            )
        except Exception:  # noqa: BLE001
            _LOGGER.debug("Failed to write BYD debug dump.", exc_info=True)

    async def _async_write_debug_dump(
        self,
        category: str,
        payload: dict[str, Any],
    ) -> None:
        await self._hass.async_add_executor_job(
            self._write_debug_dump, category, payload
        )

    # ------------------------------------------------------------------
    # pyBYD callbacks
    # ------------------------------------------------------------------

    def _handle_mqtt_event(
        self, event: str, vin: str, respond_data: dict[str, Any]
    ) -> None:
        """Handle generic MQTT events from pyBYD.

        Captures per-event counters plus a rolling window of lightweight
        samples (timestamp, event name, key list, and a whitelisted value
        summary) for the diagnostic ``mqtt_event_log`` sensor.  Full payloads
        only land on disk when debug dumps are enabled.
        """
        self._mqtt_event_counters[event] = self._mqtt_event_counters.get(event, 0) + 1
        keys = sorted(respond_data.keys()) if isinstance(respond_data, dict) else []
        self._mqtt_event_samples.append(
            {
                "t": datetime.now(tz=UTC).isoformat(),
                "event": event,
                "vin": vin[-6:] if vin else "-",
                "keys": keys[:20],
                "summary": _extract_mqtt_sample_fields(event, respond_data),
            }
        )
        if len(self._mqtt_event_samples) > 50:
            self._mqtt_event_samples = self._mqtt_event_samples[-50:]
        # Event-bus hook: let automations react to any push instantly,
        # without polling the car.
        self._hass.bus.async_fire(
            _HA_EVENT_PUSH,
            {
                "vin": vin,
                "event": event,
                "summary": _extract_mqtt_sample_fields(event, respond_data),
            },
        )
        if self._debug_dumps_enabled:
            dump: dict[str, Any] = {
                "vin": vin,
                "mqtt_event": event,
                "respond_data": respond_data,
            }
            self._hass.async_create_task(
                self._async_write_debug_dump(f"mqtt_{event}", dump)
            )

    def _handle_mqtt_connect(self) -> None:
        """Handle an MQTT (re)connect from pyBYD — the 'cloud back up' signal.

        Fired when the push channel connects/reconnects to the BYD cloud
        (e.g. after a maintenance outage).  Surfaced as an event-bus event
        so automations can react to the service returning.
        """
        self._hass.bus.async_fire(
            _HA_EVENT_CLOUD_ONLINE, {"entry_id": self._entry.entry_id}
        )
        _LOGGER.debug(
            "MQTT (re)connected — cloud online: entry=%s", self._entry.entry_id
        )

    @property
    def mqtt_event_counters(self) -> dict[str, int]:
        """Per-event MQTT push counts since HA start."""
        return dict(self._mqtt_event_counters)

    @property
    def mqtt_event_samples(self) -> list[dict[str, Any]]:
        """Last 50 MQTT events received (timestamp + event_type + key list)."""
        return list(self._mqtt_event_samples)

    def _handle_command_ack(self, ack: CommandAckEvent) -> None:
        """Process a structured command ACK from pyBYD (diagnostics)."""
        _LOGGER.debug(
            "Command ack received: vin=%s serial=%s correlated=%s success=%s result=%s",
            ack.vin[-6:] if ack.vin else "-",
            ack.request_serial,
            ack.is_correlated,
            ack.success,
            ack.result,
        )

    def _handle_command_lifecycle(self, event: CommandLifecycleEvent) -> None:
        """Handle pyBYD-owned command lifecycle events."""
        payload: dict[str, Any] = {
            "vin": event.vin,
            "request_serial": event.request_serial,
            "status": event.status.value,
            "reason": event.reason,
            "command": event.command,
            "timestamp": event.timestamp,
        }
        if event.ack is not None:
            payload["ack_success"] = event.ack.success
            payload["ack_result"] = event.ack.result

        self._hass.bus.async_fire(_HA_EVENT_COMMAND_LIFECYCLE, payload)

        _LOGGER.debug(
            "Command lifecycle event: vin=%s serial=%s status=%s reason=%s",
            event.vin[-6:] if event.vin else "-",
            event.request_serial,
            event.status.value,
            event.reason,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def register_coordinators(
        self,
        coordinators: dict[str, BydDataUpdateCoordinator],
        gps_coordinators: dict[str, BydGpsUpdateCoordinator],
    ) -> None:
        """Register coordinators (used by on_state_changed)."""
        self._coordinators = coordinators
        self._gps_coordinators = gps_coordinators

    @property
    def config(self) -> BydConfig:
        return self._config

    @property
    def commands_enabled(self) -> bool:
        """Return True when command access has been verified."""
        return self._commands_enabled

    @property
    def commands_failed_reason(self) -> str | None:
        """Return the failure code from the last verify attempt, or None."""
        return self._commands_failed_reason

    @property
    def debug_dumps_enabled(self) -> bool:
        return self._debug_dumps_enabled

    async def async_write_debug_dump(
        self, category: str, payload: dict[str, Any]
    ) -> None:
        await self._async_write_debug_dump(category, payload)

    async def async_shutdown(self) -> None:
        await self._invalidate_client()

    async def async_verify_commands(self, vin: str) -> bool:
        """Verify the control PIN and enable remote commands.

        Returns ``True`` when verification succeeded, ``False`` otherwise.
        On failure the error code is stored in :attr:`commands_failed_reason`
        and a warning is logged.  Does **not** raise.
        """
        if not self._config.control_pin:
            _LOGGER.debug("No control PIN configured — skipping command verification")
            return False

        client = await self._ensure_client()
        try:
            await client.verify_command_access(vin)
        except BydControlPasswordError as exc:
            self._commands_enabled = False
            self._commands_failed_reason = exc.code
            if exc.code == "5006":
                _LOGGER.warning(
                    "BYD cloud control is temporarily locked; "
                    "command actions disabled (code=%s)",
                    exc.code,
                )
            else:
                _LOGGER.warning(
                    "Command PIN is wrong, disabled command actions (code=%s)",
                    exc.code,
                )
            return False
        except Exception:  # noqa: BLE001
            _LOGGER.warning(
                "Command access verification failed unexpectedly; "
                "command actions disabled",
                exc_info=True,
            )
            self._commands_enabled = False
            self._commands_failed_reason = "verify_error"
            return False

        self._commands_enabled = True
        self._commands_failed_reason = None
        self._verified_vin = vin
        _LOGGER.info("Command access verified — remote control actions enabled")
        return True

    async def _ensure_client(self) -> BydClient:
        if self._client is None:
            _LOGGER.debug(
                "Creating new pyBYD client: entry_id=%s",
                self._entry.entry_id,
            )
            client_kwargs: dict[str, Any] = {
                "session": self._http_session,
                "on_mqtt_event": self._handle_mqtt_event,
                "on_command_ack": self._handle_command_ack,
                "on_command_lifecycle": self._handle_command_lifecycle,
            }
            # Wire the MQTT (re)connect callback only when the installed
            # pybyd supports it, so an older pinned pybyd does not break setup.
            if "on_mqtt_connect" in inspect.signature(BydClient).parameters:
                client_kwargs["on_mqtt_connect"] = self._handle_mqtt_connect
            self._client = BydClient(self._config, **client_kwargs)
            await self._client.async_start()

            # Re-verify command access after client recreation.
            if self._verified_vin is not None and self._config.control_pin:
                await self.async_verify_commands(self._verified_vin)
        return self._client

    async def _invalidate_client(self) -> None:
        if self._client is not None:
            _LOGGER.debug(
                "Invalidating pyBYD client: entry_id=%s",
                self._entry.entry_id,
            )
            try:
                await self._client.async_close()
            except Exception:  # noqa: BLE001
                pass
            self._client = None
            self._commands_enabled = False

    async def async_refresh_vehicle_metadata(self, vin: str) -> Vehicle | None:
        """Re-fetch ``/app/account/getAllListByUserId`` and return the entry.

        Used to refresh the static-ish metadata that travels with the
        vehicle list — most notably ``tbox_version`` which changes after
        an OTA install.  Returns the matching :class:`Vehicle` or
        ``None`` if the cloud no longer reports this VIN.
        """
        client = await self._ensure_client()
        try:
            vehicles = await client.get_vehicles()
        except BydEndpointNotSupportedError:
            return None
        return next((v for v in vehicles if v.vin == vin), None)

    async def async_get_car(self, vin: str, vehicle: Vehicle) -> BydCar:
        """Obtain a ``BydCar`` aggregate for *vin*.

        The ``on_state_changed`` callback triggers coordinator updates
        so that HA entities re-render immediately on any state change
        (including MQTT push and post-command projections).
        """
        client = await self._ensure_client()

        def _on_state_changed(changed_vin: str, snapshot: VehicleSnapshot) -> None:
            coordinator = self._coordinators.get(changed_vin)
            if coordinator is not None:
                coordinator._async_handle_state_push(snapshot)
            gps_coordinator = self._gps_coordinators.get(changed_vin)
            if gps_coordinator is not None:
                gps_coordinator._async_handle_state_push(snapshot)

        return await client.get_car(
            vin,
            vehicle=vehicle,
            on_state_changed=_on_state_changed,
        )

    async def async_call(
        self,
        handler: Any,
        *,
        vin: str | None = None,
        command: str | None = None,
    ) -> Any:
        """Execute a raw pyBYD call with error translation.

        Handles session expiry (re-auth), transport errors, rate limits,
        and authentication failures.  Used during initial setup and by
        the GPS coordinator.
        """
        call_started = perf_counter()
        _LOGGER.debug(
            "BYD API call started: entry_id=%s, vin=%s, command=%s",
            self._entry.entry_id,
            vin[-6:] if vin else "-",
            command or "-",
        )
        try:
            client = await self._ensure_client()
            result = await handler(client)
            _LOGGER.debug(
                "BYD API call succeeded: entry_id=%s, vin=%s, "
                "command=%s, duration_ms=%.1f",
                self._entry.entry_id,
                vin[-6:] if vin else "-",
                command or "-",
                (perf_counter() - call_started) * 1000,
            )
            return result
        except BydSessionExpiredError:
            await self._invalidate_client()
            try:
                client = await self._ensure_client()
                return await handler(client)
            except (
                BydSessionExpiredError,
                BydAuthenticationError,
            ) as retry_exc:
                raise ConfigEntryAuthFailed(str(retry_exc)) from retry_exc
            except (BydApiError, BydTransportError) as retry_exc:
                raise UpdateFailed(str(retry_exc)) from retry_exc
            except Exception as retry_exc:  # noqa: BLE001
                raise UpdateFailed(str(retry_exc)) from retry_exc
        except BydControlPasswordError as exc:
            self._commands_enabled = False
            self._commands_failed_reason = exc.code
            if exc.code == "5006":
                _LOGGER.warning(
                    "BYD cloud control is temporarily locked; "
                    "command actions disabled (code=%s)",
                    exc.code,
                )
            else:
                _LOGGER.warning(
                    "Command PIN is wrong, disabled command actions (code=%s)",
                    exc.code,
                )
            raise UpdateFailed(
                "Control PIN rejected or cloud control temporarily locked"
            ) from exc
        except BydRateLimitError as exc:
            raise UpdateFailed(
                "Command rate limited by BYD cloud, please retry shortly"
            ) from exc
        except BydEndpointNotSupportedError as exc:
            raise UpdateFailed("Feature not supported for this vehicle/region") from exc
        except BydTransportError as exc:
            await self._invalidate_client()
            raise UpdateFailed(str(exc)) from exc
        except BydAuthenticationError as exc:
            raise ConfigEntryAuthFailed(str(exc)) from exc
        except BydApiError as exc:
            raise UpdateFailed(str(exc)) from exc
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug(
                "BYD API call failed: entry_id=%s, vin=%s, command=%s, "
                "duration_ms=%.1f, error=%s",
                self._entry.entry_id,
                vin[-6:] if vin else "-",
                command or "-",
                (perf_counter() - call_started) * 1000,
                type(exc).__name__,
            )
            raise


class BydDataUpdateCoordinator(DataUpdateCoordinator[VehicleSnapshot]):
    """Coordinator for telemetry + HVAC updates for a single VIN.

    Holds a ``BydCar`` reference (set after first refresh).
    ``_async_update_data()`` calls ``car.update_realtime()`` and
    conditionally ``car.update_hvac()``, then returns ``car.state``.
    Receives state-change callbacks from the state engine, which
    trigger ``async_set_updated_data(car.state)``.
    Retains ``_should_fetch_hvac()`` as consumer-side optimisation.

    When realtime transitions from ON -> OFF, performs a final HVAC
    reconcile immediately and schedules one delayed retry to avoid stale
    HVAC/seat states when the vehicle powers down.
    """

    _HVAC_FINAL_RECONCILE_RETRY_DELAY_SECONDS = 60

    # Override parent annotations: ``data`` is None until first refresh,
    # and we assign ``update_interval = None`` to pause polling (HA
    # accepts None at runtime; the stub doesn't mark it Optional).
    data: VehicleSnapshot | None
    update_interval: timedelta | None

    def __init__(
        self,
        hass: HomeAssistant,
        api: BydApi,
        vehicle: Vehicle,
        vin: str,
        poll_interval: int,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_telemetry_{vin[-6:]}",
            update_interval=timedelta(seconds=poll_interval),
        )
        self._api = api
        self._vehicle = vehicle
        self._vin = vin
        self._fixed_interval = timedelta(seconds=poll_interval)
        self._polling_enabled = True
        self._force_next_refresh = False
        self._car: BydCar | None = None
        # Authoritative last-known power state, kept on the instance so a
        # power on/off edge is acted on exactly once no matter which path
        # (MQTT push or scheduled poll) observes it — both call
        # _maybe_handle_state_transitions, and keying off the snapshot's
        # "previous" let the same edge fire twice (duplicate trips/events).
        self._last_power_on: bool | None = None
        self._realtime_endpoint_unsupported: bool = False
        self._energy_supported: bool | None = None
        self._charge_session_started_at: datetime | None = None
        self._charge_session_start_soc: float | None = None
        # When charging transitions ON → OFF mid-session, record the
        # moment.  ``_track_charge_session`` then waits the coalesce
        # window before actually closing the session — micro-pauses
        # during AC slow charge (BYD reports chargingState oscillating
        # ON/OFF every ~30s while the car deep-sleeps mid-charge) no
        # longer break a single physical session into many counted ones.
        self._charging_off_since: datetime | None = None
        self._charge_curve: list[dict[str, Any]] = []
        self._charge_sessions: list[dict[str, Any]] = []
        # Power-integration of battery charge energy (finer than 1% SoC steps):
        # last sample's time + power (kW) while charging.
        self._charge_power_last_at: datetime | None = None
        self._charge_power_last_kw: float | None = None
        # Rolling (odometer_km, soc_pct) samples for the trailing-window
        # efficiency sensor.  In-memory; rebuilds over the window distance
        # after a restart.  Trimmed to the last _EFFICIENCY_WINDOW_KM.
        self._efficiency_window: list[tuple[float, float]] = []
        self._consumption_trend: str | None = None
        self._last_mqtt_push_at: datetime | None = None
        self._last_successful_fetch_at: datetime | None = None
        # Diagnostic for the manual Fetch energy button — captures
        # when the button last fired and what BYD answered (ok /
        # unsupported / error).  Useful because the EnergyConsumption
        # sensors don't always change visibly (cloud returns same
        # payload or rejects with code=1001), so without an explicit
        # attempt timestamp the button looks broken.
        self._last_energy_fetch_at: datetime | None = None
        self._last_energy_fetch_status: str | None = None
        self._consecutive_fetch_failures: int = 0
        # Streak of consecutive service-busy (1008) realtime failures; drives
        # the adaptive backoff so we stop hammering an overloaded backend.
        self._service_busy_streak: int = 0
        # Hardening: track whether we've ever received a non-sentinel realtime
        # payload.  Used to (a) skip adaptive backoff during the first 5 min
        # after startup so we hammer the cloud until the car responds, and
        # (b) schedule an auto-force-poll if the first fetch returns sentinel
        # zeros (car was in deep sleep when HA restarted).
        self._setup_started_at: datetime = datetime.now(tz=UTC)
        self._first_real_payload_at: datetime | None = None
        self._auto_recovery_scheduled: bool = False
        # ``getEnergyConsumption`` returns a payload but with
        # ``nearestEnergyConsumption.*Distribution`` always ``"--"`` on
        # several VINs (notably Sealion 7 EU).  Treat as a separate flag
        # so we can suppress only the 4 distribution sensors while keeping
        # the rest of the energy entities working.
        self._energy_distribution_supported: bool | None = None
        self._cancel_hvac_final_retry: CALLBACK_TYPE | None = None
        self.update_pending: bool = False
        self._debounce_timer: CALLBACK_TYPE | None = None
        self._pending_schedule_updates: dict[str, Any] = {}
        # Trip tracking: snapshot taken when the car powers ON, persisted
        # so deltas (distance, SoC used) survive HA restarts mid-trip.
        self._trip_store: Store[dict[str, Any]] = Store(
            hass, _TRIP_STORE_VERSION, f"{DOMAIN}.trip_{vin}"
        )
        self._trip_store_loaded = False
        self._trip_data: dict[str, Any] = {}

    # ------------------------------------------------------------------
    # State-engine push
    # ------------------------------------------------------------------

    @callback
    def _async_handle_state_push(self, snapshot: VehicleSnapshot) -> None:
        """Update from a state-engine push and reset next poll from this update."""
        previous_snapshot = self.data
        self._last_mqtt_push_at = datetime.now(tz=UTC)
        self._schedule_hvac_final_reconcile_if_needed(previous_snapshot, snapshot)
        self._track_charge_session(previous_snapshot, snapshot)
        self._track_total_charge(snapshot)
        self._track_charge_power_integral(snapshot)
        self._track_efficiency_window(snapshot)
        self._track_distance_anchors(snapshot)
        self._maybe_fire_phase_changed(previous_snapshot, snapshot)
        self._maybe_handle_state_transitions(previous_snapshot, snapshot)

        previous_timestamp = None
        if previous_snapshot is not None and previous_snapshot.realtime is not None:
            previous_timestamp = getattr(previous_snapshot.realtime, "timestamp", None)

        current_timestamp = None
        if snapshot.realtime is not None:
            current_timestamp = getattr(snapshot.realtime, "timestamp", None)

        if current_timestamp is not None and current_timestamp != previous_timestamp:
            self.async_set_updated_data(snapshot)
            return

        self.data = snapshot
        self.last_update_success = True
        self.async_update_listeners()

    @callback
    def _cancel_pending_hvac_final_retry(self) -> None:
        """Cancel any scheduled delayed HVAC final-reconcile retry."""
        if self._cancel_hvac_final_retry is not None:
            self._cancel_hvac_final_retry()
            self._cancel_hvac_final_retry = None

    @callback
    def _schedule_hvac_final_reconcile_if_needed(
        self,
        previous_snapshot: VehicleSnapshot | None,
        current_snapshot: VehicleSnapshot | None,
    ) -> None:
        """Schedule immediate + delayed HVAC reconcile on ON->OFF transition."""
        was_on = self._is_vehicle_on_from_snapshot(previous_snapshot) is True
        is_on = self._is_vehicle_on_from_snapshot(current_snapshot) is True

        if not was_on:
            return

        if is_on:
            self._cancel_pending_hvac_final_retry()
            return

        _LOGGER.debug(
            "Vehicle transitioned OFF, scheduling final HVAC reconcile: vin=%s",
            self._vin[-6:],
        )

        self._cancel_pending_hvac_final_retry()
        self.hass.async_create_task(self._async_run_hvac_final_reconcile(attempt=1))

        @callback
        def _retry(_now: Any) -> None:
            self._cancel_hvac_final_retry = None
            self.hass.async_create_task(self._async_run_hvac_final_reconcile(attempt=2))

        self._cancel_hvac_final_retry = async_call_later(
            self.hass,
            self._HVAC_FINAL_RECONCILE_RETRY_DELAY_SECONDS,
            _retry,
        )

    async def _async_run_hvac_final_reconcile(self, *, attempt: int) -> None:
        """Run one HVAC reconcile attempt after an ON->OFF transition."""
        if not self._polling_enabled:
            _LOGGER.debug(
                "Skipping final HVAC reconcile (polling disabled): vin=%s, attempt=%s",
                self._vin[-6:],
                attempt,
            )
            return

        car = self._car
        if car is None:
            return

        _LOGGER.debug(
            "Running final HVAC reconcile: vin=%s, attempt=%s",
            self._vin[-6:],
            attempt,
        )

        try:
            await car.update_hvac()
        except _AUTH_ERRORS:
            raise
        except _RECOVERABLE_ERRORS as exc:
            _LOGGER.debug(
                "Final HVAC reconcile failed: vin=%s, attempt=%s, error=%s",
                self._vin,
                attempt,
                exc,
            )

        snapshot = car.state
        if snapshot.hvac is not None:
            self.async_set_updated_data(snapshot)

    @property
    def car(self) -> BydCar | None:
        """Return the ``BydCar`` instance if available."""
        return self._car

    @property
    def vehicle(self) -> Vehicle:
        return self._vehicle

    async def async_schedule_climate(
        self,
        *,
        temperature: float = 21.0,
        booking_time_iso: str | None = None,
        duration: int = 20,
    ) -> None:
        """Schedule HVAC pre-conditioning at the given time.

        ``booking_time_iso``: ISO datetime (local) when climate should start.
        Defaults to "now + 1 hour" if omitted.
        """
        if self._car is None:
            raise HomeAssistantError(
                f"BYD vehicle {self._vin[-6:]} not ready for climate commands"
            )
        from pybyd.models.control import ClimateScheduleParams

        if booking_time_iso is None:
            booking_dt = datetime.now(tz=UTC) + timedelta(hours=1)
        else:
            booking_dt = datetime.fromisoformat(booking_time_iso)
            if booking_dt.tzinfo is None:
                booking_dt = booking_dt.replace(tzinfo=UTC)

        # ``duration`` (minutes) maps to BYD's ``time_span`` code:
        # 1=10min, 2=15min, 3=20min, 4=25min, 5=30min.  Closest match.
        time_span_code = max(1, min(5, round((duration - 5) / 5)))
        params = ClimateScheduleParams(
            remote_mode=1,
            booking_time=int(booking_dt.timestamp()),
            temperature=temperature,
            time_span=time_span_code,
        )
        try:
            await self._car.hvac.schedule(params)
        except BydEndpointNotSupportedError as exc:
            raise HomeAssistantError(
                "schedule_climate not supported for this vehicle"
            ) from exc
        except (BydApiError, BydTransportError) as exc:
            raise HomeAssistantError(f"schedule_climate failed: {exc}") from exc
        _LOGGER.info(
            "schedule_climate accepted: vin=%s booking_time=%s temp=%s duration=%s",
            self._vin[-6:],
            booking_dt.isoformat(),
            temperature,
            duration,
        )

    async def async_force_poll_now(self) -> None:
        """Force a fresh fetch of realtime + charging in sequence.

        GPS lives on ``BydGpsUpdateCoordinator``; the ``force_poll_now``
        service handler triggers it separately.

        Intended for use immediately after an external state change
        (plug-in, key fob press, etc.) when waiting for the next scheduled
        poll would be too slow.  Caller should await this and then read
        the freshly-updated entity states.

        Wake-aware: if the first realtime fetch comes back offline/stale
        (a sleeping T-Box makes the cloud return its last cached snapshot),
        send a benign flash-lights wake and retry the fetch once the car
        reports online — so a single press genuinely refreshes a sleeping
        car instead of silently returning hours-old data.  When the car has
        no cell coverage the wake is rejected (code 6002) and we keep the
        last-known snapshot; that is a connectivity limit, not something
        polling can fix.
        """
        try:
            await self.async_fetch_realtime()
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning("force_poll: realtime failed vin=%s err=%s", self._vin, exc)

        if self._should_wake_for_force_poll():
            await self._wake_and_refetch()

        try:
            await self.async_fetch_charging()
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning("force_poll: charging failed vin=%s err=%s", self._vin, exc)

    def _should_wake_for_force_poll(self) -> bool:
        """Whether a force-poll should attempt a flash-lights wake.

        True only when the car supports flash-lights *and* the current
        snapshot looks like the "sleeping car" sentinel (offline / stale),
        so an already-awake car never triggers a needless light flash.
        """
        car = self._car
        if car is None:
            return False
        finder = getattr(car, "finder", None)
        if finder is None or not getattr(finder, "flash_available", False):
            return False
        return self._is_sentinel_payload(getattr(car, "state", None))

    async def _wake_and_refetch(self) -> None:
        """Send a benign wake (flash lights) then re-fetch until online.

        Polls realtime for up to ~60 s after the wake.  Swallows a rejected
        wake (code 6002 = weak signal around the vehicle): an unreachable
        T-Box cannot be woken remotely, so we keep the last-known snapshot
        rather than spin.
        """
        car = self._car
        if car is None:
            return
        wake_timeout = 60.0
        wake_interval = 8.0
        _LOGGER.info(
            "force_poll: car looks offline; sending flash-lights wake vin=%s",
            self._vin[-6:],
        )
        try:
            await car.finder.flash_lights()
        except Exception as exc:  # noqa: BLE001
            _LOGGER.info(
                "force_poll: wake not delivered (car unreachable?) vin=%s err=%s",
                self._vin[-6:],
                exc,
            )
            return
        elapsed = 0.0
        while elapsed < wake_timeout:
            await asyncio.sleep(wake_interval)
            elapsed += wake_interval
            try:
                await self.async_fetch_realtime()
            except Exception as exc:  # noqa: BLE001
                _LOGGER.debug(
                    "force_poll: post-wake fetch failed vin=%s err=%s",
                    self._vin[-6:],
                    exc,
                )
                continue
            if not self._is_sentinel_payload(getattr(car, "state", None)):
                _LOGGER.info(
                    "force_poll: car woke; fresh data after %.0fs vin=%s",
                    elapsed,
                    self._vin[-6:],
                )
                return
        _LOGGER.info(
            "force_poll: car did not wake within %.0fs vin=%s",
            wake_timeout,
            self._vin[-6:],
        )

    async def async_refresh_firmware_metadata(self) -> str | None:
        """Refresh ``vehicle.tbox_version`` from the cloud vehicle list.

        Fires :event:`byd_vehicle_firmware_changed` on the HA bus when
        ``tbox_version`` transitions from its previous value (typically
        after an OTA is applied).  Returns the new version or ``None``
        if the refresh failed.
        """
        previous = self._vehicle.tbox_version
        refreshed = await self._api.async_refresh_vehicle_metadata(self._vin)
        if refreshed is None:
            return None
        self._vehicle = refreshed
        if previous != refreshed.tbox_version:
            self.hass.bus.async_fire(
                "byd_vehicle_firmware_changed",
                {
                    "vin": self._vin,
                    "previous_version": previous or None,
                    "new_version": refreshed.tbox_version or None,
                },
            )
            _LOGGER.info(
                "Firmware change detected: vin=%s, %s -> %s",
                self._vin[-6:],
                previous or "?",
                refreshed.tbox_version or "?",
            )
            # Push the updated snapshot so the tbox_version sensor refreshes.
            current = self.data
            if current is not None:
                self.async_set_updated_data(
                    VehicleSnapshot(
                        vehicle=refreshed,
                        realtime=current.realtime,
                        hvac=current.hvac,
                        gps=current.gps,
                        charging=current.charging,
                        energy=current.energy,
                        charging_schedule=current.charging_schedule,
                    )
                )
        return refreshed.tbox_version or None

    @property
    def has_pin_configured(self) -> bool:
        """Return True when a non-empty control PIN exists in config."""
        pin = self._api.config.control_pin
        return isinstance(pin, str) and bool(pin.strip())

    @property
    def has_operation_pin(self) -> bool:
        """Return True when a PIN is configured **and** command access verified."""
        return self.has_pin_configured and self._api.commands_enabled

    @property
    def vin(self) -> str:
        return self._vin

    @staticmethod
    def _is_vehicle_on_from_snapshot(
        snapshot: VehicleSnapshot | None,
    ) -> bool | None:
        if snapshot is None or snapshot.realtime is None:
            return None
        return snapshot.realtime.is_vehicle_on

    @property
    def is_vehicle_on(self) -> bool:
        return self._is_vehicle_on_from_snapshot(self.data) is True

    def capability_available(self, capability_key: str) -> bool:
        """Return capability availability from pyBYD.

        Missing capability metadata is treated as unavailable.
        """
        car = self._car
        if car is None:
            return False
        capabilities = getattr(car, "capabilities", None)
        if capabilities is None:
            return False
        value = getattr(capabilities, capability_key, None)
        return bool(value)

    def _should_fetch_hvac(
        self,
        snapshot: VehicleSnapshot | None,
        *,
        force: bool = False,
    ) -> bool:
        """Decide whether HVAC data should be fetched."""
        if force:
            return True
        if snapshot is not None and snapshot.hvac is None:
            return True
        return self._is_vehicle_on_from_snapshot(snapshot) is True

    async def async_ensure_car(self) -> None:
        """Bind the BydCar early (capability fetch) outside the refresh path.

        Called by setup before the first refresh so the GPS coordinator,
        which borrows this coordinator's car, can refresh in parallel
        instead of silently producing an empty snapshot.
        """
        if self._car is None:
            self._car = await self._api.async_get_car(self._vin, self._vehicle)

    async def _async_update_data(self) -> VehicleSnapshot:
        """Fetch telemetry + conditional HVAC and return car.state."""
        _LOGGER.debug("Telemetry refresh started: vin=%s", self._vin[-6:])
        if not self._trip_store_loaded:
            self._trip_store_loaded = True
            self._trip_data = await self._trip_store.async_load() or {}
        force = self._force_next_refresh
        self._force_next_refresh = False
        previous_snapshot = self.data

        if self.update_pending:
            _LOGGER.debug(
                "Skipping telemetry refresh due to pending schedule update: vin=%s",
                self._vin[-6:],
            )
            if self.data is not None:
                return self.data
            return VehicleSnapshot(vehicle=self._vehicle)

        if not self._polling_enabled and not force:
            if self.data is not None:
                return self.data
            return VehicleSnapshot(vehicle=self._vehicle)

        if self._car is None:
            self._car = await self._api.async_get_car(self._vin, self._vehicle)

        car = self._car

        # --- Realtime ---
        realtime_fetch_succeeded = False
        try:
            await car.update_realtime()
            realtime_fetch_succeeded = True
        except _AUTH_ERRORS:
            raise
        except BydEndpointNotSupportedError:
            if not self._realtime_endpoint_unsupported:
                _LOGGER.warning(
                    "Realtime HTTP endpoint not supported for vin=%s — "
                    "will rely on MQTT push (logged once only)",
                    self._vin,
                )
                self._realtime_endpoint_unsupported = True
        except _RECOVERABLE_ERRORS as exc:
            self._note_fetch_failure(exc)
            _LOGGER.warning(
                "Realtime fetch failed: vin=%s, error=%s, consecutive_failures=%d, "
                "service_busy_streak=%d",
                self._vin,
                exc,
                self._consecutive_fetch_failures,
                self._service_busy_streak,
            )

        # --- HVAC (conditional) ---
        if self._should_fetch_hvac(car.state, force=force):
            try:
                await car.update_hvac()
            except _AUTH_ERRORS:
                raise
            except _RECOVERABLE_ERRORS as exc:
                _LOGGER.warning(
                    "HVAC fetch failed: vin=%s, error=%s",
                    self._vin,
                    exc,
                )
        else:
            _LOGGER.debug(
                "HVAC fetch skipped: vin=%s, reason=vehicle_not_on",
                self._vin[-6:],
            )

        # --- Charging (Schedule & Live) ---
        try:
            await car.update_charging()
        except _AUTH_ERRORS:
            raise
        except _RECOVERABLE_ERRORS as exc:
            _LOGGER.warning(
                "Charging fetch failed: vin=%s, error=%s",
                self._vin,
                exc,
            )

        snapshot = car.state
        self._schedule_hvac_final_reconcile_if_needed(previous_snapshot, snapshot)

        # Bail if we still have no realtime data at all
        if snapshot.realtime is None and not self._realtime_endpoint_unsupported:
            raise UpdateFailed(
                f"Realtime state unavailable for {self._vin}; no data returned from API"
            )

        # Debug dump
        if self._api.debug_dumps_enabled:
            dump: dict[str, Any] = {"vin": self._vin, "sections": {}}
            if snapshot.realtime is not None:
                dump["sections"]["realtime"] = snapshot.realtime.model_dump(mode="json")
            if snapshot.hvac is not None:
                dump["sections"]["hvac"] = snapshot.hvac.model_dump(mode="json")
            self.hass.async_create_task(
                self._api.async_write_debug_dump("telemetry", dump)
            )

        _LOGGER.debug(
            "Telemetry refresh succeeded: vin=%s, realtime=%s, hvac=%s",
            self._vin[-6:],
            snapshot.realtime is not None,
            snapshot.hvac is not None,
        )

        # Health tracking: only the realtime HTTP fetch actually succeeding
        # this cycle counts as a success.  Looking at ``snapshot.realtime``
        # alone is misleading — pyBYD keeps the last successful payload
        # cached on the BydCar, so the snapshot stays populated across
        # failures and the cloud_responsive sensor never flips even during
        # a multi-hour outage (903 consecutive 1008/500 errors observed
        # 2026-05-25 night without the connectivity binary_sensor noticing).
        if realtime_fetch_succeeded:
            self._note_fetch_success()

        # Mark the first non-sentinel payload — used by the adaptive
        # interval to know when to stop aggressive startup polling.
        if self._first_real_payload_at is None and not self._is_sentinel_payload(
            snapshot
        ):
            self._first_real_payload_at = datetime.now(tz=UTC)

        # If we got a sentinel payload and haven't yet seen real data,
        # schedule a one-shot recovery force-poll 60s later.  Covers the
        # "battery_level=unknown after restart" case where the car was
        # in deep sleep when HA came back.
        if (
            self._first_real_payload_at is None
            and not self._auto_recovery_scheduled
            and self._is_sentinel_payload(snapshot)
        ):
            self._auto_recovery_scheduled = True
            _LOGGER.info(
                "vin=%s: sentinel payload at startup, scheduling recovery "
                "force-poll in 60s",
                self._vin[-6:],
            )

            async def _recover(_now: Any) -> None:
                try:
                    await self.async_force_poll_now()
                finally:
                    self._auto_recovery_scheduled = False

            async_call_later(self.hass, 60, _recover)

        # Adapt next poll interval based on the just-observed state.
        self._apply_adaptive_interval(snapshot)
        self._track_charge_session(previous_snapshot, snapshot)
        self._track_total_charge(snapshot)
        self._track_charge_power_integral(snapshot)
        self._track_efficiency_window(snapshot)
        self._track_distance_anchors(snapshot)
        self._maybe_fire_phase_changed(previous_snapshot, snapshot)
        self._maybe_handle_state_transitions(previous_snapshot, snapshot)

        return snapshot

    # ------------------------------------------------------------------
    # Adaptive polling
    # ------------------------------------------------------------------

    # MQTT push within this window suppresses HTTP polling further —
    # the car is actively pushing state, so HTTP is redundant.
    _MQTT_AWARE_WINDOW = timedelta(minutes=10)
    _MQTT_AWARE_BONUS = 2  # additional multiplier when push is recent

    # Aggressive-poll window after startup: skip adaptive backoff for the
    # first 5 minutes so a deep-sleep car doesn't strand us on stale data.
    _STARTUP_AGGRESSIVE_WINDOW = timedelta(minutes=5)

    # Below this instantaneous battery power (W), an active charge is
    # considered "AC slow" — SOC rises ~1% every ~12 min, so a 1×
    # cadence wastes calls.  Threshold sits between Schuko (~2.3 kW)
    # and the smallest DC fast chargers (~25 kW).
    _AC_SLOW_CHARGE_THRESHOLD_W = 5000.0

    # Service-busy (1008) backoff: when the backend returns bursts of
    # BydServiceBusyError, widen the poll interval instead of hammering.
    # Backoff kicks in at the threshold and doubles per extra failure,
    # capped, with a hard ceiling on the resulting interval.
    _SERVICE_BUSY_BACKOFF_THRESHOLD = 3
    _SERVICE_BUSY_BACKOFF_MAX_FACTOR = 8
    _MAX_BACKOFF_INTERVAL = timedelta(minutes=30)

    def _is_sentinel_payload(self, snapshot: VehicleSnapshot | None) -> bool:
        """Whether the realtime payload looks like a "sleeping car" sentinel.

        Indicators: ``online_state == 0`` AND ``elec_percent == 0`` AND
        ``total_mileage == 0`` (or ``None``).  These are the values BYD's
        cloud returns when the T-Box hasn't been queried recently.
        """
        if snapshot is None or snapshot.realtime is None:
            return True
        rt = snapshot.realtime
        if getattr(rt, "is_online", None) is False:
            return True
        elec = getattr(rt, "elec_percent", None)
        mileage = getattr(rt, "total_mileage", None)
        return (elec is None or elec == 0) and (mileage is None or mileage == 0)

    def _compute_adaptive_interval(self, snapshot: VehicleSnapshot) -> timedelta:
        """Return the next telemetry interval based on the current snapshot.

        Multipliers (applied to the user-configured ``_fixed_interval``):
          * 1×   — driving OR fast-charging (vehicle on / DC charge)
          * 4×   — AC slow-charging (|battery_power| below
            :attr:`_AC_SLOW_CHARGE_THRESHOLD_W`)
          * 2×   — plugged & waiting (schedule pending or connector locked)
          * 4×   — online idle (cable in, no schedule, no charge)
          * 8×   — offline / sleeping

        An additional 2× multiplier is applied when an MQTT push has
        arrived within :attr:`_MQTT_AWARE_WINDOW` — when the car is
        actively notifying us, HTTP polling is redundant.
        """
        base = self._fixed_interval
        if snapshot is None or snapshot.realtime is None:
            multiplier = 4
        else:
            realtime = snapshot.realtime
            charging = snapshot.charging
            is_active_charge = (
                charging is not None and getattr(charging, "charging_state", None) == 1
            ) or getattr(realtime, "is_charging", None) is True
            is_vehicle_on = getattr(realtime, "is_vehicle_on", None) is True
            if is_active_charge or is_vehicle_on:
                multiplier = 1
                if is_active_charge and not is_vehicle_on:
                    power_w = getattr(realtime, "gl", None)
                    try:
                        power_abs = abs(float(power_w)) if power_w is not None else None
                    except (TypeError, ValueError):
                        power_abs = None
                    if (
                        power_abs is not None
                        and power_abs < self._AC_SLOW_CHARGE_THRESHOLD_W
                    ):
                        multiplier = 4
            elif charging is not None and (
                getattr(charging, "wait_status", None) == 1
                or getattr(charging, "charging_state", None) == 9
                or getattr(charging, "connect_state", None) == 1
            ):
                multiplier = 2
            elif getattr(realtime, "is_online", None) is False:
                multiplier = 8
            else:
                multiplier = 4

        # Startup-aggressive: until we've received a real (non-sentinel)
        # payload OR 5 min have elapsed, keep polling at 1× to chase the
        # car out of deep sleep.
        now = datetime.now(tz=UTC)
        in_startup_window = (
            now - self._setup_started_at < self._STARTUP_AGGRESSIVE_WINDOW
        )
        if in_startup_window and self._first_real_payload_at is None:
            return base

        # MQTT-aware bonus: only when not in the 1× (active) bucket — we
        # want fresh HTTP data while charging/driving even if push is
        # flowing.  For all idle/waiting states, trust the push channel.
        if (
            multiplier > 1
            and self._last_mqtt_push_at is not None
            and now - self._last_mqtt_push_at < self._MQTT_AWARE_WINDOW
        ):
            multiplier *= self._MQTT_AWARE_BONUS

        # Service-busy (1008) backoff: bursts of "Error de servicio" mean the
        # backend wants fewer requests — widen the interval (doubling per extra
        # failure past the threshold, capped) instead of hammering. Skipped
        # during the startup window above (returned early), so a deep-asleep
        # car is still chased before we ever back off.
        if self._service_busy_streak >= self._SERVICE_BUSY_BACKOFF_THRESHOLD:
            busy_factor = min(
                2
                ** (
                    self._service_busy_streak - self._SERVICE_BUSY_BACKOFF_THRESHOLD + 1
                ),
                self._SERVICE_BUSY_BACKOFF_MAX_FACTOR,
            )
            multiplier *= busy_factor

        interval = base * multiplier
        return min(interval, self._MAX_BACKOFF_INTERVAL)

    def _apply_adaptive_interval(self, snapshot: VehicleSnapshot) -> None:
        """Update ``update_interval`` from the adaptive policy."""
        if not self._polling_enabled:
            return
        new_interval = self._compute_adaptive_interval(snapshot)
        if new_interval == self.update_interval:
            return
        _LOGGER.debug(
            "Adaptive poll interval: vin=%s, %s -> %s",
            self._vin[-6:],
            self.update_interval,
            new_interval,
        )
        self.update_interval = new_interval

    def _derive_phase(self, snap: VehicleSnapshot | None) -> str | None:
        """Replicate the logic from sensor._charge_session_phase.

        Returns the same string label so the HA event fires consistently
        with the value shown by ``sensor.charge_session_phase``.
        """
        if snap is None or snap.charging is None:
            return None
        ch = snap.charging
        rt = snap.realtime
        connect_state = getattr(ch, "connect_state", None)
        charging_state = getattr(ch, "charging_state", None)
        wait_status = getattr(ch, "wait_status", None)
        soc = getattr(ch, "soc", None) or (
            getattr(rt, "elec_percent", None) if rt else None
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

    @staticmethod
    def _read_odo(snap: VehicleSnapshot | None) -> float | None:
        """Odometer (km) from a snapshot as a float, or None if absent.

        Single reader for the ``realtime.total_mileage`` access duplicated
        across the trip / distance / efficiency algorithms. Does NOT filter
        0 — callers that treat 0 as a sentinel keep their own checks.
        """
        if snap is None or snap.realtime is None:
            return None
        val = getattr(snap.realtime, "total_mileage", None)
        return float(val) if isinstance(val, (int, float)) else None

    @staticmethod
    def _read_soc(
        snap: VehicleSnapshot | None, prefer: str = "charging"
    ) -> float | None:
        """SoC (%) from a snapshot, or None.

        ``prefer="charging"`` (default) reads ``charging.soc`` then falls back
        to ``realtime.elec_percent`` — correct while charging (charging
        endpoint is fresh). ``prefer="realtime"`` reverses it — correct while
        driving (the charging endpoint isn't polled, so realtime is fresher).
        Uses ``is not None`` so a genuine 0 % isn't skipped.
        """
        if snap is None:
            return None
        ch = getattr(snap, "charging", None)
        rt = getattr(snap, "realtime", None)
        ch_soc = getattr(ch, "soc", None) if ch is not None else None
        rt_soc = getattr(rt, "elec_percent", None) if rt is not None else None
        order = (rt_soc, ch_soc) if prefer == "realtime" else (ch_soc, rt_soc)
        for s in order:
            if s is not None:
                return float(s)
        return None

    @staticmethod
    def _soc_to_kwh(soc_delta: float | None) -> float | None:
        """SoC delta → kWh (delegates to the pure, tested _logic helper)."""
        return _logic.soc_to_kwh(soc_delta, _DEFAULT_BATTERY_KWH)

    @staticmethod
    def _efficiency_per_100km(
        energy_kwh: float | None, distance_km: float | None
    ) -> float | None:
        """kWh/100km (delegates to the pure, tested _logic helper)."""
        return _logic.efficiency_per_100km(energy_kwh, distance_km)

    @staticmethod
    def _is_charging(snap: VehicleSnapshot | None) -> bool:
        """True when the snapshot's charging endpoint reports charging_state 1."""
        if snap is None or snap.charging is None:
            return False
        return getattr(snap.charging, "charging_state", None) == 1

    def _record_trip_history(
        self, trip: dict[str, Any], *, inferred: bool = False
    ) -> None:
        """Append a compact trip record to the capped rolling history.

        Single owner of the history-entry shape + cap, shared by the real
        power-off trip path and the inferred offline-drive path.
        """
        entry: dict[str, Any] = {
            "started_at": trip.get("started_at"),
            "distance_km": trip.get("distance_km"),
            "energy_kwh": trip.get("energy_kwh"),
            "efficiency_kwh_per_100km": trip.get("efficiency_kwh_per_100km"),
        }
        if inferred:
            entry["inferred"] = True
        history = self._trip_data.get("trip_history")
        if not isinstance(history, list):
            history = []
        self._trip_data["trip_history"] = (history + [entry])[-_TRIP_HISTORY_MAX:]

    @staticmethod
    def _ota_active(snap: VehicleSnapshot | None) -> bool | None:
        if snap is None or snap.realtime is None:
            return None
        val = getattr(snap.realtime, "upgrade_status", None)
        if val is None:
            return None
        try:
            return int(val) > 0
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _plug_connected(snap: VehicleSnapshot | None) -> bool | None:
        if snap is None:
            return None
        if snap.charging is not None:
            state = getattr(snap.charging, "connect_state", None)
            if state is not None:
                return int(state) > 0
        if snap.realtime is not None:
            state = getattr(snap.realtime, "connect_state", None)
            if state is not None:
                return int(state) > 0
        return None

    @staticmethod
    def _power_on(snap: VehicleSnapshot | None) -> bool | None:
        if snap is None or snap.realtime is None:
            return None
        gear = getattr(snap.realtime, "power_gear", None)
        if gear == PowerGear.ON:
            return True
        if gear == PowerGear.OFF:
            return False
        return None  # missing / UNKNOWN / sentinel payload

    def _maybe_handle_state_transitions(
        self,
        previous: VehicleSnapshot | None,
        current: VehicleSnapshot,
    ) -> None:
        """Run the post-update transition handlers — one concern each."""
        if previous is None:
            return
        self._maybe_refresh_on_ota_done(previous, current)
        self._maybe_refresh_on_plug_in(previous, current)
        self._handle_power_edge(previous, current)
        self._maybe_fire_capability_changes(previous, current)

    def _maybe_refresh_on_ota_done(
        self, previous: VehicleSnapshot, current: VehicleSnapshot
    ) -> None:
        """OTA done (upgrade_status on→off): force a charging + firmware
        refresh now — the next regular poll wouldn't for minutes."""
        if self._ota_active(previous) is True and self._ota_active(current) is False:
            _LOGGER.info(
                "OTA finished on vin=%s — refreshing charging + firmware",
                self._vin[-6:],
            )
            self.hass.async_create_task(self._async_post_ota_refresh())

    def _maybe_refresh_on_plug_in(
        self, previous: VehicleSnapshot, current: VehicleSnapshot
    ) -> None:
        """Plug-in (connect off→on): force a charging-snapshot refresh so
        charge_session_phase flips within seconds, not minutes."""
        if (
            self._plug_connected(previous) is False
            and self._plug_connected(current) is True
        ):
            _LOGGER.info(
                "Plug-in detected on vin=%s — refreshing charging snapshot",
                self._vin[-6:],
            )
            self.hass.async_create_task(self._async_post_plug_refresh())

    def _handle_power_edge(
        self, previous: VehicleSnapshot, current: VehicleSnapshot
    ) -> None:
        """Power on/off edge (deduped) + offline-drive recovery when no edge.

        Dedupe against the authoritative tracked state, not the snapshot's
        "previous": the same edge is otherwise seen by both the push and the
        poll path and fires twice. A ``None`` reading (sentinel/sleeping
        payload) is ignored so it can't clobber the known state.
        """
        new_power = self._power_on(current)
        if new_power is not None and new_power != self._last_power_on:
            if self._last_power_on is not None:
                # A genuine edge (not the very first observation at startup).
                self._handle_power_transition(is_on=new_power, snapshot=current)
            self._last_power_on = new_power
        else:
            # No power flank this step.  If the odometer nonetheless jumped,
            # the car was driven while we were blind (cloud blackout / long
            # sleep-gap) and we never saw vehicle_on — recover that drive.
            self._maybe_detect_offline_drive(previous, current, self._last_power_on)

    def _maybe_detect_offline_drive(
        self,
        previous: VehicleSnapshot,
        current: VehicleSnapshot,
        last_power_on: bool | None,
    ) -> None:
        """Infer + announce a drive that happened entirely between two polls.

        Fires :event:`byd_vehicle_offline_drive` (and records the distance in
        the trip history) when the odometer advanced by a real margin while
        the car was known powered-off — so a blackout/sleep-gap drive isn't
        silently lost. May be an aggregate of several drives; it's marked
        ``inferred`` and cannot be decomposed.
        """
        # Only when the car was known powered-off — a real tracked trip
        # already covers the on→…→off case.
        if last_power_on is True:
            return
        prev_odo = self._read_odo(previous)
        cur_odo = self._read_odo(current)
        if prev_odo is None or cur_odo is None:
            return
        distance = round(cur_odo - prev_odo, 1)
        if distance < _MIN_OFFLINE_DRIVE_KM or distance > _MAX_OFFLINE_DRIVE_KM:
            return

        prev_soc = self._read_soc(previous)
        cur_soc = self._read_soc(current)
        soc_used = (
            round(prev_soc - cur_soc, 1)
            if isinstance(prev_soc, (int, float)) and isinstance(cur_soc, (int, float))
            else None
        )
        # Offline path suppresses negative energy (gate on soc_used > 0) and,
        # like the trip path, skips energy on sub-_MIN_ENERGY_TRIP_KM distances.
        positive_soc = (
            soc_used
            if (
                isinstance(soc_used, (int, float))
                and soc_used > 0
                and distance >= _MIN_ENERGY_TRIP_KM
            )
            else None
        )
        energy_kwh = self._soc_to_kwh(positive_soc)
        efficiency = self._efficiency_per_100km(energy_kwh, distance)

        now = datetime.now(tz=UTC)
        trip = {
            "inferred": True,
            "started_at": (
                self._last_successful_fetch_at.isoformat()
                if self._last_successful_fetch_at is not None
                else now.isoformat()
            ),
            "ended_at": now.isoformat(),
            "start_odometer": float(prev_odo),
            "end_odometer": float(cur_odo),
            "distance_km": distance,
            "soc_used": soc_used,
            "energy_kwh": energy_kwh,
            "efficiency_kwh_per_100km": efficiency,
        }
        self._record_trip_history(trip, inferred=True)
        if self._trip_store_loaded:
            self._trip_store.async_delay_save(
                lambda: dict(self._trip_data), _TRIP_STORE_SAVE_DELAY_SECONDS
            )
        _LOGGER.info(
            "Offline drive inferred: vin=%s distance=%skm soc_used=%s",
            self._vin[-6:],
            distance,
            soc_used,
        )
        self.hass.bus.async_fire(_HA_EVENT_OFFLINE_DRIVE, {"vin": self._vin, **trip})

    @callback
    def _handle_power_transition(
        self,
        *,
        is_on: bool,
        snapshot: VehicleSnapshot,
    ) -> None:
        """Fire :event:`byd_vehicle_power_changed` and maintain trip state.

        On OFF→ON: capture a trip-start baseline (SoC, odometer, position)
        and persist it.  On ON→OFF: build a trip summary (distance, SoC
        used, duration) against that baseline.  Both directions fire the
        event so automations get the fastest available power signal
        (MQTT push when the car is online, next poll otherwise).
        """
        now = datetime.now(tz=UTC)
        soc = self._read_soc(snapshot)
        odometer = self._read_odo(snapshot)
        latitude: Any = None
        longitude: Any = None
        if snapshot.gps is not None:
            latitude = getattr(snapshot.gps, "latitude", None)
            longitude = getattr(snapshot.gps, "longitude", None)

        payload: dict[str, Any] = {
            "vin": self._vin,
            # ``is_on`` is what device_trigger.py matches on — keep it.
            "is_on": is_on,
            "state": "on" if is_on else "off",
            "timestamp": now.isoformat(),
        }

        if is_on:
            trip_start: dict[str, Any] = {
                "started_at": now.isoformat(),
                "odometer": odometer,
                "soc": soc,
                "latitude": latitude,
                "longitude": longitude,
            }
            self._trip_data["trip_start"] = trip_start
            payload["trip_start"] = trip_start
            _LOGGER.info(
                "Vehicle powered ON: vin=%s soc=%s odometer=%s",
                self._vin[-6:],
                soc,
                odometer,
            )
        else:
            trip = self._build_trip_summary(
                ended_at=now,
                end_odometer=odometer,
                end_soc=soc,
            )
            trip_distance = trip.get("distance_km") if trip is not None else None
            is_real_trip = (
                trip is not None
                and isinstance(trip_distance, (int, float))
                and trip_distance >= _MIN_TRIP_KM
            )
            if trip is not None and is_real_trip:
                self._trip_data["last_trip"] = trip
                payload["trip"] = trip
                # Record into the capped rolling history for the aggregate
                # stats sensors (shared with the offline-drive path).
                self._record_trip_history(trip)
            _LOGGER.info(
                "Vehicle powered OFF: vin=%s trip=%s",
                self._vin[-6:],
                trip,
            )

        if self._trip_store_loaded:
            self._trip_store.async_delay_save(
                lambda: dict(self._trip_data),
                _TRIP_STORE_SAVE_DELAY_SECONDS,
            )
        self.hass.bus.async_fire(_HA_EVENT_POWER_CHANGED, payload)

    def _build_trip_summary(
        self,
        *,
        ended_at: datetime,
        end_odometer: Any,
        end_soc: Any,
    ) -> dict[str, Any] | None:
        """Build a trip summary from the persisted trip-start baseline."""
        trip_start = self._trip_data.get("trip_start")
        if not isinstance(trip_start, dict):
            return None
        summary: dict[str, Any] = {
            "started_at": trip_start.get("started_at"),
            "ended_at": ended_at.isoformat(),
            "start_odometer": trip_start.get("odometer"),
            "end_odometer": end_odometer,
            "start_soc": trip_start.get("soc"),
            "end_soc": end_soc,
            "start_latitude": trip_start.get("latitude"),
            "start_longitude": trip_start.get("longitude"),
        }
        try:
            started = datetime.fromisoformat(str(trip_start.get("started_at")))
            duration = (ended_at - started).total_seconds() / 60.0
            summary["duration_minutes"] = round(duration, 1)
        except (TypeError, ValueError):
            summary["duration_minutes"] = None
        start_odo = trip_start.get("odometer")
        if (
            isinstance(start_odo, (int, float))
            and isinstance(end_odometer, (int, float))
            and end_odometer >= start_odo
        ):
            summary["distance_km"] = round(float(end_odometer) - float(start_odo), 1)
        else:
            summary["distance_km"] = None
        start_soc = trip_start.get("soc")
        if isinstance(start_soc, (int, float)) and isinstance(end_soc, (int, float)):
            summary["soc_used"] = round(float(start_soc) - float(end_soc), 1)
        else:
            summary["soc_used"] = None

        # Coarse per-trip energy + efficiency from the SoC delta and pack
        # nameplate.  We poll at intervals (not per CAN tick), so we cannot
        # sum the signed discharge counter the way an on-device logger would;
        # the SoC-based estimate is the honest approximation available here.
        # A negative soc_used (net regen / charged-while-on) yields negative
        # energy, which we surface as-is but skip for the efficiency figure.
        # Trip path SURFACES negative energy (net regen); efficiency helper
        # still excludes it. Skip energy entirely on sub-_MIN_ENERGY_TRIP_KM
        # trips — integer SoC can't measure them (1 km → 82 kWh/100km garbage).
        _dist = summary["distance_km"]
        if isinstance(_dist, (int, float)) and _dist >= _MIN_ENERGY_TRIP_KM:
            summary["energy_kwh"] = self._soc_to_kwh(summary["soc_used"])
        else:
            summary["energy_kwh"] = None
        summary["efficiency_kwh_per_100km"] = self._efficiency_per_100km(
            summary["energy_kwh"], summary["distance_km"]
        )
        return summary

    def _maybe_fire_capability_changes(
        self,
        previous: VehicleSnapshot | None,
        current: VehicleSnapshot,
    ) -> None:
        """Fire HA events when ``vehicleFunLearnInfo`` flags transition.

        Every realtime payload carries the capability dict.  When a flag
        changes value (e.g. ``otaUpgrade`` goes 0 → 1 announcing a new
        OTA, or ``sentryStatusLearnInfo`` flips after the user toggles
        sentry from the car) we fire one event per flag so automations
        can react without polling the diagnostic sensor.

        Event: ``byd_vehicle_capability_changed`` with
        ``{vin, flag, previous_value, new_value}``.
        """

        def _flags(snap: VehicleSnapshot | None) -> dict[str, Any]:
            if snap is None or snap.vehicle is None:
                return {}
            raw = getattr(snap.vehicle, "raw", None)
            if not isinstance(raw, dict):
                return {}
            funlearn = raw.get("vehicleFunLearnInfo")
            return funlearn if isinstance(funlearn, dict) else {}

        prev_flags = _flags(previous)
        new_flags = _flags(current)
        if not prev_flags or not new_flags:
            return

        for flag, new_value in new_flags.items():
            prev_value = prev_flags.get(flag)
            if prev_value == new_value:
                continue
            self.hass.bus.async_fire(
                "byd_vehicle_capability_changed",
                {
                    "vin": self._vin,
                    "flag": flag,
                    "previous_value": prev_value,
                    "new_value": new_value,
                },
            )
            _LOGGER.debug(
                "Capability change: vin=%s %s: %s -> %s",
                self._vin[-6:],
                flag,
                prev_value,
                new_value,
            )

    async def _async_post_ota_refresh(self) -> None:
        """Background: refresh charging + firmware after OTA completion."""
        try:
            await self.async_fetch_charging()
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning(
                "post-OTA charging refresh failed vin=%s err=%s", self._vin, exc
            )
        try:
            await self.async_refresh_firmware_metadata()
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning(
                "post-OTA firmware refresh failed vin=%s err=%s", self._vin, exc
            )

    async def _async_post_plug_refresh(self) -> None:
        """Background: refresh charging snapshot + GPS after plug detected.

        GPS often goes stale while the car was parked unplugged (last
        successful ``getGpsInfo`` may be hours old).  Plug-in means the
        car is awake on the cloud, so this is a free opportunity to
        re-anchor the position before the next idle window.

        GPS lives on a sibling coordinator (``BydGpsUpdateCoordinator``)
        registered on the api, so reach across to fire its fetch.
        """
        try:
            await self.async_fetch_charging()
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning(
                "post-plug charging refresh failed vin=%s err=%s", self._vin, exc
            )
        gps_coordinator = self._api._gps_coordinators.get(self._vin)
        if gps_coordinator is not None:
            try:
                await gps_coordinator.async_fetch_gps()
            except Exception as exc:  # noqa: BLE001
                _LOGGER.warning(
                    "post-plug GPS refresh failed vin=%s err=%s", self._vin, exc
                )

    def _maybe_fire_phase_changed(
        self,
        previous: VehicleSnapshot | None,
        current: VehicleSnapshot,
    ) -> None:
        """Fire :event:`byd_vehicle_phase_changed` on phase transition."""
        prev_phase = self._derive_phase(previous)
        new_phase = self._derive_phase(current)
        if new_phase is None or prev_phase == new_phase:
            return
        self.hass.bus.async_fire(
            "byd_vehicle_phase_changed",
            {
                "vin": self._vin,
                "previous_phase": prev_phase,
                "new_phase": new_phase,
            },
        )
        _LOGGER.debug(
            "Phase change: vin=%s %s -> %s",
            self._vin[-6:],
            prev_phase or "?",
            new_phase,
        )

    # Power on/off edges are detected in _maybe_handle_state_transitions
    # (push + poll paths) and handled by _handle_power_transition, which
    # fires :event:`byd_vehicle_power_changed` with ``is_on`` (consumed by
    # device_trigger.py) plus the trip snapshot/summary payload.

    # AC slow charge surfaces micro-pauses in ``charging_state`` every
    # ~30 seconds (the car deep-sleeps mid-charge and the cloud loses
    # the chargingState=1 flag temporarily).  Coalesce two ON periods
    # into the same session if the OFF gap between them is shorter
    # than this window.  10 min is well above the observed 30-60s
    # pause cadence and well below any legitimate short-then-restart
    # scenario.
    _CHARGE_SESSION_COALESCE_WINDOW_SECONDS = 600

    #: Trailing window (km) for the recent-efficiency sensor.
    _EFFICIENCY_WINDOW_KM = 30.0
    #: Minimum spanned distance before a figure is published (km).
    _EFFICIENCY_MIN_KM = 5.0

    def _track_efficiency_window(self, current: VehicleSnapshot) -> None:
        """Append an (odometer, SoC) sample and trim to the trailing window.

        Feeds :attr:`recent_efficiency_kwh_per_100km`.  Samples are only
        kept when both odometer and SoC are present and the odometer has
        advanced (skips parked/charging noise and SoC rises from charging).
        """
        # Realtime-first SoC: during a drive the charging endpoint isn't
        # polled, so realtime.elec_percent is the fresher value here.
        odo_f = self._read_odo(current)
        soc_f = self._read_soc(current, prefer="realtime")
        if odo_f is None or soc_f is None:
            return
        window = self._efficiency_window
        if window:
            last_odo, last_soc = window[-1]
            if odo_f < last_odo:
                # Odometer went backwards (sentinel/glitch) — reset.
                window.clear()
            elif soc_f - last_soc >= _EFFICIENCY_SOC_RISE_RESET:
                # SoC jumped up = a charge happened. Efficiency measured across
                # a charge is bogus (undercounts), so start a fresh window.
                window.clear()
            elif odo_f == last_odo:
                # No movement: refresh the latest SoC in place so a small SoC
                # rise while parked doesn't look like consumption.
                window[-1] = (odo_f, soc_f)
                return
        window.append((odo_f, soc_f))
        # Trim from the front to keep only the last _EFFICIENCY_WINDOW_KM.
        while len(window) > 2 and (odo_f - window[0][0]) > self._EFFICIENCY_WINDOW_KM:
            window.pop(0)
        self._update_consumption_trend()

    #: Short comparison window (km) for the consumption-trend arrow.
    _EFFICIENCY_SHORT_KM = 5.0

    def _efficiency_over(self, km_back: float) -> float | None:
        """SoC-based efficiency (kWh/100km) over the last ``km_back`` km."""
        window = self._efficiency_window
        if len(window) < 2:
            return None
        end_odo, end_soc = window[-1]
        start: tuple[float, float] | None = None
        for odo, soc in window:
            if end_odo - odo <= km_back:
                start = (odo, soc)
                break
        if start is None:
            return None
        distance = end_odo - start[0]
        if distance < 1.0:
            return None
        soc_used = start[1] - end_soc
        if soc_used <= 0:
            return None
        return self._efficiency_per_100km(self._soc_to_kwh(soc_used), distance)

    def _update_consumption_trend(self) -> None:
        """Recompute the consumption-trend state with hysteresis.

        Compares short-window (~5 km) efficiency against the full ~30 km
        window.  Hysteresis bands (enter 0.90/1.10, exit 0.95/1.05) stop the
        arrow flapping at every sample.
        """
        short = self._efficiency_over(self._EFFICIENCY_SHORT_KM)
        long = self._efficiency_over(self._EFFICIENCY_WINDOW_KM)
        if not short or not long or long <= 0:
            return
        self._consumption_trend = _logic.next_trend_state(
            self._consumption_trend, short / long
        )

    @property
    def consumption_trend(self) -> str | None:
        """Recent consumption trend: improving / steady / worsening."""
        return self._consumption_trend

    @property
    def recent_efficiency_kwh_per_100km(self) -> float | None:
        """Trailing-window energy efficiency (kWh/100km), SoC-based.

        Computed over the last :attr:`_EFFICIENCY_WINDOW_KM` of driving from
        the SoC drop × pack nameplate and the odometer delta.  Returns
        ``None`` until at least :attr:`_EFFICIENCY_MIN_KM` is spanned or when
        the net SoC change is a rise (regen/charge), where consumption is
        undefined.
        """
        window = self._efficiency_window
        if len(window) < 2:
            return None
        start_odo, start_soc = window[0]
        end_odo, end_soc = window[-1]
        distance = end_odo - start_odo
        if distance < self._EFFICIENCY_MIN_KM:
            return None
        soc_used = start_soc - end_soc
        if soc_used <= 0:
            return None
        return self._efficiency_per_100km(self._soc_to_kwh(soc_used), distance)

    def _track_distance_anchors(self, current: VehicleSnapshot) -> None:
        """Maintain start-of-day / start-of-month odometer anchors.

        Anchors are stored in the persisted trip store keyed by the local
        (HA timezone) day / month so "distance today" and "distance this
        month" survive restarts and roll over cleanly at midnight / the 1st.
        The anchor is the first odometer reading seen in that period.
        """
        odo = self._read_odo(current)
        if odo is None or odo <= 0:
            return
        now_local = dt_util.now()
        day_key = now_local.date().isoformat()
        month_key = now_local.strftime("%Y-%m")
        changed = False
        day_anchor = self._trip_data.get("odo_day_anchor")
        if not isinstance(day_anchor, dict) or day_anchor.get("day") != day_key:
            self._trip_data["odo_day_anchor"] = {"day": day_key, "odo": float(odo)}
            changed = True
        month_anchor = self._trip_data.get("odo_month_anchor")
        if not isinstance(month_anchor, dict) or month_anchor.get("month") != month_key:
            self._trip_data["odo_month_anchor"] = {
                "month": month_key,
                "odo": float(odo),
            }
            changed = True
        if changed and self._trip_store_loaded:
            self._trip_store.async_delay_save(
                lambda: dict(self._trip_data), _TRIP_STORE_SAVE_DELAY_SECONDS
            )

    def _distance_since_anchor(self, anchor_key: str, sub_key: str) -> float | None:
        anchor = self._trip_data.get(anchor_key)
        if not isinstance(anchor, dict):
            return None
        start = anchor.get("odo")
        odo = self._read_odo(self.data)
        if not isinstance(start, (int, float)) or odo is None:
            return None
        return round(max(0.0, odo - float(start)), 1)

    @property
    def distance_today_km(self) -> float | None:
        """Km driven since the first odometer reading of the local day."""
        return self._distance_since_anchor("odo_day_anchor", "day")

    @property
    def distance_this_month_km(self) -> float | None:
        """Km driven since the first odometer reading of the local month."""
        return self._distance_since_anchor("odo_month_anchor", "month")

    def _trip_history(self) -> list[dict[str, Any]]:
        h = self._trip_data.get("trip_history")
        return h if isinstance(h, list) else []

    @staticmethod
    def _parse_trip_dt(trip: dict[str, Any]) -> datetime | None:
        raw = trip.get("started_at")
        if not isinstance(raw, str):
            return None
        try:
            return datetime.fromisoformat(raw)
        except ValueError:
            return None

    @property
    def trips_this_month(self) -> int | None:
        """Number of recorded trips started in the current local month."""
        history = self._trip_history()
        if not history:
            return None
        now = dt_util.now()
        count = 0
        for trip in history:
            dt = self._parse_trip_dt(trip)
            if dt is not None:
                local = dt_util.as_local(dt)
                if local.year == now.year and local.month == now.month:
                    count += 1
        return count

    @property
    def avg_efficiency_30d_kwh_per_100km(self) -> float | None:
        """Distance-weighted average efficiency over the last 30 days."""
        cutoff = dt_util.now() - timedelta(days=30)
        dist_sum = 0.0
        energy_sum = 0.0
        for trip in self._trip_history():
            dt = self._parse_trip_dt(trip)
            if dt is None or dt_util.as_local(dt) < cutoff:
                continue
            dist = trip.get("distance_km")
            energy = trip.get("energy_kwh")
            if (
                isinstance(dist, (int, float))
                and dist > 0
                and isinstance(energy, (int, float))
                and energy > 0
            ):
                dist_sum += dist
                energy_sum += energy
        return self._efficiency_per_100km(energy_sum, dist_sum)

    @property
    def driving_streak_days(self) -> int | None:
        """Consecutive calendar days (ending today/yesterday) with a trip."""
        history = self._trip_history()
        if not history:
            return None
        days = set()
        for trip in history:
            dt = self._parse_trip_dt(trip)
            if dt is not None:
                days.add(dt_util.as_local(dt).date())
        if not days:
            return None
        today = dt_util.now().date()
        # Streak only counts if it reaches today or yesterday.
        if today not in days and (today - timedelta(days=1)) not in days:
            return 0
        streak = 0
        cursor = today if today in days else today - timedelta(days=1)
        while cursor in days:
            streak += 1
            cursor -= timedelta(days=1)
        return streak

    @property
    def km_per_soc_percent(self) -> float | None:
        """Estimated range per 1% of SoC (km/%), from range ÷ current SoC."""
        snap = self.data
        if snap is None or snap.realtime is None:
            return None
        rng = getattr(snap.realtime, "ev_endurance", None) or getattr(
            snap.realtime, "endurance_mileage", None
        )
        soc = getattr(snap.realtime, "elec_percent", None)
        if isinstance(rng, (int, float)) and isinstance(soc, (int, float)) and soc > 0:
            return round(float(rng) / float(soc), 2)
        return None

    def _track_charge_session(
        self,
        previous: VehicleSnapshot | None,
        current: VehicleSnapshot,
    ) -> None:
        """Record the timestamp + start SoC when ``charging_state`` -> 1.

        Coalesces micro-pauses during AC slow charge (see class constant
        :attr:`_CHARGE_SESSION_COALESCE_WINDOW_SECONDS`): an OFF gap
        shorter than the window does not end the current session.
        """

        now = datetime.now(tz=UTC)
        was = self._is_charging(previous)
        now_charging = self._is_charging(current)

        if now_charging and not was:
            # Resume vs new session: if the previous OFF window fell
            # inside the coalesce budget AND a session was active,
            # we keep started_at / start_soc / curve intact.
            within_coalesce = (
                self._charging_off_since is not None
                and (now - self._charging_off_since).total_seconds()
                < self._CHARGE_SESSION_COALESCE_WINDOW_SECONDS
            )
            if not (within_coalesce and self._charge_session_started_at is not None):
                self._charge_session_started_at = now
                self._charge_session_start_soc = self._read_soc(current)
                self._charge_curve = []
            self._charging_off_since = None

        elif not now_charging and was:
            # Mark when charging dropped out.  Defer closing the session
            # until the gap exceeds the coalesce window — see below.
            self._charging_off_since = now

        # If we're sitting in an OFF window past the coalesce budget,
        # actually close the session.  Runs on every update so the
        # timeout fires even without further state transitions.
        if (
            self._charging_off_since is not None
            and not now_charging
            and self._charge_session_started_at is not None
            and (now - self._charging_off_since).total_seconds()
            >= self._CHARGE_SESSION_COALESCE_WINDOW_SECONDS
        ):
            self._record_finished_session(current, self._read_soc(current))
            self._charge_session_started_at = None
            self._charge_session_start_soc = None
            self._charging_off_since = None

        if now_charging:
            soc = self._read_soc(current)
            power = None
            if current.realtime is not None:
                gl = getattr(current.realtime, "gl", None)
                if gl is not None:
                    power = round(abs(float(gl)) / 1000.0, 2)
            if soc is not None:
                self._charge_curve.append(
                    {
                        "t": datetime.now(tz=UTC).isoformat(),
                        "soc": soc,
                        "kw": power,
                    }
                )
                if len(self._charge_curve) > 200:
                    self._charge_curve = self._charge_curve[-200:]

    @property
    def trip_started_at(self) -> datetime | None:
        """UTC timestamp of the latest power-ON transition (trip start)."""
        trip_start = self._trip_data.get("trip_start")
        if not isinstance(trip_start, dict):
            return None
        try:
            return datetime.fromisoformat(str(trip_start.get("started_at")))
        except (TypeError, ValueError):
            return None

    @property
    def trip_start_soc(self) -> float | None:
        """Battery SoC captured when the car last powered ON."""
        trip_start = self._trip_data.get("trip_start")
        if isinstance(trip_start, dict):
            soc = trip_start.get("soc")
            if isinstance(soc, (int, float)):
                return float(soc)
        return None

    @property
    def trip_start_odometer(self) -> float | None:
        """Odometer captured when the car last powered ON."""
        trip_start = self._trip_data.get("trip_start")
        if isinstance(trip_start, dict):
            odo = trip_start.get("odometer")
            if isinstance(odo, (int, float)):
                return float(odo)
        return None

    @property
    def trip_distance_km(self) -> float | None:
        """Distance driven since the car last powered ON.

        Live while driving (current odometer minus trip-start odometer);
        after power-off it naturally freezes at the trip total.
        """
        start = self.trip_start_odometer
        if start is None:
            return None
        current = self._read_odo(self.data)
        if current is None or current < start:
            return None
        return round(current - start, 1)

    @property
    def last_trip(self) -> dict[str, Any] | None:
        """Summary of the most recently completed trip (power ON→OFF)."""
        trip = self._trip_data.get("last_trip")
        return trip if isinstance(trip, dict) else None

    @property
    def charge_session_started_at(self) -> datetime | None:
        """UTC timestamp of the latest ``charging_state==1`` transition."""
        return self._charge_session_started_at

    @property
    def charge_curve(self) -> list[dict[str, Any]]:
        """Timeline of (timestamp, SoC, kW) samples for the current session."""
        return list(self._charge_curve)

    @property
    def charge_sessions(self) -> list[dict[str, Any]]:
        """Summaries of the last completed charging sessions (max 10)."""
        return list(self._charge_sessions)

    def _record_finished_session(
        self, current: VehicleSnapshot, end_soc: float | None
    ) -> None:
        """Append a finished-session summary to the rolling history."""
        if self._charge_session_started_at is None:
            return
        started = self._charge_session_started_at
        duration_min = int((datetime.now(tz=UTC) - started).total_seconds() // 60)
        start_soc = self._charge_session_start_soc
        soc_added = None
        kwh_added = None
        if start_soc is not None and end_soc is not None:
            soc_added = round(max(0.0, float(end_soc) - float(start_soc)), 1)
            kwh_added = self._soc_to_kwh(soc_added)
        powers = [kw for s in self._charge_curve if (kw := s.get("kw")) is not None]
        avg_kw = round(sum(powers) / len(powers), 2) if powers else None
        self._charge_sessions.append(
            {
                "started_at": started.isoformat(),
                "ended_at": datetime.now(tz=UTC).isoformat(),
                "duration_minutes": duration_min,
                "start_soc": start_soc,
                "end_soc": end_soc,
                "soc_added": soc_added,
                "kwh_added": kwh_added,
                "avg_kw": avg_kw,
                "samples": len(self._charge_curve),
            }
        )
        if len(self._charge_sessions) > 10:
            self._charge_sessions = self._charge_sessions[-10:]
        # NOTE: the lifetime total_charge_kwh counter is maintained by
        # _track_total_charge (SoC-rise integrator), NOT here — the
        # session-close path was fragile (missed charges across restarts /
        # unclean closes).

    def _track_total_charge(self, current: VehicleSnapshot) -> None:
        """Accumulate lifetime charge energy from SoC rises while charging.

        Robust alternative to the old session-close accumulation: on every
        snapshot, while charging, add (SoC rise since the last reading) × pack
        to ``total_charge_kwh``. The SoC anchor is persisted so a restart
        mid-charge doesn't drop the gain across the gap; it's cleared when not
        charging so a driving SoC drop is never counted. Feeds the Energy
        dashboard + the battery side of the charging-efficiency meters.
        """
        soc = self._read_soc(current)
        if soc is None:
            return
        td = self._trip_data
        if not self._is_charging(current):
            if td.get("charge_soc_anchor") is not None:
                td["charge_soc_anchor"] = None
            return
        anchor = td.get("charge_soc_anchor")
        if isinstance(anchor, (int, float)) and soc > anchor:
            gain = self._soc_to_kwh(soc - anchor)
            if gain:
                td["total_charge_kwh"] = round(
                    float(td.get("total_charge_kwh", 0.0)) + gain, 2
                )
        td["charge_soc_anchor"] = float(soc)
        if self._trip_store_loaded:
            self._trip_store.async_delay_save(
                lambda: dict(td), _TRIP_STORE_SAVE_DELAY_SECONDS
            )

    def _track_charge_power_integral(self, current: VehicleSnapshot) -> None:
        """Integrate battery charge power over time → energy into the battery.

        Finer than the SoC-rise estimate (1% SoC ≈ 0.8 kWh): accumulates
        ``power(kW) × Δt(h)`` (trapezoidal) on every sample while charging,
        using ``realtime.gl`` (battery power, W). Gaps longer than
        :data:`_CHARGE_POWER_MAX_GAP_H` are skipped (lost samples) rather than
        extrapolated. Counter persisted in the trip store; reset of the sample
        anchors when not charging. This is the higher-resolution "energy into
        battery" figure for the charging-efficiency comparison.
        """
        now = datetime.now(tz=UTC)
        gl = (
            getattr(current.realtime, "gl", None)
            if current.realtime is not None
            else None
        )
        power_kw = (
            round(abs(float(gl)) / 1000.0, 3) if isinstance(gl, (int, float)) else None
        )
        if not self._is_charging(current) or power_kw is None:
            self._charge_power_last_at = None
            self._charge_power_last_kw = None
            return
        last_at = self._charge_power_last_at
        last_kw = self._charge_power_last_kw
        if last_at is not None and last_kw is not None:
            dt_h = (now - last_at).total_seconds() / 3600.0
            if 0 < dt_h <= _CHARGE_POWER_MAX_GAP_H:
                energy = (power_kw + last_kw) / 2.0 * dt_h  # trapezoidal
                td = self._trip_data
                td["total_charge_kwh_integrated"] = round(
                    float(td.get("total_charge_kwh_integrated", 0.0)) + energy, 3
                )
                if self._trip_store_loaded:
                    self._trip_store.async_delay_save(
                        lambda: dict(td), _TRIP_STORE_SAVE_DELAY_SECONDS
                    )
        self._charge_power_last_at = now
        self._charge_power_last_kw = power_kw

    @property
    def total_charge_energy_integrated_kwh(self) -> float | None:
        """Lifetime energy into the battery from power×time integration (kWh)."""
        val = self._trip_data.get("total_charge_kwh_integrated")
        return round(float(val), 2) if isinstance(val, (int, float)) else None

    @property
    def total_charge_kwh(self) -> float | None:
        """Lifetime cumulative charge energy added (kWh), monotonic.

        SoC-based estimate accumulated across finished charge sessions;
        suitable as a TOTAL_INCREASING source for the Energy dashboard.
        """
        val = self._trip_data.get("total_charge_kwh")
        return float(val) if isinstance(val, (int, float)) else None

    @property
    def charge_session_duration_minutes(self) -> int | None:
        """Minutes elapsed in the current charge session.

        Freezes once ``charging_state`` flips off so the value reads as
        "how long the actual charge lasted", not "how long ago we started".
        Without this guard the duration kept growing for the entire
        coalesce-window-plus-quiet-period after the car finished
        charging — observed in production where the duration sensor
        climbed to ``284 min`` ~80 min after a session that really
        ended at ``204 min``, because no fresh poll arrived to trigger
        the eventual reset in ``_track_charge_session``.
        """
        if self._charge_session_started_at is None:
            return None
        end = self._charging_off_since or datetime.now(tz=UTC)
        delta = end - self._charge_session_started_at
        return int(delta.total_seconds() // 60)

    @property
    def charge_session_soc_added(self) -> float | None:
        """SoC percentage points gained since the session started."""
        if self._charge_session_start_soc is None or self.data is None:
            return None
        current_soc = self._read_soc(self.data)
        if current_soc is None:
            return None
        delta = float(current_soc) - self._charge_session_start_soc
        return round(max(0.0, delta), 1)

    @property
    def charge_session_kwh_added(self) -> float | None:
        """Approximate kWh delivered to the battery this session.

        Derived from the SoC delta against the Sealion 7 Comfort
        nameplate capacity (82.5 kWh).  Coarse but works for any
        charging source — V2C, public AC, public DC — since the cloud
        reports the same SoC field regardless of where the energy
        came from.  When pyBYD adds per-model capacity metadata, swap
        the constant here.
        """
        soc_added = self.charge_session_soc_added
        if soc_added is None:
            return None
        return self._soc_to_kwh(soc_added)

    @property
    def time_until_full_minutes(self) -> int | None:
        """Estimated minutes to reach 100% at the current charge rate.

        Returns ``None`` when not actively charging or when the AC charge
        power is too low to extrapolate reliably.
        """
        if self.data is None or self.data.charging is None:
            return None
        if getattr(self.data.charging, "charging_state", None) != 1:
            return None
        # SoC remaining
        soc = self._read_soc(self.data)
        if soc is None or soc >= 100:
            return None
        remaining_soc = 100.0 - float(soc)
        # AC charge power (W) from realtime.gl when available
        power_w = None
        if self.data.realtime is not None:
            gl = getattr(self.data.realtime, "gl", None)
            if gl is not None:
                power_w = abs(float(gl))
        if power_w is None or power_w < 500:
            return None
        kwh_remaining = _DEFAULT_BATTERY_KWH * remaining_soc / 100.0
        minutes = (kwh_remaining * 1000.0 / power_w) * 60.0
        return int(minutes)

    @property
    def last_mqtt_push_at(self) -> datetime | None:
        """Timestamp of the most recent MQTT state push."""
        return self._last_mqtt_push_at

    @property
    def last_successful_fetch_at(self) -> datetime | None:
        """Timestamp of the most recent successful HTTP fetch."""
        return self._last_successful_fetch_at

    @property
    def last_energy_fetch_at(self) -> datetime | None:
        """Timestamp of the most recent ``Fetch energy data`` attempt."""
        return self._last_energy_fetch_at

    @property
    def last_energy_fetch_status(self) -> str | None:
        """Outcome of the most recent ``Fetch energy data`` attempt.

        One of ``ok`` (snapshot updated), ``unsupported`` (cloud
        rejected with code=1001 or similar), or ``error`` (generic
        failure). ``None`` until the first attempt fires.
        """
        return self._last_energy_fetch_status

    def _note_fetch_success(self) -> None:
        """Record a successful realtime fetch (scheduled OR on-demand).

        Single owner of the success-side health counters so every fetch path
        updates them identically — scattering this previously left force-poll
        fetches with a stale ``minutes_since_data`` and a stuck ``rate_limited``.
        """
        self._last_successful_fetch_at = datetime.now(tz=UTC)
        self._consecutive_fetch_failures = 0
        self._service_busy_streak = 0

    def _note_fetch_failure(self, exc: Exception) -> None:
        """Record a failed realtime fetch. Tracks 1008 bursts separately."""
        self._consecutive_fetch_failures += 1
        if isinstance(exc, BydServiceBusyError):
            self._service_busy_streak += 1
        else:
            self._service_busy_streak = 0

    @property
    def is_cloud_responsive(self) -> bool:
        """Whether the cloud has been responding recently.

        Flips to ``False`` after 3 consecutive failed fetches and resets
        to ``True`` on the next success.
        """
        return self._consecutive_fetch_failures < 3

    @property
    def connection_status(self) -> str:
        """Synthesised data-link state for the user.

        - ``paused``: polling disabled (by the user / an automation) — we
          are not even trying to fetch.
        - ``rate_limited``: the cloud is throttling us (repeated 1008
          service-busy) — usually transient, the adaptive interval backs off.
        - ``unreachable``: 3+ consecutive failed fetches — **no data is
          coming in**; the cloud is down, the network is broken, or the car
          is out of coverage / deep asleep and the cloud can't reach it.
        - ``ok``: data is flowing.
        """
        if not self._polling_enabled:
            return "paused"
        if self._service_busy_streak >= self._SERVICE_BUSY_BACKOFF_THRESHOLD:
            return "rate_limited"
        if not self.is_cloud_responsive:
            return "unreachable"
        return "ok"

    @property
    def minutes_since_last_data(self) -> float | None:
        """Minutes since we last received ANY data (fetch or MQTT push)."""
        candidates = [
            t
            for t in (self._last_successful_fetch_at, self._last_mqtt_push_at)
            if t is not None
        ]
        if not candidates:
            return None
        delta = datetime.now(tz=UTC) - max(candidates)
        return round(delta.total_seconds() / 60.0, 1)

    @property
    def effective_poll_interval_seconds(self) -> int:
        """Current next-poll interval (may differ from base due to adaptive)."""
        if self.update_interval is None:
            return 0
        return int(self.update_interval.total_seconds())

    # ------------------------------------------------------------------
    # Polling control
    # ------------------------------------------------------------------

    @property
    def polling_enabled(self) -> bool:
        return self._polling_enabled

    @property
    def poll_interval_seconds(self) -> int:
        """Return the configured telemetry poll interval in seconds."""
        return int(self._fixed_interval.total_seconds())

    def set_poll_interval(self, seconds: int) -> None:
        """Set telemetry poll interval base in seconds.

        The actual ``update_interval`` may be larger if the adaptive policy
        decides the vehicle is idle/sleeping — see
        :meth:`_compute_adaptive_interval`.
        """
        self._fixed_interval = timedelta(seconds=seconds)
        if self._polling_enabled:
            # Re-apply adaptive against current state so the new base
            # takes effect on the next tick.
            if self.data is not None:
                self._apply_adaptive_interval(self.data)
            else:
                self.update_interval = self._fixed_interval
        self.async_update_listeners()

    def set_polling_enabled(self, enabled: bool) -> bool:
        was_enabled = self._polling_enabled
        self._polling_enabled = bool(enabled)
        if not self._polling_enabled:
            self._cancel_pending_hvac_final_retry()
        self.update_interval = self._fixed_interval if self._polling_enabled else None
        return not was_enabled and self._polling_enabled

    async def async_set_polling_enabled(self, enabled: bool) -> None:
        """Update polling state and resume scheduling when re-enabled."""
        if self.set_polling_enabled(enabled):
            await self.async_request_refresh()

    async def async_force_refresh(self) -> None:
        self._force_next_refresh = True
        await self.async_request_refresh()

    # ------------------------------------------------------------------
    # Service helpers — direct BydCar calls
    # ------------------------------------------------------------------

    async def async_fetch_realtime(self) -> None:
        """Service handler: fetch fresh realtime via BydCar.

        Mirrors :meth:`async_fetch_charging` — pushes the refreshed
        snapshot via ``async_set_updated_data`` so dependent sensors
        refresh immediately instead of waiting for the next adaptive
        poll cycle (which can be 8 min away during idle/sleep buckets).
        """
        if self._car is None:
            return
        try:
            result = await self._car.update_realtime()
            _LOGGER.info(
                "fetch_realtime result: vin=%s, payload=%s",
                self._vin[-6:],
                result,
            )
            # Count on-demand/force fetches identically to scheduled polls
            # (resets BOTH failure counters — fixes a stuck rate_limited after
            # a successful force-poll during a 1008 burst).
            self._note_fetch_success()
            self.async_set_updated_data(self._car.state)
        except Exception as exc:  # noqa: BLE001
            # Record the failure too, so failed on-demand / wake-loop fetches
            # move the health counters instead of silently diverging.
            self._note_fetch_failure(exc)
            _LOGGER.warning(
                "Service fetch_realtime failed: vin=%s, error=%s",
                self._vin,
                exc,
            )

    async def async_fetch_hvac(self) -> None:
        """Service handler: fetch fresh HVAC via BydCar.

        Mirrors :meth:`async_fetch_charging` — propagates the refreshed
        snapshot so HVAC-derived sensors (wind position, recirculation,
        target temperature) refresh immediately on demand.
        """
        if self._car is None:
            return
        try:
            result = await self._car.update_hvac()
            _LOGGER.info(
                "fetch_hvac result: vin=%s, payload=%s",
                self._vin[-6:],
                result,
            )
            self.async_set_updated_data(self._car.state)
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning(
                "Service fetch_hvac failed: vin=%s, error=%s",
                self._vin,
                exc,
            )

    async def async_fetch_charging(self) -> None:
        """Service handler: refresh charging state + schedule from one homePage call.

        Hits ``/control/smartCharge/homePage`` once and updates both the
        live charging snapshot section (also pushed via MQTT) and the
        schedule section (HTTP-only).  Pushes the refreshed snapshot
        out so dependent sensors update immediately.
        """
        if self._car is None:
            return
        try:
            result = await self._car.update_charging()
            _LOGGER.info(
                "fetch_charging result: vin=%s, payload=%s",
                self._vin[-6:],
                result,
            )
            self.async_set_updated_data(self._car.state)
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning(
                "Service fetch_charging failed: vin=%s, error=%s",
                self._vin,
                exc,
            )

    @property
    def energy_supported(self) -> bool | None:
        """Whether ``/vehicleInfo/vehicle/getEnergyConsumption`` is supported.

        ``None`` until the first fetch has been attempted, then ``True``
        on success or ``False`` when the cloud answers ``code=1001`` for
        this VIN (typically Sealion 7 EU and a few other trims).
        """
        return self._energy_supported

    @property
    def energy_distribution_supported(self) -> bool | None:
        """Whether per-leg ``*_distribution`` fields populate for this VIN.

        Distinct from :attr:`energy_supported` because the endpoint can
        respond ``200 OK`` with the four ``*Distribution`` fields fixed
        to the ``"--"`` sentinel (which pyBYD normalises to ``None``).
        """
        return self._energy_distribution_supported

    async def async_fetch_energy(self) -> None:
        """Service handler: fetch energy consumption and log the raw response.

        Mirrors :meth:`async_fetch_charging` — pushes the refreshed snapshot
        out via ``async_set_updated_data`` so the dependent EnergyConsumption
        sensors update immediately instead of waiting for the next poll
        cycle (up to 8 min away with the adaptive backoff during idle).

        Always propagates at the end so the user sees visual feedback
        (sensor ``last_reported`` ticks) even when the endpoint is
        unsupported on this VIN — otherwise the button looks broken on
        e.g. Sealion 7 EU where the cloud returns ``code=1001``.
        """
        if self._car is None:
            return
        self._last_energy_fetch_at = datetime.now(tz=UTC)
        try:
            result = await self._car.update_energy()
            self._energy_supported = True
            self._last_energy_fetch_status = "ok"
            nearest = getattr(result, "nearest_energy_consumption", None)
            if nearest is not None:
                self._energy_distribution_supported = any(
                    getattr(nearest, attr, None) is not None
                    for attr in (
                        "drive_distribution",
                        "elect_distribution",
                        "air_distribution",
                        "other_distribution",
                    )
                )
            _LOGGER.info(
                "fetch_energy result: vin=%s, payload=%s",
                self._vin[-6:],
                result,
            )
        except BydEndpointNotSupportedError as exc:
            self._energy_supported = False
            self._energy_distribution_supported = False
            self._last_energy_fetch_status = "unsupported"
            _LOGGER.info(
                "fetch_energy not supported for this VIN; "
                "distribution sensors will be skipped: vin=%s, code=%s",
                self._vin[-6:],
                getattr(exc, "code", "?"),
            )
        except Exception as exc:  # noqa: BLE001
            self._last_energy_fetch_status = "error"
            _LOGGER.warning(
                "Service fetch_energy failed: vin=%s, error=%s",
                self._vin,
                exc,
            )
        # Always propagate — covers every path so the diagnostic
        # ``last_energy_fetch_at`` / ``last_energy_fetch_status``
        # sensors tick on every button press, and the dependent
        # EnergyConsumption sensors refresh on the success path.
        if self._car is not None:
            self.async_set_updated_data(self._car.state)

    async def async_request_schedule_update(
        self, property_name: str, new_value: Any
    ) -> None:
        """Queue a debounced update for the charging schedule."""
        self._pending_schedule_updates[property_name] = new_value

        if self._debounce_timer is not None:
            self._debounce_timer()
            self._debounce_timer = None

        self.update_pending = True

        @callback
        def _execute_update(_now: Any) -> None:
            self._debounce_timer = None
            self.hass.async_create_task(self._async_execute_schedule_update())

        self._debounce_timer = async_call_later(
            self.hass,
            10.0,
            _execute_update,
        )

    async def _async_execute_schedule_update(self) -> None:
        """Execute the pending schedule update.

        Bails out cleanly when there is no live charging-schedule baseline
        to merge against — refusing to overwrite the cloud with synthetic
        defaults is safer than guessing.
        """
        if not self._pending_schedule_updates:
            self.update_pending = False
            return

        charge = None
        if self.data and self.data.charging_schedule:
            charge = self.data.charging_schedule.charge
        if charge is None:
            _LOGGER.warning(
                "Schedule update aborted vin=%s — no current schedule baseline",
                self._vin[-6:],
            )
            self._pending_schedule_updates.clear()
            self.update_pending = False
            return

        updates = self._pending_schedule_updates
        enabled = updates.get(
            "enabled", charge.status if charge.status is not None else True
        )
        charge_to_full = updates.get(
            "charge_to_full",
            charge.charge_until_full if charge.charge_until_full is not None else True,
        )
        pattern = updates.get(
            "pattern", charge.charge_way if charge.charge_way is not None else "e"
        )

        start_time_obj = updates.get("start_time")
        if start_time_obj is not None:
            start_charge_time = start_time_obj.strftime("%H:%M")
        elif charge.start_time is not None:
            start_charge_time = charge.start_time.strftime("%H:%M")
        else:
            _LOGGER.warning(
                "Schedule update aborted vin=%s — no start_time baseline",
                self._vin[-6:],
            )
            self._pending_schedule_updates.clear()
            self.update_pending = False
            return

        if charge_to_full:
            end_charge_time = "full"
        else:
            end_time_obj = updates.get("end_time")
            if end_time_obj is not None:
                end_charge_time = end_time_obj.strftime("%H:%M")
            elif charge.end_time is not None:
                end_charge_time = charge.end_time.strftime("%H:%M")
            else:
                _LOGGER.warning(
                    "Schedule update aborted vin=%s — no end_time baseline",
                    self._vin[-6:],
                )
                self._pending_schedule_updates.clear()
                self.update_pending = False
                return

        self._pending_schedule_updates.clear()

        try:
            await self.async_save_charging_schedule(
                start_charge_time=start_charge_time,
                end_charge_time=end_charge_time,
                charge_way=pattern,
                enabled=enabled,
            )
        except (
            BydEndpointNotSupportedError,
            BydRemoteControlError,
            BydAuthenticationError,
            BydApiError,
            BydTransportError,
        ) as exc:
            _LOGGER.warning(
                "Debounced schedule save failed vin=%s: %s", self._vin[-6:], exc
            )
        finally:
            self.update_pending = False
            await self.async_request_refresh()

    async def async_save_charging_schedule(
        self,
        *,
        start_charge_time: str,
        end_charge_time: str,
        charge_way: str,
        enabled: bool = True,
    ) -> None:
        """Push a new smart-charging schedule and refresh the snapshot.

        Wire format follows the captured ``saveOrUpdate`` request:
        ``startChargeTime`` / ``endChargeTime`` are ``"HH:MM"`` strings
        (or the ``"full"`` sentinel on end), ``chargeWay`` selects the
        repeat (``"s"`` / ``"e"`` / comma-separated weekday indices),
        ``status`` is the enabled flag.
        """
        if self._car is None:
            raise HomeAssistantError(
                f"BYD vehicle {self._vin[-6:]} not ready for charging commands"
            )
        try:
            await self._car.save_charging_schedule(
                start_charge_time=start_charge_time,
                end_charge_time=end_charge_time,
                charge_way=charge_way,
                enabled=enabled,
            )
        except BydEndpointNotSupportedError as exc:
            raise HomeAssistantError(
                "save_charging_schedule not supported for this vehicle/region"
            ) from exc
        except BydDataUnavailableError as exc:
            # BYD code 6002: cloud accepted the request but couldn't
            # reach the vehicle (weak cellular signal, parked in a
            # garage, etc.).  Recoverable — surface a friendlier
            # message than the raw "endpoint failed: code=6002 …".
            raise HomeAssistantError(
                "Vehicle has a weak network signal — try again when "
                "it's back in coverage"
            ) from exc
        except BydRemoteControlError as exc:
            raise HomeAssistantError(
                f"save_charging_schedule failed to settle: {exc}"
            ) from exc
        except BydAuthenticationError as exc:
            raise HomeAssistantError(
                f"save_charging_schedule failed (auth): {exc}"
            ) from exc
        except (BydApiError, BydTransportError) as exc:
            raise HomeAssistantError(f"save_charging_schedule failed: {exc}") from exc

        _LOGGER.info(
            "save_charging_schedule accepted: vin=%s, "
            "start=%s end=%s charge_way=%s enabled=%s",
            self._vin[-6:],
            start_charge_time,
            end_charge_time,
            charge_way,
            enabled,
        )
        # ``car.save_charging_schedule`` polls ``changeResult`` until
        # the cloud reports a terminal state and then refreshes the
        # snapshot via ``update_charging`` — push the new snapshot out
        # to subscribers so dependent sensors update immediately.
        self.async_set_updated_data(self._car.state)

    async def async_start_charging(self) -> None:
        """Start charging immediately and refresh charging state on success.

        Raises :class:`HomeAssistantError` on failure (auth, transport,
        unsupported endpoint, polling timeout) so service callers see a
        loud failure rather than a silent no-op.
        """
        if self._car is None:
            raise HomeAssistantError(
                f"BYD vehicle {self._vin[-6:]} not ready for charging commands"
            )
        try:
            result = await self._car.start_charging()
        except BydEndpointNotSupportedError as exc:
            raise HomeAssistantError(
                "start_charging not supported for this vehicle/region"
            ) from exc
        except BydRemoteControlError as exc:
            raise HomeAssistantError(f"start_charging failed to settle: {exc}") from exc
        except BydAuthenticationError as exc:
            raise HomeAssistantError(f"start_charging failed (auth): {exc}") from exc
        except (BydApiError, BydTransportError) as exc:
            raise HomeAssistantError(f"start_charging failed: {exc}") from exc

        _LOGGER.info(
            "start_charging settled: vin=%s, message=%s",
            self._vin[-6:],
            result.message,
        )

        try:
            await self._car.update_charging()
            await self.async_request_refresh()
        except Exception:  # noqa: BLE001
            _LOGGER.debug(
                "Post-start_charging refresh failed (non-fatal)",
                exc_info=True,
            )

    async def async_stop_charging(self) -> None:
        """Stop charging immediately and refresh charging state."""
        if self._car is None:
            raise HomeAssistantError(
                f"BYD vehicle {self._vin[-6:]} not ready for charging commands"
            )
        try:
            result = await self._car.stop_charging()
        except BydEndpointNotSupportedError as exc:
            raise HomeAssistantError(
                "stop_charging not supported for this vehicle/region"
            ) from exc
        except BydRemoteControlError as exc:
            raise HomeAssistantError(f"stop_charging failed to settle: {exc}") from exc
        except BydAuthenticationError as exc:
            raise HomeAssistantError(f"stop_charging failed (auth): {exc}") from exc
        except (BydApiError, BydTransportError) as exc:
            raise HomeAssistantError(f"stop_charging failed: {exc}") from exc

        _LOGGER.info(
            "stop_charging settled: vin=%s, message=%s",
            self._vin[-6:],
            result.message,
        )

        try:
            await self._car.update_charging()
            await self.async_request_refresh()
        except Exception:  # noqa: BLE001
            _LOGGER.debug(
                "Post-stop_charging refresh failed (non-fatal)",
                exc_info=True,
            )


class BydGpsUpdateCoordinator(DataUpdateCoordinator[VehicleSnapshot]):
    """Coordinator for GPS updates for a single VIN.

    Uses the ``BydCar`` from the telemetry coordinator so GPS data flows
    through the same state engine and benefits from the value-quality
    validators (Null Island rejection).
    """

    # See note on BydDataUpdateCoordinator above — same parent annotations.
    data: VehicleSnapshot | None
    update_interval: timedelta | None

    def __init__(
        self,
        hass: HomeAssistant,
        api: BydApi,
        vehicle: Vehicle,
        vin: str,
        poll_interval: int,
        *,
        telemetry_coordinator: BydDataUpdateCoordinator | None = None,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_gps_{vin[-6:]}",
            update_interval=timedelta(seconds=poll_interval),
        )
        self._api = api
        self._vehicle = vehicle
        self._vin = vin
        self._telemetry_coordinator = telemetry_coordinator
        self._fixed_interval = timedelta(seconds=poll_interval)
        self._polling_enabled = True
        self._force_next_refresh = False

    @property
    def polling_enabled(self) -> bool:
        return self._polling_enabled

    @property
    def poll_interval_seconds(self) -> int:
        """Return the configured GPS poll interval in seconds."""
        return int(self._fixed_interval.total_seconds())

    def set_poll_interval(self, seconds: int) -> None:
        """Set GPS poll interval in seconds."""
        self._fixed_interval = timedelta(seconds=seconds)
        if self._polling_enabled:
            self.update_interval = self._fixed_interval
        self.async_update_listeners()

    def set_polling_enabled(self, enabled: bool) -> bool:
        was_enabled = self._polling_enabled
        self._polling_enabled = bool(enabled)
        self.update_interval = self._fixed_interval if self._polling_enabled else None
        return not was_enabled and self._polling_enabled

    async def async_set_polling_enabled(self, enabled: bool) -> None:
        """Update polling state and resume scheduling when re-enabled."""
        if self.set_polling_enabled(enabled):
            await self.async_request_refresh()

    async def async_force_refresh(self) -> None:
        self._force_next_refresh = True
        await self.async_request_refresh()

    async def async_fetch_gps(self) -> None:
        """Service handler: fetch fresh GPS via BydCar."""
        car = self._get_car()
        if car is None:
            return
        try:
            result = await car.update_gps()
            _LOGGER.info(
                "fetch_gps result: vin=%s, payload=%s",
                self._vin[-6:],
                result,
            )
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning(
                "Service fetch_gps failed: vin=%s, error=%s",
                self._vin,
                exc,
            )

    def _get_car(self) -> BydCar | None:
        """Return BydCar from telemetry coordinator."""
        if self._telemetry_coordinator is not None:
            return self._telemetry_coordinator.car
        return None

    @callback
    def _async_handle_state_push(self, snapshot: VehicleSnapshot) -> None:
        """Update GPS state from a push and reset next poll on new GPS timestamp."""
        if snapshot.gps is None:
            return

        previous_timestamp = None
        if self.data is not None and self.data.gps is not None:
            previous_timestamp = getattr(self.data.gps, "gps_timestamp", None)

        current_timestamp = getattr(snapshot.gps, "gps_timestamp", None)

        if current_timestamp is not None and current_timestamp != previous_timestamp:
            self.async_set_updated_data(snapshot)
            return

        self.data = snapshot
        self.last_update_success = True
        self.async_update_listeners()

    async def _async_update_data(self) -> VehicleSnapshot:
        """Fetch GPS data and return the current car state snapshot."""
        _LOGGER.debug("GPS refresh started: vin=%s", self._vin[-6:])
        force = self._force_next_refresh
        self._force_next_refresh = False

        if not self._polling_enabled and not force:
            if self.data is not None:
                return self.data
            return VehicleSnapshot(vehicle=self._vehicle)

        car = self._get_car()
        if car is None:
            if self.data is not None:
                return self.data
            return VehicleSnapshot(vehicle=self._vehicle)

        try:
            await car.update_gps()
        except _AUTH_ERRORS:
            raise
        except BydDataUnavailableError:
            _LOGGER.debug(
                "GPS data unavailable (vehicle may lack signal): vin=%s",
                self._vin,
            )
        except _RECOVERABLE_ERRORS as exc:
            _LOGGER.warning("GPS fetch failed: vin=%s, error=%s", self._vin, exc)

        snapshot = car.state
        if snapshot.gps is None:
            if self.data is not None:
                _LOGGER.debug(
                    "GPS unavailable, preserving last known position: vin=%s",
                    self._vin,
                )
                return self.data
            return VehicleSnapshot(vehicle=self._vehicle)

        if self._api.debug_dumps_enabled and snapshot.gps is not None:
            dump: dict[str, Any] = {
                "vin": self._vin,
                "sections": {"gps": snapshot.gps.model_dump(mode="json")},
            }
            self.hass.async_create_task(self._api.async_write_debug_dump("gps", dump))
        _LOGGER.debug(
            "GPS refresh succeeded: vin=%s, gps=%s",
            self._vin[-6:],
            snapshot.gps is not None,
        )
        return snapshot


def get_vehicle_display(vehicle: Vehicle) -> str:
    return vehicle.model_name or vehicle.vin
