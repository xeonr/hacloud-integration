"""Clients for the Home Assistant cloud instance API.

The API is ConnectRPC. We speak Connect's JSON protocol with plain aiohttp
rather than pull in a protobuf runtime: every call is a POST of a JSON body to
``<base>/<package>.<Service>/<Method>``, answered with 200 and a JSON body, or
a non-200 with a Connect error document (``{"code", "message", "details"}``).

Two credentials, two clients:

- ``HacloudUserApi`` holds the person's token from the device-code sign-in. It
  only ever calls ``xeonr.hacloud.v1.InstanceService/CreateInstance`` and lives
  for the length of a config flow. The token is never stored.
- ``HacloudInstanceApi`` holds the instance credential CreateInstance returned
  and calls everything in ``xeonr.hacloud.private.v1``.

Field names are protobuf JSON: we send lowerCamelCase and accept either form
back. 64-bit integers arrive as strings, timestamps as RFC 3339.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from datetime import datetime
import json
import logging
from typing import Any

import aiohttp

from .const import CONNECT_TIMEOUT

_LOGGER = logging.getLogger(__name__)

PUBLIC_PACKAGE = "xeonr.hacloud.v1"
PRIVATE_PACKAGE = "xeonr.hacloud.private.v1"

# ErrorInfo reasons for GetConnection's failed_precondition. See
# ConnectError.reason for how they are read.
REASON_REMOTE_ACCESS_DISABLED = "REMOTE_ACCESS_DISABLED"
REASON_RESERVATION_REMOVED = "RESERVATION_REMOVED"


class HacloudError(Exception):
    """Any failure talking to the instance API."""


class HacloudConnectionError(HacloudError):
    """The API could not be reached, or answered with something unparseable."""


class ConnectError(HacloudError):
    """A Connect error document returned by the API."""

    def __init__(
        self,
        code: str,
        message: str = "",
        details: list[dict[str, Any]] | None = None,
        http_status: int | None = None,
    ) -> None:
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code
        self.message = message
        self.details = details or []
        self.http_status = http_status

    @property
    def reason(self) -> str | None:
        """The google.rpc.ErrorInfo reason, if the error carries one.

        Connect's JSON error details are ``{"type", "value", "debug"}`` where
        ``value`` is the base64 binary message and ``debug`` an optional JSON
        rendering. We read ``debug.reason`` when present and otherwise decode
        field 1 of the ErrorInfo bytes ourselves, so no protobuf runtime is
        needed for one string.
        """
        for detail in self.details:
            if not str(detail.get("type", "")).endswith("google.rpc.ErrorInfo"):
                continue
            debug = detail.get("debug")
            if isinstance(debug, dict) and debug.get("reason"):
                return str(debug["reason"])
            value = detail.get("value")
            if isinstance(value, str):
                reason = _error_info_reason(value)
                if reason:
                    return reason
        return None

    @property
    def is_auth(self) -> bool:
        """Whether the credential itself was refused."""
        return self.code in ("unauthenticated", "permission_denied")


def _error_info_reason(value: str) -> str | None:
    """Pull ErrorInfo.reason (field 1, a string) out of its wire bytes."""
    try:
        raw = base64.b64decode(value + "=" * (-len(value) % 4))
    except ValueError:
        return None
    pos = 0
    while pos < len(raw):
        key, pos = _read_varint(raw, pos)
        if key is None:
            return None
        field_number, wire_type = key >> 3, key & 7
        if wire_type == 2:
            length, pos = _read_varint(raw, pos)
            if length is None or pos + length > len(raw):
                return None
            if field_number == 1:
                return raw[pos : pos + length].decode("utf-8", "replace")
            pos += length
        elif wire_type == 0:
            _, pos = _read_varint(raw, pos)
            if _ is None:
                return None
        else:
            return None
    return None


def _read_varint(raw: bytes, pos: int) -> tuple[int | None, int]:
    result = shift = 0
    while pos < len(raw):
        byte = raw[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, pos
        shift += 7
        if shift > 63:
            break
    return None, pos


def _get(data: dict[str, Any], camel: str, default: Any = None) -> Any:
    """Read a protobuf-JSON field by its camelCase or snake_case name."""
    if camel in data:
        return data[camel]
    snake = "".join(f"_{c.lower()}" if c.isupper() else c for c in camel)
    return data.get(snake, default)


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    # Python 3.11+ fromisoformat takes "Z" and up to 6 fractional digits; Go
    # renders up to 9, so trim to microseconds.
    if "." in value:
        head, _, tail = value.partition(".")
        digits = "".join(c for c in tail if c.isdigit())
        zone = tail[len(digits) :]
        value = f"{head}.{digits[:6]}{zone}"
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


@dataclass(frozen=True)
class Instance:
    """The parts of xeonr.hacloud.v1.Instance the integration uses."""

    id: str
    team: str
    name: str
    url: str

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Instance:
        return cls(
            id=_get(data, "id", ""),
            team=_get(data, "team", ""),
            name=_get(data, "name", ""),
            url=_get(data, "url", ""),
        )


@dataclass(frozen=True)
class CreatedInstance:
    instance: Instance
    instance_secret: str


@dataclass(frozen=True)
class Connection:
    """GetConnectionResponse."""

    instance_id: str
    agent_url: str
    agent_credential: str = field(repr=False)
    reservation_name: str
    public_url: str


@dataclass(frozen=True)
class IceServer:
    urls: list[str]
    username: str = ""
    credential: str = field(default="", repr=False)


@dataclass(frozen=True)
class IceServers:
    servers: list[IceServer]
    expires_at: datetime | None


@dataclass(frozen=True)
class InstanceBackup:
    home_assistant_backup_id: str
    size_bytes: int
    metadata_json: str
    created_at: datetime | None

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> InstanceBackup:
        return cls(
            home_assistant_backup_id=_get(data, "homeAssistantBackupId", ""),
            size_bytes=int(_get(data, "sizeBytes", 0) or 0),
            metadata_json=_get(data, "metadataJson", ""),
            created_at=_parse_time(_get(data, "createdAt")),
        )


@dataclass(frozen=True)
class UploadPart:
    part_number: int
    url: str = field(repr=False)


@dataclass(frozen=True)
class BackupUpload:
    upload_id: str
    part_size_bytes: int
    parts: list[UploadPart]
    urls_expire_at: datetime | None


@dataclass(frozen=True)
class BackupDownload:
    backup: InstanceBackup
    download_url: str = field(repr=False)
    download_expires_at: datetime | None


class _ConnectClient:
    """One Connect JSON client bound to a base URL and a bearer."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        base_url: str,
        token: str,
        user_agent: str,
    ) -> None:
        self._session = session
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._user_agent = user_agent

    async def call(
        self, package: str, service: str, method: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        url = f"{self._base_url}/{package}.{service}/{method}"
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
            "Connect-Protocol-Version": "1",
            "User-Agent": self._user_agent,
        }
        try:
            async with self._session.post(
                url,
                json=body,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=CONNECT_TIMEOUT),
            ) as resp:
                text = await resp.text()
                status = resp.status
        except (TimeoutError, aiohttp.ClientError) as err:
            raise HacloudConnectionError(f"{service}/{method}: {err}") from err

        try:
            data = json.loads(text) if text else {}
        except ValueError as err:
            if status != 200:
                raise ConnectError(
                    _code_for_status(status), text[:200], http_status=status
                ) from err
            raise HacloudConnectionError(
                f"{service}/{method}: unparseable response"
            ) from err

        if status != 200:
            if not isinstance(data, dict) or "code" not in data:
                raise ConnectError(_code_for_status(status), http_status=status)
            raise ConnectError(
                str(data["code"]),
                str(data.get("message", "")),
                data.get("details") if isinstance(data.get("details"), list) else None,
                http_status=status,
            )
        if not isinstance(data, dict):
            raise HacloudConnectionError(f"{service}/{method}: not an object")
        return data


