"""Select entities for BYD Vehicle seat climate control."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from homeassistant.components.select import SelectEntity, SelectEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from pybyd._capabilities.seat import SeatLevel, SeatPosition
from pybyd.models.realtime import SeatHeatVentState
from pybyd.models.vehicle import Vehicle

from .const import DOMAIN
from .coordinator import BydDataUpdateCoordinator
from .entity import BydActionEntity, BydVehicleEntity

_LOGGER = logging.getLogger(__name__)

# Derive options from the enum – single source of truth, no duplicate mappings.
SEAT_LEVEL_OPTIONS = [s.name.lower() for s in SeatHeatVentState if s.value > 0]

# Map UI option text → SeatLevel used by the pyBYD capability.
_OPTION_TO_LEVEL: dict[str, SeatLevel] = {
    "off": SeatLevel.OFF,
    "low": SeatLevel.LOW,
    "high": SeatLevel.HIGH,
}

# --- Charge-schedule repeat (charge_way) ---
# Wire format: "s" = single (one-shot), "e" = every day, comma-separated
# weekday indices (0=Mon) = those days. We expose the three common modes;
# the merge in the coordinator consumes the value under the "pattern" key.
CHARGE_REPEAT_OPTIONS = ["once", "daily", "weekdays"]
_OPTION_TO_CHARGE_WAY: dict[str, str] = {
    "once": "s",
    "daily": "e",
    "weekdays": "0,1,2,3,4",
}


def _charge_way_to_option(charge_way: Any) -> str | None:
    """Map a raw ``charge_way`` string to a repeat option label."""
    if charge_way is None:
        return None
    cw = str(charge_way).strip().lower()
    if cw == "s":
        return "once"
    if cw == "e":
        return "daily"
    if "," in cw or cw.isdigit():
        return "weekdays"  # any explicit weekday set
    return None


def _seat_status_to_option(value: Any) -> str | None:
    """Map a seat status value to a UI option label.

    Uses ``SeatHeatVentState`` member names directly so there is no
    separate int<->string mapping to keep in sync.

    Returns ``"off"`` when *value* is ``None`` or ``NO_DATA`` (0) because
    the entity exists but no data has been received yet (vehicle may be
    off). The safe assumption is the feature exists but is idle.
    """
    if value is None:
        return "off"
    if not isinstance(value, SeatHeatVentState):
        try:
            value = SeatHeatVentState(int(value))
        except (TypeError, ValueError):
            return "off"
    if value == SeatHeatVentState.NO_DATA:
        return "off"
    return value.name.lower() if value.value > 0 else "off"


@dataclass(frozen=True, kw_only=True)
class BydSeatClimateDescription(SelectEntityDescription):
    """Describe a BYD seat climate select entity."""

    seat_position: SeatPosition | None
    """pyBYD seat position, or None for unsupported rear seats."""
    mode: str
    """``'heat'`` or ``'vent'``."""
    hvac_attr: str
    """Attribute name on HVAC / realtime status for current state."""
    capability_key: str | None
    """Normalized pyBYD capability flag name."""


SEAT_CLIMATE_DESCRIPTIONS: tuple[BydSeatClimateDescription, ...] = (
    BydSeatClimateDescription(
        key="driver_seat_heat",
        icon="mdi:car-seat-heater",
        seat_position=SeatPosition.DRIVER,
        mode="heat",
        hvac_attr="main_seat_heat_state",
        capability_key="driver_seat_heat",
    ),
    BydSeatClimateDescription(
        key="driver_seat_ventilation",
        icon="mdi:car-seat-cooler",
        seat_position=SeatPosition.DRIVER,
        mode="vent",
        hvac_attr="main_seat_ventilation_state",
        capability_key="driver_seat_ventilation",
    ),
    BydSeatClimateDescription(
        key="passenger_seat_heat",
        icon="mdi:car-seat-heater",
        seat_position=SeatPosition.COPILOT,
        mode="heat",
        hvac_attr="copilot_seat_heat_state",
        capability_key="passenger_seat_heat",
    ),
    BydSeatClimateDescription(
        key="passenger_seat_ventilation",
        icon="mdi:car-seat-cooler",
        seat_position=SeatPosition.COPILOT,
        mode="vent",
        hvac_attr="copilot_seat_ventilation_state",
        capability_key="passenger_seat_ventilation",
    ),
    BydSeatClimateDescription(
        key="rear_left_seat_heat",
        icon="mdi:car-seat-heater",
        seat_position=None,
        mode="heat",
        hvac_attr="lr_seat_heat_state",
        capability_key=None,
        entity_registry_enabled_default=False,
    ),
    BydSeatClimateDescription(
        key="rear_left_seat_ventilation",
        icon="mdi:car-seat-cooler",
        seat_position=None,
        mode="vent",
        hvac_attr="lr_seat_ventilation_state",
        capability_key=None,
        entity_registry_enabled_default=False,
    ),
    BydSeatClimateDescription(
        key="rear_right_seat_heat",
        icon="mdi:car-seat-heater",
        seat_position=None,
        mode="heat",
        hvac_attr="rr_seat_heat_state",
        capability_key=None,
        entity_registry_enabled_default=False,
    ),
    BydSeatClimateDescription(
        key="rear_right_seat_ventilation",
        icon="mdi:car-seat-cooler",
        seat_position=None,
        mode="vent",
        hvac_attr="rr_seat_ventilation_state",
        capability_key=None,
        entity_registry_enabled_default=False,
    ),
    # NOTE: third-row seats (``lr_third_*``/``rr_third_*`` in realtime) only
    # exist on the 6/7-seater Sealion 7 trim, not on the EU 5-seater
    # Comfort.  Until we have a reliable capability signal to detect the
    # trim (``function_nos`` does not contain a clear "third row" code),
    # we omit these descriptors to avoid cluttering 5-seater installs.
    # 5-seater orphan entries are cleaned up via
    # ``_LEGACY_SELECT_UNIQUE_ID_REMOVALS`` below.
)

# Selects whose unique-id suffix is removed from the registry on setup —
# entries that the integration created in earlier versions but no longer
# manages.  Currently: the four third-row seat entries (only meaningful on
# 6/7-seater Sealion 7 trims, which we do not yet detect).
_LEGACY_SELECT_UNIQUE_ID_REMOVALS: frozenset[str] = frozenset(
    {
        "select_third_left_seat_heat",
        "select_third_left_seat_ventilation",
        "select_third_right_seat_heat",
        "select_third_right_seat_ventilation",
    }
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up BYD seat climate select entities from a config entry."""
    data = hass.data[DOMAIN][entry.entry_id]
    coordinators: dict[str, BydDataUpdateCoordinator] = data["coordinators"]

    registry = er.async_get(hass)
    for vin in coordinators:
        for suffix in _LEGACY_SELECT_UNIQUE_ID_REMOVALS:
            stale = registry.async_get_entity_id("select", DOMAIN, f"{vin}_{suffix}")
            if stale:
                registry.async_remove(stale)

    entities: list[SelectEntity] = []
    for vin, coordinator in coordinators.items():
        vehicle = coordinator.vehicle
        for description in SEAT_CLIMATE_DESCRIPTIONS:
            capability_key = description.capability_key
            # Capability-gated entities (driver/copilot front seats) only
            # get created when the vehicle actually advertises them.  Rear
            # seat entities (``capability_key=None``) are always created
            # but stay disabled-by-default — the read side surfaces the
            # state from ``lr_*``/``rr_*`` fields when the feature exists;
            # commands warn-and-no-op until pyBYD's SeatCapability adds
            # rear positions.
            if capability_key is not None and not coordinator.capability_available(
                capability_key
            ):
                continue
            entities.append(
                BydSeatClimateSelect(coordinator, vin, vehicle, description)
            )
        # Charge-schedule repeat selector (created unconditionally, like the
        # start/end time entities in time.py).
        entities.append(BydChargeRepeatSelect(coordinator, vin, vehicle))

    async_add_entities(entities)


