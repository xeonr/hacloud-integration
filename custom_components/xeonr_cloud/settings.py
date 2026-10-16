"""Endpoints and client id, overridable from configuration.yaml.

```yaml
xeonr_cloud:
  client_id: ...        # the device-flow OAuth client (hacloud-infra)
  auth_issuer: https://auth.xeonr.dev
  api_base_url: https://hacloud.xeonr.dev
```

Everything is optional; the defaults are production. The API base URL a
config entry was created against is stored on the entry, so changing it here
only affects new sign-ins.
"""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.const import __version__ as HA_VERSION
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import config_validation as cv
from homeassistant.util.hass_dict import HassKey
import voluptuous as vol

from .const import (
    DEFAULT_API_BASE_URL,
    DEFAULT_AUTH_ISSUER,
    DEFAULT_CLIENT_ID,
    DOMAIN,
)

CONF_CLIENT_ID = "client_id"
CONF_AUTH_ISSUER = "auth_issuer"
CONF_API_BASE_URL = "api_base_url"

CONFIG_SCHEMA = vol.Schema(
    {
        DOMAIN: vol.Schema(
            {
                vol.Optional(CONF_CLIENT_ID): cv.string,
                vol.Optional(CONF_AUTH_ISSUER): cv.url,
                vol.Optional(CONF_API_BASE_URL): cv.url,
            }
        )
    },
    extra=vol.ALLOW_EXTRA,
)


@dataclass(frozen=True)
class Settings:
    client_id: str = DEFAULT_CLIENT_ID
    auth_issuer: str = DEFAULT_AUTH_ISSUER
    api_base_url: str = DEFAULT_API_BASE_URL


DATA_SETTINGS: HassKey[Settings] = HassKey(f"{DOMAIN}_settings")


@callback
def async_store_settings(hass: HomeAssistant, conf: dict | None) -> None:
    conf = conf or {}
    hass.data[DATA_SETTINGS] = Settings(
        client_id=conf.get(CONF_CLIENT_ID, DEFAULT_CLIENT_ID),
        auth_issuer=conf.get(CONF_AUTH_ISSUER, DEFAULT_AUTH_ISSUER).rstrip("/"),
        api_base_url=conf.get(CONF_API_BASE_URL, DEFAULT_API_BASE_URL).rstrip("/"),
    )


@callback
def async_get_settings(hass: HomeAssistant) -> Settings:
    return hass.data.get(DATA_SETTINGS) or Settings()


def user_agent(hass: HomeAssistant) -> str:
    return f"xeonr_cloud HomeAssistant/{HA_VERSION}"
