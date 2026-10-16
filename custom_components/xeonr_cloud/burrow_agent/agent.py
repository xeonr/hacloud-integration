"""The agent: keeps one Burrow tunnel registered and serves what the gateway sends.

A port of the HTTP half of ``burrow-cli/pkg/agent/agent.go``, with two
deliberate differences:

- **drain overlaps.** On ``drain`` the Go agent drops its session and redials
  after a backoff; this one dials a replacement at once and keeps serving on
  the old session until the gateway closes it, so a gateway rollout is a blip
  rather than an outage;
- **backoff resets.** After a session that stayed up, the next reconnect
  starts from the minimum delay again instead of wherever earlier failures
  left it.

Data streams are not proxied anywhere: each is handed, with its preamble, to a
:data:`StreamHandler` running in this process. :class:`burrow_agent.aiohttp.
AiohttpStreamHandler` is the one that feeds an aiohttp application.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
from collections.abc import Awaitable, Callable
from typing import Final
from urllib.parse import urlsplit, urlunsplit

import aiohttp

from . import wire, yamux
from .wire import Preamble, Registered

_LOGGER = logging.getLogger(__name__)

__version__: Final = "0.1.0"

#: Serves one data stream. Called with the stream positioned just past the
#: preamble line. The agent half-closes (FIN) the stream when it returns and
#: resets it if it raises.
type StreamHandler = Callable[[yamux.Stream, Preamble], Awaitable[None]]

#: Called with the new value of :attr:`Agent.connected` whenever it or
#: :attr:`Agent.public_url` changes. Runs in the event loop; must not block.
type StateCallback = Callable[[bool], None]


class AgentError(Exception):
    """Base class for this library's errors."""


