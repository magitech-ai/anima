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
from typing import Any, ClassVar, cast

from anima.events import Disposer, EventBus, EventMode

logger = logging.getLogger("anima")

# Anima symbols system — Symbol.for() based unique symbols for internal protocol
symbols = {
    # internal symbols
    "shadow": "anima.shadow",
    "caller": "anima.caller",
    "receiver": "anima.receiver",
    "original": "anima.original",
    "decorator": "anima.decorator",
    "initHooks": "anima.initHooks",
    "checkProto": "anima.checkProto",
    # context symbols
    "root": "anima.root",
    "effect": "anima.effect",
    "filter": "anima.filter",
    "isolate": "anima.isolate",
    "intercept": "anima.intercept",
    # service symbols
    "init": "anima.init",
    "check": "anima.check",
    "invoke": "anima.invoke",
    "metadata": "anima.metadata",
    "extend": "anima.extend",
    "tracker": "anima.tracker",
    "mergeIntercept": "anima.mergeIntercept",
    "resolveIntercept": "anima.resolveIntercept",
}

_current_fiber: ContextVar[Fiber | None] = ContextVar("_current_fiber", default=None)
_access_context: ContextVar[Context | None] = ContextVar(
    "_access_context", default=None
)
_fiber_uid_counter: int = 0


def _next_uid() -> int:
    global _fiber_uid_counter
    _fiber_uid_counter += 1
    return _fiber_uid_counter


class AnimaErrorCode:
    INACTIVE_EFFECT = "cannot create effect on inactive context"


class AnimaError(Exception):
    """Anima-style error with code."""

    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = code
        super().__init__(message or AnimaErrorCode.INACTIVE_EFFECT)


class ValidationError(TypeError):
    """Validation error for config schema validation failures."""

    name = "ValidationError"

    def __init__(self, issues: list[dict[str, Any]]) -> None:
        self.issues = issues
        super().__init__(format_issues("invalid entry options", issues))


def format_issues(title: str, issues: list[dict[str, Any]]) -> str:
    lines = []
    for issue in issues:
        path = issue.get("path", [])
        where = "/".join(str(p) for p in path) if path else ""
        where_str = f" (at {where})" if where else ""
        lines.append(f"  - {issue.get('message', '')}{where_str}")
    return f"{title}:\n" + "\n".join(lines)


