"""Fixtures: a localhost fake cloud and a fake tunnel agent."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Callable
from unittest.mock import patch

from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.xeonr_cloud.const import (
    CONF_API_BASE_URL,
    CONF_INSTANCE_ID,
    CONF_INSTANCE_SECRET,
    CONF_SET_EXTERNAL_URL,
    CONF_TEAM,
    DOMAIN,
)

from .fake_cloud import INSTANCE_ID, INSTANCE_SECRET, TEAM_A, FakeCloud


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Load custom_components/ in every test."""
    return


@pytest.fixture
async def fake_cloud(socket_enabled) -> AsyncGenerator[FakeCloud]:
    cloud = FakeCloud()
    await cloud.start()
    yield cloud
    await cloud.stop()


@pytest.fixture
async def configured(hass: HomeAssistant, fake_cloud: FakeCloud) -> FakeCloud:
    """The integration's YAML settings pointed at the fake."""
    assert await async_setup_component(
        hass,
        DOMAIN,
        {
            DOMAIN: {
                "client_id": "test-client",
                "auth_issuer": fake_cloud.url,
                "api_base_url": fake_cloud.url,
            }
        },
    )
    return fake_cloud


@pytest.fixture(autouse=True)
def no_poll_wait():
    async def _no_wait(_seconds: float) -> None:
        await asyncio.sleep(0)

    with patch("custom_components.xeonr_cloud.auth._sleep", _no_wait):
        yield


class FakeAgent:
    """Stands in for burrow_agent.Agent behind the TunnelAgent adapter."""

    instances: list[FakeAgent] = []

    def __init__(self, hass, connection, on_state_change: Callable[[bool], None]):
        self.connection = connection
        self._on_state_change = on_state_change
        self.connected = False
        self.stopped = False
        self._stop = asyncio.Event()
        self.fail_with: Exception | None = None
        FakeAgent.instances.append(self)

    async def run(self) -> None:
        if self.fail_with:
            raise self.fail_with
        self.connected = True
        self._on_state_change(True)
        await self._stop.wait()
        self.connected = False
        self._on_state_change(False)

    async def drop(self) -> None:
        """Simulate the gateway dropping the session."""
        self._stop.set()

    async def stop(self) -> None:
        self.stopped = True
        self._stop.set()


@pytest.fixture
def fake_agent():
    FakeAgent.instances = []
    with patch(
        "custom_components.xeonr_cloud.create_burrow_agent",
        side_effect=lambda hass, conn, cb: FakeAgent(hass, conn, cb),
    ):
        yield FakeAgent


@pytest.fixture
def entry(hass: HomeAssistant, fake_cloud: FakeCloud) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Home",
        unique_id=INSTANCE_ID,
        data={
            CONF_INSTANCE_ID: INSTANCE_ID,
            CONF_INSTANCE_SECRET: INSTANCE_SECRET,
            CONF_TEAM: TEAM_A,
            CONF_API_BASE_URL: fake_cloud.url,
        },
        options={CONF_SET_EXTERNAL_URL: True},
    )
    entry.add_to_hass(hass)
    return entry


async def wait_for(
    hass: HomeAssistant, condition: Callable[[], bool], limit: float = 3
) -> None:
    """Wait for background work (interval jobs, the tunnel supervisor),
    which async_block_till_done does not wait for."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + limit
    while not condition():
        if loop.time() > deadline:
            raise AssertionError("condition not met in time")
        await hass.async_block_till_done()
        await asyncio.sleep(0.01)
