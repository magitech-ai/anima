import asyncio
from typing import Any

import pytest

from anima.events import EventBus


@pytest.mark.asyncio
async def test_event_bus_prepend_and_dispose():
    bus = EventBus()
    calls = []

    d1 = bus.on("test", lambda x: calls.append(f"first {x}"))
    d2 = bus.on("test", lambda x: calls.append(f"second {x}"), prepend=True)

    bus.emit("test", 1)
    assert calls == ["second 1", "first 1"]

    # Dispose d2
    d2()
    bus.emit("test", 2)
    assert calls == ["second 1", "first 1", "first 2"]

    # Disposing twice is safe
    d2()

    # Dispose d1
    d1()
    bus.emit("test", 3)
    assert calls == ["second 1", "first 1", "first 2"]


@pytest.mark.asyncio
async def test_emit_async_listeners_and_error_handling():
    bus = EventBus()
    async_calls = []

    async def async_listener(val):
        await asyncio.sleep(0.01)
        async_calls.append(val)

    def failing_listener(val):
        raise RuntimeError("boom")

    bus.on("async_event", failing_listener)
    bus.on("async_event", async_listener)

    # Emitter should catch exception from failing_listener and schedule async_listener
    bus.emit("async_event", "hello")
    await asyncio.sleep(0.05)
    assert async_calls == ["hello"]


def test_emit_sync_fallback():
    # Calling emit outside an active loop executes coroutine via asyncio.run
    bus = EventBus()
    ran = []

    async def coro_listener():
        ran.append(True)

    bus.on("sync_run", coro_listener)
    bus.emit("sync_run")
    assert ran == [True]


@pytest.mark.asyncio
async def test_parallel_dispatch_empty_and_mixed():
    bus = EventBus()

    # Empty listeners
    assert await bus.parallel("empty") == []

    async def a1():
        await asyncio.sleep(0.01)
        return "async_val"

    def s1():
        return "sync_val"

    bus.on("mixed", a1)
    bus.on("mixed", s1)

    results = await bus.parallel("mixed")
    assert results == ["async_val", "sync_val"]


@pytest.mark.asyncio
async def test_serial_dispatch():
    bus = EventBus()

    # Empty serial returns None
    assert await bus.serial("none") is None

    async def ret_none():
        return None

    def ret_val():
        return 42

    def ret_later():
        return 99

    bus.on("serial_test", ret_none)
    bus.on("serial_test", ret_val)
    bus.on("serial_test", ret_later)

    assert await bus.serial("serial_test") == 42


def test_bail_dispatch_sync_and_async():
    bus = EventBus()
    assert bus.bail("empty") is None

    def b1():
        return None

    async def b2():
        return "from_async_bail"

    def b3():
        return "unreachable"

    bus.on("bail_event", b1)
    bus.on("bail_event", b2)
    bus.on("bail_event", b3)

    assert bus.bail("bail_event") == "from_async_bail"


@pytest.mark.asyncio
async def test_waterfall_default_handlers_and_empty():
    bus = EventBus()

    # No listeners, no default handler -> returns first arg
    assert await bus.waterfall("empty", "raw") == "raw"
    assert await bus.waterfall("empty") is None

    # Async default handler
    async def async_default(x):
        return f"async_{x}"

    assert (
        await bus.waterfall("empty", "val", default_handler=async_default)
        == "async_val"
    )


@pytest.mark.asyncio
async def test_waterfall_signature_variations():
    bus = EventBus()

    # Function with varargs: (*args, next_fn)
    async def varargs_middleware(*args):
        val = args[0]
        next_fn = args[1]
        res = await next_fn(f"varargs_{val}")
        return f"wrapped_{res}"

    # Function with named keyword next parameter: (val, next)
    async def kw_middleware(val, next):
        return await next(f"kw_{val}")

    # Function with named keyword next_fn parameter: (val, next_fn)
    async def kw_fn_middleware(val, next_fn):
        return await next_fn(f"kwfn_{val}")

    bus.on("sig_test", varargs_middleware)
    bus.on("sig_test", kw_middleware)
    bus.on("sig_test", kw_fn_middleware)

    result = await bus.waterfall(
        "sig_test", "start", default_handler=lambda s: f"end({s})"
    )
    assert result == "wrapped_end(kwfn_kw_varargs_start)"


@pytest.mark.asyncio
async def test_waterfall_kwonly_next_and_sync_return():
    bus = EventBus()

    # Keyword-only next parameter
    async def kwonly_middleware(val, *, next):
        res = await next(f"kwonly_{val}")
        return f"post_{res}"

    # Middleware without next parameter (sink)
    def sink_middleware(val):
        return f"terminal_{val}"

    bus.on("flow", kwonly_middleware)
    bus.on("flow", sink_middleware)

    res = await bus.waterfall("flow", "input")
    assert res == "post_terminal_kwonly_input"


@pytest.mark.asyncio
async def test_waterfall_pure_sync_short_circuit():
    bus = EventBus()

    def sync_interceptor(val, next):
        if val == "bypass":
            return "immediate_sync_bypass"
        return next(val)

    bus.on("intercept", sync_interceptor)
    res = await bus.waterfall(
        "intercept", "bypass", default_handler=lambda s: "default"
    )
    assert res == "immediate_sync_bypass"


@pytest.mark.asyncio
async def test_once_listener_sync_and_async():
    bus = EventBus()
    sync_calls = []
    async_calls = []

    def sync_listener(val):
        sync_calls.append(val)
        return val

    async def async_listener(val):
        await asyncio.sleep(0.001)
        async_calls.append(val)
        return val

    bus.once("ping", sync_listener)
    bus.once("ping_async", async_listener)

    # First emission triggers both
    bus.emit("ping", 1)
    await bus.parallel("ping_async", 10)
    assert sync_calls == [1]
    assert async_calls == [10]

    # Second emission does not trigger
    bus.emit("ping", 2)
    await bus.parallel("ping_async", 20)
    assert sync_calls == [1]
    assert async_calls == [10]

    # Manual disposal before execution
    never_called = []
    d = bus.once("cancel", lambda: never_called.append(True))
    d()
    d()  # Second call exercises if not disposed False branch
    bus.emit("cancel")
    assert never_called == []


@pytest.mark.asyncio
async def test_event_bus_filtering_and_global():
    bus = EventBus()
    calls = []

    class DummyContext:
        def __init__(self, name: str, allowed: set[str]):
            self.name = name
            self.allowed = allowed

        def match(self, target: Any) -> bool:
            return getattr(target, "name", None) in self.allowed

    ctx1 = DummyContext("ctx1", {"ctx2"})
    ctx2 = DummyContext("ctx2", {"ctx1"})
    ctx3 = DummyContext("ctx3", set())

    # Listener with global_=True
    bus.on("msg", lambda m: calls.append(f"global:{m}"), global_=True, ctx=ctx3)
    # Listener with ctx=None
    bus.on("msg", lambda m: calls.append(f"no_ctx:{m}"))
    # Listener matching ctx2
    bus.on("msg", lambda m: calls.append(f"ctx2:{m}"), ctx=ctx2)
    # Listener matching ctx3 (which ctx1 does not allow)
    bus.on("msg", lambda m: calls.append(f"ctx3:{m}"), ctx=ctx3)

    bus.emit("msg", "hello", this_arg=ctx1)
    assert calls == ["global:hello", "no_ctx:hello", "ctx2:hello"]
