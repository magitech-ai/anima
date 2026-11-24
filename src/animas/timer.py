"""
Reversible timer service for Animas.
Provides timeouts, intervals, debouncing, throttling, and cancellable sleeps
tied to the fiber effect lifecycle.
"""

from __future__ import annotations

import asyncio
import inspect
import time
from collections.abc import Callable
from typing import Any

from animas.core import Context, Fiber, Service
from animas.events import Disposer


class Scheduled[F: Callable[..., Any]]:
    """Callable wrapper with an explicit disposal handle."""

    def __init__(self, fn: Callable[..., Any], disposer: Disposer) -> None:
        self._fn = fn
        self._disposer = disposer

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        return self._fn(*args, **kwargs)

    def dispose(self) -> None:
        self._disposer()


class _AsyncInterval:
    """Async iterator over timer ticks, bound to a context's lifecycle."""

    def __init__(self, fiber: Fiber, delay: float) -> None:
        self._delay = delay
        self._pending: list[asyncio.Future[None]] = []
        self._done = False
        self._reason: BaseException | None = None
        self._task = asyncio.get_running_loop().create_task(self._ticks())
        # Bind to the service's own fiber so disposing the service stops the
        # ticks even when interval() is called from another context.
        self._disposer = fiber.effect(
            lambda: lambda: self._stop(RuntimeError("Context has been disposed"))
        )

    async def _ticks(self) -> None:
        while True:
            await asyncio.sleep(self._delay)
            self._emit_tick()

    def _emit_tick(self) -> None:
        if self._pending:
            future = self._pending.pop(0)
            if not future.done():
                future.set_result(None)

    def _stop(self, reason: BaseException | None = None) -> None:
        if self._done:
            return
        self._done = True
        self._reason = reason
        self._task.cancel()
        for future in self._pending:
            if future.done():
                continue
            if reason is None:
                future.set_exception(StopAsyncIteration())
            else:
                future.set_exception(reason)
        self._pending.clear()

    def __aiter__(self) -> _AsyncInterval:
        return self

    async def __anext__(self) -> None:
        if self._reason is not None:
            raise self._reason
        if self._done:
            raise StopAsyncIteration
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._pending.append(future)
        await future

    async def aclose(self) -> None:
        if self._done:
            return
        self._stop()

    async def athrow(self, error: BaseException) -> None:
        if self._done:
            raise StopAsyncIteration
        self._stop(error)
        raise StopAsyncIteration


