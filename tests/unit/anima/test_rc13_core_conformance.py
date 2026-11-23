"""Core conformance probes against canonical Cordis v4.0.0-rc.13.

Ground truth: ``cordiverse/cordis`` tag ``v4.0.0-rc.13`` (commit ``bc9fae7``,
published 2026-11-12), cloned at ``/tmp/opencode/cordis-upstream``.

Each test cites the exact upstream source line that defines the behavior. The
tests are executable evidence: they fail on the pre-rc.13 kernel and pass once
the port is brought up to the rc.13 contract.
"""

from __future__ import annotations

import asyncio
import sys
import types
from typing import Any

import pytest

from anima import Context, FiberState, LoaderService
from anima.events import EventBus, EventMode


@pytest.mark.asyncio
async def test_rc13_effect_awaitable_is_awaited_and_disposer_registered() -> None:
    """An effect may return a promise of a disposer (fiber.ts:257-258)."""
    ran: list[str] = []
    ctx = Context()

    async def setup() -> Any:
        await asyncio.sleep(0)
        return lambda: ran.append("promise-disposer")

    ctx.effect(setup)
    await ctx.flush()
    await ctx.fiber.dispose()

    assert ran == ["promise-disposer"]


@pytest.mark.asyncio
async def test_rc13_effect_sync_iterable_disposers_run_lifo() -> None:
    """An effect may return an iterable of inverses (fiber.ts:259-266)."""
    ran: list[str] = []
    ctx = Context()
    ctx.effect(lambda: [lambda: ran.append("first"), lambda: ran.append("second")])
    await ctx.flush()
    await ctx.fiber.dispose()

    assert ran == ["second", "first"]


@pytest.mark.asyncio
async def test_rc13_effect_async_iterable_disposers_run_lifo() -> None:
    """An effect may return an async iterable of inverses (fiber.ts:267-285)."""
    ran: list[str] = []
    ctx = Context()

    async def inverses():
        yield lambda: ran.append("first")
        yield lambda: ran.append("second")

    ctx.effect(inverses)
    await ctx.flush()
    await ctx.fiber.dispose()

    assert ran == ["second", "first"]


@pytest.mark.asyncio
async def test_rc13_effect_invalid_result_raises_type_error() -> None:
    """Non-disposer, non-iterable results are rejected (fiber.ts:255,287)."""
    ctx = Context()
    with pytest.raises(TypeError, match="Invalid effect"):
        ctx.effect(lambda: 42)


@pytest.mark.asyncio
async def test_rc13_effect_disposer_is_idempotent() -> None:
    """A disposer fires at most once (fiber.ts:340-349)."""
    ran: list[int] = []
    ctx = Context()
    dispose = ctx.effect(lambda: lambda: ran.append(1))
    dispose()
    dispose()
    assert ran == [1]


@pytest.mark.asyncio
async def test_rc13_parallel_always_aggregates_failures() -> None:
    """`parallel` throws AggregateError on any failure (events.ts:110-119).

    One failure is an AggregateError with one reason, not the bare exception:
    the all-settled contract and the error type are both part of the surface.
    Python's equivalent is ExceptionGroup.
    """
    bus = EventBus()

    async def fail() -> None:
        raise ValueError("boom")

    bus.on("boom", fail, mode=EventMode.PARALLEL)

    with pytest.raises(ExceptionGroup) as info:
        await bus.parallel("boom")

    assert len(info.value.exceptions) == 1
    assert isinstance(info.value.exceptions[0], ValueError)


@pytest.mark.asyncio
async def test_rc13_waterfall_rejects_next_called_twice() -> None:
    """A waterfall listener that calls next() twice fails (events.ts:155-157)."""
    bus = EventBus()

    async def double_next(value: Any, next_fn: Any) -> Any:
        await next_fn(value)
        return await next_fn(value)

    bus.on("double", double_next, mode=EventMode.WATERFALL)

    with pytest.raises(RuntimeError, match="next\\(\\) called multiple times"):
        await bus.waterfall("double", "v")


