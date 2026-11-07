"""
Core Kernel primitives in Python:
- Context: Container of services, events, effects, and child contexts
- Service: Base class for pluggable capabilities
- Fiber: Runtime state machine and lifecycle handle for plugin instances
"""

from __future__ import annotations

import asyncio
import inspect
from contextvars import ContextVar
from enum import Enum
from typing import Any, Awaitable, Callable, Union

from kernel.events import Disposer, EventBus, EventMode

_current_fiber: ContextVar[Fiber | None] = ContextVar("_current_fiber", default=None)


class FiberState(str, Enum):
    PENDING = "PENDING"
    LOADING = "LOADING"
    ACTIVE = "ACTIVE"
    UNLOADING = "UNLOADING"
    DISPOSED = "DISPOSED"
    FAILED = "FAILED"


class Fiber:
    """The runtime handle for a loaded plugin instance."""

    def __init__(
        self,
        ctx: Context,
        target: Any,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        parent_fiber: Fiber | None = None,
    ) -> None:
        self.ctx = ctx
        self.target = target
        self.args = args
        self.kwargs = kwargs
        self.parent_fiber = parent_fiber
        self.child_fibers: list[Fiber] = []
        self.effects: list[Disposer] = []
        self.state: FiberState = FiberState.PENDING
        self.name: str = getattr(target, "name", getattr(target, "__name__", str(target)))
        
        # Read inject dependencies
        inject_decl = getattr(target, "inject", None)
        if isinstance(inject_decl, (list, tuple, set)):
            self.inject: list[str] = list(inject_decl)
        else:
            self.inject = []

    def check_dependencies(self) -> bool:
        """Check if all required services exist on context."""
        return all(self.ctx.has(service) for service in self.inject)

    async def load(self) -> Any:
        """Mount and activate the plugin."""
        if self.state in (FiberState.LOADING, FiberState.ACTIVE):
            return None

        if not self.check_dependencies():
            self.state = FiberState.PENDING
            return None

        self.state = FiberState.LOADING
        token = _current_fiber.set(self)
        instance = None
        try:
            target = self.target
            # 1. Target is a class (e.g. Service subclass or general class)
            if inspect.isclass(target):
                instance = target(self.ctx, *self.args, **self.kwargs)
                if hasattr(instance, "apply") and callable(instance.apply):
                    res = instance.apply(self.ctx)
                    if inspect.isawaitable(res):
                        await res
            # 2. Target is an object with an apply method
            elif hasattr(target, "apply") and callable(target.apply):
                res = target.apply(self.ctx, *self.args, **self.kwargs)
                if inspect.isawaitable(res):
                    await res
                instance = target
            # 3. Target is a callable / function plugin
            elif callable(target):
                res = target(self.ctx, *self.args, **self.kwargs)
                if inspect.isawaitable(res):
                    await res
                instance = res
            else:
                raise ValueError(f"Plugin target {target} must be a callable or class with apply(ctx)")

            self.state = FiberState.ACTIVE
            return instance
        except Exception:
            self.state = FiberState.FAILED
            raise
        finally:
            _current_fiber.reset(token)

    async def dispose(self) -> None:
        """Unload plugin and unwind all registered effects in LIFO order."""
        if self.state in (FiberState.UNLOADING, FiberState.DISPOSED):
            return

        self.state = FiberState.UNLOADING

        # Dispose child fibers first
        for child in reversed(self.child_fibers):
            await child.dispose()
        self.child_fibers.clear()

        # Run registered effect disposers in reverse order (LIFO)
        for disposer in reversed(self.effects):
            try:
                res = disposer()
                if inspect.isawaitable(res):
                    await res
            except Exception as e:
                import logging
                logging.getLogger("kernel.fiber").exception("Error during fiber disposal: %s", e)

        self.effects.clear()
        self.state = FiberState.DISPOSED


class Service:
    """Base class for services mounted on Context."""

    def __init__(self, ctx: Context, name: str) -> None:
        self.ctx = ctx
        self.name = name
        ctx._register_service(name, self)

        # Register unregister disposer effect
        def _unregister():
            ctx._unregister_service(name)

        ctx.effect(lambda: _unregister)


