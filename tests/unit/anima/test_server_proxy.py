"""Unit tests for Server Proxy extension."""

import asyncio
import base64

import pytest

from anima.core import Context
from anima.http import HttpService
from anima.server import ServerService
from anima.server_proxy import ProxyConfig, ServerProxy


@pytest.mark.asyncio
async def test_proxy_http_get_post_headers():
    # Upstream server
    ctx_up = Context()
    HttpService(ctx_up)
    srv_up = ServerService(ctx_up)

    srv_up.get("/hello", lambda req, res: res.text("world"))
    srv_up.get("/search", lambda req, res: res.text(req.query.get("q", [""])[0]))
    srv_up.get("/root", lambda req, res: res.text("root-ok"))
    srv_up.post("/echo", lambda req, res: res.text(req.text()))
    srv_up.post(
        "/json",
        lambda req, res: (
            res.json(req.json()),
            res.headers.update({"x-custom": "header-val"}),
        ),
    )
    srv_up.get(
        "/not-found",
        lambda req, res: res.text("Not Found Here", status=404),
    )

    await srv_up.listen(host="127.0.0.1", port=0)

    # Proxy server
    ctx_proxy = Context()
    HttpService(ctx_proxy)
    srv_proxy = ServerService(ctx_proxy)
    ServerProxy(ctx_proxy, ProxyConfig(base_url=srv_up.url))
    await srv_proxy.listen(host="127.0.0.1", port=0)

    try:
        # Check ctx.server.proxy.base_url
        assert ctx_proxy.server.proxy.base_url == srv_proxy.url

        # 1. Simple GET
        res1 = await ctx_proxy.http.get(f"{srv_proxy.url}/hello", response_type="text")
        assert res1 == "world"

        # 2. Query string
        res2 = await ctx_proxy.http.get(
            f"{srv_proxy.url}/search",
            params={"q": "testing123"},
            response_type="text",
        )
        assert res2 == "testing123"

        # 3. POST text body
        res3 = await ctx_proxy.http.post(
            f"{srv_proxy.url}/echo",
            data="hello proxy",
            response_type="text",
        )
        assert res3 == "hello proxy"

        # 4. POST JSON and headers
        resp4 = await ctx_proxy.http.post(
            f"{srv_proxy.url}/json",
            data={"foo": "bar"},
            response_type="response",
        )
        assert resp4.status == 200
        assert resp4.json() == {"foo": "bar"}
        assert resp4.headers.get("x-custom") == "header-val"

        # 5. Upstream 404
        with pytest.raises(Exception) as exc:
            await ctx_proxy.http.get(f"{srv_proxy.url}/not-found")
        assert exc.value.status == 404
    finally:
        await srv_proxy.close()
        await srv_up.close()


@pytest.mark.asyncio
async def test_proxy_prefix_base_url():
    # Upstream server
    ctx_up = Context()
    HttpService(ctx_up)
    srv_up = ServerService(ctx_up)

    srv_up.get("/api/v1/data", lambda req, res: res.text("scoped-data"))
    await srv_up.listen(host="127.0.0.1", port=0)

    # Proxy server configured with /api/v1/ prefix
    ctx_proxy = Context()
    HttpService(ctx_proxy)
    srv_proxy = ServerService(ctx_proxy)
    ServerProxy(ctx_proxy, {"base_url": f"{srv_up.url}/api/v1/"})
    await srv_proxy.listen(host="127.0.0.1", port=0)

    try:
        # Request /data on proxy forwards to /api/v1/data upstream
        res = await ctx_proxy.http.get(f"{srv_proxy.url}/data", response_type="text")
        assert res == "scoped-data"
    finally:
        await srv_proxy.close()
        await srv_up.close()


