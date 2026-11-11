"""SOCKS4 and SOCKS5 proxy support extension for HttpService."""

from __future__ import annotations

import asyncio
import socket
import ssl
import urllib.parse
from typing import Any, ClassVar

from anima.core import Context, inject
from anima.http import HttpError, HttpRequest, HttpResponse


class SocksProxyHandler:
    """SOCKS protocol client establishing forwarded TCP streams."""

    def __init__(self, proxy_url: urllib.parse.ParseResult) -> None:
        self.proxy_url = proxy_url
        self.scheme = proxy_url.scheme.lower()
        self.proxy_host = proxy_url.hostname or "127.0.0.1"
        self.proxy_port = proxy_url.port or 1080
        self.username = proxy_url.username
        self.password = proxy_url.password

    async def __call__(self, current_req: HttpRequest) -> HttpResponse:
        target = urllib.parse.urlparse(current_req.url)
        dest_host = target.hostname or "127.0.0.1"
        is_https = target.scheme.lower() in ("https", "wss")
        dest_port = target.port or (443 if is_https else 80)

        # Note: socks4/socks5 resolve the destination locally before
        # handing it to the proxy; socks4a/socks/socks5h send the hostname to
        # the proxy for remote resolution. Literal IP addresses are used as-is.
        should_lookup = self.scheme in ("socks4", "socks5")
        if should_lookup:
            try:
                socket.inet_aton(dest_host)
            except OSError:
                try:
                    socket.inet_pton(socket.AF_INET6, dest_host)
                except OSError:
                    # Domain name: resolve locally to an IPv4 address.
                    try:
                        dest_host = str(
                            socket.getaddrinfo(dest_host, None, socket.AF_INET)[0][4][0]
                        )
                    except OSError as exc:  # pragma: no cover
                        raise HttpError(
                            f"Failed to resolve destination {dest_host}: {exc}",
                            code="PROXY_ERROR",
                        ) from exc

        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(self.proxy_host, self.proxy_port),
                timeout=current_req.timeout,
            )
        except Exception as e:
            raise HttpError(
                f"Failed to connect to SOCKS proxy at {self.proxy_host}:{self.proxy_port}: {e}",
                code="PROXY_ERROR",
            ) from e

        try:
            if self.scheme in ("socks4", "socks4a"):
                await self._handshake_socks4(reader, writer, dest_host, dest_port)
            else:
                await self._handshake_socks5(reader, writer, dest_host, dest_port)

            # Optional TLS upgrade for https target
            if is_https:
                await writer.start_tls(
                    ssl.create_default_context(), server_hostname=dest_host
                )

            # Send HTTP request
            path = target.path or "/"
            if target.query:
                path = f"{path}?{target.query}"

            skipped = {"host", "connection", "content-length"}
            req_lines = [f"{current_req.method} {path} HTTP/1.1"]
            req_lines.append(f"Host: {target.netloc}")
            for hk, hv in current_req.headers.items():
                if hk.lower() not in skipped:
                    req_lines.append(f"{hk}: {hv}")
            if current_req.data:
                req_lines.append(f"Content-Length: {len(current_req.data)}")
            req_lines.append("Connection: close\r\n\r\n")

            writer.write("\r\n".join(req_lines).encode("utf-8"))
            if current_req.data:
                writer.write(current_req.data)
            await writer.drain()

            # Read HTTP response status line
            status_line = (await reader.readline()).decode("utf-8").strip()
            if not status_line:
                raise HttpError(
                    "Empty response from destination server", code="EMPTY_RESPONSE"
                )

            parts = status_line.split(" ", 2)
            status = int(parts[1]) if len(parts) >= 2 else 500
            reason = parts[2] if len(parts) >= 3 else "Error"

            # Read headers
            hdrs: dict[str, str] = {}
            while True:
                line = await reader.readline()
                if not line or line in (b"\r\n", b"\n"):
                    break
                decoded = line.decode("utf-8").strip()
                if ":" in decoded:
                    hk, hv = decoded.split(":", 1)
                    hdrs[hk.strip().lower()] = hv.strip()

            body = await reader.read()
            return HttpResponse(
                status=status,
                reason=reason,
                headers=hdrs,
                data=body,
                url=current_req.url,
            )
        except HttpError:
            raise
        except Exception as e:
            raise HttpError(
                f"SOCKS communication error: {e}", code="PROXY_ERROR"
            ) from e
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (OSError, ConnectionResetError):  # pragma: no cover
                pass

    async def _handshake_socks4(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        dest_host: str,
        dest_port: int,
    ) -> None:
        try:
            ip_packed = socket.inet_aton(dest_host)
            is_4a = False
        except OSError:
            # SOCKS4a for domain names
            ip_packed = b"\x00\x00\x00\x01"
            is_4a = True

        user_b = (self.username or "").encode("utf-8")
        packet = (
            b"\x04\x01" + dest_port.to_bytes(2, "big") + ip_packed + user_b + b"\x00"
        )
        if is_4a:
            packet += dest_host.encode("utf-8") + b"\x00"

        writer.write(packet)
        await writer.drain()

        resp = await reader.readexactly(8)
        if resp[1] != 0x5A:  # 90 is success
            raise HttpError(
                f"SOCKS4 server rejected connection with status 0x{resp[1]:02x}"
            )

    async def _handshake_socks5(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        dest_host: str,
        dest_port: int,
    ) -> None:
        # Step 1: Greeting
        if self.username:
            greeting = b"\x05\x02\x00\x02"  # No Auth + Username/Password
        else:
            greeting = b"\x05\x01\x00"  # No Auth

        writer.write(greeting)
        await writer.drain()

        method_resp = await reader.readexactly(2)
        if method_resp[0] != 0x05:
            raise HttpError(f"Invalid SOCKS5 version: {method_resp[0]}")

        chosen_method = method_resp[1]
        if chosen_method == 0x02:
            # Username/password auth (RFC 1929)
            u_b = (self.username or "").encode("utf-8")
            p_b = (self.password or "").encode("utf-8")
            auth_packet = (
                b"\x01"
                + len(u_b).to_bytes(1, "big")
                + u_b
                + len(p_b).to_bytes(1, "big")
                + p_b
            )
            writer.write(auth_packet)
            await writer.drain()

            auth_resp = await reader.readexactly(2)
            if auth_resp[1] != 0x00:
                raise HttpError("SOCKS5 authentication failed", code="AUTH_FAILED")
        elif chosen_method != 0x00:
            raise HttpError(
                f"SOCKS5 server rejected authentication methods: 0x{chosen_method:02x}"
            )

        # Step 2: Connect request
        try:
            ip_packed = socket.inet_aton(dest_host)
            addr_type = 0x01
            addr_data = ip_packed
        except OSError:
            try:
                ip6_packed = socket.inet_pton(socket.AF_INET6, dest_host)
                addr_type = 0x04
                addr_data = ip6_packed
            except OSError:
                host_b = dest_host.encode("utf-8")
                addr_type = 0x03
                addr_data = len(host_b).to_bytes(1, "big") + host_b

        req = (
            b"\x05\x01\x00"
            + bytes([addr_type])
            + addr_data
            + dest_port.to_bytes(2, "big")
        )
        writer.write(req)
        await writer.drain()

        # Step 3: Response
        resp_hdr = await reader.readexactly(4)
        if resp_hdr[1] != 0x00:
            raise HttpError(f"SOCKS5 connect error code: 0x{resp_hdr[1]:02x}")

        atyp = resp_hdr[3]
        if atyp == 0x01:
            await reader.readexactly(4 + 2)
        elif atyp == 0x03:
            dlen = (await reader.readexactly(1))[0]
            await reader.readexactly(dlen + 2)
        elif atyp == 0x04:
            await reader.readexactly(16 + 2)


@inject("http")
class HttpSocks:
    """Plugin registering SOCKS proxy connectors with HttpService."""

    name = "http:socks"
    inject: ClassVar[list[str]] = ["http"]

    def __init__(self, ctx: Context, **kwargs: Any) -> None:
        self.ctx = ctx
        self.apply(ctx)

    def apply(self, ctx: Context) -> None:
        """Register SOCKS schemes onto ctx.http."""
        protocols = ["socks", "socks4", "socks4a", "socks5", "socks5h"]
        ctx.http.proxy(protocols, lambda url: SocksProxyHandler(url))


def apply(ctx: Context, config: dict[str, Any] | None = None) -> HttpSocks:
    """Mount HttpSocks plugin onto context."""
    return HttpSocks(ctx)
