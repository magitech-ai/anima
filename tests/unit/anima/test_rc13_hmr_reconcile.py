"""Executable probes for the rc.13 programmatic HMR core.

Ground truth: `cordiverse/cordis` v4.0.0-rc.13, `packages/hmr/src/index.ts`
lines 286-301 (transaction) and 525-753 (stages), and `Hmr.Result` at 785-836.
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from anima import Context, FsService, LocalFs
from anima.hmr import HmrService, SkipReason
from anima.watcher import WatcherService

SOURCE_V1 = "VERSION = 'v1'\ndef comp(ctx, config=None):\n    return lambda: None\n"
SOURCE_V2 = "VERSION = 'v2'\ndef comp(ctx, config=None):\n    return lambda: None\n"


async def _service(ctx: Context, tmp_path: Path) -> HmrService:
    ctx.use(WatcherService)
    ctx.use(HmrService, root=tmp_path, interval=0.01)
    await ctx.flush()
    return ctx.hmr


@pytest.mark.asyncio
async def test_reconcile_commits_a_changed_module(tmp_path: Path) -> None:
    """A changed module is reimported and its fibers replaced in place."""
    (tmp_path / "rc13_hmr_one.py").write_text(SOURCE_V1, encoding="utf-8")
    sys.path.insert(0, str(tmp_path))
    try:
        module = importlib.import_module("rc13_hmr_one")
        ctx = Context()
        hmr = await _service(ctx, tmp_path)
        fiber = ctx.use(module.comp)
        await ctx.flush()

        (tmp_path / "rc13_hmr_one.py").write_text(SOURCE_V2, encoding="utf-8")
        result = await hmr.reconcile(lambda: [tmp_path])

        assert not result.aborted
        assert not result.skipped
        assert fiber in result.settled
        assert sys.modules["rc13_hmr_one"].VERSION == "v2"
        assert fiber.target is sys.modules["rc13_hmr_one"].comp
    finally:
        sys.modules.pop("rc13_hmr_one", None)
        sys.path.remove(str(tmp_path))


@pytest.mark.asyncio
async def test_reconcile_aborts_a_broken_unit(tmp_path: Path) -> None:
    """A failed import aborts only that unit and restores its old module."""
    (tmp_path / "rc13_hmr_two.py").write_text(SOURCE_V1, encoding="utf-8")
    sys.path.insert(0, str(tmp_path))
    try:
        module = importlib.import_module("rc13_hmr_two")
        ctx = Context()
        hmr = await _service(ctx, tmp_path)
        fiber = ctx.use(module.comp)
        await ctx.flush()

        (tmp_path / "rc13_hmr_two.py").write_text(
            "VERSION = 'v2'\nthis is not python\n", encoding="utf-8"
        )
        result = await hmr.reconcile(lambda: [tmp_path])

        assert len(result.aborted) == 1
        assert result.aborted[0].urls == ["rc13_hmr_two"]
        assert result.settled == []
        assert sys.modules["rc13_hmr_two"].VERSION == "v1"
        assert fiber.state.value == "ACTIVE"
    finally:
        sys.modules.pop("rc13_hmr_two", None)
        sys.path.remove(str(tmp_path))


@pytest.mark.asyncio
async def test_reconcile_skips_host_and_native_modules(tmp_path: Path) -> None:
    """Framework modules and native addons are reported as skipped."""
    ctx = Context()
    hmr = await _service(ctx, tmp_path)

    import anima

    framework = Path(anima.__file__).resolve().parent
    result = await hmr.reconcile(lambda: [framework])
    assert result.skipped
    assert {item.reason for item in result.skipped} == {SkipReason.HOST_MODULE}

    fake = ModuleType("rc13_native")
    fake.__file__ = str(tmp_path / "rc13_native.so")
    sys.modules["rc13_native"] = fake
    try:
        result = await hmr.reconcile(lambda: [tmp_path])
        assert any(item.reason is SkipReason.NATIVE_ADDON for item in result.skipped)
    finally:
        sys.modules.pop("rc13_native", None)


@pytest.mark.asyncio
async def test_reconcile_rejects_invalid_roots(tmp_path: Path) -> None:
    """Roots must be absolute paths; no roots is a no-op."""
    ctx = Context()
    hmr = await _service(ctx, tmp_path)

    empty = await hmr.reconcile(list)
    assert (empty.settled, empty.aborted, empty.skipped) == ([], [], [])
    with pytest.raises(TypeError):
        await hmr.reconcile(lambda: [42])
    with pytest.raises(ValueError):
        await hmr.reconcile(lambda: ["relative/path"])
    noop = await hmr.reconcile()
    assert noop.settled == []


@pytest.mark.asyncio
async def test_reconcile_aborts_invalid_component(tmp_path: Path) -> None:
    """A module that stops exporting a component aborts its unit."""
    (tmp_path / "rc13_hmr_three.py").write_text(SOURCE_V1, encoding="utf-8")
    sys.path.insert(0, str(tmp_path))
    try:
        module = importlib.import_module("rc13_hmr_three")
        ctx = Context()
        hmr = await _service(ctx, tmp_path)
        fiber = ctx.use(module.comp)
        await ctx.flush()

        (tmp_path / "rc13_hmr_three.py").write_text(
            "VERSION = 'v2'\ncomp = 42\n", encoding="utf-8"
        )
        result = await hmr.reconcile(lambda: [tmp_path])

        assert len(result.aborted) == 1
        assert "invalid component" in str(result.aborted[0].error)
        assert result.settled == []
        assert fiber.state.value == "ACTIVE"
    finally:
        sys.modules.pop("rc13_hmr_three", None)
        sys.path.remove(str(tmp_path))


@pytest.mark.asyncio
async def test_reconcile_ignores_disposed_fibers(tmp_path: Path) -> None:
    """A disposed fiber is not a stale component."""
    (tmp_path / "rc13_hmr_four.py").write_text(SOURCE_V1, encoding="utf-8")
    sys.path.insert(0, str(tmp_path))
    try:
        module = importlib.import_module("rc13_hmr_four")
        ctx = Context()
        hmr = await _service(ctx, tmp_path)
        fiber = ctx.use(module.comp)
        await ctx.flush()
        await fiber.dispose()

        (tmp_path / "rc13_hmr_four.py").write_text(SOURCE_V2, encoding="utf-8")
        result = await hmr.reconcile(lambda: [tmp_path])
        assert result.settled == []
    finally:
        sys.modules.pop("rc13_hmr_four", None)
        sys.path.remove(str(tmp_path))


@pytest.mark.asyncio
async def test_reconcile_handles_failed_and_disposed_settlement(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """A failed settle is observed; a fiber disposed meanwhile is not settled."""

    class Stub:
        def __init__(self, uid: int | None, fails: bool) -> None:
            self.uid = uid
            self.fails = fails

        async def await_settle(self) -> None:
            if self.fails:
                raise RuntimeError("settle failed")

    ctx = Context()
    hmr = await _service(ctx, tmp_path)
    monkeypatch.setattr(
        hmr.ctx.registry,
        "replace",
        lambda _old, _new: [Stub(1, True), Stub(None, False)],
    )
    (tmp_path / "rc13_fake_target.py").write_text(SOURCE_V1, encoding="utf-8")
    fake = ModuleType("rc13_fake_target")
    fake.__file__ = str(tmp_path / "rc13_fake_target.py")

    def comp(_c: Any, config: Any = None) -> None:
        return None

    comp.__module__ = "rc13_fake_target"
    fake.comp = comp
    monkeypatch.setitem(sys.modules, "rc13_fake_target", fake)
    ctx.use(comp)
    await ctx.flush()

    result = await hmr.reconcile(lambda: [tmp_path])
    assert [fiber.uid for fiber in result.settled] == [1]


def test_reconcile_helpers_guard_odd_modules(tmp_path: Path) -> None:
    """Isolation and path helpers tolerate modules without usable files."""
    ctx = Context()
    hmr = HmrService(ctx, {"root": str(tmp_path)})

    assert hmr._is_host_module(None) is False
    assert hmr._is_host_module(ModuleType("plain")) is False
    bad = ModuleType("bad_path")
    bad.__file__ = "\0"
    assert hmr._is_host_module(bad) is False

    class FailingResolver(ModuleType):
        @property
        def __file__(self) -> str:  # type: ignore[override]
            return "\0"

    # A str with a NUL byte makes Path.resolve() fail in _match_roots.
    match_ctx = Context()
    match_hmr = HmrService(match_ctx, {"root": str(tmp_path)})
    sys.modules["rc13_bad_file"] = bad
    try:
        assert "rc13_bad_file" not in match_hmr._match_roots({tmp_path})
    finally:
        sys.modules.pop("rc13_bad_file", None)

    hmr._restore_module("never_loaded", (None, {}))
    assert "never_loaded" not in sys.modules

    # A module without a usable file cannot be re-imported; the helper
    # still evicts it and reports the import failure to its caller.
    sys.modules["rc13_no_file"] = ModuleType("rc13_no_file")
    try:
        with pytest.raises(ModuleNotFoundError):
            hmr._reimport("rc13_no_file", sys.modules["rc13_no_file"])
    finally:
        sys.modules.pop("rc13_no_file", None)


@pytest.mark.asyncio
async def test_watch_config_validation(tmp_path: Path) -> None:
    """rc.13's watch block is validated at mount time."""
    ctx = Context()
    bad_configs = [
        {"watch": []},
        {"watch": {"enabled": "yes"}},
        {"watch": {"files": "*.py"}},
        {"watch": {"files": [1]}},
        {"watch": {"debounce": "100"}},
        {"watch": {"debounce": -1}},
    ]
    for config in bad_configs:
        with pytest.raises((TypeError, ValueError)):
            HmrService(ctx, {"root": str(tmp_path), **config})


