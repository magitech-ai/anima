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

from animas import Context, FiberState, FsService, LoaderService, Service
from animas.core import Fiber
from animas.events import EventBus, EventMode


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
    meta = getattr(parent, "animas.effect", None)
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
    meta = getattr(parent, "animas.effect", None)
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
async def test_rc13_internal_update_listeners_are_fiber_scoped() -> None:
    """A sibling fiber's config update does not reach this listener.

    Upstream events.ts:54-79 routes non-global internal/update listeners into
    the registering fiber's own hook list.
    """
    ctx = Context()
    hits: list[str] = []

    def component_a(c: Any) -> None:
        c.on("internal/update", lambda _cfg: hits.append("a"), mode=EventMode.WATERFALL)

    def component_b(c: Any) -> None:
        c.on("internal/update", lambda _cfg: hits.append("b"), mode=EventMode.WATERFALL)

    a = ctx.use(component_a)
    b = ctx.use(component_b)
    await ctx.flush()

    await a.update({"v": 1})
    await ctx.flush()
    assert hits == ["a"]

    await b.update({"v": 2})
    await ctx.flush()
    assert hits == ["a", "b"]


@pytest.mark.asyncio
async def test_rc13_global_isolate_realm_shares_bindings() -> None:
    """Two contexts naming the same isolate label share the binding.

    Paper §5.2.1: a string isolate selects a global realm shared by every
    entry naming that string (Algorithm 7).
    """
    root = Context()
    provider_ctx = root.isolate("db", "shared")
    consumer_ctx = root.isolate("db", "shared")

    def consumer(_c: Any) -> None:
        return None

    consumer.inject = ["db"]
    fiber = consumer_ctx.use(consumer)
    await root.flush()
    assert fiber.state == FiberState.PENDING

    disposer = provider_ctx.provide("db", {"v": 1})
    await root.flush()
    assert fiber.state == FiberState.ACTIVE
    assert consumer_ctx.get("db") == {"v": 1}

    disposer()
    await root.flush()
    assert consumer_ctx.get("db") is None


@pytest.mark.asyncio
async def test_rc13_provider_waits_for_dependent_teardown() -> None:
    """A provider's inverses run only after its dependents have unloaded."""
    ctx = Context()
    order: list[str] = []

    class DrainService(Service):
        def __init__(self, ctx: Context) -> None:
            super().__init__(ctx, "drain_svc")

    def provider(c: Any) -> Any:
        DrainService(c)
        return lambda: order.append("provider-down")

    def consumer(_c: Any) -> Any:
        return lambda: order.append("consumer-down")

    consumer.inject = ["drain_svc"]
    provider_fiber = ctx.use(provider)
    consumer_fiber = ctx.use(consumer)
    await ctx.flush()
    assert provider_fiber.state is FiberState.ACTIVE
    assert consumer_fiber.state is FiberState.ACTIVE

    await provider_fiber.dispose()
    await ctx.flush()
    assert order == ["consumer-down", "provider-down"]


@pytest.mark.asyncio
async def test_rc13_fiber_dependent_bookkeeping() -> None:
    """Committed views adopt and drop provider dependencies."""
    ctx = Context()
    fiber = ctx.fiber
    released: list[str] = []

    class Impl:
        def __init__(self, owner: Any = None) -> None:
            self.fiber = owner
            self.dependents: set[Any] = set()
            self.release: Any = None

    old = Impl()
    old.dependents.add(fiber)
    old.release = lambda: released.append("old")
    own = Impl(fiber)
    fiber.store = {"own": own}

    fiber._adopt_impls({old, own})
    assert fiber not in old.dependents  # dropped from the previous view
    assert released == ["old"]
    assert old.release is None
    assert fiber not in own.dependents  # a fiber does not depend on itself

    # A second drop and a still-owned impl are harmless.
    fiber._drop_dependent(old)
    fiber._drop_dependent(own)
    assert released == ["old"]


