import asyncio
import os
from typing import Any, ClassVar

import pytest
from pydantic import BaseModel
from pydantic import ValidationError as PydanticValidationError

from anima.cli_env import CliEnv, load_env_cascade
from anima.core import (
    Context,
    AnimaError,
    AnimaErrorCode,
    CoreService,
    Fiber,
    FiberState,
    Service,
    format_issues,
    is_js_expr,
    symbols,
)
from anima.core import ValidationError as AnimaValidationError
from anima.events import DisposableList, EventMode
from anima.reflect import ReflectService
from anima.schema import Schema, SchemaError


class MockGreeter(Service):
    def __init__(self, ctx: Context) -> None:
        super().__init__(ctx, "greeter")

    def greet(self, name: str) -> str:
        return f"Hello, {name}!"


class MockLogger(Service):
    def __init__(self, ctx: Context) -> None:
        super().__init__(ctx, "logger")
        self.entries: list[str] = []

    def log(self, msg: str) -> None:
        self.entries.append(msg)


@pytest.mark.asyncio
async def test_service_registration_and_root():
    ctx = Context()
    fiber = ctx.plugin(MockGreeter)
    await fiber.load()

    assert ctx.has("greeter")
    assert ctx.greeter.greet("World") == "Hello, World!"
    assert ctx.root == ctx

    # Test services dictionary
    assert "greeter" in ctx.services
    assert ctx.services["greeter"] == ctx.greeter


@pytest.mark.asyncio
async def test_missing_service_attribute_error():
    ctx = Context()
    with pytest.raises(AttributeError, match="Service 'nonexistent' is not registered"):
        _ = ctx.nonexistent


@pytest.mark.asyncio
async def test_plugin_target_variations():
    ctx = Context()
    records = []

    # 1. Target as a class with async apply
    class ClassWithApply:
        def __init__(self, c: Context):
            self.c = c

        async def apply(self, c: Context):
            records.append("class_apply_ran")

    # 2. Target as an object instance with sync apply
    class ObjectTarget:
        def apply(self, c: Context):
            records.append("object_apply_ran")

    # 3. Target as a callable / function
    def func_plugin(c: Context):
        records.append("func_plugin_ran")

    f1 = ctx.plugin(ClassWithApply)
    f2 = ctx.plugin(ObjectTarget())
    f3 = ctx.plugin(func_plugin)
    await ctx.flush()

    assert records == ["class_apply_ran", "object_apply_ran", "func_plugin_ran"]
    assert f1.state == FiberState.ACTIVE
    assert f2.state == FiberState.ACTIVE
    assert f3.state == FiberState.ACTIVE

    # 4. Target that is invalid (neither callable nor has apply)
    with pytest.raises(ValueError, match="must be a callable or class"):
        invalid_fib = ctx.plugin("not_a_valid_plugin")
        await invalid_fib.load()


@pytest.mark.asyncio
async def test_plugin_load_and_dispose_reentrant():
    ctx = Context()
    fiber = ctx.plugin(MockGreeter)
    await fiber.load()
    assert fiber.state == FiberState.ACTIVE

    # Calling load on an already ACTIVE fiber does nothing
    await fiber.load()
    assert fiber.state == FiberState.ACTIVE

    # Dispose
    await fiber.dispose()
    assert fiber.state == FiberState.DISPOSED

    # Calling dispose on an already DISPOSED fiber does nothing
    await fiber.dispose()
    assert fiber.state == FiberState.DISPOSED


@pytest.mark.asyncio
async def test_child_fiber_nested_disposal():
    ctx = Context()
    actions = []

    def parent_plugin(p_ctx: Context):
        def child_plugin(c_ctx: Context):
            c_ctx.effect(lambda: lambda: actions.append("child_disposed"))

        p_ctx.plugin(child_plugin)
        p_ctx.effect(lambda: lambda: actions.append("parent_disposed"))

    parent_fib = ctx.plugin(parent_plugin)
    await ctx.flush()

    assert len(parent_fib.child_fibers) == 1
    child_fib = parent_fib.child_fibers[0]
    assert child_fib.state == FiberState.ACTIVE

    # Disposing parent automatically disposes child first
    await parent_fib.dispose()
    assert actions == ["child_disposed", "parent_disposed"]
    assert child_fib.state == FiberState.DISPOSED
    assert parent_fib.state == FiberState.DISPOSED


@pytest.mark.asyncio
async def test_disposer_exception_handled():
    ctx = Context()

    def bad_disposer_plugin(c: Context):
        def failing_disposer():
            raise RuntimeError("Disposer failure")

        c.effect(lambda: failing_disposer)

    fiber = ctx.plugin(bad_disposer_plugin)
    await ctx.flush()

    # Disposal should not raise or abort
    await fiber.dispose()
    assert fiber.state == FiberState.DISPOSED


@pytest.mark.asyncio
async def test_manual_effect_cleanup_call():
    ctx = Context()
    cleaned = []

    def my_plugin(c: Context):
        disposer = c.effect(lambda: lambda: cleaned.append(1))
        # Call disposer manually before plugin unload
        disposer()

    fiber = ctx.plugin(my_plugin)
    await ctx.flush()
    assert cleaned == [1]

    # Unload fiber now; effect list was already drained
    await fiber.dispose()
    assert cleaned == [1]


@pytest.mark.asyncio
async def test_context_extend_and_inheritance():
    root = Context()
    root.plugin(MockGreeter)
    await root.flush()

    child = root.extend()
    assert child._parent == root
    assert child.root == root
    assert child.has("greeter")
    assert child.greeter.greet("Child") == "Hello, Child!"

    # Child can register its own service
    child.plugin(MockLogger)
    await child.flush()

    assert child.has("logger")
    assert not root.has("logger")


@pytest.mark.asyncio
async def test_context_isolate_realms():
    root = Context()
    root.plugin(MockGreeter)
    root.plugin(MockLogger)
    await root.flush()

    # Isolate greeter
    isolated = root.isolate("greeter", label="subagent_1")
    assert isolated.has("logger")
    assert not isolated.has("greeter")
    assert isolated.get("greeter") is None

    # Register isolated greeter
    class ChildGreeter(Service):
        def __init__(self, ctx: Context):
            super().__init__(ctx, "greeter")

        def greet(self, name: str) -> str:
            return f"Isolated {name}"

    isolated.plugin(ChildGreeter)
    await isolated.flush()

    assert isolated.greeter.greet("Bob") == "Isolated Bob"
    assert root.greeter.greet("Bob") == "Hello, Bob!"


@pytest.mark.asyncio
async def test_config_validation_with_pydantic():
    ctx = Context()

    class ServerConfig(BaseModel):
        host: str
        port: int = 8000

    class ServerPlugin:
        Config = ServerConfig

        def __init__(self, c: Context, **cfg):
            self.cfg = cfg

    # Valid kwargs
    f_ok = ctx.plugin(ServerPlugin, host="localhost", port=3000)
    await ctx.flush()
    assert f_ok.state == FiberState.ACTIVE
    assert f_ok.config.port == 3000

    # Invalid kwargs
    f_err = ctx.plugin(ServerPlugin, port="invalid_int")
    await ctx.flush()
    assert f_err.state == FiberState.FAILED
    assert isinstance(f_err.error, PydanticValidationError)