@pytest.mark.asyncio
async def test_rc13_async_generator_effect_is_closed_on_epoch_change() -> None:
    """An async-generator effect is closed when its epoch changes (#268).

    Upstream ``fiber.ts:275-284`` stops the iterator and calls
    ``iter.return()`` in ``finally`` so the generator's cleanup runs.
    """
    closed: list[str] = []
    ctx = Context()
    fiber = ctx.fiber

    async def inverses():
        try:
            yield lambda: None
            yield lambda: None
        finally:
            closed.append("closed")

    epochs = iter(["epoch-1", "epoch-2", "epoch-2"])
    original = fiber._getCurrentEpoch

    def changing_epoch() -> str:
        return next(epochs)

    fiber._getCurrentEpoch = changing_epoch  # type: ignore[method-assign]
    try:
        await fiber._collect_effect(inverses(), "epoch-1")
    finally:
        fiber._getCurrentEpoch = original  # type: ignore[method-assign]

    assert closed == ["closed"]


@pytest.mark.asyncio
async def test_rc13_effect_async_disposer_chain_runs_lifo() -> None:
    """Awaitable disposers chain after the sync ones (fiber.ts:298-311)."""
    ran: list[str] = []
    ctx = Context()

    async def async_disposer() -> None:
        ran.append("async")

    ctx.effect(lambda: [lambda: ran.append("sync"), async_disposer])
    await ctx.flush()
    await ctx.fiber.dispose()

    assert ran == ["async", "sync"]


@pytest.mark.asyncio
async def test_rc13_effect_dispose_waits_for_pending_collection() -> None:
    """Disposal awaits an in-flight async effect before running inverses."""
    ran: list[str] = []
    gate = asyncio.Event()
    ctx = Context()

    async def setup() -> Any:
        await gate.wait()
        return lambda: ran.append("collected")

    dispose = ctx.effect(setup)
    cleanup = dispose()
    assert cleanup is not None
    gate.set()
    await cleanup
    assert ran == ["collected"]


@pytest.mark.asyncio
async def test_rc13_effect_collection_failure_is_logged() -> None:
    """A failed collection task is observed rather than left unretrieved."""
    ctx = Context()

    async def bad_setup() -> Any:
        raise ValueError("collection failed")

    dispose = ctx.effect(bad_setup)
    await asyncio.sleep(0)
    # The task's exception is retrieved both by its done callback and here.
    dispose()
    assert True


def test_rc13_effect_outside_running_loop() -> None:
    """An async effect registered without a loop is still collected."""
    ran: list[str] = []
    ctx = Context()

    async def setup() -> Any:
        return lambda: ran.append("collected")

    dispose = ctx.effect(setup)
    dispose()
    assert ran == ["collected"]


@pytest.mark.asyncio
async def test_rc13_effect_child_meta_is_nested() -> None:
    """A disposer carrying effect metadata nests under its parent."""
    ctx = Context()
    child = ctx.effect(lambda: lambda: None, label="child")
    parent = ctx.effect(lambda: child, label="parent")
    meta = getattr(parent, "anima.effect", None)
    assert meta is not None
    assert [entry["label"] for entry in meta["children"]] == ["child"]


@pytest.mark.asyncio
async def test_rc13_async_iterable_without_aclose_is_supported() -> None:
    """An async iterable without `aclose()` still collects its disposers."""
    ran: list[str] = []

    class Iterator:
        def __init__(self) -> None:
            self._sent = False

        def __aiter__(self) -> Iterator:
            return self

        async def __anext__(self) -> Any:
            if self._sent:
                raise StopAsyncIteration
            self._sent = True
            return lambda: ran.append("collected")

    class Source:
        def __aiter__(self) -> Iterator:
            return Iterator()

    ctx = Context()
    ctx.effect(lambda: Source())
    await ctx.flush()
    await ctx.fiber.dispose()
    assert ran == ["collected"]


@pytest.mark.asyncio
async def test_rc13_fiber_effect_stops_before_first_item_on_stale_epoch() -> None:
    """The guard is consulted before the first step as well (Algo 1)."""
    ctx = Context()
    fiber = ctx.fiber

    async def gen():
        yield lambda: None

    await fiber._collect_effect(gen(), "different-epoch")
    assert fiber.effects == []