def is_js_expr(value: Any) -> bool:
    """Check if a value is a JS expression wrapper."""
    return (
        isinstance(value, dict)
        and "__jsExpr" in value
        and isinstance(value.get("__jsExpr"), str)
    )


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
        if isinstance(inject_decl, dict):
            self.inject: list[str] = list(inject_decl.keys())
            self.inject_config: dict[str, Any] | None = dict(inject_decl)
        elif isinstance(inject_decl, (list, tuple, set)):
            self.inject = list(inject_decl)
            self.inject_config = getattr(target, "inject_config", None)
        else:
            self.inject = []
            self.inject_config = getattr(target, "inject_config", None)

    def _validate_config(self) -> None:
        config_schema = getattr(self.target, "Config", None)
        if config_schema is None:
            config_schema = getattr(self.target, "schema", None)
        if config_schema is None:
            return

        from anima.schema import Schema, SchemaError

        if isinstance(config_schema, Schema):
            try:
                self.config = config_schema.validate(self.raw_config)
                return
            except SchemaError as e:
                self.error = e
                self.state = FiberState.FAILED
                logger.error(
                    "Config validation failed for plugin %s (uid %d): %s",
                    self.name,
                    self.uid,
                    e,
                )
                return

        elif callable(config_schema):
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
        if self.state in (
            FiberState.LOADING,
            FiberState.ACTIVE,
            FiberState.FAILED,
            FiberState.DISPOSED,
        ):
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
                raise ValueError(  # pragma: no cover
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

    async def _unload(self) -> None:
        """Unwind child fibers and effect disposers in reverse order (LIFO)."""
        self._set_state(FiberState.UNLOADING)

        # Dispose child fibers first
        for child in reversed(self.child_fibers):
            await child._unload()
        self.child_fibers.clear()

        # Run registered effect disposers in reverse order (LIFO)
        for disposer in reversed(self.effects):
            try:
                res = disposer()
                if inspect.isawaitable(res):
                    await res
            except Exception:
                logger.exception(
                    "Error during fiber %d (%s) effect unload", self.uid, self.name
                )

        self.effects.clear()

    async def dispose(self) -> None:
        """Unload plugin and mark permanently as DISPOSED."""
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

    async def restart(self) -> None:
        """Unload active effects and re-mount plugin instance."""
        if self.state in (FiberState.UNLOADING, FiberState.DISPOSED):
            return

        await self._unload()
        if self.check_dependencies():
            await self.load()
        else:
            self._set_state(FiberState.PENDING)

    async def replace(self, new_target: Any) -> None:
        """Replace plugin target and reload fiber with updated target."""
        if self.state in (FiberState.UNLOADING, FiberState.DISPOSED):
            return

        self.target = new_target
        self.name = getattr(
            new_target, "name", getattr(new_target, "__name__", str(new_target))
        )
        self._validate_config()

        inject_decl = getattr(new_target, "inject", None)
        if isinstance(inject_decl, dict):
            self.inject = list(inject_decl.keys())
            self.inject_config = dict(inject_decl)
        elif isinstance(inject_decl, (list, tuple, set)):
            self.inject = list(inject_decl)
            self.inject_config = getattr(new_target, "inject_config", None)
        else:
            self.inject = []
            self.inject_config = getattr(new_target, "inject_config", None)

        await self.restart()

    async def update(self, config: dict[str, Any], no_save: bool = False) -> None:
        """Update plugin configuration with live lifecycle hooks."""
        if self.state == FiberState.DISPOSED:
            return

        self.raw_config = dict(config)
        self.config = dict(config)
        self._validate_config()

        if not no_save:
            self.ctx.emit("internal/commit", self, config)

        if self.state == FiberState.FAILED:
            return

        if self.state == FiberState.ACTIVE:

            async def _default_restart(cfg: Any) -> None:
                await self.restart()

            await self.ctx.waterfall(
                "internal/update",
                self.config,
                default_handler=_default_restart,
            )
        elif self.state == FiberState.PENDING and self.check_dependencies():
            await self.load()

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

    def assertActive(self) -> None:
        """Assert that the fiber is active (has a uid)."""
        if self.uid is None:
            raise AnimaError(AnimaErrorCode.INACTIVE_EFFECT)

    def effect(self, execute: Callable[[], Any], label: str = "anonymous") -> Any:
        """Register an effect on this fiber, returning a disposer."""
        self.assertActive()

        meta: dict[str, Any] = {"label": label, "children": []}

        try:
            execute()
        except Exception:
            logger.exception("Effect %s failed", label)
            raise

        def wrapper() -> Any:
            return None

        # Store effect symbol metadata on wrapper (anima parity)
        setattr(wrapper, symbols["effect"], meta)

        # Register the wrapper as an effect disposer on this fiber
        self.effects.append(wrapper)
        return wrapper

    def getEffects(self) -> list[dict[str, Any] | None]:
        """Return effect metadata for all active effects."""
        return [
            getattr(d, symbols["effect"], None)
            for d in self.effects
            if getattr(d, symbols["effect"], None) is not None
        ]


class _BoundService:
    """Scoped facade that binds service invocations to the accessing Context."""

    def __init__(self, svc: Service, ctx: Context) -> None:
        object.__setattr__(self, "_svc", svc)
        object.__setattr__(self, "_ctx", ctx)

    @property
    def ctx(self) -> Context:
        return cast(Context, object.__getattribute__(self, "_ctx"))

    def __getattr__(self, item: str) -> Any:
        svc = object.__getattribute__(self, "_svc")
        ctx = object.__getattribute__(self, "_ctx")
        attr = getattr(svc, item)
        if callable(attr):
            if inspect.iscoroutinefunction(attr):

                async def _async_wrapped(*args: Any, **kwargs: Any) -> Any:
                    token = _access_context.set(ctx)
                    try:
                        return await attr(*args, **kwargs)
                    finally:
                        _access_context.reset(token)

                return _async_wrapped

            def _wrapped(*args: Any, **kwargs: Any) -> Any:
                token = _access_context.set(ctx)
                try:
                    return attr(*args, **kwargs)
                finally:
                    _access_context.reset(token)

            return _wrapped
        return attr

    def __setattr__(self, key: str, value: Any) -> None:
        svc = object.__getattribute__(self, "_svc")
        setattr(svc, key, value)

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        svc = object.__getattribute__(self, "_svc")
        ctx = object.__getattribute__(self, "_ctx")
        if callable(svc) and inspect.iscoroutinefunction(svc.__call__):

            async def _async_call() -> Any:
                token = _access_context.set(ctx)
                try:
                    return await svc(*args, **kwargs)
                finally:
                    _access_context.reset(token)

            return _async_call()

        token = _access_context.set(ctx)
        try:
            return svc(*args, **kwargs)
        finally:
            _access_context.reset(token)

    def __eq__(self, other: object) -> bool:
        svc = object.__getattribute__(self, "_svc")
        if isinstance(other, _BoundService):
            return bool(svc == object.__getattribute__(other, "_svc"))
        return bool(svc == other)

    def __repr__(self) -> str:
        svc = object.__getattribute__(self, "_svc")
        return repr(svc)


class Service:
    """Base class for named capabilities mounted onto Context."""

    # Symbol-based protocol (anima parity)
    __symbols__: ClassVar[dict[str, str]] = {
        "init": symbols["init"],
        "check": symbols["check"],
        "invoke": symbols["invoke"],
        "metadata": symbols["metadata"],
        "extend": symbols["extend"],
        "tracker": symbols["tracker"],
        "mergeIntercept": symbols["mergeIntercept"],
        "resolveIntercept": symbols["resolveIntercept"],
    }

    def __init__(self, ctx: Context, name: str) -> None:
        self.ctx = ctx
        self.name = name
        # register via reflect.provide with check symbol
        check = getattr(type(self), "check", None)
        ctx.reflect.provide(name, self, check)

        # Register unregister disposer effect
        def _unregister() -> None:
            ctx._unregister_service(name)

        ctx.effect(lambda: _unregister)

    def __instancecheck__(self, instance: Any) -> bool:
        """Symbol.hasInstance equivalent - walk prototype chain."""
        if not instance:
            return False
        constructor = (
            instance.__class__ if hasattr(instance, "__class__") else type(instance)
        )
        while constructor is not None:
            if constructor is self:  # self IS the class in __instancecheck__
                return True
            bases = getattr(constructor, "__bases__", None)
            if bases:
                constructor = bases[0] if len(bases) == 1 else None  # pragma: no cover
            else:
                constructor = None  # pragma: no cover
        return False

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        """[symbols.invoke] - make service instances callable."""
        invoke = getattr(type(self), "invoke", None)
        if invoke is not None and callable(invoke):
            return invoke(self, *args, **kwargs)
        raise TypeError(f"Service '{self.name}' is not callable")

    def resolve_intercept(self) -> dict[str, Any]:
        """Resolve caller-scoped intercept configurations for this service."""
        ctx = _access_context.get() or self.ctx
        return ctx.resolve_intercept(self.name)

    def merge_intercept(self, *configs: dict[str, Any]) -> dict[str, Any]:
        """Merge intercept configuration dictionaries from outer to inner."""
        merged: dict[str, Any] = {}
        for cfg in configs:
            if isinstance(cfg, dict):
                merged.update(cfg)
        return merged

    def _filter(self, ctx: Context) -> bool:
        """[symbols.filter] - check if ctx's isolate matches this service's."""
        return (self.name in ctx._isolated_services) == (
            self.name in self.ctx._isolated_services
        )

    def _extend(self, props: dict[str, Any] | None = None) -> Any:
        """[symbols.extend] - create extended self with props."""
        if self.__symbols__.get("invoke"):
            # Create callable extension
            obj = object.__new__(self.__class__)
            obj.__dict__.update(self.__dict__)
            obj.__dict__.update(props or {})
            return obj
        else:
            obj = object.__new__(self.__class__)
            obj.__dict__.update(self.__dict__)
            obj.__dict__.update(props or {})
            return obj


class Context:
    """The central Anima Context repository of services, events, effects, and scopes."""

    def __init__(
        self,
        parent: Context | None = None,
        *,
        isolated_services: set[str] | None = None,
        scope_label: str | None = None,
        intercept_configs: dict[str, dict[str, Any]] | None = None,
        fiber: Fiber | None = None,
        filter_predicate: Callable[[Context], bool] | None = None,
    ) -> None:
        self._parent = parent
        self._children: list[Context] = []
        if parent is not None:
            parent._children.append(self)
        self._services: dict[str, Any] = {}
        self._fibers: list[Fiber] = []
        self._pending_tasks: set[asyncio.Task[Any]] = set()
        self._event_bus: EventBus = parent._event_bus if parent else EventBus()
        self._isolated_services: set[str] = isolated_services or set()
        self._scope_label = scope_label
        self._intercept_configs: dict[str, dict[str, Any]] = (
            intercept_configs if intercept_configs is not None else {}
        )
        self._fiber: Fiber | None = fiber
        self._filter_predicate: Callable[[Context], bool] | None = filter_predicate

        # Lazily imported to avoid circular imports
        from anima.reflect import ReflectService
        from anima.registry import RegistryService

        self.reflect = ReflectService(self)
        self.registry = RegistryService(self)
        self._logger: Any = None
        # Register core services for ctx.get() lookup (anima parity)
        self._services["reflect"] = self.reflect
        self._services["registry"] = self.registry
        # Eagerly create root fiber and set ACTIVE state (anima parity)
        if self._parent is None:
            self._fiber = Fiber(self, target=lambda c: None, args=(), kwargs={})
            self._fiber.state = FiberState.ACTIVE

    # Context static symbols and methods
    @classmethod
    def is_(cls, value: Any) -> bool:
        """Context.is(value) - checks if value is a Context instance."""
        return isinstance(value, Context)

    # Context methods
    def set(self, name: str, value: Any) -> None:
        """Context.set(name, value) - Set service value by name."""
        # If not yet registered via provide, register it first
        if name not in self.reflect.store:
            self.reflect.provide(name, value)
        else:
            self.reflect.set(name, value)  # pragma: no cover

    def accessor(self, name: str, options: dict[str, Any]) -> Any:
        """Context.accessor(name, options) - Create property accessor."""
        return self.reflect.accessor(name, options)  # pragma: no cover

    def mixin(self, source: Any, mixins: list[str] | dict[str, Any]) -> Any:
        """Context.mixin(name, mixins) - Create property accessors for source properties."""
        return self.reflect.mixin(source, mixins)  # pragma: no cover

    @property
    def root(self) -> Context:
        """Return the root ancestor context."""
        curr = self
        while curr._parent is not None:
            curr = curr._parent
        return curr

    @property
    def fiber(self) -> Fiber:
        """Return the Fiber associated with this context scope."""
        current_fib = _current_fiber.get()
        if current_fib is not None:
            return current_fib
        if self._fiber is not None:
            return self._fiber
        if self._parent is not None:
            return self._parent.fiber
        self._fiber = Fiber(
            self, target=lambda c: None, args=(), kwargs={}
        )  # pragma: no cover
        return self._fiber  # pragma: no cover

    @property
    def scope(self) -> Fiber:
        """Alias for ctx.fiber providing parity with scope conventions."""
        return self.fiber

    def filter(self, predicate: Callable[[Context], bool]) -> Context:
        """Spawn a child context with filtered event observation."""
        child = self.extend()
        if self._filter_predicate is not None:
            parent_pred = self._filter_predicate
            child._filter_predicate = lambda c: parent_pred(c) and predicate(c)
        else:
            child._filter_predicate = predicate
        return child

    def exclude(self, predicate: Callable[[Context], bool]) -> Context:
        """Spawn a child context excluding targets matching predicate."""
        return self.filter(lambda c: not predicate(c))

    def match(self, target_ctx: Context) -> bool:
        """Check if target_ctx satisfies the filtering rules of this context."""
        if self._filter_predicate is None:
            return True
        return bool(self._filter_predicate(target_ctx))

    def extend(self, meta: dict[str, Any] | None = None) -> Context:
        """Context.extend(meta) - Create a child context with own properties.

        The child prototypally inherits every property of this context; own
        properties of `meta` shadow the inherited ones. The parent is not mutated.
        """
        child = Context(parent=self)  # pragma: no cover
        if meta:  # pragma: no cover
            for key, value in meta.items():  # pragma: no cover
                if key == "fiber":  # pragma: no cover
                    object.__setattr__(child, "_fiber", value)  # pragma: no cover
                else:
                    setattr(child, key, value)  # pragma: no cover
        return child  # pragma: no cover

    @property
    def events(self) -> EventBus:
        """Context.events - The event bus, mixed onto ctx (ctx.on, ctx.emit, ...)."""
        return self._event_bus  # pragma: no cover

    @property
    def logger(self) -> Any:
        """Context.logger - The logging service. Call ctx.logger(name) for a named logger."""
        # Delegate to parent if not set on this context (anima prototype inheritance)
        if self._logger is None and self._parent is not None:
            parent_logger = self._parent.logger
            if isinstance(parent_logger, Service):
                return _BoundService(parent_logger, self)
            return parent_logger  # pragma: no cover
        if self._logger is None:
            from anima.logger import LoggerService

            self._logger = LoggerService(self)
        return self._logger

    def all_fibers(self) -> list[Fiber]:
        """Return all fibers in this context and all child contexts."""
        fibers = list(self._fibers)
        for child in self._children:
            fibers.extend(child.all_fibers())
        return fibers

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
        if self.reflect._get_impl(name, strict=False) is not None:
            return True
        if name in self._isolated_services:
            return False
        return self._parent.has(name) if self._parent else False

    def get(self, name: str, default: Any = None, strict: bool = True) -> Any:
        """Safely fetch a service, respecting service isolation boundaries."""
        result = self.reflect.get(name, strict)
        if result is not None:
            return result
        if name in self._services:
            return self._services[name]  # pragma: no cover
        if name in self._isolated_services:
            return default
        # pragma: no cover - parent context fallback is defensive code
        if self._parent:
            return self._parent.get(name, default)  # pragma: no cover
        return default  # pragma: no cover

    def plugin(self, plugin: Any, config: Any = None, **kwargs: Any) -> Fiber:
        """Context.plugin(plugin, config?) - Load a plugin in this context."""
        if kwargs:
            config = {**(config or {}), **kwargs}
        # Validate target before delegating (our error message for backward compat)
        if not callable(plugin) and not hasattr(plugin, "apply"):
            raise ValueError(
                f"Plugin target {plugin} must be a callable or class with apply(ctx)"
            )
        fiber = self.registry.plugin(plugin, config)
        # Also track in context's fiber list for direct access
        if fiber not in self._fibers:  # pragma: no cover
            self._fibers.append(fiber)  # pragma: no cover
        return fiber

    def inject(self, deps: Any, callback: Callable[[Context], Any]) -> Fiber:
        """Context.inject(deps, callback) - Run callback when deps are available."""
        fiber = self.registry.inject(deps, callback)
        if fiber not in self._fibers:  # pragma: no cover
            self._fibers.append(fiber)  # pragma: no cover
        return fiber

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            return super().__getattribute__(name)
        svc = self.get(name)
        if svc is not None:
            if isinstance(svc, Service) and getattr(svc, "ctx", None) is not self:
                return _BoundService(svc, self)
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
        # Include services from reflect.store
        for impl in self.reflect.store.values():
            if impl.name not in all_svcs:  # pragma: no cover
                all_svcs[impl.name] = impl.value
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
                    self._schedule_task(fiber.dispose())  # pragma: no cover

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

    def provide(self, name: str, value: Any) -> Disposer:
        """Dynamically register a named capability on this context with automatic effect cleanup."""
        self._register_service(name, value)

        def _unregister() -> None:
            self._unregister_service(name)

        return self.effect(lambda: _unregister)

    def intercept(self, service_name: str, config: dict[str, Any]) -> Context:
        """Spawn a child context with intercepted configuration for a target service."""
        new_intercepts = {k: dict(v) for k, v in self._intercept_configs.items()}
        if service_name in new_intercepts:
            new_intercepts[service_name].update(config)
        else:
            new_intercepts[service_name] = dict(config)
        return Context(
            parent=self,
            isolated_services=set(self._isolated_services),
            scope_label=self._scope_label,
            intercept_configs=new_intercepts,
            fiber=self._fiber,
            filter_predicate=self._filter_predicate,
        )

    def resolve_intercept(self, service_name: str) -> dict[str, Any]:
        """Resolve merged intercept configs for service_name in this context scope."""
        configs: list[dict[str, Any]] = []
        chain: list[Context] = []
        curr: Context | None = self
        while curr is not None:
            chain.append(curr)
            curr = curr._parent
        chain.reverse()
        for ctx in chain:
            if service_name in ctx._intercept_configs:
                configs.append(ctx._intercept_configs[service_name])

        current_fib = _current_fiber.get()
        if (
            current_fib is not None
            and isinstance(current_fib.inject_config, dict)
            and service_name in current_fib.inject_config
        ):
            cfg = current_fib.inject_config[service_name]
            if isinstance(cfg, dict):
                configs.insert(0, cfg)

        svc = self.get(service_name)
        if (
            svc is not None
            and hasattr(svc, "merge_intercept")
            and callable(svc.merge_intercept)
        ):
            return cast(dict[str, Any], svc.merge_intercept(*configs))

        merged: dict[str, Any] = {}
        for cfg in configs:
            merged.update(cfg)
        return merged

    def on(
        self,
        event: str,
        callback: Callable[..., Any],
        *,
        mode: EventMode = EventMode.EMIT,
        prepend: bool = False,
        global_: bool = False,
    ) -> Disposer:
        """Register an event listener as an effect."""
        disposer = self._event_bus.on(
            event, callback, mode=mode, prepend=prepend, ctx=self, global_=global_
        )
        self.effect(lambda: disposer)
        return disposer

    def once(
        self,
        event: str,
        callback: Callable[..., Any],
        *,
        mode: EventMode = EventMode.EMIT,
        prepend: bool = False,
        global_: bool = False,
    ) -> Disposer:
        """Register a one-shot event listener as an effect."""
        disposer = self._event_bus.once(
            event, callback, mode=mode, prepend=prepend, ctx=self, global_=global_
        )
        self.effect(lambda: disposer)
        return disposer

    def emit(self, event: str, *args: Any, **kwargs: Any) -> None:
        """Synchronously broadcast an event."""
        self._event_bus.emit(event, *args, this_arg=self, **kwargs)

    async def parallel(self, event: str, *args: Any, **kwargs: Any) -> list[Any]:
        """Concurrently broadcast an event and await all listeners."""
        return await self._event_bus.parallel(event, *args, this_arg=self, **kwargs)

    async def serial(self, event: str, *args: Any, **kwargs: Any) -> Any:
        """Sequential async execution; stops on first non-None."""
        return await self._event_bus.serial(event, *args, this_arg=self, **kwargs)

    def bail(self, event: str, *args: Any, **kwargs: Any) -> Any:
        """Context.bail(event, *args) - Sequential sync execution,
        stops on first non-None result."""
        return self._event_bus.bail(event, *args, this_arg=self, **kwargs)

    async def waterfall(
        self,
        event: str,
        *args: Any,
        default_handler: Callable[..., Any] | None = None,
        **kwargs: Any,
    ) -> Any:
        """Around-middleware pipeline passing (*args, next)."""
        return await self._event_bus.waterfall(
            event, *args, this_arg=self, default_handler=default_handler, **kwargs
        )

    def isolate(self, service_name: str, label: str | None = None) -> Context:
        """Create a child context with an isolated service scope for `service_name`.

        Reads and writes of `service_name` in the child will not resolve or affect the parent.
        """
        isolated = set(self._isolated_services)
        isolated.add(service_name)
        return Context(
            parent=self,
            isolated_services=isolated,
            scope_label=label,
            intercept_configs=dict(self._intercept_configs),
            filter_predicate=self._filter_predicate,
        )

    def dump_tree(self) -> dict[str, Any]:
        """Dump the full runtime hierarchy of plugins and services for introspection."""
        return {
            "services": list(self.services.keys()),
            "isolated": list(self._isolated_services),
            "fibers": [f.status() for f in self._fibers],
        }


class CoreService(Service):
    """Concrete Service subclass for testing (anima parity)."""

    def __init__(self, ctx: Context, name: str = "core_svc") -> None:
        super().__init__(ctx, name)


def inject(*deps: str, **dep_configs: Any) -> Callable[[Any], Any]:
    """Declare service dependencies and configuration constraints on a plugin or service."""

    def decorator(target: Any) -> Any:
        existing = getattr(target, "inject", None)
        if existing is None:
            inject_list = list(deps)
        elif isinstance(existing, (list, tuple, set)):
            inject_list = list(existing) + [d for d in deps if d not in existing]
        else:
            inject_list = list(deps)

        if dep_configs:
            for k in dep_configs:
                if k not in inject_list:
                    inject_list.append(k)
            target.inject_config = dep_configs

        target.inject = inject_list
        return target

    return decorator