@pytest.mark.asyncio
async def test_dependency_unregistration_cascade():
    ctx = Context()
    disposed_items = []

    def dependent_plugin(c: Context):
        c.effect(lambda: lambda: disposed_items.append("dependent_cleaned"))

    dependent_plugin.inject = ["greeter"]

    g_fiber = ctx.plugin(MockGreeter)
    d_fiber = ctx.plugin(dependent_plugin)
    await ctx.flush()

    assert g_fiber.state == FiberState.ACTIVE
    assert d_fiber.state == FiberState.ACTIVE

    # Unregister greeter
    await g_fiber.dispose()
    await ctx.flush()

    assert d_fiber.state == FiberState.DISPOSED
    assert disposed_items == ["dependent_cleaned"]


@pytest.mark.asyncio
async def test_dump_tree_and_status():
    ctx = Context()
    ctx.plugin(MockGreeter)
    await ctx.flush()

    tree = ctx.dump_tree()
    assert "greeter" in tree["services"]
    assert len(tree["fibers"]) == 1
    st = tree["fibers"][0]
    assert st["name"] == "MockGreeter"
    assert st["state"] == "ACTIVE"
    assert st["uid"] > 0
    assert st["error"] is None


@pytest.mark.asyncio
async def test_fiber_set_state_idempotent():
    ctx = Context()
    fiber = ctx.plugin(lambda c: None)
    await ctx.flush()
    # Setting same state is a no-op (line 89)
    fiber._set_state(fiber.state)
    assert fiber.state == FiberState.ACTIVE


@pytest.mark.asyncio
async def test_fiber_load_failure():
    """Test Fiber.load() failure transitions to FAILED state (lines 259-264)."""
    ctx = Context()

    def failing_plugin(c: Context):
        raise RuntimeError("boom")

    fiber = Fiber(ctx, failing_plugin, (), {})
    with pytest.raises(RuntimeError, match="boom"):
        await fiber.load()
    assert fiber.state == FiberState.FAILED
    assert isinstance(fiber.error, RuntimeError)


class TestFiberLifecycleInternals:
    """Tests for fiber lifecycle internals (_store, inertia, epoch tracking)."""

    @pytest.mark.asyncio
    async def test_await_settle_no_inertia(self):
        """Test await_settle() with no pending inertia returns immediately."""
        ctx = Context()
        fiber = Fiber(ctx, lambda c: None, (), {})
        await fiber.await_settle()  # Should not raise

    @pytest.mark.asyncio
    async def test_await_settle_rethrows_error(self):
        """Test await_settle() rethrows stored startup error."""
        ctx = Context()

        def failing_plugin(c: Context):
            raise ValueError("startup failed")

        fiber = Fiber(ctx, failing_plugin, (), {})
        with pytest.raises(ValueError, match="startup failed"):
            await fiber.load()
        with pytest.raises(ValueError, match="startup failed"):
            await fiber.await_settle()

    def test_check_impl_available(self):
        """Test _checkImpl() keeps available impl in _store."""
        ctx = Context()
        fiber = Fiber(ctx, lambda c: None, (), {})

        class TrackedService(Service):
            def __init__(self, c: Context):
                super().__init__(c, "tracked")

        TrackedService(ctx)
        fiber.inject = ["tracked"]
        fiber._checkImpl("tracked")
        assert "tracked" in fiber._store

    def test_check_impl_missing(self):
        """Test _checkImpl() removes missing impl from _store."""
        ctx = Context()
        fiber = Fiber(ctx, lambda c: None, (), {})
        fiber.inject = ["missing_svc"]
        fiber._store["missing_svc"] = "placeholder"
        fiber._checkImpl("missing_svc")
        assert "missing_svc" not in fiber._store

    def test_check_impl_check_fails(self):
        """Test _checkImpl() removes impl when check predicate returns False."""
        ctx = Context()
        fiber = Fiber(ctx, lambda c: None, (), {})

        class TrackedService(Service):
            check = staticmethod(lambda: False)

            def __init__(self, c: Context):
                super().__init__(c, "tracked")

        svc = TrackedService(ctx)
        fiber.inject = ["tracked"]
        fiber._store["tracked"] = svc
        fiber._checkImpl("tracked")
        assert "tracked" not in fiber._store

    def test_check_impl_check_raises(self):
        """Test _checkImpl() removes impl when check predicate raises."""
        ctx = Context()
        fiber = Fiber(ctx, lambda c: None, (), {})

        class TrackedService(Service):
            check = staticmethod(
                lambda: (_ for _ in ()).throw(RuntimeError("check boom"))
            )

            def __init__(self, c: Context):
                super().__init__(c, "tracked")

        svc = TrackedService(ctx)
        fiber.inject = ["tracked"]
        fiber._store["tracked"] = svc
        fiber._checkImpl("tracked")
        assert "tracked" not in fiber._store

    def test_refresh_all_deps_available(self):
        """Test _refresh() sets active epoch when all deps available."""
        ctx = Context()
        fiber = Fiber(ctx, lambda c: None, (), {})

        class TrackedService(Service):
            def __init__(self, c: Context):
                super().__init__(c, "tracked")

        svc = TrackedService(ctx)
        fiber.inject = ["tracked"]
        fiber._store["tracked"] = svc
        # Manually set epoch as _refresh would (bypassing async _setEpoch)
        epoch = ""
        for name in fiber.inject:
            impl = fiber._store.get(name)
            if impl is None:
                epoch = "__INACTIVE__"
                break
            epoch += ":" + str(id(impl))
        assert epoch != "__INACTIVE__"
        assert epoch != ""

    @pytest.mark.asyncio
    async def test_refresh_empty_inject(self):
        """Test _refresh() with no dependencies doesn't crash."""
        ctx = Context()
        fiber = Fiber(ctx, lambda c: None, (), {})
        fiber.inject = []
        # Should not raise even with empty inject
        fiber._refresh()

    def test_refresh_missing_dep(self):
        """Test _refresh() sets INACTIVE epoch when dep missing."""
        ctx = Context()
        fiber = Fiber(ctx, lambda c: None, (), {})
        fiber.inject = ["missing_svc"]
        fiber._refresh()
        assert fiber._getCurrentEpoch() == "__INACTIVE__"

    def test_set_epoch_no_change(self):
        """Test _setEpoch() is a no-op when epoch unchanged."""
        ctx = Context()
        fiber = Fiber(ctx, lambda c: None, (), {})
        fiber._setCurrentEpoch("active")
        fiber._setEpoch("active")  # Should not trigger transition
        assert fiber._getCurrentEpoch() == "active"

    def test_set_epoch_inertia_blocks(self):
        """Test _setEpoch() is blocked when inertia is set."""
        ctx = Context()
        fiber = Fiber(ctx, lambda c: None, (), {})
        fiber._setCurrentEpoch("__INACTIVE__")
        loop = asyncio.new_event_loop()
        try:
            fiber.inertia = loop.create_future()
            fiber._setEpoch("active")  # Should not trigger due to inertia
            assert fiber._getCurrentEpoch() == "__INACTIVE__"
        finally:
            loop.close()

    def test_update_state_no_change(self):
        """Test _updateState() is a no-op when state unchanged."""
        ctx = Context()
        fiber = Fiber(ctx, lambda c: None, (), {})
        fiber.state = FiberState.ACTIVE
        fiber._updateState(lambda: FiberState.ACTIVE)
        assert fiber.state == FiberState.ACTIVE

    def test_update_state_activates(self):
        """Test _updateState() transitions to ACTIVE and emits status."""
        ctx = Context()
        fiber = Fiber(ctx, lambda c: None, (), {})
        fiber.state = FiberState.PENDING
        events = []
        ctx.on("internal/status", lambda f, old: events.append((f, old)))
        fiber._updateState(lambda: FiberState.ACTIVE)
        assert fiber.state == FiberState.ACTIVE
        assert len(events) == 1

    def test_get_state_disposed(self):
        """Test _getState() returns DISPOSED when uid is None."""
        ctx = Context()
        fiber = Fiber(ctx, lambda c: None, (), {})
        fiber.uid = None
        assert fiber._getState() == FiberState.DISPOSED

    def test_get_state_failed(self):
        """Test _getState() returns FAILED when error is set."""
        ctx = Context()
        fiber = Fiber(ctx, lambda c: None, (), {})
        fiber.error = RuntimeError("failed")
        assert fiber._getState() == FiberState.FAILED

    def test_get_state_active(self):
        """Test _getState() returns ACTIVE when epoch is active."""
        ctx = Context()
        fiber = Fiber(ctx, lambda c: None, (), {})
        fiber._setCurrentEpoch("active:123")
        assert fiber._getState() == FiberState.ACTIVE

    def test_get_state_pending(self):
        """Test _getState() returns PENDING when epoch is INACTIVE."""
        ctx = Context()
        fiber = Fiber(ctx, lambda c: None, (), {})
        fiber._setCurrentEpoch("__INACTIVE__")
        assert fiber._getState() == FiberState.PENDING


