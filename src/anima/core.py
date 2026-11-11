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
from collections.abc import (
    AsyncIterable,
    Awaitable,
    Callable,
    Coroutine,
    Iterable,
    Iterator,
)
from contextvars import ContextVar
from enum import Enum
from typing import Any, ClassVar, cast

from anima.events import Disposer, EventBus, EventMode

logger = logging.getLogger("anima")

# Anima symbols system — Symbol.for() based unique symbols for internal protocol
symbols = {
    # internal symbols
    "shadow": "anima.shadow",
    "receiver": "anima.receiver",
    "original": "anima.original",
    "initHooks": "anima.initHooks",
    "checkProto": "anima.checkProto",
    # context symbols
    "effect": "anima.effect",
    "filter": "anima.filter",
    "isolate": "anima.isolate",
    "intercept": "anima.intercept",
    # service symbols
    "init": "anima.init",
    "check": "anima.check",
    "config": "anima.config",
    "invoke": "anima.invoke",
    "metadata": "anima.metadata",
    "extend": "anima.extend",
    "tracker": "anima.tracker",
    "resolveConfig": "anima.resolveConfig",
    # anima-only intercept-chain machinery (no upstream counterpart)
    "mergeIntercept": "anima.mergeIntercept",
    "resolveIntercept": "anima.resolveIntercept",
}

# Anima context brand for Context.is() checks (symbol-based brand)
_CONTEXT_BRAND = "_anima_context_brand"

