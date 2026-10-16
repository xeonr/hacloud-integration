"""A localhost fake of Xeonr Auth, the hacloud instance API and storage.

Real HTTP over 127.0.0.1, so the integration's own aiohttp code (Connect JSON,
form posts, presigned PUT/GET with Content-Length) is what gets exercised.
Responses follow the contract on xeonr/protocol feat/hacloud-contract
(7524b88): camelCase protobuf JSON, uint64 as strings, Connect error bodies.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
import hashlib
import json
from typing import Any

from aiohttp import web
from aiohttp.test_utils import TestServer

INSTANCE_ID = "6f1c2a9e-0000-4000-8000-000000000001"
INSTANCE_SECRET = "hacloud_inst_test_secret"
USER_TOKEN = "user-access-token"
TEAM_A = "urn:xeonr:auth:team:aaaaaaaa-0000-4000-8000-000000000000"
TEAM_B = "urn:xeonr:auth:team:bbbbbbbb-0000-4000-8000-000000000000"


def error_info(reason: str) -> dict[str, Any]:
    """A google.rpc.ErrorInfo detail as connect-go renders it, no debug."""
    raw = reason.encode()
    value = bytes([0x0A, len(raw)]) + raw + bytes([0x12, 4]) + b"test"
    return {
        "type": "google.rpc.ErrorInfo",
        "value": base64.b64encode(value).decode().rstrip("="),
    }


@dataclass
class Upload:
    backup_id: str
    size: int
    metadata_json: str
    part_size: int
    parts: dict[int, bytes] = field(default_factory=dict)


@dataclass
class FakeCloud:
    """State the tests poke at."""

    # Auth
    teams: list[dict[str, Any]] = field(
        default_factory=lambda: [
            {"teamId": TEAM_A, "slug": "home", "name": "Home"},
        ]
    )
    device_error: str | None = None
    poll_results: list[str] = field(default_factory=list)  # errors before success
    polls: int = 0

    # Instance API
    create_error: tuple[str, str] | None = None
    created: list[dict[str, Any]] = field(default_factory=list)
    regenerated: list[str] = field(default_factory=list)
    regenerate_not_found: bool = False
    connection_error: tuple[str, str, list] | None = None
    seen: list[dict[str, Any]] = field(default_factory=list)
    auth_error_all: bool = False
    ice_ttl: timedelta = timedelta(hours=1)
    ice_calls: int = 0
    ice_error: tuple[str, str] | None = None

    # Backups
    part_size: int = 64
    uploads: dict[str, Upload] = field(default_factory=dict)
    backups: dict[str, dict[str, Any]] = field(default_factory=dict)
    put_failures: int = 0
    received_put_lengths: list[int] = field(default_factory=list)

    server: TestServer | None = None
    calls: list[str] = field(default_factory=list)

    @property
    def url(self) -> str:
        assert self.server is not None
        return str(self.server.make_url("")).rstrip("/")

    # --- server

    def app(self) -> web.Application:
        app = web.Application()
        app.router.add_get("/.well-known/openid-configuration", self._discovery)
        app.router.add_post("/api/v1/oauth/code", self._device_code)
        app.router.add_post("/api/v1/oauth/token", self._token)
        app.router.add_post(
            "/xeonr.auth.api.v1.TeamsService/ListMyTeams", self._list_my_teams
        )
        app.router.add_post("/{service}/{method}", self._connect)
        app.router.add_put("/storage/{upload}/{part}", self._put_part)
        app.router.add_get("/storage/backup/{backup_id}", self._get_object)
        return app

    async def start(self) -> None:
        self.server = TestServer(self.app(), host="127.0.0.1")
        await self.server.start_server()

    async def stop(self) -> None:
        if self.server is not None:
            await self.server.close()

    # --- auth

    async def _discovery(self, request: web.Request) -> web.Response:
        return web.json_response(
            {
                "issuer": self.url,
                "device_authorization_endpoint": f"{self.url}/api/v1/oauth/code",
                "token_endpoint": f"{self.url}/api/v1/oauth/token",
            }
        )

    async def _device_code(self, request: web.Request) -> web.Response:
        form = await request.post()
        self.calls.append(f"device_code scope={form.get('scope')}")
        if self.device_error:
            return web.json_response({"error": self.device_error}, status=400)
        return web.json_response(
            {
                "device_code": "DEVCODE-1",
                "user_code": "ABC123",
                "verification_uri": "https://xnr.app/tv",
                "verification_uri_complete": "https://xnr.app/tv?code=ABC123",
                "expires_in": 300,
                "interval": 5,
            }
        )

    async def _token(self, request: web.Request) -> web.Response:
        form = await request.post()
        assert form["grant_type"] == "urn:ietf:params:oauth:grant-type:device_code"
        assert form["device_code"] == "DEVCODE-1"
        self.polls += 1
        if self.poll_results:
            return web.json_response({"error": self.poll_results.pop(0)}, status=400)
        return web.json_response(
            {
                "access_token": USER_TOKEN,
                "token_type": "Bearer",
                "expires_in": 3600,
                "scope": form.get("scope", ""),
            }
        )

    async def _list_my_teams(self, request: web.Request) -> web.Response:
        assert request.headers["Authorization"] == f"Bearer {USER_TOKEN}"
        return web.json_response(
            {
                "teams": [
                    {
                        "team": t,
                        "role": "TEAM_ROLE_MEMBER",
                        "applicationRoles": ["editor"],
                        "entitled": True,
                    }
                    for t in self.teams
                ]
            }
        )

    # --- Connect

    @staticmethod
    def _error(
        code: str, message: str = "", details: list | None = None, status: int = 400
    ) -> web.Response:
        body: dict[str, Any] = {"code": code, "message": message}
        if details:
            body["details"] = details
        status = {
            "unauthenticated": 401,
            "permission_denied": 403,
            "not_found": 404,
            "resource_exhausted": 429,
            "failed_precondition": 400,
            "invalid_argument": 400,
        }.get(code, status)
        return web.json_response(body, status=status)

    async def _connect(self, request: web.Request) -> web.Response:
        service = request.match_info["service"]
        method = request.match_info["method"]
        assert request.headers.get("Connect-Protocol-Version") == "1"
        body = await request.json()
        self.calls.append(f"{service}/{method}")
        token = request.headers.get("Authorization", "").removeprefix("Bearer ")

        if (
            service == "xeonr.hacloud.v1.InstanceService"
            and method == "RegenerateInstanceSecret"
        ):
            if token != USER_TOKEN:
                return self._error("unauthenticated")
            if self.regenerate_not_found:
                return self._error("not_found", "no such instance")
            self.regenerated.append(body["id"])
            return web.json_response(
                {
                    "instance": {
                        "id": body["id"],
                        "team": TEAM_A,
                        "name": "Home",
                        "url": "https://quiet-otter.brw.xnr.app",
                    },
                    "instanceSecret": INSTANCE_SECRET,
                }
            )

        if service == "xeonr.hacloud.v1.InstanceService" and method == "CreateInstance":
            if token != USER_TOKEN:
                return self._error("unauthenticated")
            if self.create_error:
                return self._error(*self.create_error)
            self.created.append(body)
            return web.json_response(
                {
                    "instance": {
                        "id": INSTANCE_ID
                        if len(self.created) == 1
                        else f"{INSTANCE_ID[:-1]}{len(self.created)}",
                        "team": body["team"] or TEAM_A,
                        "name": body["name"],
                        "url": "https://quiet-otter.brw.xnr.app",
                        "remoteAccessEnabled": True,
                        "connectionState": "CONNECTION_STATE_NEVER_CONNECTED",
                    },
                    "instanceSecret": INSTANCE_SECRET,
                }
            )

        if not service.startswith("xeonr.hacloud.private.v1."):
            return self._error("unimplemented", status=404)
        if token != INSTANCE_SECRET or self.auth_error_all:
            return self._error("unauthenticated", "instance credential revoked")
        handler = getattr(self, f"_rpc_{method}", None)
        if handler is None:
            return self._error("unimplemented", status=404)
        return await handler(body)

    async def _rpc_GetConnection(self, body: dict) -> web.Response:
        if self.connection_error:
            code, message, details = self.connection_error
            return self._error(code, message, details)
        return web.json_response(
            {
                "instanceId": INSTANCE_ID,
                "agentUrl": "wss://connect.brw.xnr.app/v1/agent",
                "agentCredential": "burrow_agent_cred",
                "reservationName": "quiet-otter",
                "publicUrl": "https://quiet-otter.brw.xnr.app",
            }
        )

    async def _rpc_MarkConnectionSeen(self, body: dict) -> web.Response:
        self.seen.append(body)
        return web.json_response({"recordedAt": "2026-10-16T12:00:00.123456789Z"})

    async def _rpc_ListIceServers(self, body: dict) -> web.Response:
        if self.ice_error:
            return self._error(*self.ice_error)
        self.ice_calls += 1
        expires = datetime.now(UTC) + self.ice_ttl
        return web.json_response(
            {
                "iceServers": [
                    {"urls": ["stun:turn.prod.wtf:3478"]},
                    {
                        "urls": [
                            "turn:turn.prod.wtf:3478?transport=udp",
                            "turns:turn.prod.wtf:5349?transport=tcp",
                        ],
                        "username": f"{int(expires.timestamp())}:{INSTANCE_ID}",
                        "credential": f"cred-{self.ice_calls}",
                    },
                ],
                "expiresAt": expires.isoformat().replace("+00:00", "Z"),
            }
        )

    # --- backups

    async def _rpc_ListInstanceBackups(self, body: dict) -> web.Response:
        return web.json_response({"backups": list(self.backups.values())})

    async def _rpc_CreateInstanceBackup(self, body: dict) -> web.Response:
        metadata = json.loads(body["metadataJson"])
        if not metadata.get("protected"):
            return self._error("invalid_argument", "backup is not encrypted")
        size = int(body["sizeBytes"])
        upload_id = f"up-{len(self.uploads) + 1}"
        self.uploads[upload_id] = Upload(
            backup_id=body["homeAssistantBackupId"],
            size=size,
            metadata_json=body["metadataJson"],
            part_size=self.part_size,
        )
        count = -(-size // self.part_size)
        return web.json_response(
            {
                "uploadId": upload_id,
                "partSizeBytes": str(self.part_size),
                "parts": [
                    {
                        "partNumber": n,
                        "url": f"{self.url}/storage/{upload_id}/{n}?X-Amz-Signature=x",
                    }
                    for n in range(1, count + 1)
                ],
                "urlsExpireAt": "2026-10-16T13:00:00Z",
            }
        )

    async def _put_part(self, request: web.Request) -> web.Response:
        upload = self.uploads[request.match_info["upload"]]
        number = int(request.match_info["part"])
        assert "chunked" not in request.headers.get("Transfer-Encoding", "")
        length = int(request.headers["Content-Length"])
        data = await request.read()
        assert len(data) == length
        if self.put_failures:
            self.put_failures -= 1
            return web.Response(status=503, text="SlowDown")
        self.received_put_lengths.append(length)
        upload.parts[number] = data
        return web.Response(
            status=200, headers={"ETag": f'"{hashlib.md5(data).hexdigest()}"'}
        )

    async def _rpc_MarkInstanceBackupUploaded(self, body: dict) -> web.Response:
        upload = self.uploads.pop(body["uploadId"])
        parts = body["parts"]
        assert [p["partNumber"] for p in parts] == sorted(upload.parts)
        for p in parts:
            assert (
                p["etag"]
                == f'"{hashlib.md5(upload.parts[p["partNumber"]]).hexdigest()}"'
            )
        blob = b"".join(upload.parts[n] for n in sorted(upload.parts))
        assert len(blob) == upload.size
        record = {
            "homeAssistantBackupId": upload.backup_id,
            "sizeBytes": str(upload.size),
            "metadataJson": upload.metadata_json,
            "createdAt": "2026-10-16T12:00:00Z",
            "_blob": base64.b64encode(blob).decode(),
        }
        self.backups[upload.backup_id] = record
        return web.json_response(
            {"backup": {k: v for k, v in record.items() if k != "_blob"}}
        )

    async def _rpc_GetInstanceBackup(self, body: dict) -> web.Response:
        backup_id = body["homeAssistantBackupId"]
        if backup_id not in self.backups:
            return self._error("not_found", "no such backup")
        record = self.backups[backup_id]
        return web.json_response(
            {
                "backup": {k: v for k, v in record.items() if k != "_blob"},
                "downloadUrl": f"{self.url}/storage/backup/{backup_id}?X-Amz-Signature=x",
                "downloadExpiresAt": "2026-10-16T13:00:00Z",
            }
        )

    async def _get_object(self, request: web.Request) -> web.StreamResponse:
        record = self.backups.get(request.match_info["backup_id"])
        if record is None:
            return web.Response(status=404)
        return web.Response(body=base64.b64decode(record["_blob"]))

    async def _rpc_DeleteInstanceBackup(self, body: dict) -> web.Response:
        self.backups.pop(body["homeAssistantBackupId"], None)
        return web.json_response({})