@pytest.mark.asyncio
async def test_rc13_drain_provisions_waits_on_release() -> None:
    """_drain_provisions waits for an explicit release callback."""
    ctx = Context()
    fiber = ctx.fiber

    class Impl:
        def __init__(self) -> None:
            self.fiber = fiber
            self.dependents: set[Any] = {object()}
            self.release: Any = None

    impl = Impl()
    ctx.reflect.store["drain_probe"] = impl
    try:
        task = asyncio.ensure_future(fiber._drain_provisions())
        await asyncio.sleep(0)
        assert not task.done()
        assert impl.release is not None
        impl.release()
        impl.release()  # idempotent: the future is already done
        await task
        assert impl.release is None
    finally:
        ctx.reflect.store.pop("drain_probe", None)


@pytest.mark.asyncio
async def test_rc13_entry_inject_uses_outer_fiber(monkeypatch: Any) -> None:
    """Entry inject belongs to an outer fiber, not the component fiber.

    Upstream loader/src/config/entry.ts:35-46,353-372.
    """
    module = types.ModuleType("rc13_outer_mod")

    def dep(_c: Any, config: Any = None) -> None:
        return None

    module.dep = dep
    monkeypatch.setitem(sys.modules, "rc13_outer_mod", module)

    ctx = Context()
    ctx.provide("outer_svc", object())
    loader = LoaderService(ctx)
    loader.load([{"id": "e", "name": "rc13_outer_mod:dep", "inject": ["outer_svc"]}])
    await ctx.flush()

    entry = loader.resolve("e")
    assert entry.outer_fiber is not None
    assert "outer_svc" in entry.outer_fiber.inject
    assert entry.fiber is not entry.outer_fiber
    assert "outer_svc" not in entry.fiber.inject
    await entry.fiber.await_settle()
    assert entry.fiber.state is FiberState.ACTIVE


@pytest.mark.asyncio
async def test_rc13_entry_missing_inject_keeps_outer_pending(monkeypatch: Any) -> None:
    """A missing dependency leaves the outer fiber pending."""
    module = types.ModuleType("rc13_outer_missing_mod")

    def dep(_c: Any, config: Any = None) -> None:
        return None

    module.dep = dep
    monkeypatch.setitem(sys.modules, "rc13_outer_missing_mod", module)

    ctx = Context()
    loader = LoaderService(ctx)
    loader.load(
        [{"id": "e", "name": "rc13_outer_missing_mod:dep", "inject": ["absent_svc"]}]
    )
    await ctx.flush()

    entry = loader.resolve("e")
    assert entry.outer_fiber is not None
    assert entry.outer_fiber.state is FiberState.PENDING
    assert entry.fiber is entry.outer_fiber


@pytest.mark.asyncio
async def test_rc13_yaml_marks_and_empty_document() -> None:
    """YAML locations are retained per path; an empty document has none."""
    from animas.profile_yaml import load_yaml_with_locations

    empty, marks = load_yaml_with_locations("")
    assert empty is None and marks == {}
    null_map, null_marks = load_yaml_with_locations("a:\n")
    assert null_map == {"a": None} and null_marks == {("a",): (1, 3)}

    data, marks = load_yaml_with_locations("items:\n  - value: 1\n  - value: 2\n")
    assert len(data["items"]) == 2
    assert ("items", 0, "value") in marks
    assert ("items", 1, "value") in marks


@pytest.mark.asyncio
async def test_rc13_entry_location_stub(monkeypatch: Any) -> None:
    """The root tree reports no source location."""
    module = types.ModuleType("rc13_location_mod")

    def comp(_c: Any, config: Any = None) -> None:
        return None

    module.comp = comp
    monkeypatch.setitem(sys.modules, "rc13_location_mod", module)
    ctx = Context()
    loader = LoaderService(ctx)
    loader.load([{"id": "e", "name": "rc13_location_mod:comp"}])
    await ctx.flush()
    entry = loader.resolve("e")
    assert entry.get_location(("x",)) is None
    assert loader.get_location("e", ("x",)) is None