class AgentRefusedError(AgentError):
    """The gateway refused the tunnel in a way retrying would only repeat.

    ``code`` is one of :data:`burrow_agent.wire.FATAL_CODES`, for example
    ``unauthenticated`` for a revoked credential or ``name_taken`` for a name
    the credential may not use.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message or code)
        self.code = code
        self.message = message


class UnsupportedProtocolError(AgentError, ValueError):
    """This library serves HTTP tunnels only; TCP and UDP are not implemented."""


class _WebSocketConn:
    """A WebSocket as a byte pipe, as Go's ``websocket.NetConn`` presents it.

    Each write is one binary message; reads concatenate binary messages. Frame
    boundaries mean nothing to yamux, which does its own framing.
    """

    def __init__(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        self._ws = ws

    async def read(self) -> bytes:
        while True:
            msg = await self._ws.receive()
            if msg.type is aiohttp.WSMsgType.BINARY:
                data: bytes = msg.data
                if data:
                    return data
                continue
            if msg.type in (aiohttp.WSMsgType.PING, aiohttp.WSMsgType.PONG):
                continue
            if msg.type is aiohttp.WSMsgType.TEXT:
                raise wire.ProtocolError("text frame on the agent WebSocket")
            # CLOSE, CLOSING, CLOSED, ERROR: the pipe is gone.
            return b""

    async def write(self, data: bytes) -> None:
        await self._ws.send_bytes(data)

    async def close(self) -> None:
        await self._ws.close()


class _Backoff:
    """Exponential backoff with full jitter in its upper half."""

    def __init__(self, minimum: float, maximum: float) -> None:
        self._min = minimum
        self._max = maximum
        self._next = minimum

    def next(self) -> float:
        delay = self._next
        self._next = min(self._next * 2, self._max)
        return random.uniform(delay / 2, delay)

    def reset(self) -> None:
        self._next = self._min


class _Connection:
    """One WebSocket + yamux session to a gateway, registered or about to be."""

    def __init__(
        self, agent: Agent, ws: aiohttp.ClientWebSocketResponse, *, loop: asyncio.AbstractEventLoop
    ) -> None:
        self._agent = agent
        self._loop = loop
        self.mux = yamux.Session(_WebSocketConn(ws), client=True)
        self.mux.start()
        self.drained = asyncio.Event()
        self.registered: Registered | None = None
        self.started_at = loop.time()
        self._control: yamux.Stream | None = None
        self._tasks: list[asyncio.Task[None]] = []
        self._handlers: set[asyncio.Task[None]] = set()
        self._last_pong = loop.time()
        self._closer: asyncio.Task[None] | None = None

    def close_soon(self) -> None:
        """Close from inside one of this connection's own tasks."""
        if self._closer is None:
            self._closer = self._loop.create_task(self.close(), name="burrow-close")

    async def register(self, reg: wire.Register, timeout: float) -> Registered:
        async with asyncio.timeout(timeout):
            self._control = control = await self.mux.open()
            await control.write(reg.encode())
            try:
                line = await control.readuntil(b"\n", wire.MAX_LINE)
            except EOFError:
                raise AgentError(
                    "the gateway closed the session before answering the registration"
                ) from None
        msg = wire.decode_line(line)
        kind = msg.get("type")
        if kind == wire.TYPE_ERROR:
            err = wire.Error.from_message(msg)
            if err.code in wire.FATAL_CODES:
                raise AgentRefusedError(err.code, err.message)
            raise AgentError(f"the gateway refused the tunnel: {err.message or err.code}")
        if kind != wire.TYPE_REGISTERED:
            raise wire.ProtocolError(f"unexpected {kind!r} from the gateway")
        self.registered = Registered.from_message(msg)
        self._last_pong = self._loop.time()
        return self.registered

    def serve(self) -> None:
        self._tasks = [
            asyncio.create_task(self._accept_loop(), name="burrow-accept"),
            asyncio.create_task(self._control_loop(), name="burrow-control"),
            asyncio.create_task(self._ping_loop(), name="burrow-ping"),
        ]

    async def retire(self, grace: float) -> None:
        """Let a drained session finish what it is doing, then close it.

        The gateway closes it itself once its drain grace is up; this only
        bounds how long we wait for that.
        """
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(grace):
                await self.mux.wait_closed()
                return
        with contextlib.suppress(yamux.YamuxError):
            await self.mux.go_away()
        exclude = [self._control] if self._control is not None else []
        await self.mux.wait_streams_idle(10.0, exclude=exclude)
        await self.close()

    async def close(self) -> None:
        current = asyncio.current_task()
        for task in (*self._tasks, *self._handlers):
            if task is not current:
                task.cancel()
        await self.mux.close()
        pending = [t for t in (*self._tasks, *self._handlers) if t is not current]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    async def _accept_loop(self) -> None:
        try:
            while True:
                stream = await self.mux.accept()
                task = asyncio.create_task(
                    self._serve_stream(stream), name=f"burrow-stream-{stream.id}"
                )
                self._handlers.add(task)
                task.add_done_callback(self._handlers.discard)
        except yamux.SessionClosedError:
            pass

    async def _serve_stream(self, stream: yamux.Stream) -> None:
        try:
            async with asyncio.timeout(30.0):
                line = await stream.readuntil(b"\n", wire.MAX_LINE)
            preamble = Preamble.decode(line)
        except (EOFError, TimeoutError, ValueError, yamux.YamuxError) as err:
            _LOGGER.debug("Burrow stream %d ended before its preamble: %r", stream.id, err)
            stream.reset()
            return
        if preamble.kind != wire.PROTO_HTTP:
            _LOGGER.error(
                "Burrow gateway sent a %r stream; this agent serves HTTP tunnels only",
                preamble.kind,
            )
            stream.reset()
            return
        try:
            await self._agent._handler(stream, preamble)
        except asyncio.CancelledError:
            stream.reset()
            raise
        except (yamux.YamuxError, ConnectionError):
            # The gateway or the session went away mid-request: routine.
            stream.reset()
        except Exception:
            _LOGGER.exception("Burrow stream handler failed for request %s", preamble.id)
            stream.reset()
        else:
            await stream.close()

    async def _control_loop(self) -> None:
        control = self._control
        assert control is not None
        try:
            while True:
                msg = wire.decode_line(await control.readuntil(b"\n", wire.MAX_LINE))
                kind = msg.get("type")
                if kind == wire.TYPE_PONG:
                    self._last_pong = self._loop.time()
                elif kind == wire.TYPE_DRAIN:
                    _LOGGER.info("Burrow gateway is restarting; moving the tunnel")
                    self.drained.set()
                elif kind == wire.TYPE_PING:
                    await control.write(wire.control(wire.TYPE_PONG))
                # Anything else is newer than this agent; ignore it.
        except (EOFError, ValueError, wire.ProtocolError, yamux.YamuxError) as err:
            _LOGGER.debug("Burrow control stream ended: %r", err)
        # The control stream is the session: without it nothing can register,
        # ping or drain, so close the lot (as the Go agent's cancelSession does).
        self.close_soon()

    async def _ping_loop(self) -> None:
        control = self._control
        assert control is not None
        interval = self._agent._ping_interval
        try:
            while True:
                await asyncio.sleep(interval)
                if self._loop.time() - self._last_pong > interval + self._agent._ping_timeout:
                    _LOGGER.info("Burrow gateway stopped answering pings; reconnecting")
                    break
                await control.write(wire.control(wire.TYPE_PING))
        except yamux.YamuxError:
            pass
        self.close_soon()


