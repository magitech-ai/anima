import asyncio
import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

import pytest

from anima.core import Context, FiberState
from anima.hmr import HmrService
from anima.watcher import WatcherService


@pytest.mark.asyncio
async def test_hmr_service_lifecycle_and_start_stop(tmp_path: Path):
    ctx = Context()
    ctx.use(WatcherService)
    ctx.use(HmrService, root=tmp_path, interval=0.01)
    await ctx.flush()

    hmr: HmrService = ctx.hmr
    assert hmr._active is True

    # Calling start when already active is a no-op
    hmr.start()
    assert hmr._active is True

    # Calling stop
    hmr.stop()
    assert hmr._active is False
    assert hmr._disposer is None

    # Calling stop again is safe
    hmr.stop()


@pytest.mark.asyncio
async def test_hmr_find_module_by_path(tmp_path: Path):
    ctx = Context()
    hmr = HmrService(ctx, {"root": tmp_path})

    # Create a dynamic module
    mod_path = tmp_path / "dynamic_pkg" / "mod_a.py"
    mod_path.parent.mkdir(parents=True, exist_ok=True)
    mod_path.write_text("VALUE = 10\n")

    mod_name = "test_dynamic_pkg_mod_a"
    spec = importlib.util.spec_from_file_location(mod_name, str(mod_path))
    assert spec is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    assert spec.loader is not None
    spec.loader.exec_module(mod)

    try:
        # Match via mod.__file__
        found_name, found_mod = hmr._find_module_by_path(mod_path)  # type: ignore[misc]
        assert found_name == mod_name
        assert found_mod is mod

        # Match via relative path when __file__ is None
        mod.__file__ = None  # type: ignore[assignment]
        # Register under relative module name
        rel_mod_name = "dynamic_pkg.mod_a"
        sys.modules[rel_mod_name] = mod

        found_rel_name, found_rel_mod = hmr._find_module_by_path(mod_path)  # type: ignore[misc]
        assert found_rel_name == rel_mod_name
        assert found_rel_mod is mod

        # Module with None in sys.modules
        sys.modules["none_mod"] = None  # type: ignore[assignment]
        assert hmr._find_module_by_path(mod_path) == (rel_mod_name, mod)
        sys.modules.pop("none_mod", None)

        # Module with invalid __file__ raising ValueError on Path resolve
        dummy_mod = ModuleType("bad_file_mod")
        dummy_mod.__file__ = "\0invalid_path"
        sys.modules["bad_file_mod"] = dummy_mod
        assert hmr._find_module_by_path(mod_path) == (rel_mod_name, mod)
        sys.modules.pop("bad_file_mod", None)

        # Path not in sys.modules
        assert hmr._find_module_by_path(tmp_path / "unknown.py") is None

        # Path outside root raising ValueError in relative_to
        assert hmr._find_module_by_path(Path("/other/path/mod.py")) is None

        # Exception when resolving __file__
        with patch.object(Path, "resolve", side_effect=OSError("disk error")):
            assert hmr._find_module_by_path(mod_path) is None

    finally:
        sys.modules.pop(mod_name, None)
        sys.modules.pop("dynamic_pkg.mod_a", None)


@pytest.mark.asyncio
async def test_hmr_on_file_change_filtering(tmp_path: Path):
    ctx = Context()
    hmr = HmrService(ctx, {"root": tmp_path})

    # Action not change or add
    await hmr._on_file_change("unlink", tmp_path / "foo.py")

    # Not .py suffix
    await hmr._on_file_change("change", tmp_path / "foo.txt")

    # Module not found
    await hmr._on_file_change("change", tmp_path / "untracked.py")

    # Module successfully matched and reloaded via _on_file_change
    valid_file = tmp_path / "valid_change.py"
    valid_file.write_text("X = 1\n")
    mod_name = "valid_change"
    spec = importlib.util.spec_from_file_location(mod_name, str(valid_file))
    assert spec is not None
    vmod = importlib.util.module_from_spec(spec)
    vmod.__spec__ = spec
    sys.modules[mod_name] = vmod
    assert spec.loader is not None
    spec.loader.exec_module(vmod)

    try:
        await hmr._on_file_change("change", valid_file)
    finally:
        sys.modules.pop(mod_name, None)


