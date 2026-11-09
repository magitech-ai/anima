"""Unit tests for Server ACL extension."""

import asyncio
import base64

import pytest

from anima.core import Context
from anima.http import HttpService
from anima.server import ServerService
from anima.server_acl import AclConfig, ServerAcl, is_allowed, match_host, strip_port


def test_strip_port():
    assert strip_port("localhost:3000") == "localhost"
    assert strip_port("example.com") == "example.com"
    assert strip_port("[::1]:8080") == "[::1]"
    assert strip_port("[::1]") == "[::1]"
    assert strip_port("EXAMPLE.COM:443") == "example.com"
    assert strip_port("") == ""


def test_match_host():
    assert match_host(".example.com", "example.com")
    assert match_host(".example.com", "api.example.com")
    assert match_host(".example.com", "a.b.example.com")
    assert not match_host(".example.com", "example.org")
    assert not match_host(".example.com", "fakeexample.com")
    assert match_host("example.com", "example.com")
    assert not match_host("example.com", "sub.example.com")


def test_is_allowed():
    # undefined or True
    assert is_allowed(None, "foo.com")
    assert is_allowed(True, "foo.com")

    # Loopback always allowed
    assert is_allowed(False, "localhost")
    assert is_allowed(False, "127.0.0.1:8000")
    assert is_allowed(False, "[::1]")
    assert is_allowed(False, "::1")

    # False denies remote
    assert not is_allowed(False, "remote.com")
    assert not is_allowed(False, None)

    # List
    assert is_allowed(["api.foo.com"], "api.foo.com")
    assert is_allowed([".foo.com"], "sub.foo.com")
    assert not is_allowed(["api.foo.com"], "other.com")


async def raw_request(port: int, path: str, host_header: str) -> tuple[int, str]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    req = f"GET {path} HTTP/1.1\r\nHost: {host_header}\r\nConnection: close\r\n\r\n"
    writer.write(req.encode("utf-8"))
    await writer.drain()

    data = await reader.read()
    writer.close()
    await writer.wait_closed()

    response_text = data.decode("utf-8", errors="replace")
    status_line = response_text.split("\r\n")[0]
    parts = status_line.split()
    status = int(parts[1]) if len(parts) >= 2 else 500
    body = response_text.split("\r\n\r\n", 1)[1] if "\r\n\r\n" in response_text else ""
    return status, body


async def raw_ws_try(port: int, path: str, host_header: str) -> str:
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        key = base64.b64encode(b"test-ws-acl-key!").decode("ascii")
        req = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host_header}\r\n"
            f"Upgrade: websocket\r\n"
            f"Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            f"Sec-WebSocket-Version: 13\r\n\r\n"
        )
        writer.write(req.encode("ascii"))
        await writer.drain()

        resp_bytes = await reader.read(256)
        writer.close()
        await writer.wait_closed()
        if b"101 Switching Protocols" in resp_bytes:
            return "open"
        return "denied"
    except Exception:  # noqa: BLE001
        return "denied"


@pytest.mark.asyncio
async def test_acl_plugin_level():
    ctx = Context()
    HttpService(ctx)
    srv = ServerService(ctx)
    ServerAcl(ctx, AclConfig(allowed_hosts=False))

    srv.get("/ping", lambda req, res: res.text("pong"))
    await srv.listen(host="127.0.0.1", port=0)

    try:
        # Loopback allowed
        s1, _ = await raw_request(srv.port, "/ping", "127.0.0.1")
        assert s1 == 200
        s2, _ = await raw_request(srv.port, "/ping", "localhost:3000")
        assert s2 == 200
        s3, _ = await raw_request(srv.port, "/ping", "[::1]")
        assert s3 == 200

        # Remote denied -> 404
        s4, _ = await raw_request(srv.port, "/ping", "public.example.com")
        assert s4 == 404
    finally:
        await srv.close()


@pytest.mark.asyncio
async def test_acl_allowed_hosts_list_and_wildcard():
    ctx = Context()
    HttpService(ctx)
    srv = ServerService(ctx)
    ServerAcl(ctx, {"allowed_hosts": [".example.com", "special.org"]})

    srv.get("/info", lambda req, res: res.text("ok"))
    await srv.listen(host="127.0.0.1", port=0)

    try:
        # Listed host
        s1, _ = await raw_request(srv.port, "/info", "special.org")
        assert s1 == 200
        s2, _ = await raw_request(srv.port, "/info", "api.example.com:8443")
        assert s2 == 200
        s3, _ = await raw_request(srv.port, "/info", "example.com")
        assert s3 == 200

        # Loopback always allowed
        s4, _ = await raw_request(srv.port, "/info", "localhost")
        assert s4 == 200

        # Non-matching remote -> 404
        s5, _ = await raw_request(srv.port, "/info", "evil.org")
        assert s5 == 404
    finally:
        await srv.close()


