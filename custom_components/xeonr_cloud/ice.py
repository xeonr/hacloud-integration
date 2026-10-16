"""ICE servers: TURN credentials from the instance API, registered with web_rtc."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
import logging

from homeassistant.components.web_rtc import async_register_ice_servers
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.event import async_track_point_in_utc_time
from homeassistant.util import dt as dt_util
from webrtc_models import RTCIceServer

from .api import ConnectError, HacloudError, HacloudInstanceApi
from .const import ICE_DEFAULT_REFRESH, ICE_REFRESH_MARGIN, ICE_RETRY

_LOGGER = logging.getLogger(__name__)


class IceServerManager:
    """Fetches, registers and refreshes this instance's ICE servers."""

    def __init__(
        self,
        hass: HomeAssistant,
        api: HacloudInstanceApi,
        on_auth_error: Callable[[], None],
    ) -> None:
        self.hass = hass
        self.api = api
        self._on_auth_error = on_auth_error
        self._servers: list[RTCIceServer] = []
        self.expires_at: datetime | None = None
        self._unregister: CALLBACK_TYPE | None = None
        self._unsub_refresh: CALLBACK_TYPE | None = None
        self._stopped = False

    def _get_servers(self) -> list[RTCIceServer]:
        """What web_rtc asks for on every offer. Expired TURN credentials
        are useless, so they are withheld rather than served stale."""
        if self.expires_at is not None and dt_util.utcnow() >= self.expires_at:
            return []
        return self._servers

    async def async_start(self) -> None:
        self._unregister = async_register_ice_servers(self.hass, self._get_servers)
        await self._async_refresh()

    @callback
    def async_stop(self) -> None:
        self._stopped = True
        if self._unsub_refresh is not None:
            self._unsub_refresh()
            self._unsub_refresh = None
        if self._unregister is not None:
            self._unregister()
            self._unregister = None

    async def _async_refresh(self, _now: datetime | None = None) -> None:
        self._unsub_refresh = None
        if self._stopped:
            return
        try:
            result = await self.api.list_ice_servers()
        except ConnectError as err:
            if err.is_unlinked:
                self._on_auth_error()
                return
            _LOGGER.warning("ListIceServers failed: %s", err)
            self._schedule(dt_util.utcnow() + ICE_RETRY)
            return
        except HacloudError as err:
            _LOGGER.warning("ListIceServers failed: %s", err)
            self._schedule(dt_util.utcnow() + ICE_RETRY)
            return

        self._servers = [
            RTCIceServer(
                urls=s.urls,
                username=s.username or None,
                credential=s.credential or None,
            )
            for s in result.servers
            if s.urls
        ]
        self.expires_at = result.expires_at
        self._schedule(self._next_refresh(dt_util.utcnow()))

    def _next_refresh(self, now: datetime) -> datetime:
        if self.expires_at is None:
            return now + ICE_DEFAULT_REFRESH
        lifetime = self.expires_at - now
        margin = min(ICE_REFRESH_MARGIN, lifetime / 2)
        return max(self.expires_at - margin, now + timedelta(seconds=30))

    @callback
    def _schedule(self, when: datetime) -> None:
        if self._stopped:
            return
        self._unsub_refresh = async_track_point_in_utc_time(
            self.hass, self._async_refresh, when
        )
