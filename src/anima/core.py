"""
Anima: The Composable Micro-Kernel by Magitech.
Complete specification implementing:
- Context: Service registry, isolation realms, intercept configurations, and child scopes
- Service: Base class for pluggable, dependency-injected capabilities
- Fiber: Runtime state machine, config validation, reversible effects, and introspection
- Registry: Live plugin tree introspection and dependency diagnostics
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from enum import Enum
from typing import Any, cast

from anima.events import Disposer, EventBus, EventMode

logger = logging.getLogger("anima")

_current_fiber: ContextVar[Fiber | None] = ContextVar("_current_fiber", default=None)
_fiber_uid_counter: int = 0


def _next_uid() -> int:
    global _fiber_uid_counter
    _fiber_uid_counter += 1
    return _fiber_uid_counter


class FiberState(str, Enum):
    PENDING = "PENDING"
    LOADING = "LOADING"
    ACTIVE = "ACTIVE"
    UNLOADING = "UNLOADING"
    DISPOSED = "DISPOSED"
    FAILED = "FAILED"


class Fiber:
    """The runtime handle and lifecycle manager for a loaded plugin instance."""

    def __init__(
        self,
        ctx: Context,
        target: Any,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        parent_fiber: Fiber | None = None,
    ) -> None:
        self.uid: int = _next_uid()
        self.ctx = ctx
        self.target = target
        self.args = args
        self.raw_config: dict[str, Any] = dict(kwargs)
        self.config: Any = dict(kwargs)
        self.parent_fiber = parent_fiber
        self.child_fibers: list[Fiber] = []
        self.effects: list[Disposer] = []
        self.state: FiberState = FiberState.PENDING
        self.error: Exception | None = None
        self.name: str = getattr(
            target, "name", getattr(target, "__name__", str(target))
        )

        # Validate config if plugin declares a Config schema (e.g. Pydantic or callable)
        self._validate_config()

        # Read inject dependencies
        inject_decl = getattr(target, "inject", None)
        if isinstance(inject_decl, (list, tuple, set)):
            self.inject: list[str] = list(inject_decl)
        else:
            self.inject = []

    def _validate_config(self) -> None:
        config_schema = getattr(self.target, "Config", None)
        if config_schema is not None and callable(config_schema):
            try:
                self.config = config_schema(**self.raw_config)
            except Exception as e:  # noqa: BLE001
                self.error = e
                self.state = FiberState.FAILED
                logger.error(
                    "Config validation failed for plugin %s (uid %d): %s",
                    self.name,
                    self.uid,
                    e,
                )

    def _set_state(self, new_state: FiberState, error: Exception | None = None) -> None:
        old_state = self.state
        if old_state == new_state:
            return
        self.state = new_state
        self.error = error
        self.ctx.emit("internal/status", self, old_state, new_state)

    def check_dependencies(self) -> bool:
        """Check if all required services exist on context."""
        return all(self.ctx.has(service) for service in self.inject)

    async def load(self) -> Any:
        """Mount and activate the plugin."""
        if self.state in (FiberState.LOADING, FiberState.ACTIVE, FiberState.FAILED):
            return None

        if not self.check_dependencies():
            self._set_state(FiberState.PENDING)
            return None

        self._set_state(FiberState.LOADING)
        token = _current_fiber.set(self)
        instance = None
        try:
            target = self.target

            # 1. Target is a class (e.g. Service subclass or general plugin class)
            if inspect.isclass(target):
                target_cls = cast(type[Any], target)
                instance = target_cls(self.ctx, *self.args, **self.raw_config)
                if hasattr(instance, "apply") and callable(instance.apply):
                    res = instance.apply(self.ctx)
                    if inspect.isawaitable(res):
                        await res
            # 2. Target is an object with an apply method
            elif hasattr(target, "apply") and callable(target.apply):
                res = target.apply(self.ctx, *self.args, **self.raw_config)
                if inspect.isawaitable(res):
                    await res
                instance = target
            # 3. Target is a callable / function plugin
            elif callable(target):
                res = target(self.ctx, *self.args, **self.raw_config)
                if inspect.isawaitable(res):
                    await res
                instance = res
            else:
                raise ValueError(
                    f"Plugin target {target} must be a callable or class with apply(ctx)"
                )

            self._set_state(FiberState.ACTIVE)
            return instance
        except Exception as e:
            self._set_state(FiberState.FAILED, error=e)
            logger.exception(
                "Plugin %s (uid %d) failed during load", self.name, self.uid
            )
            raise
        finally:
            _current_fiber.reset(token)

    async def dispose(self) -> None:
        """Unload plugin and unwind all registered effects in LIFO order."""
        if self.state in (FiberState.UNLOADING, FiberState.DISPOSED):
            return

        self._set_state(FiberState.UNLOADING)

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
            except Exception:
                logger.exception(
                    "Error during fiber %d (%s) disposal", self.uid, self.name
                )

        self.effects.clear()
        self._set_state(FiberState.DISPOSED)

    def status(self) -> dict[str, Any]:
        """Return diagnostic status snapshot of this fiber."""
        return {
            "uid": self.uid,
            "name": self.name,
            "state": self.state.value,
            "inject": self.inject,
            "effects_count": len(self.effects),
            "children_count": len(self.child_fibers),
            "error": str(self.error) if self.error else None,
        }


class Service:
    """Base class for named capabilities mounted onto Context."""

    def __init__(self, ctx: Context, name: str) -> None:
        self.ctx = ctx
        self.name = name
        ctx._register_service(name, self)

        # Register unregister disposer effect
        def _unregister() -> None:
            ctx._unregister_service(name)

        ctx.effect(lambda: _unregister)


class Context:
    """The central Anima Context repository of services, events, effects, and scopes."""

    def __init__(
        self,
        parent: Context | None = None,
        *,
        isolated_services: set[str] | None = None,
        scope_label: str | None = None,
    ) -> None:
        self._parent = parent
        self._services: dict[str, Any] = {}
        self._fibers: list[Fiber] = []
        self._pending_tasks: set[asyncio.Task[Any]] = set()
        self._event_bus: EventBus = parent._event_bus if parent else EventBus()
        self._isolated_services: set[str] = isolated_services or set()
        self._scope_label = scope_label
        self._intercept_configs: dict[str, dict[str, Any]] = {}

    @property
    def root(self) -> Context:
        """Return the root ancestor context."""
        curr = self
        while curr._parent is not None:
            curr = curr._parent
        return curr

    async def flush(self) -> None:
        """Wait for all pending plugin load and event tasks to settle."""
        while self._pending_tasks:
            tasks = list(self._pending_tasks)
            await asyncio.gather(*tasks, return_exceptions=True)
            for t in tasks:
                self._pending_tasks.discard(t)

    def _schedule_task(self, coro: Awaitable[Any]) -> asyncio.Task[Any] | None:
        async def _run() -> Any:
            return await coro

        try:
            loop = asyncio.get_running_loop()
            task: asyncio.Task[Any] = loop.create_task(_run())
            self._pending_tasks.add(task)
            task.add_done_callback(self._pending_tasks.discard)
            return task
        except RuntimeError:
            asyncio.run(_run())
            return None

    def has(self, name: str) -> bool:
        """Check if a service is registered in this context scope."""
        if name in self._services:
            return True
        if name in self._isolated_services:
            return False
        return self._parent.has(name) if self._parent else False

    def get(self, name: str, default: Any = None) -> Any:
        """Safely fetch a service, respecting service isolation boundaries."""
        if name in self._services:
            return self._services[name]
        if name in self._isolated_services:
            return default
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
            f"Available services: {list(self.services.keys())}"
        )

    @property
    def services(self) -> dict[str, Any]:
        """Dictionary of all accessible services in this scope."""
        all_svcs: dict[str, Any] = {}
        if self._parent:
            for k, v in self._parent.services.items():
                if k not in self._isolated_services:
                    all_svcs[k] = v
        all_svcs.update(self._services)
        return all_svcs

    def _register_service(self, name: str, service: Any) -> None:
        """Register a service and re-evaluate any PENDING fibers waiting on it."""
        self._services[name] = service
        self.emit("internal/service", name, service)

        # Check pending fibers across this context and children
        for fiber in list(self._fibers):
            if fiber.state == FiberState.PENDING and fiber.check_dependencies():
                self._schedule_task(fiber.load())

    def _unregister_service(self, name: str) -> None:
        """Unregister a service and dispose any dependent fibers."""
        if name in self._services:
            del self._services[name]
            self.emit("internal/service-removed", name)
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

        self.emit("internal/plugin", fiber)

        # If dependencies satisfied and not failed, load now
        if fiber.state != FiberState.FAILED and fiber.check_dependencies():
            self._schedule_task(fiber.load())
        return fiber

    def effect(
        self, func: Callable[[], Disposer | None], label: str | None = None
    ) -> Disposer:
        """Register a cleanup-aware reversible effect on the active fiber."""
        fiber = _current_fiber.get()
        disposer = func()

        def _cleanup() -> Any:
            if callable(disposer):
                if fiber is not None and disposer in fiber.effects:
                    fiber.effects.remove(disposer)
                res = disposer()
                return res
            return None

        if callable(disposer) and fiber is not None:
            fiber.effects.append(disposer)

        return _cleanup

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
        """Create a child context inheriting all parent services."""
        return Context(parent=self)

    def isolate(self, service_name: str, label: str | None = None) -> Context:
        """Create a child context with an isolated service scope for `service_name`.

        Reads and writes of `service_name` in the child will not resolve or affect the parent.
        """
        isolated = set(self._isolated_services)
        isolated.add(service_name)
        return Context(parent=self, isolated_services=isolated, scope_label=label)

    def dump_tree(self) -> dict[str, Any]:
        """Dump the full runtime hierarchy of plugins and services for introspection."""
        return {
            "services": list(self.services.keys()),
            "isolated": list(self._isolated_services),
            "fibers": [f.status() for f in self._fibers],
        }
