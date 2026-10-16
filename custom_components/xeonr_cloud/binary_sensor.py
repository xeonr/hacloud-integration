"""The remote access connected sensor."""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import XeonrCloudConfigEntry
from .const import CONSOLE_URL, DOMAIN, SIGNAL_CONNECTION_STATE


async def async_setup_entry(
    hass: HomeAssistant,
    entry: XeonrCloudConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([RemoteAccessConnectedSensor(entry)])


class RemoteAccessConnectedSensor(BinarySensorEntity):
    """On while the Burrow tunnel is connected."""

    _attr_has_entity_name = True
    _attr_translation_key = "remote_access"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_should_poll = False

    def __init__(self, entry: XeonrCloudConfigEntry) -> None:
        self._entry = entry
        self._manager = entry.runtime_data.connection
        key = entry.unique_id or entry.entry_id
        self._attr_unique_id = f"{key}_remote_access"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, key)},
            name=entry.title,
            manufacturer="Xeonr",
            model="Home Assistant cloud",
            entry_type=DeviceEntryType.SERVICE,
            configuration_url=CONSOLE_URL,
        )

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                SIGNAL_CONNECTION_STATE.format(self._entry.entry_id),
                self._handle_update,
            )
        )

    @callback
    def _handle_update(self) -> None:
        self.async_write_ha_state()

    @property
    def is_on(self) -> bool:
        return self._manager.connected

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"url": self._manager.public_url, "state": self._manager.state}
