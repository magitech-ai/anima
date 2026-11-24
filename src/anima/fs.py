"""The rc.13 filesystem service: mountable providers and routed watches.

Python port of ``packages/fs`` and ``packages/fs-node`` from Cordis v4.0.0-rc.13.
Locations are absolute paths (or ``file:`` URI strings); providers are mounted
at a scheme such as ``file:`` or at a directory prefix, and each operation goes
to the provider with the longest matching prefix.
"""

from __future__ import annotations

import asyncio
import fnmatch
import logging
import os
import re
import urllib.parse
import urllib.request
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar, Protocol, runtime_checkable

from anima.core import Context, Service

logger = logging.getLogger("anima.fs")


def _walk_tree(base: Path) -> Iterator[Path]:
    """Yield descendants of base, following symlinked directories.

    ``Path.rglob`` does not descend into symlinked directories on Python
    3.12, which makes files behind a link invisible to the watcher. Track
    directory identities so a link cycle terminates.
    """
    visited: set[tuple[int, int]] = set()

    def descend(directory: Path) -> Iterator[Path]:
        try:
            stat = directory.stat()
        except OSError:
            return
        identity = (stat.st_dev, stat.st_ino)
        if identity in visited:
            return
        visited.add(identity)
        try:
            with os.scandir(directory) as entries:
                children = [Path(entry.path) for entry in entries]
        except OSError:
            return
        for path in children:
            yield path
            try:
                if path.is_dir():
                    yield from descend(path)
            except OSError:  # pragma: no cover - broken link/vanished path
                continue

    yield from descend(base)


_SCHEME = re.compile(r"^[a-zA-Z][a-zA-Z\d+.-]*:$")
_DRIVE = re.compile(r"^[a-zA-Z]:")
_WATCH_EVENTS = ("change", "ready", "error")


def _is_uri(text: str) -> bool:
    """A URI scheme, but not a Windows drive letter such as ``C:\\tmp``."""
    return bool(re.match(r"^[a-zA-Z][a-zA-Z\d+.-]*:", text) and not _DRIVE.match(text))


class FsError(Exception):
    """A filesystem-service error with a stable code (upstream FsError)."""

    def __init__(self, code: str, location: str, message: str) -> None:
        self.code = code
        self.location = location
        super().__init__(message)


@dataclass
class WatchOptions:
    """Options for a watch; ``filter`` keeps entries below a directory."""

    filter: list[str] | Callable[[str, bool], bool] | None = None


@runtime_checkable
class FsProvider(Protocol):
    """A provider mounted at a location; optional ``watch`` serves watches."""

    def watch(self, watcher: Watcher) -> Callable[[], Awaitable[None] | None]: ...


@dataclass
class Mount:
    prefix: str
    provider: FsProvider


@dataclass
class Connection:
    mount: Mount
    dispose: Callable[[], Awaitable[None] | None]


def _normalize_prefix(prefix: str | Path) -> str:
    """Normalize a scheme or a directory prefix for routing."""
    text = str(prefix)
    if _SCHEME.match(text) and not _DRIVE.match(text):
        return text.lower()
    return Path(text).resolve().as_uri().rstrip("/") + "/"


def _location_text(path: str | Path) -> str:
    """Turn a location into the string used for prefix matching."""
    text = str(path)
    if _is_uri(text):
        return text
    return Path(text).resolve().as_uri()


class Watcher:
    """A watch on a location; it ends with the fiber that created it."""

    def __init__(
        self,
        ctx: Context,
        url: str,
        options: WatchOptions,
        connect: Callable[[Watcher], Callable[[], Awaitable[None] | None]],
    ) -> None:
        self.ctx = ctx
        self.url = url
        self.options = options
        self._hooks: dict[str, list[dict[str, Any]]] = {
            name: [] for name in _WATCH_EVENTS
        }
        self._ready = False
        self._closed = False
        self.dispose = ctx.effect(lambda: connect(self), label="ctx.fs.watch()")

    def on(self, name: str, listener: Callable[..., Any]) -> Callable[[], None]:
        """Add a listener for change, ready or error; return a disposer."""
        if name not in self._hooks:
            raise ValueError(f"unknown watch event {name!r}")
        hook: dict[str, Any] = {"listener": listener, "removed": False}
        self._hooks[name].append(hook)
        if name == "ready" and self._ready:
            self._call(listener, ())

        def dispose() -> None:
            hook["removed"] = True
            if hook in self._hooks[name]:
                self._hooks[name].remove(hook)

        return dispose

    def emit(self, name: str, *args: Any) -> None:
        """Report an event, as the serving provider does."""
        if self._closed:
            return
        if name == "error":
            self._error(args[0] if args else None)
            return
        if name == "ready":
            if self._ready:
                return
            self._ready = True
        for hook in list(self._hooks[name]):
            if not hook["removed"]:
                self._call(hook["listener"], args)

    def _call(self, listener: Callable[..., Any], args: tuple[Any, ...]) -> None:
        async def run() -> None:
            try:
                result = listener(*args)
                if asyncio.iscoroutine(result) or isinstance(result, asyncio.Future):
                    await result
            except Exception as exc:  # noqa: BLE001 - listener errors are reported
                self._error(exc)

        asyncio.get_running_loop().create_task(run())

    def _error(self, error: Any) -> None:
        if self._closed:
            return
        hooks = self._hooks["error"]
        if not hooks:
            logger.warning("fs watch error: %s", error)
            return
        for hook in list(hooks):
            if not hook["removed"]:
                self._call(hook["listener"], (error,))


