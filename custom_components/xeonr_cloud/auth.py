"""Xeonr Auth device-code sign-in (RFC 8628), the way burrow-cli does it.

Used only inside the config flow. The access token it yields is handed to
CreateInstance and to TeamsService/ListMyTeams, then dropped; no refresh token
is requested (no ``offline_access``), and nothing here is persisted.

Xeonr Auth specifics (auth/api/pkg/api/oauth.go):

- device authorization is ``{issuer}/api/v1/oauth/code``, the token endpoint
  ``{issuer}/api/v1/oauth/token``; both are also in the discovery document;
- polling errors are HTTP 400 ``{"error": ...}``: ``authorization_pending``,
  ``slow_down``, ``access_denied``, and ``invalid_grant`` for a code that is
  unknown, expired or already claimed (it never sends ``expired_token``, but
  we treat that as expiry too);
- there is no ``audience``/``resource`` parameter: the token's ``aud`` comes
  from the scopes asked for.
"""

from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass, field
import json
import logging
import time
from typing import Any

import aiohttp

from .const import CONNECT_TIMEOUT

_LOGGER = logging.getLogger(__name__)

DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
TEAMS_CLAIM = "urn:xeonr:auth:teams"

# Indirection so tests can poll without waiting.
_sleep = asyncio.sleep


class AuthError(Exception):
    """Sign-in failed."""

    def __init__(self, code: str, description: str = "") -> None:
        super().__init__(f"{code}: {description}" if description else code)
        self.code = code
        self.description = description


class AuthCannotConnect(AuthError):
    def __init__(self, description: str = "") -> None:
        super().__init__("cannot_connect", description)


@dataclass(frozen=True)
class DeviceAuthorization:
    device_code: str = field(repr=False)
    user_code: str
    verification_uri: str
    verification_uri_complete: str
    expires_in: int
    interval: int


@dataclass(frozen=True)
class Team:
    urn: str
    slug: str
    name: str
    application_roles: tuple[str, ...] = ()

    @property
    def label(self) -> str:
        if self.name and self.slug and self.name != self.slug:
            return f"{self.name} ({self.slug})"
        return self.name or self.slug or self.urn


