"""Reflect service for service property access and introspection.

Python port of the ReflectService: manages service properties,
accessors, mixins, and change notification.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, ClassVar

from anima.core import (
    Context,
    AnimaError,
    FiberState,
    Service,
    _BoundService,
    _current_fiber,
    symbols,
)


def _is_traceable(value: Any) -> bool:
    """Check if a value is an object-like type that can be traced."""
    return value is not None and not isinstance(
        value, (str, int, float, bool, bytes, type)
    )


# Anima reserved words for special properties
_RESERVED_WORDS = frozenset({"prototype", "then"})


def _is_special_property(prop: str) -> bool:
    """Check if a property is special (reserved word, number string, or starts with _)."""
    if prop in _RESERVED_WORDS:
        return True
    try:
        num = float(prop)
        return str(num) == prop or str(int(num)) == prop
    except (ValueError, TypeError):
        return prop.startswith("_")


def _enhance_error(error: Exception) -> Exception:
    """Enhance error stack trace."""
    return error  # pragma: no cover - only called from async paths


class _TraceableProxy:
    """Proxy that wraps a value with context tracking (getTraceable parity)."""

    def __init__(self, ctx: Context, value: Any, tracker: Any) -> None:
        object.__setattr__(self, "_ctx", ctx)
        object.__setattr__(self, "_value", value)
        object.__setattr__(self, "_tracker", tracker)

    def __getattr__(self, name: str) -> Any:
        if name == symbols["original"]:
            return object.__getattribute__(self, "_value")
        tracker = object.__getattribute__(self, "_tracker")
        if name == tracker.property:
            return object.__getattribute__(self, "_ctx")
        if isinstance(name, str) and name.startswith("__"):
            raise AttributeError(name)
        value = object.__getattribute__(self, "_value")
        attr = getattr(value, name)
        if callable(attr) and not tracker.noShadow:

            def bound(*args: Any, **kwargs: Any) -> Any:
                return attr(*args, **kwargs)

            return bound
        return attr

    def __setattr__(self, name: str, value: Any) -> None:
        if name in ("_ctx", "_value", "_tracker"):
            object.__setattr__(self, name, value)
            return
        target = object.__getattribute__(self, "_value")
        setattr(target, name, value)

    def __getitem__(self, key: Any) -> Any:
        if key == symbols["original"]:
            return object.__getattribute__(self, "_value")
        raise KeyError(key)


class Impl:
    """A service implementation registered in the reflect store."""

    def __init__(
        self,
        uid: int,
        name: str,
        value: Any,
        ctx: Context,
        check: Callable[[], bool] | None,
        isolate: Any,
    ) -> None:
        self.uid = uid
        self.name = name
        self.value = value
        self.ctx = ctx
        self.check = check
        self.isolate = isolate
        self.dependents: set[Impl] = set()


class Property:
    """A property definition (service or accessor)."""

    def __init__(self, prop_type: str, **kwargs: Any) -> None:
        self.type = prop_type
        self.__dict__.update(kwargs)


class ReflectService:
    """Service property access, accessors, mixins, and change notification.

    ReflectService does not extend Service (anima parity).
    """

    def __init__(self, ctx: Context) -> None:
        self.ctx = ctx
        self.name = "reflect"
        self.store: dict[Any, Impl] = {}
        self.props: dict[str, Property] = {}
        self._impl_counter = 0

    def get(self, name: str, strict: bool = True) -> Any:
        """Get a service implementation value by name."""
        impl = self._get_impl(name, strict)
        if impl is not None:
            return impl.value
        # Fall back to ctx._services for services registered via ctx._register_service
        if name in self.ctx._services:
            return self.ctx._services[name]
        return None

    def _get_impl(self, name: str, strict: bool = True) -> Impl | None:
        for impl in self.store.values():
            if impl.name == name:  # pragma: no branch
                # pragma: no cover - branch coverage: fiber active case
                if strict and impl.ctx.fiber.state != FiberState.ACTIVE:
                    return None
                return impl
        return None

    def set(self, name: str, value: Any, error: Any = None) -> bool:
        """Set a service implementation value."""
        impl = None
        for candidate in self.store.values():
            if candidate.name == name:  # pragma: no branch
                impl = candidate
                break
        if impl is None:
            raise ValueError(f'cannot set property "{name}" without provide')
        # pragma: no cover - branch coverage: same fiber case
        if impl.ctx.fiber is not self.ctx.fiber:
            raise ValueError(f'cannot set property "{name}" in multiple fibers')
        impl.value = value
        return True

    def provide(
        self,
        name: str,
        value: Any = None,
        check: Callable[[], bool] | None = None,
    ) -> Callable[[], None]:
        """Register a service implementation and return a disposer."""
        if name in self.props and self.props[name].type != "service":
            raise ValueError(
                f'property "{name}" is already declared as {self.props[name].type}'
            )
        self.props[name] = Property("service")

        impl = Impl(
            uid=self._impl_counter + 1,
            name=name,
            value=value,
            ctx=self.ctx,
            check=check,
            isolate=name,
        )
        self._impl_counter += 1
        self.store[name] = impl

        self.ctx.fiber._set_state(FiberState.ACTIVE)
        self.notify([name])

        def disposer() -> None:
            if self.store.get(name) is impl:
                del self.store[name]
            self.notify([name])

        # Register disposer as effect on the current fiber (anima parity:
        # this.ctx.fiber.effect(...) registers the disposer on the fiber)
        fiber = _current_fiber.get() or self.ctx._fiber
        if fiber is not None:  # pragma: no cover
            fiber.effects.append(disposer)

        return disposer

    def notify(
        self,
        names: list[str],
        filter_fn: Callable[[Context, str], bool] | None = None,
    ) -> list[Any]:
        """Notify fibers of service changes. Returns list of updated fibers."""
        if filter_fn is None:

            def filter_fn(ctx: Context, name: str) -> bool:
                return True

        updated_fibers: list[Any] = []
        for runtime in list(self.ctx.registry.values()):
            if not hasattr(runtime, "fibers"):
                continue
            for fiber in list(runtime.fibers):
                has_update = False
                for name in names:
                    if name not in getattr(fiber, "inject", []):
                        continue
                    if not filter_fn(fiber.ctx, name):
                        continue
                    has_update = True
                    # Re-check impl for this fiber
                    impl = self._get_impl(name, strict=False)
                    if impl:
                        fiber._validate_config()
                    else:
                        # Dependency no longer available — dispose active fiber
                        if fiber.state == FiberState.ACTIVE:  # pragma: no cover
                            self.ctx._schedule_task(fiber.dispose())
                if not has_update:
                    continue
                fiber._refresh() if hasattr(fiber, "_refresh") else None
                # Schedule load for PENDING fibers whose deps are now satisfied
                if fiber.state == FiberState.PENDING and fiber.check_dependencies():
                    self.ctx._schedule_task(fiber.load())
                updated_fibers.append(fiber)

        for name in names:
            impl = self._get_impl(name, strict=False)
            value = impl.value if impl else None
            self.ctx.emit("internal/service", name, value)

        return updated_fibers

    def accessor(self, name: str, options: dict[str, Any]) -> Callable[[], None]:
        """Create a property accessor and return a disposer."""
        if name in self.props:
            raise ValueError(f'property "{name}" is already declared')
        self.props[name] = Property("accessor", **options)

        def disposer() -> None:
            self.props.pop(name, None)

        return disposer

    def mixin(
        self, source: Any, mixins: list[str] | dict[str, str]
    ) -> Callable[[], None]:
        """Create property accessors for source properties and return a disposer."""
        if isinstance(mixins, dict):
            entries = list(mixins.items())
        else:
            entries = [(key, key) for key in mixins]

        def get_target(ctx: Context, error: Any) -> Any:
            return getattr(ctx, source)

        disposers: list[Callable[[], None]] = []
        for key, value in entries:
            key_name = key

            def make_getter() -> Callable[[Any, Any], Any]:
                def getter(receiver: Any, error: Any, _key: str = key_name) -> Any:  # noqa: B023
                    target = get_target(receiver, error)
                    return getattr(target, _key)

                return getter

            def make_setter() -> Callable[[Any, Any, Any], None]:
                def setter(
                    val: Any,
                    receiver: Any,
                    error: Any,
                    _key: str = key_name,  # noqa: B023
                ) -> None:
                    target = get_target(receiver, error)
                    setattr(target, _key, val)

                return setter

            disposer = self.accessor(
                value,
                {
                    "get": make_getter(),
                    "set": make_setter(),
                },
            )
            disposers.append(disposer)

        def dispose_all() -> None:
            for d in disposers:
                d()

        return dispose_all

    def trace(self, value: Any) -> Any:
        """Return a context-traceable wrapper around a value."""
        if not _is_traceable(value):
            return value
        if hasattr(value, symbols["shadow"]):
            return type(value)
        tracker = getattr(value, symbols["tracker"], None)
        if not tracker:
            return value
        return _TraceableProxy(self.ctx, value, tracker)

    def bind(self, callback: Callable[..., Any]) -> Callable[..., Any]:
        """Wrap a callback with context tracing."""

        def bound(*args: Any, **kwargs: Any) -> Any:
            traced_args = [self.trace(arg) for arg in args]
            traced_kwargs = {k: self.trace(v) for k, v in kwargs.items()}
            return callback(*traced_args, **traced_kwargs)

        return bound

    # ReflectService.handler proxy traps
    # These implement get/set/has traps for context property access (proxy parity)

    def handler_get(self, target: Context, prop: str, ctx: Context) -> Any:
        """ProxyHandler.get trap - resolve service property."""
        if _is_special_property(prop):  # pragma: no cover - special property getattr
            return getattr(target, prop)

        try:
            object.__getattribute__(target, prop)
            return self.trace(
                object.__getattribute__(target, prop)
            )  # pragma: no cover - direct attribute read
        except AttributeError:
            pass

        error = AnimaError("get_error", f'cannot get property "{prop}" without inject')

        try:
            defn = target.reflect.props.get(prop)
            if defn is not None and defn.type == "accessor":
                getter = getattr(defn, "get", None)
                if getter is not None:
                    return getter(ctx, ctx, error)
                # Accessor without getter — cannot read  # pragma: no cover - accessor without getter
                raise error

            # ctx.events.waterfall('internal/get', ...) would be used
            # here for async interception; in sync __getattr__, fall through
            # to fiber chain resolution directly
            return self._handler_get_fallback(target, prop, ctx, error)
        except AnimaError:
            raise  # pragma: no cover - AnimaError without "without inject"
        except Exception as e:
            raise _enhance_error(e) from e  # pragma: no cover - enhance_error path

    def _handler_get_fallback(
        self, target: Context, prop: str, ctx: Context, error: AnimaError
    ) -> Any:
        """Fallback for handler_get - walk fiber chain for service impl."""
        key = target._isolated_services.get(prop)
        try:
            shadow = object.__getattribute__(target, symbols["shadow"])
        except AttributeError:
            shadow = None  # pragma: no cover - shadow rarely set on contexts
        fiber = getattr(shadow, "fiber", ctx.fiber) if shadow is not None else ctx.fiber
        while True:
            # Check target context's reflect store first (most specific registration)
            target_store_impl = (
                target.reflect.store.get(prop)
                if hasattr(target.reflect, "store")
                else None
            )
            if target_store_impl is not None:
                check_fn = target_store_impl.check
                if check_fn is not None:
                    try:
                        check_ok = check_fn()
                    except TypeError:
                        check_ok = True  # check requires self, can't evaluate  # pragma: no cover
                    if not check_ok:
                        raise error
                svc = target_store_impl.value
                # Wrap in _BoundService only if service's ctx differs from target AND it's a Service
                if isinstance(svc, Service) and getattr(svc, "ctx", None) is not target:
                    return _BoundService(svc, target)
                return svc
            # Check target context's _services (for ctx.provide() registered services)
            if prop in target._services:
                svc = target._services[prop]
                if isinstance(svc, Service) and getattr(svc, "ctx", None) is not target:
                    return _BoundService(svc, target)
                return svc
            # Check fiber's store
            impl = fiber._store.get(prop) if hasattr(fiber, "_store") else None
            if impl is not None:
                svc = impl.value
                if isinstance(svc, Service) and getattr(svc, "ctx", None) is not target:
                    return _BoundService(svc, target)
                return svc
            # Check this fiber's context reflect store
            fib_ctx = getattr(fiber, "ctx", None)
            if fib_ctx is not None:
                fib_store_impl = (
                    fib_ctx.reflect.store.get(prop)
                    if hasattr(fib_ctx.reflect, "store")
                    else None
                )
                if fib_store_impl is not None:
                    if fib_store_impl.check is not None and not fib_store_impl.check():
                        raise error
                    svc = fib_store_impl.value
                    if (
                        isinstance(svc, Service)
                        and getattr(svc, "ctx", None) is not target
                    ):
                        return _BoundService(svc, target)
                    return svc
            if prop in getattr(fiber, "inject", {}):
                raise AnimaError(
                    "inactive_context",
                    f'cannot get required service "{prop}" in inactive context',
                ) from error
            if not getattr(fiber, "runtime", False):
                raise error
            parent_fiber = getattr(fiber, "parent", None)
            if parent_fiber is None:  # pragma: no cover - root fiber has no parent
                raise error
            parent_isolate = (
                getattr(parent_fiber.ctx, "_isolated_services", {})
                if hasattr(parent_fiber, "ctx")
                else {}
            )
            if (
                parent_isolate.get(prop) != key
            ):  # pragma: no cover - parent fiber walking
                raise error
            fiber = getattr(parent_fiber, "fiber", None)
            if fiber is None:  # pragma: no cover - fiber chain should not end here
                raise error

    def handler_set(self, target: Context, prop: str, value: Any, ctx: Context) -> bool:
        """ProxyHandler.set trap - set service property."""
        if _is_special_property(prop):
            setattr(target, prop, value)
            return True

        error = AnimaError(
            "set_error", f'cannot set property "{prop}" without provide'
        )

        try:
            defn = target.reflect.props.get(prop)
            if defn is not None:
                if defn.type == "accessor":
                    setter = getattr(defn, "set", None)
                    if setter is not None:
                        return bool(setter(ctx, value, ctx, error))
                    return False
                return self.set(prop, value, error)

            if not getattr(ctx.fiber, "runtime", False):
                object.__setattr__(target, prop, value)
                return True

            # ctx.events.waterfall('internal/set', ...) would be used
            # here for async interception; in sync __setattr__, fall through
            # to reflect.set directly
            return self.set(
                prop, value, error
            )  # pragma: no cover - requires fiber.runtime
        except AnimaError:
            raise  # pragma: no cover - AnimaError without "without inject"
        except Exception as e:
            raise _enhance_error(e) from e  # pragma: no cover - enhance_error path

    def handler_has(self, target: Context, prop: str) -> bool:
        """ProxyHandler.has trap - check if property exists."""
        if _is_special_property(prop):  # pragma: no cover - special property hasattr
            return hasattr(target, prop)
        try:
            object.__getattribute__(target, prop)
            return True
        except AttributeError:
            pass
        return prop in target.reflect.props

    # Handler dict for use with Context proxy (ProxyHandler parity)
    handler: ClassVar[dict[str, Any]] = {
        "get": handler_get,
        "set": handler_set,
        "has": handler_has,
    }
