"""The tunnel: GetConnection, the Burrow agent, and MarkConnectionSeen."""

from __future__ import annotations

import asyncio
from datetime import datetime
import logging
from typing import TYPE_CHECKING

from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_time_interval

from .api import (
    REASON_RESERVATION_REMOVED,
    ConnectError,
    Connection,
    HacloudError,
    HacloudInstanceApi,
)
from .const import (
    CONF_EXTERNAL_URL_SET_BY_US,
    CONF_SET_EXTERNAL_URL,
    PRECONDITION_RECHECK,
    RECONNECT_MAX_SECONDS,
    RECONNECT_MIN_SECONDS,
    SEEN_INTERVAL,
    SIGNAL_CONNECTION_STATE,
)
from .repairs import (
    async_clear_connection_issues,
    async_raise_agent_unavailable,
    async_raise_precondition,
    async_raise_unlinked,
)
from .tunnel import AgentFactory, TunnelAgent, TunnelUnavailable

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry

_LOGGER = logging.getLogger(__name__)

# State values, also the binary sensor's "state" attribute.
STATE_STARTING = "starting"
STATE_CONNECTED = "connected"
STATE_DISCONNECTED = "disconnected"
STATE_REMOTE_ACCESS_DISABLED = "remote_access_disabled"
STATE_RESERVATION_REMOVED = "reservation_removed"
STATE_UNLINKED = "unlinked"
STATE_UNAVAILABLE = "agent_unavailable"


def precondition_state(err: ConnectError) -> str:
    """Map GetConnection's failed_precondition to a state.

    The server says which by an ErrorInfo reason; without one we fall back to
    the message text, and default to remote access being off (the more
    common, and self-healing, of the two).
    """
    reason = (err.reason or "").upper()
    if reason == REASON_RESERVATION_REMOVED or (
        not reason and "reservation" in err.message.lower()
    ):
        return STATE_RESERVATION_REMOVED
    return STATE_REMOTE_ACCESS_DISABLED


