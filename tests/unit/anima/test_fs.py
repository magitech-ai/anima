"""Probes for the rc.13 `fs` service port (packages/fs, packages/fs-node)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from anima import Context
from anima.fs import (
    BaseFs,
    FsError,
    FsService,
    LocalFs,
    Watcher,
    WatchOptions,
)


class Provider:
    def __init__(self) -> None:
        self.watchers: list[Watcher] = []
        self.disposed = 0

    def watch(self, watcher: Watcher) -> Any:
        self.watchers.append(watcher)
        return lambda: setattr(self, "disposed", self.disposed + 1)


class NoWatch:
    pass


async def _ctx() -> Context:
    ctx = Context()
    ctx.use(FsService)
    await ctx.flush()
    return ctx


def test_windows_drive_letter_is_not_a_uri_scheme() -> None:
    """A ``C:\\`` drive prefix must be treated as a path, not a scheme."""
    from anima.fs import _is_uri, _location_text, _normalize_prefix

    assert _is_uri("file:///tmp/x") is True
    assert _is_uri("C:\\tmp\\x") is False
    assert _normalize_prefix("C:").startswith("file:")
    assert _location_text("C:\\tmp\\x").startswith("file:")


def test_fs_error_and_watch_options() -> None:
    error = FsError("NoProvider", "file:///x", "no provider")
    assert (error.code, error.location, str(error)) == (
        "NoProvider",
        "file:///x",
        "no provider",
    )
    assert WatchOptions().filter is None


@pytest.mark.asyncio
async def test_mount_rejects_duplicate_and_routes_longest_prefix(
    tmp_path: Path,
) -> None:
    ctx = await _ctx()
    first = Provider()
    second = Provider()
    ctx.fs.mount("file:", first)
    with pytest.raises(FsError, match="already mounted"):
        ctx.fs.mount("file:", second)

    sub = tmp_path / "sub"
    sub.mkdir()
    ctx.fs.mount(sub, second)
    assert ctx.fs._match(tmp_path.as_uri()).provider is first
    assert ctx.fs._match(sub.as_uri()).provider is second
    assert ctx.fs._match("s3:") is None
    with pytest.raises(FsError) as info:
        ctx.fs.watch("s3:object")
    assert info.value.code == "NoProvider"


@pytest.mark.asyncio
async def test_resolve_handles_uris_absolute_and_relative(tmp_path: Path) -> None:
    ctx = await _ctx()
    uri = tmp_path.as_uri()
    assert ctx.fs.resolve(uri) == uri
    assert ctx.fs.resolve(tmp_path) == uri

    ctx.baseUrl = str(tmp_path)
    assert ctx.fs.resolve("child") == (tmp_path / "child").as_uri()
    ctx.baseUrl = None
    with pytest.raises(TypeError, match="without ctx.baseUrl"):
        ctx.fs.resolve("child")


@pytest.mark.asyncio
async def test_watch_lifecycle_and_events(tmp_path: Path) -> None:
    ctx = await _ctx()
    provider = Provider()
    ctx.fs.mount("file:", provider)
    watcher = ctx.fs.watch(tmp_path)
    assert provider.watchers == [watcher]

    events: list[Any] = []
    watcher.on("change", lambda url: events.append(url))
    watcher.on("ready", lambda: events.append("ready"))
    watcher.emit("ready")
    watcher.emit("ready")
    watcher.emit("change", "file:///x")
    await asyncio.sleep(0)
    assert events == ["ready", "file:///x"]

    with pytest.raises(ValueError, match="unknown watch event"):
        watcher.on("bogus", lambda: None)

    # A ready listener added after the first ready is called asynchronously.
    late: list[str] = []
    watcher.on("ready", lambda: late.append("late"))
    await asyncio.sleep(0)
    assert late == ["late"]

    # Errors are logged when no error listener exists, then dispatched.
    watcher.emit("error", RuntimeError("boom"))
    errors: list[Any] = []
    watcher.on("error", errors.append)
    watcher.emit("error", RuntimeError("again"))
    await asyncio.sleep(0)
    assert len(errors) == 1

    await watcher.dispose()
    assert provider.disposed == 1
    watcher.emit("change", "file:///ignored")
    await asyncio.sleep(0)
    assert "file:///ignored" not in events


@pytest.mark.asyncio
async def test_listener_failure_is_reported_as_error(tmp_path: Path) -> None:
    ctx = await _ctx()
    ctx.fs.mount("file:", Provider())
    watcher = ctx.fs.watch(tmp_path)
    errors: list[Any] = []
    watcher.on("error", errors.append)

    def broken(_url: str) -> None:
        raise RuntimeError("listener failed")

    watcher.on("change", broken)
    watcher.emit("change", "file:///x")
    await asyncio.sleep(0.01)
    assert len(errors) == 1


@pytest.mark.asyncio
async def test_watch_requires_provider_support(tmp_path: Path) -> None:
    ctx = await _ctx()
    ctx.fs.mount("file:", NoWatch())  # type: ignore[arg-type]
    with pytest.raises(FsError) as info:
        ctx.fs.watch(tmp_path)
    assert info.value.code == "NotSupported"


@pytest.mark.asyncio
async def test_mount_changes_reroute_existing_watches(tmp_path: Path) -> None:
    ctx = await _ctx()
    first = Provider()
    second = Provider()
    root_mount = ctx.fs.mount("file:", first)
    watcher = ctx.fs.watch(tmp_path)
    assert provider_of(ctx, watcher) is first

    sub_mount = ctx.fs.mount(tmp_path, second)
    await asyncio.sleep(0.01)
    assert provider_of(ctx, watcher) is second
    assert first.disposed == 1

    sub_mount()
    await asyncio.sleep(0.01)
    assert provider_of(ctx, watcher) is first

    root_mount()
    await asyncio.sleep(0.01)
    assert watcher in ctx.fs._watchers
    assert ctx.fs._watchers[watcher] is None

    # A watch with no provider is resumed when one is mounted again.
    resumed = Provider()
    ctx.fs.mount("file:", resumed)
    await asyncio.sleep(0.01)
    assert provider_of(ctx, watcher) is resumed


class AsyncProvider:
    def __init__(self) -> None:
        self.disposed = 0

    def watch(self, watcher: Watcher) -> Any:
        async def dispose() -> None:
            self.disposed += 1

        return dispose


def provider_of(ctx: Context, watcher: Watcher) -> Any:
    return ctx.fs._watchers[watcher].mount.provider


@pytest.mark.asyncio
async def test_reroute_reports_unsupported_provider(tmp_path: Path) -> None:
    ctx = await _ctx()
    ctx.fs.mount("file:", Provider())
    watcher = ctx.fs.watch(tmp_path)
    errors: list[Any] = []
    watcher.on("error", errors.append)

    # Disposing the mount suspends the watch; mounting a provider without
    # watch support at the same prefix then reports a NotSupported error.
    for mount in list(ctx.fs._mounts):
        ctx.fs._mounts.pop(mount)
    ctx.fs._reroute()
    await asyncio.sleep(0.01)
    ctx.fs.mount("file:", NoWatch())  # type: ignore[arg-type]
    await asyncio.sleep(0.01)
    assert any(getattr(error, "code", None) == "NotSupported" for error in errors)


def test_base_fs_requires_mount() -> None:
    ctx = Context()
    with pytest.raises(TypeError, match="mount point"):
        BaseFs(ctx, {})


@pytest.mark.asyncio
async def test_local_fs_watch_reports_changes_with_filter(tmp_path: Path) -> None:
    ctx = Context()
    ctx.use(FsService)
    ctx.use(LocalFs)
    await ctx.flush()
    watcher = ctx.fs.watch(tmp_path, WatchOptions(filter=["*.txt"]))
    ready: list[str] = []
    changed: list[str] = []
    watcher.on("ready", lambda: ready.append("ready"))
    watcher.on("change", changed.append)

    await asyncio.sleep(0.1)
    assert ready == ["ready"]
    target = tmp_path / "a.txt"
    target.write_text("one")
    await asyncio.sleep(0.15)
    assert any(url.endswith("a.txt") for url in changed)
    await watcher.dispose()


@pytest.mark.asyncio
async def test_local_fs_watch_file_and_callable_filter(tmp_path: Path) -> None:
    target = tmp_path / "file.txt"
    target.write_text("one")
    ctx = Context()
    ctx.use(FsService)
    ctx.use(LocalFs)
    await ctx.flush()
    watcher = ctx.fs.watch(target, WatchOptions(filter=lambda path, is_dir: True))
    changed: list[str] = []
    watcher.on("change", changed.append)
    await asyncio.sleep(0.1)
    target.write_text("two")
    await asyncio.sleep(0.15)
    assert changed
    await watcher.dispose()


@pytest.mark.asyncio
async def test_watcher_disposer_async_listener_and_closed_state(tmp_path: Path) -> None:
    ctx = await _ctx()
    ctx.fs.mount("file:", AsyncProvider())
    watcher = ctx.fs.watch(tmp_path)
    seen: list[str] = []

    async def listener(url: str) -> None:
        seen.append(url)

    off = watcher.on("change", listener)
    # Mark a hook removed without deleting it, to exercise the skip path.
    watcher._hooks["change"][0]["removed"] = True
    watcher.emit("change", "file:///skipped")
    await asyncio.sleep(0)
    assert seen == []
    watcher._hooks["change"][0]["removed"] = False
    off()
    off()
    off = watcher.on("change", listener)
    watcher.emit("change", "file:///a")
    await asyncio.sleep(0)
    assert seen == ["file:///a"]
    off()
    watcher.emit("change", "file:///b")
    await asyncio.sleep(0)
    assert seen == ["file:///a"]

    errors: list[Any] = []
    off_error = watcher.on("error", errors.append)
    watcher._hooks["error"][0]["removed"] = True
    watcher.emit("error", RuntimeError("skipped"))
    await asyncio.sleep(0)
    assert errors == []
    watcher._hooks["error"][0]["removed"] = False
    watcher.emit("error", RuntimeError("one"))
    await asyncio.sleep(0)
    assert len(errors) == 1
    off_error()
    watcher.emit("error", RuntimeError("two"))
    await asyncio.sleep(0)
    assert len(errors) == 1

    await watcher.dispose()
    watcher._error(RuntimeError("after close"))
    # _match accepts plain paths as well as URIs.
    assert ctx.fs._match(str(tmp_path)) is not None


@pytest.mark.asyncio
async def test_reroute_unchanged_and_dispose_without_connection(
    tmp_path: Path,
) -> None:
    ctx = await _ctx()
    ctx.fs.mount("file:", Provider())
    watcher = ctx.fs.watch(tmp_path)
    # A reroute that changes nothing skips every watch.
    ctx.fs._reroute()
    # Removing the connection first exercises the no-connection dispose path.
    ctx.fs._watchers.pop(watcher)
    await watcher.dispose()


@pytest.mark.asyncio
async def test_local_fs_missing_root_and_dispose(tmp_path: Path) -> None:
    ctx = Context()
    ctx.use(FsService)
    ctx.use(LocalFs)
    await ctx.flush()
    watcher = ctx.fs.watch(tmp_path / "missing")
    await asyncio.sleep(0.05)
    await watcher.dispose()


@pytest.mark.asyncio
async def test_local_fs_filter_none_and_stat_failure(
    tmp_path: Path, monkeypatch: Any
) -> None:
    (tmp_path / "a.txt").write_text("one")
    ctx = Context()
    ctx.use(FsService)
    ctx.use(LocalFs)
    await ctx.flush()

    # A callable filter that excludes everything leaves the scan empty.
    watcher = ctx.fs.watch(tmp_path, WatchOptions(filter=lambda *_: False))
    await asyncio.sleep(0.05)
    await watcher.dispose()

    # With no filter, a path whose stat fails is skipped.
    original = Path.stat

    def failing(self: Path, *args: Any, **kwargs: Any) -> Any:
        if self.name == "a.txt":
            raise OSError("gone")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", failing)
    watcher = ctx.fs.watch(tmp_path)
    await asyncio.sleep(0.05)
    await watcher.dispose()


@pytest.mark.asyncio
async def test_reroute_awaits_async_disposer(tmp_path: Path) -> None:
    ctx = await _ctx()
    first = AsyncProvider()
    ctx.fs.mount("file:", first)
    ctx.fs.watch(tmp_path)
    ctx.fs.mount(tmp_path, AsyncProvider())
    await asyncio.sleep(0.01)
    assert first.disposed == 1
