"""Host header and loopback access-control extension for ServerService.

Anima-original surface: not part of the ported kernel.
"""

from __future__ import annotations

from typing import Any, ClassVar

from anima.core import Context, inject
from anima.server import Route, ServerRequest

LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "[::1]"})


def strip_port(host: str) -> str:
    """Normalize and strip port component from host header."""
    h = host.lower().strip()
    if not h:
        return ""
    if h.startswith("["):
        end = h.find("]")
        return h[: end + 1] if end >= 0 else h
    # IPv6 address without brackets contains multiple colons
    if h.count(":") > 1:
        return h
    i = h.find(":")
    return h[:i] if i >= 0 else h


def match_host(pattern: str, host: str) -> bool:
    """Check if host matches pattern, supporting .example.com subdomains."""
    p = pattern.lower().strip()
    h = host.lower().strip()
    if p.startswith("."):
        return h == p[1:] or h.endswith(p)
    return p == h


def is_allowed(
    allowed: bool | list[str] | None,
    host_header: str | None,
) -> bool:
    """Determine whether request host header is authorized."""
    if allowed is None or allowed is True:
        return True
    host = strip_port(host_header or "")
    if host in LOOPBACK_HOSTS:
        return True
    if allowed is False:
        return False
    return any(match_host(p, host) for p in allowed)


class AclConfig:
    """Configuration options for ServerAcl."""

    def __init__(self, allowed_hosts: bool | list[str] | None = True) -> None:
        self.allowed_hosts = allowed_hosts


@inject("server")
class ServerAcl:
    """Access control list component enforcing allowed host header restrictions."""

    name = "server:acl"
    inject: ClassVar[list[str]] = ["server"]

    def __init__(
        self,
        ctx: Context,
        config: AclConfig | dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        self.ctx = ctx
        if isinstance(config, AclConfig):
            self.allowed_hosts = config.allowed_hosts
        elif isinstance(config, dict):
            self.allowed_hosts = config.get(
                "allowed_hosts", config.get("allowedHosts", True)
            )
        else:
            self.allowed_hosts = kwargs.get(
                "allowed_hosts", kwargs.get("allowedHosts", True)
            )

        self.apply(ctx)

    def apply(self, ctx: Context) -> None:
        """Register route-check listener onto server."""

        def _on_route_check(req: ServerRequest, route: Route) -> bool | None:
            # Route options take precedence, then component configuration
            allowed = route.options.get(
                "allowed_hosts",
                route.options.get("allowedHosts", self.allowed_hosts),
            )
            host = req.headers.get("host")
            if not is_allowed(allowed, host):
                return True  # Veto route
            return None

        ctx.on("server/route-check", _on_route_check)


def apply(
    ctx: Context,
    config: AclConfig | dict[str, Any] | None = None,
    **kwargs: Any,
) -> ServerAcl:
    """Mount ServerAcl component onto context."""
    return ServerAcl(ctx, config, **kwargs)
