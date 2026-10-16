"""Config flow: device sign-in, team pick, CreateInstance, external_url."""

from __future__ import annotations

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
import pytest

from custom_components.xeonr_cloud.const import (
    CONF_EXTERNAL_URL_SET_BY_US,
    CONF_INSTANCE_SECRET,
    CONF_SET_EXTERNAL_URL,
    CONF_TEAM,
    DOMAIN,
    REAUTH_RELINK_NEW,
)

from .fake_cloud import (
    INSTANCE_ID,
    INSTANCE_SECRET,
    TEAM_A,
    TEAM_B,
    USER_TOKEN,
    FakeCloud,
)


async def _sign_in(
    hass: HomeAssistant,
    flow_id: str | None = None,
    *,
    source=config_entries.SOURCE_USER,
):
    """Walk the user (or reauth confirm) form and the device progress step."""
    if flow_id is None:
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": source}
        )
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "user"
        flow_id = result["flow_id"]
    result = await hass.config_entries.flow.async_configure(flow_id, {})
    assert result["type"] is FlowResultType.SHOW_PROGRESS
    assert result["step_id"] == "device"
    assert result["description_placeholders"] == {
        "url": "https://xnr.app/tv?code=ABC123",
        "code": "ABC123",
    }
    await hass.async_block_till_done()
    return await hass.config_entries.flow.async_configure(flow_id)


async def test_single_team_sets_up_and_offers_external_url(
    hass: HomeAssistant, configured: FakeCloud, fake_agent
) -> None:
    configured.poll_results = ["authorization_pending", "slow_down"]
    hass.config.location_name = "Holiday cottage"
    assert hass.config.external_url is None

    result = await _sign_in(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "external_url"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_SET_EXTERNAL_URL: True}
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Holiday cottage"
    assert result["result"].unique_id == INSTANCE_ID

    data = result["data"]
    assert data[CONF_INSTANCE_SECRET] == INSTANCE_SECRET
    assert data[CONF_TEAM] == TEAM_A
    # The person's token is never kept.
    assert USER_TOKEN not in str(dict(result["result"].data))
    assert USER_TOKEN not in str(dict(result["result"].options))
    assert result["options"] == {CONF_SET_EXTERNAL_URL: True}

    assert configured.polls == 3
    assert configured.created == [
        {
            "team": TEAM_A,
            "name": "Holiday cottage",
            "homeAssistantVersion": configured.created[0]["homeAssistantVersion"],
            "integrationVersion": "0.1.0",
        }
    ]
    assert (
        "device_code scope=openid profile auth:teams:read hacloud:instances:read "
        "hacloud:instances:edit burrow:managed:read burrow:managed:edit "
        "upl:bucket:read upl:bucket:manage"
    ) in configured.calls

    # Setup ran: the tunnel URL became external_url.
    assert hass.config.external_url == "https://quiet-otter.brw.xnr.app"
    assert (
        result["result"].data[CONF_EXTERNAL_URL_SET_BY_US] == hass.config.external_url
    )


async def test_external_url_step_skipped_when_set(
    hass: HomeAssistant, configured: FakeCloud, fake_agent
) -> None:
    await hass.config.async_update(external_url="https://ha.example.com")
    result = await _sign_in(hass)
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["options"] == {CONF_SET_EXTERNAL_URL: False}
    assert hass.config.external_url == "https://ha.example.com"


async def test_several_teams_ask_which(
    hass: HomeAssistant, configured: FakeCloud, fake_agent
) -> None:
    configured.teams = [
        {"teamId": TEAM_B, "slug": "work", "name": "Work"},
        {"teamId": TEAM_A, "slug": "home", "name": "Home"},
    ]
    result = await _sign_in(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "team"
    options = result["data_schema"].schema[CONF_TEAM].config["options"]
    assert [o["label"] for o in options] == ["Home (home)", "Work (work)"]

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_TEAM: TEAM_B}
    )
    assert result["step_id"] == "external_url"
    assert configured.created[0]["team"] == TEAM_B


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (("resource_exhausted", "limit"), "instance_limit"),
        (("failed_precondition", "not enabled"), "product_not_enabled"),
        (("permission_denied", "editor"), "not_editor"),
        (("internal", "boom"), "cannot_connect"),
    ],
)
async def test_create_instance_refused(
    hass: HomeAssistant, configured: FakeCloud, error, reason
) -> None:
    configured.create_error = error
    result = await _sign_in(hass)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == reason


@pytest.mark.parametrize(
    ("poll", "reason"),
    [("access_denied", "access_denied"), ("invalid_grant", "code_expired")],
)
async def test_sign_in_ends(
    hass: HomeAssistant, configured: FakeCloud, poll, reason
) -> None:
    configured.poll_results = ["authorization_pending", poll]
    result = await _sign_in(hass)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == reason
    assert configured.created == []


async def test_unregistered_client(hass: HomeAssistant, configured: FakeCloud) -> None:
    configured.device_error = "invalid_client"
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "invalid_client"


async def test_single_instance(
    hass: HomeAssistant, configured: FakeCloud, entry
) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "single_instance_allowed"


async def test_reauth_regenerates_the_credential(
    hass: HomeAssistant, configured: FakeCloud, entry, fake_agent
) -> None:
    result = await entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"
    result = await _sign_in(hass, result["flow_id"])
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    # Same instance, new credential; nothing new linked.
    assert configured.regenerated == [INSTANCE_ID]
    assert configured.created == []
    assert entry.unique_id == INSTANCE_ID
    assert entry.data[CONF_INSTANCE_SECRET] == INSTANCE_SECRET


async def test_reauth_relinks_when_the_instance_is_gone(
    hass: HomeAssistant, configured: FakeCloud, entry, fake_agent
) -> None:
    configured.regenerate_not_found = True
    configured.teams.append({"teamId": TEAM_B, "slug": "work", "name": "Work"})
    result = await entry.start_reauth_flow(hass)
    result = await _sign_in(hass, result["flow_id"])
    assert result["reason"] == "reauth_successful"
    # The entry's own team is reused without asking.
    assert configured.created[0]["team"] == TEAM_A


async def test_reauth_after_reservation_removed_links_new(
    hass: HomeAssistant, configured: FakeCloud, entry, fake_agent
) -> None:
    result = await entry.start_reauth_flow(
        hass, data={**entry.data, REAUTH_RELINK_NEW: True}
    )
    result = await _sign_in(hass, result["flow_id"])
    assert result["reason"] == "reauth_successful"
    assert configured.regenerated == []
    assert len(configured.created) == 1


async def test_options_flow(
    hass: HomeAssistant, configured: FakeCloud, entry, fake_agent
) -> None:
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.config.external_url == "https://quiet-otter.brw.xnr.app"

    # Ours: may be turned off, which clears it.
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_SET_EXTERNAL_URL: False}
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert hass.config.external_url is None
    assert CONF_EXTERNAL_URL_SET_BY_US not in entry.data

    # The user's: left alone, and the option is not offered.
    await hass.config.async_update(external_url="https://ha.example.com")
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "external_url_set_by_user"
