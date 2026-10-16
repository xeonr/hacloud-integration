"""The backup agent against the fake instance API and fake storage."""

from __future__ import annotations

from collections.abc import AsyncIterator
import json
import os
from unittest.mock import patch

from homeassistant.components.backup import (
    AgentBackup,
    BackupAgentError,
    BackupNotFound,
    async_get_manager,
)
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
import pytest

from custom_components.xeonr_cloud.const import DOMAIN

from .fake_cloud import INSTANCE_ID, FakeCloud

PAYLOAD = os.urandom(200)


def _backup(*, protected: bool = True, size: int = len(PAYLOAD)) -> AgentBackup:
    return AgentBackup(
        addons=[],
        backup_id="abc123",
        date="2026-10-16T12:00:00+00:00",
        database_included=True,
        extra_metadata={"instance_id": "x", "with_automatic_settings": False},
        folders=[],
        homeassistant_included=True,
        homeassistant_version="2026.10.1",
        name="Nightly",
        protected=protected,
        size=size,
    )


def _opener(data: bytes, chunk: int = 37):
    opened = 0

    async def open_stream() -> AsyncIterator[bytes]:
        nonlocal opened
        opened += 1

        async def gen() -> AsyncIterator[bytes]:
            for i in range(0, len(data), chunk):
                yield data[i : i + chunk]

        return gen()

    open_stream.opened = lambda: opened  # type: ignore[attr-defined]
    return open_stream


@pytest.fixture
async def agent(hass: HomeAssistant, fake_cloud: FakeCloud, entry, fake_agent):
    assert await async_setup_component(hass, "backup", {})
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    agents = async_get_manager(hass).backup_agents
    return agents[f"{DOMAIN}.{INSTANCE_ID}"]


async def test_agent_registered_and_removed(hass: HomeAssistant, agent, entry) -> None:
    assert agent.name == "Home"
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert f"{DOMAIN}.{INSTANCE_ID}" not in async_get_manager(hass).backup_agents


async def test_round_trip(hass: HomeAssistant, agent, fake_cloud: FakeCloud) -> None:
    progress: list[int] = []
    await agent.async_upload_backup(
        open_stream=_opener(PAYLOAD),
        backup=_backup(),
        on_progress=lambda *, bytes_uploaded, **kw: progress.append(bytes_uploaded),
    )
    # Four presigned parts of 64 bytes (the last 8), each with its length.
    assert fake_cloud.received_put_lengths == [64, 64, 64, 8]
    assert progress[-1] == len(PAYLOAD)
    assert progress == sorted(progress)

    [listed] = await agent.async_list_backups()
    assert listed == _backup()
    assert await agent.async_get_backup("abc123") == _backup()

    stream = await agent.async_download_backup("abc123")
    assert b"".join([c async for c in stream]) == PAYLOAD

    await agent.async_delete_backup("abc123")
    assert await agent.async_list_backups() == []
    with pytest.raises(BackupNotFound):
        await agent.async_get_backup("abc123")
    with pytest.raises(BackupNotFound):
        await agent.async_download_backup("abc123")
    # Deleting again is fine.
    await agent.async_delete_backup("abc123")


async def test_metadata_is_sent_verbatim(agent, fake_cloud: FakeCloud) -> None:
    await agent.async_upload_backup(
        open_stream=_opener(PAYLOAD), backup=_backup(), on_progress=lambda **kw: None
    )
    stored = fake_cloud.backups["abc123"]
    assert stored["sizeBytes"] == "200"
    assert json.loads(stored["metadataJson"]) == _backup().as_dict()


async def test_unencrypted_is_refused(agent, fake_cloud: FakeCloud) -> None:
    with pytest.raises(BackupAgentError, match="encrypted"):
        await agent.async_upload_backup(
            open_stream=_opener(PAYLOAD),
            backup=_backup(protected=False),
            on_progress=lambda **kw: None,
        )
    assert (
        "xeonr.hacloud.private.v1.InstanceBackupService/CreateInstanceBackup"
        not in fake_cloud.calls
    )
    assert fake_cloud.backups == {}


async def test_quota(agent, fake_cloud: FakeCloud) -> None:
    async def _exhausted(body):
        return fake_cloud._error("resource_exhausted", "over 10 GiB")

    fake_cloud._rpc_CreateInstanceBackup = _exhausted
    with pytest.raises(BackupAgentError, match="quota"):
        await agent.async_upload_backup(
            open_stream=_opener(PAYLOAD),
            backup=_backup(),
            on_progress=lambda **kw: None,
        )


async def test_part_retry_resumes_at_offset(agent, fake_cloud: FakeCloud) -> None:
    fake_cloud.put_failures = 1
    opener = _opener(PAYLOAD)
    with patch("custom_components.xeonr_cloud.backup.PART_RETRY_SECONDS", 0):
        await agent.async_upload_backup(
            open_stream=opener, backup=_backup(), on_progress=lambda **kw: None
        )
    assert opener.opened() == 2
    stream = await agent.async_download_backup("abc123")
    assert b"".join([c async for c in stream]) == PAYLOAD


async def test_short_stream_fails(agent, fake_cloud: FakeCloud) -> None:
    with pytest.raises(BackupAgentError, match="ended early"):
        await agent.async_upload_backup(
            open_stream=_opener(PAYLOAD[:150]),
            backup=_backup(),
            on_progress=lambda **kw: None,
        )
    assert fake_cloud.backups == {}