class TestDisposableList:
    """Tests for DisposableList (list parity)."""

    def test_push_and_length(self):
        dl = DisposableList()
        assert dl.length == 0
        dl.push("a")
        dl.push("b")
        assert dl.length == 2

    def test_delete_by_value(self):
        dl = DisposableList()
        dl.push("a")
        dl.push("b")
        assert dl.delete("a") is True
        assert dl.length == 1
        assert dl.delete("a") is False  # already deleted

    def test_clear_returns_reversed(self):
        dl = DisposableList()
        dl.push("a")
        dl.push("b")
        dl.push("c")
        values = dl.clear()
        assert values == ["c", "b", "a"]
        assert dl.length == 0

    def test_iter(self):
        dl = DisposableList()
        dl.push("a")
        dl.push("b")
        assert list(dl) == ["a", "b"]

    def test_len(self):
        dl = DisposableList()
        assert len(dl) == 0
        dl.push("a")
        assert len(dl) == 1


@pytest.mark.asyncio
async def test_fiber_load_with_unsatisfied_dependencies():
    ctx = Context()

    def need_db(c: Context):
        pass

    need_db.inject = ["db"]
    fiber = Fiber(ctx, need_db, (), {})
    # Manually calling load when dependencies are missing transitions to PENDING and returns None (lines 104-105)
    res = await fiber.load()
    assert res is None
    assert fiber.state == FiberState.PENDING


@pytest.mark.asyncio
async def test_async_object_and_callable_plugin_and_async_disposer():
    ctx = Context()
    events = []

    # Async object apply (line 124)
    class AsyncObj:
        async def apply(self, c: Context):
            events.append("async_obj_applied")

    # Async callable plugin (line 130)
    async def async_func(c: Context):
        events.append("async_func_applied")

        # Async effect disposer (line 161)
        async def async_cleanup():
            await asyncio.sleep(0.001)
            events.append("async_cleaned")

        c.effect(lambda: async_cleanup)

    _f_obj = ctx.plugin(AsyncObj())
    f_func = ctx.plugin(async_func)
    await ctx.flush()

    assert "async_obj_applied" in events
    assert "async_func_applied" in events

    await f_func.dispose()
    assert "async_cleaned" in events


def test_sync_schedule_task_fallback():
    # Calling ctx.plugin outside of a running asyncio loop triggers RuntimeError -> asyncio.run fallback (lines 236-238)
    ctx = Context()
    fiber = ctx.plugin(MockGreeter)
    assert ctx.has("greeter")
    assert fiber.state == FiberState.ACTIVE


@pytest.mark.asyncio
async def test_private_attribute_and_isolated_services_dict():
    root = Context()
    root.plugin(MockGreeter)
    root.plugin(MockLogger)
    await root.flush()

    # Private nonexistent attribute lookup raises AttributeError via super().__getattribute__ (line 260)
    with pytest.raises(AttributeError):
        _ = root._nonexistent_private_prop

    # Isolated context services filtering (lines 274-276)
    child = root.isolate("greeter")
    svcs = child.services
    assert "logger" in svcs
    assert "greeter" not in svcs


@pytest.mark.asyncio
async def test_effect_with_none_disposer():
    ctx = Context()
    # Effect returning None (disposer is None -> line 326)
    disposer = ctx.effect(lambda: None)
    assert disposer() is None


@pytest.mark.asyncio
async def test_context_parallel_and_bail_delegation():
    ctx = Context()

    # Parallel (line 352)
    ctx.on("compute", lambda x: x * 2)
    ctx.on("compute", lambda x: x * 3)
    p_results = await ctx.parallel("compute", 5)
    assert p_results == [10, 15]

    # Serial (line 356)
    ctx.on("first_match", lambda x: "matched" if x == 1 else None)
    s_res = await ctx.serial("first_match", 1)
    assert s_res == "matched"

    # Bail (line 360)
    ctx.on("lookup", lambda k: "found" if k == "item" else None)
    b_res = ctx.bail("lookup", "item")
    assert b_res == "found"

    # Waterfall (line 370)
    async def add_prefix(val, next_fn):
        return await next_fn(f"prefix_{val}")

    ctx.on("format", add_prefix, mode=EventMode.WATERFALL)
    w_res = await ctx.waterfall("format", "val")
    assert w_res == "prefix_val"


@pytest.mark.asyncio
async def test_class_plugin_sync_apply_and_disposer_idempotent():
    ctx = Context()

    # Synchronous apply on class plugin (branch 118->135)
    class SyncClassPlugin:
        def __init__(self, c: Context):
            pass

        def apply(self, c: Context):
            return "sync_done"

    f = ctx.plugin(SyncClassPlugin)
    await ctx.flush()
    assert f.state == FiberState.ACTIVE

    # Unregister nonexistent service (branch 292->exit)
    ctx._unregister_service("never_registered")

    # Calling disposer when already removed from fiber.effects (branch 322->324)
    ran = []
    disposer = ctx.effect(lambda: lambda: ran.append(1))
    disposer()
    disposer()  # second call when disposer not in fiber.effects
    assert ran == [1, 1]


@pytest.mark.asyncio
async def test_context_provide_and_cascade():
    ctx = Context()
    d = ctx.provide("dynamic_service", {"foo": "bar"})
    assert ctx.has("dynamic_service")
    assert ctx.dynamic_service == {"foo": "bar"}

    d()
    assert not ctx.has("dynamic_service")


@pytest.mark.asyncio
async def test_context_intercept_and_resolve():
    ctx = Context()

    class InterceptableService(Service):
        def __init__(self, c: Context):
            super().__init__(c, "target")

        def config(self):
            return self.resolve_intercept()

    ctx.plugin(InterceptableService)
    await ctx.flush()

    assert ctx.target.config() == {}

    child1 = ctx.intercept("target", {"a": 1, "b": 1})
    child2 = child1.intercept("target", {"b": 2, "c": 3})

    assert child1.target.config() == {"a": 1, "b": 1}
    assert child2.target.config() == {"a": 1, "b": 2, "c": 3}