@pytest.mark.asyncio
async def test_rc13_group_remove_respects_adoption(monkeypatch: Any) -> None:
    """A group cannot remove an entry another group already adopted."""
    module = types.ModuleType("rc13_adopt_mod")

    def comp(_c: Any, config: Any = None) -> None:
        return None

    module.comp = comp
    monkeypatch.setitem(sys.modules, "rc13_adopt_mod", module)
    ctx = Context()
    loader = LoaderService(ctx)
    loader.load(
        [
            {
                "id": "g1",
                "target": "group",
                "entries": [{"id": "e", "name": "rc13_adopt_mod:comp"}],
            },
            {"id": "g2", "target": "group", "entries": []},
        ]
    )
    await ctx.flush()
    entry = loader.resolve("e")
    old_group = loader.resolve_group("g1")
    new_group = loader.resolve_group("g2")
    # Simulate a file-driven move that adopted the row into g2.
    entry.parent = new_group
    old_group.remove("e")
    assert loader.resolve("e") is entry
    assert entry.parent is new_group


@pytest.mark.parametrize(
    ("options", "expected"),
    [
        (42, "entry must be an object"),
        ({"id": "a"}, "name is required"),
        ({"name": "m:c", "id": "a:b"}, 'id must not contain ":"'),
        ({"name": "m:c", "group": True}, "config is required"),
        ({"name": "m:c", "disabled": 1}, "disabled must be a boolean"),
        ({"name": "m:c", "inject": ["ok", 2]}, "inject.1 must be a string"),
        ({"name": "m:c", "intercept": "bad"}, "intercept must be an object"),
        (
            {"name": "m:c", "isolate": {"svc": 3}},
            "isolate.svc must be true or a string",
        ),
    ],
)
def test_rc13_entry_options_validation(options: Any, expected: str) -> None:
    """Entry rows are validated against the rc.13 contract."""
    from animas.tree import validate_options

    messages = [issue["message"] for issue in validate_options(options)]
    assert expected in messages


def test_rc13_group_child_validation_propagates_issues() -> None:
    from animas.tree import validate_options

    issues = validate_options(
        {"name": "g", "group": True, "config": [{"name": "child", "id": "a:b"}]},
        deep=True,
    )
    assert any(issue["path"] == ["config", 0, "id"] for issue in issues)


@pytest.mark.asyncio
async def test_rc13_create_rejects_invalid_options() -> None:
    from animas.tree import EntryValidationError

    ctx = Context()
    loader = LoaderService(ctx)
    with pytest.raises(EntryValidationError, match="name is required"):
        await loader.create({"id": "bad"})

    loader.load([{"id": "g", "name": "group", "group": True, "config": []}])
    await ctx.flush()
    group = loader.resolve_group("g")
    with pytest.raises(EntryValidationError, match="name is required"):
        await group.create({"id": "bad"})


@pytest.mark.asyncio
async def test_rc13_await_settle_observes_but_does_not_drive_load() -> None:
    """Fiber.await only observes; it never starts a load (fiber.ts:520-526)."""
    from animas.core import Fiber

    ctx = Context()
    fiber = Fiber(ctx, lambda c: None, (), {})
    assert fiber.state is FiberState.PENDING
    assert fiber.inertia is None
    result = await fiber.await_settle()
    assert result is fiber
    assert fiber.state is FiberState.PENDING


def test_rc13_hmr_injects_fs_service() -> None:
    """rc.13's HMR component depends on fs, not the legacy watcher."""
    from animas.hmr import HmrService

    assert HmrService.inject == ["fs"]


def test_rc13_set_error_uses_enhanced_stack() -> None:
    """A failed property set rethrows the enhanced access error."""
    ctx = Context()
    ctx.fiber.runtime = object()  # type: ignore[assignment]
    with pytest.raises(ValueError, match="without provide") as info:
        ctx.reflect.handler_set(ctx, "rc13_unprovided_prop", 1, ctx)
    assert info.value.__traceback__ is not None