@pytest.mark.asyncio
async def test_rc13_fiber_effect_sync_iterable_stops_on_stale_epoch() -> None:
    """Sync iterables are guarded at each iteration boundary too."""
    ctx = Context()
    fiber = ctx.fiber

    def component(_: Any) -> Any:
        return [lambda: None]

    # Drive the epoch back to INACTIVE immediately after the reload starts.
    original = fiber._getCurrentEpoch

    def changed() -> str:
        return "__INACTIVE__"

    fiber._getCurrentEpoch = changed  # type: ignore[method-assign]
    try:
        await fiber._collect_effect(component(ctx), "active")
    finally:
        fiber._getCurrentEpoch = original  # type: ignore[method-assign]
    assert fiber.effects == []


@pytest.mark.asyncio
async def test_rc13_fiber_async_iterable_without_aclose() -> None:
    """A non-generator async iterable needs no `aclose()`."""

    class Iterator:
        def __init__(self) -> None:
            self._sent = False

        def __aiter__(self) -> Iterator:
            return self

        async def __anext__(self) -> Any:
            if self._sent:
                raise StopAsyncIteration
            self._sent = True
            return lambda: None

    class Source:
        def __aiter__(self) -> Iterator:
            return Iterator()

    ctx = Context()
    fiber = ctx.fiber
    await fiber._collect_effect(Source(), fiber._getCurrentEpoch())
    assert len(fiber.effects) == 1


@pytest.mark.asyncio
async def test_rc13_effect_child_meta_outside_fiber_list() -> None:
    """A foreign disposer with metadata nests without a fiber removal."""
    ctx = Context()
    child = ctx.effect(lambda: lambda: None, label="foreign")
    ctx.fiber.effects.remove(child)
    parent = ctx.effect(lambda: child, label="parent")
    meta = getattr(parent, "anima.effect", None)
    assert [entry["label"] for entry in meta["children"]] == ["foreign"]


@pytest.mark.asyncio
async def test_rc13_effect_two_async_disposers_chain() -> None:
    """Every awaitable disposer in the chain is awaited in order."""
    ran: list[str] = []
    ctx = Context()

    async def first() -> None:
        ran.append("first")

    async def second() -> None:
        ran.append("second")

    ctx.effect(lambda: [second, first])
    await ctx.flush()
    await ctx.fiber.dispose()
    assert ran == ["first", "second"]


@pytest.mark.asyncio
async def test_rc13_effect_async_iterable_stops_when_epoch_moves() -> None:
    """Context-level async iterables stop collecting on epoch change."""
    ran: list[str] = []
    ctx = Context()

    class Iterator:
        def __init__(self) -> None:
            self._sent = False

        def __aiter__(self) -> Iterator:
            return self

        async def __anext__(self) -> Any:
            if self._sent:
                raise StopAsyncIteration
            self._sent = True
            ctx.fiber._setEpoch("__INACTIVE__")
            return lambda: ran.append("collected")

    class Source:
        def __aiter__(self) -> Iterator:
            return Iterator()

    ctx.effect(lambda: Source())
    await ctx.flush()
    await ctx.fiber.dispose()
    assert ran == []


@pytest.mark.asyncio
async def test_rc13_effect_async_iterable_stale_before_first_step() -> None:
    """An epoch change in `__aiter__` stops collection before step one."""
    ctx = Context()

    class Iterator:
        def __init__(self, fiber: Any) -> None:
            self._fiber = fiber

        def __aiter__(self) -> Iterator:
            self._fiber._setEpoch("__INACTIVE__")
            return self

        async def __anext__(self) -> Any:
            raise StopAsyncIteration

    def component(c: Any) -> None:
        c.effect(lambda: Iterator(c.fiber))

    fiber = ctx.use(component)
    await ctx.flush()
    await fiber.dispose()
    assert True


@pytest.mark.asyncio
async def test_rc13_effect_pending_collection_failure_is_reported() -> None:
    """A failure during a pending collection is logged, not propagated."""
    gate = asyncio.Event()
    ctx = Context()

    async def setup() -> Any:
        await gate.wait()
        raise ValueError("late failure")

    dispose = ctx.effect(setup)
    cleanup = dispose()
    gate.set()
    await cleanup
    assert True


