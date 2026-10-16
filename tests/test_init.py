"""Setup, unload, the tunnel, MarkConnectionSeen and repairs."""

from __future__ import annotations

from datetime import timedelta
import sys
from unittest.mock import patch

from homeassistant.components.repairs import DOMAIN as REPAIRS_DOMAIN
from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import async_fire_time_changed
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator

from custom_components.xeonr_cloud.const import (
    CONF_EXTERNAL_URL_SET_BY_US,
    DOMAIN,
    PRECONDITION_RECHECK,
    SEEN_INTERVAL,
)

from .conftest import MANIFEST_VERSION, wait_for
from .fake_cloud import FakeCloud, error_info

SENSOR = "binary_sensor.home_remote_access"
URL = "https://quiet-otter.brw.xnr.app"


def _issue(hass: HomeAssistant, kind: str, entry) -> ir.IssueEntry | None:
    return ir.async_get(hass).async_get_issue(DOMAIN, f"{kind}_{entry.entry_id}")


async def test_setup_connects_and_unload_stops(
    hass: HomeAssistant, fake_cloud: FakeCloud, entry, fake_agent
) -> None:
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED

    [agent] = fake_agent.instances
    assert agent.connection.agent_url == "wss://connect.brw.xnr.app/v1/agent"
    assert agent.connection.agent_credential == "burrow_agent_cred"
    assert agent.connection.reservation_name == "quiet-otter"

    state = hass.states.get(SENSOR)
    assert state.state == "on"
    assert state.attributes["url"] == URL
    assert state.attributes["device_class"] == "connectivity"

    # Seen on start (not yet connected) and on connect.
    assert [s["agentConnected"] for s in fake_cloud.seen][:1] == [False]
    assert fake_cloud.seen[-1]["agentConnected"] is True
    assert fake_cloud.seen[-1]["integrationVersion"] == MANIFEST_VERSION

    assert hass.config.external_url == URL
    assert entry.data[CONF_EXTERNAL_URL_SET_BY_US] == URL

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED
    assert agent.stopped
    # The URL we set stays: unloading is not unlinking.
    assert hass.config.external_url == URL


async def test_seen_every_interval_and_on_disconnect(
    hass: HomeAssistant,
    fake_cloud: FakeCloud,
    entry,
    fake_agent,
) -> None:
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    count = len(fake_cloud.seen)

    async_fire_time_changed(hass, dt_util.utcnow() + SEEN_INTERVAL)
    await wait_for(hass, lambda: len(fake_cloud.seen) == count + 1)
    assert SEEN_INTERVAL <= timedelta(minutes=5)

    # The gateway drops the session: off, reported, and reconnected.
    [agent] = fake_agent.instances
    await agent.drop()
    await wait_for(hass, lambda: fake_cloud.seen[-1]["agentConnected"] is False)
    assert hass.states.get(SENSOR).state == "off"

    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=10))
    await wait_for(hass, lambda: len(fake_agent.instances) == 2)
    assert hass.states.get(SENSOR).state == "on"
    await wait_for(hass, lambda: fake_cloud.seen[-1]["agentConnected"] is True)


async def test_user_external_url_is_never_overwritten(
    hass: HomeAssistant, fake_cloud: FakeCloud, entry, fake_agent
) -> None:
    await hass.config.async_update(external_url="https://ha.example.com")
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.config.external_url == "https://ha.example.com"
    assert CONF_EXTERNAL_URL_SET_BY_US not in entry.data