class ConnectionManager:
    """Keeps the tunnel up and reports on it."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        api: HacloudInstanceApi,
        agent_factory: AgentFactory,
        *,
        home_assistant_version: str,
        integration_version: str,
    ) -> None:
        self.hass = hass
        self.entry = entry
        self.api = api
        self._agent_factory = agent_factory
        self._ha_version = home_assistant_version
        self._integration_version = integration_version

        self.connection: Connection | None = None
        self.state = STATE_STARTING
        self.last_seen_at: datetime | None = None
        self._agent: TunnelAgent | None = None
        self._stopping = asyncio.Event()
        self._wake = asyncio.Event()
        self._seen_lock = asyncio.Lock()
        self._unsub_interval: CALLBACK_TYPE | None = None

    @property
    def connected(self) -> bool:
        return self.state == STATE_CONNECTED

    @property
    def public_url(self) -> str | None:
        return self.connection.public_url if self.connection else None

    async def async_start(
        self,
        connection: Connection | None,
        precondition: ConnectError | None = None,
    ) -> None:
        """Start from the GetConnection result setup already fetched."""
        if connection is not None:
            await self._async_connection_ready(connection)
        elif precondition is not None:
            self._set_state(precondition_state(precondition))
            async_raise_precondition(
                self.hass,
                self.entry,
                reservation_removed=self.state == STATE_RESERVATION_REMOVED,
            )

        await self.async_mark_seen()
        self._unsub_interval = async_track_time_interval(
            self.hass, self._async_interval_seen, SEEN_INTERVAL
        )
        self.entry.async_create_background_task(
            self.hass,
            self._async_supervise(),
            f"xeonr_cloud tunnel {self.entry.entry_id}",
        )

    async def async_stop(self) -> None:
        self._stopping.set()
        self._wake.set()
        if self._unsub_interval is not None:
            self._unsub_interval()
            self._unsub_interval = None
        if self._agent is not None:
            try:
                await self._agent.stop()
            except Exception:
                _LOGGER.debug("Agent stop failed", exc_info=True)
            self._agent = None

    async def _async_interval_seen(self, _now: datetime) -> None:
        await self.async_mark_seen()

    async def async_mark_seen(self) -> None:
        """Report versions and agent state. Failures are logged, not raised:
        the next interval tries again."""
        async with self._seen_lock:
            try:
                await self.api.mark_connection_seen(
                    home_assistant_version=self._ha_version,
                    integration_version=self._integration_version,
                    agent_connected=self.connected,
                )
            except ConnectError as err:
                if err.is_auth:
                    self.on_auth_error()
                    return
                _LOGGER.debug("MarkConnectionSeen failed: %s", err)
            except HacloudError as err:
                _LOGGER.debug("MarkConnectionSeen failed: %s", err)
            else:
                self.last_seen_at = datetime.now()

    async def _async_connection_ready(self, connection: Connection) -> None:
        self.connection = connection
        async_clear_connection_issues(self.hass, self.entry)
        await self.async_apply_external_url()
        if self.state in (
            STATE_STARTING,
            STATE_REMOTE_ACCESS_DISABLED,
            STATE_RESERVATION_REMOVED,
            STATE_UNAVAILABLE,
        ):
            self._set_state(STATE_DISCONNECTED)

    async def async_apply_external_url(self) -> None:
        """Offer the tunnel as external_url, never touching one the user set."""
        if self.connection is None or not self.connection.public_url:
            return
        url = self.connection.public_url.rstrip("/")
        ours = self.entry.data.get(CONF_EXTERNAL_URL_SET_BY_US)
        current = self.hass.config.external_url
        wanted = self.entry.options.get(CONF_SET_EXTERNAL_URL, True)

        if wanted and (current is None or (ours and current == ours)):
            if current != url:
                _LOGGER.info("Setting external_url to %s", url)
                await self.hass.config.async_update(external_url=url)
            if ours != url:
                self._store_ours(url)
        elif not wanted and ours and current == ours:
            _LOGGER.info("Clearing the external_url this integration set")
            await self.hass.config.async_update(external_url=None)
            self._store_ours(None)
        elif ours and current != ours:
            # Someone changed it since; it is theirs now.
            self._store_ours(None)

    def _store_ours(self, url: str | None) -> None:
        data = dict(self.entry.data)
        if url is None:
            data.pop(CONF_EXTERNAL_URL_SET_BY_US, None)
        else:
            data[CONF_EXTERNAL_URL_SET_BY_US] = url
        self.hass.config_entries.async_update_entry(self.entry, data=data)

    @callback
    def _set_state(self, state: str) -> None:
        if state == self.state:
            return
        previous_connected = self.connected
        self.state = state
        async_dispatcher_send(
            self.hass, SIGNAL_CONNECTION_STATE.format(self.entry.entry_id)
        )
        if previous_connected != self.connected and not self._stopping.is_set():
            self.entry.async_create_task(
                self.hass, self.async_mark_seen(), "xeonr_cloud mark seen"
            )

    def _on_agent_state(self, connected: bool) -> None:
        """Agent callback; may come from any thread."""
        self.hass.loop.call_soon_threadsafe(self._apply_agent_state, connected)

    @callback
    def _apply_agent_state(self, connected: bool) -> None:
        if self._stopping.is_set():
            return
        self._set_state(STATE_CONNECTED if connected else STATE_DISCONNECTED)

    @callback
    def on_auth_error(self) -> None:
        if self.state == STATE_UNLINKED:
            return
        self._set_state(STATE_UNLINKED)
        async_raise_unlinked(self.hass, self.entry)
        # Nothing works with a refused credential; stop until re-auth
        # reloads the entry.
        self._stopping.set()
        self._wake.set()
        if self._agent is not None:
            self.entry.async_create_task(
                self.hass, self._agent.stop(), "xeonr_cloud agent stop"
            )

    async def _sleep(self, seconds: float) -> None:
        self._wake.clear()
        try:
            await asyncio.wait_for(self._wake.wait(), seconds)
        except TimeoutError:
            pass

    async def _async_supervise(self) -> None:
        """Run the agent; on exit, ask GetConnection why and act on it."""
        backoff = RECONNECT_MIN_SECONDS
        while not self._stopping.is_set():
            if self.connection is None:
                if not await self._async_refresh_connection():
                    continue
                backoff = RECONNECT_MIN_SECONDS

            assert self.connection is not None
            try:
                self._agent = self._agent_factory(
                    self.hass, self.connection, self._on_agent_state
                )
            except TunnelUnavailable as err:
                _LOGGER.error("Remote access unavailable: %s", err)
                self._set_state(STATE_UNAVAILABLE)
                async_raise_agent_unavailable(self.hass, self.entry, str(err))
                return

            started = asyncio.get_running_loop().time()
            try:
                await self._agent.run()
            except asyncio.CancelledError:
                raise
            except Exception as err:
                _LOGGER.warning("Tunnel agent stopped: %s", err)
            finally:
                self._agent = None
            if self._stopping.is_set():
                return
            self._set_state(STATE_DISCONNECTED)

            # An agent that ran for a while earned a fresh backoff.
            if asyncio.get_running_loop().time() - started > RECONNECT_MAX_SECONDS:
                backoff = RECONNECT_MIN_SECONDS
            await self._sleep(backoff)
            backoff = min(backoff * 2, RECONNECT_MAX_SECONDS)
            # Re-read the connection: the credential, URL or switch may have
            # changed, and a refusal tells us which repair to raise.
            self.connection = None

    async def _async_refresh_connection(self) -> bool:
        """GetConnection. Returns True when the agent can be started."""
        try:
            connection = await self.api.get_connection()
        except ConnectError as err:
            if err.is_auth or err.code == "not_found":
                self.on_auth_error()
                return False
            if err.code == "failed_precondition":
                self._set_state(precondition_state(err))
                async_raise_precondition(
                    self.hass,
                    self.entry,
                    reservation_removed=self.state == STATE_RESERVATION_REMOVED,
                )
                await self._sleep(PRECONDITION_RECHECK.total_seconds())
                return False
            _LOGGER.warning("GetConnection failed: %s", err)
        except HacloudError as err:
            _LOGGER.warning("GetConnection failed: %s", err)
        else:
            await self._async_connection_ready(connection)
            return True
        await self._sleep(RECONNECT_MIN_SECONDS * 6)
        return False