class BydSeatClimateSelect(BydActionEntity, SelectEntity):
    """Select entity for a single seat heating/ventilation level.

    For driver and copilot seats, commands are dispatched through
    ``car.seat.heat()`` / ``car.seat.ventilation()`` which handle
    ``SeatClimateParams`` assembly and projections internally.

    Rear-seat entities are read-only (disabled by default) because the
    pyBYD capability does not yet map rear seat positions.
    """

    _attr_has_entity_name = True
    _attr_options = SEAT_LEVEL_OPTIONS

    entity_description: BydSeatClimateDescription

    def __init__(
        self,
        coordinator: BydDataUpdateCoordinator,
        vin: str,
        vehicle: Vehicle,
        description: BydSeatClimateDescription,
    ) -> None:
        """Initialize the select entity."""
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_translation_key = description.key
        self._vin = vin
        self._vehicle = vehicle
        self._attr_unique_id = f"{vin}_select_{description.key}"

    @property
    def current_option(self) -> str | None:
        """Return the currently selected option."""
        hvac = self._get_hvac_status()
        realtime = self._get_realtime()
        val = None
        if hvac is not None:
            val = getattr(hvac, self.entity_description.hvac_attr, None)
        if val is None and realtime is not None:
            val = getattr(realtime, self.entity_description.hvac_attr, None)
        option = _seat_status_to_option(val)
        # Fallback: entity was created so the feature exists — default to 'off'.
        return option if option is not None else "off"

    async def async_select_option(self, option: str) -> None:
        """Set the seat climate level via pyBYD capability."""
        self._ensure_action_allowed()
        desc = self.entity_description

        # Rear seats not yet supported by pyBYD SeatCapability.
        if desc.seat_position is None:
            _LOGGER.warning(
                "Rear seat commands are not yet supported by pyBYD; %s ignored",
                desc.key,
            )
            return

        level = _OPTION_TO_LEVEL.get(option)
        if level is None:
            return

        car = self.coordinator.car
        if car is None:
            return

        if desc.mode == "heat":
            await self._execute_car_command(
                car.seat.heat(desc.seat_position, level),
                command=f"seat_climate_{desc.key}",
            )
        else:
            await self._execute_car_command(
                car.seat.ventilation(desc.seat_position, level),
                command=f"seat_climate_{desc.key}",
            )


