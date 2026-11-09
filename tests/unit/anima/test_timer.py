import asyncio

import pytest

from anima.core import Context
from anima.timer import TimerService


@pytest.mark.asyncio
async def test_timeout_callback_and_disposal():
    ctx = Context()
    timer = TimerService(ctx)
    ctx.provide("timer", timer)

    sync_runs = []
    async_runs = []

    # Sync callback timeout
    timer.timeout(lambda: sync_runs.append("sync_done"), 0.01)

    # Async callback timeout
    async def async_cb():
        await asyncio.sleep(0.001)
        async_runs.append("async_done")

    timer.timeout(async_cb, 0.01)

    await asyncio.sleep(0.03)
    assert sync_runs == ["sync_done"]
    assert async_runs == ["async_done"]

    # Manual cancel before delay
    cancelled_runs = []
    d = timer.timeout(lambda: cancelled_runs.append(True), 0.05)
    d()
    await asyncio.sleep(0.06)
    assert cancelled_runs == []


@pytest.mark.asyncio
async def test_timeout_as_sleep_and_cancellation():
    ctx = Context()
    timer = TimerService(ctx)
    ctx.provide("timer", timer)

    # Normal sleep
    t0 = asyncio.get_running_loop().time()
    await timer.timeout(0.02)
    t1 = asyncio.get_running_loop().time()
    assert (t1 - t0) >= 0.015

    # Sleep cancelled via fiber effect unload
    sleep_task_done = False

    def sleeper_plugin(c: Context):
        async def _run():
            nonlocal sleep_task_done
            try:
                await c.timer.sleep(1.0)
                sleep_task_done = True
            except asyncio.CancelledError:
                pass

        asyncio.create_task(_run())

    fib = ctx.plugin(sleeper_plugin)
    await ctx.flush()
    await asyncio.sleep(0.01)

    # Unload plugin -> should cancel active sleep
    await fib.dispose()
    await asyncio.sleep(0.02)
    assert not sleep_task_done


@pytest.mark.asyncio
async def test_interval_execution_and_disposal():
    ctx = Context()
    timer = TimerService(ctx)
    ctx.provide("timer", timer)

    sync_ticks = []
    async_ticks = []

    d1 = timer.interval(lambda: sync_ticks.append(1), 0.01)

    async def async_ticker():
        await asyncio.sleep(0.001)
        async_ticks.append(1)

    d2 = timer.interval(async_ticker, 0.01)

    await asyncio.sleep(0.035)
    d1()
    d2()

    count1 = len(sync_ticks)
    count2 = len(async_ticks)
    assert count1 >= 2
    assert count2 >= 2

    # Should not increase after disposal
    await asyncio.sleep(0.02)
    assert len(sync_ticks) == count1
    assert len(async_ticks) == count2


@pytest.mark.asyncio
async def test_debounce_coalescing_and_cancellation():
    ctx = Context()
    timer = TimerService(ctx)
    ctx.provide("timer", timer)

    invocations = []

    async def debounced_target(x):
        await asyncio.sleep(0.001)
        invocations.append(x)

    scheduled = timer.debounce(debounced_target, 0.02)

    # Rapid calls within delay window
    scheduled("first")
    scheduled("second")
    scheduled("final")

    await asyncio.sleep(0.04)
    assert invocations == ["final"]

    # Call after dispose does nothing
    scheduled.dispose()
    assert scheduled("after_dispose") is None
    assert invocations == ["final"]

    # Cancel pending debounce task via dispose
    pending_calls = []
    deb = timer.debounce(lambda: pending_calls.append(1), 0.05)
    deb()
    deb.dispose()
    await asyncio.sleep(0.06)
    assert pending_calls == []

    # Sync callback in debounce
    sync_deb_calls = []
    deb_sync = timer.debounce(lambda x: sync_deb_calls.append(x), 0.01)
    deb_sync("sync_val")
    await asyncio.sleep(0.02)
    assert sync_deb_calls == ["sync_val"]


@pytest.mark.asyncio
async def test_throttle_rate_limiting_and_trailing():
    ctx = Context()
    timer = TimerService(ctx)
    ctx.provide("timer", timer)

    calls_trailing = []
    calls_no_trailing = []

    def on_trailing(x):
        calls_trailing.append(x)

    async def on_no_trailing(x):
        await asyncio.sleep(0.001)
        calls_no_trailing.append(x)

    throttled1 = timer.throttle(on_trailing, 0.03, no_trailing=False)
    throttled2 = timer.throttle(on_no_trailing, 0.03, no_trailing=True)

    # Call 1: runs immediately
    throttled1("a1")
    throttled2("b1")
    await asyncio.sleep(0.005)
    assert calls_trailing == ["a1"]
    assert calls_no_trailing == ["b1"]

    # Call 2 within window
    throttled1("a2")
    throttled2("b2")

    # Await trailing execution
    await asyncio.sleep(0.05)
    assert calls_trailing == ["a1", "a2"]
    # No-trailing dropped b2
    assert calls_no_trailing == ["b1"]

    # Manual disposal
    throttled1.dispose()
    assert throttled1("a3") is None

    throttled2.dispose()
    assert throttled2("b3") is None

    # Cancel trailing task on dispose
    trailing_cancelled = []
    t_cancel = timer.throttle(lambda x: trailing_cancelled.append(x), 0.05)
    t_cancel(1)
    t_cancel(2)  # trailing scheduled
    t_cancel.dispose()  # cancels trailing
    await asyncio.sleep(0.06)
    assert trailing_cancelled == [1]

    # Replacing trailing task with newer trailing call
    newer_trailing = []
    t_replace = timer.throttle(lambda x: newer_trailing.append(x), 0.04)
    t_replace("first")
    t_replace("second")
    t_replace("third")
    await asyncio.sleep(0.06)
    assert newer_trailing == ["first", "third"]

    # Trailing task calling async callback
    async_trailing_calls = []

    async def async_trailing_cb(x):
        await asyncio.sleep(0.001)
        async_trailing_calls.append(x)

    t_async_trail = timer.throttle(async_trailing_cb, 0.02)
    t_async_trail("initial")
    t_async_trail("queued")
    await asyncio.sleep(0.04)
    assert async_trailing_calls == ["initial", "queued"]

    # Disposal before any invocation
    d_uncalled = timer.debounce(lambda: None, 0.01)
    d_uncalled.dispose()

    t_uncalled = timer.throttle(lambda: None, 0.01)
    t_uncalled.dispose()
