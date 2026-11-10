"""Integration tests for the five auxiliary ecosystem extensions.

Exercises server:acl, server:proxy, server:static, http:socks, and cli:env
cooperating on their respective contexts, mirroring anima's explicit plugin
mounting model (each extension imported and applied explicitly).
"""

import asyncio
import os
from pathlib import Path

import pytest

from anima.cli_env import CliEnv
from anima.core import Context
from anima.http import HttpService
from anima.http_socks import HttpSocks
from anima.server import ServerService
from anima.server_acl import ServerAcl
from anima.server_proxy import ProxyConfig, ServerProxy
from anima.server_static import ServerStatic, StaticConfig


async def _raw_get(port: int, path: str, host_header: str = "127.0.0.1") -> int:
    """Return the HTTP status code for a raw request with a custom Host header."""
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(
        f"GET {path} HTTP/1.1\r\nHost: {host_header}\r\nConnection: close\r\n\r\n".encode()
    )
    await writer.drain()
    data = await reader.read()
    writer.close()
    await writer.wait_closed()
    status_line = data.decode("utf-8", errors="replace").split("\r\n")[0]
    parts = status_line.split()
    return int(parts[1]) if len(parts) >= 2 else 500


@pytest.mark.asyncio
async def test_aux_extensions_integration(tmp_path: Path):
    """All five aux extensions cooperate on their respective contexts."""
    fixtures = tmp_path / "static"
    fixtures.mkdir()
    (fixtures / "hello.txt").write_text("hello-static\n", encoding="utf-8")
    (tmp_path / ".env").write_text(
        "ANIMA_APP=integration\nANIMA_PORT=9000\n", encoding="utf-8"
    )

    # --- Context A: cli:env + server:static + server:acl ---
    ctx_a = Context()
    env_fib = ctx_a.plugin(CliEnv, base_dir=tmp_path)
    await env_fib.load()
    HttpService(ctx_a)
    srv_a = ServerService(ctx_a)
    ServerAcl(ctx_a, {"allowed_hosts": [".example.com"]})
    ServerStatic(ctx_a, StaticConfig(root=str(fixtures)))
    await srv_a.listen(host="127.0.0.1", port=0)

    try:
        # cli:env loaded onto ctx.env and os.environ
        assert ctx_a.env.get("ANIMA_APP") == "integration"
        assert os.environ.get("ANIMA_APP") == "integration"

        # static file served over http (loopback host always allowed by acl)
        text = await ctx_a.http.get(f"{srv_a.url}/hello.txt", response_type="text")
        assert text == "hello-static\n"

        # server:acl: loopback request OK
        assert await _raw_get(srv_a.port, "/hello.txt", "127.0.0.1") == 200
        # server:acl: remote host blocked
        assert await _raw_get(srv_a.port, "/hello.txt", "evil.com") == 404
    finally:
        await env_fib.dispose()
        await srv_a.close()

    # --- Context B: server:proxy + http:socks ---
    ctx_up = Context()
    HttpService(ctx_up)
    srv_up = ServerService(ctx_up)
    srv_up.get("/api/hello", lambda req, res: res.text("proxied-ok"))
    await srv_up.listen(host="127.0.0.1", port=0)

    ctx_b = Context()
    HttpService(ctx_b)
    srv_b = ServerService(ctx_b)
    ServerProxy(ctx_b, ProxyConfig(base_url=srv_up.url))
    HttpSocks(ctx_b)
    await srv_b.listen(host="127.0.0.1", port=0)

    try:
        # http:socks: all SOCKS schemes registered as connectors
        for scheme in ("socks", "socks4", "socks4a", "socks5", "socks5h"):
            assert scheme in ctx_b.http._proxies

        # server:proxy: forwards to upstream
        text = await ctx_b.http.get(f"{srv_b.url}/api/hello", response_type="text")
        assert text == "proxied-ok"
    finally:
        await srv_b.close()
        await srv_up.close()
