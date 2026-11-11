"""Fiber epoch machine and lifecycle races (Phase 1, chunk P1-1).

Every transition of the fiber lifecycle is driven here and asserted with the
same state sequence the implementation produces: PENDING -> LOADING -> ACTIVE,
ACTIVE -> UNLOADING -> PENDING, and the FAILED recovery path via update().
"""

import asyncio

import pytest
from pydantic import BaseModel

from anima.core import Context, Fiber, FiberState, Service, symbols


class _StrictConfig(BaseModel):
    num: int


def _strict_plugin_class():
    """A plugin class with a validated config, built fresh per test."""

    class StrictPlugin:
        Config = _StrictConfig

        def __init__(self, c: Context, config=None):
            self.config = config

    return StrictPlugin


@pytest.mark.asyncio
async def test_reload_reruns_body_on_active_fiber():
    """_reload executes the runner directly, so restart re-runs the body."""
    ctx = Context()
    runs = []

    def plugin(c):
        runs.append("ran")

    fib = ctx.plugin(plugin)
    await ctx.flush()
    assert runs == ["ran"]
    assert fib.state == FiberState.ACTIVE

    await fib.restart()
    assert runs == ["ran", "ran"]
    assert fib.state == FiberState.ACTIVE


@pytest.mark.asyncio
async def test_reentrant_load_joins():
    """Two concurrent load() calls join one in-flight reload; body runs once."""
    ctx = Context()
    runs = []

    class SlowPlugin:
        async def init(self):
            await asyncio.sleep(0.01)
            runs.append("ran")

    fib = Fiber(ctx, SlowPlugin, (), {})
    first, second = await asyncio.gather(fib.load(), fib.load())
    assert runs == ["ran"]
    assert first is second
    assert isinstance(first, SlowPlugin)
    assert fib.state == FiberState.ACTIVE


@pytest.mark.asyncio
async def test_dispose_during_load_never_resurrects():
    """Dispose while a reload is in flight ends DISPOSED, never ACTIVE."""
    ctx = Context()
    started = asyncio.Event()
    release = asyncio.Event()
    runs = []

    async def slow_plugin(c):
        started.set()
        await release.wait()
        runs.append("ran")

    fib = Fiber(ctx, slow_plugin, (), {})
    load_task = asyncio.create_task(fib.load())
    await started.wait()
    dispose_task = asyncio.create_task(fib.dispose())
    await asyncio.sleep(0)
    release.set()
    await load_task
    await dispose_task
    assert runs == ["ran"]
    assert fib.state == FiberState.DISPOSED
    assert fib.uid is None


@pytest.mark.asyncio
async def test_reload_staleness_checkpoint_skips_body():
    """A reload whose epoch moved during the checkpoint never runs the body."""
    ctx = Context()
    runs = []

    def plugin(c):
        runs.append("ran")

    fib = Fiber(ctx, plugin, (), {})
    fib._setEpoch("")
    fib._setEpoch("__INACTIVE__")
    await fib.await_settle()
    assert runs == []
    assert fib.state == FiberState.PENDING


@pytest.mark.asyncio
async def test_failed_fiber_recovers_via_update():
    """A FAILED fiber recovers to ACTIVE when update() supplies a valid config."""
    ctx = Context()
    StrictPlugin = _strict_plugin_class()
    fib = ctx.plugin(StrictPlugin, num="not_an_int")
    await ctx.flush()
    assert fib.state == FiberState.FAILED

    await fib.update({"num": 7})
    await fib.await_settle()
    assert fib.state == FiberState.ACTIVE
    assert fib.config.num == 7


@pytest.mark.asyncio
async def test_await_fiber_returns_fiber():
    """await fiber settles the lifecycle and returns the fiber itself."""
    ctx = Context()
    fib = ctx.plugin(lambda c: None)
    result = await fib
    assert result is fib
    assert fib.state == FiberState.ACTIVE


