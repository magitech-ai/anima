import asyncio
import base64
import hashlib

import pytest

from anima.core import Context
from anima.http import HttpService
from anima.server import ServerRequest, ServerResponse, ServerService


@pytest.mark.asyncio
async def test_server_service_registration():
    ctx = Context()
    srv = ServerService(ctx)
    assert ctx.has("server")
    assert ctx.server == srv


@pytest.mark.asyncio
async def test_server_http_routing_and_parameters():
    ctx = Context()
    HttpService(ctx)
    srv = ServerService(ctx)

    # 1. Register routes
    srv.get("/hello", lambda req, res: res.text("Hello World!"))
    srv.post("/api/echo", lambda req, res: res.json({"echo": req.json()}))
    srv.get(
        "/users/<user_id>", lambda req, res: res.json({"user": req.params["user_id"]})
    )

    await srv.listen(host="127.0.0.1", port=0)
    base_url = srv.url

    try:
        # GET /hello
        text_resp = await ctx.http.get(f"{base_url}/hello", response_type="text")
        assert text_resp == "Hello World!"

        # POST /api/echo
        json_resp = await ctx.http.post(
            f"{base_url}/api/echo",
            data={"msg": "test-data"},
            response_type="json",
        )
        assert json_resp == {"echo": {"msg": "test-data"}}

        # Param route /users/alice
        param_resp = await ctx.http.get(f"{base_url}/users/alice", response_type="json")
        assert param_resp == {"user": "alice"}

        # 404 not found
        with pytest.raises(Exception) as exc_404:
            await ctx.http.get(f"{base_url}/nonexistent")
        assert exc_404.value.status == 404
    finally:
        await srv.close()


@pytest.mark.asyncio
async def test_server_route_disposal_and_method_not_allowed():
    ctx = Context()
    HttpService(ctx)
    srv = ServerService(ctx)

    disposer = srv.get("/temporary", lambda req, res: res.text("alive"))
    await srv.listen(host="127.0.0.1", port=0)

    try:
        resp = await ctx.http.get(f"{srv.url}/temporary", response_type="text")
        assert resp == "alive"

        # POST to GET route -> 405
        with pytest.raises(Exception) as exc_405:
            await ctx.http.post(f"{srv.url}/temporary")
        assert exc_405.value.status == 405

        # Unregister route
        disposer()
        disposer()  # Double dispose is safe
        with pytest.raises(Exception) as exc_404:
            await ctx.http.get(f"{srv.url}/temporary")
        assert exc_404.value.status == 404
    finally:
        await srv.close()


@pytest.mark.asyncio
async def test_server_waterfall_middleware():
    ctx = Context()
    HttpService(ctx)
    srv = ServerService(ctx)

    srv.get("/ping", lambda req, res: res.text("pong"))

    async def middleware(req, res, next_fn):
        res.headers["X-Custom-Server"] = "active"
        await next_fn(req, res)

    ctx.on("server/request", middleware)

    await srv.listen(host="127.0.0.1", port=0)
    try:
        resp = await ctx.http.get(f"{srv.url}/ping", response_type="response")
        assert resp.status == 200
        assert resp.text() == "pong"
        assert resp.headers.get("x-custom-server") == "active"
    finally:
        await srv.close()


