import asyncio
import pytest
from pydantic import BaseModel, ValidationError

from anima import Context, EventMode, FiberState, Service


class MockGreeter(Service):
    def __init__(self, ctx: Context) -> None:
        super().__init__(ctx, "greeter")

    def greet(self, name: str) -> str:
        return f"Hello, {name}!"


class MockLogger(Service):
    def __init__(self, ctx: Context) -> None:
        super().__init__(ctx, "logger")
        self.logs: list[str] = []

    def log(self, msg: str) -> None:
        self.logs.append(msg)


@pytest.mark.asyncio
async def test_service_registration():
    ctx = Context()
    fiber = ctx.plugin(MockGreeter)
    await fiber.load()

    assert ctx.has("greeter")
    assert ctx.greeter.greet("World") == "Hello, World!"
    assert ctx.root == ctx


@pytest.mark.asyncio
async def test_inject_dependencies_lifecycle():
    ctx = Context()
    loaded_order = []

    def consumer_plugin(context: Context):
        loaded_order.append(f"consumer got {context.greeter.greet('Agent')}")

    consumer_plugin.inject = ["greeter"]

    # Mount consumer BEFORE greeter is registered
    consumer_fiber = ctx.plugin(consumer_plugin)
    assert consumer_fiber.state == FiberState.PENDING
    assert loaded_order == []

    # Mount greeter
    greeter_fiber = ctx.plugin(MockGreeter)
    await ctx.flush()

    # Consumer should now be active
    assert consumer_fiber.state == FiberState.ACTIVE
    assert loaded_order == ["consumer got Hello, Agent!"]


@pytest.mark.asyncio
async def test_reversible_effects_and_disposal():
    ctx = Context()
    cleaned_up = []

    def resource_plugin(c: Context):
        def cleanup():
            cleaned_up.append("resource_cleaned")

        c.effect(lambda: cleanup)

    fiber = ctx.plugin(resource_plugin)
    await fiber.load()
    assert fiber.state == FiberState.ACTIVE
    assert cleaned_up == []

    await fiber.dispose()
    assert fiber.state == FiberState.DISPOSED
    assert cleaned_up == ["resource_cleaned"]


@pytest.mark.asyncio
async def test_service_unregistration_disposes_dependents():
    ctx = Context()
    consumer_disposed = []

    def consumer_plugin(c: Context):
        def on_dispose():
            consumer_disposed.append("consumer_unloaded")

        c.effect(lambda: on_dispose)

    consumer_plugin.inject = ["greeter"]

    greeter_fiber = ctx.plugin(MockGreeter)
    consumer_fiber = ctx.plugin(consumer_plugin)
    await ctx.flush()

    assert greeter_fiber.state == FiberState.ACTIVE
    assert consumer_fiber.state == FiberState.ACTIVE
    assert consumer_disposed == []

    # Unload greeter -> consumer must be cleanly disposed
    await greeter_fiber.dispose()
    await ctx.flush()

    assert not ctx.has("greeter")
    assert consumer_fiber.state == FiberState.DISPOSED
    assert consumer_disposed == ["consumer_unloaded"]


@pytest.mark.asyncio
async def test_waterfall_pipeline():
    ctx = Context()

    async def middleware_1(val: str, next_fn):
        res = await next_fn(f"[m1 {val}]")
        return f"m1_post({res})"

    async def middleware_2(val: str, next_fn):
        res = await next_fn(f"{val} m2")
        return f"m2_post({res})"

    ctx.on("transform", middleware_1, mode=EventMode.WATERFALL)
    ctx.on("transform", middleware_2, mode=EventMode.WATERFALL)

    result = await ctx.waterfall(
        "transform",
        "core",
        default_handler=lambda s: f"final({s})",
    )

    assert result == "m1_post(m2_post(final([m1 core] m2)))"


@pytest.mark.asyncio
async def test_waterfall_short_circuit():
    ctx = Context()

    async def guard(val: str, next_fn):
        if val == "blocked":
            return "VETOED"
        return await next_fn(val)

    async def normal(val: str, next_fn):
        return await next_fn(f"processed: {val}")

    ctx.on("check", guard, mode=EventMode.WATERFALL)
    ctx.on("check", normal, mode=EventMode.WATERFALL)

    blocked_res = await ctx.waterfall("check", "blocked", default_handler=lambda s: "ok")
    assert blocked_res == "VETOED"

    allowed_res = await ctx.waterfall("check", "allowed", default_handler=lambda s: f"ok: {s}")
    assert allowed_res == "ok: processed: allowed"