async def test_remote_access_disabled_is_a_repair_that_heals(
    hass: HomeAssistant,
    fake_cloud: FakeCloud,
    entry,
    fake_agent,
) -> None:
    fake_cloud.connection_error = (
        "failed_precondition",
        "remote access is switched off",
        [error_info("REMOTE_ACCESS_DISABLED")],
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    # Setup still succeeds: backups and ICE do not need the tunnel.
    assert entry.state is ConfigEntryState.LOADED
    assert fake_agent.instances == []

    issue = _issue(hass, "remote_access_disabled", entry)
    assert issue is not None and not issue.is_fixable
    state = hass.states.get(SENSOR)
    assert state.state == "off"
    assert state.attributes["state"] == "remote_access_disabled"
    assert hass.config.external_url is None

    # Turned back on in the console.
    fake_cloud.connection_error = None
    async_fire_time_changed(hass, dt_util.utcnow() + PRECONDITION_RECHECK)
    await wait_for(hass, lambda: hass.states.get(SENSOR).state == "on")
    assert _issue(hass, "remote_access_disabled", entry) is None
    assert len(fake_agent.instances) == 1
    assert hass.states.get(SENSOR).state == "on"


async def test_precondition_without_detail_reads_message(
    hass: HomeAssistant, fake_cloud: FakeCloud, entry, fake_agent
) -> None:
    fake_cloud.connection_error = (
        "failed_precondition",
        "the reservation was removed in Burrow",
        [],
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert _issue(hass, "reservation_removed", entry) is not None


async def test_reservation_removed_fix_starts_relink(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    fake_cloud: FakeCloud,
    entry,
    fake_agent,
) -> None:
    fake_cloud.connection_error = (
        "failed_precondition",
        "gone",
        [error_info("RESERVATION_REMOVED")],
    )
    assert await async_setup_component(hass, REPAIRS_DOMAIN, {})
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    issue = _issue(hass, "reservation_removed", entry)
    assert issue is not None and issue.is_fixable
    assert hass.states.get(SENSOR).attributes["state"] == "reservation_removed"

    client = await hass_client()
    resp = await client.post(
        "/api/repairs/issues/fix",
        json={"handler": DOMAIN, "issue_id": issue.issue_id},
    )
    assert resp.status == 200
    flow = await resp.json()
    assert flow["step_id"] == "confirm"
    resp = await client.post(f"/api/repairs/issues/fix/{flow['flow_id']}", json={})
    assert (await resp.json())["type"] == "create_entry"
    await hass.async_block_till_done()

    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [f["context"]["source"] for f in flows] == [SOURCE_REAUTH]


@pytest.mark.parametrize("code", ["unauthenticated", "not_found"])
async def test_revoked_credential_at_setup(
    hass: HomeAssistant, fake_cloud: FakeCloud, entry, fake_agent, code
) -> None:
    """A revoked key answers unauthenticated, or not_found for ~30 s after
    the unlink while Auth's cache holds it: both are an unlink."""
    fake_cloud.auth_error_all = True
    fake_cloud.auth_error_code = code
    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_ERROR
    issue = _issue(hass, "instance_unlinked", entry)
    assert issue is not None and issue.is_fixable
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [f["context"]["source"] for f in flows] == [SOURCE_REAUTH]


async def test_wrong_credential_is_not_an_unlink(
    hass: HomeAssistant, fake_cloud: FakeCloud, entry, fake_agent
) -> None:
    """permission_denied: the bearer is not an instance key. Re-auth fixes
    it, but nothing says the instance was unlinked."""
    fake_cloud.auth_error_all = True
    fake_cloud.auth_error_code = "permission_denied"
    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert _issue(hass, "instance_unlinked", entry) is None
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [f["context"]["source"] for f in flows] == [SOURCE_REAUTH]


async def test_unlinked_while_running(
    hass: HomeAssistant,
    fake_cloud: FakeCloud,
    entry,
    fake_agent,
) -> None:
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    [agent] = fake_agent.instances

    # Deleted in the console: the credential is revoked.
    fake_cloud.auth_error_all = True
    async_fire_time_changed(hass, dt_util.utcnow() + SEEN_INTERVAL)
    await wait_for(hass, lambda: agent.stopped)

    assert _issue(hass, "instance_unlinked", entry) is not None
    assert hass.states.get(SENSOR).attributes["state"] == "unlinked"
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [f["context"]["source"] for f in flows] == [SOURCE_REAUTH]


async def test_api_down_retries_setup(
    hass: HomeAssistant, fake_cloud: FakeCloud, entry, fake_agent
) -> None:
    fake_cloud.connection_error = ("unavailable", "down", [])
    assert not await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_agent_library_missing_is_a_repair(
    hass: HomeAssistant, fake_cloud: FakeCloud, entry, http_runner
) -> None:
    """Without the library the real factory reports remote access unavailable
    instead of crashing; backups and ICE keep working."""
    with patch.dict(sys.modules, {"custom_components.xeonr_cloud.burrow_agent": None}):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await wait_for(
            hass, lambda: _issue(hass, "agent_unavailable", entry) is not None
        )
    assert entry.state is ConfigEntryState.LOADED
    assert hass.states.get(SENSOR).attributes["state"] == "agent_unavailable"


async def test_agent_waits_for_the_http_server(
    hass: HomeAssistant, fake_cloud: FakeCloud, entry, fake_agent, http_runner
) -> None:
    """On a restart the entry sets up before HA's HTTP server has a runner."""
    http_runner.runner = None
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert fake_agent.instances == []
    http_runner.runner = object()
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=2))
    await wait_for(hass, lambda: len(fake_agent.instances) == 1)