_current_fiber: ContextVar[Fiber | None] = ContextVar("_current_fiber", default=None)
_access_context: ContextVar[Context | None] = ContextVar(
    "_access_context", default=None
)


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
        *,
        root: bool = False,
    ) -> None:
        self.ctx = ctx
        self.target = target
        self.args = args
        self.raw_config: dict[str, Any] = dict(kwargs)
        self.config: Any = dict(kwargs)
        self.parent_fiber = parent_fiber
        self.child_fibers: list[Fiber] = []
        self.effects: list[Disposer] = []
        self._is_root = root
        self.uid: int | None
        self.state: FiberState
        self._runner_epoch: str
        if root:
            # The root fiber is uid 0 and starts ACTIVE with an empty epoch.
            self.uid = 0
            self.state = FiberState.ACTIVE
            self._runner_epoch = ""
        else:
            # Uids come from the owning registry's counter, so the first
            # plugin fiber on a context is 1 and the root never consumes one.
            self.uid = ctx.registry.counter
            self.state = FiberState.PENDING
            self._runner_epoch = "__INACTIVE__"
        self.error: Exception | None = None
        self.name: str = getattr(
            target, "name", getattr(target, "__name__", str(target))
        )
        # Service impl snapshot (Dict<Impl>)
        self._store: dict[str, Any] = {}
        # Snapshot of _store taken when a reload starts; None while unloaded.
        self.store: dict[str, Any] | None = None
        # In-flight load/unload transition (asyncio.Future or None)
        self.inertia: asyncio.Future[None] | None = None
        # The plugin instance produced by the last successful load.
        self._instance: Any = None
        # True once a reload has started for this fiber; await_settle()
        # drives a load only for a fiber that never attempted one, so a
        # fiber that deliberately settled to PENDING is not reloaded.
        self._load_attempted: bool = False

        # Read inject dependencies. Validation is lazy: it runs in _reload
        # at activation time, so a bad config leaves the fiber FAILED but
        # recoverable via update().
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

    def _validate_config_value(self, config: Any) -> Any:
        """Validate a config value against the plugin's declared schema.

        Synchronous validation used by _resolve_config at activation time.
        Returns the validated config; raises on failure.
        """
        config_schema = getattr(self.target, "Config", None)
        if config_schema is None:
            config_schema = getattr(self.target, "schema", None)
        if config_schema is None:
            return config

        from anima.schema import Schema

        if isinstance(config_schema, Schema):
            return config_schema.validate(config)
        if callable(config_schema):
            if isinstance(config, dict):
                return config_schema(**config)
            return config_schema(config)
        return config

    def check_dependencies(self) -> bool:
        """Check if all required services exist on context."""
        return all(self.ctx.has(service) for service in self.inject)

    async def load(self) -> Any:
        """Mount and activate the plugin, joining any in-flight transition.

        Returns the plugin instance when ACTIVE, else None. A plugin
        failure does not raise here; it is stored on the fiber and
        rethrown by await_settle().
        """
        if self.state == FiberState.DISPOSED:
            return None
        if self.state == FiberState.ACTIVE:
            return self._instance
        if self.state == FiberState.FAILED:
            return None
        if self.inertia is not None:
            await self.inertia
            return self._instance if self.state == FiberState.ACTIVE else None
        for name in self.inject:
            self._checkImpl(name)
        self._refresh()
        if self.inertia is None and self._getCurrentEpoch() != "__INACTIVE__":
            # The epoch went active while no loop was running, so no
            # transition started; start the reload now that a loop exists.
            self._updateState(
                lambda: self._begin_transition(self._getCurrentEpoch(), "__INACTIVE__")
            )
        if self.inertia is not None:
            await self.inertia
        return self._instance if self.state == FiberState.ACTIVE else None

    async def _unload(self) -> None:
        """Unwind child fibers and effect disposers in reverse order (LIFO)."""
        # Dispose child fibers first
        for child in reversed(list(self.child_fibers)):
            await child.dispose()
        self.child_fibers.clear()

        # Run registered effect disposers in reverse order (LIFO)
        for disposer in reversed(list(self.effects)):
            try:
                res = disposer()
                if inspect.isawaitable(res):
                    await res
            except Exception:
                logger.exception(
                    "Error during fiber %s (uid %s) effect unload", self.uid, self.name
                )

        self.effects.clear()
        self.store = None
        self._updateState(self._unload_complete)

    async def dispose(self) -> None:
        """Unload the plugin and mark it permanently as DISPOSED.

        Disposing the root fiber restarts it instead. A dispose issued
        while a load is in flight joins that transition; the fiber is
        never resurrected afterwards.
        """
        if self._is_root:
            await self.restart()
            return
        if self.uid is None:
            return
        if self.parent_fiber is not None and self in self.parent_fiber.child_fibers:
            self.parent_fiber.child_fibers.remove(self)
        runtime = self.ctx.registry.get(self.target)
        if runtime is not None:
            if self in runtime.fibers:
                runtime.fibers.remove(self)
            if not runtime.fibers:
                self.ctx.registry._unregister(runtime)
        self.uid = None
        self.ctx.emit("internal/plugin", self)
        self._setEpoch("__INACTIVE__")
        if self.inertia is None:
            # Epoch was already INACTIVE (e.g. a PENDING fiber), so no
            # transition started; begin the unload directly.
            self._updateState(self._begin_unload)
        while self.inertia is not None:
            await self.inertia

    async def restart(self) -> None:
        """Unload and re-mount the plugin via the epoch machine."""
        self.assertActive()
        for name in self.inject:
            self._checkImpl(name)
        self._setEpoch("__INACTIVE__")
        self._refresh()
        await self.await_settle()
        if self.state != self._getState():
            self._updateState(lambda: None)

    async def _reload(self, epoch: str) -> None:
        """Execute the plugin body directly, with a staleness checkpoint.

        `epoch` is the epoch this reload was started for, captured when the
        transition was decided; the checkpoint compares it against the
        current epoch so a reload superseded before it ran never executes
        the plugin body. A plugin failure is logged and stored on the
        fiber, not raised; await_settle() rethrows it.
        """
        self.store = dict(self._store)
        old_epoch = epoch
        self._load_attempted = True
        try:
            await asyncio.sleep(0)
            if self._getCurrentEpoch() == old_epoch:
                self.config = await self._resolve_config(self.raw_config)
                await self._execute(old_epoch)
                self.error = None
        except Exception as e:
            logger.exception(
                "Plugin %s (uid %s) failed during reload", self.name, self.uid
            )
            self.error = e
            self._setCurrentEpoch("__INACTIVE__")
        self._updateState(lambda: self._reload_complete(old_epoch))

    async def _resolve_config(self, config: Any) -> Any:
        """Resolve plugin config through the internal/config waterfall.

        Runs the waterfall hook, then applies schema validation if
        declared. Validation is synchronous; the waterfall stays async.
        """

        async def _default(cfg: Any) -> Any:
            return cfg

        resolved = await self.ctx.waterfall(
            "internal/config", config, default_handler=_default
        )
        return self._validate_config_value(resolved)

    @staticmethod
    def _safe_getattr(obj: Any, name: str) -> Any:
        """getattr that returns None instead of raising.

        A Context instance raises AnimaError (not AttributeError) for
        unknown names, so a plain getattr default does not apply.
        """
        try:
            return getattr(obj, name)
        except Exception:  # noqa: BLE001 - probe only; any failure means absent
            return None

    @staticmethod
    def _safe_getattr_static(obj: Any, name: str) -> Any:
        """getattr_static that returns None instead of raising.

        Unlike getattr, this does not bind functions stored as class
        attributes, so a single initHooks function stays a plain callable
        instead of arriving as a bound method expecting self.
        """
        try:
            return inspect.getattr_static(obj, name)
        except Exception:  # noqa: BLE001 - probe only; any failure means absent
            return None

    def _call_with_config(self, fn: Callable[..., Any], config: Any) -> Any:
        """Call a plugin entry point with as many leading args as it accepts.

        The entry point is invoked with (ctx, config); shorter signatures
        receive fewer arguments, matching the JS call convention where
        extra arguments are dropped.
        """
        try:
            sig = inspect.signature(fn)
        except (TypeError, ValueError):
            return fn(self.ctx, config)
        params = list(sig.parameters.values())
        positional = [
            p
            for p in params
            if p.kind
            in (
                inspect.Parameter.POSITIONAL_ONLY,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
            )
        ]
        if (
            len(positional) >= 2
            and inspect.isclass(fn)
            and issubclass(fn, Service)
            and positional[1].name != "config"
        ):
            # Fail loud instead of misrouting the config dict into a
            # non-config second parameter (a Service subclass mounted
            # here must declare (ctx, config)).
            raise TypeError(
                f"Service {fn.__name__} declares __init__ with a "
                f"non-config second parameter '{positional[1].name}'; "
                "services mounted here must declare (ctx, config)"
            )
        has_varargs = any(p.kind == inspect.Parameter.VAR_POSITIONAL for p in params)
        if has_varargs or len(positional) >= 2:
            return fn(self.ctx, config)
        if len(positional) == 1:
            return fn(self.ctx)
        return fn()

    async def _execute(self, epoch: str) -> None:
        """Run the plugin body and collect its effect."""
        token = _current_fiber.set(self)
        try:
            target = self.target
            config = self.config

            # 1. Target is a class: construct, then run init hooks and init.
            # Class plugins never call apply().
            if inspect.isclass(target):
                target_cls = cast(type[Any], target)
                instance = self._call_with_config(target_cls, config)
                hooks = self._safe_getattr_static(instance, symbols["initHooks"])
                if hooks is not None:
                    if not isinstance(hooks, (list, tuple)):
                        hooks = [hooks]
                    for hook in hooks:
                        res = hook()
                        if inspect.isawaitable(res):
                            await res
                init_fn = self._safe_getattr(instance, symbols["init"])
                if init_fn is None:
                    init_fn = self._safe_getattr(instance, "init")
                result = None
                if init_fn is not None and callable(init_fn):
                    result = init_fn()
                    if inspect.isawaitable(result):
                        result = await result
                self._instance = instance
                await self._collect_effect(result, epoch)
            # 2. Target is an object with an apply method
            elif hasattr(target, "apply") and callable(target.apply):
                result = self._call_with_config(target.apply, config)
                if inspect.isawaitable(result):
                    result = await result
                self._instance = target
                await self._collect_effect(result, epoch)
            # 3. Target is a callable / function plugin
            elif callable(target):
                result = self._call_with_config(target, config)
                if inspect.isawaitable(result):
                    result = await result
                self._instance = result
                await self._collect_effect(result, epoch)
            else:
                raise ValueError(
                    f"Plugin target {target} must be a callable or class with apply(ctx)"
                )
        finally:
            _current_fiber.reset(token)

    async def _collect_effect(self, result: Any, epoch: str) -> None:
        """Collect the plugin body's return value as an effect disposer."""
        if result is None:
            return
        if callable(result):
            self.effects.append(result)
            return
        if inspect.isawaitable(result):
            await self._collect_effect(await result, epoch)
            return
        if isinstance(result, AsyncIterable):
            async for item in result:
                if self._getCurrentEpoch() != epoch:
                    return
                self._collect_item(item)
            return
        if isinstance(result, Iterable):
            for item in result:
                self._collect_item(item)
            return
        raise TypeError("Invalid effect")

    def _collect_item(self, item: Any) -> None:
        """Collect one item of an iterable effect."""
        if item is None:
            return
        if callable(item):
            self.effects.append(item)
            return
        raise TypeError("Invalid effect")

    async def replace(self, new_target: Any) -> None:
        """Replace plugin target and reload fiber with updated target."""
        if self.state in (FiberState.UNLOADING, FiberState.DISPOSED):
            return

        self.target = new_target
        self.name = getattr(
            new_target, "name", getattr(new_target, "__name__", str(new_target))
        )

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
        """Update plugin configuration with live lifecycle hooks.

        Raises on a disposed fiber, and raises the validation error when
        the new config fails validation on an ACTIVE fiber (the fiber
        keeps its old config). On a non-ACTIVE fiber resolution is
        deferred: the error is cleared and the epoch machine drives a
        fresh activation, which validates then.
        """
        self.assertActive()
        self.raw_config = dict(config)

        if self.state != FiberState.ACTIVE:
            self.error = None
            self._setEpoch("__INACTIVE__")
            self._refresh()
            return

        resolved = await self._resolve_config(config)
        if not no_save:
            self.ctx.emit("internal/commit", self, resolved)

        async def _default(cfg: Any) -> None:
            self.config = cfg
            self.error = None
            await self.restart()

        await self.ctx.waterfall(
            "internal/update",
            resolved,
            default_handler=_default,
        )

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

    async def await_settle(self) -> Fiber:
        """Wait for current lifecycle work and rethrow startup errors.

        Fiber.await() parity. Awaits any in-flight inertia (load/unload)
        transition, then rethrows any startup error stored on the fiber.
        A PENDING fiber with no transition in flight drives its own load
        first, so awaiting a fiber whose load was only scheduled still
        settles it; a fiber that already completed a load cycle is left
        alone, so awaiting a fiber that deliberately settled to PENDING
        does not reload it. Returns the fiber so `await fiber` yields the
        fiber itself.
        """
        if (
            self.state == FiberState.PENDING
            and self.inertia is None
            and not self._load_attempted
        ):
            await self.load()
        while self.inertia is not None:
            await self.inertia
        if self.error is not None:
            raise self.error
        return self

    async def await_(self) -> Fiber:
        """Alias for await_settle() matching Fiber.await() parity."""
        return await self.await_settle()

    def __await__(self) -> Iterator[Any]:
        """Make the fiber awaitable: awaiting it settles the lifecycle."""
        return self.await_settle().__await__()

    def _checkImpl(self, name: str) -> None:
        """Check if a required service implementation is available for this fiber.

        Fiber._checkImpl() parity. Removes the impl from _store if the
        service is no longer available or its check predicate fails.
        Services registered directly on the context (Context.provide)
        live outside the reflect store; they are represented by a
        synthetic impl owned by the context's fiber.
        """
        impl = self.ctx.reflect._get_impl(name, strict=True)
        if impl is None:
            if self.ctx.has(name):
                from anima.reflect import Impl

                self._store[name] = Impl(
                    uid=0,
                    name=name,
                    value=self.ctx.get(name, strict=False),
                    ctx=self.ctx,
                    check=None,
                    isolate=name,
                    fiber=self.ctx._fiber,
                )
                return
            self._store.pop(name, None)
            return
        try:
            if impl.check is not None and not impl.check():
                self._store.pop(name, None)
                return
        except Exception as e:  # noqa: BLE001 - defensive: check predicate may raise
            self.ctx.logger.error("check failed for %s: %s", name, e)
            self._store.pop(name, None)
            return
        self._store[name] = impl

    def _refresh(self) -> None:
        """Recalculate the fiber's epoch from its inject dependencies.

        Fiber._refresh() parity. Builds an epoch string from the uids
        of the fibers owning the required service implementations;
        sets INACTIVE if any dep is missing.
        """
        epoch = ""
        for name in self.inject:
            impl = self._store.get(name)
            if impl is None:
                epoch = "__INACTIVE__"
                break
            provider = impl.fiber
            epoch += ":" + str(provider.uid if provider is not None else impl.uid)
        self._setEpoch(epoch)

    def _setEpoch(self, epoch: str) -> None:
        """Store the fiber's epoch and drive the matching transition.

        Fiber._setEpoch() parity. When the epoch changes from INACTIVE
        to active, starts _reload(); otherwise starts _unload(). A
        transition already in flight leaves the stored epoch for the
        in-flight transition to chain into.
        """
        old_epoch = self._getCurrentEpoch()
        if epoch == old_epoch:
            return
        self._setCurrentEpoch(epoch)
        if self.inertia is not None:
            return
        self._updateState(lambda: self._begin_transition(epoch, old_epoch))

    def _try_start(
        self, coro_fn: Callable[[], Coroutine[Any, Any, None]]
    ) -> asyncio.Task[None] | None:
        """Start a lifecycle transition task, or None with no loop running.

        The task is registered on the context's pending set so flush()
        waits for notify-driven transitions too, not just scheduled loads.
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return None
        task = loop.create_task(coro_fn())
        self.inertia = task
        self.ctx._pending_tasks.add(task)
        task.add_done_callback(self.ctx._pending_tasks.discard)
        return task

    def _begin_transition(self, epoch: str, old_epoch: str) -> FiberState:
        """Start the transition for an epoch change; return the target state."""
        if epoch != "__INACTIVE__" and old_epoch == "__INACTIVE__":
            if self._try_start(lambda: self._reload(epoch)) is not None:
                return FiberState.LOADING
            return self.state
        return self._begin_unload()

    def _begin_unload(self) -> FiberState:
        """Start the unload transition; return the target state."""
        if self._try_start(self._unload) is not None:
            return FiberState.UNLOADING
        return self.state

    def _reload_complete(self, old_epoch: str) -> FiberState | None:
        """Finish a reload: clear inertia, or chain into an unload if the epoch moved."""
        if self._getCurrentEpoch() == old_epoch:
            self.inertia = None
            return None
        return self._begin_unload()

    def _unload_complete(self) -> FiberState | None:
        """Finish an unload: clear inertia, or chain into a reload if the epoch is active."""
        if self._getCurrentEpoch() == "__INACTIVE__":
            self.inertia = None
            return None
        if self._try_start(lambda: self._reload(self._getCurrentEpoch())) is not None:
            return FiberState.LOADING
        return self.state

    def _getCurrentEpoch(self) -> str:
        """Get the current epoch string from the runner."""
        return self._runner_epoch

    def _setCurrentEpoch(self, epoch: str) -> None:
        """Set the current epoch string on the runner."""
        self._runner_epoch = epoch

    def _updateState(self, callback: Any) -> None:
        """Update fiber state and notify dependents of state changes.

        Fiber._updateState() parity. Emits internal/status with the
        fiber and the old state, and on ACTIVE<->NON-ACTIVE transitions
        notifies reflect of service changes.
        """
        old_state = self.state
        new_state = callback()
        if new_state is None:
            new_state = self._getState()
        self.state = new_state
        if old_state == new_state:
            return
        self.ctx.emit("internal/status", self, old_state)
        # Only notify changes between ACTIVE and NON-ACTIVE states
        if old_state != FiberState.ACTIVE and new_state != FiberState.ACTIVE:
            return
        for impl in self.ctx.reflect.store.values():
            if impl.fiber is not self:
                continue
            self.ctx.reflect.notify([impl.name])

    def _getState(self) -> FiberState:
        """Determine current fiber state from uid, error, and runner epoch."""
        if self.uid is None:
            return FiberState.DISPOSED
        if self.error is not None:
            return FiberState.FAILED
        if self._getCurrentEpoch() != "__INACTIVE__":
            return FiberState.ACTIVE
        return FiberState.PENDING

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

    @staticmethod
    def _is_async_call(svc: Service) -> bool:
        """Whether calling ``svc`` produces an awaitable result.

        ``Service.__call__`` itself is a plain function, so checking it
        always answers False. The async-ness lives in the resolved
        symbol-keyed invoke, or in a subclass ``__call__`` override.
        """
        invoke = getattr(svc, symbols["invoke"], None)
        if invoke is not None:
            return bool(inspect.iscoroutinefunction(invoke))
        call = type(svc).__call__
        return call is not Service.__call__ and bool(inspect.iscoroutinefunction(call))

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        svc = object.__getattribute__(self, "_svc")
        ctx = object.__getattribute__(self, "_ctx")
        if self._is_async_call(svc):

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


class _ServiceMeta(type):
    """Metaclass for Service: proxy-aware isinstance checks.

    Mirrors the upstream Symbol.hasInstance walk: the check follows
    the constructor chain, unwrapping bound-service facades so a
    service accessed through another context still checks as its own
    class. Defined on the metaclass because isinstance() only consults
    __instancecheck__ there, never on the class itself.
    """

    def __instancecheck__(cls, instance: Any) -> bool:
        if not instance:
            return False
        target = instance
        if isinstance(target, _BoundService):
            target = object.__getattribute__(target, "_svc")
        return any(base is cls for base in type(target).__mro__)


class Service(metaclass=_ServiceMeta):
    """Base class for named capabilities mounted onto Context."""

    # Symbol-based protocol (anima parity)
    __symbols__: ClassVar[dict[str, str]] = {
        "init": symbols["init"],
        "check": symbols["check"],
        "config": symbols["config"],
        "invoke": symbols["invoke"],
        "metadata": symbols["metadata"],
        "extend": symbols["extend"],
        "tracker": symbols["tracker"],
        "resolveConfig": symbols["resolveConfig"],
        "mergeIntercept": symbols["mergeIntercept"],
        "resolveIntercept": symbols["resolveIntercept"],
    }

    def __init__(self, ctx: Context, name: str | None = None) -> None:
        """Register this service under ``name``.

        ``name`` falls back to the ``provide`` class attribute when
        omitted (``name ??= constructor['provide']``). A name must be
        a string; anything else fails loud instead of corrupting the
        registry with an unhashable key.
        """
        if name is None:
            name = getattr(type(self), "provide", None)
        if name is None:
            raise TypeError(
                f"{type(self).__name__} requires a service name or a "
                "'provide' class attribute"
            )
        if not isinstance(name, str):
            raise TypeError(f"Service name must be a string, got {type(name).__name__}")
        self.ctx = ctx
        self.name = name
        # register via reflect.provide with check symbol
        check = getattr(type(self), "check", None)
        ctx.reflect.provide(name, self, check)

        # Register unregister disposer effect
        def _unregister() -> None:
            ctx._unregister_service(name)

        ctx.effect(lambda: _unregister)

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        """[symbols.invoke] - make service instances callable.

        Dispatches to the callable stored under the declared symbol
        key (``symbols["invoke"]``); a plain-named ``invoke``
        attribute is not consulted.
        """
        invoke = getattr(self, symbols["invoke"], None)
        if invoke is not None and callable(invoke):
            return invoke(*args, **kwargs)
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

    def resolve_config(
        self,
        base: dict[str, Any] | None = None,
        head: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """[symbols.resolveConfig] - resolve intercept config chain.

        Walks up the context's intercept config chain (prototype chain in
        parent chain here), collecting configs for this service name
        at each level. Base is prepended, head is appended. If the service
        declares Config.merge, uses that; otherwise shallow merge.
        """
        configs: list[dict[str, Any]] = []
        ctx: Context | None = self.ctx
        while ctx is not None:
            if self.name in ctx._intercept_configs:
                configs.insert(0, ctx._intercept_configs[self.name])
            ctx = ctx._parent
        if base:
            configs.insert(0, base)
        if head:
            configs.append(head)
        config_cls = getattr(type(self), "Config", None)
        if config_cls is not None and hasattr(config_cls, "merge"):
            return cast(dict[str, Any], config_cls.merge(*configs))
        merged: dict[str, Any] = {}
        for cfg in configs:
            merged.update(cfg)
        return merged

    def _filter(self, ctx: Context) -> bool:
        """[symbols.filter] - check if ctx's isolate label matches this service's."""
        my_label = self.ctx._isolated_services.get(self.name)
        their_label = ctx._isolated_services.get(self.name)
        return my_label == their_label

    def _extend(self, props: dict[str, Any] | None = None) -> Any:
        """[symbols.extend] - create extended self with props.

        Python has no callable-vs-plain instance distinction: every
        Service defines __call__, so the extension is always an
        instance of the same class and callability follows the
        symbol-keyed invoke either way.
        """
        obj = object.__new__(self.__class__)
        obj.__dict__.update(self.__dict__)
        obj.__dict__.update(props or {})
        return obj


class Context:
    """The central Anima Context repository of services, events, effects, and scopes."""

    # Context static context symbols (anima parity).
    # static readonly effect/filter/isolate/intercept: unique symbol
    # These conflict with instance methods of the same name, so we use SYMBOL_
    # prefix. Access via Context.SYMBOL_EFFECT etc. or anima.core.symbols["effect"].
    SYMBOL_EFFECT: ClassVar[str] = symbols["effect"]
    SYMBOL_FILTER: ClassVar[str] = symbols["filter"]
    SYMBOL_ISOLATE: ClassVar[str] = symbols["isolate"]
    SYMBOL_INTERCEPT: ClassVar[str] = symbols["intercept"]

    # Context symbols dict (anima parity).
    # Anima uses Context[symbols.isolate] etc. as keys. Access via
    # Context.symbols["effect"] etc., equivalent to Context.SYMBOL_EFFECT.
    symbols: ClassVar[dict[str, str]] = {
        "effect": symbols["effect"],
        "filter": symbols["filter"],
        "isolate": symbols["isolate"],
        "intercept": symbols["intercept"],
        "init": symbols["init"],
        "check": symbols["check"],
        "config": symbols["config"],
        "invoke": symbols["invoke"],
        "metadata": symbols["metadata"],
        "extend": symbols["extend"],
        "tracker": symbols["tracker"],
        "resolveConfig": symbols["resolveConfig"],
        "mergeIntercept": symbols["mergeIntercept"],
        "resolveIntercept": symbols["resolveIntercept"],
    }

    def __init__(
        self,
        parent: Context | None = None,
        *,
        isolated_services: dict[str, Any] | None = None,
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
        # isolation map service name -> scope label (Dict<symbol>)
        self._isolated_services: dict[str, Any] = isolated_services or {}
        self._scope_label = scope_label
        # intercept map service name -> config (Dict)
        self._intercept_configs: dict[str, dict[str, Any]] = (
            intercept_configs if intercept_configs is not None else {}
        )
        self._fiber: Fiber | None = fiber
        self._filter_predicate: Callable[[Context], bool] | None = filter_predicate
        # Anima context brand for Context.is() checks
        object.__setattr__(self, _CONTEXT_BRAND, True)
        # baseUrl used to resolve relative plugin/module specifiers
        self.baseUrl: str | None = None

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
            self._fiber = Fiber(
                self, target=lambda c: None, args=(), kwargs={}, root=True
            )

    # Context static symbols and methods
    @classmethod
    def is_(cls, value: Any) -> bool:
        """Context.is(value) - symbol-based brand check.

        !!value?.[Context.is as any]
        Works across realms because the brand is keyed by a unique symbol/string.
        """
        return bool(getattr(value, _CONTEXT_BRAND, False))

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
            self, target=lambda c: None, args=(), kwargs={}, root=True
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
        child = Context(
            parent=self,
            isolated_services=dict(self._isolated_services),
            intercept_configs=None,
            filter_predicate=self._filter_predicate,
        )
        if meta:  # pragma: no cover
            for key, value in meta.items():  # pragma: no cover
                if key == "fiber":  # pragma: no cover
                    object.__setattr__(child, "_fiber", value)  # pragma: no cover
                else:
                    setattr(child, key, value)  # pragma: no cover
        return child

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
        """Context get trap - resolve service property via reflect handler."""
        if name.startswith("_"):
            return super().__getattribute__(name)
        # ProxyHandler.get trap
        try:
            reflect = object.__getattribute__(self, "reflect")
        except AttributeError:  # pragma: no cover - reflect is always set
            # reflect is always set during Context.__init__; this is defensive
            reflect = None  # pragma: no cover
        if reflect is not None:  # pragma: no branch - reflect is always set
            try:
                return reflect.handler_get(self, name, self)
            except AnimaError as e:
                if "without inject" in str(e):
                    raise AttributeError(
                        f"Service '{name}' is not registered on Context. "
                        f"Available services: {list(self.services.keys())}"
                    ) from e
                raise  # pragma: no cover - AnimaError without "without inject"
        raise AttributeError(  # pragma: no cover - reflect is always set in __init__
            f"Service '{name}' is not registered on Context. "
            f"Available services: {list(self.services.keys())}"
        )

    def __setattr__(self, name: str, value: Any) -> None:
        """Context set trap - route service writes through reflect handler."""
        if name.startswith("_"):
            object.__setattr__(self, name, value)
            return
        # ProxyHandler.set trap
        try:
            reflect = object.__getattribute__(self, "reflect")
        except AttributeError:  # pragma: no cover - reflect is always set
            reflect = None  # pragma: no cover
        if reflect is not None:  # pragma: no branch - reflect is always set
            reflect.handler_set(self, name, value, self)
            return  # set was handled by handler
        # Default: direct instance attribute
        object.__setattr__(self, name, value)

    def __contains__(self, name: str) -> bool:
        """Context has trap - check service registration via reflect handler."""
        if name.startswith("_"):
            return hasattr(self, name)
        if name in self._services:
            return True
        try:
            reflect = object.__getattribute__(self, "reflect")
        except AttributeError:  # pragma: no cover - reflect is always set
            reflect = None  # pragma: no cover
        if reflect is not None and reflect.handler_has(
            self, name
        ):  # pragma: no branch - reflect is always set
            return True
        return self._parent.__contains__(name) if self._parent else False

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
        new_intercepts: dict[str, dict[str, Any]] = {}
        new_intercepts[service_name] = dict(config)
        return Context(
            parent=self,
            isolated_services=dict(self._isolated_services),
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

    def isolate(self, service_name: str, label: Any | None = None) -> Context:
        """Create a child context with an isolated service scope for `service_name`.

        shadow = Object.create(this[symbols.isolate]); shadow[name] = label ?? Symbol(name).
        Below the returned context, reads and writes of `service_name`
        resolve against the new label instead of the parent's.
        Passing the same `label` to two `isolate()` calls joins their scopes.
        """
        isolated = dict(self._isolated_services)
        isolated[service_name] = (
            label if label is not None else f"__symbol__{service_name}"
        )
        return Context(
            parent=self,
            isolated_services=isolated,
            scope_label=label,
            intercept_configs=None,
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

    def __init__(self, ctx: Context, config: dict[str, Any] | None = None) -> None:
        cfg = dict(config or {})
        super().__init__(ctx, cfg.get("name", "core_svc"))


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
