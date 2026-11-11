"""Echo route middleware for ServerService."""

from __future__ import annotations

from typing import Any, ClassVar

from anima.core import Context, inject
from anima.server import ServerRequest, ServerResponse, WebSocket


class EchoConfig:
    """Configuration options for ServerEcho."""

    def __init__(self) -> None:
        pass


@inject("server")
class ServerEcho:
    """Echo component mounting POST and WebSocket echo endpoints at root."""

    name = "server:echo"
    inject: ClassVar[list[str]] = ["server"]

    def __init__(
        self,
        ctx: Context,
        config: EchoConfig | dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        self.ctx = ctx
        self.apply(ctx)

    def apply(self, ctx: Context) -> None:
        """Register echo POST and WebSocket routes at root."""

        async def _echo_post(req: ServerRequest, res: ServerResponse) -> None:
            content_type = req.headers.get("content-type")
            if content_type:
                res.headers["content-type"] = content_type
            res.body = req.body

        async def _echo_ws(req: ServerRequest, ws: WebSocket) -> None:
            async for msg in ws:
                await ws.send_text(msg)

        ctx.server.post("", _echo_post)
        ctx.server.ws("", _echo_ws)


def apply(
    ctx: Context,
    config: EchoConfig | dict[str, Any] | None = None,
    **kwargs: Any,
) -> ServerEcho:
    """Mount ServerEcho component onto context."""
    return ServerEcho(ctx, config, **kwargs)
