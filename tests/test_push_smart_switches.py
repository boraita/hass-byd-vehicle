"""Tests for the push-notification and smart-charging coordinator methods.

These exercise the coordinator layer that backs ``BydPushNotificationSwitch``
and ``BydSmartChargingSwitch`` — that the right client method is invoked with
the right ``enable`` flag, and that push state is cached / fails soft.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from homeassistant.helpers.update_coordinator import UpdateFailed

from .conftest import make_coordinator


def _wire_api(coord):
    """Make ``api.async_call(handler, ...)`` run the handler against a fake client.

    Returns the fake client so tests can assert which method was called.
    """
    client = SimpleNamespace(
        get_push_state=AsyncMock(return_value=SimpleNamespace(is_enabled=True)),
        set_push_state=AsyncMock(return_value=None),
        toggle_smart_charging=AsyncMock(return_value=None),
    )

    async def _run(handler, *, vin=None, command=None):
        return await handler(client)

    coord._api.async_call = AsyncMock(side_effect=_run)
    return client


async def test_refresh_push_state_caches_enabled(hass):
    coord = make_coordinator(hass)
    client = _wire_api(coord)

    result = await coord.async_refresh_push_state()

    assert result is True
    assert coord.push_enabled is True
    client.get_push_state.assert_awaited_once()


async def test_refresh_push_state_failsoft_keeps_previous(hass):
    coord = make_coordinator(hass)
    coord._push_enabled = True  # previously known-good
    coord._api.async_call = AsyncMock(side_effect=UpdateFailed("boom"))

    result = await coord.async_refresh_push_state()

    # A transient failure must not flip the cached state to unknown.
    assert result is True
    assert coord.push_enabled is True


async def test_set_push_enabled_calls_client_and_caches(hass):
    coord = make_coordinator(hass)
    client = _wire_api(coord)

    await coord.async_set_push_enabled(False)

    client.set_push_state.assert_awaited_once()
    assert client.set_push_state.await_args.kwargs["enable"] is False
    assert coord.push_enabled is False


async def test_set_smart_charging_calls_client(hass):
    coord = make_coordinator(hass)
    client = _wire_api(coord)

    await coord.async_set_smart_charging(True)

    client.toggle_smart_charging.assert_awaited_once()
    assert client.toggle_smart_charging.await_args.kwargs["enable"] is True


async def test_set_push_enabled_propagates_failure(hass):
    coord = make_coordinator(hass)
    coord._api.async_call = AsyncMock(side_effect=UpdateFailed("nope"))

    with pytest.raises(UpdateFailed):
        await coord.async_set_push_enabled(True)
