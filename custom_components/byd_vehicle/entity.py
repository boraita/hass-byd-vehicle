"""Base entity mixins for BYD Vehicle."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from pybyd import (
    BydControlPasswordError,
    BydEndpointNotSupportedError,
    BydRemoteControlError,
    VehicleSnapshot,
)
from pybyd.models.energy import EnergyConsumption
from pybyd.models.gps import GpsInfo
from pybyd.models.hvac import HvacStatus
from pybyd.models.realtime import VehicleRealtimeData
from pybyd.models.vehicle import Vehicle

from . import _logic
from .const import DOMAIN
from .coordinator import BydDataUpdateCoordinator, get_vehicle_display

_LOGGER = logging.getLogger(__name__)


class BydVehicleEntity(CoordinatorEntity[BydDataUpdateCoordinator]):
    """Mixin providing common properties for BYD vehicle entities.

    Subclasses must set ``_vin`` and ``_vehicle`` before calling
    ``super().__init__``.  Data is read from ``coordinator.data`` which
    is a :class:`VehicleSnapshot` — no local shadow state, no optimistic
    tracking.
    """

    _vin: str
    _vehicle: Vehicle

    @property
    def device_info(self) -> DeviceInfo:
        """Return device info common to every BYD entity."""
        return DeviceInfo(
            identifiers={(DOMAIN, self._vin)},
            name=get_vehicle_display(self._vehicle),
            manufacturer=self._vehicle.brand_name or "BYD",
            model=self._vehicle.model_name,
            serial_number=self._vin,
            hw_version=self._vehicle.tbox_version or None,
        )

    @property
    def available(self) -> bool:
        """Available when coordinator has a snapshot with vehicle data."""
        if not super().available:
            return False
        return self.coordinator.data is not None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return VIN as the default extra attribute."""
        return {"vin": self._vin}

    # ------------------------------------------------------------------
    # Snapshot data helpers
    # ------------------------------------------------------------------

    def _snapshot(self) -> VehicleSnapshot | None:
        """Return the coordinator's current snapshot."""
        return self.coordinator.data

    def _get_realtime(self) -> VehicleRealtimeData | None:
        """Return realtime data from the snapshot."""
        snap = self._snapshot()
        return snap.realtime if snap is not None else None

    def _get_hvac_status(self) -> HvacStatus | None:
        """Return HVAC status from the snapshot."""
        snap = self._snapshot()
        return snap.hvac if snap is not None else None

    def _get_gps(self) -> GpsInfo | None:
        """Return GPS data from the snapshot."""
        snap = self._snapshot()
        return snap.gps if snap is not None else None

    def _get_energy(self) -> EnergyConsumption | None:
        """Return energy-consumption data from the snapshot."""
        snap = self._snapshot()
        return snap.energy if snap is not None else None

    # Exact ``source`` token → snapshot-section resolver. Prefix families
    # (``energy_*``, ``charging_schedule_*``) are handled after this table in
    # _get_source_obj, so exact "energy"/"charging_schedule" win over them.
    _EXACT_SOURCE_RESOLVERS: dict[str, Callable[[BydVehicleEntity], Any]] = {
        "realtime": lambda self: self._get_realtime(),
        "hvac": lambda self: self._get_hvac_status(),
        "gps": lambda self: self._get_gps(),
        "energy": lambda self: self._get_energy(),
        "charging": lambda self: (
            s.charging if (s := self._snapshot()) is not None else None
        ),
        "vehicle": lambda self: (
            s.vehicle if (s := self._snapshot()) is not None else self._vehicle
        ),
        "coordinator": lambda self: self.coordinator,
        "snapshot": lambda self: self._snapshot(),
    }

    def _get_source_obj(self, source: str = "realtime") -> Any | None:
        """Return the snapshot section for the given *source* string.

        Supported values: ``"realtime"``, ``"hvac"``, ``"gps"``,
        ``"energy"``, ``"energy_cumulative"``, ``"energy_nearest"``,
        ``"energy_self_graph"``, ``"energy_auto_model_graph"``,
        ``"charging"`` (smart-charging status from ``/smartCharge/homePage``),
        ``"charging_schedule"``, ``"charging_schedule_charge"``,
        ``"charging_schedule_journey"``, ``"snapshot"`` (the full
        :class:`VehicleSnapshot` for cross-section merged value_fn lookups).
        """
        resolver = self._EXACT_SOURCE_RESOLVERS.get(source)
        if resolver is not None:
            return resolver(self)
        if source.startswith("energy_"):
            energy = self._get_energy()
            if energy is None:
                return None
            attr = source[len("energy_") :]
            # Map URL-style suffixes to model attribute names.
            attr_map = {
                "cumulative": "cumulative_energy_consumption",
                "nearest": "nearest_energy_consumption",
                "self_graph": "self_graph",
                "auto_model_graph": "auto_model_graph",
            }
            return getattr(energy, attr_map.get(attr, attr), None)
        if source.startswith("charging_schedule"):
            snap = self._snapshot()
            schedule = snap.charging_schedule if snap is not None else None
            if source == "charging_schedule":
                return schedule
            if schedule is None:
                return None
            attr = source[len("charging_schedule_") :]
            return getattr(schedule, attr, None)
        return None

    def _is_vehicle_on(self) -> bool:
        """Return True when the vehicle is on."""
        realtime = self._get_realtime()
        if realtime is None:
            return False
        return bool(realtime.is_vehicle_on)

    # ------------------------------------------------------------------
    # Command helpers
    # ------------------------------------------------------------------

    def _connectivity_hint(self) -> str:
        """Suffix telling the user to check the connection when offline.

        A command that fails while the T-Box has no signal is almost always
        connectivity, not a real rejection — point at that instead of leaving
        a bare error (e.g. "close windows" failing because the car is out of
        coverage).
        """
        realtime = self._get_realtime()
        online = getattr(realtime, "is_online", None) if realtime is not None else None
        status = getattr(self.coordinator, "connection_status", None)
        if online is False or status in ("unreachable", "rate_limited"):
            return (
                " — the car looks offline (weak signal / no coverage); "
                "check its connection and retry once it is reachable"
            )
        return ""

    def _command_pin_error_message(self) -> str:
        """Return a user-facing error message for command PIN issues."""
        if self.coordinator.has_pin_configured:
            return (
                "Command PIN is invalid or cloud control is locked — "
                "reconfigure the integration to update your Control PIN"
            )
        return "Control PIN is not configured; set Control PIN to enable actions"

    #: Seconds to wait after a successful action before forcing a poll.
    #: Long enough for the BYD cloud + T-Box to reflect the new physical
    #: state (windows in vent position, lock state, climate on, etc.)
    #: in the next realtime payload, short enough that the user sees the
    #: UI update without manually refreshing.
    _POST_ACTION_REFRESH_DELAY = 15

    async def _execute_car_command(
        self,
        coro: Any,
        *,
        command: str,
    ) -> None:
        """Execute a BydCar capability command with HA error handling.

        On :class:`BydRemoteControlError` (rejection / timeout / asleep car)
        the failure is surfaced as :class:`HomeAssistantError` — pyBYD has
        already rolled back the optimistic projection, so no fake state is
        shown. Other failures are likewise re-raised as
        :class:`HomeAssistantError`.

        After a genuine success schedules a force-poll
        :attr:`_POST_ACTION_REFRESH_DELAY` seconds later so the UI catches up
        to the real vehicle state without the user pressing Force poll.
        """
        if not self.coordinator.has_operation_pin:
            raise HomeAssistantError(self._command_pin_error_message())
        try:
            await coro
        except BydRemoteControlError as exc:
            # pyBYD already rolled back the optimistic projection, so the
            # reported state stays honest; surface the failure honestly.
            raise self._command_error(
                "remote_control", command, exc, getattr(exc, "code", None)
            ) from exc
        except BydControlPasswordError as exc:
            raise self._command_error(
                "password", command, exc, getattr(exc, "code", None)
            ) from exc
        except BydEndpointNotSupportedError as exc:
            raise self._command_error("unsupported", command, exc, None) from exc
        except Exception as exc:  # noqa: BLE001
            raise self._command_error("generic", command, exc, None) from exc
        self._schedule_post_action_refresh(command)

    def _command_error(
        self, kind: str, command: str, exc: Exception, code: str | None
    ) -> HomeAssistantError:
        """Build the user-facing error for a failed command.

        Classification (the exception type → ``kind``) lives here; the pure
        message mapping + the connectivity-hint folding live in ``_logic`` so
        the wording is unit-tested without HA.
        """
        base, mode = _logic.command_error(kind, command, code, str(exc))
        hint = self._connectivity_hint()
        if mode == _logic.HINT_APPEND_OR_RETRY:
            msg = base + (hint or " — try again")
        elif mode == _logic.HINT_APPEND:
            msg = base + hint
        else:
            msg = base
        _LOGGER.warning("%s command failed: %s (code=%s)", command, msg, code)
        return HomeAssistantError(msg)

    def _schedule_post_action_refresh(self, command: str) -> None:
        """Schedule a one-shot force-poll after a successful command.

        Run as a fire-and-forget timer; a failure to refresh logs at
        debug and does not propagate — the next regular adaptive poll
        will catch up anyway.
        """

        async def _refresh(_now: Any) -> None:
            try:
                await self.coordinator.async_force_refresh()
                _LOGGER.debug(
                    "Post-action refresh fired for command=%s vin=%s",
                    command,
                    self.coordinator.vin[-6:] if self.coordinator.vin else "?",
                )
            except Exception:  # noqa: BLE001
                _LOGGER.debug(
                    "Post-action refresh failed for command=%s",
                    command,
                    exc_info=True,
                )

        async_call_later(self.hass, self._POST_ACTION_REFRESH_DELAY, _refresh)


class BydActionEntity(BydVehicleEntity):
    """Base for action entities requiring a verified Control PIN."""

    @property
    def entity_registry_enabled_default(self) -> bool:
        """Gate default enabled state by whether a PIN is configured.

        Uses ``has_pin_configured`` (config-level check) so entities are
        *registered* when a PIN exists, even if verification has not yet
        succeeded.  The runtime gate (``has_operation_pin``) prevents
        commands from actually executing when verification failed.
        """
        enabled_default = getattr(self, "_attr_entity_registry_enabled_default", None)
        if enabled_default is None:
            description = getattr(self, "entity_description", None)
            enabled_default = getattr(
                description,
                "entity_registry_enabled_default",
                True,
            )
        return bool(enabled_default) and self.coordinator.has_pin_configured

    def _ensure_action_allowed(self) -> None:
        """Raise when actions are attempted without a verified Control PIN."""
        if not self.coordinator.has_operation_pin:
            raise HomeAssistantError(self._command_pin_error_message())
