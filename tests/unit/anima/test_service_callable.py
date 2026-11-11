"""Service callable correctness + symbols reconciliation (P1-2).

F12: Service.__call__ dispatches to the callable stored under the
declared symbols["invoke"] key (not a plain-named ``invoke``
attribute), and _BoundService awaits an async invoke result through
the bound-service path.
F13: __instancecheck__ lives on the Service metaclass (proxy-aware);
_extend has no dead branch; Service.__init__ falls back to the
``provide`` class attribute for the service name.
Section 3.5: the symbols dict carries ``config`` and
``resolveConfig``; the unused ``caller``/``decorator``/``root`` keys
are gone; ``mergeIntercept``/``resolveIntercept`` stay anima-only.
_call_with_config fails loud when a Service subclass declares a
non-config second constructor parameter.
"""

from typing import Any

import pytest

from anima.core import (
    Context,
    CoreService,
    FiberState,
    Service,
    _BoundService,
    symbols,
)


class _OtherService(Service):
    """Unrelated service used for negative isinstance checks."""

    def __init__(self, ctx: Context) -> None:
        super().__init__(ctx, "other_svc")


def test_symbol_invoke_sync_callable():
    """A service with a symbol-keyed invoke is callable."""
    ctx = Context()

    class SyncService(Service):
        def __init__(self, c: Context) -> None:
            super().__init__(c, "sync_svc")

        def _invoke(self, x: int) -> int:
            return x + 1

    setattr(SyncService, symbols["invoke"], SyncService._invoke)
    svc = SyncService(ctx)
    assert svc(41) == 42


@pytest.mark.asyncio
async def test_symbol_invoke_async_direct():
    """An async symbol-keyed invoke returns an awaitable result."""
    ctx = Context()

    class AsyncService(Service):
        def __init__(self, c: Context) -> None:
            super().__init__(c, "async_svc")

        async def _invoke(self, x: int) -> int:
            return x * 2

    setattr(AsyncService, symbols["invoke"], AsyncService._invoke)
    svc = AsyncService(ctx)
    assert await svc(21) == 42


@pytest.mark.asyncio
async def test_symbol_invoke_async_via_bound_service():
    """A bound service awaits an async invoke result."""
    ctx = Context()

    class AsyncService(Service):
        def __init__(self, c: Context) -> None:
            super().__init__(c, "async_svc")

        async def _invoke(self, x: int) -> int:
            return x * 2

    setattr(AsyncService, symbols["invoke"], AsyncService._invoke)
    svc = AsyncService(ctx)
    bound = _BoundService(svc, ctx)
    assert await bound(21) == 42


@pytest.mark.asyncio
async def test_bound_async_invoke_runs_with_bound_context():
    """An async invoke awaited through a bound service sees the bound ctx.

    Before the fix, _BoundService called the service synchronously and
    returned the invoke coroutine un-awaited, so the invoke body ran
    after the access-context token was reset and saw the service's own
    context instead of the bound one. The intercept lookup below only
    resolves when the bound (child) context is active during the await.
    """
    parent = Context()

    class InterceptService(Service):
        def __init__(self, c: Context, config: dict | None = None) -> None:
            super().__init__(c, "intercept_svc")

        async def _invoke(self) -> dict[str, Any]:
            return self.resolve_intercept()

    setattr(InterceptService, symbols["invoke"], InterceptService._invoke)
    svc = InterceptService(parent)
    child = Context(
        parent=parent,
        intercept_configs={"intercept_svc": {"level": "debug"}},
    )
    bound = _BoundService(svc, child)
    assert await bound() == {"level": "debug"}


def test_service_call_without_invoke_raises():
    """A service with no symbol-keyed invoke is not callable."""
    ctx = Context()
    svc = CoreService(ctx)
    with pytest.raises(TypeError, match="is not callable"):
        svc()


def test_plain_named_invoke_not_dispatched():
    """A plain-named ``invoke`` attribute is not the symbol-keyed invoke."""
    ctx = Context()

    class PlainInvokeService(Service):
        def __init__(self, c: Context) -> None:
            super().__init__(c, "plain_invokable")

        def invoke(self, *args: Any, **kwargs: Any) -> str:
            return "plain"

    svc = PlainInvokeService(ctx)
    with pytest.raises(TypeError, match="is not callable"):
        svc()


def test_instancecheck_bound_service_proxy_aware():
    """isinstance sees through a bound-service facade to the service."""
    ctx = Context()
    svc = CoreService(ctx)
    bound = _BoundService(svc, ctx)
    assert isinstance(bound, CoreService)
    assert isinstance(bound, Service)
    assert not isinstance(bound, _OtherService)