@pytest.mark.asyncio
async def test_hmr_reload_class_component(tmp_path: Path):
    # Write version 1 before the watcher starts, so its initial scan
    # records it silently (ignore_initial) and only the version 2 change
    # triggers a reload. Otherwise a watcher reload of version 1 can race
    # the explicit reload and emit a second lifecycle cycle.
    mod_file = tmp_path / "sample_component.py"
    mod_file.write_text(
        "class MyComponent:\n"
        "    version = 1\n"
        "    def __init__(self, ctx):\n"
        "        self.v = 1\n"
    )

    ctx = Context()
    ctx.use(WatcherService)
    ctx.use(HmrService, root=tmp_path, interval=0.01)
    await ctx.flush()

    hmr: HmrService = ctx.hmr

    mod_name = "sample_component"
    spec = importlib.util.spec_from_file_location(mod_name, str(mod_file))
    assert spec is not None
    mod = importlib.util.module_from_spec(spec)
    mod.__spec__ = spec
    sys.modules[mod_name] = mod
    assert spec.loader is not None
    spec.loader.exec_module(mod)

    events: list[tuple[str, str]] = []
    ctx.on("hmr/before-reload", lambda name, p: events.append(("before", name)))
    ctx.on("hmr/after-reload", lambda name, p: events.append(("after", name)))
    ctx.on("hmr/reload", lambda name, p: events.append(("reload", name)))

    try:
        # Mount component
        fib = ctx.use(mod.MyComponent)
        await ctx.flush()
        assert fib.state == FiberState.ACTIVE

        # Update file on disk
        import os
        import time

        mod_file.write_text(
            "class MyComponent:\n"
            "    version = 2\n"
            "    def __init__(self, ctx):\n"
            "        self.v = 2\n"
        )
        os.utime(mod_file, (time.time() + 2, time.time() + 2))

        reloaded = await hmr.reload_module(mod_name, path=mod_file)
        assert reloaded is not None
        assert reloaded.MyComponent.version == 2
        assert fib.target.version == 2

        assert [e[0] for e in events] == ["before", "after", "reload"]

        # Reload with path=None and directory already on sys.path
        sys.path.insert(0, str(tmp_path))
        try:
            reloaded2 = await hmr.reload_module(mod_name, path=None)
            assert reloaded2 is not None
        finally:
            if str(tmp_path) in sys.path:
                sys.path.remove(str(tmp_path))

    finally:
        sys.modules.pop(mod_name, None)


@pytest.mark.asyncio
async def test_hmr_reload_concurrent_deduped(tmp_path: Path):
    # Two reloads of the same module racing each other (e.g. a watcher
    # event firing while an explicit reload is in flight) share one
    # in-flight reload: lifecycle events fire exactly once.
    ctx = Context()
    hmr = HmrService(ctx, {"root": tmp_path})

    mod_file = tmp_path / "dedup_component.py"
    mod_file.write_text("VALUE = 1\n")

    mod_name = "dedup_component"
    spec = importlib.util.spec_from_file_location(mod_name, str(mod_file))
    assert spec is not None
    mod = importlib.util.module_from_spec(spec)
    mod.__spec__ = spec
    sys.modules[mod_name] = mod
    assert spec.loader is not None
    spec.loader.exec_module(mod)

    events: list[str] = []
    ctx.on("hmr/before-reload", lambda name, p: events.append("before"))
    ctx.on("hmr/after-reload", lambda name, p: events.append("after"))
    ctx.on("hmr/reload", lambda name, p: events.append("reload"))

    try:
        mod_file.write_text("VALUE = 2\n")
        # Bump mtime so the reload does not reuse cached bytecode: the
        # rewrite lands in the same second at the same size otherwise.
        import os
        import time

        os.utime(mod_file, (time.time() + 2, time.time() + 2))

        first, second = await asyncio.gather(
            hmr.reload_module(mod_name, path=mod_file),
            hmr.reload_module(mod_name, path=mod_file),
        )
        assert first is second
        assert first is not None
        assert first.VALUE == 2
        assert events == ["before", "after", "reload"]
    finally:
        sys.modules.pop(mod_name, None)


@pytest.mark.asyncio
async def test_hmr_reload_instance_and_function_components(tmp_path: Path):
    ctx = Context()
    ctx.use(WatcherService)
    hmr = HmrService(ctx, {"root": tmp_path})

    mod_file = tmp_path / "variants_component.py"
    mod_file.write_text(
        "class ServiceObj:\n"
        "    val = 1\n"
        "    def apply(self, ctx):\n"
        "        pass\n"
        "def func_component(ctx):\n"
        "    pass\n"
    )

    mod_name = "variants_component"
    spec = importlib.util.spec_from_file_location(mod_name, str(mod_file))
    assert spec is not None
    mod = importlib.util.module_from_spec(spec)
    mod.__spec__ = spec
    sys.modules[mod_name] = mod
    assert spec.loader is not None
    spec.loader.exec_module(mod)

    try:
        instance_target = mod.ServiceObj()
        fib_inst = ctx.use(instance_target)
        fib_func = ctx.use(mod.func_component)

        # Disposed fiber should be ignored
        fib_disposed = ctx.use(lambda c: None)
        await ctx.flush()
        await fib_disposed.dispose()

        # Modify fib_func target __name__ so it is not in reloaded module (triggers fallback restart)
        mod.func_component.__name__ = "non_existent_func"

        # Reload with target removed in new code (falls back to restart)
        mod_file.write_text(
            "class ServiceObj:\n"
            "    val = 2\n"
            "    def apply(self, ctx):\n"
            "        pass\n"
            "# func_component deleted\n"
        )

        reloaded = await hmr.reload_module(mod_name, mod=mod, path=mod_file)
        assert reloaded is not None
        assert fib_inst.state == FiberState.ACTIVE
        assert fib_func.state == FiberState.ACTIVE

    finally:
        sys.modules.pop(mod_name, None)