class Context:
    """The central Context repository of services, events, and plugins."""

    def __init__(self, parent: Context | None = None) -> None:
        self._parent = parent
        self._services: dict[str, Any] = {}
        self._fibers: list[Fiber] = []
        self._pending_tasks: set[asyncio.Task] = set()
        self._event_bus: EventBus = parent._event_bus if parent else EventBus()

    async def flush(self) -> None:
        """Wait for all pending plugin load and event tasks to settle."""
        while self._pending_tasks:
            tasks = list(self._pending_tasks)
            await asyncio.gather(*tasks, return_exceptions=True)

    def _schedule_task(self, coro: Awaitable[Any]) -> asyncio.Task | None:
        try:
            loop = asyncio.get_running_loop()
            task = loop.create_task(coro)
            self._pending_tasks.add(task)
            task.add_done_callback(self._pending_tasks.discard)
            return task
        except RuntimeError:
            asyncio.run(coro)
            return None

    def has(self, name: str) -> bool:
        """Check if a service is registered in this context or parent."""
        if name in self._services:
            return True
        return self._parent.has(name) if self._parent else False

    def get(self, name: str, default: Any = None) -> Any:
        """Safely fetch a service, returning default if not present."""
        if name in self._services:
            return self._services[name]
        if self._parent:
            return self._parent.get(name, default)
        return default

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            return super().__getattribute__(name)
        svc = self.get(name)
        if svc is not None:
            return svc
        raise AttributeError(
            f"Service '{name}' is not registered on Context. "
            f"Available services: {list(self._services.keys())}"
        )

    def _register_service(self, name: str, service: Any) -> None:
        """Register a service and re-evaluate any PENDING fibers waiting on it."""
        self._services[name] = service
        # Check pending fibers
        for fiber in list(self._fibers):
            if fiber.state == FiberState.PENDING and fiber.check_dependencies():
                self._schedule_task(fiber.load())

    def _unregister_service(self, name: str) -> None:
        """Unregister a service and dispose any dependent fibers."""
        if name in self._services:
            del self._services[name]
            # Fibers depending on this service must be disposed
            for fiber in list(self._fibers):
                if fiber.state == FiberState.ACTIVE and name in fiber.inject:
                    self._schedule_task(fiber.dispose())

    def plugin(self, target: Any, *args: Any, **kwargs: Any) -> Fiber:
        """Mount a plugin (class, object, or function) onto this context."""
        current_fib = _current_fiber.get()
        fiber = Fiber(self, target, args, kwargs, parent_fiber=current_fib)
        self._fibers.append(fiber)
        if current_fib is not None:
            current_fib.child_fibers.append(fiber)

        # If dependencies satisfied, load now
        if fiber.check_dependencies():
            self._schedule_task(fiber.load())
        return fiber

    def effect(self, func: Callable[[], Disposer | None]) -> None:
        """Register a reversible effect with the currently loading fiber."""
        fiber = _current_fiber.get()
        disposer = func()
        if callable(disposer) and fiber is not None:
            fiber.effects.append(disposer)

    def on(
        self,
        event: str,
        callback: Callable[..., Any],
        *,
        mode: EventMode = EventMode.EMIT,
        prepend: bool = False,
    ) -> Disposer:
        """Register an event listener as an effect."""
        disposer = self._event_bus.on(event, callback, mode=mode, prepend=prepend)
        self.effect(lambda: disposer)
        return disposer

    def emit(self, event: str, *args: Any, **kwargs: Any) -> None:
        """Synchronously broadcast an event."""
        self._event_bus.emit(event, *args, **kwargs)

    async def parallel(self, event: str, *args: Any, **kwargs: Any) -> list[Any]:
        """Concurrently broadcast an event and await all listeners."""
        return await self._event_bus.parallel(event, *args, **kwargs)

    async def serial(self, event: str, *args: Any, **kwargs: Any) -> Any:
        """Sequential async execution; stops on first non-None."""
        return await self._event_bus.serial(event, *args, **kwargs)

    def bail(self, event: str, *args: Any, **kwargs: Any) -> Any:
        """Sequential sync execution; stops on first non-None."""
        return self._event_bus.bail(event, *args, **kwargs)

    async def waterfall(
        self,
        event: str,
        *args: Any,
        default_handler: Callable[..., Any] | None = None,
        **kwargs: Any,
    ) -> Any:
        """Around-middleware pipeline passing (*args, next)."""
        return await self._event_bus.waterfall(
            event, *args, default_handler=default_handler, **kwargs
        )

    def extend(self) -> Context:
        """Create an isolated child context (for subagents or scoped presets)."""
        return Context(parent=self)