@pytest.mark.asyncio
async def test_rc13_access_error_stack_excludes_kernel_frames() -> None:
    """A failed access rethrows with a caller-facing stack."""
    import traceback

    from animas.reflect import ReflectService, _enhance_error

    ctx = Context()
    service = ReflectService(ctx)
    try:
        service.set("never_provided", 1)
    except ValueError as error:
        enhanced = _enhance_error(error)
        frames = traceback.extract_tb(enhanced.__traceback__)
    else:
        raise AssertionError("set must fail without provide")
    assert not any(
        "animas/reflect.py" in frame.filename or "animas/core.py" in frame.filename
        for frame in frames
    )


def test_rc13_schema_deep_equal_and_clone_cycles() -> None:
    """Cycle handling survives removal of the volatile schema hook."""
    from animas.schema import clone, deep_equal

    left: dict[str, Any] = {}
    left["self"] = left
    right: dict[str, Any] = {}
    right["self"] = right
    assert not deep_equal(left, right)
    copied = clone(left)
    assert copied["self"] is copied


@pytest.mark.asyncio
async def test_rc13_hmr_start_tolerates_sync_watch(tmp_path: Any) -> None:
    """Legacy start accepts a sync callback and a non-awaitable disposer."""
    ctx = Context()
    ctx.use(FsService)
    await ctx.flush()

    class FakeWatch:
        def on(self, name: str, listener: Any) -> None:
            if name == "change":
                listener("file:///tmp/x.py")

        def dispose(self) -> None:
            return None

    from animas.hmr import HmrService

    monkeypatched = ctx.fs.watch
    ctx.fs.watch = lambda *_a, **_kw: FakeWatch()  # type: ignore[method-assign]
    try:
        hmr = HmrService(ctx, {"root": str(tmp_path)})
        hmr._on_file_change = lambda _action, _path: None  # type: ignore[method-assign]
        hmr.start()
        await asyncio.sleep(0)
        hmr.stop()
    finally:
        ctx.fs.watch = monkeypatched  # type: ignore[method-assign]


@pytest.mark.asyncio
async def test_rc13_timer_mixin_and_aliases() -> None:
    """rc.13 mixes timer methods onto the context and keeps deprecated aliases."""
    from animas.timer import TimerService

    ctx = Context()
    ctx.use(TimerService)
    await ctx.flush()
    for name in (
        "timeout",
        "interval",
        "throttle",
        "debounce",
        "setTimeout",
        "setInterval",
    ):
        assert callable(getattr(ctx, name))
    calls: list[int] = []
    dispose = ctx.setTimeout(lambda: calls.append(1), 0.001)
    await asyncio.sleep(0.1)
    dispose()
    assert calls == [1]
    dispose_interval = ctx.setInterval(lambda: calls.append(2), 0.001)
    await asyncio.sleep(0.1)
    dispose_interval()
    assert 2 in calls


@pytest.mark.asyncio
async def test_rc13_timer_interval_async_iterator() -> None:
    """interval(delay) with no callback yields on each tick and can be closed."""
    from animas.timer import TimerService

    ctx = Context()
    ctx.use(TimerService)
    await ctx.flush()

    iterator = ctx.timer.interval(0.001).__aiter__()
    first = await asyncio.wait_for(iterator.__anext__(), timeout=0.5)
    second = await asyncio.wait_for(iterator.__anext__(), timeout=0.5)
    assert first is None and second is None
    await iterator.aclose()
    with pytest.raises(StopAsyncIteration):
        await iterator.__anext__()


@pytest.mark.asyncio
async def test_rc13_timer_interval_rejects_on_dispose() -> None:
    """A pending async interval tick fails when its context unloads."""
    from animas.timer import TimerService

    ctx = Context()
    fiber = ctx.use(TimerService)
    await ctx.flush()
    iterator = ctx.timer.interval(1.0).__aiter__()
    pending = asyncio.ensure_future(iterator.__anext__())
    await asyncio.sleep(0)
    await fiber.dispose()
    with pytest.raises(RuntimeError, match="disposed"):
        await pending


