"""Asynchronous HTTP and WebSocket server engine for Anima microkernel."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import inspect
import json
import re
import urllib.parse
from collections.abc import AsyncIterator, Callable
from typing import Any

from anima.core import Context, Disposer, Service

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class ServerRequest:
    """Incoming HTTP request encapsulation."""

    def __init__(
        self,
        method: str,
        path: str,
        headers: dict[str, str],
        body: bytes,
        query: dict[str, list[str]],
        params: dict[str, str] | None = None,
    ) -> None:
        self.method = method.upper()
        self.path = path
        self.headers = headers
        self.body = body
        self.query = query
        self.params: dict[str, str] = params if params is not None else {}

    def text(self, encoding: str = "utf-8") -> str:
        """Decode request body as text."""
        return self.body.decode(encoding)

    def json(self) -> Any:
        """Decode request body as JSON."""
        return json.loads(self.text())


class ServerResponse:
    """Outgoing HTTP response builder."""

    def __init__(self) -> None:
        self.status = 200
        self.reason = "OK"
        self.headers: dict[str, str] = {
            "Content-Type": "text/plain; charset=utf-8",
            "Connection": "close",
        }
        self.body: bytes = b""

    def text(self, content: str, status: int = 200) -> None:
        """Set response body as plain text."""
        self.status = status
        self.reason = "OK" if status < 400 else "Error"
        self.headers["Content-Type"] = "text/plain; charset=utf-8"
        self.body = content.encode("utf-8")

    def json(self, data: Any, status: int = 200) -> None:
        """Set response body as JSON."""
        self.status = status
        self.reason = "OK" if status < 400 else "Error"
        self.headers["Content-Type"] = "application/json; charset=utf-8"
        self.body = json.dumps(data).encode("utf-8")

    def send(self, body: bytes, status: int = 200) -> None:
        """Set response body as raw bytes."""
        self.status = status
        self.reason = "OK" if status < 400 else "Error"
        self.body = body


class WebSocket:
    """RFC 6455 WebSocket connection wrapper."""

    def __init__(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self.reader = reader
        self.writer = writer
        self.closed = False

    async def send_text(self, message: str) -> None:
        """Send an unmasked text frame to client."""
        if self.closed:
            return
        payload = message.encode("utf-8")
        frame = self._build_frame(0x01, payload)
        self.writer.write(frame)
        await self.writer.drain()

    async def send_bytes(self, data: bytes) -> None:
        """Send an unmasked binary frame to client."""
        if self.closed:
            return
        frame = self._build_frame(0x02, data)
        self.writer.write(frame)
        await self.writer.drain()

    async def close(self, code: int = 1000) -> None:
        """Send close frame and shutdown connection."""
        if self.closed:
            return
        self.closed = True
        payload = bytes([code >> 8, code & 0xFF])
        frame = self._build_frame(0x08, payload)
        self.writer.write(frame)
        try:
            await self.writer.drain()
        except (OSError, ConnectionResetError):  # pragma: no cover
            pass
        self.writer.close()
        try:
            await self.writer.wait_closed()
        except (OSError, ConnectionResetError):  # pragma: no cover
            pass

    def _build_frame(self, opcode: int, payload: bytes) -> bytes:
        header = bytearray()
        # FIN bit set, no RSVP
        header.append(0x80 | (opcode & 0x0F))
        length = len(payload)
        if length < 126:
            header.append(length)
        elif length < 65536:
            header.append(126)
            header.extend([length >> 8, length & 0xFF])
        else:
            header.append(127)
            header.extend(length.to_bytes(8, byteorder="big"))
        return bytes(header) + payload

    async def __aiter__(self) -> AsyncIterator[str]:
        """Asynchronously iterate incoming text frames until closed."""
        while not self.closed:
            try:
                head = await self.reader.readexactly(2)
            except (asyncio.IncompleteReadError, ConnectionResetError):
                break

            opcode = head[0] & 0x0F
            is_masked = bool(head[1] & 0x80)
            length = head[1] & 0x7F

            if length == 126:
                ext_len = await self.reader.readexactly(2)
                length = int.from_bytes(ext_len, byteorder="big")
            elif length == 127:
                ext_len = await self.reader.readexactly(8)
                length = int.from_bytes(ext_len, byteorder="big")

            mask_key = b""
            if is_masked:
                mask_key = await self.reader.readexactly(4)

            payload = await self.reader.readexactly(length)
            if is_masked:
                payload = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))

            if opcode == 0x08:  # Close frame
                await self.close()
                break
            elif opcode == 0x09:  # Ping
                pong = self._build_frame(0x0A, payload)
                self.writer.write(pong)
                await self.writer.drain()
            elif opcode == 0x01:  # Text frame
                yield payload.decode("utf-8")


class Route:
    """HTTP route definition with regex matcher and parameter extraction."""

    def __init__(
        self,
        method: str,
        path_spec: str,
        handler: Callable[..., Any],
    ) -> None:
        self.method = method.upper()
        self.path_spec = path_spec
        self.handler = handler
        self.param_names: list[str] = []

        # Convert <param> syntax to regex named groups
        regex_parts: list[str] = ["^"]
        tokens = [t for t in re.split(r"(<[^>]+>)", path_spec) if t]
        for tok in tokens:
            if tok.startswith("<") and tok.endswith(">"):
                p_name = tok[1:-1]
                self.param_names.append(p_name)
                regex_parts.append(r"([^/]+)")
            else:
                regex_parts.append(re.escape(tok))
        regex_parts.append("$")
        self.pattern = re.compile("".join(regex_parts))

    def match(self, path: str) -> dict[str, str] | None:
        """Test if path matches route pattern, returning extracted parameters."""
        m = self.pattern.match(path)
        if not m:
            return None
        return dict(zip(self.param_names, m.groups(), strict=False))


class ServerService(Service):
    """Contextual HTTP and WebSocket server engine."""

    def __init__(self, ctx: Context, name: str = "server") -> None:
        super().__init__(ctx, name)
        self.routes: list[Route] = []
        self.ws_routes: dict[str, Callable[[WebSocket], Any]] = {}
        self.server: asyncio.Server | None = None
        self.host: str = "127.0.0.1"
        self.port: int = 0
        self.url: str = ""

    def route(self, method: str, path: str, handler: Callable[..., Any]) -> Disposer:
        """Register an HTTP route."""
        r = Route(method, path, handler)
        self.routes.append(r)

        def _dispose() -> None:
            if r in self.routes:
                self.routes.remove(r)

        self.ctx.effect(lambda: _dispose)
        return _dispose

    def get(self, path: str, handler: Callable[..., Any]) -> Disposer:
        """Register a GET route."""
        return self.route("GET", path, handler)

    def post(self, path: str, handler: Callable[..., Any]) -> Disposer:
        """Register a POST route."""
        return self.route("POST", path, handler)

    def put(self, path: str, handler: Callable[..., Any]) -> Disposer:
        """Register a PUT route."""
        return self.route("PUT", path, handler)

    def patch(self, path: str, handler: Callable[..., Any]) -> Disposer:
        """Register a PATCH route."""
        return self.route("PATCH", path, handler)

    def delete(self, path: str, handler: Callable[..., Any]) -> Disposer:
        """Register a DELETE route."""
        return self.route("DELETE", path, handler)

    def head(self, path: str, handler: Callable[..., Any]) -> Disposer:
        """Register a HEAD route."""
        return self.route("HEAD", path, handler)

    def all(self, path: str, handler: Callable[..., Any]) -> Disposer:
        """Register a route for all HTTP methods."""
        return self.route("ALL", path, handler)

    def ws(self, path: str, handler: Callable[[WebSocket], Any]) -> Disposer:
        """Register a WebSocket endpoint."""
        self.ws_routes[path] = handler

        def _dispose() -> None:
            self.ws_routes.pop(path, None)

        self.ctx.effect(lambda: _dispose)
        return _dispose

    async def listen(self, host: str = "127.0.0.1", port: int = 0) -> None:
        """Start listening for incoming HTTP and WebSocket connections."""
        self.host = host
        self.server = await asyncio.start_server(self._handle_client, host, port)
        sock = self.server.sockets[0]
        self.port = sock.getsockname()[1]
        self.url = f"http://{host}:{self.port}"

        self.ctx.effect(lambda: self.close)

    async def close(self) -> None:
        """Stop and tear down the server."""
        if self.server:
            self.server.close()
            await self.server.wait_closed()
            self.server = None

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Handle incoming TCP client connection."""
        try:
            line_bytes = await reader.readline()
            if not line_bytes:
                writer.close()
                return

            req_line = line_bytes.decode("utf-8").strip()
            parts = req_line.split()
            if len(parts) < 2:
                writer.close()
                return

            method, full_path = parts[0], parts[1]
            headers: dict[str, str] = {}

            while True:
                hdr_line = await reader.readline()
                if not hdr_line or hdr_line == b"\r\n":
                    break
                decoded_hdr = hdr_line.decode("utf-8").strip()
                if ":" in decoded_hdr:
                    k, v = decoded_hdr.split(":", 1)
                    headers[k.strip().lower()] = v.strip()

            parsed_url = urllib.parse.urlparse(full_path)
            path = parsed_url.path
            query = urllib.parse.parse_qs(parsed_url.query)

            # Check WebSocket upgrade
            if (
                headers.get("upgrade", "").lower() == "websocket"
                and path in self.ws_routes
            ):
                key = headers.get("sec-websocket-key", "")
                accept_val = base64.b64encode(
                    hashlib.sha1((key + WS_GUID).encode("ascii")).digest()
                ).decode("ascii")

                upgrade_resp = (
                    "HTTP/1.1 101 Switching Protocols\r\n"
                    "Upgrade: websocket\r\n"
                    "Connection: Upgrade\r\n"
                    f"Sec-WebSocket-Accept: {accept_val}\r\n\r\n"
                )
                writer.write(upgrade_resp.encode("ascii"))
                await writer.drain()

                ws = WebSocket(reader, writer)
                handler = self.ws_routes[path]
                res = handler(ws)
                if inspect.isawaitable(res):
                    await res
                else:
                    while not ws.closed:
                        await asyncio.sleep(0.01)
                return

            # Read HTTP body
            content_length = int(headers.get("content-length", 0))
            body = (
                await reader.readexactly(content_length) if content_length > 0 else b""
            )

            req = ServerRequest(method, path, headers, body, query)
            res = ServerResponse()

            # Route resolution
            matched_route: Route | None = None
            path_exists = False

            for r in self.routes:
                params = r.match(path)
                if params is not None:
                    path_exists = True
                    if r.method in (method.upper(), "ALL"):
                        matched_route = r
                        req.params = params
                        break

            if matched_route is None:
                if path_exists:
                    res.status = 405
                    res.reason = "Method Not Allowed"
                    res.text("Method Not Allowed", status=405)
                else:
                    res.status = 404
                    res.reason = "Not Found"
                    res.text("Not Found", status=404)
            else:

                async def _run_route(
                    current_req: ServerRequest, current_res: ServerResponse
                ) -> None:
                    handler_res = matched_route.handler(current_req, current_res)
                    if inspect.isawaitable(handler_res):
                        await handler_res

                await self.ctx.waterfall(
                    "server/request",
                    req,
                    res,
                    default_handler=_run_route,
                )

            # Write HTTP response
            res.headers["Content-Length"] = str(len(res.body))
            resp_lines = [
                f"HTTP/1.1 {res.status} {res.reason}",
            ]
            for hk, hv in res.headers.items():
                resp_lines.append(f"{hk}: {hv}")
            resp_lines.append("\r\n")

            writer.write("\r\n".join(resp_lines).encode("utf-8") + res.body)
            await writer.drain()
        except (OSError, ConnectionResetError):  # pragma: no cover
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (OSError, ConnectionResetError):  # pragma: no cover
                pass
