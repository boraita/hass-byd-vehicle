"""Tests for the configurable usable-battery-capacity feature."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from custom_components.byd_vehicle import _sanitize_battery_kwh
from custom_components.byd_vehicle.const import (
    DEFAULT_BATTERY_KWH,
    MAX_BATTERY_KWH,
    MIN_BATTERY_KWH,
)

from .conftest import make_coordinator


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (91.3, 91.3),
        (82.5, 82.5),
        (None, DEFAULT_BATTERY_KWH),
        ("not-a-number", DEFAULT_BATTERY_KWH),
        (0, DEFAULT_BATTERY_KWH),
        (-5, DEFAULT_BATTERY_KWH),
        (1.0, MIN_BATTERY_KWH),  # clamped up
        (9999.0, MAX_BATTERY_KWH),  # clamped down
        ("88.0", 88.0),  # numeric string coerces
    ],
)
def test_sanitize_battery_kwh(raw, expected):
    assert _sanitize_battery_kwh(raw) == expected


def test_battery_kwh_property_defaults(hass):
    coord = make_coordinator(hass)  # default fixture uses 100.0
    assert coord.battery_kwh == 100.0


def test_charge_session_kwh_added_uses_configured_capacity(hass):
    """SoC delta -> kWh must scale with the configured capacity."""
    coord = make_coordinator(hass, battery_kwh=100.0)
    coord._charge_session_start_soc = 50.0
    coord.data = SimpleNamespace(
        charging=SimpleNamespace(soc=70),
        realtime=None,
    )
    # 20% of 100 kWh = 20.0 kWh
    assert coord.charge_session_soc_added == 20.0
    assert coord.charge_session_kwh_added == 20.0


def test_charge_session_kwh_added_scales_with_smaller_battery(hass):
    coord = make_coordinator(hass, battery_kwh=82.5)
    coord._charge_session_start_soc = 50.0
    coord.data = SimpleNamespace(
        charging=SimpleNamespace(soc=70),
        realtime=None,
    )
    # 20% of 82.5 kWh = 16.5 kWh
    assert coord.charge_session_kwh_added == 16.5


def test_charge_session_kwh_added_none_without_baseline(hass):
    coord = make_coordinator(hass)
    coord._charge_session_start_soc = None
    coord.data = SimpleNamespace(charging=SimpleNamespace(soc=70), realtime=None)
    assert coord.charge_session_kwh_added is None


def test_time_until_full_minutes_uses_configured_capacity(hass):
    """Minutes-to-full must extrapolate against the configured capacity."""
    coord = make_coordinator(hass, battery_kwh=100.0)
    coord.data = SimpleNamespace(
        charging=SimpleNamespace(soc=50, charging_state=1),
        realtime=SimpleNamespace(gl=10000),  # 10 kW
    )
    # 50 kWh remaining at 10 kW -> 5 h -> 300 min
    assert coord.time_until_full_minutes == 300


def test_time_until_full_minutes_none_when_not_charging(hass):
    coord = make_coordinator(hass)
    coord.data = SimpleNamespace(
        charging=SimpleNamespace(soc=50, charging_state=0),
        realtime=SimpleNamespace(gl=10000),
    )
    assert coord.time_until_full_minutes is None