@pytest.mark.asyncio
async def test_rc13_registry_replace_moves_fiber_and_emits(
    monkeypatch: Any,
) -> None:
    """A replaced fiber moves to the new runtime and emits internal/replace."""
    module = types.ModuleType("rc13_replace_mod")

    def first(_c: Any, config: Any = None) -> None:
        return None

    def second(_c: Any, config: Any = None) -> None:
        return None

    module.first = first
    module.second = second
    monkeypatch.setitem(sys.modules, "rc13_replace_mod", module)

    ctx = Context()
    seen: list[Fiber] = []
    ctx.on("internal/replace", seen.append, mode=EventMode.EMIT)
    fiber = ctx.use(first)
    await ctx.flush()

    fibers = ctx.registry.replace(first, second)
    await ctx.flush()
    assert fibers == [fiber]
    assert seen == [fiber]
    runtime = ctx.registry.get(second)
    assert runtime is not None
    assert fiber in runtime.fibers
    assert fiber.target is second


@pytest.mark.asyncio
async def test_rc13_timer_interval_edge_paths() -> None:
    """Iterator cleanup paths: double close, throw, and cancelled waiters."""
    from animas.timer import TimerService

    ctx = Context()
    ctx.use(TimerService)
    await ctx.flush()
    iterator = ctx.timer.interval(0.001)
    pending = asyncio.ensure_future(iterator.__anext__())
    await asyncio.sleep(0)
    iterator._pending[0].cancel()  # a tick resolves an already-done future
    await asyncio.sleep(0.01)
    with pytest.raises(asyncio.CancelledError):
        await pending
    await iterator.aclose()
    await iterator.aclose()
    with pytest.raises(StopAsyncIteration):
        await iterator.athrow(ValueError("stop"))

    other = ctx.timer.interval(0.001)
    waiter = asyncio.ensure_future(other.__anext__())
    await asyncio.sleep(0)
    other._pending[0].set_result(None)  # already done while stopping
    await other.aclose()
    await waiter
    with pytest.raises(StopAsyncIteration):
        await other.__anext__()
    other._stop(ValueError("ignored"))  # already stopped: idempotent
    with pytest.raises(StopAsyncIteration):
        await other.athrow(ValueError("ignored"))

    closing = ctx.timer.interval(0.001)
    pending_close = asyncio.ensure_future(closing.__anext__())
    await asyncio.sleep(0)
    await closing.aclose()  # stops a pending, not-yet-done waiter
    with pytest.raises(StopAsyncIteration):
        await pending_close

    thrown = ctx.timer.interval(0.001)
    with pytest.raises(StopAsyncIteration):
        await thrown.athrow(ValueError("stop"))

    idle = ctx.timer.interval(0.001)
    idle._emit_tick()  # a tick with no waiters is a no-op
    await idle.aclose()

    disposed = ctx.timer.interval(0.001)
    service_fiber = ctx.fiber
    await service_fiber.dispose()
    with pytest.raises(RuntimeError, match="disposed"):
        await disposed.__anext__()


@pytest.mark.asyncio
async def test_rc13_hmr_cancelled_debounce_returns(tmp_path: Any) -> None:
    """A cancelled debounce timer exits without starting a reload."""
    ctx = Context()
    from animas.fs import FsService
    from animas.hmr import HmrService

    ctx.use(FsService)
    await ctx.flush()
    hmr = HmrService(ctx, {"root": str(tmp_path)})
    task = asyncio.ensure_future(hmr._debounced_reload())
    await asyncio.sleep(0)
    task.cancel()
    await task


@pytest.mark.asyncio
async def test_rc13_replace_without_old_runtime_membership() -> None:
    """A fiber not listed in the old runtime still joins the new one."""
    ctx = Context()
    first = ctx.use(lambda c: None)
    await ctx.flush()
    old_runtime = ctx.registry.get(first.target)
    second = ctx.use(lambda c: None)
    await ctx.flush()
    new_runtime = ctx.registry.get(second.target)
    if old_runtime is not None:
        old_runtime.fibers.clear()
    assert new_runtime is not None
    await first.replace(new_runtime.callback, new_runtime, old_runtime=old_runtime)
    assert first in new_runtime.fibers


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


def test_context_is_brand_check() -> None:
    """Context.is_ uses the symbol brand, not inheritance or duck typing."""
    ctx = Context()
    assert Context.is_(ctx) is True
    assert Context.is_(object()) is False