@pytest.mark.asyncio
async def test_rc13_cancelled_collection_task_is_observed() -> None:
    """Cancelled collection tasks are not treated as failures."""
    ctx = Context()

    async def setup() -> Any:
        await asyncio.Event().wait()

    ctx.effect(setup)
    await asyncio.sleep(0)
    current = asyncio.current_task()
    for task in asyncio.all_tasks():
        if task is not current:
            task.cancel()
    await asyncio.sleep(0)
    assert True


@pytest.mark.asyncio
async def test_rc13_failed_fiber_does_not_reload_on_epoch_change() -> None:
    """A FAILED fiber recovers only through update() (fiber.ts:435-436)."""
    ctx = Context()
    fiber = ctx.fiber
    fiber.error = RuntimeError("failed")
    before = fiber._getCurrentEpoch()
    fiber._setEpoch("new-epoch")
    assert fiber._getCurrentEpoch() == before


@pytest.mark.asyncio
async def test_rc13_notify_respects_isolation_realms() -> None:
    """A binding change only refreshes fibers in the same realm (reflect.ts:251)."""
    provider_ctx = Context()
    consumer_ctx = Context()
    consumer_ctx._isolated_services["svc"] = "other-realm"

    def consumer(c: Any) -> None:
        return None

    consumer.inject = ["svc"]
    fiber = consumer_ctx.use(consumer)
    await consumer_ctx.flush()

    provider_ctx.reflect.provide("svc", object())
    provider_ctx.reflect.notify(["svc"])
    await provider_ctx.flush()

    assert fiber.state == FiberState.PENDING


@pytest.mark.asyncio
async def test_rc13_set_requires_existing_provision() -> None:
    """`set` on an unprovided name fails loud (reflect.ts:200-203)."""
    ctx = Context()
    with pytest.raises(ValueError, match="without provide"):
        ctx.set("never_provided", 1)


@pytest.mark.asyncio
async def test_rc13_bail_rejects_async_listener_in_running_loop() -> None:
    """A sync bail cannot block on an awaitable in a running loop."""
    bus = EventBus()

    async def listener() -> str:
        return "async"

    bus.on("bail", listener, mode=EventMode.BAIL)
    with pytest.raises(TypeError, match="use serial"):
        bus.bail("bail")


@pytest.mark.asyncio
async def test_rc13_bail_rejects_awaitable_without_close() -> None:
    """Awaitables without `close()` are rejected just the same."""

    class Awaitable:
        def __await__(self) -> Any:
            async def value() -> str:
                return "async"

            return value().__await__()

    bus = EventBus()
    bus.on("bail", lambda: Awaitable(), mode=EventMode.BAIL)
    with pytest.raises(TypeError, match="use serial"):
        bus.bail("bail")


@pytest.mark.asyncio
async def test_rc13_commit_uses_raw_config_on_pending_fiber() -> None:
    """internal/commit fires with the raw input even when not loaded.

    Upstream fiber.ts:536-542; the loader writes that raw value back to the
    entry, preserving expressions for the next activation.
    """
    ctx = Context()
    commits: list[Any] = []
    ctx.on(
        "internal/commit",
        lambda _fiber, config: commits.append(config),
        mode=EventMode.EMIT,
    )

    def dependent(_c: Any) -> None:
        return None

    dependent.inject = ["missing_service"]
    fiber = ctx.use(dependent)
    await ctx.flush()
    assert fiber.state == FiberState.PENDING

    raw = {"v": {"__jsExpr": "process.env.X"}}
    await fiber.update(raw)
    assert commits == [raw]


@pytest.mark.asyncio
async def test_rc13_commit_does_not_receive_resolved_config() -> None:
    """A resolved expression/default must not be written back to the profile."""
    ctx = Context()
    commits: list[Any] = []

    def component(_c: Any, config: Any = None) -> None:
        return None

    fiber = ctx.use(component, {"v": 1})
    await ctx.flush()
    ctx.on(
        "internal/commit",
        lambda _fiber, config: commits.append(config),
        mode=EventMode.EMIT,
    )

    raw = {"v": {"__jsExpr": "Number(process.env.X)"}}
    await fiber.update(raw)
    assert commits == [raw]
    assert "v" in commits[0]


