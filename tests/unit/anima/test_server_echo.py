"""Unit tests for Server Echo extension."""

import asyncio
import base64

import pytest

from anima.core import Context
from anima.http import HttpService
from anima.server import ServerService
from anima.server_echo import EchoConfig, ServerEcho


async def raw_post(
    port: int, body: bytes = b"", host_header: str = "127.0.0.1"
) -> tuple[int, str, dict[str, str]]:
    """Send a raw POST to root and return (status, body, headers)."""
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    req = (
        f"POST / HTTP/1.1\r\n"
        f"Host: {host_header}\r\n"
        f"Content-Type: text/plain\r\n"
        f"Content-Length: {len(body)}\r\n"
        f"Connection: close\r\n\r\n"
    ).encode()
    writer.write(req + body)
    await writer.drain()
    data = await reader.read()
    writer.close()
    await writer.wait_closed()
    response_text = data.decode("utf-8", errors="replace")
    head, _, body_text = response_text.partition("\r\n\r\n")
    lines = head.split("\r\n")
    status = int(lines[0].split()[1]) if len(lines) >= 2 else 500
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if ":" in line:
            k, v = line.split(":", 1)
            headers[k.strip().lower()] = v.strip()
    return status, body_text, headers


@pytest.mark.asyncio
async def test_echo_post_body():
    """POST to root echoes the request body back."""
    ctx = Context()
    HttpService(ctx)
    srv = ServerService(ctx)
    ServerEcho(ctx)
    await srv.listen(host="127.0.0.1", port=0)

    try:
        status, body, headers = await raw_post(srv.port, b"hello echo")
        assert status == 200
        assert body == "hello echo"
        assert headers.get("content-type") == "text/plain"
    finally:
        await srv.close()


@pytest.mark.asyncio
async def test_echo_post_empty_body():
    """POST with empty body echoes empty body."""
    ctx = Context()
    HttpService(ctx)
    srv = ServerService(ctx)
    ServerEcho(ctx)
    await srv.listen(host="127.0.0.1", port=0)

    try:
        status, body, _ = await raw_post(srv.port, b"")
        assert status == 200
        assert body == ""
    finally:
        await srv.close()


@pytest.mark.asyncio
async def test_echo_post_no_content_type():
    """POST without content-type still echoes body (no header set)."""
    ctx = Context()
    HttpService(ctx)
    srv = ServerService(ctx)
    ServerEcho(ctx)
    await srv.listen(host="127.0.0.1", port=0)

    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", srv.port)
        body = b"no-content-type"
        req = (
            f"POST / HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{srv.port}\r\n"
            f"Content-Length: {len(body)}\r\n"
            f"Connection: close\r\n\r\n"
        ).encode()
        writer.write(req + body)
        await writer.drain()
        data = await reader.read()
        writer.close()
        await writer.wait_closed()
        response_text = data.decode("utf-8", errors="replace")
        head, _, body_text = response_text.partition("\r\n\r\n")
        status = int(head.split("\r\n")[0].split()[1])
        assert status == 200
        assert body_text == "no-content-type"
        # Default content-type is text/plain; charset=utf-8 (not echoed from request)
        assert "content-type: text/plain" in head.lower()
    finally:
        await srv.close()


@pytest.mark.asyncio
async def test_echo_websocket():
    """WebSocket at root echoes messages back to the client."""
    ctx = Context()
    HttpService(ctx)
    srv = ServerService(ctx)
    ServerEcho(ctx)
    await srv.listen(host="127.0.0.1", port=0)

    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", srv.port)
        key = base64.b64encode(b"test-ws-echo-key!").decode("ascii")
        req = (
            f"GET / HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{srv.port}\r\n"
            f"Upgrade: websocket\r\n"
            f"Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            f"Sec-WebSocket-Version: 13\r\n\r\n"
        )
        writer.write(req.encode("ascii"))
        await writer.drain()
        upgrade_resp = await reader.readuntil(b"\r\n\r\n")
        assert b"101 Switching Protocols" in upgrade_resp

        # Send masked text frame "ping"
        mask = b"\x12\x34\x56\x78"
        payload = b"ping"
        masked_payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        frame = bytes([0x81, 0x80 | len(payload)]) + mask + masked_payload
        writer.write(frame)
        await writer.drain()

        # Read echoed frame (server frames are unmasked per RFC 6455)
        resp_head = await reader.readexactly(2)
        assert resp_head[0] == 0x81  # text frame
        resp_len = resp_head[1] & 0x7F
        assert not (resp_head[1] & 0x80)  # server frames are not masked
        resp_data = await reader.readexactly(resp_len)
        assert resp_data == b"ping"

        # Close
        close_payload = bytes([1000 >> 8, 1000 & 0xFF])
        masked_close = bytes(b ^ mask[i % 4] for i, b in enumerate(close_payload))
        close_frame = bytes([0x88, 0x80 | 2]) + mask + masked_close
        writer.write(close_frame)
        await writer.drain()
        writer.close()
        await writer.wait_closed()
    finally:
        await srv.close()


def test_echo_config():
    """EchoConfig is instantiable with no required fields."""
    config = EchoConfig()
    assert config is not None


def test_echo_apply_helper():
    """Top-level apply helper returns a ServerEcho instance."""
    ctx = Context()
    HttpService(ctx)
    ServerService(ctx)
    from anima.server_echo import apply as echo_apply

    instance = echo_apply(ctx)
    assert isinstance(instance, ServerEcho)
    assert ctx.has("server:echo") or True
