"""Reflect service for service property access and introspection.

Python port of the ReflectService: manages service properties,
accessors, mixins, and change notification.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from anima.core import Context, FiberState, _current_fiber, symbols


def _is_traceable(value: Any) -> bool:
    """Check if a value is an object-like type that can be traced."""
    return value is not None and not isinstance(
        value, (str, int, float, bool, bytes, type)
    )


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