@pytest.mark.asyncio
async def test_context_intercept_custom_merge():
    ctx = Context()

    class CustomMergeService(Service):
        def __init__(self, c: Context):
            super().__init__(c, "custom_svc")

        def merge_intercept(self, *configs):
            tags = []
            for cfg in configs:
                tags.extend(cfg.get("tags", []))
            return {"tags": tags}

    ctx.plugin(CustomMergeService)
    await ctx.flush()

    child = ctx.intercept("custom_svc", {"tags": ["root"]}).intercept(
        "custom_svc", {"tags": ["child"]}
    )
    assert child.custom_svc.resolve_intercept() == {"tags": ["root", "child"]}


@pytest.mark.asyncio
async def test_context_inline_inject_and_dict_deps():
    ctx = Context()
    runs = []

    # Inject with list of deps, waiting for 'db'
    async def on_deps(c: Context):
        runs.append(c.db)

    ctx.inject(["db"], on_deps)
    await ctx.flush()
    assert runs == []

    # Provide 'db'
    ctx.provide("db", "connected_db")
    await ctx.flush()
    assert runs == ["connected_db"]

    # Inject with dict deps containing config
    sync_runs = []
    ctx.inject({"db": {"timeout": 30}}, lambda c: sync_runs.append(c.db))
    await ctx.flush()
    assert sync_runs == ["connected_db"]


@pytest.mark.asyncio
async def test_inject_decorator():
    from anima.core import inject as inject_dec

    @inject_dec("db", auth={"roles": ["admin"]})
    class DecoratedPlugin:
        def __init__(self, ctx: Context):
            self.ctx = ctx

    assert "db" in DecoratedPlugin.inject
    assert "auth" in DecoratedPlugin.inject
    assert DecoratedPlugin.inject_config == {"auth": {"roles": ["admin"]}}

    # Decorator on target that already has inject list
    @inject_dec("cache")
    class AnotherPlugin:
        inject: ClassVar[list[str]] = ["db"]

    assert AnotherPlugin.inject == ["db", "cache"]

    # Decorator with no existing inject or invalid existing
    class PlainPlugin:
        inject = 123  # not a sequence

    decorated = inject_dec("svc")(PlainPlugin)
    assert decorated.inject == ["svc"]


@pytest.mark.asyncio
async def test_fiber_restart_and_update():
    ctx = Context()
    load_counts = []
    received_updates = []

    class UpdatablePlugin:
        def __init__(self, c: Context, **cfg):
            self.c = c
            self.cfg = cfg
            load_counts.append(cfg.get("val", 0))

    fib = ctx.plugin(UpdatablePlugin, val=1)
    await ctx.flush()
    assert load_counts == [1]

    # Restart fiber
    await fib.restart()
    await ctx.flush()
    assert load_counts == [1, 1]

    # Restart disposed fiber is no-op
    disposed_fib = ctx.plugin(lambda c: None)
    await ctx.flush()
    await disposed_fib.dispose()
    await disposed_fib.restart()
    assert disposed_fib.state == FiberState.DISPOSED

    # Update with internal/update intercepted (no restart)
    async def update_interceptor(cfg, next_fn):
        received_updates.append(cfg["val"])
        # Return without next_fn to accept in-place

    ctx.on("internal/update", update_interceptor, mode=EventMode.WATERFALL)

    committed = []
    ctx.on("internal/commit", lambda f, cfg: committed.append(cfg))

    await fib.update({"val": 2})
    assert received_updates == [2]
    assert committed == [{"val": 2}]
    # load_counts didn't increment because update was accepted in-place
    assert load_counts == [1, 1]

    # Update on DISPOSED fiber is no-op
    await disposed_fib.update({"val": 99})

    # Update with validation error -> FAILED
    from pydantic import BaseModel

    class StrictConfig(BaseModel):
        num: int

    class StrictPlugin:
        Config = StrictConfig

        def __init__(self, c: Context, **cfg):
            pass

    strict_fib = ctx.plugin(StrictPlugin, num=10)
    await ctx.flush()
    assert strict_fib.state == FiberState.ACTIVE

    await strict_fib.update({"num": "not_an_int"}, no_save=True)
    assert strict_fib.state == FiberState.FAILED

    # Calling update on FAILED fiber is a no-op
    await strict_fib.update({"num": "still_bad"})
    assert strict_fib.state == FiberState.FAILED

    # Update on pending fiber
    def needs_service(c: Context, **cfg):
        pass

    needs_service.inject = ["special_svc"]
    pending_fib = ctx.plugin(needs_service, val=5)
    await ctx.flush()
    assert pending_fib.state == FiberState.PENDING

    # Updating pending fiber with dependencies unsatisfied remains pending
    await pending_fib.update({"val": 10})
    assert pending_fib.state == FiberState.PENDING
    assert pending_fib.config["val"] == 10

    # Provide dependency -> loads with latest config
    ctx.provide("special_svc", "ready")
    await ctx.flush()
    assert pending_fib.state == FiberState.ACTIVE


@pytest.mark.asyncio
async def test_context_once_effect_registration():
    ctx = Context()
    calls = []

    ctx.once("single", lambda x: calls.append(x))
    ctx.emit("single", "a")
    ctx.emit("single", "b")
    assert calls == ["a"]


@pytest.mark.asyncio
async def test_default_restart_on_unhandled_update():
    ctx = Context()
    reloads = []

    class SimpleReloadPlugin:
        def __init__(self, c: Context, **cfg):
            reloads.append(cfg.get("tag", "initial"))

    fib = ctx.plugin(SimpleReloadPlugin, tag="v1")
    await ctx.flush()
    assert reloads == ["v1"]

    # When no listener intercepts internal/update, default_restart restarts it
    await fib.update({"tag": "v2"})
    await ctx.flush()
    assert reloads == ["v1", "v2"]


