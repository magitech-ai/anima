"""
Reversible timer service for Anima.
Provides timeouts, intervals, debouncing, throttling, and cancellable sleeps
tied to the fiber effect lifecycle.
"""

from __future__ import annotations

import asyncio
import inspect
import time
from collections.abc import Callable
from typing import Any

from anima.core import Context, Service
from anima.events import Disposer


class Scheduled[F: Callable[..., Any]]:
    """Callable wrapper with an explicit disposal handle."""

    def __init__(self, fn: Callable[..., Any], disposer: Disposer) -> None:
        self._fn = fn
        self._disposer = disposer

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        return self._fn(*args, **kwargs)

    def dispose(self) -> None:
        self._disposer()


class TimerService(Service):
    """Contextual timer service providing lifecycle-bound scheduling primitives."""

    def __init__(self, ctx: Context) -> None:
        super().__init__(ctx, "timer")
        self._active_tasks: set[asyncio.Task[Any]] = set()

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
        callback: Callable[..., Any],
        delay: float,
    ) -> Disposer:
        """Repeatedly invoke a callback every delay seconds until disposed."""
        d = float(delay)

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

            now = time.time()
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
                    last_call = time.time()
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