class FsService(Service):
    """Forward filesystem operations to the provider of the longest prefix."""

    def __init__(self, ctx: Context) -> None:
        super().__init__(ctx, "fs")
        self._mounts: dict[str, Mount] = {}
        self._watchers: dict[Watcher, Connection | None] = {}

    def mount(
        self, prefix: str | Path, provider: FsProvider
    ) -> Callable[[], Awaitable[None] | None]:
        """Mount ``provider`` at a scheme or directory prefix."""
        key = _normalize_prefix(prefix)

        def setup() -> Callable[[], Awaitable[None] | None]:
            if key in self._mounts:
                raise FsError(
                    "AlreadyMounted", key, f"a provider is already mounted at {key}"
                )
            self._mounts[key] = Mount(key, provider)
            self._reroute()

            def teardown() -> None:
                self._mounts.pop(key, None)
                self._reroute()

            return teardown

        return self.ctx.effect(setup, label="ctx.fs.mount()")

    def resolve(self, path: str | Path) -> str:
        """Resolve a location against ``ctx.baseUrl`` for relative references."""
        text = str(path)
        if _is_uri(text):
            return text
        candidate = Path(text)
        if not candidate.is_absolute():
            base = getattr(self.ctx, "baseUrl", None)
            if not base:
                raise TypeError(
                    f"cannot resolve the relative reference {text!r} without ctx.baseUrl"
                )
            candidate = Path(base) / candidate
        return candidate.resolve().as_uri()

    def _match(self, url: str) -> Mount | None:
        found: Mount | None = None
        location = _location_text(url)
        for mount in self._mounts.values():
            if mount.prefix.endswith(":"):
                matches = location.startswith(mount.prefix)
            else:
                matches = location.startswith(mount.prefix.rstrip("/"))
            if matches and (found is None or len(mount.prefix) > len(found.prefix)):
                found = mount
        return found

    def _route(self, url: str) -> Mount:
        mount = self._match(url)
        if mount is None:
            raise FsError("NoProvider", url, f"no provider is mounted for {url}")
        return mount

    def _connect(self, watcher: Watcher, mount: Mount) -> None:
        provider = mount.provider
        watch = getattr(provider, "watch", None)
        if watch is None:
            raise FsError(
                "NotSupported",
                watcher.url,
                f"the provider mounted at {mount.prefix} does not support watch()",
            )
        self._watchers[watcher] = Connection(mount, watch(watcher))

    async def _disconnect(self, watcher: Watcher) -> None:
        connection = self._watchers.get(watcher)
        if connection is None:
            return
        self._watchers[watcher] = None
        result = connection.dispose()
        if result is not None:
            await result

    def _reroute(self) -> None:
        for watcher, connection in list(self._watchers.items()):
            mount = self._match(watcher.url)
            unchanged = (
                mount is connection.mount if connection is not None else mount is None
            )
            if unchanged:
                continue
            # Disconnect fully before connecting the new provider: doing both
            # at once would let the old disposer clear the new connection.
            asyncio.get_running_loop().create_task(self._switch(watcher, mount))

    async def _switch(self, watcher: Watcher, mount: Mount | None) -> None:
        await self._disconnect(watcher)
        if mount is None:
            logger.debug("watch of %s suspended without a provider", watcher.url)
            return
        try:
            self._connect(watcher, mount)
        except FsError as exc:
            watcher.emit("error", exc)

    def watch(self, path: str | Path, options: WatchOptions | None = None) -> Watcher:
        """Watch a file or directory, routed to its mounted provider."""
        url = self.resolve(path)
        mount = self._route(url)

        def connect(watcher: Watcher) -> Callable[[], Awaitable[None]]:
            self._connect(watcher, mount)

            async def dispose() -> None:
                watcher._closed = True
                connection = self._watchers.pop(watcher, None)
                if connection is not None:
                    result = connection.dispose()
                    if result is not None:
                        await result

            return dispose

        return Watcher(self.ctx, url, options or WatchOptions(), connect)


class BaseFs:
    """A provider that mounts itself at ``config.mount`` or its static mount."""

    inject: ClassVar[list[str]] = ["fs"]
    mount: ClassVar[str | None] = None

    def __init__(self, ctx: Context, config: dict[str, Any] | None = None) -> None:
        mount = (config or {}).get("mount", self.mount)
        if not mount:
            raise TypeError("expected a mount point in the config")
        self.ctx = ctx
        self.fs: FsService = ctx.fs
        self.fs.mount(mount, self)


class LocalFs(BaseFs):
    """The local file system, mounted at ``file:`` by default."""

    mount = "file:"

    def watch(self, watcher: Watcher) -> Callable[[], None]:
        parsed = urllib.parse.urlsplit(watcher.url)
        root = Path(urllib.request.url2pathname(parsed.path)).resolve()
        keep = watcher.options.filter
        interval = 0.05
        states: dict[Path, tuple[float, int]] = {}

        def included(path: Path) -> bool:
            if keep is None:
                return True
            relative = path.relative_to(root).as_posix()
            if callable(keep):
                return keep(relative, path.is_dir())
            return any(fnmatch.fnmatch(relative, pattern) for pattern in keep)

        def scan() -> dict[Path, tuple[float, int]]:
            result: dict[Path, tuple[float, int]] = {}
            if not root.exists():
                return result
            paths = [root] if root.is_file() else list(_walk_tree(root))
            for path in paths:
                if not included(path):
                    continue
                try:
                    stat = path.stat()
                except OSError:
                    continue
                result[path] = (stat.st_mtime, stat.st_size)
            return result

        async def poll() -> None:
            nonlocal states
            states = scan()
            watcher.emit("ready")
            while True:
                await asyncio.sleep(interval)
                current = scan()
                for path, stat in current.items():
                    if path not in states or states[path] != stat:
                        watcher.emit("change", path.as_uri())
                states = current

        task = asyncio.get_running_loop().create_task(poll())

        def dispose() -> None:
            task.cancel()

        return dispose