@pytest.mark.asyncio
async def test_await_alias_returns_fiber():
    """await_() is an alias for await_settle()."""
    ctx = Context()
    fib = ctx.plugin(lambda c: None)
    result = await fib.await_()
    assert result is fib
    assert fib.state == FiberState.ACTIVE


@pytest.mark.asyncio
async def test_await_fiber_rethrows_startup_error():
    """await fiber rethrows the plugin's startup error."""

    def failing(c):
        raise RuntimeError("boom")

    ctx = Context()
    fib = Fiber(ctx, failing, (), {})
    with pytest.raises(RuntimeError, match="boom"):
        await fib
    assert fib.state == FiberState.FAILED


@pytest.mark.asyncio
async def test_internal_status_two_args():
    """internal/status emits exactly (fiber, old_state) on every transition."""
    ctx = Context()
    events = []
    ctx.on("internal/status", lambda f, old: events.append((f, old)))
    fib = ctx.plugin(lambda c: None)
    await ctx.flush()
    assert events == [
        (fib, FiberState.PENDING),
        (fib, FiberState.LOADING),
    ]


@pytest.mark.asyncio
async def test_root_fiber_uid_zero_and_dispose_restarts():
    """The root fiber has uid 0; disposing it restarts it, staying ACTIVE."""
    ctx = Context()
    root = ctx.fiber
    assert root.uid == 0
    assert root.state == FiberState.ACTIVE

    class Core(Service):
        def __init__(self, c: Context):
            super().__init__(c, "core_svc")

    Core(ctx)
    assert ctx.has("core_svc")
    await root.dispose()
    assert root.state == FiberState.ACTIVE
    assert root.uid == 0
    assert not ctx.has("core_svc")


@pytest.mark.asyncio
async def test_load_on_disposed_returns_none():
    """load() on a DISPOSED fiber returns None."""
    ctx = Context()
    fib = ctx.plugin(lambda c: None)
    await ctx.flush()
    await fib.dispose()
    assert await fib.load() is None
    assert fib.state == FiberState.DISPOSED


@pytest.mark.asyncio
async def test_dispose_idempotent():
    """Disposing twice is a no-op the second time."""
    ctx = Context()
    fib = ctx.plugin(lambda c: None)
    await ctx.flush()
    await fib.dispose()
    await fib.dispose()
    assert fib.state == FiberState.DISPOSED
    assert fib.uid is None


@pytest.mark.asyncio
async def test_dispose_direct_fiber():
    """A fiber built directly (no registry parent) disposes cleanly."""
    ctx = Context()
    fib = Fiber(ctx, lambda c: None, (), {})
    await fib.load()
    assert fib.state == FiberState.ACTIVE
    await fib.dispose()
    assert fib.state == FiberState.DISPOSED
    assert fib.uid is None


@pytest.mark.asyncio
async def test_dispose_fiber_not_in_runtime():
    """Disposing a fiber its runtime does not list leaves the runtime alone."""
    ctx = Context()

    def plugin(c):
        pass

    registered = ctx.plugin(plugin)
    await ctx.flush()
    direct = Fiber(ctx, plugin, (), {})
    await direct.load()
    assert direct.state == FiberState.ACTIVE
    await direct.dispose()
    assert direct.state == FiberState.DISPOSED
    assert registered.state == FiberState.ACTIVE


@pytest.mark.asyncio
async def test_dispose_after_replace():
    """Disposing after replace() still reaches DISPOSED."""
    ctx = Context()

    class V1:
        def __init__(self, c):
            pass

    class V2:
        def __init__(self, c):
            pass

    fib = ctx.plugin(V1)
    await ctx.flush()
    await fib.replace(V2)
    assert fib.state == FiberState.ACTIVE
    await fib.dispose()
    assert fib.state == FiberState.DISPOSED


def test_load_drives_deferred_transition():
    """A refresh with no loop running stores the epoch; load() drives it."""
    ctx = Context()
    ctx.provide("db", "connected")
    runs = []

    def needs_db(c):
        runs.append("ran")

    needs_db.inject = ["db"]
    fib = Fiber(ctx, needs_db, (), {})
    fib._checkImpl("db")
    fib._refresh()
    assert fib._getCurrentEpoch() == ":0"
    assert fib.state == FiberState.PENDING
    assert fib.inertia is None

    asyncio.run(fib.load())
    assert runs == ["ran"]
    assert fib.state == FiberState.ACTIVE