@pytest.mark.asyncio
async def test_server_websocket_handshake_and_echo():
    ctx = Context()
    srv = ServerService(ctx)

    received_messages = []

    async def ws_handler(ws):
        async for msg in ws:
            received_messages.append(msg)
            await ws.send_text(f"echo:{msg}")
            if msg == "close":
                await ws.close()

    srv.ws("/ws/chat", ws_handler)
    await srv.listen(host="127.0.0.1", port=0)

    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", srv.port)

        # Send WebSocket upgrade request
        key = base64.b64encode(b"test-websocket-k").decode("ascii")
        upgrade_req = (
            f"GET /ws/chat HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{srv.port}\r\n"
            f"Upgrade: websocket\r\n"
            f"Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            f"Sec-WebSocket-Version: 13\r\n\r\n"
        )
        writer.write(upgrade_req.encode("ascii"))
        await writer.drain()

        # Read handshake response
        handshake_resp = await reader.readuntil(b"\r\n\r\n")
        assert b"101 Switching Protocols" in handshake_resp

        # Verify Sec-WebSocket-Accept calculation
        expected_accept = base64.b64encode(
            hashlib.sha1(
                (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")
            ).digest()
        ).decode("ascii")
        assert expected_accept.encode("ascii") in handshake_resp

        # Send client masked text frame "hello"
        # Opcode 1 (text), FIN bit set (0x81), Masked bit set (0x80 | length)
        payload = b"hello"
        mask_key = b"\x12\x34\x56\x78"
        masked_payload = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))
        frame = bytes([0x81, 0x80 | len(payload)]) + mask_key + masked_payload
        writer.write(frame)
        await writer.drain()

        # Read server unmasked text frame
        header = await reader.readexactly(2)
        assert header[0] == 0x81  # text frame
        length = header[1] & 0x7F
        server_msg = await reader.readexactly(length)
        assert server_msg.decode("utf-8") == "echo:hello"
        assert received_messages == ["hello"]

        # Close frame
        close_frame = bytes([0x88, 0x80]) + mask_key  # opcode 8, len 0 masked
        writer.write(close_frame)
        await writer.drain()

        writer.close()
        await writer.wait_closed()
    finally:
        await srv.close()


@pytest.mark.asyncio
async def test_server_http_methods_and_response_send():
    ctx = Context()
    HttpService(ctx)
    srv = ServerService(ctx)

    srv.put("/put", lambda req, res: res.send(b"put-resp", status=200))
    srv.patch("/patch", lambda req, res: res.send(b"patch-resp", status=200))
    srv.delete("/del", lambda req, res: res.send(b"del-resp", status=204))
    srv.head("/hd", lambda req, res: res.send(b"", status=200))
    srv.all("/any", lambda req, res: res.text(f"method:{req.method}"))

    await srv.listen(host="127.0.0.1", port=0)
    try:
        r_put = await ctx.http.put(f"{srv.url}/put", response_type="bytes")
        assert r_put == b"put-resp"

        r_patch = await ctx.http.patch(f"{srv.url}/patch", response_type="bytes")
        assert r_patch == b"patch-resp"

        r_del = await ctx.http.delete(f"{srv.url}/del")
        assert r_del.status == 204

        r_hd = await ctx.http.head(f"{srv.url}/hd")
        assert r_hd.status == 200

        r_any_get = await ctx.http.get(f"{srv.url}/any", response_type="text")
        assert r_any_get == "method:GET"

        r_any_post = await ctx.http.post(f"{srv.url}/any", response_type="text")
        assert r_any_post == "method:POST"
    finally:
        await srv.close()
        # Second close is safe
        await srv.close()


@pytest.mark.asyncio
async def test_server_malformed_requests_and_empty_connections():
    ctx = Context()
    srv = ServerService(ctx)
    await srv.listen(host="127.0.0.1", port=0)

    try:
        # 1. Immediate close without sending data
        _r, w = await asyncio.open_connection("127.0.0.1", srv.port)
        w.close()
        await w.wait_closed()

        # 2. Malformed request line (less than 2 parts)
        _r2, w2 = await asyncio.open_connection("127.0.0.1", srv.port)
        w2.write(b"GARBAGE_NO_PATH\r\n\r\n")
        await w2.drain()
        w2.close()
        await w2.wait_closed()

        # 3. Request with header line without colon
        r3, w3 = await asyncio.open_connection("127.0.0.1", srv.port)
        w3.write(b"GET / HTTP/1.1\r\nMalformedHeaderLineWithoutColon\r\n\r\n")
        await w3.drain()
        resp_bytes = await r3.read()
        assert b"404" in resp_bytes
        w3.close()
        await w3.wait_closed()
    finally:
        await srv.close()