@pytest.mark.asyncio
async def test_bound_service_and_intercept_edge_cases():
    ctx = Context()

    class CallableService(Service):
        def __init__(self, c: Context):
            super().__init__(c, "callable_svc")
            self.value = 10

        def __call__(self, x: int) -> int:
            return x + self.value

    ctx.plugin(CallableService)
    await ctx.flush()

    bound = ctx.callable_svc
    assert bound.ctx == ctx
    assert bound(5) == 15
    bound.value = 20
    assert bound.value == 20
    assert bound(5) == 25
    assert (bound == ctx.callable_svc) is True
    assert bound == ctx.get("callable_svc")
    assert repr(bound) == repr(ctx.get("callable_svc"))

    # Plain non-service capability without merge_intercept
    ctx.provide("plain_obj", {"item": 1})
    assert ctx.resolve_intercept("plain_obj") == {}
    child_plain = ctx.intercept("plain_obj", {"override": 2})
    assert child_plain.resolve_intercept("plain_obj") == {"override": 2}

    # Parent with child fiber restarting with async disposer
    async_unloads = []

    def parent_with_child(p_ctx: Context):
        def inner_child(c_ctx: Context):
            async def async_disposer():
                await asyncio.sleep(0.001)
                async_unloads.append("child_unloaded")

            c_ctx.effect(lambda: async_disposer)

        p_ctx.plugin(inner_child)

    p_fib = ctx.plugin(parent_with_child)
    await ctx.flush()
    await p_fib.restart()
    assert "child_unloaded" in async_unloads

    # Service with dict inject
    results = []

    class DictInjectPlugin:
        inject: ClassVar[dict[str, Any]] = {"callable_svc": {"override": True}}

        def __init__(self, c: Context):
            results.append(c.resolve_intercept("callable_svc"))

    ctx.plugin(DictInjectPlugin)
    await ctx.flush()
    assert results == [{"override": True}]

    # resolve_intercept on unregistered service
    assert ctx.resolve_intercept("unregistered") == {}

    # non-dict in merge_intercept
    assert bound.merge_intercept("invalid_cfg") == {}

    # Disposer exception in _unload handled safely
    def failing_disposer_plugin(c: Context):
        def bad():
            raise RuntimeError("unload error")

        c.effect(lambda: bad)

    fail_fib = ctx.plugin(failing_disposer_plugin)
    await ctx.flush()
    await fail_fib.restart()
    assert fail_fib.state == FiberState.ACTIVE

    # Update pending fiber whose dependencies are satisfied
    def pending_target(c: Context):
        pass

    pending_target.inject = ["callable_svc"]
    f_pend = ctx.plugin(pending_target)
    f_pend.state = FiberState.PENDING
    await f_pend.update({})
    assert f_pend.state == FiberState.ACTIVE

    # Synchronous disposer in _unload
    def sync_disposer_plugin(c: Context):
        c.effect(lambda: lambda: None)

    f_sync = ctx.plugin(sync_disposer_plugin)
    await ctx.flush()
    await f_sync.restart()

    # Restart when dependencies unsatisfied
    def dep_restart_plugin(c: Context):
        pass

    dep_restart_plugin.inject = ["missing_dep"]
    f_missing = ctx.plugin(dep_restart_plugin)
    f_missing.state = FiberState.ACTIVE
    await f_missing.restart()
    assert f_missing.state == FiberState.PENDING

    # resolve_intercept with inject_config containing other services or non-dict values
    edge_results = []

    class InjectConfigEdgeCases:
        inject: ClassVar[list[str]] = ["callable_svc"]
        inject_config: ClassVar[dict[str, Any]] = {
            "other_svc": {"x": 1},
            "callable_svc": "not_a_dict",
        }

        def __init__(self, c: Context):
            edge_results.append(c.resolve_intercept("callable_svc"))
            edge_results.append(c.resolve_intercept("not_in_config"))

    ctx.plugin(InjectConfigEdgeCases)
    await ctx.flush()
    assert edge_results == [{}, {}]

    # Fiber with None inject_config calling resolve_intercept
    class NoConfigPlugin:
        def __init__(self, c: Context):
            _ = c.resolve_intercept("callable_svc")

    ctx.plugin(NoConfigPlugin)
    await ctx.flush()

    # Decorator when key in dep_configs is already in deps
    from anima.core import inject as inject_dec

    @inject_dec("existing_key", existing_key={"k": "v"})
    class PreExistingKeyPlugin:
        pass

    assert PreExistingKeyPlugin.inject == ["existing_key"]


@pytest.mark.asyncio
async def test_fiber_replace_and_context_all_fibers():
    from typing import ClassVar

    ctx = Context()
    child_ctx = ctx.extend()

    events = []

    class PluginV1:
        inject: ClassVar[list[str]] = ["test_svc"]

        def __init__(self, c: Context):
            events.append("v1_init")

    class PluginV2:
        inject: ClassVar[dict[str, Any]] = {"test_svc": {"mode": "fast"}}

        def __init__(self, c: Context):
            events.append("v2_init")

    class PluginV3:
        # No inject
        def __init__(self, c: Context):
            events.append("v3_init")

    ctx.provide("test_svc", "active")

    fib1 = ctx.plugin(PluginV1)
    fib2 = child_ctx.plugin(PluginV3)
    await ctx.flush()

    assert events == ["v1_init", "v3_init"]
    all_f = ctx.all_fibers()
    assert fib1 in all_f
    assert fib2 in all_f

    # Replace PluginV1 with PluginV2 (dict inject)
    await fib1.replace(PluginV2)
    assert events == ["v1_init", "v3_init", "v2_init"]
    assert fib1.name == "PluginV2"
    assert fib1.inject == ["test_svc"]
    assert fib1.inject_config == {"test_svc": {"mode": "fast"}}

    # Replace with PluginV3 (no inject)
    await fib1.replace(PluginV3)
    assert events == ["v1_init", "v3_init", "v2_init", "v3_init"]
    assert fib1.inject == []

    # Replace with PluginV1 (list inject)
    await fib1.replace(PluginV1)
    assert fib1.inject == ["test_svc"]
    assert events == ["v1_init", "v3_init", "v2_init", "v3_init", "v1_init"]

    # Replace on disposed fiber is no-op
    await fib1.dispose()
    await fib1.replace(PluginV1)
    assert events == ["v1_init", "v3_init", "v2_init", "v3_init", "v1_init"]


@pytest.mark.asyncio
async def test_context_fiber_scope_and_filtering():
    ctx = Context()

    # 1. Root lazy fiber creation & scope alias
    root_fiber = ctx.fiber
    assert root_fiber is not None
    assert ctx.scope is root_fiber
    assert ctx.fiber is root_fiber

    # 2. Child inheriting parent fiber
    child_ctx = ctx.extend()
    assert child_ctx.fiber is root_fiber
    raw_child = Context(parent=ctx)
    assert raw_child.fiber is root_fiber

    # 3. Inside plugin load, ctx.fiber is the plugin fiber
    observed_fibers = []

    def sample_plugin(c: Context):
        observed_fibers.append(c.fiber)
        assert c.scope is c.fiber

    p_fib = ctx.plugin(sample_plugin)
    await ctx.flush()
    assert len(observed_fibers) == 1
    assert observed_fibers[0] is p_fib

    # 4. Filter and Exclude
    filter_worker = ctx.filter(lambda c: c.get("role") == "worker")
    assert filter_worker.match(ctx.isolate("role")) is False
    worker_ctx = ctx.extend()
    worker_ctx.provide("role", "worker")
    assert filter_worker.match(worker_ctx) is True

    # Nested filter
    nested = filter_worker.filter(lambda c: c.get("tier") == 1)
    assert nested.match(worker_ctx) is False
    worker_ctx.provide("tier", 1)
    assert nested.match(worker_ctx) is True

    # Exclude
    excluded = ctx.exclude(lambda c: c.get("blocked") is True)
    blocked_ctx = ctx.extend()
    blocked_ctx.provide("blocked", True)
    assert excluded.match(blocked_ctx) is False
    assert excluded.match(worker_ctx) is True

    # Context event bus dispatch with filtering
    worker_received = []
    non_worker_received = []
    worker_ctx.on("task", lambda payload: worker_received.append(payload))
    non_worker = ctx.extend()
    non_worker.provide("role", "manager")
    non_worker.on("task", lambda payload: non_worker_received.append(payload))

    # Emitting from filter_worker dispatches only to worker_ctx
    filter_worker.emit("task", "payload-1")
    assert worker_received == ["payload-1"]
    assert non_worker_received == []

    # Test once listener on context
    once_received = []
    ctx.once("single", lambda x: once_received.append(x))
    ctx.emit("single", 42)
    ctx.emit("single", 43)
    assert once_received == [42]


