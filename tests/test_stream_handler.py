"""The vendored AiohttpStreamHandler against a real aiohttp app.

Home Assistant serves its frontend, fonts and icons with ``web.FileResponse``,
which calls ``loop.sendfile`` on the transport. burrow-agent-py v0.1.0's
StreamTransport made that raise RuntimeError, so every static file through the
tunnel failed (HTTP/2 protocol errors and 502s in a browser) while generated
responses worked. These tests drive real responses through the vendored
handler over a fake yamux stream, so that class of bug fails here.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from aiohttp import web
import pytest

from custom_components.xeonr_cloud.burrow_agent import Preamble
from custom_components.xeonr_cloud.burrow_agent.aiohttp import AiohttpStreamHandler


class FakeStream:
    """Just enough of yamux.Stream: one request in, the response out."""

    def __init__(self, request: bytes) -> None:
        self._request = request
        self._head_read = False
        self.written = bytearray()
        self.closed = asyncio.Event()

    async def readuntil(self, separator: bytes = b"\n", limit: int = 65536) -> bytes:
        end = self._request.index(separator) + len(separator)
        head, self._request = self._request[:end], self._request[end:]
        self._head_read = True
        return head

    async def read(self, n: int = -1) -> bytes:
        if self._request:
            data, self._request = self._request, b""
            return data
        # Like the gateway: no FIN until it is done with the stream.
        await self.closed.wait()
        return b""

    async def write(self, data: bytes | bytearray | memoryview) -> None:
        self.written += bytes(data)

    async def close(self) -> None:
        self.closed.set()

    def reset(self) -> None:
        self.closed.set()


def _parse(raw: bytes) -> tuple[int, dict[str, str], bytes]:
    head, _, body = raw.partition(b"\r\n\r\n")
    lines = head.decode().split("\r\n")
    status = int(lines[0].split()[1])
    headers = {
        k.lower(): v.strip() for k, _, v in (line.partition(":") for line in lines[1:])
    }
    if headers.get("transfer-encoding") == "chunked":
        out, rest = bytearray(), body
        while True:
            size_line, _, rest = rest.partition(b"\r\n")
            size = int(size_line, 16)
            if size == 0:
                break
            out += rest[:size]
            rest = rest[size + 2 :]
        body = bytes(out)
    return status, headers, body


@pytest.fixture
async def runner(tmp_path: Path):
    blob = bytes(range(256)) * 2048  # 512 KiB, above the transport's high water
    (tmp_path / "app.js").write_bytes(blob)

    async def remote(request: web.Request) -> web.Response:
        return web.json_response(
            {
                "remote": request.remote,
                "secure": request.secure,
                "xff": request.headers.get("X-Forwarded-For"),
            }
        )

    app = web.Application()
    app.router.add_get("/remote", remote)

    async def static(request: web.Request) -> web.FileResponse:
        return web.FileResponse(tmp_path / "app.js")

    app.router.add_get("/app.js", static)
    runner = web.AppRunner(app)
    await runner.setup()
    yield runner, blob
    await runner.cleanup()


async def _serve(runner: web.AppRunner, request: bytes) -> tuple[int, dict, bytes]:
    stream = FakeStream(request)
    preamble = Preamble(kind="http", remote="203.0.113.7:51234", host="x.brw.xnr.app")
    await asyncio.wait_for(AiohttpStreamHandler(runner)(stream, preamble), 10)
    return _parse(bytes(stream.written))


async def test_static_file_is_served_through_the_tunnel(runner) -> None:
    runner, blob = runner
    status, _, body = await _serve(
        runner, b"GET /app.js HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n"
    )
    assert status == 200
    assert body == blob


async def test_real_ip_and_no_forwarded_headers(runner) -> None:
    runner, _ = runner
    status, _, body = await _serve(
        runner,
        b"GET /remote HTTP/1.1\r\nHost: x\r\nX-Forwarded-For: 1.2.3.4\r\n"
        b"Connection: close\r\n\r\n",
    )
    assert status == 200
    assert body == b'{"remote": "203.0.113.7", "secure": true, "xff": null}'