@pytest.mark.asyncio
async def test_server_websocket_advanced_framing():
    ctx = Context()
    srv = ServerService(ctx)

    received_frames = []

    def sync_ws_handler(ws):
        # Synchronous WS handler
        async def _run():
            await ws.send_bytes(b"initial_bytes")
            async for msg in ws:
                received_frames.append(msg)
                if msg == "close_now":
                    await ws.close()
                    # Subsequent send calls are safe no-ops
                    await ws.send_text("after_close")
                    await ws.send_bytes(b"after_close")
                    await ws.close()

        asyncio.create_task(_run())

    ws_disposer = srv.ws("/ws/advanced", sync_ws_handler)
    await srv.listen(host="127.0.0.1", port=0)

    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", srv.port)

        key = base64.b64encode(b"test-ws-key-1234").decode("ascii")
        upgrade_req = (
            f"GET /ws/advanced HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{srv.port}\r\n"
            f"Upgrade: websocket\r\n"
            f"Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            f"Sec-WebSocket-Version: 13\r\n\r\n"
        )
        writer.write(upgrade_req.encode("ascii"))
        await writer.drain()

        handshake_resp = await reader.readuntil(b"\r\n\r\n")
        assert b"101 Switching Protocols" in handshake_resp

        # Read initial binary frame (opcode 0x02)
        h = await reader.readexactly(2)
        assert h[0] == 0x82  # binary frame
        length = h[1] & 0x7F
        data = await reader.readexactly(length)
        assert data == b"initial_bytes"

        # 1. Send Ping frame (opcode 0x09) with mask
        ping_payload = b"ping_payload"
        mask = b"\x01\x02\x03\x04"
        masked_ping = bytes(b ^ mask[i % 4] for i, b in enumerate(ping_payload))
        writer.write(bytes([0x89, 0x80 | len(ping_payload)]) + mask + masked_ping)
        await writer.drain()

        # Read Pong frame (opcode 0x0A)
        pong_h = await reader.readexactly(2)
        assert pong_h[0] == 0x8A  # pong
        pong_len = pong_h[1] & 0x7F
        pong_data = await reader.readexactly(pong_len)
        assert pong_data == b"ping_payload"

        # 2. Send 16-bit extended length frame (len between 126 and 65535)
        med_payload = b"x" * 200
        masked_med = bytes(b ^ mask[i % 4] for i, b in enumerate(med_payload))
        med_frame = (
            bytes([0x81, 0x80 | 126]) + (200).to_bytes(2, "big") + mask + masked_med
        )
        writer.write(med_frame)
        await writer.drain()

        # 3. Send unmasked text frame (is_masked=False)
        unmasked_payload = b"unmasked_msg"
        unmasked_frame = bytes([0x81, len(unmasked_payload)]) + unmasked_payload
        writer.write(unmasked_frame)
        await writer.drain()

        # Send an ignored binary frame (opcode 0x02) to exercise fall-through in text iterator
        ignored_bin = (
            bytes([0x82, 0x80 | 4])
            + mask
            + bytes(b ^ mask[i % 4] for i, b in enumerate(b"skip"))
        )
        writer.write(ignored_bin)
        await writer.drain()

        # 4. Trigger close
        close_trigger = b"close_now"
        masked_close_trigger = bytes(
            b ^ mask[i % 4] for i, b in enumerate(close_trigger)
        )
        writer.write(
            bytes([0x81, 0x80 | len(close_trigger)]) + mask + masked_close_trigger
        )
        await writer.drain()

        # Read close frame from server
        c_h = await reader.readexactly(2)
        assert c_h[0] == 0x88  # close frame
        c_len = c_h[1] & 0x7F
        if c_len > 0:
            await reader.readexactly(c_len)

        assert "x" * 200 in received_frames
        assert "unmasked_msg" in received_frames

        writer.close()
        await writer.wait_closed()

        # Test ws_disposer
        ws_disposer()
        assert all(r.path_spec != "/ws/advanced" for r in srv.ws_routes)
    finally:
        await srv.close()


