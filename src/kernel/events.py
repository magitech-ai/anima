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
from enum import Enum
from typing import Any, Awaitable, Callable, Union

Disposer = Callable[[], Union[None, Awaitable[None]]]


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
    ):
        self.event = event
        self.callback = callback
        self.mode = mode
        self.prepend = prepend
        self.is_async = inspect.iscoroutinefunction(callback) or inspect.isawaitable(callback)


class EventBus:
    """Event bus adhering to the event dispatch specification."""

    def __init__(self) -> None:
        self._listeners: dict[str, list[Listener]] = {}

    def on(
        self,
        event: str,
        callback: Callable[..., Any],
        *,
        mode: EventMode = EventMode.EMIT,
        prepend: bool = False,
    ) -> Disposer:
        """Register an event listener. Returns a disposer function."""
        listener = Listener(event=event, callback=callback, mode=mode, prepend=prepend)
        if event not in self._listeners:
            self._listeners[event] = []

        if prepend:
            self._listeners[event].insert(0, listener)
        else:
            self._listeners[event].append(listener)

        def dispose() -> None:
            if event in self._listeners and listener in self._listeners[event]:
                self._listeners[event].remove(listener)

        return dispose

    def emit(self, event: str, *args: Any, **kwargs: Any) -> None:
        """Synchronous broadcast. Listeners observe in registration order without awaiting."""
        listeners = list(self._listeners.get(event, []))
        for listener in listeners:
            try:
                res = listener.callback(*args, **kwargs)
                if inspect.isawaitable(res):
                    try:
                        loop = asyncio.get_running_loop()
                        loop.create_task(res)
                    except RuntimeError:
                        asyncio.run(res)
            except Exception as e:
                # Observers do not crash the emitter
                import logging
                logging.getLogger("kernel.events").exception("Error in emit listener: %s", e)

    async def parallel(self, event: str, *args: Any, **kwargs: Any) -> list[Any]:
        """All listeners run concurrently and are awaited together."""
        listeners = list(self._listeners.get(event, []))
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

    async def serial(self, event: str, *args: Any, **kwargs: Any) -> Any:
        """Listeners run in order, awaited. The first non-None return value wins and stops the rest."""
        listeners = list(self._listeners.get(event, []))
        for listener in listeners:
            res = listener.callback(*args, **kwargs)
            if inspect.isawaitable(res):
                res = await res
            if res is not None:
                return res
        return None

    def bail(self, event: str, *args: Any, **kwargs: Any) -> Any:
        """Synchronous version of serial. First non-None return value wins."""
        listeners = list(self._listeners.get(event, []))
        for listener in listeners:
            res = listener.callback(*args, **kwargs)
            if inspect.isawaitable(res):
                # Cannot block safely in synchronous bail if awaitable returned
                res = asyncio.run(res)
            if res is not None:
                return res
        return None

    async def waterfall(
        self,
        event: str,
        *args: Any,
        default_handler: Callable[..., Any] | None = None,
        **kwargs: Any,
    ) -> Any:
        """Around-middleware pipeline.
        
        Each listener receives `(*args, next)`.
        Calling `await next(*args)` passes control down the chain.
        Returning without calling `next()` short-circuits.
        """
        listeners = list(self._listeners.get(event, []))

        async def dispatch(index: int, *current_args: Any, **current_kwargs: Any) -> Any:
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
            # Inspect callback signature to pass next_fn appropriately
            sig = inspect.signature(cb)
            params = list(sig.parameters.values())

            # If last parameter is 'next' or named accordingly, pass next_fn
            if len(params) > len(current_args):
                res = cb(*current_args, next_fn, **current_kwargs)
            else:
                res = cb(*current_args, next=next_fn, **current_kwargs)

            if inspect.isawaitable(res):
                return await res
            return res

        return await dispatch(0, *args, **kwargs)
