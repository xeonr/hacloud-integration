"""Serve Burrow streams with an aiohttp application already running in-process.

:class:`AiohttpStreamHandler` takes an aiohttp ``AppRunner`` (Home Assistant's
own) and, for each tunnelled HTTP stream, attaches a protocol from the runner's
server to a transport backed by the stream — the approach of
``SniTunClientAioHttp`` in NabuCasa/snitun. Nothing listens on a socket and
nothing is proxied to localhost, which matters (DESIGN §3.2 of Home Assistant
cloud):

- **The peer address is the public client's.** The transport's
  ``get_extra_info("peername")`` is the preamble's ``remote``, which the gateway
  has already real-IP resolved, so ``request.remote`` is the caller on the
  internet. Proxying to ``127.0.0.1`` would make every internet request look
  local, which Home Assistant's ``trusted_networks`` provider, local-only users
  and IP bans would all believe.
- **Forwarded headers are stripped.** The gateway's reverse proxy sets
  ``X-Forwarded-For``/``-Proto``/``-Host`` on every request
  (``burrow/pkg/gateway/gateway.go``, ``SetXForwarded``), and Home Assistant
  answers 400 to a forwarded header from a proxy it was not told to trust. The
  preamble already carries the real address, so nothing is lost by removing
  ``Forwarded`` and every ``X-Forwarded-*`` header before the app sees them.

The stripping applies to the request head that opens the stream. That is the
only HTTP head a stream carries: the gateway sends one request per stream
(``DisableKeepAlives``) and builds that request itself, so a public client
cannot smuggle a second one onto it; after a protocol upgrade (Home
Assistant's WebSocket API) the bytes are WebSocket frames, not HTTP.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import logging
import ssl
from typing import Any, Final

from aiohttp import web

from . import yamux
from .wire import Preamble

_LOGGER = logging.getLogger(__name__)

#: The request head is buffered whole so its headers can be filtered. aiohttp
#: refuses far smaller heads itself; this only bounds the buffer.
MAX_HEAD: Final = 128 * 1024
_READ_CHUNK: Final = 64 * 1024
_HIGH_WATER: Final = 256 * 1024
_LOW_WATER: Final = 64 * 1024

_TOO_LARGE: Final = (
    b"HTTP/1.1 431 Request Header Fields Too Large\r\n"
    b"Content-Length: 0\r\nConnection: close\r\n\r\n"
)


def is_forwarded_header(name: bytes) -> bool:
    """``Forwarded`` and every ``X-Forwarded-*`` header, case-insensitively."""
    name = name.strip().lower()
    return name == b"forwarded" or name.startswith(b"x-forwarded-")


def strip_forwarded_headers(head: bytes) -> bytes:
    """Remove forwarded headers from one HTTP/1.x request head.

    ``head`` is the request line and headers through the blank line that ends
    them. Obsolete line folding (a continuation line starting with space or
    tab) belongs to the header before it and is dropped with it.
    """
    lines = head.split(b"\r\n")
    out = [lines[0]]
    dropping = False
    for line in lines[1:]:
        if not line:
            out.append(line)
            dropping = False
            continue
        if line[:1] in (b" ", b"\t"):
            if not dropping:
                out.append(line)
            continue
        dropping = is_forwarded_header(line.split(b":", 1)[0])
        if not dropping:
            out.append(line)
    return b"\r\n".join(out)


def peername_for(remote: str) -> tuple[str, int] | tuple[str, int, int, int]:
    """The ``peername`` a socket from ``remote`` would report.

    The preamble's ``remote`` is a bare IP (the gateway's ``ClientIP``), but a
    ``host:port`` form is accepted too. Anything that is not an IP address is
    refused rather than passed through: an unparseable peer must never reach
    an application that makes trust decisions on it.
    """
    candidate = remote.strip()
    try:
        ip = ipaddress.ip_address(candidate)
    except ValueError:
        host, sep, port = candidate.rpartition(":")
        if not sep or not port.isdigit():
            raise ValueError(f"preamble remote {remote!r} is not an IP address") from None
        ip = ipaddress.ip_address(host.strip("[]"))
    if isinstance(ip, ipaddress.IPv6Address):
        return (str(ip), 0, 0, 0)
    return (str(ip), 0)


class AiohttpStreamHandler:
    """A :data:`burrow_agent.StreamHandler` that feeds an aiohttp runner.

    :param runner: a set-up ``web.AppRunner`` (or any ``BaseRunner``). It need
        not have any site: only its server is used.
    :param secure: whether the public side is HTTPS, which it is behind
        Burrow's edge. aiohttp derives ``request.secure`` and
        ``request.scheme`` from whether the transport has an SSL context, so
        when this is true the transport reports a placeholder one: TLS was
        terminated before the tunnel, but the client did use it.
    """

    def __init__(self, runner: web.BaseRunner, *, secure: bool = True) -> None:
        self._runner = runner
        self._sslcontext = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER) if secure else None

    async def __call__(self, stream: yamux.Stream, preamble: Preamble) -> None:
        server = self._runner.server
        if server is None:
            raise RuntimeError("the aiohttp runner is not set up")
        peername = peername_for(preamble.remote)
        try:
            head = await stream.readuntil(b"\r\n\r\n", MAX_HEAD)
        except EOFError:
            return
        except ValueError:
            await stream.write(_TOO_LARGE)
            return
        transport = StreamTransport(
            stream,
            server(),
            extra={"peername": peername, "sockname": None, "sslcontext": self._sslcontext},
        )
        await transport.run(self.filter_head(head, preamble))

    def filter_head(self, head: bytes, preamble: Preamble) -> bytes:
        """Rewrite the request head before the app sees it.

        Strips the forwarded headers (see the module docstring). Override to
        remove more; call this implementation to keep the stripping.
        """
        return strip_forwarded_headers(head)


class StreamTransport(asyncio.Transport):
    """An ``asyncio.Transport`` whose bytes travel over a yamux stream.

    Writes are buffered and pumped into the stream by a task, with
    ``pause_writing``/``resume_writing`` applied to the protocol around a high
    water mark, so a slow public client back-pressures the application
    through yamux's flow control rather than through an unbounded buffer.
    """

    def __init__(
        self,
        stream: yamux.Stream,
        protocol: asyncio.Protocol,
        *,
        extra: dict[str, Any],
    ) -> None:
        super().__init__(extra)
        self._stream = stream
        self._protocol = protocol
        self._buffer = bytearray()
        self._inflight = 0
        self._wakeup = asyncio.Event()
        self._reading = asyncio.Event()
        self._reading.set()
        self._closing = False
        self._writing_paused = False
        self._high = _HIGH_WATER
        self._low = _LOW_WATER
        self._lost = False

    async def run(self, first: bytes = b"") -> None:
        """Attach the protocol and pump bytes until either side is done."""
        self._protocol.connection_made(self)
        reader = asyncio.create_task(self._read_loop(first), name="burrow-http-read")
        exc: Exception | None = None
        try:
            await self._write_loop()
        except asyncio.CancelledError:
            exc = ConnectionResetError("the tunnel session closed")
            raise
        except (yamux.YamuxError, ConnectionError) as err:
            exc = err
            raise
        finally:
            self._closing = True
            reader.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await reader
            self._connection_lost(exc)

    async def _read_loop(self, first: bytes) -> None:
        try:
            if first:
                self._protocol.data_received(first)
            while not self._closing:
                await self._reading.wait()
                data = await self._stream.read(_READ_CHUNK)
                if not data:
                    break
                self._protocol.data_received(data)
        except (yamux.YamuxError, ConnectionError):
            pass
        if not self._closing:
            # The gateway sends FIN only once it is finished with the stream —
            # the response is read, or the public client went away (it never
            # half-closes to wait for a reply). Writing on would only fill a
            # window nobody reads, so end the connection.
            self._protocol.eof_received()
            self.abort()

    async def _write_loop(self) -> None:
        while True:
            if self._buffer:
                data = bytes(self._buffer)
                self._buffer.clear()
                self._inflight = len(data)
                try:
                    await self._stream.write(data)
                finally:
                    self._inflight = 0
                self._maybe_resume_writing()
                continue
            if self._closing:
                return
            self._wakeup.clear()
            await self._wakeup.wait()

    def _connection_lost(self, exc: Exception | None) -> None:
        if self._lost:
            return
        self._lost = True
        self._protocol.connection_lost(exc)

    @property
    def _sendfile_compatible(self) -> Any:
        """Make ``loop.sendfile()`` say "not implemented" rather than fail.

        aiohttp's ``FileResponse`` (every static file Home Assistant serves)
        calls ``loop.sendfile(transport, …)`` and falls back to plain writes
        only on ``NotImplementedError``. asyncio reads this attribute to pick a
        mode and raises ``RuntimeError`` for a transport without one — which
        broke every static file through the tunnel in 0.1.0. A stream has no
        file descriptor to sendfile into, and asyncio's own fallback mode
        needs its private ``_FlowControlMixin``, so the answer is the one
        uvloop gives: not implemented.
        """
        raise NotImplementedError("sendfile is not supported on a tunnelled stream")

    # -- asyncio.Transport -----------------------------------------------------

    def write(self, data: bytes | bytearray | memoryview) -> None:
        if self._closing or not data:
            return
        self._buffer += data
        self._wakeup.set()
        if not self._writing_paused and self.get_write_buffer_size() > self._high:
            self._writing_paused = True
            self._protocol.pause_writing()

    def writelines(self, list_of_data: Any) -> None:
        self.write(b"".join(list_of_data))

    def _maybe_resume_writing(self) -> None:
        if self._writing_paused and self.get_write_buffer_size() <= self._low:
            self._writing_paused = False
            self._protocol.resume_writing()

    def can_write_eof(self) -> bool:
        return False

    def write_eof(self) -> None:
        raise NotImplementedError("a tunnelled HTTP stream cannot half-close")

    def is_closing(self) -> bool:
        return self._closing

    def close(self) -> None:
        """Flush what is buffered, then end the stream."""
        self._closing = True
        self._wakeup.set()

    def abort(self) -> None:
        """End the stream now, discarding anything unsent."""
        self._closing = True
        self._buffer.clear()
        self._wakeup.set()

    def get_write_buffer_size(self) -> int:
        return len(self._buffer) + self._inflight

    def get_write_buffer_limits(self) -> tuple[int, int]:
        return (self._low, self._high)

    def set_write_buffer_limits(self, high: int | None = None, low: int | None = None) -> None:
        self._high = _HIGH_WATER if high is None else high
        self._low = (self._high // 4) if low is None else low

    def pause_reading(self) -> None:
        self._reading.clear()

    def resume_reading(self) -> None:
        self._reading.set()

    def is_reading(self) -> bool:
        return self._reading.is_set() and not self._closing

    def set_protocol(self, protocol: asyncio.BaseProtocol) -> None:
        assert isinstance(protocol, asyncio.Protocol)
        self._protocol = protocol

    def get_protocol(self) -> asyncio.BaseProtocol:
        return self._protocol