@pytest.mark.asyncio
async def test_server_large_frames_and_async_handler():
    ctx = Context()
    HttpService(ctx)
    srv = ServerService(ctx)

    # 1. Async route handler
    async def async_handler(req: ServerRequest, res: ServerResponse):
        await asyncio.sleep(0.001)
        res.json({"status": "async_ok"})

    srv.get("/async-route", async_handler)

    # 2. WebSocket handler sending large text and large bytes (len 126 and 127)
    async def large_ws_handler(ws):
        # Frame of length between 126 and 65535
        await ws.send_text("m" * 300)
        # Frame of length >= 65536
        await ws.send_bytes(b"L" * 70000)

        # Receive large message from client
        async for msg in ws:
            if msg == "done":
                await ws.close()

    srv.ws("/ws/large", large_ws_handler)
    await srv.listen(host="127.0.0.1", port=0)

    try:
        # Test async route
        res = await ctx.http.get(f"{srv.url}/async-route", response_type="json")
        assert res == {"status": "async_ok"}

        # Connect WebSocket
        reader, writer = await asyncio.open_connection("127.0.0.1", srv.port)
        key = base64.b64encode(b"test-ws-large-key").decode("ascii")
        writer.write(
            f"GET /ws/large HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{srv.port}\r\n"
            f"Upgrade: websocket\r\n"
            f"Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            f"Sec-WebSocket-Version: 13\r\n\r\n".encode("ascii")
        )
        await writer.drain()
        await reader.readuntil(b"\r\n\r\n")

        # 1. Read medium server text frame (len 300, header 126)
        h1 = await reader.readexactly(4)
        assert h1[0] == 0x81
        assert h1[1] == 126
        len1 = int.from_bytes(h1[2:4], "big")
        data1 = await reader.readexactly(len1)
        assert data1 == b"m" * 300

        # 2. Read huge server binary frame (len 70000, header 127)
        h2 = await reader.readexactly(10)
        assert h2[0] == 0x82
        assert h2[1] == 127
        len2 = int.from_bytes(h2[2:10], "big")
        data2 = await reader.readexactly(len2)
        assert len(data2) == 70000

        # 3. Client sends 64-bit length frame to server (opcode 1, len 70000)
        client_huge = b"z" * 70000
        mask = b"\x11\x22\x33\x44"
        masked_huge = bytes(b ^ mask[i % 4] for i, b in enumerate(client_huge))
        header_huge = bytes([0x81, 0x80 | 127]) + (70000).to_bytes(8, "big") + mask
        writer.write(header_huge + masked_huge)
        await writer.drain()

        # Send done to close
        writer.write(
            bytes([0x81, 0x80 | 4])
            + mask
            + bytes(b ^ mask[i % 4] for i, b in enumerate(b"done"))
        )
        await writer.drain()

        # Read close frame
        c_h = await reader.readexactly(2)
        assert c_h[0] == 0x88
        c_len = c_h[1] & 0x7F
        if c_len > 0:
            await reader.readexactly(c_len)

        writer.close()
        await writer.wait_closed()

        # 4. Abrupt disconnect test on WebSocket
        r3, w3 = await asyncio.open_connection("127.0.0.1", srv.port)
        w3.write(
            f"GET /ws/large HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{srv.port}\r\n"
            f"Upgrade: websocket\r\n"
            f"Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            f"Sec-WebSocket-Version: 13\r\n\r\n".encode("ascii")
        )
        await w3.drain()
        await r3.readuntil(b"\r\n\r\n")
        # Abruptly close
        w3.close()
        await w3.wait_closed()
    finally:
        await srv.close()


