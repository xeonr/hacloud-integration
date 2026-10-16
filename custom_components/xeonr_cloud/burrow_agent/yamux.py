"""A yamux session for asyncio, interoperable with hashicorp/yamux.

Python has no maintained yamux implementation, so this is a port of the parts
of hashicorp/yamux v0.1.2 (``session.go``, ``stream.go``, ``const.go``) that an
agent needs, kept deliberately close to the Go so the two can be read side by
side. The framing is the 12-byte header from yamux's ``spec.md``::

    version:u8 = 0 | type:u8 | flags:u16 | stream_id:u32 | length:u32   (big-endian)

Types are Data (0), WindowUpdate (1), Ping (2) and GoAway (3); flags are SYN
(1), ACK (2), FIN (4) and RST (8). For Data, ``length`` is the payload that
follows; for WindowUpdate it is the window increment; for Ping an opaque value
echoed back; for GoAway the reason code.

Flow control is per stream: each side starts with a 256 KiB window, and the
receiver grows the sender's window with WindowUpdate frames up to its own
``max_stream_window`` (the Burrow gateway uses ``1 << 20``). The update rule —
send only once the increment reaches half the maximum, or when a flag must go
out — is Go's ``sendWindowUpdate`` exactly, so both ends account identically.

What the Go peer is strict about, and this implementation therefore never does:

- send more data on a stream than its send window allows (the peer tears the
  whole session down with ``ErrRecvWindowExceeded``);
- send FIN twice, or a FIN on a stream it has reset (``ErrUnexpectedFlag``);
- leave a stream the peer opened un-ACKed for 75 seconds (Go's
  ``StreamOpenTimeout`` closes the *session*). Streams are ACKed the moment they
  are accepted, which :meth:`Session.accept` does as soon as it is called.
"""

from __future__ import annotations

import asyncio
import contextlib
import enum
import logging
import struct
from collections import deque
from collections.abc import Callable, Iterable
from typing import Final, Protocol

_LOGGER = logging.getLogger(__name__)

PROTO_VERSION: Final = 0

TYPE_DATA: Final = 0
TYPE_WINDOW_UPDATE: Final = 1
TYPE_PING: Final = 2
TYPE_GO_AWAY: Final = 3

FLAG_SYN: Final = 1
FLAG_ACK: Final = 2
FLAG_FIN: Final = 4
FLAG_RST: Final = 8

GO_AWAY_NORMAL: Final = 0
GO_AWAY_PROTO_ERR: Final = 1
GO_AWAY_INTERNAL_ERR: Final = 2

HEADER_SIZE: Final = 12
INITIAL_STREAM_WINDOW: Final = 256 * 1024
#: The Burrow gateway's ``MaxStreamWindowSize`` (``burrow/pkg/gateway``).
DEFAULT_MAX_STREAM_WINDOW: Final = 1 << 20
DEFAULT_ACCEPT_BACKLOG: Final = 256
DEFAULT_STREAM_CLOSE_TIMEOUT: Final = 300.0
DEFAULT_WRITE_TIMEOUT: Final = 10.0
#: Largest Data frame this side writes. The protocol allows anything up to the
#: send window; a smaller cap keeps one busy stream from monopolising the
#: shared connection for a megabyte at a time.
MAX_FRAME_PAYLOAD: Final = 64 * 1024
_MAX_UINT32: Final = 0xFFFFFFFF

_HEADER = struct.Struct(">BBHII")


class YamuxError(Exception):
    """Base class for yamux failures."""


class SessionClosedError(YamuxError):
    """The session is shut down."""


class ProtocolViolationError(YamuxError):
    """The peer broke the protocol; the session cannot continue."""


class RemoteGoAwayError(YamuxError):
    """The peer sent GoAway: it accepts no new streams."""


class StreamClosedError(YamuxError):
    """Writing to a stream this side has already closed."""


class StreamResetError(YamuxError):
    """The stream was reset (RST) by either side."""


class Conn(Protocol):
    """The byte pipe under a session: a WebSocket in Burrow, a socket in tests."""

    async def read(self) -> bytes:
        """The next chunk of bytes; ``b""`` at end of stream."""
        ...

    async def write(self, data: bytes) -> None: ...

    async def close(self) -> None: ...


def encode_header(msg_type: int, flags: int, stream_id: int, length: int) -> bytes:
    return _HEADER.pack(PROTO_VERSION, msg_type, flags, stream_id, length)


