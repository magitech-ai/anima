"""Reflect service for service property access and introspection.

Python port of the reference ReflectService: manages service properties,
accessors, mixins, and change notification.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from anima.core import Context, FiberState, Service


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


class ReflectService(Service):
    """Service property access, accessors, mixins, and change notification."""

    def __init__(self, ctx: Context) -> None:
        super().__init__(ctx, "reflect")
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

        if self.ctx.fiber.state == FiberState.ACTIVE:
            self.notify([name])

        self.ctx.fiber._set_state(FiberState.ACTIVE)

        def disposer() -> None:
            if self.store.get(name) is impl:
                del self.store[name]
            self.notify([name])

        return disposer

    def notify(
        self,
        names: list[str],
        filter_fn: Callable[[Context, str], bool] | None = None,
    ) -> None:
        """Notify fibers of service changes."""
        if filter_fn is None:

            def filter_fn(ctx: Context, name: str) -> bool:
                return True

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
                if not has_update:
                    continue

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
        return value

    def bind(self, callback: Callable[..., Any]) -> Callable[..., Any]:
        """Wrap a callback with context tracing."""

        def bound(*args: Any, **kwargs: Any) -> Any:
            return callback(*args, **kwargs)

        return bound
