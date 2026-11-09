"""
Event Bus implementation for Python.

Supports the 5 event dispatch modes:
- emit: Synchronous broadcast (fire-and-forget, non-blocking)
- parallel: Asynchronous concurrent broadcast (asyncio.gather)
- serial: Asynchronous sequential execution with short-circuit on non-None
- bail: Synchronous sequential execution with short-circuit on non-None
- waterfall: Around-middleware pipeline passing `(*args, next)`
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from enum import Enum
from typing import Any

Disposer = Callable[[], None | Awaitable[None]]


class EventMode(str, Enum):
    EMIT = "emit"
    PARALLEL = "parallel"
    SERIAL = "serial"
    BAIL = "bail"
    WATERFALL = "waterfall"


class Listener:
    def __init__(
        self,
        event: str,
        callback: Callable[..., Any],
        mode: EventMode = EventMode.EMIT,
        prepend: bool = False,
        ctx: Any | None = None,
        global_: bool = False,
    ):
        self.event = event
        self.callback = callback
        self.mode = mode
        self.prepend = prepend
        self.ctx = ctx
        self.global_ = global_
        self.is_async = inspect.iscoroutinefunction(callback) or inspect.isawaitable(
            callback
        )


async def _await_res(aw: Awaitable[Any]) -> Any:
    return await aw


class EventBus:
    """Event bus adhering to the event dispatch specification."""

    def __init__(self) -> None:
        self._listeners: dict[str, list[Listener]] = {}

    def _filter_listeners(self, event: str, this_arg: Any = None) -> list[Listener]:
        listeners = list(self._listeners.get(event, []))
        if this_arg is None or not hasattr(this_arg, "match"):
            return listeners
        filtered: list[Listener] = []
        for listener in listeners:
            if listener.global_ or listener.ctx is None or this_arg.match(listener.ctx):
                filtered.append(listener)
        return filtered

    def on(
        self,
        event: str,
        callback: Callable[..., Any],
        *,
        mode: EventMode = EventMode.EMIT,
        prepend: bool = False,
        ctx: Any | None = None,
        global_: bool = False,
    ) -> Disposer:
        """Register an event listener. Returns a disposer function."""
        listener = Listener(
            event=event,
            callback=callback,
            mode=mode,
            prepend=prepend,
            ctx=ctx,
            global_=global_,
        )
        if event not in self._listeners:
            self._listeners[event] = []

        if prepend:
            self._listeners[event].insert(0, listener)
        else:
            self._listeners[event].append(listener)

        def dispose() -> None:
            if event in self._listeners and listener in self._listeners[event]:
                self._listeners[event].remove(listener)
                if not self._listeners[event]:
                    del self._listeners[event]

        return dispose

    def once(
        self,
        event: str,
        callback: Callable[..., Any],
        *,
        mode: EventMode = EventMode.EMIT,
        prepend: bool = False,
        ctx: Any | None = None,
        global_: bool = False,
    ) -> Disposer:
        """Register a one-shot event listener that disposes itself after first execution."""
        disposed = False
        inner_disposer: Disposer | None = None

        def dispose_self() -> None:
            nonlocal disposed
            if not disposed:
                disposed = True
                assert inner_disposer is not None
                inner_disposer()

        if inspect.iscoroutinefunction(callback):

            async def _async_wrapper(*args: Any, **kwargs: Any) -> Any:
                dispose_self()
                return await callback(*args, **kwargs)

            inner_disposer = self.on(
                event,
                _async_wrapper,
                mode=mode,
                prepend=prepend,
                ctx=ctx,
                global_=global_,
            )
        else:

            def _sync_wrapper(*args: Any, **kwargs: Any) -> Any:
                dispose_self()
                return callback(*args, **kwargs)

            inner_disposer = self.on(
                event,
                _sync_wrapper,
                mode=mode,
                prepend=prepend,
                ctx=ctx,
                global_=global_,
            )

        return dispose_self

    def emit(self, event: str, *args: Any, this_arg: Any = None, **kwargs: Any) -> None:
        """Synchronous broadcast. Listeners observe in registration order without awaiting."""
        listeners = self._filter_listeners(event, this_arg)
        for listener in listeners:
            try:
                res = listener.callback(*args, **kwargs)
                if inspect.isawaitable(res):
                    try:
                        loop = asyncio.get_running_loop()
                        loop.create_task(_await_res(res))
                    except RuntimeError:
                        asyncio.run(_await_res(res))
            except Exception:
                # Observers do not crash the emitter
                import logging

                logging.getLogger("anima.events").exception("Error in emit listener")

    async def parallel(
        self, event: str, *args: Any, this_arg: Any = None, **kwargs: Any
    ) -> list[Any]:
        """All listeners run concurrently and are awaited together."""
        listeners = self._filter_listeners(event, this_arg)
        tasks = []
        for listener in listeners:
            res = listener.callback(*args, **kwargs)
            if inspect.isawaitable(res):
                tasks.append(res)
            else:

                async def _sync_wrapper(val: Any) -> Any:
                    return val

                tasks.append(_sync_wrapper(res))

        if not tasks:
            return []
        return await asyncio.gather(*tasks)

    async def serial(
        self, event: str, *args: Any, this_arg: Any = None, **kwargs: Any
    ) -> Any:
        """Listeners run in order, awaited. The first non-None return value wins and stops the rest."""
        listeners = self._filter_listeners(event, this_arg)
        for listener in listeners:
            res = listener.callback(*args, **kwargs)
            if inspect.isawaitable(res):
                res = await res
            if res is not None:
                return res
        return None

    def bail(self, event: str, *args: Any, this_arg: Any = None, **kwargs: Any) -> Any:
        """Synchronous version of serial. First non-None return value wins."""
        listeners = self._filter_listeners(event, this_arg)
        for listener in listeners:
            res = listener.callback(*args, **kwargs)
            if inspect.isawaitable(res):
                # Cannot block safely in synchronous bail if awaitable returned
                res = asyncio.run(_await_res(res))
            if res is not None:
                return res
        return None

    async def waterfall(
        self,
        event: str,
        *args: Any,
        this_arg: Any = None,
        default_handler: Callable[..., Any] | None = None,
        **kwargs: Any,
    ) -> Any:
        """Around-middleware pipeline.

        Each listener receives `(*args, next)`.
        Calling `await next(*args)` passes control down the chain.
        Returning without calling `next()` short-circuits.
        """
        listeners = self._filter_listeners(event, this_arg)

        async def dispatch(
            index: int, *current_args: Any, **current_kwargs: Any
        ) -> Any:
            if index >= len(listeners):
                if default_handler is not None:
                    res = default_handler(*current_args, **current_kwargs)
                    if inspect.isawaitable(res):
                        return await res
                    return res
                return current_args[0] if current_args else None

            current_listener = listeners[index]

            async def next_fn(*next_args: Any, **next_kwargs: Any) -> Any:
                call_args = next_args if next_args else current_args
                merged_kwargs = {**current_kwargs, **next_kwargs}
                return await dispatch(index + 1, *call_args, **merged_kwargs)

            cb = current_listener.callback
            sig = inspect.signature(cb)
            params = list(sig.parameters.values())
            has_varargs = any(
                p.kind == inspect.Parameter.VAR_POSITIONAL for p in params
            )
            param_names = [p.name for p in params]

            if "next" in param_names or "next_fn" in param_names:
                next_name = "next" if "next" in param_names else "next_fn"
                param = sig.parameters[next_name]
                if param.kind == inspect.Parameter.KEYWORD_ONLY:
                    merged = dict(current_kwargs)
                    merged[next_name] = next_fn
                    res = cb(*current_args, **merged)
                else:
                    res = cb(*current_args, next_fn, **current_kwargs)
            elif has_varargs or len(params) > len(current_args):
                res = cb(*current_args, next_fn, **current_kwargs)
            else:
                res = cb(*current_args, **current_kwargs)

            if inspect.isawaitable(res):
                return await res
            return res

        return await dispatch(0, *args, **kwargs)