class TimerService(Service):
    """Contextual timer service providing lifecycle-bound scheduling primitives."""

    def __init__(self, ctx: Context) -> None:
        super().__init__(ctx, "timer")
        self._own_fiber = ctx.fiber
        self._active_tasks: set[asyncio.Task[Any]] = set()
        # Upstream packages/timer/src/index.ts:14 mixes these methods onto
        # the context so components can call ctx.interval() directly.
        ctx.mixin(
            "timer",
            [
                "timeout",
                "interval",
                "throttle",
                "debounce",
                "setTimeout",
                "setInterval",
            ],
        )

    def setTimeout(self, callback: Callable[..., Any], delay: float) -> Any:
        """Deprecated alias for timeout() (upstream timer.ts:17-20)."""
        result: Any = self.timeout(callback, delay)
        return result

    def setInterval(self, callback: Callable[..., Any], delay: float) -> Any:
        """Deprecated alias for interval() (upstream timer.ts:22-25)."""
        return self.interval(callback, delay)

    def timeout(
        self,
        callback_or_delay: Callable[..., Any] | float,
        delay: float | None = None,
    ) -> Any:
        """Schedule a one-shot execution after delay seconds.

        If called with (delay), returns an awaitable sleep bound to the fiber lifecycle.
        If called with (callback, delay), returns an unregister disposer.
        """
        if callable(callback_or_delay):
            callback = callback_or_delay
            d = float(delay if delay is not None else 0.0)

            async def _runner() -> None:
                await asyncio.sleep(d)
                res = callback()
                if inspect.isawaitable(res):
                    await res

            loop = asyncio.get_running_loop()
            task = loop.create_task(_runner())
            self._active_tasks.add(task)
            task.add_done_callback(self._active_tasks.discard)

            def _cancel() -> None:
                task.cancel()
                self._active_tasks.discard(task)

            disposer = self.ctx.effect(lambda: _cancel)

            def _manual_dispose() -> None:
                _cancel()
                disposer()

            return _manual_dispose
        else:
            sleep_delay = float(callback_or_delay)
            return self.sleep(sleep_delay)

    async def sleep(self, delay: float) -> None:
        """Cancellable sleep that aborts if the calling fiber unloads."""
        loop = asyncio.get_running_loop()
        future: asyncio.Future[None] = loop.create_future()

        def _cancel_sleep() -> None:
            if not future.done():
                future.cancel()

        disposer = self.ctx.effect(lambda: _cancel_sleep)

        def _resolve() -> None:
            future.set_result(None)

        timer_handle = loop.call_later(float(delay), _resolve)

        try:
            await future
        finally:
            timer_handle.cancel()
            disposer()

    def interval(
        self,
        callback_or_delay: Callable[..., Any] | float,
        delay: float | None = None,
    ) -> Any:
        """Invoke a callback every delay seconds, or yield ticks when called with only a delay.

        Upstream timer.ts:54-104 overloads `interval`: with a callback it
        returns a disposer; with only a delay it returns an async iterator
        whose ticks stop when the owning context is disposed.
        """
        if not callable(callback_or_delay):
            return _AsyncInterval(self._own_fiber, float(callback_or_delay))
        callback = callback_or_delay
        d = float(delay if delay is not None else 0.0)

        async def _loop() -> None:
            while True:
                await asyncio.sleep(d)
                res = callback()
                if inspect.isawaitable(res):
                    await res

        loop = asyncio.get_running_loop()
        task = loop.create_task(_loop())
        self._active_tasks.add(task)
        task.add_done_callback(self._active_tasks.discard)

        def _cancel() -> None:
            task.cancel()
            self._active_tasks.discard(task)

        disposer = self.ctx.effect(lambda: _cancel)

        def _manual_dispose() -> None:
            _cancel()
            disposer()

        return _manual_dispose

    def debounce(
        self,
        callback: Callable[..., Any],
        delay: float,
    ) -> Scheduled[Any]:
        """Wrap callback to coalesce invocations within delay seconds."""
        d = float(delay)
        task: asyncio.Task[Any] | None = None
        is_disposed = False

        def _cancel() -> None:
            nonlocal is_disposed, task
            is_disposed = True
            if task is not None:
                task.cancel()
                self._active_tasks.discard(task)

        effect_disposer = self.ctx.effect(lambda: _cancel)

        def _invoke(*args: Any, **kwargs: Any) -> None:
            nonlocal task
            if is_disposed:
                return
            if task is not None:
                task.cancel()
                self._active_tasks.discard(task)

            async def _wait_and_run() -> None:
                await asyncio.sleep(d)
                res = callback(*args, **kwargs)
                if inspect.isawaitable(res):
                    await res

            loop = asyncio.get_running_loop()
            task = loop.create_task(_wait_and_run())
            self._active_tasks.add(task)
            task.add_done_callback(self._active_tasks.discard)

        def _manual_dispose() -> None:
            _cancel()
            effect_disposer()

        return Scheduled(_invoke, _manual_dispose)

    def throttle(
        self,
        callback: Callable[..., Any],
        delay: float,
        *,
        no_trailing: bool = False,
    ) -> Scheduled[Any]:
        """Wrap callback to limit invocations to at most once per delay seconds."""
        d = float(delay)
        last_call: float = -1e9
        trailing_task: asyncio.Task[Any] | None = None
        is_disposed = False

        def _cancel() -> None:
            nonlocal is_disposed, trailing_task
            is_disposed = True
            if trailing_task is not None:
                trailing_task.cancel()
                self._active_tasks.discard(trailing_task)

        effect_disposer = self.ctx.effect(lambda: _cancel)

        def _invoke(*args: Any, **kwargs: Any) -> Any:
            nonlocal last_call, trailing_task
            if is_disposed:
                return None

            # Monotonic clock: a wall-clock jump (NTP step, suspend/resume)
            # must not stretch or collapse the throttle window.
            now = time.monotonic()
            remaining = d - (now - last_call)

            if remaining <= 0:
                last_call = now
                res = callback(*args, **kwargs)
                if inspect.isawaitable(res):
                    loop = asyncio.get_running_loop()

                    async def _await_result(aw: Any) -> None:
                        await aw

                    run_task: asyncio.Task[None] = loop.create_task(_await_result(res))
                    self._active_tasks.add(run_task)
                    run_task.add_done_callback(self._active_tasks.discard)
                return res

            if not no_trailing:
                if trailing_task is not None:
                    trailing_task.cancel()
                    self._active_tasks.discard(trailing_task)

                async def _delayed_trailing() -> None:
                    nonlocal last_call
                    await asyncio.sleep(remaining)
                    last_call = time.monotonic()
                    res = callback(*args, **kwargs)
                    if inspect.isawaitable(res):
                        await res

                loop = asyncio.get_running_loop()
                trailing_task = loop.create_task(_delayed_trailing())
                self._active_tasks.add(trailing_task)
                trailing_task.add_done_callback(self._active_tasks.discard)
            return None

        def _manual_dispose() -> None:
            _cancel()
            effect_disposer()

        return Scheduled(_invoke, _manual_dispose)
