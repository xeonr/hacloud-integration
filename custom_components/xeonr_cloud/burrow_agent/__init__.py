"""Burrow tunnel agent for asyncio.

Speaks Burrow's agent protocol — a yamux session over a WebSocket to the
gateway, JSON control messages, a JSON preamble on each data stream — and hands
each tunnelled HTTP stream to a handler in this process. See README.md.

Every import inside this package is relative, so it can be vendored under any
parent package unchanged.
"""

from .agent import (
    Agent,
    AgentError,
    AgentRefusedError,
    StateCallback,
    StreamHandler,
    UnsupportedProtocolError,
    __version__,
)
from .wire import PROTOCOL_VERSION, Preamble, ProtocolError, Registered
from .yamux import Stream

__all__ = [
    "PROTOCOL_VERSION",
    "Agent",
    "AgentError",
    "AgentRefusedError",
    "Preamble",
    "ProtocolError",
    "Registered",
    "StateCallback",
    "Stream",
    "StreamHandler",
    "UnsupportedProtocolError",
    "__version__",
]