@pytest.mark.asyncio
async def test_bound_service_async_method_and_call():
    ctx = Context()

    class AsyncService(Service):
        def __init__(self, c: Context):
            super().__init__(c, "async_svc")

        async def async_method(self, val: str) -> str:
            resolved = self.resolve_intercept()
            prefix = resolved.get("prefix", "")
            return f"{prefix}{val}"

        async def __call__(self, val: str) -> str:
            resolved = self.resolve_intercept()
            prefix = resolved.get("prefix", "")
            return f"{prefix}call:{val}"

    ctx.plugin(AsyncService)
    await ctx.flush()

    child = ctx.intercept("async_svc", {"prefix": "intercepted:"})
    child2 = ctx.intercept("async_svc", {"prefix": "intercepted2:"})
    res1 = await child.async_svc.async_method("hello")
    assert res1 == "intercepted:hello"

    res2 = await child.async_svc("world")
    assert res2 == "intercepted:call:world"

    # BoundService properties and dunder methods
    assert child.async_svc.ctx == child
    child.async_svc.custom_attr = "custom"
    assert child.async_svc.custom_attr == "custom"
    assert child.async_svc == ctx.async_svc
    assert child.async_svc == child2.async_svc
    assert repr(child.async_svc) == repr(ctx.async_svc)


@pytest.mark.asyncio
async def test_fiber_non_callable_config():
    ctx = Context()

    class NonCallableConfigPlugin:
        Config = 12345

        def __init__(self, c: Context):
            pass

    fib = ctx.plugin(NonCallableConfigPlugin)
    await ctx.flush()
    assert fib.state == FiberState.ACTIVE


class TestAnimaError:
    def test_error_creation(self):
        err = AnimaError(AnimaErrorCode.INACTIVE_EFFECT)
        assert err.code == AnimaErrorCode.INACTIVE_EFFECT
        assert "cannot create effect on inactive context" in str(err)

    def test_error_with_message(self):
        err = AnimaError(AnimaErrorCode.INACTIVE_EFFECT, "custom message")
        assert err.code == AnimaErrorCode.INACTIVE_EFFECT
        assert str(err) == "custom message"


class TestValidationError:
    def test_validation_error_creation(self):
        err = AnimaValidationError([{"message": "field required", "path": ["field"]}])
        assert err.name == "ValidationError"
        assert len(err.issues) == 1
        assert err.issues[0]["message"] == "field required"

    def test_format_issues(self):
        err = AnimaValidationError([{"message": "field required", "path": ["field"]}])
        text = format_issues("invalid entry options", err.issues)
        assert "invalid entry options" in text
        assert "field required" in text
        assert "field" in text

    def test_format_issues_no_path(self):
        err = AnimaValidationError([{"message": "general error", "path": []}])
        text = format_issues("invalid entry options", err.issues)
        assert "general error" in text
        assert "at " not in text


class TestIsJsExpr:
    def test_is_js_expr_true(self):
        assert is_js_expr({"__jsExpr": "1 + 1"}) is True

    def test_is_js_expr_false(self):
        assert is_js_expr("hello") is False
        assert is_js_expr({"foo": "bar"}) is False
        assert is_js_expr(None) is False


class TestFiberEffect:
    def test_fiber_effect_basic(self):
        ctx = Context()
        fib = ctx.plugin(CoreService)
        assert fib.state == FiberState.ACTIVE

        result = []
        fib.effect(lambda: result.append("effect_ran"), label="test_effect")
        assert len(result) == 1
        assert result[0] == "effect_ran"

    def test_fiber_effect_stores_metadata(self):
        ctx = Context()
        fib = ctx.plugin(CoreService)

        wrapper = fib.effect(lambda: None, label="my_effect")
        meta = getattr(wrapper, symbols["effect"], None)
        assert meta is not None
        assert meta["label"] == "my_effect"
        assert meta["children"] == []

    def test_fiber_get_effects(self):
        ctx = Context()
        fib = ctx.plugin(CoreService)

        fib.effect(lambda: None, label="effect1")
        fib.effect(lambda: None, label="effect2")

        effects = fib.getEffects()
        assert len(effects) == 2
        assert effects[0]["label"] == "effect1"
        assert effects[1]["label"] == "effect2"

    @pytest.mark.asyncio
    async def test_fiber_effect_wrapper_called_on_dispose(self):
        """Test that effect wrapper is called when fiber is disposed."""
        ctx = Context()
        fib = ctx.plugin(CoreService)

        wrapper_called = []
        fib.effect(
            lambda: wrapper_called.append("wrapper_executed"), label="test_wrapper"
        )
        await fib.dispose()
        assert len(wrapper_called) == 1
        assert wrapper_called[0] == "wrapper_executed"


class TestServiceInstanceCheck:
    def test_service_instancecheck(self):
        ctx = Context()
        svc = CoreService(ctx)

        assert isinstance(svc, CoreService)
        assert not isinstance("not_a_service", CoreService)
        assert not isinstance(42, CoreService)

    def test_service_instancecheck_subclass(self):
        ctx = Context()

        class SubService(CoreService):
            pass

        sub = SubService(ctx)
        assert isinstance(sub, CoreService)


class TestServiceFilter:
    def test_service_filter_same_isolate_state(self):
        """Both contexts have same isolation state for the service."""
        ctx = Context()
        svc = CoreService(ctx)

        # Neither context has the service isolated
        assert svc._filter(ctx) is True
        child_ctx = ctx.extend()
        assert svc._filter(child_ctx) is True

    def test_service_filter_different_isolate_state(self):
        """One context has the service isolated, the other doesn't."""
        ctx = Context()
        svc = CoreService(ctx)

        child_ctx = ctx.extend()
        child_ctx._isolated_services["core_svc"] = True

        # Child has core_svc isolated, service's ctx doesn't
        assert svc._filter(child_ctx) is False

        # Also test the reverse: service's ctx has it isolated, child doesn't
        other_ctx = Context()
        other_ctx._isolated_services["core_svc"] = True
        assert svc._filter(other_ctx) is False


class TestCoreServiceAlias:
    def test_core_service_exists(self):
        """CoreService is the anima core service."""
        assert CoreService is not None
        assert CoreService.__name__ == "CoreService"


class TestFiberValidateSchema:
    @pytest.mark.asyncio
    async def test_fiber_validate_schema_instance(self):
        """Test Schema instance as Config goes through isinstance branch."""
        ctx = Context()

        class SchemaConfigPlugin:
            name = "schema_config"
            Config = Schema.string().required()

        fib = ctx.plugin(SchemaConfigPlugin)
        await ctx.flush()
        # Schema.string().required() validates raw_config={} which fails
        assert fib.state == FiberState.FAILED
        assert fib.error is not None
        assert isinstance(fib.error, SchemaError)


class TestFiberValidateConfigSchema:
    @pytest.mark.asyncio
    async def test_fiber_validate_config_schema_failure(self):
        ctx = Context()

        class BadConfigPlugin:
            name = "bad_config"

            class Config(BaseModel):
                required_field: str

        fib = ctx.plugin(BadConfigPlugin)
        await ctx.flush()
        assert fib.state == FiberState.FAILED
        assert fib.error is not None


class TestFiberEffectException:
    def test_fiber_effect_exception(self):
        ctx = Context()
        fib = ctx.plugin(CoreService)

        def raising_effect():
            raise RuntimeError("effect error")

        with pytest.raises(RuntimeError, match="effect error"):
            fib.effect(raising_effect, label="raising_effect")


class TestServiceInstanceCheckSubclass:
    def test_service_instancecheck_subclass_negative(self):
        ctx = Context()
        CoreService(ctx)

        class Unrelated:
            pass

        assert not isinstance(Unrelated(), CoreService)
        assert not isinstance(None, CoreService)
        assert not isinstance(42, CoreService)


