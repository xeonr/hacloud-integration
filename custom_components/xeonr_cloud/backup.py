"""Backup agent: encrypted backups to the team's storage, via presigned URLs.

Backup bytes never pass through the instance API. CreateInstanceBackup
returns one presigned PUT per part; we stream the backup into them in order,
then MarkInstanceBackupUploaded completes the multipart upload with the ETags.
Downloads are a presigned GET.

Only encrypted ("protected") backups are accepted, matching the server, which
refuses unencrypted metadata with invalid_argument (DESIGN §3.5).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Coroutine
import json
import logging
import math
from typing import TYPE_CHECKING, Any, NoReturn

import aiohttp
from homeassistant.components.backup import (
    AgentBackup,
    BackupAgent,
    BackupAgentError,
    BackupNotFound,
    OnProgressCallback,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import (
    ChunkAsyncStreamIterator,
    async_get_clientsession,
)
from homeassistant.util.hass_dict import HassKey

from .api import ConnectError, HacloudError, InstanceBackup, UploadPart
from .const import DOMAIN

if TYPE_CHECKING:
    from . import XeonrCloudConfigEntry

_LOGGER = logging.getLogger(__name__)

DATA_BACKUP_AGENT_LISTENERS: HassKey[list[Callable[[], None]]] = HassKey(
    f"{DOMAIN}.backup_agent_listeners"
)

# Per-part attempts. A retry reopens the backup stream and skips to the
# part's offset, so it costs a re-read but never re-uploads earlier parts.
PART_ATTEMPTS = 3
PART_RETRY_SECONDS = 5
_TRANSFER_TIMEOUT = aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=300)


async def async_get_backup_agents(
    hass: HomeAssistant, **kwargs: Any
) -> list[BackupAgent]:
    return [
        XeonrCloudBackupAgent(hass, entry)
        for entry in hass.config_entries.async_loaded_entries(DOMAIN)
    ]


@callback
def async_register_backup_agents_listener(
    hass: HomeAssistant,
    *,
    listener: Callable[[], None],
    **kwargs: Any,
) -> Callable[[], None]:
    hass.data.setdefault(DATA_BACKUP_AGENT_LISTENERS, []).append(listener)

    @callback
    def remove_listener() -> None:
        hass.data[DATA_BACKUP_AGENT_LISTENERS].remove(listener)
        if not hass.data[DATA_BACKUP_AGENT_LISTENERS]:
            del hass.data[DATA_BACKUP_AGENT_LISTENERS]

    return remove_listener


@callback
def async_notify_backup_listeners(hass: HomeAssistant) -> None:
    for listener in hass.data.get(DATA_BACKUP_AGENT_LISTENERS, []):
        listener()


def _to_agent_backup(backup: InstanceBackup) -> AgentBackup | None:
    try:
        return AgentBackup.from_dict(json.loads(backup.metadata_json))
    except (ValueError, KeyError, TypeError) as err:
        _LOGGER.warning(
            "Skipping backup %s with unreadable metadata: %s",
            backup.home_assistant_backup_id,
            err,
        )
        return None


class _PartReader:
    """Hands out consecutive byte ranges of one backup stream as bodies."""

    def __init__(self, stream: AsyncIterator[bytes]) -> None:
        self._stream = stream
        self._leftover = b""
        self.ended_early = False

    async def body(
        self, length: int, on_bytes: Callable[[int], None]
    ) -> AsyncIterator[bytes]:
        remaining = length
        if self._leftover:
            chunk, self._leftover = (
                self._leftover[:remaining],
                self._leftover[remaining:],
            )
            remaining -= len(chunk)
            on_bytes(len(chunk))
            yield chunk
        while remaining > 0:
            try:
                chunk = await anext(self._stream)
            except StopAsyncIteration:
                self.ended_early = True
                raise BackupAgentError("Backup stream ended early") from None
            if len(chunk) > remaining:
                chunk, self._leftover = chunk[:remaining], chunk[remaining:]
            remaining -= len(chunk)
            on_bytes(len(chunk))
            yield chunk


async def _skip(stream: AsyncIterator[bytes], offset: int) -> AsyncIterator[bytes]:
    """Return the stream positioned at offset."""
    leftover = b""
    while offset > 0:
        chunk = await anext(stream)
        if len(chunk) > offset:
            leftover = chunk[offset:]
        offset -= min(len(chunk), offset)

    async def _rest() -> AsyncIterator[bytes]:
        if leftover:
            yield leftover
        async for chunk in stream:
            yield chunk

    return _rest()


class XeonrCloudBackupAgent(BackupAgent):
    """One agent per linked instance."""

    domain = DOMAIN

    def __init__(self, hass: HomeAssistant, entry: XeonrCloudConfigEntry) -> None:
        super().__init__()
        self._hass = hass
        self._entry = entry
        self.name = entry.title
        self.unique_id = entry.unique_id or entry.entry_id

    @property
    def _api(self):
        return self._entry.runtime_data.api

    def _raise(self, err: HacloudError, action: str) -> NoReturn:
        if isinstance(err, ConnectError):
            if err.is_auth:
                self._entry.runtime_data.on_auth_error()
            if err.code == "not_found":
                raise BackupNotFound(f"Backup not found: {err.message}") from err
            if err.code == "resource_exhausted":
                raise BackupAgentError(
                    f"Backup quota exceeded for this instance: {err.message}"
                ) from err
            if err.code == "failed_precondition":
                # The team's backup bucket does not exist yet; it is made
                # when someone with storage admin links or edits the instance.
                raise BackupAgentError(
                    f"Backup storage is not ready for this instance: {err.message}"
                ) from err
            if err.code == "already_exists":
                raise BackupAgentError(
                    f"This backup is already stored in Xeonr cloud: {err.message}"
                ) from err
            if err.code == "invalid_argument":
                raise BackupAgentError(f"Backup refused: {err.message}") from err
        raise BackupAgentError(f"Failed to {action}: {err}") from err

    async def async_upload_backup(
        self,
        *,
        open_stream: Callable[[], Coroutine[Any, Any, AsyncIterator[bytes]]],
        backup: AgentBackup,
        on_progress: OnProgressCallback,
        **kwargs: Any,
    ) -> None:
        if not backup.protected:
            raise BackupAgentError(
                "Xeonr cloud only stores encrypted backups; turn on backup "
                "encryption for this location"
            )
        try:
            upload = await self._api.create_backup(
                backup_id=backup.backup_id,
                size_bytes=backup.size,
                metadata_json=json.dumps(backup.as_dict()),
            )
        except HacloudError as err:
            self._raise(err, "start the upload")

        part_size = upload.part_size_bytes
        if part_size <= 0 or len(upload.parts) != math.ceil(backup.size / part_size):
            raise BackupAgentError(
                f"Server returned {len(upload.parts)} parts of {part_size} bytes "
                f"for a {backup.size}-byte backup"
            )

        session = async_get_clientsession(self._hass)
        reader = _PartReader(await open_stream())
        uploaded = 0

        def _count(n: int) -> None:
            nonlocal uploaded
            uploaded += n
            on_progress(bytes_uploaded=uploaded)

        completed: list[tuple[int, str]] = []
        for index, part in enumerate(upload.parts):
            offset = index * part_size
            length = min(part_size, backup.size - offset)
            last_error: Exception | None = None
            for attempt in range(1, PART_ATTEMPTS + 1):
                if attempt > 1:
                    # The failed attempt consumed an unknown part of the
                    # stream; restart it at this part's offset and carry on
                    # from there for the remaining parts too.
                    await asyncio.sleep(PART_RETRY_SECONDS)
                    reader = _PartReader(await _skip(await open_stream(), offset))
                    uploaded = offset
                try:
                    etag = await _put_part(
                        session, part, length, reader.body(length, _count)
                    )
                except _RetryablePartError as err:
                    if reader.ended_early:
                        # Shorter than its declared size; retrying cannot help.
                        raise BackupAgentError(
                            f"Backup stream ended early in part {part.part_number}"
                        ) from err
                    last_error = err
                    _LOGGER.info(
                        "Part %s attempt %s failed: %s", part.part_number, attempt, err
                    )
                    continue
                completed.append((part.part_number, etag))
                break
            else:
                raise BackupAgentError(
                    f"Failed to upload part {part.part_number}: {last_error}"
                ) from last_error

        try:
            await self._api.mark_backup_uploaded(
                upload_id=upload.upload_id, parts=completed
            )
        except HacloudError as err:
            self._raise(err, "complete the upload")

    async def async_download_backup(
        self, backup_id: str, **kwargs: Any
    ) -> AsyncIterator[bytes]:
        try:
            download = await self._api.get_backup(backup_id)
        except HacloudError as err:
            self._raise(err, "get the backup")
        session = async_get_clientsession(self._hass)
        try:
            resp = await session.get(download.download_url, timeout=_TRANSFER_TIMEOUT)
        except (TimeoutError, aiohttp.ClientError) as err:
            raise BackupAgentError(f"Failed to download backup: {err}") from err
        if resp.status == 404:
            resp.release()
            raise BackupNotFound(f"Backup {backup_id} not found in storage")
        if resp.status != 200:
            resp.release()
            raise BackupAgentError(f"Failed to download backup: HTTP {resp.status}")
        return ChunkAsyncStreamIterator(resp.content)

    async def async_delete_backup(self, backup_id: str, **kwargs: Any) -> None:
        try:
            await self._api.delete_backup(backup_id)
        except HacloudError as err:
            self._raise(err, "delete the backup")

    async def async_list_backups(self, **kwargs: Any) -> list[AgentBackup]:
        try:
            backups = await self._api.list_backups()
        except HacloudError as err:
            self._raise(err, "list backups")
        return [b for b in map(_to_agent_backup, backups) if b is not None]

    async def async_get_backup(self, backup_id: str, **kwargs: Any) -> AgentBackup:
        try:
            download = await self._api.get_backup(backup_id)
        except HacloudError as err:
            self._raise(err, "get the backup")
        backup = _to_agent_backup(download.backup)
        if backup is None:
            raise BackupNotFound(f"Backup {backup_id} has unreadable metadata")
        return backup


class _RetryablePartError(Exception):
    """A part upload that may succeed if tried again."""


async def _put_part(
    session: aiohttp.ClientSession,
    part: UploadPart,
    length: int,
    body: AsyncIterator[bytes],
) -> str:
    """PUT one part to its presigned URL and return storage's ETag."""
    try:
        async with session.put(
            part.url,
            data=body,
            # A presigned PUT needs a length; without it aiohttp would send
            # the generator chunked.
            headers={"Content-Length": str(length)},
            timeout=_TRANSFER_TIMEOUT,
        ) as resp:
            if resp.status not in (200, 201):
                text = (await resp.text())[:200]
                message = f"Part {part.part_number} refused: HTTP {resp.status} {text}"
                if resp.status >= 500 or resp.status == 429:
                    raise _RetryablePartError(message)
                raise BackupAgentError(message)
            etag = resp.headers.get("ETag")
    except (TimeoutError, aiohttp.ClientError) as err:
        raise _RetryablePartError(str(err)) from err
    if not etag:
        raise BackupAgentError(f"Storage returned no ETag for part {part.part_number}")
    return etag
