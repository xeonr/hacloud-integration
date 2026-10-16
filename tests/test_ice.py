"""ICE servers: registered with web_rtc, refreshed before expiry, unregistered."""

from __future__ import annotations

from datetime import timedelta

from homeassistant.components.web_rtc import async_get_ice_servers
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from .conftest import wait_for
from .fake_cloud import INSTANCE_ID, FakeCloud


def _turn(hass: HomeAssistant):
    return [
        s
        for s in async_get_ice_servers(hass)
        if any(u.startswith("turn") for u in s.urls)
    ]


async def test_registered_refreshed_and_unregistered(
    hass: HomeAssistant, fake_cloud: FakeCloud, entry, fake_agent
) -> None:
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    servers = async_get_ice_servers(hass)
    assert ["stun:turn.prod.wtf:3478"] in [s.urls for s in servers]
    [turn] = _turn(hass)
    assert turn.urls == [
        "turn:turn.prod.wtf:3478?transport=udp",
        "turns:turn.prod.wtf:5349?transport=tcp",
    ]
    assert turn.username.endswith(f":{INSTANCE_ID}")
    assert turn.credential == "cred-1"
    # Home Assistant's default Google STUN is replaced, not added to.
    assert not any("google" in u for s in servers for u in s.urls)

    # Ten minutes before the hour-long credential expires, it is refreshed.
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=49))
    await hass.async_block_till_done()
    assert fake_cloud.ice_calls == 1
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=50, seconds=5))
    await wait_for(hass, lambda: fake_cloud.ice_calls == 2)
    [turn] = _turn(hass)
    assert turn.credential == "cred-2"

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert _turn(hass) == []


async def test_short_lived_credentials_refresh_at_half_life(
    hass: HomeAssistant, fake_cloud: FakeCloud, entry, fake_agent
) -> None:
    fake_cloud.ice_ttl = timedelta(minutes=4)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=2, seconds=1))
    await wait_for(hass, lambda: fake_cloud.ice_calls == 2)


async def test_failure_retries_and_expired_credentials_are_withheld(
    hass: HomeAssistant, fake_cloud: FakeCloud, entry, fake_agent
) -> None:
    fake_cloud.ice_ttl = timedelta(minutes=2)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert len(_turn(hass)) == 1

    fake_cloud.ice_error = ("unavailable", "coturn secret missing")
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=1, seconds=1))
    await hass.async_block_till_done()
    assert fake_cloud.ice_calls == 1

    # Past expiry with no fresh credential: nothing stale is handed out.
    with_expired = dt_util.utcnow() + timedelta(minutes=3)
    from unittest.mock import patch

    with patch(
        "custom_components.xeonr_cloud.ice.dt_util.utcnow", return_value=with_expired
    ):
        assert _turn(hass) == []

    fake_cloud.ice_error = None
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=2, seconds=5))
    await wait_for(hass, lambda: fake_cloud.ice_calls == 2)
    assert len(_turn(hass)) == 1