class XeonrAuthClient:
    """Device flow plus the one teams call the config flow needs."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        issuer: str,
        client_id: str,
        scopes: tuple[str, ...],
    ) -> None:
        self._session = session
        self._issuer = issuer.rstrip("/")
        self._client_id = client_id
        self._scopes = scopes
        self._endpoints: dict[str, str] | None = None

    async def _discover(self) -> dict[str, str]:
        if self._endpoints is not None:
            return self._endpoints
        endpoints = {
            "device_authorization_endpoint": f"{self._issuer}/api/v1/oauth/code",
            "token_endpoint": f"{self._issuer}/api/v1/oauth/token",
        }
        try:
            async with self._session.get(
                f"{self._issuer}/.well-known/openid-configuration",
                timeout=aiohttp.ClientTimeout(total=CONNECT_TIMEOUT),
            ) as resp:
                if resp.status == 200:
                    doc = await resp.json(content_type=None)
                    for key in endpoints:
                        if isinstance(doc.get(key), str) and doc[key]:
                            endpoints[key] = doc[key]
        except (TimeoutError, aiohttp.ClientError, ValueError) as err:
            # Fall back to the known paths; a real outage shows up on the
            # next request with a better error.
            _LOGGER.debug("OIDC discovery failed: %s", err)
        self._endpoints = endpoints
        return endpoints

    async def _post_form(
        self, url: str, form: dict[str, str]
    ) -> tuple[int, dict[str, Any]]:
        try:
            async with self._session.post(
                url,
                data=form,
                headers={"Accept": "application/json"},
                timeout=aiohttp.ClientTimeout(total=CONNECT_TIMEOUT),
            ) as resp:
                status = resp.status
                try:
                    body = await resp.json(content_type=None)
                except ValueError:
                    body = {}
        except (TimeoutError, aiohttp.ClientError) as err:
            raise AuthCannotConnect(str(err)) from err
        return status, body if isinstance(body, dict) else {}

    async def start(self) -> DeviceAuthorization:
        endpoints = await self._discover()
        status, body = await self._post_form(
            endpoints["device_authorization_endpoint"],
            {"client_id": self._client_id, "scope": " ".join(self._scopes)},
        )
        if status != 200 or "device_code" not in body:
            raise AuthError(
                str(body.get("error", f"http_{status}")),
                str(body.get("error_description", "")),
            )
        uri = str(body.get("verification_uri", ""))
        return DeviceAuthorization(
            device_code=str(body["device_code"]),
            user_code=str(body.get("user_code", "")),
            verification_uri=uri,
            verification_uri_complete=str(body.get("verification_uri_complete") or uri),
            expires_in=int(body.get("expires_in", 300)),
            interval=max(int(body.get("interval", 5)), 1),
        )

    async def poll(self, device: DeviceAuthorization) -> str:
        """Poll until the person approves; return the access token."""
        endpoints = await self._discover()
        interval = device.interval
        deadline = time.monotonic() + device.expires_in
        while True:
            await _sleep(interval)
            if time.monotonic() > deadline:
                raise AuthError("expired_token")
            status, body = await self._post_form(
                endpoints["token_endpoint"],
                {
                    "grant_type": DEVICE_GRANT,
                    "device_code": device.device_code,
                    "client_id": self._client_id,
                },
            )
            if status == 200 and body.get("access_token"):
                return str(body["access_token"])
            error = str(body.get("error", f"http_{status}"))
            if error == "authorization_pending":
                continue
            if error == "slow_down":
                interval += 5
                continue
            if error in ("invalid_grant", "expired_token"):
                raise AuthError("expired_token", str(body.get("error_description", "")))
            raise AuthError(error, str(body.get("error_description", "")))

    async def list_teams(self, access_token: str) -> list[Team]:
        """Teams entitled to this product, from TeamsService/ListMyTeams.

        Falls back to the token's teams claim if the call fails.
        """
        try:
            async with self._session.post(
                f"{self._issuer}/xeonr.auth.api.v1.TeamsService/ListMyTeams",
                json={},
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json",
                    "Connect-Protocol-Version": "1",
                },
                timeout=aiohttp.ClientTimeout(total=CONNECT_TIMEOUT),
            ) as resp:
                status = resp.status
                body = await resp.json(content_type=None)
        except (TimeoutError, aiohttp.ClientError, ValueError) as err:
            _LOGGER.debug("ListMyTeams failed, using the token claim: %s", err)
            return teams_from_token(access_token)
        if status != 200 or not isinstance(body, dict):
            _LOGGER.debug("ListMyTeams returned %s, using the token claim", status)
            return teams_from_token(access_token)

        teams: list[Team] = []
        for item in body.get("teams", []) or []:
            if item.get("entitled") is False:
                continue
            team = item.get("team") or {}
            urn = team.get("teamId") or team.get("team_id") or ""
            if not urn:
                continue
            teams.append(
                Team(
                    urn=urn,
                    slug=team.get("slug", ""),
                    name=team.get("name", ""),
                    application_roles=tuple(
                        item.get("applicationRoles")
                        or item.get("application_roles")
                        or ()
                    ),
                )
            )
        return teams


def teams_from_token(access_token: str) -> list[Team]:
    """Read the urn:xeonr:auth:teams claim without verifying the token.

    Only used to offer choices; the API verifies the token and the role.
    """
    try:
        payload = access_token.split(".")[1]
        claims = json.loads(
            base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
        )
    except (IndexError, ValueError):
        return []
    teams = []
    for item in claims.get(TEAMS_CLAIM, []) or []:
        if isinstance(item, dict) and item.get("id"):
            teams.append(
                Team(
                    urn=item["id"],
                    slug=item.get("slug", ""),
                    name=item.get("slug", ""),
                    application_roles=tuple(item.get("roles") or ()),
                )
            )
    return teams
