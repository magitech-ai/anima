"""Unit tests for HTTP SOCKS proxy extension."""

import asyncio
import socket

import pytest

from anima.core import Context
from anima.http import HttpError, HttpService
from anima.http_socks import HttpSocks
from anima.server import ServerService


class MockSocksServer:
    """In-memory mock SOCKS4/5 proxy server for testing."""

    def __init__(
        self, username: str | None = None, password: str | None = None
    ) -> None:
        self.server: asyncio.Server | None = None
        self.port: int = 0
        self.username = username
        self.password = password

    async def start(self) -> None:
        self.server = await asyncio.start_server(self._handle_client, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self.server:
            self.server.close()
            await self.server.wait_closed()

    async def _handle_client(
        self, client_r: asyncio.StreamReader, client_w: asyncio.StreamWriter
    ) -> None:
        try:
            ver = await client_r.readexactly(1)
            if ver == b"\x05":
                # SOCKS5
                nmethods_b = await client_r.readexactly(1)
                _methods = await client_r.readexactly(nmethods_b[0])

                if self.username:
                    client_w.write(b"\x05\x02")  # Username/password
                    await client_w.drain()

                    # Read auth
                    _auth_ver = await client_r.readexactly(1)
                    ulen = (await client_r.readexactly(1))[0]
                    user = (await client_r.readexactly(ulen)).decode("utf-8")
                    plen = (await client_r.readexactly(1))[0]
                    pwd = (await client_r.readexactly(plen)).decode("utf-8")

                    if user == self.username and pwd == self.password:
                        client_w.write(b"\x01\x00")
                        await client_w.drain()
                    else:
                        client_w.write(b"\x01\x01")
                        await client_w.drain()
                        client_w.close()
                        return
                else:
                    client_w.write(b"\x05\x00")  # No auth
                    await client_w.drain()

                # Read request
                req_hdr = await client_r.readexactly(4)
                _cmd, atyp = req_hdr[1], req_hdr[3]
                if atyp == 0x01:  # IPv4
                    ip_b = await client_r.readexactly(4)
                    dest_host = socket.inet_ntoa(ip_b)
                elif atyp == 0x03:  # Domain
                    dlen = (await client_r.readexactly(1))[0]
                    dest_host = (await client_r.readexactly(dlen)).decode("utf-8")
                else:
                    client_w.close()
                    return

                port_b = await client_r.readexactly(2)
                dest_port = int.from_bytes(port_b, "big")

                # Connect to target
                try:
                    target_r, target_w = await asyncio.open_connection(
                        dest_host, dest_port
                    )
                except OSError:
                    client_w.write(b"\x05\x01\x00\x01\x00\x00\x00\x00\x00\x00")
                    await client_w.drain()
                    client_w.close()
                    return

                client_w.write(b"\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00")
                await client_w.drain()

                await self._pipe(client_r, client_w, target_r, target_w)
            elif ver == b"\x04":
                # SOCKS4
                _cmd = (await client_r.readexactly(1))[0]
                port_b = await client_r.readexactly(2)
                dest_port = int.from_bytes(port_b, "big")
                ip_b = await client_r.readexactly(4)
                dest_host = socket.inet_ntoa(ip_b)
                # Read user id until 0x00
                while True:
                    ch = await client_r.readexactly(1)
                    if ch == b"\x00":
                        break

                try:
                    target_r, target_w = await asyncio.open_connection(
                        dest_host, dest_port
                    )
                except OSError:
                    client_w.write(b"\x00\x5b\x00\x00\x00\x00\x00\x00")
                    await client_w.drain()
                    client_w.close()
                    return

                client_w.write(b"\x00\x5a\x00\x00\x00\x00\x00\x00")
                await client_w.drain()

                await self._pipe(client_r, client_w, target_r, target_w)
            else:
                client_w.close()
        except Exception:  # noqa: BLE001
            client_w.close()

    async def _pipe(
        self,
        c_r: asyncio.StreamReader,
        c_w: asyncio.StreamWriter,
        t_r: asyncio.StreamReader,
        t_w: asyncio.StreamWriter,
    ) -> None:
        async def forward(src_r: asyncio.StreamReader, dst_w: asyncio.StreamWriter):
            try:
                while True:
                    data = await src_r.read(4096)
                    if not data:
                        break
                    dst_w.write(data)
                    await dst_w.drain()
            except OSError:
                pass  # peer closed mid-pipe; nothing left to forward
            finally:
                dst_w.close()

        await asyncio.gather(
            forward(c_r, t_w), forward(t_r, c_w), return_exceptions=True
        )


@pytest.mark.asyncio
async def test_socks5_unauthenticated_proxy():
    # 1. Target HTTP server
    ctx_srv = Context()
    srv = ServerService(ctx_srv)
    srv.get("/target", lambda req, res: res.text("reached-via-socks5"))
    await srv.listen(host="127.0.0.1", port=0)

    # 2. Mock SOCKS5 proxy
    socks_srv = MockSocksServer()
    await socks_srv.start()

    # 3. Client Context with HttpSocks
    ctx_client = Context()
    HttpService(ctx_client)
    HttpSocks(ctx_client)

    try:
        # Request target via socks5 proxy agent
        res = await ctx_client.http.get(
            f"{srv.url}/target",
            proxy_agent=f"socks5://127.0.0.1:{socks_srv.port}",
            response_type="text",
        )
        assert res == "reached-via-socks5"
    finally:
        await socks_srv.stop()
        await srv.close()


@pytest.mark.asyncio
async def test_socks5_authenticated_proxy():
    # 1. Target HTTP server
    ctx_srv = Context()
    srv = ServerService(ctx_srv)
    srv.get("/auth-target", lambda req, res: res.text("auth-socks-ok"))
    await srv.listen(host="127.0.0.1", port=0)

    # 2. Mock SOCKS5 proxy with auth
    socks_srv = MockSocksServer(username="myuser", password="mypassword")
    await socks_srv.start()

    # 3. Client Context
    ctx_client = Context()
    HttpService(ctx_client)
    HttpSocks(ctx_client)

    try:
        # Valid auth
        res = await ctx_client.http.get(
            f"{srv.url}/auth-target",
            proxy_agent=f"socks5h://myuser:mypassword@127.0.0.1:{socks_srv.port}",
            response_type="text",
        )
        assert res == "auth-socks-ok"

        # Invalid auth raises HttpError
        with pytest.raises(HttpError):
            await ctx_client.http.get(
                f"{srv.url}/auth-target",
                proxy_agent=f"socks5h://wronguser:wrongpass@127.0.0.1:{socks_srv.port}",
            )
    finally:
        await socks_srv.stop()
        await srv.close()


@pytest.mark.asyncio
async def test_socks4_proxy():
    # 1. Target HTTP server
    ctx_srv = Context()
    srv = ServerService(ctx_srv)
    srv.get("/socks4", lambda req, res: res.text("socks4-success"))
    await srv.listen(host="127.0.0.1", port=0)

    # 2. Mock SOCKS proxy
    socks_srv = MockSocksServer()
    await socks_srv.start()

    # 3. Client Context
    ctx_client = Context()
    HttpService(ctx_client)
    HttpSocks(ctx_client)

    try:
        res = await ctx_client.http.get(
            f"{srv.url}/socks4",
            proxy_agent=f"socks4://127.0.0.1:{socks_srv.port}",
            response_type="text",
        )
        assert res == "socks4-success"
    finally:
        await socks_srv.stop()
        await srv.close()


@pytest.mark.asyncio
async def test_socks_proxy_connection_failure():
    ctx = Context()
    HttpService(ctx)
    from anima.http_socks import apply as socks_apply

    socks_apply(ctx)

    # Dead port
    with pytest.raises(HttpError):
        await ctx.http.get(
            "http://127.0.0.1:54321/fail",
            proxy_agent="socks5://127.0.0.1:59999",
        )


@pytest.mark.asyncio
async def test_socks_post_query_and_domain():
    # Target server
    ctx_srv = Context()
    srv = ServerService(ctx_srv)
    srv.post(
        "/api/data",
        lambda req, res: res.json(
            {"q": req.query.get("k", [""])[0], "body": req.text()}
        ),
    )
    await srv.listen(host="127.0.0.1", port=0)

    socks_srv = MockSocksServer()
    await socks_srv.start()

    ctx_client = Context()
    HttpService(ctx_client)
    HttpSocks(ctx_client)

    try:
        # POST with query params and body through SOCKS5
        res = await ctx_client.http.post(
            f"http://localhost:{srv.port}/api/data",
            params={"k": "v123"},
            data="payload_data",
            proxy_agent=f"socks5://127.0.0.1:{socks_srv.port}",
            response_type="json",
        )
        assert res == {"q": "v123", "body": "payload_data"}
    finally:
        await socks_srv.stop()
        await srv.close()


async def _scripted_proxy(replies: list[bytes]) -> asyncio.Server:
    """Start a proxy that answers each client write with the next scripted reply."""

    async def handler(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        try:
            for reply in replies:
                await r.read(1024)
                w.write(reply)
                await w.drain()
        finally:
            w.close()

    return await asyncio.start_server(handler, "127.0.0.1", 0)


def _port(server: asyncio.Server) -> int:
    return server.sockets[0].getsockname()[1]


S5_OK_IPV4 = b"\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00"
HTTP_OK = b"HTTP/1.1 200 OK\r\nX-Flag\r\nContent-Length: 2\r\n\r\nok"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("scheme", "replies", "message"),
    [
        ("socks5", [b"\x04\x00"], "Invalid SOCKS5 version"),
        ("socks5", [b"\x05\xff"], "rejected authentication methods"),
        (
            "socks5",
            [b"\x05\x00", b"\x05\x01\x00\x01\x00\x00\x00\x00\x00\x00"],
            "SOCKS5 connect error code",
        ),
        ("socks4", [b"\x00\x5b\x00\x00\x00\x00\x00\x00"], "SOCKS4 server rejected"),
        ("socks5", [b"\x05\x00", S5_OK_IPV4, b""], "Empty response"),
    ],
)
async def test_socks_protocol_errors(
    scheme: str, replies: list[bytes], message: str
) -> None:
    ctx = Context()
    HttpService(ctx)
    HttpSocks(ctx)
    server = await _scripted_proxy(replies)
    try:
        with pytest.raises(HttpError) as exc:
            await ctx.http.get(
                "http://127.0.0.1:8080/test",
                proxy_agent=f"{scheme}://127.0.0.1:{_port(server)}",
            )
        assert message in str(exc.value)
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("scheme", "target", "replies"),
    [
        # IPv6 destination, domain-typed bind address in reply
        (
            "socks5",
            "http://[::1]:8080/test",
            [b"\x05\x00", b"\x05\x00\x00\x03\x04test\x1f\x90", HTTP_OK],
        ),
        # Domain destination, IPv6-typed bind address in reply
        (
            "socks5h",
            "http://example.invalid/test",
            [b"\x05\x00", b"\x05\x00\x00\x04" + b"\x00" * 18, HTTP_OK],
        ),
        # Unknown bind address type is tolerated
        (
            "socks",
            "http://127.0.0.1:8080/test",
            [b"\x05\x00", b"\x05\x00\x00\x09", HTTP_OK],
        ),
        # SOCKS4a with a domain destination
        (
            "socks4a",
            "http://example.invalid:8080/test",
            [b"\x00\x5a\x00\x00\x00\x00\x00\x00", HTTP_OK],
        ),
    ],
)
async def test_socks_address_types(
    scheme: str, target: str, replies: list[bytes]
) -> None:
    ctx = Context()
    HttpService(ctx)
    HttpSocks(ctx)
    server = await _scripted_proxy(replies)
    try:
        res = await ctx.http.get(
            target,
            headers={"Host": "ignored", "X-Custom": "1"},
            proxy_agent=f"{scheme}://127.0.0.1:{_port(server)}",
            response_type="response",
        )
        assert res.status == 200
        assert res.text() == "ok"
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_socks_https_target_tls_failure() -> None:
    """A plaintext peer behind the proxy fails the TLS upgrade loudly."""
    ctx = Context()
    HttpService(ctx)
    HttpSocks(ctx)
    server = await _scripted_proxy([b"\x05\x00", S5_OK_IPV4, b"not-tls"])
    try:
        with pytest.raises(HttpError) as exc:
            await ctx.http.get(
                "https://127.0.0.1/secure",
                proxy_agent=f"socks5://127.0.0.1:{_port(server)}",
                timeout=5.0,
            )
        assert exc.value.code == "PROXY_ERROR"
    finally:
        server.close()
        await server.wait_closed()
