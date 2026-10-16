"""The agent <-> gateway protocol: control messages and the data-stream preamble.

A port of ``burrow-cli/pkg/wire/wire.go``. The transport underneath is one
WebSocket to ``wss://connect.<domain>/v1/agent`` carrying a yamux session; the
agent is the yamux client and opens the first stream, the *control* stream,
which carries newline-delimited JSON. Every other stream is opened by the
gateway and starts with one JSON :class:`Preamble` line.

Field names and spellings here are the wire format and must match the Go
structs exactly — the gateway decodes them with ``encoding/json``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final

#: Bumped when a change would confuse an older agent. The gateway compares it
#: for equality and refuses anything else with ``CODE_UNSUPPORTED``.
PROTOCOL_VERSION: Final = 1

#: Where an agent dials, on the gateway's connect host.
AGENT_PATH: Final = "/v1/agent"

TYPE_REGISTER: Final = "register"
TYPE_REGISTERED: Final = "registered"
TYPE_ERROR: Final = "error"
TYPE_PING: Final = "ping"
TYPE_PONG: Final = "pong"
#: The gateway is going away (a rolling restart): reconnect now, while the
#: current session still serves.
TYPE_DRAIN: Final = "drain"

CODE_UNAUTHENTICATED: Final = "unauthenticated"
CODE_FORBIDDEN: Final = "forbidden"
CODE_NAME_TAKEN: Final = "name_taken"
CODE_NO_TEAM: Final = "no_team"
CODE_QUOTA: Final = "quota"
CODE_UNSUPPORTED: Final = "unsupported"
CODE_INTERNAL: Final = "internal"

#: Refusals that retrying would only repeat. The same set the Go agent treats
#: as fatal (``pkg/agent/agent.go``); anything else is retried with backoff.
FATAL_CODES: Final = frozenset(
    {
        CODE_UNAUTHENTICATED,
        CODE_FORBIDDEN,
        CODE_NAME_TAKEN,
        CODE_NO_TEAM,
        CODE_QUOTA,
        CODE_UNSUPPORTED,
    }
)

#: Protocols the wire knows. This library serves only ``http``.
PROTO_HTTP: Final = "http"
PROTO_TCP: Final = "tcp"
PROTO_UDP: Final = "udp"

#: A control line or preamble longer than this is a broken peer, not a message.
MAX_LINE: Final = 64 * 1024


class ProtocolError(Exception):
    """The gateway sent something this agent cannot make sense of."""


@dataclass(frozen=True, slots=True)
class Register:
    """Asks for a tunnel. The first message the agent sends on the control stream."""

    proto: str
    target: str
    name: str = ""
    team: str = ""
    label: str = ""
    policy: str = ""
    agent_version: str = ""

    def encode(self) -> bytes:
        # Go's omitempty fields are dropped when empty; type, version, proto
        # and target are always present.
        msg: dict[str, Any] = {
            "type": TYPE_REGISTER,
            "version": PROTOCOL_VERSION,
            "proto": self.proto,
        }
        if self.name:
            msg["name"] = self.name
        if self.team:
            msg["team"] = self.team
        msg["target"] = self.target
        if self.label:
            msg["label"] = self.label
        if self.policy:
            msg["policy"] = self.policy
        if self.agent_version:
            msg["agent_version"] = self.agent_version
        return encode_line(msg)


@dataclass(frozen=True, slots=True)
class Registered:
    """The gateway's answer to :class:`Register`: what is now being served."""

    url: str
    name: str
    team: str
    host: str = ""
    tcp_port: int = 0
    team_slug: str = ""
    access: str = ""
    lease_expires: datetime | None = None
    inspect: str = ""
    raw: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)

    @classmethod
    def from_message(cls, msg: dict[str, Any]) -> Registered:
        return cls(
            url=_str(msg, "url"),
            name=_str(msg, "name"),
            team=_str(msg, "team"),
            host=_str(msg, "host"),
            tcp_port=int(msg.get("tcp_port") or 0),
            team_slug=_str(msg, "team_slug"),
            access=_str(msg, "access"),
            lease_expires=_parse_time(msg.get("lease_expires")),
            inspect=_str(msg, "inspect"),
            raw=msg,
        )


@dataclass(frozen=True, slots=True)
class Error:
    """A refusal with a machine-readable code."""

    code: str
    message: str

    @classmethod
    def from_message(cls, msg: dict[str, Any]) -> Error:
        return cls(code=_str(msg, "code"), message=_str(msg, "message"))


@dataclass(frozen=True, slots=True)
class Preamble:
    """Opens every data stream the gateway pushes to the agent.

    ``remote`` is the public client's address, already real-IP resolved by the
    gateway. ``host`` is the hostname the request arrived on; it may be echoed
    but must not be trusted for routing — the gateway has already routed.
    ``id`` correlates the stream with the gateway's traffic record.
    """

    kind: str
    remote: str
    host: str = ""
    id: str = ""

    @classmethod
    def decode(cls, line: bytes) -> Preamble:
        msg = decode_line(line)
        kind = msg.get("kind")
        if not isinstance(kind, str) or not kind:
            raise ProtocolError("preamble has no kind")
        return cls(
            kind=kind,
            remote=_str(msg, "remote"),
            host=_str(msg, "host"),
            id=_str(msg, "id"),
        )

    def encode(self) -> bytes:
        msg: dict[str, Any] = {"kind": self.kind, "remote": self.remote}
        if self.host:
            msg["host"] = self.host
        if self.id:
            msg["id"] = self.id
        return encode_line(msg)


def encode_line(msg: dict[str, Any]) -> bytes:
    """One JSON value and a newline, as Go's ``json.Encoder.Encode`` writes it."""
    return json.dumps(msg, separators=(",", ":"), ensure_ascii=False).encode() + b"\n"


def decode_line(line: bytes) -> dict[str, Any]:
    try:
        msg = json.loads(line)
    except (UnicodeDecodeError, json.JSONDecodeError) as err:
        raise ProtocolError(f"malformed JSON from the gateway: {err}") from err
    if not isinstance(msg, dict):
        raise ProtocolError("expected a JSON object from the gateway")
    return msg


def control(kind: str) -> bytes:
    """A bare ``{"type": ...}`` control message, e.g. ``ping``."""
    return encode_line({"type": kind})


def _str(msg: dict[str, Any], key: str) -> str:
    v = msg.get(key)
    return v if isinstance(v, str) else ""


def _parse_time(v: object) -> datetime | None:
    # Go marshals a zero time.Time (omitempty does not apply to structs) as
    # 0001-01-01T00:00:00Z, which means "none".
    if not isinstance(v, str) or not v or v.startswith("0001-01-01"):
        return None
    try:
        return datetime.fromisoformat(v)
    except ValueError:
        return None
