"""Device triggers for BYD Vehicle.

Exposes the push-driven events as UI-selectable automation triggers so
users can pick "Vehicle turned on/off" in the automation editor instead
of hand-writing an event trigger.  Backed by the ``byd_vehicle_power_changed``
event bus event fired from the MQTT push path (no polling involved).
"""

from __future__ import annotations

import voluptuous as vol
from homeassistant.components.device_automation import DEVICE_TRIGGER_BASE_SCHEMA
from homeassistant.components.homeassistant.triggers import event as event_trigger
from homeassistant.const import (
    CONF_DEVICE_ID,
    CONF_DOMAIN,
    CONF_PLATFORM,
    CONF_TYPE,
)
from homeassistant.core import CALLBACK_TYPE, HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.trigger import TriggerActionType, TriggerInfo
from homeassistant.helpers.typing import ConfigType

from .const import DOMAIN

TRIGGER_TURNED_ON = "turned_on"
TRIGGER_TURNED_OFF = "turned_off"
TRIGGER_TYPES: set[str] = {TRIGGER_TURNED_ON, TRIGGER_TURNED_OFF}

# Mirror of coordinator._HA_EVENT_POWER_CHANGED.
_EVENT_POWER_CHANGED = f"{DOMAIN}_power_changed"

TRIGGER_SCHEMA = DEVICE_TRIGGER_BASE_SCHEMA.extend(
    {vol.Required(CONF_TYPE): vol.In(TRIGGER_TYPES)}
)


def _vin_for_device(hass: HomeAssistant, device_id: str) -> str | None:
    """Return the BYD VIN backing a device, or None if not a BYD device."""
    device = dr.async_get(hass).async_get(device_id)
    if device is None:
        return None
    for domain, identifier in device.identifiers:
        if domain == DOMAIN:
            return identifier
    return None


async def async_get_triggers(
    hass: HomeAssistant, device_id: str
) -> list[dict[str, str]]:
    """List device triggers for a BYD Vehicle device."""
    if _vin_for_device(hass, device_id) is None:
        return []
    base = {
        CONF_PLATFORM: "device",
        CONF_DOMAIN: DOMAIN,
        CONF_DEVICE_ID: device_id,
    }
    return [{**base, CONF_TYPE: trigger_type} for trigger_type in TRIGGER_TYPES]


async def async_attach_trigger(
    hass: HomeAssistant,
    config: ConfigType,
    action: TriggerActionType,
    trigger_info: TriggerInfo,
) -> CALLBACK_TYPE:
    """Attach a trigger: fire when the vehicle turns on/off."""
    vin = _vin_for_device(hass, config[CONF_DEVICE_ID])
    want_on = config[CONF_TYPE] == TRIGGER_TURNED_ON
    event_config = event_trigger.TRIGGER_SCHEMA(
        {
            event_trigger.CONF_PLATFORM: "event",
            event_trigger.CONF_EVENT_TYPE: _EVENT_POWER_CHANGED,
            event_trigger.CONF_EVENT_DATA: {"vin": vin, "is_on": want_on},
        }
    )
    return await event_trigger.async_attach_trigger(
        hass, event_config, action, trigger_info, platform_type="device"
    )
