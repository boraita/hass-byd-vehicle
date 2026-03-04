"""Lock control for BYD Vehicle."""

from __future__ import annotations

from typing import Any

from homeassistant.components.lock import LockEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from pybyd.models.vehicle import Vehicle

from .const import DOMAIN
from .coordinator import BydDataUpdateCoordinator
from .entity import BydActionEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up BYD lock entities from a config entry."""
    data = hass.data[DOMAIN][entry.entry_id]
    coordinators: dict[str, BydDataUpdateCoordinator] = data["coordinators"]

    entities: list[LockEntity] = []

    for vin, coordinator in coordinators.items():
        if not (
            coordinator.capability_available("lock")
            or coordinator.capability_available("unlock")
        ):
            continue
        entities.append(BydLock(coordinator, vin, coordinator.vehicle))

    async_add_entities(entities)


class BydLock(BydActionEntity, LockEntity):
    """Representation of BYD lock control.

    Reads lock state from ``VehicleSnapshot.realtime.is_locked``.
    Commands go through ``car.lock.lock()`` / ``car.lock.unlock()``
    which handle projections and guard windows internally.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "lock"

    def __init__(
        self,
        coordinator: BydDataUpdateCoordinator,
        vin: str,
        vehicle: Vehicle,
    ) -> None:
        super().__init__(coordinator)
        self._vin = vin
        self._vehicle = vehicle
        self._attr_unique_id = f"{vin}_lock"

    @property
    def is_locked(self) -> bool | None:
        """Return True if all doors are locked."""
        realtime = self._get_realtime()
        if realtime is not None:
            return realtime.is_locked
        return None

    @property
    def assumed_state(self) -> bool:
        """Return True when lock state is assumed (no realtime data)."""
        realtime = self._get_realtime()
        return realtime is None or realtime.is_locked is None

    async def async_lock(self, **_: Any) -> None:
        """Lock the vehicle."""
        car = self.coordinator.car
        if car is None:
            return
        await self._execute_car_command(
            car.lock.lock(),
            command="lock",
        )

    async def async_unlock(self, **_: Any) -> None:
        """Unlock the vehicle."""
        car = self.coordinator.car
        if car is None:
            return
        await self._execute_car_command(
            car.lock.unlock(),
            command="unlock",
        )