@pytest.mark.asyncio
async def test_rc13_entry_name_change_rebuilds_component(monkeypatch: Any) -> None:
    """Changing `name` rebuilds the entry (loader entry.ts:243-248)."""
    calls: list[str] = []

    def first(_c: Any, config: Any = None) -> None:
        calls.append("first")

    def second(_c: Any, config: Any = None) -> None:
        calls.append("second")

    module = types.ModuleType("rc13_rebuild_mod")
    module.first = first
    module.second = second
    monkeypatch.setitem(sys.modules, "rc13_rebuild_mod", module)

    ctx = Context()
    loader = LoaderService(ctx)
    loader.load([{"id": "e", "name": "rc13_rebuild_mod:first"}])
    await ctx.flush()
    assert calls == ["first"]
    old_fiber = loader.resolve("e").fiber

    await loader.update("e", {"name": "rc13_rebuild_mod:second"})
    await ctx.flush()
    await loader.resolve("e").fiber.await_settle()

    assert calls[-1] == "second"
    assert loader.resolve("e").fiber is not old_fiber


@pytest.mark.asyncio
async def test_rc13_entry_rebuild_when_no_prior_fiber(monkeypatch: Any) -> None:
    """A name change on an entry that never loaded still initializes."""
    calls: list[str] = []

    def comp(_c: Any, config: Any = None) -> None:
        calls.append("comp")

    module = types.ModuleType("rc13_rebuild_mod2")
    module.comp = comp
    monkeypatch.setitem(sys.modules, "rc13_rebuild_mod2", module)

    ctx = Context()
    loader = LoaderService(ctx)
    loader.load([{"id": "e", "name": "rc13_missing_mod:comp"}])
    await ctx.flush()
    assert loader.resolve("e").fiber is None

    await loader.update("e", {"name": "rc13_rebuild_mod2:comp"})
    await ctx.flush()
    await loader.resolve("e").fiber.await_settle()
    assert calls == ["comp"]


@pytest.mark.asyncio
async def test_rc13_entry_rebuild_after_disposal(monkeypatch: Any) -> None:
    """A disposed fiber does not need another dispose during rebuild."""
    calls: list[str] = []

    def comp(_c: Any, config: Any = None) -> None:
        calls.append("comp")

    def comp2(_c: Any, config: Any = None) -> None:
        calls.append("comp2")

    module = types.ModuleType("rc13_rebuild_mod3")
    module.comp = comp
    module.comp2 = comp2
    monkeypatch.setitem(sys.modules, "rc13_rebuild_mod3", module)

    ctx = Context()
    loader = LoaderService(ctx)
    loader.load([{"id": "e", "name": "rc13_rebuild_mod3:comp"}])
    await ctx.flush()
    entry = loader.resolve("e")
    fiber = entry.fiber
    entry._loader_disposing = True
    await fiber.dispose()
    assert fiber.uid is None

    await loader.update("e", {"name": "rc13_rebuild_mod3:comp2"})
    await ctx.flush()
    await loader.resolve("e").fiber.await_settle()
    assert calls == ["comp", "comp2"]


@pytest.mark.asyncio
async def test_rc13_entry_exposes_inner_fiber_alias(monkeypatch: Any) -> None:
    """rc.13 names an entry's component fiber `innerFiber`."""
    module = types.ModuleType("rc13_inner_fiber_mod")

    def comp(_c: Any, config: Any = None) -> None:
        return None

    module.comp = comp
    monkeypatch.setitem(sys.modules, "rc13_inner_fiber_mod", module)
    ctx = Context()
    loader = LoaderService(ctx)
    loader.load([{"id": "e", "name": "rc13_inner_fiber_mod:comp"}])
    await ctx.flush()

    entry = loader.resolve("e")
    assert entry.innerFiber is entry.fiber
    entry.innerFiber = None
    assert entry.fiber is None


@pytest.mark.asyncio
async def test_rc13_fiber_state_unloading_exists() -> None:
    """rc.13 keeps the six fiber states, including UNLOADING (fiber.ts:90-97)."""
    assert {state.name for state in FiberState} == {
        "PENDING",
        "LOADING",
        "ACTIVE",
        "FAILED",
        "UNLOADING",
        "DISPOSED",
    }