@pytest.mark.asyncio
async def test_server_sync_ws_immediate_close():
    ctx = Context()
    srv = ServerService(ctx)

    def immediate_close_ws(ws):
        ws.closed = True

    srv.ws("/ws/immediate", immediate_close_ws)
    await srv.listen(host="127.0.0.1", port=0)

    try:
        r, w = await asyncio.open_connection("127.0.0.1", srv.port)
        key = base64.b64encode(b"test-ws-imm-key").decode("ascii")
        w.write(
            f"GET /ws/immediate HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{srv.port}\r\n"
            f"Upgrade: websocket\r\n"
            f"Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            f"Sec-WebSocket-Version: 13\r\n\r\n".encode("ascii")
        )
        await w.drain()
        await r.readuntil(b"\r\n\r\n")
        w.close()
        await w.wait_closed()
    finally:
        await srv.close()


async def _raw_get(port: int, path: str) -> bytes:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(f"GET {path} HTTP/1.1\r\nHost: localhost\r\n\r\n".encode())
    await writer.drain()
    data = await reader.read()
    writer.close()
    await writer.wait_closed()
    return data


@pytest.mark.asyncio
async def test_server_routing_extensions():
    ctx = Context()
    srv = ServerService(ctx)

    # Colon-style parameters
    srv.get("/items/:item_id", lambda req, res: res.text(req.params["item_id"]))

    # Middleware-style handler deferring past the last matching route -> 404
    async def defer(req, res, next_fn):
        await next_fn()

    srv.get("/deferred", defer)

    # Handler returning a distinct response object replaces the in-flight one
    def replace(req, res):
        out = ServerResponse()
        out.send(b"replaced", status=201)
        out.headers["X-Replaced"] = "1"
        return out

    srv.get("/replace", replace)

    # Nonstandard status code falls back to a generic reason phrase
    srv.get("/odd", lambda req, res: res.send(b"odd", status=599))
    srv.get("/odd-ok", lambda req, res: res.send(b"odd", status=299))

    await srv.listen(host="127.0.0.1", port=0)
    try:
        assert (await _raw_get(srv.port, "/items/42")).endswith(b"42")
        assert (await _raw_get(srv.port, "/deferred")).startswith(
            b"HTTP/1.1 404 Not Found"
        )
        replaced = await _raw_get(srv.port, "/replace")
        assert replaced.startswith(b"HTTP/1.1 201 Created")
        assert b"X-Replaced: 1" in replaced
        assert replaced.endswith(b"replaced")
        assert (await _raw_get(srv.port, "/odd")).startswith(b"HTTP/1.1 599 Error")
        assert (await _raw_get(srv.port, "/odd-ok")).startswith(b"HTTP/1.1 299 OK")
    finally:
        await srv.close()


@pytest.mark.asyncio
async def test_server_ws_intercept_options_and_dispose():
    ctx = Context()
    ServerService(ctx)
    scoped = ctx.intercept("server", {"routes": {"WS /live": {"tag": "scoped"}}})

    dispose = scoped.server.ws("/live", lambda ws: None, options={"base": True})
    (route,) = ctx.server.ws_routes
    assert route.options == {"base": True, "tag": "scoped"}

    dispose()
    dispose()  # idempotent
    assert ctx.server.ws_routes == []


@pytest.mark.asyncio
async def test_server_ws_unmatched_upgrade_returns_404():
    ctx = Context()
    srv = ServerService(ctx)
    await srv.listen(host="127.0.0.1", port=0)
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", srv.port)
        writer.write(
            b"GET /nowhere HTTP/1.1\r\nHost: localhost\r\n"
            b"Upgrade: websocket\r\nConnection: Upgrade\r\n\r\n"
        )
        await writer.drain()
        data = await reader.read()
        writer.close()
        await writer.wait_closed()
        assert data.startswith(b"HTTP/1.1 404 Not Found")
    finally:
        await srv.close()