@pytest.mark.asyncio
async def test_hmr_reload_error_handling(tmp_path: Path):
    ctx = Context()
    hmr = HmrService(ctx, {"root": tmp_path})

    # Non-existent module
    assert await hmr.reload_module("non_existent_module_xyz") is None

    # Module with path=None and no __file__
    synth_mod = ModuleType("synthetic_mod")
    sys.modules["synthetic_mod"] = synth_mod
    try:
        assert (
            await hmr.reload_module("synthetic_mod", mod=synth_mod, path=None) is None
        )
    finally:
        sys.modules.pop("synthetic_mod", None)

    # Syntax error during reload
    mod_file = tmp_path / "broken_component.py"
    mod_file.write_text("VALID = 1\n")

    mod_name = "broken_component"
    spec = importlib.util.spec_from_file_location(mod_name, str(mod_file))
    assert spec is not None
    mod = importlib.util.module_from_spec(spec)
    mod.__spec__ = spec
    sys.modules[mod_name] = mod
    assert spec.loader is not None
    spec.loader.exec_module(mod)

    errors: list[Exception] = []
    ctx.on("hmr/error", lambda err, name, p: errors.append(err))

    try:
        # Write broken syntax
        mod_file.write_text("class Broken:\n  syntax error here :(\n")

        res = await hmr.reload_module(mod_name, mod=mod, path=mod_file)
        assert res is None
        assert len(errors) == 1
        assert isinstance(errors[0], SyntaxError)

    finally:
        sys.modules.pop(mod_name, None)


@pytest.mark.asyncio
async def test_hmr_reload_joins_stale_inflight_then_reloads(tmp_path: Path):
    """A reload racing a stale in-flight reload returns the current content.

    A slow effect disposer holds the first reload in flight; the file is
    updated while it runs. The second reload must not join the stale one
    (which would return version 1); it waits, then reloads version 2.
    """
    ctx = Context()
    ctx.use(WatcherService)
    ctx.use(HmrService, root=tmp_path, interval=0.01)
    await ctx.flush()

    hmr: HmrService = ctx.hmr
    mod_file = tmp_path / "sample_component.py"
    mod_file.write_text(
        "import asyncio\n"
        "class MyComponent:\n"
        "    version = 1\n"
        "    def __init__(self, ctx):\n"
        "        self.v = 1\n"
        "        ctx.effect(lambda: self._slow_disposer)\n"
        "    async def _slow_disposer(self):\n"
        "        await asyncio.sleep(0.5)\n"
    )
    mod_name = "sample_component"
    spec = importlib.util.spec_from_file_location(mod_name, str(mod_file))
    assert spec is not None
    mod = importlib.util.module_from_spec(spec)
    mod.__spec__ = spec
    sys.modules[mod_name] = mod
    assert spec.loader is not None
    spec.loader.exec_module(mod)

    try:
        fib = ctx.use(mod.MyComponent)
        await ctx.flush()
        assert fib.state == FiberState.ACTIVE

        # Start a reload of version 1; the slow disposer holds it in flight.
        first = asyncio.create_task(hmr.reload_module(mod_name, path=mod_file))
        await asyncio.sleep(0.1)
        assert hmr._inflight.get(mod_name) is not None

        # Update the file while the first reload is still running.
        import os
        import time

        mod_file.write_text(
            "class MyComponent:\n"
            "    version = 2\n"
            "    def __init__(self, ctx):\n"
            "        self.v = 2\n"
        )
        os.utime(mod_file, (time.time() + 2, time.time() + 2))

        reloaded = await hmr.reload_module(mod_name, path=mod_file)
        assert reloaded is not None
        assert reloaded.MyComponent.version == 2
        assert fib.target.version == 2
        assert await first is not None
    finally:
        sys.modules.pop(mod_name, None)


@pytest.mark.asyncio
async def test_hmr_reload_missing_file_returns_none(tmp_path: Path):
    """reload_module on a missing file returns None instead of raising."""
    ctx = Context()
    ctx.use(WatcherService)
    ctx.use(HmrService, root=tmp_path, interval=0.01)
    await ctx.flush()
    hmr: HmrService = ctx.hmr
    assert await hmr.reload_module("missing_mod", path=tmp_path / "nope.py") is None
