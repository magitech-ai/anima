import asyncio
import pytest

from kernel import Context, EventMode, FiberState, Service


class MockGreeter(Service):
    def __init__(self, ctx: Context) -> None:
        super().__init__(ctx, "greeter")

    def greet(self, name: str) -> str:
        return f"Hello, {name}!"


@pytest.mark.asyncio
async def test_service_registration():
    ctx = Context()
    fiber = ctx.plugin(MockGreeter)
    await fiber.load()

    assert ctx.has("greeter")
    assert ctx.greeter.greet("World") == "Hello, World!"


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
async def test_waterfall_pipeline():
    ctx = Context()

    # Middleware 1: Appends prefix
    async def middleware_1(val: str, next_fn):
        res = await next_fn(f"[m1 {val}]")
        return f"m1_post({res})"

    # Middleware 2: Appends suffix
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