class BydChargeRepeatSelect(BydVehicleEntity, SelectEntity):
    """Editable repeat mode for the smart-charging schedule (charge_way).

    Reads the current repeat from ``charging_schedule.charge.charge_way`` and
    writes a new one through the coordinator's debounced schedule save (under
    the ``"pattern"`` key, which the merge maps to ``charge_way``). Companion
    to the start/end ``time`` entities — created unconditionally.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "scheduled_charge_repeat"
    _attr_icon = "mdi:calendar-refresh"
    _attr_options = CHARGE_REPEAT_OPTIONS

    def __init__(
        self,
        coordinator: BydDataUpdateCoordinator,
        vin: str,
        vehicle: Vehicle,
    ) -> None:
        super().__init__(coordinator)
        self._vin = vin
        self._vehicle = vehicle
        self._attr_unique_id = f"{vin}_select_scheduled_charge_repeat"
        self._optimistic: str | None = None

    def _charge(self) -> Any:
        if self.coordinator.data and self.coordinator.data.charging_schedule:
            return self.coordinator.data.charging_schedule.charge
        return None

    @property
    def current_option(self) -> str | None:
        """Return the current repeat option derived from charge_way."""
        if self._optimistic is not None:
            return self._optimistic
        charge = self._charge()
        if charge is None:
            return None
        return _charge_way_to_option(getattr(charge, "charge_way", None))

    @callback
    def _handle_coordinator_update(self) -> None:
        self._optimistic = None
        super()._handle_coordinator_update()

    async def async_select_option(self, option: str) -> None:
        """Set the charge repeat mode via the debounced schedule save."""
        charge_way = _OPTION_TO_CHARGE_WAY.get(option)
        if charge_way is None:
            return
        self._optimistic = option
        self.async_write_ha_state()
        await self.coordinator.async_request_schedule_update("pattern", charge_way)
