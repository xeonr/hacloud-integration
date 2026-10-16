"""Xeonr Home Assistant cloud: remote access, ICE servers and backups."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform, __version__ as HA_VERSION
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.typing import ConfigType
from homeassistant.loader import async_get_integration

from .api import ConnectError, HacloudError, HacloudInstanceApi
from .backup import async_notify_backup_listeners
from .connection import ConnectionManager
from .const import CONF_API_BASE_URL, CONF_INSTANCE_SECRET, DOMAIN
from .ice import IceServerManager
from .repairs import async_clear_unlinked, async_raise_unlinked
from .settings import (
    CONFIG_SCHEMA,  # noqa: F401
    async_get_settings,
    async_store_settings,
    user_agent,
)
from .tunnel import create_burrow_agent

PLATFORMS: list[Platform] = [Platform.BINARY_SENSOR]


@dataclass
class XeonrCloudData:
    api: HacloudInstanceApi
    connection: ConnectionManager
    ice: IceServerManager
    on_auth_error: Callable[[], None]


type XeonrCloudConfigEntry = ConfigEntry[XeonrCloudData]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    async_store_settings(hass, config.get(DOMAIN))
    return True


async def async_setup_entry(hass: HomeAssistant, entry: XeonrCloudConfigEntry) -> bool:
    api = HacloudInstanceApi(
        async_get_clientsession(hass),
        entry.data.get(CONF_API_BASE_URL) or async_get_settings(hass).api_base_url,
        entry.data[CONF_INSTANCE_SECRET],
        user_agent(hass),
    )

    connection = None
    precondition: ConnectError | None = None
    try:
        connection = await api.get_connection()
    except ConnectError as err:
        if err.is_unlinked:
            async_raise_unlinked(hass, entry, start_reauth=False)
            raise ConfigEntryAuthFailed(str(err)) from err
        if err.is_wrong_credential:
            # The stored secret is not an instance key; re-auth regenerates one.
            raise ConfigEntryAuthFailed(str(err)) from err
        if err.code != "failed_precondition":
            raise ConfigEntryNotReady(str(err)) from err
        # Remote access off or reservation removed: the tunnel waits, the
        # rest (backups, ICE) still works.
        precondition = err
    except HacloudError as err:
        raise ConfigEntryNotReady(str(err)) from err
    async_clear_unlinked(hass, entry)

    integration = await async_get_integration(hass, DOMAIN)
    manager = ConnectionManager(
        hass,
        entry,
        api,
        create_burrow_agent,
        home_assistant_version=HA_VERSION,
        integration_version=str(integration.version or ""),
    )
    ice = IceServerManager(hass, api, manager.on_auth_error)
    entry.runtime_data = XeonrCloudData(
        api=api, connection=manager, ice=ice, on_auth_error=manager.on_auth_error
    )

    await manager.async_start(connection, precondition)
    entry.async_on_unload(manager.async_stop)
    await ice.async_start()
    entry.async_on_unload(ice.async_stop)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    entry.async_on_unload(entry.add_update_listener(_async_options_updated))

    @callback
    def _notify_backup_listeners() -> None:
        async_notify_backup_listeners(hass)

    entry.async_on_unload(entry.async_on_state_change(_notify_backup_listeners))
    return True


async def _async_options_updated(
    hass: HomeAssistant, entry: XeonrCloudConfigEntry
) -> None:
    await entry.runtime_data.connection.async_apply_external_url()


async def async_unload_entry(hass: HomeAssistant, entry: XeonrCloudConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