@pytest.mark.asyncio
async def test_proxy_websocket():
    # Upstream server with WebSocket
    ctx_up = Context()
    HttpService(ctx_up)
    srv_up = ServerService(ctx_up)

    received_upstream = []

    async def upstream_ws(ws):
        async for msg in ws:
            received_upstream.append(msg)
            if msg == "ping":
                await ws.send_text("pong")
            elif msg == "close":
                await ws.close()

    srv_up.ws("/ws/echo", upstream_ws)
    await srv_up.listen(host="127.0.0.1", port=0)

    # Proxy server
    ctx_proxy = Context()
    HttpService(ctx_proxy)
    srv_proxy = ServerService(ctx_proxy)
    ServerProxy(ctx_proxy, ProxyConfig(base_url=srv_up.url))
    await srv_proxy.listen(host="127.0.0.1", port=0)

    try:
        # Connect downstream client to proxy
        reader, writer = await asyncio.open_connection("127.0.0.1", srv_proxy.port)
        key = base64.b64encode(b"test-ws-proxy-k!").decode("ascii")
        req = (
            f"GET /ws/echo HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{srv_proxy.port}\r\n"
            f"Upgrade: websocket\r\n"
            f"Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            f"Sec-WebSocket-Version: 13\r\n\r\n"
        )
        writer.write(req.encode("ascii"))
        await writer.drain()

        # Read 101 response
        upgrade_resp = await reader.readuntil(b"\r\n\r\n")
        assert b"101 Switching Protocols" in upgrade_resp

        # Send masked text frame from downstream client: "ping"
        mask = b"\x12\x34\x56\x78"
        payload = b"ping"
        masked_payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        frame = bytes([0x81, 0x80 | len(payload)]) + mask + masked_payload
        writer.write(frame)
        await writer.drain()

        # Read response frame from proxy: "pong"
        resp_head = await reader.readexactly(2)
        resp_len = resp_head[1] & 0x7F
        resp_data = await reader.readexactly(resp_len)
        assert resp_data == b"pong"

        # Send close frame
        close_payload = bytes([1000 >> 8, 1000 & 0xFF])
        masked_close = bytes(b ^ mask[i % 4] for i, b in enumerate(close_payload))
        close_frame = bytes([0x88, 0x80 | 2]) + mask + masked_close
        writer.write(close_frame)
        await writer.drain()

        writer.close()
        await writer.wait_closed()
        assert "ping" in received_upstream
    finally:
        await srv_proxy.close()
        await srv_up.close()


@pytest.mark.asyncio
async def test_proxy_edge_cases_and_coverage():
    ctx = Context()
    HttpService(ctx)
    srv = ServerService(ctx)

    # 1. ServerProxy with kwargs and base_url fallback before listen
    from anima.server_proxy import apply as proxy_apply

    proxy_instance = proxy_apply(ctx, base_url="ws://127.0.0.1:59999/prefix")
    assert proxy_instance.base_url == "ws://127.0.0.1:59999/prefix"

    # 2. Test WS proxy upstream connection failure
    await srv.listen(host="127.0.0.1", port=0)
    reader, writer = await asyncio.open_connection("127.0.0.1", srv.port)
    key = base64.b64encode(b"test-ws-fail-key").decode("ascii")
    req = (
        f"GET /ws/down?query=val HTTP/1.1\r\n"
        f"Host: 127.0.0.1:{srv.port}\r\n"
        f"Upgrade: websocket\r\n"
        f"Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        f"Sec-WebSocket-Version: 13\r\n\r\n"
    )
    writer.write(req.encode("ascii"))
    await writer.drain()

    await reader.read(256)
    writer.close()
    await writer.wait_closed()
    await srv.close()


@pytest.mark.asyncio
async def test_proxy_ws_https_and_exceptions():
    ctx = Context()
    HttpService(ctx)
    srv = ServerService(ctx)

    from anima.server_proxy import apply as proxy_apply

    proxy_apply(ctx, base_url="https://127.0.0.1:59999/secure")

    await srv.listen(host="127.0.0.1", port=0)
    reader, writer = await asyncio.open_connection("127.0.0.1", srv.port)
    key = base64.b64encode(b"test-ws-https-k!").decode("ascii")
    req = (
        f"GET /path HTTP/1.1\r\n"
        f"Host: 127.0.0.1:{srv.port}\r\n"
        f"Upgrade: websocket\r\n"
        f"Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        f"Sec-WebSocket-Version: 13\r\n\r\n"
    )
    writer.write(req.encode("ascii"))
    await writer.drain()

    await reader.read(256)
    writer.close()
    await writer.wait_closed()
    await srv.close()