def test_instancecheck_metaclass_direct():
    """The instance check lives on the Service metaclass, not the class."""
    ctx = Context()

    class SubService(CoreService):
        pass

    sub = SubService(ctx)
    check = type(CoreService).__instancecheck__
    assert check(CoreService, sub) is True
    assert check(CoreService, None) is False
    assert check(CoreService, 42) is False
    assert check(CoreService, "not_a_service") is False


def test_service_name_falls_back_to_provide():
    """Service.__init__ uses the ``provide`` class attribute as the name."""
    ctx = Context()

    class ProvidedService(Service):
        provide = "provided_svc"

        def __init__(self, c: Context) -> None:
            super().__init__(c)

    svc = ProvidedService(ctx)
    assert svc.name == "provided_svc"


def test_service_name_required_without_provide():
    """No name and no ``provide`` fails loud at construction."""
    ctx = Context()

    class NamelessService(Service):
        def __init__(self, c: Context) -> None:
            super().__init__(c)

    with pytest.raises(TypeError, match="requires a service name"):
        NamelessService(ctx)


def test_service_name_must_be_string():
    """A non-string service name fails loud instead of corrupting state."""
    ctx = Context()
    with pytest.raises(TypeError, match="must be a string"):
        CoreService(ctx, {"name": {"not": "a string"}})


@pytest.mark.asyncio
async def test_provide_service_mounts_without_config():
    """A ``provide`` service mounts through the registry with no config."""
    ctx = Context()

    class ProvidedService(Service):
        provide = "provided_svc"

        def __init__(self, c: Context, config: dict | None = None) -> None:
            super().__init__(c)

    fib = ctx.use(ProvidedService)
    await ctx.flush()
    assert fib.state == FiberState.ACTIVE
    svc = ctx.get("provided_svc")
    assert isinstance(svc, ProvidedService)
    assert svc.name == "provided_svc"


def test_extend_callable_service_stays_callable():
    """An extended service with a symbol-keyed invoke stays callable."""
    ctx = Context()

    class CallableService(Service):
        def __init__(self, c: Context) -> None:
            super().__init__(c, "callable_svc")

        def _invoke(self, x: int) -> int:
            return x * 3

    setattr(CallableService, symbols["invoke"], CallableService._invoke)
    svc = CallableService(ctx)
    extended = svc._extend({"extra": "data"})
    assert extended.extra == "data"
    assert extended(14) == 42


def test_symbols_config_and_resolve_config_keys():
    """The symbols dict carries config/resolveConfig; dead keys are gone."""
    assert symbols["config"] == "anima.config"
    assert symbols["resolveConfig"] == "anima.resolveConfig"
    assert "caller" not in symbols
    assert "decorator" not in symbols
    assert "root" not in symbols
    assert Service.__symbols__["config"] == symbols["config"]
    assert Service.__symbols__["resolveConfig"] == symbols["resolveConfig"]
    assert Context.symbols["config"] == symbols["config"]
    assert Context.symbols["resolveConfig"] == symbols["resolveConfig"]
    # Kept anima-only intercept-chain machinery.
    assert "mergeIntercept" in symbols
    assert "resolveIntercept" in symbols


@pytest.mark.asyncio
async def test_call_with_config_guard_rejects_non_config_param():
    """A Service whose 2nd constructor parameter is not config fails loud."""
    ctx = Context()

    class NameTakingService(Service):
        def __init__(self, c: Context, name: str = "named") -> None:
            super().__init__(c, name)

    fib = ctx.use(NameTakingService, {"name": "renamed"})
    await ctx.flush()
    assert fib.state == FiberState.FAILED
    assert isinstance(fib.error, TypeError)
    assert "non-config second parameter" in str(fib.error)


@pytest.mark.asyncio
async def test_call_with_config_guard_allows_ctx_only_service():
    """A Service taking only (ctx) still mounts through the registry."""
    ctx = Context()

    class CtxOnlyService(Service):
        def __init__(self, c: Context) -> None:
            super().__init__(c, "ctx_only")

    fib = ctx.use(CtxOnlyService)
    await ctx.flush()
    assert fib.state == FiberState.ACTIVE


@pytest.mark.asyncio
async def test_call_with_config_guard_allows_config_service():
    """A Service taking (ctx, config) mounts with the config dict."""
    ctx = Context()
    fib = ctx.use(CoreService, {"name": "custom_core"})
    await ctx.flush()
    assert fib.state == FiberState.ACTIVE
    svc = ctx.get("custom_core")
    assert isinstance(svc, CoreService)
    assert svc.name == "custom_core"