def test_set_epoch_deferred_no_loop():
    """_setEpoch with no loop running stores the epoch without a transition."""
    ctx = Context()
    fib = Fiber(ctx, lambda c: None, (), {})
    fib._setCurrentEpoch("__INACTIVE__")
    fib._setEpoch("active")
    assert fib._getCurrentEpoch() == "active"
    assert fib.state == FiberState.PENDING
    assert fib.inertia is None


def test_unload_complete_deferred_no_loop():
    """_unload_complete with no loop running returns the current state."""
    ctx = Context()
    fib = Fiber(ctx, lambda c: None, (), {})
    fib._setCurrentEpoch("active")
    assert fib._unload_complete() == FiberState.PENDING
    assert fib.inertia is None


def test_notify_schedules_load_for_pending_fiber():
    """Providing a missing dep wakes a PENDING fiber to ACTIVE."""
    ctx = Context()
    runs = []

    def needs_db(c):
        runs.append("ran")

    needs_db.inject = ["db"]
    fib = ctx.plugin(needs_db)
    assert fib.state == FiberState.PENDING
    ctx.reflect.provide("db", "connected")
    assert fib.state == FiberState.ACTIVE
    assert runs == ["ran"]


@pytest.mark.asyncio
async def test_call_with_config_signature_fallback():
    """A target whose signature cannot be inspected gets (ctx, config)."""
    ctx = Context()
    calls = []

    def plugin(c, config):
        calls.append(config)

    plugin.__signature__ = 123
    fib = ctx.plugin(plugin, key="val")
    await ctx.flush()
    assert fib.state == FiberState.ACTIVE
    assert calls == [{"key": "val"}]


@pytest.mark.asyncio
async def test_init_hooks_list_and_single():
    """initHooks run in order (sync and async); a single hook need not be a list."""
    ctx = Context()
    calls = []

    def sync_hook():
        calls.append("sync")

    async def async_hook():
        calls.append("async")

    class ListHooks:
        def __init__(self, c):
            pass

    setattr(ListHooks, symbols["initHooks"], [sync_hook, async_hook])

    def single_hook():
        calls.append("single")

    class SingleHook:
        def __init__(self, c):
            pass

    setattr(SingleHook, symbols["initHooks"], single_hook)

    ctx.plugin(ListHooks)
    ctx.plugin(SingleHook)
    await ctx.flush()
    assert calls == ["sync", "async", "single"]


@pytest.mark.asyncio
async def test_symbol_init_preferred_over_plain_init():
    """A symbol init runs instead of the plain init method."""

    ctx = Context()
    calls = []

    class SymbolInit:
        def __init__(self, c):
            pass

        def init(self):
            calls.append("plain")

    def symbol_init(self):
        calls.append("symbol")

    setattr(SymbolInit, symbols["init"], symbol_init)
    fib = ctx.plugin(SymbolInit)
    await ctx.flush()
    assert fib.state == FiberState.ACTIVE
    assert calls == ["symbol"]


@pytest.mark.asyncio
async def test_invalid_target_value_error():
    """A non-callable plugin target fails the fiber with a ValueError."""
    ctx = Context()
    fib = Fiber(ctx, "not_a_valid_plugin", (), {})
    assert await fib.load() is None
    assert fib.state == FiberState.FAILED
    assert isinstance(fib.error, ValueError)


@pytest.mark.asyncio
async def test_effect_disposer_returned():
    """A function plugin may return a disposer; it runs on dispose."""
    ctx = Context()
    calls = []

    def plugin(c):
        return lambda: calls.append("cleaned")

    fib = ctx.plugin(plugin)
    await ctx.flush()
    assert fib.state == FiberState.ACTIVE
    assert len(fib.effects) == 1
    await fib.dispose()
    assert calls == ["cleaned"]