class Agent:
    """Keeps one HTTP tunnel registered with a Burrow gateway.

    :param agent_url: the gateway's agent endpoint,
        ``wss://connect.<domain>/v1/agent``. A URL with no path gets
        ``/v1/agent`` appended.
    :param credential: the bearer credential (for Home Assistant cloud, the
        reservation-scoped agent credential).
    :param name: the reserved name to register. ``""`` asks for a generated
        slug, which only an ephemeral tunnel wants.
    :param handler: serves each data stream; see :data:`StreamHandler`.
    :param label: distinguishes two tunnels of one target from one identity.
    :param team: team URN or slug; empty picks the credential's only team.
    :param target: what is exposed, for display and the lease fingerprint.
    :param proto: must be ``"http"``.
    :param session: an aiohttp session to dial with (Home Assistant's shared
        one, typically). One is created and closed by the agent if omitted.
    """

    def __init__(
        self,
        agent_url: str,
        credential: str,
        name: str,
        handler: StreamHandler,
        *,
        label: str | None = None,
        team: str | None = None,
        target: str = "in-process",
        proto: str = wire.PROTO_HTTP,
        policy: str | None = None,
        agent_version: str | None = None,
        session: aiohttp.ClientSession | None = None,
        connect_timeout: float = 20.0,
        ping_interval: float = 30.0,
        ping_timeout: float = 30.0,
        backoff_min: float = 1.0,
        backoff_max: float = 30.0,
        drain_grace: float = 30.0,
    ) -> None:
        if proto in (wire.PROTO_TCP, wire.PROTO_UDP):
            raise UnsupportedProtocolError(
                f"{proto} tunnels are not implemented by burrow-agent-py; only http is"
            )
        if proto != wire.PROTO_HTTP:
            raise UnsupportedProtocolError(f"unknown tunnel protocol {proto!r}")
        if not credential:
            raise ValueError("a credential is required")
        self._url = _agent_endpoint(agent_url)
        self._credential = credential
        self._handler = handler
        self._register = wire.Register(
            proto=proto,
            target=target,
            name=name,
            team=team or "",
            label=label or "",
            policy=policy or "",
            agent_version=agent_version or f"burrow-agent-py/{__version__}",
        )
        self._session = session
        self._connect_timeout = connect_timeout
        self._ping_interval = ping_interval
        self._ping_timeout = ping_timeout
        self._backoff = _Backoff(backoff_min, backoff_max)
        self._drain_grace = drain_grace

        self._callbacks: list[StateCallback] = []
        self._live: list[_Connection] = []
        self._retiring: set[asyncio.Task[None]] = set()
        self._registration: Registered | None = None
        self._reported: tuple[bool, str | None] = (False, None)
        self._stop: asyncio.Event | None = None
        self._run_task: asyncio.Task[None] | None = None
        self._finished: asyncio.Event | None = None

    # -- public state ----------------------------------------------------------

    @property
    def connected(self) -> bool:
        """True while at least one registered session to a gateway is up."""
        return bool(self._live)

    @property
    def public_url(self) -> str | None:
        """The tunnel's public URL from the latest registration.

        Kept across disconnects: a reservation's URL does not change, and the
        last one known is what should be shown while reconnecting.
        """
        return self._registration.url if self._registration else None

    @property
    def registration(self) -> Registered | None:
        """The gateway's full answer to the latest registration."""
        return self._registration

    def on_state_change(self, cb: StateCallback) -> Callable[[], None]:
        """Call ``cb(connected)`` whenever :attr:`connected` or :attr:`public_url`
        changes. Returns a function that unsubscribes it."""
        self._callbacks.append(cb)

        def remove() -> None:
            with contextlib.suppress(ValueError):
                self._callbacks.remove(cb)

        return remove

    # -- lifecycle -------------------------------------------------------------

    async def run(self) -> None:
        """Keep the tunnel up until :meth:`stop` is called.

        Reconnects with jittered backoff on anything that is not a refusal.
        Returns ``None`` after :meth:`stop`; raises :class:`AgentRefusedError`
        when the gateway refuses the credential or the name.
        """
        if self._run_task is not None:
            raise RuntimeError("Agent.run() is already running")
        self._run_task = asyncio.current_task()
        self._stop = asyncio.Event()
        self._finished = asyncio.Event()
        own_session = self._session is None
        session = self._session or aiohttp.ClientSession()
        try:
            await self._run(session)
        finally:
            for task in list(self._retiring):
                task.cancel()
            await asyncio.gather(*self._retiring, return_exceptions=True)
            await asyncio.gather(*(c.close() for c in list(self._live)), return_exceptions=True)
            self._live.clear()
            self._notify()
            if own_session:
                await session.close()
            self._run_task = None
            self._finished.set()

    async def stop(self) -> None:
        """Close every session and wait for :meth:`run` to return."""
        if self._run_task is None or self._stop is None or self._finished is None:
            return
        self._stop.set()
        if self._run_task is not asyncio.current_task():
            await self._finished.wait()

    async def _run(self, session: aiohttp.ClientSession) -> None:
        assert self._stop is not None
        loop = asyncio.get_running_loop()
        while not self._stop.is_set():
            try:
                conn = await self._dial(session, loop)
            except AgentRefusedError:
                raise
            except (
                aiohttp.ClientError,
                OSError,
                TimeoutError,
                AgentError,
                wire.ProtocolError,
                yamux.YamuxError,
                ValueError,  # a registration answer past wire.MAX_LINE
            ) as err:
                delay = self._backoff.next()
                _LOGGER.warning(
                    "Burrow tunnel could not connect (%s); retrying in %.1fs",
                    _describe(err),
                    delay,
                )
                await self._sleep(delay)
                continue
            if self._stop.is_set():
                await conn.close()
                return

            self._adopt(conn)
            outcome = await self._wait(conn)
            if outcome == "stop":
                return
            if outcome == "drain":
                # Dial the replacement now; this session keeps serving until
                # the gateway closes it or the replacement is up.
                self._retire(conn)
                continue
            lived = loop.time() - conn.started_at
            if lived >= 60.0:
                self._backoff.reset()
            delay = self._backoff.next()
            _LOGGER.warning(
                "Burrow tunnel session ended (%s); reconnecting in %.1fs",
                _describe(conn.mux.close_reason) if conn.mux.close_reason else "closed",
                delay,
            )
            await self._sleep(delay)

    async def _dial(
        self, session: aiohttp.ClientSession, loop: asyncio.AbstractEventLoop
    ) -> _Connection:
        async with asyncio.timeout(self._connect_timeout):
            ws = await session.ws_connect(
                self._url,
                headers={"Authorization": f"Bearer {self._credential}"},
                autoping=True,
                heartbeat=None,
                # A dead link must not hold stop() for the default 10s close
                # handshake.
                timeout=aiohttp.ClientWSTimeout(ws_receive=None, ws_close=2.0),
                compress=0,
                max_msg_size=0,
            )
        conn = _Connection(self, ws, loop=loop)
        try:
            reg = await conn.register(self._register, self._connect_timeout)
        except BaseException:
            await conn.close()
            raise
        _LOGGER.info("Burrow tunnel up at %s", reg.url)
        return conn

    def _adopt(self, conn: _Connection) -> None:
        self._registration = conn.registered
        self._live.append(conn)
        conn.mux.add_close_callback(lambda: self._dropped(conn))
        conn.serve()
        self._notify()

    def _dropped(self, conn: _Connection) -> None:
        if conn in self._live:
            self._live.remove(conn)
            self._notify()

    def _retire(self, conn: _Connection) -> None:
        # Not closed when the replacement comes up: on a multi-replica gateway
        # the draining replica still routes requests that reach it into this
        # session until its own grace runs out.
        task = asyncio.create_task(conn.retire(self._drain_grace), name="burrow-retire")
        self._retiring.add(task)
        task.add_done_callback(self._retiring.discard)

    async def _wait(self, conn: _Connection) -> str:
        assert self._stop is not None
        waits = {
            asyncio.ensure_future(self._stop.wait()): "stop",
            asyncio.ensure_future(conn.drained.wait()): "drain",
            asyncio.ensure_future(conn.mux.wait_closed()): "closed",
        }
        try:
            done, _ = await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for fut in waits:
                fut.cancel()
        for outcome in ("stop", "closed", "drain"):
            if any(waits[f] == outcome for f in done):
                return outcome
        return "closed"

    async def _sleep(self, delay: float) -> None:
        assert self._stop is not None
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(delay):
                await self._stop.wait()

    def _notify(self) -> None:
        state = (self.connected, self.public_url)
        if state == self._reported:
            return
        self._reported = state
        for cb in list(self._callbacks):
            try:
                cb(state[0])
            except Exception:
                _LOGGER.exception("Burrow state callback failed")


def _agent_endpoint(url: str) -> str:
    parts = urlsplit(url)
    if parts.scheme not in ("ws", "wss", "http", "https"):
        raise ValueError(f"agent_url must be a ws:// or wss:// URL, not {url!r}")
    scheme = {"http": "ws", "https": "wss"}.get(parts.scheme, parts.scheme)
    path = parts.path if parts.path not in ("", "/") else wire.AGENT_PATH
    return urlunsplit((scheme, parts.netloc, path, parts.query, ""))


def _describe(err: BaseException) -> str:
    if isinstance(err, aiohttp.WSServerHandshakeError):
        return f"gateway answered {err.status} {err.message}".strip()
    return str(err) or type(err).__name__
