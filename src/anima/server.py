"""Asynchronous HTTP and WebSocket server engine for Anima microkernel."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import inspect
import json
import os
import re
import urllib.parse
from collections.abc import AsyncIterator, Callable
from http import HTTPStatus
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

    def _resolve_reason(self, status: int) -> str:
        try:
            return HTTPStatus(status).phrase
        except ValueError:
            return "OK" if status < 400 else "Error"

    def text(self, content: str, status: int = 200) -> None:
        """Set response body as plain text."""
        self.status = status
        self.reason = self._resolve_reason(status)
        self.headers["Content-Type"] = "text/plain; charset=utf-8"
        self.body = content.encode("utf-8")

    def json(self, data: Any, status: int = 200) -> None:
        """Set response body as JSON."""
        self.status = status
        self.reason = self._resolve_reason(status)
        self.headers["Content-Type"] = "application/json; charset=utf-8"
        self.body = json.dumps(data).encode("utf-8")

    def send(self, body: bytes, status: int = 200) -> None:
        """Set response body as raw bytes."""
        self.status = status
        self.reason = self._resolve_reason(status)
        self.body = body


class WebSocket:
    """RFC 6455 WebSocket connection wrapper supporting server and client roles."""

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        is_client: bool = False,
    ) -> None:
        self.reader = reader
        self.writer = writer
        self.is_client = is_client
        self.closed = False
        self.close_code: int | None = None

    async def send_text(self, message: str) -> None:
        """Send a text frame."""
        if self.closed:
            return
        payload = message.encode("utf-8")
        frame = self._build_frame(0x01, payload)
        self.writer.write(frame)
        await self.writer.drain()

    async def send_bytes(self, data: bytes) -> None:
        """Send a binary frame."""
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
        mask_bit = 0x80 if self.is_client else 0x00

        if length < 126:
            header.append(mask_bit | length)
        elif length < 65536:
            header.append(mask_bit | 126)
            header.extend([length >> 8, length & 0xFF])
        else:
            header.append(mask_bit | 127)
            header.extend(length.to_bytes(8, byteorder="big"))

        if self.is_client:
            mask_key = os.urandom(4)
            header.extend(mask_key)
            masked_payload = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))
            return bytes(header) + masked_payload
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
                self.close_code = int.from_bytes(payload[:2], byteorder="big")
                await self.close()
                break
            elif opcode == 0x09:  # Ping
                pong = self._build_frame(0x0A, payload)
                self.writer.write(pong)
                await self.writer.drain()
            elif opcode == 0x01:  # Text frame
                yield payload.decode("utf-8")


class Route:
    """HTTP route definition with regex matcher, wildcards, and parameter extraction."""

    def __init__(
        self,
        method: str,
        path_spec: str,
        handler: Callable[..., Any],
        options: dict[str, Any] | None = None,
    ) -> None:
        self.method = method.upper()
        # An empty path spec names the root path, matching the reference behavior.
        if path_spec == "":
            path_spec = "/"
        self.path_spec = path_spec
        self.handler = handler
        self.options: dict[str, Any] = dict(options or {})
        self.param_names: list[str] = []

        pattern = r"(/\{/\*[a-zA-Z_][a-zA-Z0-9_]*\}|\{/\*[a-zA-Z_][a-zA-Z0-9_]*\}|<[a-zA-Z_][a-zA-Z0-9_]*>|:[a-zA-Z_][a-zA-Z0-9_]*)"
        parts = re.split(pattern, path_spec)
        regex_parts: list[str] = ["^"]
        for part in parts:
            if not part:
                continue
            if "/*" in part and part.endswith("}"):
                p_name = part.split("*")[1].rstrip("}")
                self.param_names.append(p_name)
                regex_parts.append(r"(?:/(.*))?")
            elif part.startswith("<") and part.endswith(">"):
                p_name = part[1:-1]
                self.param_names.append(p_name)
                regex_parts.append(r"([^/]+)")
            elif part.startswith(":"):
                p_name = part[1:]
                self.param_names.append(p_name)
                regex_parts.append(r"([^/]+)")
            else:
                regex_parts.append(re.escape(part))
        regex_parts.append("$")
        self.pattern = re.compile("".join(regex_parts))

    def match(self, path: str) -> dict[str, str] | None:
        """Test if path matches route pattern, returning extracted parameters."""
        m = self.pattern.match(path)
        if not m:
            return None
        return {
            name: (val if val is not None else "")
            for name, val in zip(self.param_names, m.groups(), strict=False)
        }


class WsRoute(Route):
    """WebSocket route definition with pattern matching and parameter extraction."""

    def __init__(
        self,
        path_spec: str,
        handler: Callable[..., Any],
        options: dict[str, Any] | None = None,
    ) -> None:
        super().__init__("WS", path_spec, handler, options=options)


class ServerService(Service):
    """Contextual HTTP and WebSocket server engine."""

    def __init__(self, ctx: Context, name: str = "server") -> None:
        super().__init__(ctx, name)
        self.routes: list[Route] = []
        self.ws_routes: list[WsRoute] = []
        self.server: asyncio.Server | None = None
        self.host: str = "127.0.0.1"
        self.port: int = 0
        self.url: str = ""
        self.proxy: Any = None

    def route(
        self,
        method: str,
        path: str,
        handler: Callable[..., Any],
        options: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> Disposer:
        """Register an HTTP route."""
        opts = dict(options or {})
        opts.update(kwargs)
        intercept = self.resolve_intercept()
        route_intercept = intercept.get("routes", {}).get(f"{method.upper()} {path}")
        if route_intercept and isinstance(route_intercept, dict):
            opts.update(route_intercept)

        r = Route(method, path, handler, options=opts)
        self.routes.append(r)

        def _dispose() -> None:
            if r in self.routes:
                self.routes.remove(r)

        self.ctx.effect(lambda: _dispose)
        return _dispose

    def get(
        self,
        path: str,
        handler: Callable[..., Any],
        options: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> Disposer:
        """Register a GET route."""
        return self.route("GET", path, handler, options=options, **kwargs)

    def post(
        self,
        path: str,
        handler: Callable[..., Any],
        options: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> Disposer:
        """Register a POST route."""
        return self.route("POST", path, handler, options=options, **kwargs)

    def put(
        self,
        path: str,
        handler: Callable[..., Any],
        options: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> Disposer:
        """Register a PUT route."""
        return self.route("PUT", path, handler, options=options, **kwargs)

    def patch(
        self,
        path: str,
        handler: Callable[..., Any],
        options: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> Disposer:
        """Register a PATCH route."""
        return self.route("PATCH", path, handler, options=options, **kwargs)

    def delete(
        self,
        path: str,
        handler: Callable[..., Any],
        options: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> Disposer:
        """Register a DELETE route."""
        return self.route("DELETE", path, handler, options=options, **kwargs)

    def head(
        self,
        path: str,
        handler: Callable[..., Any],
        options: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> Disposer:
        """Register a HEAD route."""
        return self.route("HEAD", path, handler, options=options, **kwargs)

    def all(
        self,
        path: str,
        handler: Callable[..., Any],
        options: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> Disposer:
        """Register a route for all HTTP methods."""
        return self.route("ALL", path, handler, options=options, **kwargs)

    def ws(
        self,
        path: str,
        handler: Callable[..., Any],
        options: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> Disposer:
        """Register a WebSocket endpoint."""
        opts = dict(options or {})
        opts.update(kwargs)
        intercept = self.resolve_intercept()
        route_intercept = intercept.get("routes", {}).get(f"WS {path}")
        if route_intercept and isinstance(route_intercept, dict):
            opts.update(route_intercept)

        r = WsRoute(path, handler, options=opts)
        self.ws_routes.append(r)

        def _dispose() -> None:
            if r in self.ws_routes:
                self.ws_routes.remove(r)

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
            if headers.get("upgrade", "").lower() == "websocket":
                matched_ws: WsRoute | None = None
                ws_req: ServerRequest | None = None

                for candidate in self.ws_routes:
                    params = candidate.match(path)
                    if params is not None:
                        req_candidate = ServerRequest(
                            method, path, headers, b"", query, params
                        )
                        is_vetoed = self.ctx.bail(
                            "server/route-check", req_candidate, candidate
                        )
                        if is_vetoed:
                            continue
                        matched_ws = candidate
                        ws_req = req_candidate
                        break

                if matched_ws is not None and ws_req is not None:
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

                    ws = WebSocket(reader, writer, is_client=False)
                    handler = matched_ws.handler
                    sig = inspect.signature(handler)
                    if len(sig.parameters) >= 2:
                        res = handler(ws_req, ws)
                    else:
                        res = handler(ws)

                    if inspect.isawaitable(res):
                        await res
                    else:
                        while not ws.closed:
                            await asyncio.sleep(0.01)
                    return
                else:
                    not_found_resp = (
                        "HTTP/1.1 404 Not Found\r\n"
                        "Content-Type: text/plain; charset=utf-8\r\n"
                        "Content-Length: 9\r\n"
                        "Connection: close\r\n\r\n"
                        "Not Found"
                    )
                    writer.write(not_found_resp.encode("utf-8"))
                    await writer.drain()
                    return

            # Read HTTP body
            content_length = int(headers.get("content-length", 0))
            body = (
                await reader.readexactly(content_length) if content_length > 0 else b""
            )

            req = ServerRequest(method, path, headers, body, query)
            res = ServerResponse()

            # Route resolution with veto check and fallthrough
            matching_routes: list[tuple[Route, dict[str, str]]] = []
            path_exists_other_method = False

            for r in self.routes:
                params = r.match(path)
                if params is not None:
                    candidate_req = ServerRequest(
                        method, path, headers, body, query, params
                    )
                    is_vetoed = self.ctx.bail("server/route-check", candidate_req, r)
                    if is_vetoed:
                        continue
                    if r.method in (method.upper(), "ALL"):
                        matching_routes.append((r, params))
                    else:
                        path_exists_other_method = True

            if not matching_routes:
                if path_exists_other_method:
                    res.text("Method Not Allowed", status=405)
                else:
                    res.text("Not Found", status=404)
            else:

                async def _run_routes(route_index: int) -> None:
                    if route_index >= len(matching_routes):
                        res.text("Not Found", status=404)
                        return

                    current_r, current_params = matching_routes[route_index]
                    req.params = current_params

                    async def _next() -> None:
                        await _run_routes(route_index + 1)

                    sig = inspect.signature(current_r.handler)
                    if len(sig.parameters) >= 3:
                        handler_res = current_r.handler(req, res, _next)
                    else:
                        handler_res = current_r.handler(req, res)

                    if inspect.isawaitable(handler_res):
                        handler_res = await handler_res

                    # A handler may return a distinct ServerResponse to replace
                    # the in-flight response wholesale.
                    if (
                        isinstance(handler_res, ServerResponse)
                        and handler_res is not res
                    ):
                        res.status = handler_res.status
                        res.reason = handler_res.reason
                        res.headers.update(handler_res.headers)
                        res.body = handler_res.body

                await self.ctx.waterfall(
                    "server/request",
                    req,
                    res,
                    default_handler=lambda rq, rs: _run_routes(0),
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