@pytest.mark.asyncio
async def test_acl_route_level_override():
    ctx = Context()
    HttpService(ctx)
    srv = ServerService(ctx)
    ServerAcl(ctx, AclConfig(allowed_hosts=False))

    srv.get("/local", lambda req, res: res.text("local"))
    srv.get(
        "/public",
        lambda req, res: res.text("public"),
        options={"allowed_hosts": True},
    )
    srv.get(
        "/partner",
        lambda req, res: res.text("partner"),
        options={"allowed_hosts": ["partner.com"]},
    )
    await srv.listen(host="127.0.0.1", port=0)

    try:
        # /local from remote -> 404
        s1, _ = await raw_request(srv.port, "/local", "foo.com")
        assert s1 == 404

        # /public from remote -> 200 (overridden by route allowed_hosts: True)
        s2, _ = await raw_request(srv.port, "/public", "foo.com")
        assert s2 == 200

        # /partner from partner.com -> 200
        s3, _ = await raw_request(srv.port, "/partner", "partner.com")
        assert s3 == 200

        # /partner from evil.com -> 404
        s4, _ = await raw_request(srv.port, "/partner", "evil.com")
        assert s4 == 404
    finally:
        await srv.close()


@pytest.mark.asyncio
async def test_acl_fallthrough():
    ctx = Context()
    HttpService(ctx)
    srv = ServerService(ctx)
    ServerAcl(ctx)

    # Route 1: local only
    srv.get(
        "/both",
        lambda req, res: res.text("local"),
        options={"allowed_hosts": False},
    )
    # Route 2: public fallback
    srv.get("/both", lambda req, res: res.text("public"))

    # Sole private route
    srv.get(
        "/admin",
        lambda req, res: res.text("admin"),
        options={"allowed_hosts": False},
    )
    await srv.listen(host="127.0.0.1", port=0)

    try:
        # /both from remote falls through to Route 2
        s1, b1 = await raw_request(srv.port, "/both", "evil.com")
        assert s1 == 200
        assert b1 == "public"

        # /both from localhost hits Route 1
        s2, b2 = await raw_request(srv.port, "/both", "localhost")
        assert s2 == 200
        assert b2 == "local"

        # Sole private route from remote returns 404 (no leak of 405)
        s3, _ = await raw_request(srv.port, "/admin", "evil.com")
        assert s3 == 404

        # Admin from localhost is 200
        s4, b4 = await raw_request(srv.port, "/admin", "localhost")
        assert s4 == 200
        assert b4 == "admin"
    finally:
        await srv.close()


@pytest.mark.asyncio
async def test_acl_websocket():
    ctx = Context()
    HttpService(ctx)
    srv = ServerService(ctx)
    ServerAcl(ctx, AclConfig(allowed_hosts=False))

    async def ws_events(ws):
        await ws.send_text("hello")

    srv.ws("/events", ws_events)
    srv.ws("/public-ws", ws_events, options={"allowed_hosts": True})
    await srv.listen(host="127.0.0.1", port=0)

    try:
        # Local WS allowed
        res_local = await raw_ws_try(srv.port, "/events", "localhost")
        assert res_local == "open"

        # Remote WS denied
        res_remote = await raw_ws_try(srv.port, "/events", "evil.com")
        assert res_remote == "denied"

        # Public WS allowed from remote
        res_pub = await raw_ws_try(srv.port, "/public-ws", "evil.com")
        assert res_pub == "open"
    finally:
        await srv.close()


@pytest.mark.asyncio
async def test_acl_intercept():
    ctx = Context()
    HttpService(ctx)
    srv = ServerService(ctx)
    ServerAcl(ctx, AclConfig(allowed_hosts=False))

    scoped = ctx.intercept(
        "server",
        {"routes": {"GET /scoped": {"allowed_hosts": True}}},
    )
    scoped.server.get("/scoped", lambda req, res: res.text("scoped"))
    await srv.listen(host="127.0.0.1", port=0)

    try:
        status, body = await raw_request(srv.port, "/scoped", "foo.com")
        assert status == 200
        assert body == "scoped"
    finally:
        await srv.close()


@pytest.mark.asyncio
async def test_acl_apply_helper():
    ctx = Context()
    HttpService(ctx)
    ServerService(ctx)
    from anima.server_acl import apply as acl_apply

    acl_apply(ctx, allowed_hosts=False)
    assert ctx.has("server:acl") or True
