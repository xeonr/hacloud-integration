"""The adapter over burrow_agent's suggested API."""

from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock, patch

from homeassistant.core import HomeAssistant
import pytest

from custom_components.xeonr_cloud.api import Connection
from custom_components.xeonr_cloud.tunnel import TunnelUnavailable, create_burrow_agent

CONNECTION = Connection(
    instance_id="i",
    agent_url="wss://connect.brw.xnr.app/v1/agent",
    agent_credential="cred",
    reservation_name="quiet-otter",
    public_url="https://quiet-otter.brw.xnr.app",
)


class StubAgent:
    def __init__(self, agent_url, credential, name, handler):
        self.args = (agent_url, credential, name)
        self.handler = handler
        self.connected = False
        self.callbacks = []
        self.ran = self.stopped = False

    def on_state_change(self, cb):
        self.callbacks.append(cb)

    async def run(self):
        self.ran = True

    def stop(self):  # sync, to check the adapter copes with either
        self.stopped = True


class StubHandler:
    def __init__(self, runner):
        self.runner = runner


@pytest.fixture
def stub_library():
    pkg = types.ModuleType("custom_components.xeonr_cloud.burrow_agent")
    pkg.Agent = StubAgent
    sub = types.ModuleType("custom_components.xeonr_cloud.burrow_agent.aiohttp")
    sub.AiohttpStreamHandler = StubHandler
    with patch.dict(
        sys.modules,
        {
            "custom_components.xeonr_cloud.burrow_agent": pkg,
            "custom_components.xeonr_cloud.burrow_agent.aiohttp": sub,
        },
    ):
        yield


async def test_streams_go_to_homeassistants_own_runner(
    hass: HomeAssistant, stub_library
) -> None:
    runner = object()
    hass.http = MagicMock(runner=runner)
    states: list[bool] = []
    agent = create_burrow_agent(hass, CONNECTION, states.append)

    inner = agent._agent
    assert inner.args == ("wss://connect.brw.xnr.app/v1/agent", "cred", "quiet-otter")
    # In-process into HA's aiohttp server, never a localhost proxy.
    assert inner.handler.runner is runner

    inner.connected = True
    inner.callbacks[0]("whatever", "the", "library", passes="here")
    assert states == [True]
    assert agent.connected

    await agent.run()
    await agent.stop()
    assert inner.ran and inner.stopped


async def test_not_vendored(hass: HomeAssistant) -> None:
    with pytest.raises(TunnelUnavailable, match="not vendored"):
        create_burrow_agent(hass, CONNECTION, lambda c: None)


async def test_http_not_running(hass: HomeAssistant, stub_library) -> None:
    hass.http = None
    with pytest.raises(TunnelUnavailable, match="HTTP server"):
        create_burrow_agent(hass, CONNECTION, lambda c: None)