def decode_header(data: bytes) -> tuple[int, int, int, int, int]:
    """``(version, type, flags, stream_id, length)``."""
    version, msg_type, flags, stream_id, length = _HEADER.unpack(data)
    return version, msg_type, flags, stream_id, length


class _State(enum.Enum):
    INIT = enum.auto()
    SYN_SENT = enum.auto()
    SYN_RECEIVED = enum.auto()
    ESTABLISHED = enum.auto()
    LOCAL_CLOSE = enum.auto()
    REMOTE_CLOSE = enum.auto()
    CLOSED = enum.auto()
    RESET = enum.auto()


class _Reader:
    """``readexactly`` over a :class:`Conn` that hands out arbitrary chunks."""

    def __init__(self, conn: Conn) -> None:
        self._conn = conn
        self._buf = bytearray()

    async def readexactly(self, n: int) -> bytes:
        while len(self._buf) < n:
            chunk = await self._conn.read()
            if not chunk:
                raise EOFError
            self._buf += chunk
        out = bytes(self._buf[:n])
        del self._buf[:n]
        return out


class Stream:
    """One yamux stream: an ordered, flow-controlled byte pipe.

    Mirrors Go's ``yamux.Stream``: :meth:`close` is a half-close (FIN) of this
    side's writes, and the stream is gone once both sides have sent FIN or
    either has sent RST.
    """

    def __init__(self, session: Session, stream_id: int, state: _State) -> None:
        self._session = session
        self.id: Final = stream_id
        self._state = state
        self._recv_window = INITIAL_STREAM_WINDOW
        self._send_window = INITIAL_STREAM_WINDOW
        self._recv_buf = bytearray()
        self._recv_event = asyncio.Event()
        self._send_event = asyncio.Event()
        self._established = asyncio.Event()
        self._write_lock = asyncio.Lock()
        self._close_timer: asyncio.TimerHandle | None = None
        self._done = asyncio.Event()

    def __repr__(self) -> str:
        return f"<yamux.Stream id={self.id} state={self._state.name}>"

    @property
    def session(self) -> Session:
        return self._session

    @property
    def is_reset(self) -> bool:
        return self._state is _State.RESET

    @property
    def is_closed(self) -> bool:
        """True once neither side will send anything more."""
        return self._state in (_State.CLOSED, _State.RESET)

    @property
    def write_closed(self) -> bool:
        return self._state in (_State.LOCAL_CLOSE, _State.CLOSED, _State.RESET)

    async def wait_closed(self) -> None:
        await self._done.wait()

    # -- reading -------------------------------------------------------------

    async def read(self, n: int = -1) -> bytes:
        """Up to ``n`` bytes (all buffered if ``n < 0``); ``b""`` at EOF.

        Raises :class:`StreamResetError` if the stream was reset.
        """
        while True:
            if self._recv_buf:
                if n < 0 or n >= len(self._recv_buf):
                    out = bytes(self._recv_buf)
                    self._recv_buf.clear()
                else:
                    out = bytes(self._recv_buf[:n])
                    del self._recv_buf[:n]
                self._send_window_update()
                return out
            if self._state is _State.RESET:
                raise StreamResetError(f"stream {self.id} reset")
            if self._state in (_State.REMOTE_CLOSE, _State.CLOSED) or self._session.is_closed:
                return b""
            self._recv_event.clear()
            await self._recv_event.wait()

    async def readuntil(self, separator: bytes = b"\n", limit: int = 64 * 1024) -> bytes:
        """Read through ``separator`` and no further, leaving the rest buffered.

        Raises :class:`EOFError` if the stream ends first and
        :class:`ValueError` past ``limit`` bytes.
        """
        while True:
            idx = self._recv_buf.find(separator)
            if idx >= 0:
                end = idx + len(separator)
                out = bytes(self._recv_buf[:end])
                del self._recv_buf[:end]
                self._send_window_update()
                return out
            if len(self._recv_buf) > limit:
                raise ValueError(f"no {separator!r} within {limit} bytes")
            if self._state is _State.RESET:
                raise StreamResetError(f"stream {self.id} reset")
            if self._state in (_State.REMOTE_CLOSE, _State.CLOSED) or self._session.is_closed:
                raise EOFError
            self._recv_event.clear()
            await self._recv_event.wait()

    def _send_window_update(self) -> None:
        """Go's ``sendWindowUpdate``: grow the peer's view of our window.

        Called after the reader consumes data and once on open/accept, where
        the frame also carries SYN or ACK.
        """
        if self._state in (_State.CLOSED, _State.RESET):
            # The peer has forgotten the stream; an update would be dropped.
            return
        max_window = self._session.max_stream_window
        delta = (max_window - len(self._recv_buf)) - self._recv_window
        flags = self._send_flags()
        if delta < max_window // 2 and flags == 0:
            return
        self._recv_window += delta
        self._session._send_nowait(encode_header(TYPE_WINDOW_UPDATE, flags, self.id, delta))

    def _send_flags(self) -> int:
        if self._state is _State.INIT:
            self._state = _State.SYN_SENT
            return FLAG_SYN
        if self._state is _State.SYN_RECEIVED:
            self._state = _State.ESTABLISHED
            return FLAG_ACK
        return 0

    # -- writing -------------------------------------------------------------

    async def write(self, data: bytes | bytearray | memoryview) -> None:
        """Write all of ``data``, waiting on the peer's window as needed."""
        view = memoryview(data).cast("B")
        async with self._write_lock:
            while view:
                self._check_writable()
                if self._send_window == 0:
                    self._send_event.clear()
                    await self._send_event.wait()
                    continue
                n = min(self._send_window, len(view), MAX_FRAME_PAYLOAD)
                flags = self._send_flags()
                self._send_window -= n
                frame = encode_header(TYPE_DATA, flags, self.id, n) + view[:n]
                await self._session._send(frame)
                view = view[n:]

    def _check_writable(self) -> None:
        if self._session.is_closed:
            raise SessionClosedError("session closed")
        if self._state is _State.RESET:
            raise StreamResetError(f"stream {self.id} reset")
        if self._state in (_State.LOCAL_CLOSE, _State.CLOSED):
            raise StreamClosedError(f"stream {self.id} closed for writing")

    async def close(self) -> None:
        """Half-close: send FIN. Reads continue until the peer's FIN.

        Like Go, if the peer never answers with its own FIN the stream is reset
        after the session's stream-close timeout.
        """
        # Taking the write lock orders the FIN after any write in progress, so
        # the peer never sees FIN before the last data frame.
        async with self._write_lock:
            self._close_nowait()

    def _close_nowait(self) -> None:
        state = self._state
        if state in (_State.SYN_SENT, _State.SYN_RECEIVED, _State.ESTABLISHED, _State.INIT):
            self._state = _State.LOCAL_CLOSE
            closing = False
        elif state is _State.REMOTE_CLOSE:
            self._state = _State.CLOSED
            closing = True
        else:
            return
        if self._close_timer is not None:
            self._close_timer.cancel()
            self._close_timer = None
        flags = FLAG_FIN
        if state is _State.SYN_RECEIVED:
            flags |= FLAG_ACK
        elif state is _State.INIT:
            flags |= FLAG_SYN
        if not closing and self._session.stream_close_timeout > 0:
            self._close_timer = asyncio.get_running_loop().call_later(
                self._session.stream_close_timeout, self._close_timeout
            )
        self._session._send_nowait(encode_header(TYPE_WINDOW_UPDATE, flags, self.id, 0))
        self._notify()
        if closing:
            self._finish()

    def _close_timeout(self) -> None:
        self._close_timer = None
        if self._state is _State.LOCAL_CLOSE:
            _LOGGER.debug("yamux: stream %d never got a FIN back; resetting", self.id)
            self.reset()

    def reset(self) -> None:
        """Abort the stream in both directions (RST)."""
        if self._state in (_State.CLOSED, _State.RESET):
            return
        self._state = _State.RESET
        if not self._session.is_closed:
            self._session._send_nowait(encode_header(TYPE_WINDOW_UPDATE, FLAG_RST, self.id, 0))
        self._notify()
        self._finish()

    # -- driven by the session's receive loop ----------------------------------

    def _process_flags(self, flags: int) -> None:
        if flags & FLAG_ACK:
            if self._state is _State.SYN_SENT:
                self._state = _State.ESTABLISHED
            self._established.set()
        if flags & FLAG_FIN:
            if self._state in (_State.SYN_SENT, _State.SYN_RECEIVED, _State.ESTABLISHED):
                self._state = _State.REMOTE_CLOSE
                self._notify()
            elif self._state is _State.LOCAL_CLOSE:
                self._state = _State.CLOSED
                self._notify()
                self._finish()
            else:
                raise ProtocolViolationError(
                    f"unexpected FIN on stream {self.id} in state {self._state.name}"
                )
        if flags & FLAG_RST:
            self._state = _State.RESET
            self._notify()
            self._finish()

    def _incr_send_window(self, flags: int, length: int) -> None:
        self._process_flags(flags)
        self._send_window += length
        self._send_event.set()

    def _receive(self, flags: int, payload: bytes) -> None:
        self._process_flags(flags)
        if not payload:
            return
        if len(payload) > self._recv_window:
            raise ProtocolViolationError(
                f"receive window exceeded on stream {self.id}: {len(payload)} > {self._recv_window}"
            )
        self._recv_window -= len(payload)
        self._recv_buf += payload
        self._recv_event.set()

    def _force_close(self) -> None:
        if self._state is not _State.RESET:
            self._state = _State.CLOSED
        self._notify()
        self._finish()

    def _notify(self) -> None:
        self._recv_event.set()
        self._send_event.set()
        self._established.set()

    def _finish(self) -> None:
        if self._close_timer is not None:
            self._close_timer.cancel()
            self._close_timer = None
        self._session._forget(self.id)
        self._done.set()


