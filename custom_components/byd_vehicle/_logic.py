"""Pure, dependency-free helpers for BYD Vehicle.

No Home Assistant / pyBYD imports — only stdlib — so these can be unit-tested
in isolation (see tests/test_logic.py). The coordinator delegates the SoC→
energy / efficiency math and the consumption-trend hysteresis here.
"""

from __future__ import annotations

# Battery capacity default (Sealion 7 Comfort nameplate). Kept here so the
# SoC→kWh conversion has a single home; the coordinator passes its own value.
DEFAULT_BATTERY_KWH: float = 82.5


def soc_to_kwh(
    soc_delta: float | None, pack_kwh: float = DEFAULT_BATTERY_KWH
) -> float | None:
    """Coarse energy (kWh) from a SoC delta (%) × pack nameplate.

    Sign is preserved: a negative delta (net regen/charge) yields negative
    kWh. Returns None when the delta isn't numeric.
    """
    if not isinstance(soc_delta, (int, float)):
        return None
    return round(soc_delta * pack_kwh / 100.0, 2)


def efficiency_per_100km(
    energy_kwh: float | None, distance_km: float | None
) -> float | None:
    """kWh/100km from energy + distance; None unless BOTH are positive."""
    if (
        isinstance(energy_kwh, (int, float))
        and energy_kwh > 0
        and isinstance(distance_km, (int, float))
        and distance_km > 0
    ):
        return round(energy_kwh / distance_km * 100.0, 1)
    return None


# Consumption-trend hysteresis bands: enter improving/worsening at 0.90/1.10,
# return to steady only past 0.95/1.05 — so the arrow doesn't flap.
_TREND_ENTER_LOW = 0.90
_TREND_ENTER_HIGH = 1.10
_TREND_EXIT_LOW = 0.95
_TREND_EXIT_HIGH = 1.05


def next_trend_state(prev: str | None, ratio: float) -> str:
    """Next consumption-trend state given the previous one and short/long ratio.

    Pure FSM with hysteresis. States: improving / steady / worsening.
    """
    if prev in (None, "steady"):
        if ratio < _TREND_ENTER_LOW:
            return "improving"
        if ratio > _TREND_ENTER_HIGH:
            return "worsening"
        return "steady"
    if prev == "improving" and ratio > _TREND_EXIT_LOW:
        return "steady"
    if prev == "worsening" and ratio < _TREND_EXIT_HIGH:
        return "steady"
    return prev
