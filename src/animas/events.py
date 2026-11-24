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
import logging
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from enum import Enum
from typing import Any

logger = logging.getLogger("animas.events")

Disposer = Callable[[], None | Awaitable[None]]


class DisposableList:
    """Ordered collection of disposable values with O(1) deletion by value.

    DisposableList parity. Uses a serial-number map and a reverse
    WeakMap-like index for constant-time insert, delete, and clear.
    """

    def __init__(self) -> None:
        self._sn = 0
        self._map: OrderedDict[int, Any] = OrderedDict()
        self._weak: dict[Any, int] = {}

    @property
    def length(self) -> int:
        """Return the number of disposables in the list."""
        return len(self._map)

    def _make_disposer(self, sn: int, value: Any) -> Callable[[], None]:
        def dispose() -> None:
            self._map.pop(sn, None)
            if self._weak.get(value) == sn:
                self._weak.pop(value, None)

        return dispose

    def push(self, value: Any) -> Callable[[], None]:
        """Append a disposable and return a disposer that removes it."""
        self._sn += 1
        sn = self._sn
        self._map[sn] = value
        self._weak[value] = sn
        return self._make_disposer(sn, value)

    def unshift(self, value: Any) -> Callable[[], None]:
        """Prepend a disposable and return a disposer that removes it."""
        self._sn += 1
        sn = self._sn
        self._map[sn] = value
        self._map.move_to_end(sn, last=False)
        self._weak[value] = sn
        return self._make_disposer(sn, value)

    def delete(self, value: Any) -> bool:
        """Remove a specific disposable by identity. Returns True if found."""
        sn = self._weak.get(value)
        if sn is None:
            return False
        self._weak.pop(value, None)
        return self._map.pop(sn, None) is not None

    def clear(self) -> list[Any]:
        """Remove and return all disposables in list order (front first)."""
        values = list(self._map.values())
        self._map.clear()
        self._weak.clear()
        return values

    def __iter__(self) -> Any:
        return iter(self._map.values())

    def __len__(self) -> int:
        return self.length


def is_bailed(value: Any) -> bool:
    """isBailed(value) - true unless value is None or False."""
    return value is not None and value is not False


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
        owner: Any | None = None,
    ):
        self.owner = owner
        self.event = event
        self.callback = callback
        self.mode = mode
        self.prepend = prepend
        self.ctx = ctx
        self.global_ = global_
        self.is_async = inspect.iscoroutinefunction(callback) or inspect.isawaitable(
            callback
        )
        # Dispatch snapshots listener references, so removal has to be visible
        # through the listener itself, not only through the registry list.
        self.removed = False


async def _await_res(aw: Awaitable[Any]) -> Any:
    return await aw


def _listener_label(callback: Callable[..., Any], index: int) -> str:
    """Return a stable, human-readable label for a listener callback."""
    name = getattr(callback, "__qualname__", None) or repr(callback)
    return f"[{index}] {name}"


async def _dispatch_one(
    listener: Listener, args: tuple[Any, ...], kwargs: dict[str, Any]
) -> Any:
    """Invoke one listener and await its result when it is awaitable.

    Invoking the callback inside a coroutine (rather than in the dispatch loop)
    keeps a listener that raises synchronously from aborting the dispatch before
    its siblings are even started. A listener disposed by a sibling during
    dispatch is skipped at invocation time.
    """
    if listener.removed:
        return None
    result = listener.callback(*args, **kwargs)
    if inspect.isawaitable(result):
        return await result
    return result