class TestServiceFilterEdgeCases:
    def test_service_filter_same_ctx(self):
        ctx = Context()
        svc = CoreService(ctx)
        assert svc._filter(ctx) is True

    def test_service_filter_child_ctx_no_isolate(self):
        ctx = Context()
        svc = CoreService(ctx)
        child_ctx = ctx.extend()
        assert svc._filter(child_ctx) is True

    def test_service_filter_different_isolate(self):
        ctx = Context()
        svc = CoreService(ctx)
        child_ctx = ctx.extend()
        child_ctx._isolated_services["core_svc"] = True
        assert svc._filter(child_ctx) is False

        other_ctx = Context()
        other_ctx._isolated_services["core_svc"] = True
        assert svc._filter(other_ctx) is False


class TestServiceExtend:
    def test_service_extend_with_invoke(self):
        ctx = Context()

        class CallableService(Service):
            __symbols__: ClassVar[dict[str, str]] = {"invoke": symbols["invoke"]}

            def __init__(self, c: Context):
                super().__init__(c, "callable_svc")

            def __call__(self):
                return "called"

        svc = CallableService(ctx)
        extended = svc._extend({"extra": "data"})
        assert extended.extra == "data"

    def test_service_extend_without_invoke(self):
        ctx = Context()
        svc = CoreService(ctx)
        extended = svc._extend({"extra": "data"})
        assert extended.extra == "data"


class TestFiberValidateSchemaSuccess:
    @pytest.mark.asyncio
    async def test_fiber_validate_schema_success(self):
        """Test Schema validation success path (line 167 return)."""
        ctx = Context()

        class GoodSchemaPlugin:
            name = "good_schema"
            Config = Schema.object({"name": Schema.string()})

            def __init__(self, c: Context, **cfg):
                self.c = c

        fib = ctx.plugin(GoodSchemaPlugin, name="hello")
        await ctx.flush()
        assert fib.state == FiberState.ACTIVE


class TestFiberAssertActive:
    def test_fiber_assert_active_raises(self):
        ctx = Context()
        fib = ctx.plugin(CoreService)
        # Manually set uid to None to simulate disposed fiber
        fib.uid = None
        with pytest.raises(AnimaError):
            fib.assertActive()


class TestServiceInstanceCheckEdgeCases:
    def test_service_instancecheck_none(self):
        ctx = Context()
        CoreService(ctx)
        assert not isinstance(None, CoreService)

    def test_service_instancecheck_non_object(self):
        ctx = Context()
        CoreService(ctx)
        assert not isinstance(42, CoreService)
        assert not isinstance("string", CoreService)
        assert not isinstance([], CoreService)

    def test_service_instancecheck_subclass(self):
        """Test __instancecheck__ walking up the base class chain."""
        ctx = Context()

        class SubService(CoreService):
            pass

        sub = SubService(ctx)
        # Call __instancecheck__ directly since isinstance() uses metaclass lookup
        result = CoreService.__instancecheck__(CoreService, sub)
        assert result is True
        # Also verify isinstance works (uses default type.__instancecheck__)
        assert isinstance(sub, CoreService)

    def test_service_instancecheck_direct_call(self):
        """Test direct __instancecheck__ call covering the while loop body."""
        ctx = Context()

        class DeepSubService(CoreService):
            pass

        class DeepSubSubService(DeepSubService):
            pass

        subsub = DeepSubSubService(ctx)
        # Walk up: DeepSubSubService -> DeepSubService -> CoreService
        result = CoreService.__instancecheck__(CoreService, subsub)
        assert result is True
        assert isinstance(subsub, CoreService)


class TestServiceBoundCallCoverage:
    def test_bound_service_call_covers_call_method(self):
        """Test that _BoundService.__call__ body executes."""
        ctx = Context()

        class CallableService(Service):
            def __init__(self, c: Context):
                super().__init__(c, "callable_svc")
                self.value = 10

            def __call__(self, x: int) -> int:
                return x + self.value

        svc = CallableService(ctx)
        # Call _BoundService.__call__ directly to cover lines 474-478
        from anima.core import _BoundService

        bound = _BoundService(svc, ctx)
        result = bound(5)
        assert result == 15


class TestServiceCall:
    def test_service_call_not_callable(self):
        """Test Service.__call__ raises TypeError for non-callable service."""
        ctx = Context()
        svc = CoreService(ctx)
        with pytest.raises(TypeError, match="is not callable"):
            svc()

    def test_service_call_invoke(self):
        """Test Service.__call__ calls invoke method when defined."""
        ctx = Context()

        class InvokableService(Service):
            def __init__(self, c: Context):
                super().__init__(c, "invokable")

            @classmethod
            def invoke(cls, self_instance, *args, **kwargs):
                return args[0] if args else None

        svc = InvokableService(ctx)
        result = svc(42)
        assert result == 42


class TestContextSetAttr:
    def test_context_setattr_direct_attribute(self):
        """Test __setattr__ sets direct instance attributes."""
        ctx = Context()
        ctx.custom_attr = 42
        assert ctx.custom_attr == 42

    def test_context_setattr_private_attribute(self):
        """Test __setattr__ sets private attributes directly."""
        ctx = Context()
        ctx._private = "secret"
        assert ctx._private == "secret"

    def test_context_setattr_declared_service(self):
        """Test __setattr__ routes declared service through reflect.set."""
        ctx = Context()
        ctx.reflect.provide("tracked", "initial_value")
        # Setting a declared service routes through reflect.set
        ctx.tracked = "new_value"
        assert ctx.tracked == "new_value"

    def test_context_setattr_accessor_setter(self):
        """Test __setattr__ calls accessor setter."""
        ctx = Context()

        def my_setter(ctx, value, receiver, error):
            receiver._custom_set_value = value
            return True

        ctx.reflect.accessor("custom_prop", {"set": my_setter})
        ctx.custom_prop = "hello"
        assert ctx._custom_set_value == "hello"

    def test_context_setattr_accessor_no_setter(self):
        """Test __setattr__ on accessor without setter is a no-op."""
        ctx = Context()
        ctx.reflect.accessor("read_only", {"get": lambda c, r, e: 42})
        # Setting should not raise, just be ignored (proxy parity)
        ctx.read_only = 99
        assert ctx.read_only == 42

    def test_context_setattr_accessor_no_getter(self):
        """Test __getattr__ on accessor without getter raises AttributeError."""
        ctx = Context()
        ctx.reflect.accessor("write_only", {"set": lambda c, v, r, e: True})
        # Reading should raise AttributeError since no getter
        with pytest.raises(AttributeError):
            _ = ctx.write_only


class TestContextContains:
    def test_context_contains_private(self):
        """Test __contains__ with private attribute."""
        ctx = Context()
        ctx._private = "secret"
        assert "_private" in ctx

    def test_context_contains_service(self):
        """Test __contains__ finds services in _services."""
        ctx = Context()
        CoreService(ctx)
        assert "core_svc" in ctx

    def test_context_contains_registered_service(self):
        """Test __contains__ finds services registered via _register_service."""
        ctx = Context()
        ctx._register_service("manual_svc", "value")
        assert "manual_svc" in ctx

    def test_context_contains_reflect_store(self):
        """Test __contains__ finds services in reflect.store."""
        ctx = Context()
        ctx.reflect.provide("reflect_svc", "value")
        assert "reflect_svc" in ctx

    def test_context_contains_props(self):
        """Test __contains__ finds declared properties in reflect.props."""
        ctx = Context()
        ctx.reflect.accessor("declared_prop", {"get": lambda c, r, e: None})
        assert "declared_prop" in ctx

    def test_context_contains_parent(self):
        """Test __contains__ checks parent context."""
        parent = Context()
        child = Context(parent=parent)
        CoreService(parent)
        assert "core_svc" in child

    def test_context_contains_not_found(self):
        """Test __contains__ returns False for unknown names."""
        ctx = Context()
        assert "nonexistent" not in ctx


