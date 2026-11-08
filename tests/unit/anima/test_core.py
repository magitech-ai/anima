import asyncio

import pytest
from pydantic import BaseModel, ValidationError

from anima.core import Context, Fiber, FiberState, Service
from anima.events import EventMode


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
    assert isinstance(f_err.error, ValidationError)


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