@pytest.mark.asyncio
async def test_watch_disabled_does_not_start_watching(tmp_path: Path) -> None:
    ctx = Context()
    ctx.use(WatcherService)
    ctx.use(HmrService, {"root": str(tmp_path), "watch": {"enabled": False}})
    await ctx.flush()
    assert ctx.hmr._active is False


@pytest.mark.asyncio
async def test_watch_enabled_uses_fs_service_and_reconciles(
    tmp_path: Path, monkeypatch: Any
) -> None:
    ctx = Context()
    ctx.use(WatcherService)
    ctx.use(FsService)
    await ctx.flush()
    LocalFs(ctx)  # BaseFs mounts itself at file:
    hmr = HmrService(
        ctx,
        {
            "root": str(tmp_path),
            "watch": {"enabled": True, "files": ["*.py"], "debounce": 1},
        },
    )
    hmr.init()
    assert hmr._active is True

    seen: list[set[Path]] = []

    async def fake_reconcile(callback: Any) -> Any:
        seen.append(
            set(await callback() if inspect.isawaitable(callback()) else callback())
        )
        return None

    monkeypatch.setattr(hmr, "reconcile", fake_reconcile)
    target = tmp_path / "changed.py"
    hmr._fs_watch.emit("change", target.as_uri())
    # A rapid second change cancels the pending debounce task.
    hmr._fs_watch.emit("change", (tmp_path / "other.py").as_uri())
    await asyncio.sleep(0.05)
    assert seen == [{target, tmp_path / "other.py"}]

    pending = asyncio.ensure_future(asyncio.sleep(10))
    result = hmr._stop_fs_watch(hmr._fs_watch, pending)
    if result is not None and inspect.isawaitable(result):
        await result
    assert hmr._active is False

    class SyncWatch:
        def dispose(self) -> None:
            return None

    hmr._fs_watch = SyncWatch()
    hmr.stop()
    assert hmr._active is False

    # No pending task and an awaitable disposer are both accepted paths.
    hmr._stop_fs_watch(SyncWatch(), None)

    class AsyncWatch:
        def dispose(self) -> Any:
            async def done() -> None:
                return None

            return done()

    hmr._active = True
    hmr._fs_watch = AsyncWatch()
    hmr.stop()
    await asyncio.sleep(0)
    assert hmr._active is False

    """A transaction cannot be started from inside another one."""
    ctx = Context()
    hmr = await _service(ctx, tmp_path)

    async def callback() -> Any:
        with pytest.raises(RuntimeError, match="inside another transaction"):
            await hmr.reconcile()
        return []

    result = await hmr.reconcile(callback)
    assert result.settled == []