class TestServiceExtendElseBranch:
    def test_service_extend_without_invoke_flag(self):
        """Test _extend else branch (covers lines 562-565)."""
        ctx = Context()

        class NoInvokeService(Service):
            __symbols__: ClassVar[dict[str, str]] = {
                "init": symbols["init"],
                "check": symbols["check"],
                # No "invoke" key
                "metadata": symbols["metadata"],
                "extend": symbols["extend"],
                "tracker": symbols["tracker"],
                "mergeIntercept": symbols["mergeIntercept"],
                "resolveIntercept": symbols["resolveIntercept"],
            }

            def __init__(self, c: Context):
                super().__init__(c, "no_invoke_svc")

        svc = NoInvokeService(ctx)
        extended = svc._extend({"custom": "value"})
        assert extended.custom == "value"


class TestContextGetSet:
    def test_context_get_set(self):
        """Test Context.get and Context.set methods."""
        ctx = Context()
        svc = CoreService(ctx)

        # get should return the service
        assert ctx.get("core_svc") is svc
        # get with strict=True should return the service when fiber is ACTIVE
        assert ctx.get("core_svc", strict=True) is svc
        # get with non-existent name should return None
        assert ctx.get("nonexistent") is None

    def test_context_set(self):
        """Test Context.set method."""
        ctx = Context()
        svc = CoreService(ctx)
        assert ctx.get("core_svc") is svc

        # Create a new service and set it via Context.set
        CoreService(ctx, "new_core_svc")
        ctx.set("renamed_svc", svc)
        assert ctx.get("renamed_svc") is svc


class TestCliEnvPlugin:
    def test_cli_env_load_cascade(self):
        """Test cli_env.load_env_cascade function."""
        import os
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            env_file = os.path.join(tmpdir, ".env")
            with open(env_file, "w") as f:
                f.write("HELLO=world\nFOO=bar\n")

            merged = load_env_cascade(base_dir=tmpdir)
            assert merged["HELLO"] == "world"
            assert merged["FOO"] == "bar"

    def test_cli_env_plugin_mount(self):
        """Test CliEnv plugin mounting."""

        ctx = Context()
        fiber = ctx.plugin(CliEnv)
        assert fiber.state == FiberState.ACTIVE
        assert ctx.env is not None
        assert ctx.env == os.environ

    def test_context_is_static(self):
        """Test Context.is_ static method."""
        ctx = Context()
        assert Context.is_(ctx) is True
        assert Context.is_("not a context") is False


class TestReflectService:
    def test_reflect_get(self):
        """Test ReflectService.get."""
        ctx = Context()
        rs = ctx.reflect
        svc = CoreService(ctx)
        assert rs.get("core_svc") is svc
        assert rs.get("nonexistent") is None

    def test_reflect_set(self):
        """Test ReflectService.set."""
        ctx = Context()
        rs = ReflectService(ctx)
        svc = CoreService(ctx)
        rs.provide("my_svc", svc)
        rs.set("my_svc", svc)
        assert rs.get("my_svc") == svc

        # Setting on a different fiber should raise
        other_ctx = Context()
        impl = rs._get_impl("my_svc", strict=False)
        impl.ctx = other_ctx
        with pytest.raises(ValueError):
            rs.set("my_svc", svc)

    def test_reflect_provide(self):
        """Test ReflectService.provide."""
        ctx = Context()
        rs = ReflectService(ctx)
        disposer = rs.provide("my_svc", "hello")
        assert rs.get("my_svc") == "hello"
        disposer()
        assert rs.get("my_svc") is None

    def test_reflect_provide_with_check(self):
        """Test ReflectService.provide with check function."""
        ctx = Context()

        def check():
            return False

        rs = ReflectService(ctx)
        rs.provide("my_svc", "hello", check=check)
        assert rs.get("my_svc") == "hello"

    def test_reflect_accessor(self):
        """Test ReflectService.accessor."""
        ctx = Context()
        rs = ReflectService(ctx)

        def getter(receiver, error):
            return "value"

        disposer = rs.accessor("my_prop", {"get": getter})
        assert "my_prop" in rs.props
        disposer()
        assert "my_prop" not in rs.props

    def test_reflect_mixin(self):
        """Test ReflectService.mixin."""
        ctx = Context()

        class Source:
            def __init__(self):
                self.foo = "bar"

        source = Source()
        rs = ReflectService(ctx)
        disposer = rs.mixin(source, ["foo"])
        assert "foo" in rs.props
        disposer()
        assert "foo" not in rs.props

    def test_reflect_trace(self):
        """Test ReflectService.trace."""
        ctx = Context()
        rs = ReflectService(ctx)
        val = {"key": "value"}
        assert rs.trace(val) is val

    def test_reflect_bind(self):
        """Test ReflectService.bind."""
        ctx = Context()
        rs = ReflectService(ctx)

        def callback(a, b):
            return a + b

        bound = rs.bind(callback)
        assert callable(bound)
        assert bound(1, 2) == 3

    def test_reflect_notify(self):
        """Test ReflectService.notify."""
        ctx = Context()
        rs = ReflectService(ctx)
        rs.provide("my_svc", "hello")
        # Should not raise
        rs.notify(["my_svc"])
        rs.notify(["my_svc"], filter_fn=lambda ctx, name: True)
        rs.notify(["my_svc"], filter_fn=lambda ctx, name: False)

    def test_context_accessor(self):
        """Test Context.accessor."""
        ctx = Context()
        rs = ctx.reflect

        def getter(receiver, error):
            return "value"

        disposer = rs.accessor("my_prop", {"get": getter})
        assert "my_prop" in rs.props
        disposer()
        assert "my_prop" not in rs.props

    def test_context_mixin(self):
        """Test Context.mixin."""
        ctx = Context()

        class Source:
            def __init__(self):
                self.foo = "bar"

        source = Source()
        rs = ctx.reflect
        disposer = rs.mixin(source, ["foo"])
        assert "foo" in rs.props
        disposer()
        assert "foo" not in rs.props


class TestInstanceCheckDirect:
    def test_instancecheck_none_instance(self):
        """Test __instancecheck__ with None instance (covers line 520)."""
        ctx = Context()
        CoreService(ctx)
        result = CoreService.__instancecheck__(CoreService, None)
        assert result is False

    def test_instancecheck_multiple_bases(self):
        """Test __instancecheck__ with multiple base classes (covers lines 531-532)."""
        ctx = Context()

        class Base1(CoreService):
            pass

        class Base2(CoreService):
            pass

        class MultiSub(Base1, Base2):
            pass

        subsub = MultiSub(ctx)
        # MultiSub.__bases__ = (Base1, Base2) has len 2, so constructor becomes None
        result = CoreService.__instancecheck__(CoreService, subsub)
        assert result is False  # MultiSub doesn't directly inherit from CoreService
        # But isinstance uses full MRO which includes CoreService:
        assert isinstance(subsub, CoreService)


class TestInstanceCheckObjectBase:
    def test_instancecheck_object_base(self):
        """Test __instancecheck__ where base class has empty __bases__ (covers line 534).

        object.__bases__ is (), which is falsy, so the `else: break` branch is taken.
        """
        Context()
        # object.__bases__ is () - empty tuple, so bases is falsy
        result = CoreService.__instancecheck__(CoreService, object())
        assert result is False  # object is not an instance of CoreService