@pytest.mark.asyncio
async def test_parallel_and_serial_events():
    ctx = Context()
    events_log = []

    async def h1(name: str):
        await asyncio.sleep(0.01)
        events_log.append(f"h1 {name}")
        return None

    async def h2(name: str):
        events_log.append(f"h2 {name}")
        return "winner"

    async def h3(name: str):
        events_log.append(f"h3 {name}")
        return "too late"

    ctx.on("task", h1)
    ctx.on("task", h2)
    ctx.on("task", h3)

    # Serial stops at first non-None
    res = await ctx.serial("task", "run")
    assert res == "winner"
    assert "h1 run" in events_log
    assert "h2 run" in events_log
    assert "h3 run" not in events_log


@pytest.mark.asyncio
async def test_service_isolation_realms():
    """Verify ctx.isolate(name) creates child scopes that don't leak or mutate parent."""
    root_ctx = Context()
    root_ctx.plugin(MockGreeter)
    root_ctx.plugin(MockLogger)
    await root_ctx.flush()

    assert root_ctx.has("greeter")
    assert root_ctx.has("logger")

    # Isolate 'greeter' in child scope (e.g. for a subagent)
    child_ctx = root_ctx.isolate("greeter")

    # Child still has 'logger' (inherited)
    assert child_ctx.has("logger")
    # But 'greeter' is isolated and not yet defined in child
    assert not child_ctx.has("greeter")
    assert child_ctx.get("greeter") is None

    # Register custom greeter in child context
    class CustomGreeter(Service):
        def __init__(self, ctx: Context) -> None:
            super().__init__(ctx, "greeter")

        def greet(self, name: str) -> str:
            return f"Custom {name}!"

    child_ctx.plugin(CustomGreeter)
    await child_ctx.flush()

    # Child resolves to custom greeter
    assert child_ctx.has("greeter")
    assert child_ctx.greeter.greet("Alice") == "Custom Alice!"

    # Parent is untouched!
    assert root_ctx.greeter.greet("Alice") == "Hello, Alice!"


@pytest.mark.asyncio
async def test_config_schema_validation():
    """Verify plugin Config validation rejects invalid inputs and parses valid models."""
    ctx = Context()

    class BotConfig(BaseModel):
        token: str
        port: int = 8080

    class BotPlugin:
        Config = BotConfig

        def __init__(self, c: Context, **cfg):
            self.cfg = cfg

    # Valid config
    valid_fiber = ctx.plugin(BotPlugin, token="secret-123", port=9000)
    await ctx.flush()
    assert valid_fiber.state == FiberState.ACTIVE
    assert isinstance(valid_fiber.config, BotConfig)
    assert valid_fiber.config.port == 9000

    # Invalid config (missing required token)
    invalid_fiber = ctx.plugin(BotPlugin, port=9000)
    await ctx.flush()
    assert invalid_fiber.state == FiberState.FAILED
    assert invalid_fiber.error is not None
    assert isinstance(invalid_fiber.error, ValidationError)


@pytest.mark.asyncio
async def test_internal_lifecycle_events():
    """Verify Anima internal lifecycle events (internal/status, internal/service, internal/plugin)."""
    ctx = Context()
    status_events = []
    service_events = []
    plugin_events = []

    ctx.on("internal/status", lambda fib, old_s, new_s: status_events.append((fib.name, old_s, new_s)))
    ctx.on("internal/service", lambda name, svc: service_events.append(name))
    ctx.on("internal/plugin", lambda fib: plugin_events.append(fib.name))

    ctx.plugin(MockGreeter)
    await ctx.flush()

    assert "MockGreeter" in plugin_events
    assert "greeter" in service_events
    assert any(s[0] == "MockGreeter" and s[2] == FiberState.ACTIVE for s in status_events)


@pytest.mark.asyncio
async def test_dump_tree_and_fiber_status():
    ctx = Context()
    ctx.plugin(MockGreeter)
    await ctx.flush()

    tree = ctx.dump_tree()
    assert "greeter" in tree["services"]
    assert len(tree["fibers"]) == 1
    assert tree["fibers"][0]["name"] == "MockGreeter"
    assert tree["fibers"][0]["state"] == "ACTIVE"