class EventBus:
    """Event bus adhering to the event dispatch specification."""

    def __init__(self) -> None:
        self._listeners: dict[str, list[Listener]] = {}

    def _filter_listeners(
        self, event: str, this_arg: Any = None, owner: Any = None
    ) -> list[Listener]:
        listeners = list(self._listeners.get(event, []))
        if event == "internal/update" and owner is not None:
            # rc.13 routes non-global internal/update listeners into the
            # registering fiber's hook list (events.ts:54-79). The owner is
            # passed explicitly by Fiber.update(), since the ambient fiber may
            # belong to the caller of a nested update.
            listeners = [
                listener
                for listener in listeners
                if listener.global_ or listener.owner is None or listener.owner is owner
            ]
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
        owner: Any | None = None,
    ) -> Disposer:
        """Register an event listener. Returns a disposer function."""
        listener = Listener(
            event=event,
            callback=callback,
            mode=mode,
            prepend=prepend,
            ctx=ctx,
            global_=global_,
            owner=owner,
        )
        if event not in self._listeners:
            self._listeners[event] = []

        if prepend:
            self._listeners[event].insert(0, listener)
        else:
            self._listeners[event].append(listener)

        def dispose() -> None:
            listener.removed = True
            bucket = self._listeners.get(event)
            if bucket is not None and listener in bucket:
                bucket.remove(listener)
                if not bucket:
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

        def _consume() -> bool:
            """Claim the single invocation. False once it has been claimed."""
            nonlocal disposed
            if disposed:
                return False
            disposed = True
            assert inner_disposer is not None
            inner_disposer()
            return True

        def dispose_self() -> None:
            _consume()

        if inspect.iscoroutinefunction(callback):

            async def _async_wrapper(*args: Any, **kwargs: Any) -> Any:
                if not _consume():
                    return None
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
                if not _consume():
                    return None
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
            if listener.removed:
                continue
            # Upstream events.ts:121-126 does not catch listener errors: a
            # throwing observer aborts the broadcast and reaches the caller.
            res = listener.callback(*args, **kwargs)
            if inspect.isawaitable(res):
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    asyncio.run(_await_res(res))
                else:
                    task = loop.create_task(_await_res(res))

                    def report(done: Any) -> None:
                        if not done.cancelled() and done.exception():
                            logger.exception(
                                "Error in async emit listener",
                                exc_info=done.exception(),
                            )

                    task.add_done_callback(report)

    async def parallel(
        self, event: str, *args: Any, this_arg: Any = None, **kwargs: Any
    ) -> None:
        """All listeners run concurrently and are awaited together.

        Every listener is awaited to completion whether or not a sibling fails,
        so a failing listener can neither cancel the healthy ones nor mask the
        other failures. All ``Exception`` failures are aggregated into one
        ``ExceptionGroup`` — including a single failure, which becomes a group
        with one member. This is the Cordis rc.13 contract: ``parallel`` throws
        ``AggregateError`` whenever any listener rejects (``events.ts:110-119``).
        A non-``Exception`` ``BaseException`` (e.g. ``CancelledError``) cannot be
        grouped and is re-raised as-is once every listener has finished. Each
        failure carries a note identifying its listener.

        Raises:
            BaseException: The first non-``Exception`` failure, re-raised as-is.
            ExceptionGroup: Whenever one or more listeners failed with exceptions.
        """
        listeners = [
            listener
            for listener in self._filter_listeners(event, this_arg)
            if not listener.removed
        ]
        if not listeners:
            return
        results = await asyncio.gather(
            *(_dispatch_one(listener, args, kwargs) for listener in listeners),
            return_exceptions=True,
        )
        failures: list[tuple[str, Exception]] = []
        fatal: BaseException | None = None
        for index, (listener, result) in enumerate(zip(listeners, results)):
            if not isinstance(result, BaseException):
                continue
            label = _listener_label(listener.callback, index)
            result.add_note(f"parallel listener {label} for event {event!r}")
            if isinstance(result, Exception):
                failures.append((label, result))
            elif fatal is None:
                # CancelledError / KeyboardInterrupt / GeneratorExit: not
                # aggregatable (ExceptionGroup holds Exceptions only) and must
                # never be swallowed, so re-raise it once every listener has
                # finished.
                fatal = result
        if fatal is not None:
            raise fatal
        if not failures:
            return
        labels = ", ".join(label for label, _ in failures)
        raise ExceptionGroup(
            f"{len(failures)} of {len(listeners)} listeners failed "
            f"for event {event!r}: {labels}",
            [exc for _, exc in failures],
        )

    async def serial(
        self, event: str, *args: Any, this_arg: Any = None, **kwargs: Any
    ) -> Any:
        """Serial - listeners run in order, awaited. First bailed value wins."""
        listeners = self._filter_listeners(event, this_arg)
        for listener in listeners:
            if listener.removed:
                continue
            res = listener.callback(*args, **kwargs)
            if inspect.isawaitable(res):
                res = await res
            if is_bailed(res):
                return res
        return None

    def bail(self, event: str, *args: Any, this_arg: Any = None, **kwargs: Any) -> Any:
        """Bail - synchronous serial. First bailed value wins."""
        listeners = self._filter_listeners(event, this_arg)
        for listener in listeners:
            if listener.removed:
                continue
            res = listener.callback(*args, **kwargs)
            if inspect.isawaitable(res):
                try:
                    asyncio.get_running_loop()
                except RuntimeError:
                    # No loop to block: the historical sync-only path remains.
                    res = asyncio.run(_await_res(res))
                else:
                    # A synchronous bail cannot block on an awaitable inside a
                    # running loop, and returning it would leak an unawaited
                    # coroutine. Fail loudly instead.
                    close = getattr(res, "close", None)
                    if close is not None:
                        close()
                    raise TypeError(
                        "bail() cannot dispatch an async listener inside a "
                        "running event loop; use serial() instead"
                    )
            if is_bailed(res):
                return res
        return None

    async def waterfall(
        self,
        event: str,
        *args: Any,
        this_arg: Any = None,
        default_handler: Callable[..., Any] | None = None,
        owner: Any = None,
        **kwargs: Any,
    ) -> Any:
        """Around-middleware pipeline.

        Each listener receives `(*args, next)`.
        Calling `await next(*args)` passes control down the chain.
        Returning without calling `next()` short-circuits.
        """
        listeners = self._filter_listeners(event, this_arg, owner)

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

            if current_listener.removed:
                return await dispatch(index + 1, *current_args, **current_kwargs)

            called = False

            async def next_fn(*next_args: Any, **next_kwargs: Any) -> Any:
                nonlocal called
                if called:
                    # Upstream events.ts:155-157 rejects a second delegation.
                    raise RuntimeError("next() called multiple times")
                called = True
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