def _code_for_status(status: int) -> str:
    """Connect's HTTP-to-code mapping, for errors without a JSON body."""
    return {
        400: "internal",
        401: "unauthenticated",
        403: "permission_denied",
        404: "unimplemented",
        429: "unavailable",
        502: "unavailable",
        503: "unavailable",
        504: "unavailable",
    }.get(status, "unknown")


class HacloudUserApi:
    """The person-token surface, used only by the config flow."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        base_url: str,
        access_token: str,
        user_agent: str,
    ) -> None:
        self._client = _ConnectClient(session, base_url, access_token, user_agent)

    async def create_instance(
        self,
        *,
        team: str,
        name: str,
        home_assistant_version: str,
        integration_version: str,
    ) -> CreatedInstance:
        data = await self._client.call(
            PUBLIC_PACKAGE,
            "InstanceService",
            "CreateInstance",
            {
                "team": team,
                "name": name,
                "homeAssistantVersion": home_assistant_version,
                "integrationVersion": integration_version,
            },
        )
        secret = _get(data, "instanceSecret", "")
        if not secret:
            raise HacloudConnectionError("CreateInstance returned no instance secret")
        return CreatedInstance(
            instance=Instance.from_json(_get(data, "instance", {}) or {}),
            instance_secret=secret,
        )

    async def regenerate_instance_secret(self, instance_id: str) -> CreatedInstance:
        """A new credential for an existing instance (the old one is revoked)."""
        data = await self._client.call(
            PUBLIC_PACKAGE,
            "InstanceService",
            "RegenerateInstanceSecret",
            {"id": instance_id},
        )
        secret = _get(data, "instanceSecret", "")
        if not secret:
            raise HacloudConnectionError(
                "RegenerateInstanceSecret returned no instance secret"
            )
        return CreatedInstance(
            instance=Instance.from_json(_get(data, "instance", {}) or {}),
            instance_secret=secret,
        )


class HacloudInstanceApi:
    """The instance-credential surface: xeonr.hacloud.private.v1."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        base_url: str,
        instance_secret: str,
        user_agent: str,
    ) -> None:
        self._client = _ConnectClient(session, base_url, instance_secret, user_agent)

    async def _call(
        self, service: str, method: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        return await self._client.call(PRIVATE_PACKAGE, service, method, body)

    async def get_connection(self) -> Connection:
        data = await self._call("ConnectionService", "GetConnection", {})
        return Connection(
            instance_id=_get(data, "instanceId", ""),
            agent_url=_get(data, "agentUrl", ""),
            agent_credential=_get(data, "agentCredential", ""),
            reservation_name=_get(data, "reservationName", ""),
            public_url=_get(data, "publicUrl", ""),
        )

    async def mark_connection_seen(
        self,
        *,
        home_assistant_version: str,
        integration_version: str,
        agent_connected: bool,
    ) -> None:
        await self._call(
            "ConnectionService",
            "MarkConnectionSeen",
            {
                "homeAssistantVersion": home_assistant_version,
                "integrationVersion": integration_version,
                "agentConnected": agent_connected,
            },
        )

    async def list_ice_servers(self) -> IceServers:
        data = await self._call("IceServerService", "ListIceServers", {})
        servers = [
            IceServer(
                urls=list(_get(s, "urls", []) or []),
                username=_get(s, "username", "") or "",
                credential=_get(s, "credential", "") or "",
            )
            for s in _get(data, "iceServers", []) or []
        ]
        return IceServers(
            servers=servers, expires_at=_parse_time(_get(data, "expiresAt"))
        )

    async def list_backups(self) -> list[InstanceBackup]:
        backups: list[InstanceBackup] = []
        cursor = ""
        while True:
            data = await self._call(
                "InstanceBackupService",
                "ListInstanceBackups",
                {"cursor": cursor, "limit": 500},
            )
            backups.extend(
                InstanceBackup.from_json(b) for b in _get(data, "backups", []) or []
            )
            cursor = _get(data, "nextCursor", "") or ""
            if not cursor:
                return backups

    async def create_backup(
        self, *, backup_id: str, size_bytes: int, metadata_json: str
    ) -> BackupUpload:
        data = await self._call(
            "InstanceBackupService",
            "CreateInstanceBackup",
            {
                "homeAssistantBackupId": backup_id,
                # uint64 is a string in protobuf JSON.
                "sizeBytes": str(size_bytes),
                "metadataJson": metadata_json,
            },
        )
        parts = sorted(
            (
                UploadPart(
                    part_number=int(_get(p, "partNumber", 0)), url=_get(p, "url", "")
                )
                for p in _get(data, "parts", []) or []
            ),
            key=lambda p: p.part_number,
        )
        return BackupUpload(
            upload_id=_get(data, "uploadId", ""),
            part_size_bytes=int(_get(data, "partSizeBytes", 0) or 0),
            parts=parts,
            urls_expire_at=_parse_time(_get(data, "urlsExpireAt")),
        )

    async def mark_backup_uploaded(
        self, *, upload_id: str, parts: list[tuple[int, str]]
    ) -> InstanceBackup:
        data = await self._call(
            "InstanceBackupService",
            "MarkInstanceBackupUploaded",
            {
                "uploadId": upload_id,
                "parts": [{"partNumber": n, "etag": etag} for n, etag in parts],
            },
        )
        return InstanceBackup.from_json(_get(data, "backup", {}) or {})

    async def get_backup(self, backup_id: str) -> BackupDownload:
        data = await self._call(
            "InstanceBackupService",
            "GetInstanceBackup",
            {"homeAssistantBackupId": backup_id},
        )
        return BackupDownload(
            backup=InstanceBackup.from_json(_get(data, "backup", {}) or {}),
            download_url=_get(data, "downloadUrl", ""),
            download_expires_at=_parse_time(_get(data, "downloadExpiresAt")),
        )

    async def delete_backup(self, backup_id: str) -> None:
        await self._call(
            "InstanceBackupService",
            "DeleteInstanceBackup",
            {"homeAssistantBackupId": backup_id},
        )