@pytest.mark.asyncio
async def test_effect_awaitable():
    """A function plugin may return an awaitable resolving to a disposer."""
    ctx = Context()
    calls = []

    async def inner():
        return lambda: calls.append("cleaned")

    async def plugin(c):
        return inner()

    fib = ctx.plugin(plugin)
    await ctx.flush()
    assert fib.state == FiberState.ACTIVE
    assert len(fib.effects) == 1
    await fib.dispose()
    assert calls == ["cleaned"]


@pytest.mark.asyncio
async def test_effect_async_iterable():
    """A function plugin may return an async iterable of disposers."""
    ctx = Context()
    calls = []

    async def gen():
        yield lambda: calls.append("d1")
        yield lambda: calls.append("d2")

    def plugin(c):
        return gen()

    fib = ctx.plugin(plugin)
    await ctx.flush()
    assert fib.state == FiberState.ACTIVE
    assert len(fib.effects) == 2
    await fib.dispose()
    assert calls == ["d2", "d1"]


@pytest.mark.asyncio
async def test_effect_async_iterable_staleness():
    """An async-iterable effect stops collecting once the epoch moves."""
    ctx = Context()
    calls = []

    async def gen(c):
        yield lambda: calls.append("d1")
        c.fiber._setEpoch("__INACTIVE__")
        yield lambda: calls.append("d2")

    def plugin(c):
        return gen(c)

    fib = ctx.plugin(plugin)
    await ctx.flush()
    await fib.await_settle()
    assert fib.state == FiberState.PENDING
    assert calls == ["d1"]


@pytest.mark.asyncio
async def test_effect_iterable_with_none():
    """An iterable effect collects callables and skips None items."""
    ctx = Context()
    calls = []

    def plugin(c):
        return [lambda: calls.append("d1"), None, lambda: calls.append("d2")]

    fib = ctx.plugin(plugin)
    await ctx.flush()
    assert fib.state == FiberState.ACTIVE
    assert len(fib.effects) == 2
    await fib.dispose()
    assert calls == ["d2", "d1"]


@pytest.mark.asyncio
async def test_effect_invalid():
    """A non-callable, non-iterable effect fails the fiber with a TypeError."""

    def plugin(c):
        return 42

    ctx = Context()
    fib = ctx.plugin(plugin)
    await ctx.flush()
    assert fib.state == FiberState.FAILED
    assert isinstance(fib.error, TypeError)


@pytest.mark.asyncio
async def test_effect_invalid_iterable_item():
    """An iterable effect containing a non-callable fails with a TypeError."""

    def plugin(c):
        return [42]

    ctx = Context()
    fib = ctx.plugin(plugin)
    await ctx.flush()
    assert fib.state == FiberState.FAILED
    assert isinstance(fib.error, TypeError)


@pytest.mark.asyncio
async def test_update_no_save_skips_commit():
    """update(no_save=True) restarts without emitting internal/commit."""
    ctx = Context()
    committed = []
    ctx.on("internal/commit", lambda f, cfg: committed.append(cfg))
    load_counts = []

    class P:
        def __init__(self, c, config=None):
            load_counts.append((config or {}).get("val", 0))

    fib = ctx.plugin(P, val=1)
    await ctx.flush()
    await fib.update({"val": 2}, no_save=True)
    assert committed == []
    assert load_counts == [1, 2]
    assert fib.config == {"val": 2}


@pytest.mark.asyncio
async def test_validate_config_non_dict():
    """A callable Config receives a non-dict resolved config directly."""
    ctx = Context()

    async def config_hook(cfg, next_fn):
        return await next_fn("upper_me")

    ctx.on("internal/config", config_hook)

    def upper_config(value):
        return value.upper() if isinstance(value, str) else value

    class UpperPlugin:
        Config = upper_config

        def __init__(self, c, config=None):
            self.config = config

    fib = ctx.plugin(UpperPlugin)
    await ctx.flush()
    assert fib.state == FiberState.ACTIVE
    assert fib.config == "UPPER_ME"
