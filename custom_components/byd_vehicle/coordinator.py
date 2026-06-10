"""Data coordinators for BYD Vehicle."""

from __future__ import annotations

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
    BydSessionExpiredError,
    BydTransportError,
    CommandAckEvent,
    CommandLifecycleEvent,
    VehicleSnapshot,
)
from pybyd.config import BydConfig, DeviceProfile
from pybyd.models.realtime import PowerGear
from pybyd.models.vehicle import Vehicle

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
_HA_EVENT_POWER_CHANGED: str = f"{DOMAIN}_power_changed"

# Trip snapshots persist across HA restarts so a restart mid-trip does
# not lose the power-on baseline (SoC/odometer at trip start).
_TRIP_STORE_VERSION = 1
_TRIP_STORE_SAVE_DELAY_SECONDS = 2.0

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
        """Handle generic MQTT events from pyBYD."""
        if self._debug_dumps_enabled:
            dump: dict[str, Any] = {
                "vin": vin,
                "mqtt_event": event,
                "respond_data": respond_data,
            }
            self._hass.async_create_task(
                self._async_write_debug_dump(f"mqtt_{event}", dump)
            )

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
            self._client = BydClient(
                self._config,
                session=self._http_session,
                on_mqtt_event=self._handle_mqtt_event,
                on_command_ack=self._handle_command_ack,
                on_command_lifecycle=self._handle_command_lifecycle,
            )
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
    data: VehicleSnapshot | None  # type: ignore[assignment]
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
        self._realtime_endpoint_unsupported: bool = False
        self._energy_supported: bool | None = None
        self._charge_session_started_at: datetime | None = None
        self._charge_session_start_soc: float | None = None
        self._last_mqtt_push_at: datetime | None = None
        self._last_successful_fetch_at: datetime | None = None
        self._consecutive_fetch_failures: int = 0
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
        """
        try:
            await self.async_fetch_realtime()
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning("force_poll: realtime failed vin=%s err=%s", self._vin, exc)
        try:
            await self.async_fetch_charging()
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning("force_poll: charging failed vin=%s err=%s", self._vin, exc)

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

        if not self._polling_enabled and not force:
            if self.data is not None:
                return self.data
            return VehicleSnapshot(vehicle=self._vehicle)

        if self._car is None:
            self._car = await self._api.async_get_car(self._vin, self._vehicle)

        car = self._car

        # --- Realtime ---
        try:
            await car.update_realtime()
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
            self._consecutive_fetch_failures += 1
            _LOGGER.warning(
                "Realtime fetch failed: vin=%s, error=%s, consecutive_failures=%d",
                self._vin,
                exc,
                self._consecutive_fetch_failures,
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

        # Health tracking: a real realtime payload counts as a success.
        if snapshot.realtime is not None:
            self._last_successful_fetch_at = datetime.now(tz=UTC)
            self._consecutive_fetch_failures = 0

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
          * 1×   — actively charging OR vehicle on (driving / climate active)
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
            if charging is not None and getattr(charging, "charging_state", None) == 1:
                multiplier = 1
            elif getattr(realtime, "is_charging", None) is True:
                multiplier = 1
            elif getattr(realtime, "is_vehicle_on", None) is True:
                multiplier = 1
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

        return base * multiplier

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

    def _maybe_handle_state_transitions(
        self,
        previous: VehicleSnapshot | None,
        current: VehicleSnapshot,
    ) -> None:
        """React to ad-hoc state transitions by scheduling targeted refreshes.

        - **OTA done** (upgrade_status on → off): the next regular poll won't
          fetch the charging snapshot or firmware metadata for several
          minutes, so the user sees stale values right after the OTA. Force
          both immediately.
        - **Plug-in** (plug off → on): same problem — charging state lives on
          a separate endpoint that the regular poll doesn't hit. The user
          wants ``charge_session_phase`` to flip to ``plugged_idle`` within
          seconds of plugging in, not minutes.

        Refreshes are fire-and-forget background tasks so we don't block
        the main update path. They use ``async_create_task`` so failures
        are isolated and logged but don't propagate.
        """
        if previous is None:
            return

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

        prev_ota = _ota_active(previous)
        new_ota = _ota_active(current)
        if prev_ota is True and new_ota is False:
            _LOGGER.info(
                "OTA finished on vin=%s — refreshing charging + firmware",
                self._vin[-6:],
            )
            self.hass.async_create_task(self._async_post_ota_refresh())

        prev_plug = _plug_connected(previous)
        new_plug = _plug_connected(current)
        if prev_plug is False and new_plug is True:
            _LOGGER.info(
                "Plug-in detected on vin=%s — refreshing charging snapshot",
                self._vin[-6:],
            )
            self.hass.async_create_task(self._async_post_plug_refresh())

        def _power_on(snap: VehicleSnapshot | None) -> bool | None:
            if snap is None or snap.realtime is None:
                return None
            gear = getattr(snap.realtime, "power_gear", None)
            if gear == PowerGear.ON:
                return True
            if gear == PowerGear.OFF:
                return False
            return None  # missing / UNKNOWN / sentinel payload

        prev_power = _power_on(previous)
        new_power = _power_on(current)
        if prev_power is not None and new_power is not None and prev_power != new_power:
            self._handle_power_transition(is_on=new_power, snapshot=current)

        self._maybe_fire_capability_changes(previous, current)

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
        soc: Any = None
        odometer: Any = None
        if snapshot.charging is not None:
            soc = getattr(snapshot.charging, "soc", None)
        if snapshot.realtime is not None:
            odometer = getattr(snapshot.realtime, "total_mileage", None)
            if soc is None:
                soc = getattr(snapshot.realtime, "elec_percent", None)
        latitude: Any = None
        longitude: Any = None
        if snapshot.gps is not None:
            latitude = getattr(snapshot.gps, "latitude", None)
            longitude = getattr(snapshot.gps, "longitude", None)

        payload: dict[str, Any] = {
            "vin": self._vin,
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
            if trip is not None:
                self._trip_data["last_trip"] = trip
                payload["trip"] = trip
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
        """Background: refresh charging snapshot after plug detected."""
        try:
            await self.async_fetch_charging()
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning(
                "post-plug charging refresh failed vin=%s err=%s", self._vin, exc
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

    def _track_charge_session(
        self,
        previous: VehicleSnapshot | None,
        current: VehicleSnapshot,
    ) -> None:
        """Record the timestamp + start SoC when ``charging_state`` -> 1."""

        def _is_charging(snap: VehicleSnapshot | None) -> bool:
            if snap is None or snap.charging is None:
                return False
            return getattr(snap.charging, "charging_state", None) == 1

        def _current_soc(snap: VehicleSnapshot | None) -> float | None:
            if snap is None:
                return None
            if snap.charging is not None:
                soc = getattr(snap.charging, "soc", None)
                if soc is not None:
                    return float(soc)
            if snap.realtime is not None:
                soc = getattr(snap.realtime, "elec_percent", None)
                if soc is not None:
                    return float(soc)
            return None

        was = _is_charging(previous)
        now_charging = _is_charging(current)
        if now_charging and not was:
            self._charge_session_started_at = datetime.now(tz=UTC)
            self._charge_session_start_soc = _current_soc(current)
        elif not now_charging and was:
            # Keep the timestamp on the way out so automations can read
            # "last charge started at" — only clear on a fresh disconnect
            connect_state = (
                getattr(current.charging, "connect_state", None)
                if current.charging
                else None
            )
            if connect_state == 0:
                self._charge_session_started_at = None
                self._charge_session_start_soc = None

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
        if start is None or self.data is None or self.data.realtime is None:
            return None
        current = getattr(self.data.realtime, "total_mileage", None)
        if not isinstance(current, (int, float)) or current < start:
            return None
        return round(float(current) - start, 1)

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
    def charge_session_duration_minutes(self) -> int | None:
        """Minutes elapsed since the last charge session began."""
        if self._charge_session_started_at is None:
            return None
        delta = datetime.now(tz=UTC) - self._charge_session_started_at
        return int(delta.total_seconds() // 60)

    @property
    def charge_session_soc_added(self) -> float | None:
        """SoC percentage points gained since the session started."""
        if self._charge_session_start_soc is None or self.data is None:
            return None
        current_soc = None
        if self.data.charging is not None:
            current_soc = getattr(self.data.charging, "soc", None)
        if current_soc is None and self.data.realtime is not None:
            current_soc = getattr(self.data.realtime, "elec_percent", None)
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
        return round(soc_added * _DEFAULT_BATTERY_KWH / 100.0, 2)

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
        soc = getattr(self.data.charging, "soc", None)
        if soc is None and self.data.realtime is not None:
            soc = getattr(self.data.realtime, "elec_percent", None)
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
    def is_cloud_responsive(self) -> bool:
        """Whether the cloud has been responding recently.

        Flips to ``False`` after 3 consecutive failed fetches and resets
        to ``True`` on the next success.
        """
        return self._consecutive_fetch_failures < 3

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
        """Service handler: fetch fresh realtime via BydCar."""
        if self._car is None:
            return
        try:
            result = await self._car.update_realtime()
            _LOGGER.info(
                "fetch_realtime result: vin=%s, payload=%s",
                self._vin[-6:],
                result,
            )
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning(
                "Service fetch_realtime failed: vin=%s, error=%s",
                self._vin,
                exc,
            )

    async def async_fetch_hvac(self) -> None:
        """Service handler: fetch fresh HVAC via BydCar."""
        if self._car is None:
            return
        try:
            result = await self._car.update_hvac()
            _LOGGER.info(
                "fetch_hvac result: vin=%s, payload=%s",
                self._vin[-6:],
                result,
            )
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
        """Service handler: fetch energy consumption and log the raw response."""
        if self._car is None:
            return
        try:
            result = await self._car.update_energy()
            self._energy_supported = True
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
            _LOGGER.info(
                "fetch_energy not supported for this VIN; "
                "distribution sensors will be skipped: vin=%s, code=%s",
                self._vin[-6:],
                getattr(exc, "code", "?"),
            )
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning(
                "Service fetch_energy failed: vin=%s, error=%s",
                self._vin,
                exc,
            )

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


class BydGpsUpdateCoordinator(DataUpdateCoordinator[VehicleSnapshot]):
    """Coordinator for GPS updates for a single VIN.

    Uses the ``BydCar`` from the telemetry coordinator so GPS data flows
    through the same state engine and benefits from the value-quality
    validators (Null Island rejection).
    """

    # See note on BydDataUpdateCoordinator above — same parent annotations.
    data: VehicleSnapshot | None  # type: ignore[assignment]
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
