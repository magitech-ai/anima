"""Reverse proxy route middleware for ServerService forwarding via HttpService."""

from __future__ import annotations

import asyncio
import urllib.parse
from typing import Any, ClassVar

from anima.core import Context, inject
from anima.http import HttpError
from anima.server import ServerRequest, ServerResponse, WebSocket


def _sanitize_close_code(code: int) -> int:
    """Sanitize a WebSocket close code per RFC 6455 (anima parity).

    Only 1000 and codes in [3000, 5000) are valid for user code; anything
    else is normalized to 1000.
    """
    if code == 1000 or 3000 <= code < 5000:
        return code
    return 1000


class ProxyConfig:
    """Configuration options for ServerProxy."""

    def __init__(self, base_url: str = "") -> None:
        self.base_url = base_url


@inject("http", "server")
class ServerProxy:
    """Reverse proxy component mounting wildcard proxying onto ServerService."""

    name = "server:proxy"
    inject: ClassVar[list[str]] = ["http", "server"]

    def __init__(
        self,
        ctx: Context,
        config: ProxyConfig | dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        self.ctx = ctx
        self._target_base_url: str
        if isinstance(config, ProxyConfig):
            self._target_base_url = config.base_url
        elif isinstance(config, dict):
            self._target_base_url = str(
                config.get("base_url", config.get("baseUrl", ""))
            )
        else:
            self._target_base_url = str(
                kwargs.get("base_url", kwargs.get("baseUrl", ""))
            )

        self.apply(ctx)

    @property
    def base_url(self) -> str:
        """Return the host server base URL or target upstream URL."""
        server = self.ctx.get("server")
        url = getattr(server, "url", None)
        if isinstance(url, str) and url:
            return url
        return self._target_base_url

    def apply(self, ctx: Context) -> None:
        """Register proxy routes onto ctx.server."""
        ctx.server.proxy = self
        upstream_base = (
            self._target_base_url.rstrip("/") + "/" if self._target_base_url else ""
        )

        async def _proxy_http(
            req: ServerRequest, res: ServerResponse
        ) -> ServerResponse:
            rel_path = req.params.get("path", "").lstrip("/")
            target_url = urllib.parse.urljoin(upstream_base, rel_path)
            if req.query:
                parsed = urllib.parse.urlparse(target_url)
                qs = urllib.parse.urlencode(
                    [(k, v) for k, values in req.query.items() for v in values]
                )
                target_url = urllib.parse.urlunparse(
                    (
                        parsed.scheme,
                        parsed.netloc,
                        parsed.path,
                        parsed.params,
                        qs,
                        parsed.fragment,
                    )
                )

            hop_by_hop = {
                "host",
                "content-length",
                "connection",
                "accept-encoding",
                "content-encoding",
            }
            fwd_headers = {
                k: v for k, v in req.headers.items() if k.lower() not in hop_by_hop
            }

            body_methods = {"POST", "PUT", "PATCH"}
            data = req.body if req.method in body_methods and req.body else None

            resp = await self.ctx.http.request(
                req.method,
                target_url,
                headers=fwd_headers,
                data=data,
                response_type="response",
                validate_status=lambda s: True,
            )

            res.status = resp.status
            res.reason = resp.reason
            # undici already decoded the body; these headers no longer match
            # the forwarded bytes (anima parity).
            for hk, hv in resp.headers.items():
                if hk.lower() not in {"content-encoding", "content-length"}:
                    res.headers[hk] = hv
            res.body = resp.data
            return res

        async def _proxy_ws(req: ServerRequest, downstream: WebSocket) -> None:
            rel_path = req.params.get("path", "").lstrip("/")
            target_url = urllib.parse.urljoin(upstream_base, rel_path)
            if req.query:
                parsed = urllib.parse.urlparse(target_url)
                qs = urllib.parse.urlencode(
                    [(k, v) for k, values in req.query.items() for v in values]
                )
                target_url = urllib.parse.urlunparse(
                    (
                        parsed.scheme,
                        parsed.netloc,
                        parsed.path,
                        parsed.params,
                        qs,
                        parsed.fragment,
                    )
                )

            if target_url.startswith("https:"):
                target_ws_url = "wss:" + target_url[6:]
            elif target_url.startswith("http:"):
                target_ws_url = "ws:" + target_url[5:]
            else:
                target_ws_url = target_url

            hop_by_hop = {
                "host",
                "connection",
                "upgrade",
                "sec-websocket-key",
                "sec-websocket-version",
                "sec-websocket-extensions",
                "sec-websocket-accept",
                "content-length",
            }
            fwd_headers = {
                k: v for k, v in req.headers.items() if k.lower() not in hop_by_hop
            }

            try:
                upstream_ws = await self.ctx.http.ws(target_ws_url, headers=fwd_headers)
            except (TimeoutError, HttpError, OSError):
                await downstream.close(1011)
                return

            async def fwd_down_to_up() -> None:
                try:
                    async for msg in downstream:
                        await upstream_ws.send_text(msg)
                finally:
                    await upstream_ws.close(
                        _sanitize_close_code(downstream.close_code or 1000)
                    )

            async def fwd_up_to_down() -> None:
                try:
                    async for msg in upstream_ws:
                        await downstream.send_text(msg)
                finally:
                    await downstream.close(
                        _sanitize_close_code(upstream_ws.close_code or 1000)
                    )

            await asyncio.gather(
                fwd_down_to_up(), fwd_up_to_down(), return_exceptions=True
            )

        ctx.server.all("{/*path}", _proxy_http)
        ctx.server.ws("{/*path}", _proxy_ws)


def apply(
    ctx: Context,
    config: ProxyConfig | dict[str, Any] | None = None,
    **kwargs: Any,
) -> ServerProxy:
    """Mount ServerProxy component onto context."""
    return ServerProxy(ctx, config, **kwargs)
