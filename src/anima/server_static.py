"""Static file delivery middleware for ServerService."""

from __future__ import annotations

import fnmatch
import mimetypes
import os
import urllib.parse
from collections.abc import Callable
from pathlib import Path
from typing import Any, ClassVar

from anima.core import Context, inject
from anima.server import ServerRequest, ServerResponse


def _match_pattern(rel_path: str, pattern: str) -> bool:
    """Check if relative path or basename matches glob pattern."""
    if fnmatch.fnmatch(rel_path, pattern):
        return True
    if pattern.startswith("**/"):
        suffix = pattern[3:]
        return (
            rel_path == suffix
            or rel_path.endswith("/" + suffix)
            or fnmatch.fnmatch(rel_path, "*" + suffix)
        )
    return fnmatch.fnmatch(os.path.basename(rel_path), pattern)


class StaticConfig:
    """Configuration options for ServerStatic."""

    def __init__(
        self,
        root: str,
        download: bool = False,
        fallthrough: bool = False,
        exclude: list[str] | None = None,
        index: list[str] | None = None,
        redirect: bool = True,
        extensions: list[str] | None = None,
        error_pages: dict[int, str] | None = None,
    ) -> None:
        self.root = root
        self.download = download
        self.fallthrough = fallthrough
        self.exclude = list(exclude or [])
        self.index = list(index if index is not None else ["index.html", "index.htm"])
        self.redirect = redirect
        self.extensions = list(extensions or [])
        self.error_pages = dict(error_pages or {})


@inject("server")
class ServerStatic:
    """Static file server plugin mounting directory delivery onto ServerService."""

    name = "server:static"
    inject: ClassVar[list[str]] = ["server"]

    def __init__(
        self,
        ctx: Context,
        config: StaticConfig | dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        self.ctx = ctx
        if isinstance(config, StaticConfig):
            self.config = config
        elif isinstance(config, dict):
            self.config = StaticConfig(
                root=config.get("root", ""),
                download=config.get("download", False),
                fallthrough=config.get("fallthrough", False),
                exclude=config.get("exclude"),
                index=config.get("index"),
                redirect=config.get("redirect", True),
                extensions=config.get("extensions"),
                error_pages=config.get("error_pages", config.get("errorPages")),
            )
        else:
            self.config = StaticConfig(
                root=kwargs.get("root", ""),
                download=kwargs.get("download", False),
                fallthrough=kwargs.get("fallthrough", False),
                exclude=kwargs.get("exclude"),
                index=kwargs.get("index"),
                redirect=kwargs.get("redirect", True),
                extensions=kwargs.get("extensions"),
                error_pages=kwargs.get("error_pages", kwargs.get("errorPages")),
            )

        # Resolve root directory
        raw_root = self.config.root
        if raw_root.startswith("file://"):
            raw_root = urllib.parse.unquote(urllib.parse.urlparse(raw_root).path)
        self.base_dir = Path(raw_root).resolve()

        self.apply(ctx)

    def _is_excluded(self, rel_path: str) -> bool:
        norm = rel_path.replace("\\", "/").strip("/")
        return any(_match_pattern(norm, pat) for pat in self.config.exclude)

    def _serve_file(self, file_path: Path, res: ServerResponse) -> ServerResponse:
        content_type, _ = mimetypes.guess_type(str(file_path))
        if not content_type:
            content_type = "application/octet-stream"

        res.status = 200
        res.reason = "OK"
        res.headers["Content-Type"] = content_type
        if self.config.download:
            res.headers["Content-Disposition"] = (
                f'attachment; filename="{file_path.name}"'
            )
        else:
            res.headers["Content-Disposition"] = f'inline; filename="{file_path.name}"'

        res.body = file_path.read_bytes()
        return res

    def apply(self, ctx: Context) -> None:
        """Register static file GET route handler."""

        async def _handle_static(
            req: ServerRequest,
            res: ServerResponse,
            next_fn: Callable[[], Any],
        ) -> ServerResponse | None:
            raw_path = urllib.parse.unquote(req.params.get("path", "")).lstrip("/")

            # Path traversal validation
            target_candidate = (self.base_dir / raw_path).resolve()
            if (
                target_candidate != self.base_dir
                and self.base_dir not in target_candidate.parents
            ):
                res.status = 403
                res.reason = "Forbidden"
                res.text("Forbidden", status=403)
                return res

            # Directory handling
            if target_candidate.is_dir():
                if not req.path.endswith("/"):
                    if self.config.redirect:
                        res.status = 301
                        res.reason = "Moved Permanently"
                        res.headers["Location"] = f"{req.path}/"
                        return res
                    res.status = 404
                    res.reason = "Not Found"
                    res.text("Not Found", status=404)
                    return res

                # Check index files in directory
                for idx_name in self.config.index:
                    idx_path = (target_candidate / idx_name).resolve()
                    if idx_path.is_file():
                        rel = str(idx_path.relative_to(self.base_dir))
                        if not self._is_excluded(rel):
                            return self._serve_file(idx_path, res)

            # Exact file handling
            if target_candidate.is_file():
                rel = str(target_candidate.relative_to(self.base_dir))
                if not self._is_excluded(rel):
                    return self._serve_file(target_candidate, res)

            # Extension fallback
            for ext in self.config.extensions:
                cand = Path(f"{target_candidate}{ext}").resolve()
                if cand.is_file():
                    rel = str(cand.relative_to(self.base_dir))
                    if not self._is_excluded(rel):
                        return self._serve_file(cand, res)

            # Error page for the resolved status (any status, anima parity).
            status = res.status
            if self.config.fallthrough:
                await next_fn()
                status = res.status
            else:
                status = 404

            error_page = self.config.error_pages.get(status)
            if error_page:
                err_file = (self.base_dir / error_page).resolve()
                if err_file.is_file():
                    self._serve_file(err_file, res)
                    res.status = status
                    res.reason = res._resolve_reason(status)
                    return res

            if not self.config.fallthrough:
                res.status = 404
                res.reason = "Not Found"
                res.text("Not Found", status=404)
            return res

        ctx.server.get("{/*path}", _handle_static)


def apply(
    ctx: Context,
    config: StaticConfig | dict[str, Any] | None = None,
    **kwargs: Any,
) -> ServerStatic:
    """Mount ServerStatic plugin onto context."""
    return ServerStatic(ctx, config, **kwargs)
