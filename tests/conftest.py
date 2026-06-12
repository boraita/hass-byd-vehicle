"""Shared fixtures for BYD Vehicle tests."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from custom_components.byd_vehicle.coordinator import BydDataUpdateCoordinator

_TEST_VIN = "LC0TEST0000000001"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Enable loading of the custom integration in every test."""
    yield


def make_coordinator(hass, *, battery_kwh: float = 100.0) -> BydDataUpdateCoordinator:
    """Build a coordinator with a mocked API for unit-level tests.

    The API is a plain ``MagicMock``; constructing the coordinator does not
    call into it, so tests stub the specific method they exercise.
    """
    api = MagicMock()
    vehicle = SimpleNamespace(vin=_TEST_VIN, model_name="SEALION 7", brand_name="BYD")
    return BydDataUpdateCoordinator(
        hass,
        api,
        vehicle,
        _TEST_VIN,
        300,
        battery_kwh,
    )


@pytest.fixture
def coordinator(hass):
    """A coordinator wired with a 100 kWh battery for round-number maths."""
    return make_coordinator(hass, battery_kwh=100.0)
