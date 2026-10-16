"""A thin adapter over the vendored Burrow agent library.

Everything else in the integration talks to ``TunnelAgent``, never to
``burrow_agent`` directly, so the library's final API only has to be matched
here, and tests can substitute a fake.

The library is ``burrow-agent-py``, vendored under ``./burrow_agent`` by
``scripts/sync_burrow_agent.sh``. The API coded against (its suggested one,
until it reports a final one):

- ``Agent(agent_url, credential, name, handler)`` with ``run()``, ``stop()``,
  ``connected`` and ``on_state_change(cb)``;
- ``AiohttpStreamHandler(runner)`` from ``burrow_agent.aiohttp``, which feeds
  each tunnelled stream into Home Assistant's own aiohttp server in-process,
  with the preamble's ``remote`` as the peer address and the gateway's
  ``X-Forwarded-*`` stripped (DESIGN §3.2). We never proxy to localhost.
"""

from __future__ import annotations

from collections.abc import Callable
import inspect
import logging
from typing import Any, Protocol

from homeassistant.core import HomeAssistant

from .api import Connection

_LOGGER = logging.getLogger(__name__)


class TunnelUnavailable(Exception):
    """The agent library is not vendored, or the HTTP server is not up."""


class TunnelAgent(Protocol):
    """What the connection manager needs from an agent."""

    @property
    def connected(self) -> bool: ...

    async def run(self) -> None:
        """Run until stopped. Reconnects transient failures itself; returns or
        raises when the gateway refuses the agent outright."""

    async def stop(self) -> None: ...


AgentFactory = Callable[
    [HomeAssistant, Connection, Callable[[bool], None]], TunnelAgent
]


class _BurrowAgentAdapter:
    """Wraps burrow_agent.Agent into TunnelAgent."""

    def __init__(self, agent: Any) -> None:
        self._agent = agent

    @property
    def connected(self) -> bool:
        return bool(self._agent.connected)

    async def run(self) -> None:
        await self._agent.run()

    async def stop(self) -> None:
        result = self._agent.stop()
        if inspect.isawaitable(result):
            await result


def create_burrow_agent(
    hass: HomeAssistant,
    connection: Connection,
    on_state_change: Callable[[bool], None],
) -> TunnelAgent:
    """Build the real agent, feeding streams into hass.http.runner."""
    try:
        from .burrow_agent import Agent
        from .burrow_agent.aiohttp import AiohttpStreamHandler
    except ImportError as err:
        raise TunnelUnavailable(f"burrow_agent is not vendored: {err}") from err

    runner = getattr(getattr(hass, "http", None), "runner", None)
    if runner is None:
        raise TunnelUnavailable("Home Assistant's HTTP server is not running")

    agent = Agent(
        connection.agent_url,
        connection.agent_credential,
        connection.reservation_name,
        AiohttpStreamHandler(runner),
    )

    def _state_changed(*args: Any, **kwargs: Any) -> None:
        # The callback's arguments are not settled; read the state back off
        # the agent rather than trust a positional.
        on_state_change(bool(agent.connected))

    agent.on_state_change(_state_changed)
    return _BurrowAgentAdapter(agent)