class Session:
    """A yamux session over one :class:`Conn`.

    ``client=True`` is the agent's side: this end opens odd-numbered streams
    and the peer even-numbered ones. Call :meth:`start` (or use ``async with``)
    before anything else.
    """

    def __init__(
        self,
        conn: Conn,
        *,
        client: bool = True,
        max_stream_window: int = DEFAULT_MAX_STREAM_WINDOW,
        accept_backlog: int = DEFAULT_ACCEPT_BACKLOG,
        stream_close_timeout: float = DEFAULT_STREAM_CLOSE_TIMEOUT,
        write_timeout: float = DEFAULT_WRITE_TIMEOUT,
    ) -> None:
        if max_stream_window < INITIAL_STREAM_WINDOW:
            raise ValueError("max_stream_window must be at least the 256 KiB initial window")
        self._conn = conn
        self._reader = _Reader(conn)
        self.max_stream_window: Final = max_stream_window
        self.stream_close_timeout: Final = stream_close_timeout
        self._write_timeout = write_timeout
        self._next_id = 1 if client else 2
        self._streams: dict[int, Stream] = {}
        self._accept: asyncio.Queue[Stream] = asyncio.Queue(accept_backlog)
        self._sendq: deque[tuple[bytes, asyncio.Future[None] | None]] = deque()
        self._send_wakeup = asyncio.Event()
        self._pings: dict[int, asyncio.Future[None]] = {}
        self._ping_id = 0
        self._local_go_away = False
        self._remote_go_away = False
        self._closed = False
        self._close_reason: BaseException | None = None
        self._done = asyncio.Event()
        self._tasks: list[asyncio.Task[None]] = []
        self._on_close: list[Callable[[], None]] = []

    async def __aenter__(self) -> Session:
        self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    def start(self) -> None:
        if self._tasks:
            return
        self._tasks = [
            asyncio.create_task(self._recv_loop(), name="yamux-recv"),
            asyncio.create_task(self._send_loop(), name="yamux-send"),
        ]

    @property
    def is_closed(self) -> bool:
        return self._closed

    @property
    def close_reason(self) -> BaseException | None:
        """Why the session ended, if it ended on an error."""
        return self._close_reason

    @property
    def num_streams(self) -> int:
        return len(self._streams)

    async def wait_closed(self) -> None:
        await self._done.wait()

    def add_close_callback(self, cb: Callable[[], None]) -> None:
        if self._closed:
            cb()
        else:
            self._on_close.append(cb)

    # -- streams -------------------------------------------------------------

    async def open(self) -> Stream:
        """Open a stream. Sends its SYN at once; data may follow before the ACK."""
        if self._closed:
            raise SessionClosedError("session closed")
        if self._remote_go_away:
            raise RemoteGoAwayError("remote end is not accepting streams")
        stream_id = self._next_id
        if stream_id >= _MAX_UINT32 - 1:
            raise YamuxError("stream ids exhausted")
        self._next_id += 2
        stream = Stream(self, stream_id, _State.INIT)
        self._streams[stream_id] = stream
        stream._send_window_update()  # SYN, and our window beyond the initial 256 KiB
        return stream

    async def accept(self) -> Stream:
        """The next stream the peer opened, ACKed. Raises once the session ends."""
        while True:
            if self._closed and self._accept.empty():
                raise SessionClosedError("session closed") from self._close_reason
            get = asyncio.ensure_future(self._accept.get())
            done = asyncio.ensure_future(self._done.wait())
            try:
                await asyncio.wait({get, done}, return_when=asyncio.FIRST_COMPLETED)
            finally:
                done.cancel()
                if not get.done():
                    get.cancel()
            if get.done() and not get.cancelled():
                stream = get.result()
                if stream.is_closed:
                    continue
                stream._send_window_update()  # ACK
                return stream

    # -- session control -----------------------------------------------------

    async def ping(self, timeout: float | None = None) -> float:
        """Round-trip time of a yamux ping, in seconds."""
        loop = asyncio.get_running_loop()
        ping_id = self._ping_id
        self._ping_id = (self._ping_id + 1) & _MAX_UINT32
        fut: asyncio.Future[None] = loop.create_future()
        self._pings[ping_id] = fut
        start = loop.time()
        await self._send(encode_header(TYPE_PING, FLAG_SYN, 0, ping_id))
        try:
            async with asyncio.timeout(timeout if timeout is not None else self._write_timeout):
                await fut
        finally:
            self._pings.pop(ping_id, None)
        return loop.time() - start

    async def go_away(self) -> None:
        """Tell the peer to open no more streams. Existing streams carry on."""
        self._local_go_away = True
        await self._send(encode_header(TYPE_GO_AWAY, 0, 0, GO_AWAY_NORMAL))

    async def close(self) -> None:
        """Shut the session down: flush what is queued, then close the conn.

        Like Go's ``Session.Close`` this sends no GoAway of its own and every
        stream ends at once — reads see EOF, writes fail.
        """
        if not self._closed:
            self._shutdown(None)
        await self._done.wait()

    async def wait_streams_idle(self, timeout: float, *, exclude: Iterable[Stream] = ()) -> bool:
        """Wait until every stream but ``exclude`` has finished, up to ``timeout``.

        True if they did.
        """
        skip = {s.id for s in exclude}

        def busy() -> list[Stream]:
            return [s for sid, s in self._streams.items() if sid not in skip]

        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(timeout):
                while streams := busy():
                    await asyncio.gather(*(s.wait_closed() for s in streams))
                return True
        return not busy()

    # -- internals -----------------------------------------------------------

    def _forget(self, stream_id: int) -> None:
        self._streams.pop(stream_id, None)

    def _send_nowait(self, frame: bytes) -> None:
        """Queue a control frame without waiting for it to be written."""
        if self._closed:
            return
        self._sendq.append((frame, None))
        self._send_wakeup.set()

    async def _send(self, frame: bytes) -> None:
        """Queue a frame and wait until it has been written to the conn."""
        if self._closed:
            raise SessionClosedError("session closed")
        fut: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._sendq.append((frame, fut))
        self._send_wakeup.set()
        await fut

    async def _send_loop(self) -> None:
        try:
            while True:
                while not self._sendq:
                    if self._closed:
                        return
                    self._send_wakeup.clear()
                    await self._send_wakeup.wait()
                # Coalesce whatever is queued into one write: on a WebSocket
                # that is one message rather than one per frame.
                frames: list[bytes] = []
                waiters: list[asyncio.Future[None]] = []
                size = 0
                while self._sendq and size < 4 * MAX_FRAME_PAYLOAD:
                    frame, fut = self._sendq.popleft()
                    frames.append(frame)
                    size += len(frame)
                    if fut is not None:
                        waiters.append(fut)
                async with asyncio.timeout(self._write_timeout):
                    await self._conn.write(b"".join(frames))
                for fut in waiters:
                    if not fut.done():
                        fut.set_result(None)
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            self._shutdown(YamuxError("connection write timeout"))
        except Exception as err:
            self._shutdown(err)

    async def _recv_loop(self) -> None:
        try:
            while True:
                hdr = await self._reader.readexactly(HEADER_SIZE)
                version, msg_type, flags, stream_id, length = decode_header(hdr)
                if version != PROTO_VERSION:
                    raise ProtocolViolationError(f"invalid protocol version {version}")
                if msg_type in (TYPE_DATA, TYPE_WINDOW_UPDATE):
                    await self._handle_stream_message(msg_type, flags, stream_id, length)
                elif msg_type == TYPE_PING:
                    self._handle_ping(flags, length)
                elif msg_type == TYPE_GO_AWAY:
                    self._handle_go_away(length)
                else:
                    raise ProtocolViolationError(f"invalid message type {msg_type}")
        except asyncio.CancelledError:
            raise
        except EOFError:
            self._shutdown(None)
        except ProtocolViolationError as err:
            # Tell the peer why, as Go does, before hanging up.
            self._sendq.append((encode_header(TYPE_GO_AWAY, 0, 0, GO_AWAY_PROTO_ERR), None))
            self._send_wakeup.set()
            self._shutdown(err)
        except Exception as err:
            self._shutdown(err)

    async def _handle_stream_message(
        self, msg_type: int, flags: int, stream_id: int, length: int
    ) -> None:
        if flags & FLAG_SYN:
            self._incoming_stream(stream_id)
        stream = self._streams.get(stream_id)
        payload = b""
        if msg_type == TYPE_DATA and length:
            payload = await self._reader.readexactly(length)
        if stream is None:
            # A frame for a stream already forgotten (e.g. data in flight when
            # we reset it). Go drops these too; the payload has been consumed.
            return
        if msg_type == TYPE_WINDOW_UPDATE:
            stream._incr_send_window(flags, length)
        else:
            stream._receive(flags, payload)

    def _incoming_stream(self, stream_id: int) -> None:
        if self._local_go_away:
            self._send_nowait(encode_header(TYPE_WINDOW_UPDATE, FLAG_RST, stream_id, 0))
            return
        if stream_id in self._streams:
            raise ProtocolViolationError(f"duplicate stream {stream_id}")
        stream = Stream(self, stream_id, _State.SYN_RECEIVED)
        try:
            self._accept.put_nowait(stream)
        except asyncio.QueueFull:
            _LOGGER.warning("yamux: accept backlog full, refusing stream %d", stream_id)
            self._send_nowait(encode_header(TYPE_WINDOW_UPDATE, FLAG_RST, stream_id, 0))
            return
        self._streams[stream_id] = stream

    def _handle_ping(self, flags: int, ping_id: int) -> None:
        if flags & FLAG_SYN:
            self._send_nowait(encode_header(TYPE_PING, FLAG_ACK, 0, ping_id))
            return
        fut = self._pings.pop(ping_id, None)
        if fut is not None and not fut.done():
            fut.set_result(None)

    def _handle_go_away(self, code: int) -> None:
        if code == GO_AWAY_NORMAL:
            self._remote_go_away = True
            return
        if code == GO_AWAY_PROTO_ERR:
            raise YamuxError("yamux protocol error reported by the peer")
        if code == GO_AWAY_INTERNAL_ERR:
            raise YamuxError("yamux internal error reported by the peer")
        raise YamuxError(f"unexpected go away code {code}")

    def _shutdown(self, reason: BaseException | None) -> None:
        if self._closed:
            return
        self._closed = True
        self._close_reason = reason
        if reason is not None:
            _LOGGER.debug("yamux: session ended: %r", reason)
        for stream in list(self._streams.values()):
            stream._force_close()
        for fut in self._pings.values():
            if not fut.done():
                fut.set_exception(SessionClosedError("session closed"))
        self._send_wakeup.set()
        asyncio.get_running_loop().create_task(self._finish_shutdown(), name="yamux-close")

    async def _finish_shutdown(self) -> None:
        recv, send = self._tasks if self._tasks else (None, None)
        current = asyncio.current_task()
        # Give the send loop a moment to flush what was queued (a GoAway, the
        # last FINs), then stop both loops and close the conn.
        if send is not None and send is not current and not send.done():
            with contextlib.suppress(TimeoutError, asyncio.CancelledError):
                async with asyncio.timeout(1.0):
                    await asyncio.shield(send)
        for task in (send, recv):
            if task is not None and task is not current and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        for _, fut in self._sendq:
            if fut is not None and not fut.done():
                fut.set_exception(SessionClosedError("session closed"))
        self._sendq.clear()
        with contextlib.suppress(Exception):
            await self._conn.close()
        self._done.set()
        for cb in self._on_close:
            try:
                cb()
            except Exception:
                _LOGGER.exception("yamux: close callback failed")
        self._on_close.clear()
