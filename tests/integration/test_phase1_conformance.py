"""Phase 1 conformance suite (P1-10): one executable probe per §3 row.

Recording basis: the upstream source cannot be executed here — a bare
Node runtime cannot import the vendored TypeScript oracle, so no live
side-by-side run is possible (the P1-0 verdict). Each expectation below
is recorded as data, with a comment citing the oracle file:line it
comes from. The test asserts anima produces exactly that recorded
trace. Where anima's own surface differs from the oracle's, the
difference is pinned as a deliberate, ADR-recorded divergence and
called out in the comment instead of being silently asserted as
equivalent.

Coverage: §3.1 F1–F16 (fiber & service), §3.2 S1–S14 (schema, volatile,
effect metadata), §3.3 L1–L17 (loader, include, group, HMR), §3.4
shared utilities, §3.5 symbols, §3.6 dead / unwired scaffolding.

Two corrections to the P1-10 conformance audit are pinned here:
- L10: the audit claimed no journal oracle exists in the workspace.
  Wrong — the 242-line journal oracle lives at
  phase1/p17-oracle/cordiverse-journal.ts and the L10 probes verify
  the port against it function by function.
- F1: anima's FiberState declaration order differs from the oracle's
  const-enum order (UNLOADING/DISPOSED/FAILED swapped). The name set
  and the wire values match; comparison is name-based (ADR 0009), so
  the order is meaningless and is pinned as-is.

The six open follow-ups are NOT claimed DONE here and have no probes
asserting them as conforming behavior: the keyword-only
``_call_with_config`` hole, old ``plugins:``-keyed profiles mounting
nothing silently, the home-path-helper / array-``.includes`` chains
failing loud with no binding, no EntryTree-level ``validateOptions``
pre-check, the HMR ``stop()`` debounce/stash question, the unbounded
``_graph_cache``, and registry staleness after HMR ``replace``.
"""

from __future__ import annotations

import asyncio
import collections.abc
import errno
import importlib
import importlib.util
import inspect
import io
import json
import logging
import os
import re
import secrets
import sys
import time
import types
import typing
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, ClassVar

import pytest
import yaml

import anima
from anima import (
    CliEnv,
    Context,
    FiberState,
    Service,
    core,
    utils,
)
from anima.cli import console_main
from anima.cli_create import Scaffold
from anima.core import (
    FIBER_STATE_WIRE_VALUES,
    AnimaError,
    AnimaErrorCode,
    CoreService,
    Fiber,
    _BoundService,
    _ServiceMeta,
    is_js_expr,
    symbols,
)
from anima.hmr import HmrService, Reload, handle_error
from anima.include import (
    _PARENT_UNSET,
    Conflict,
    EntryChange,
    Include,
    IncludeError,
    Journal,
    JournalRecord,
    apply_changes,
    apply_entry_patches,
    apply_journal,
    detach,
    diff_entries,
    diff_options,
    flatten,
    merge,
    merge_records,
    place,
    reconcile,
    record,
)
from anima.jsexpr import (
    JsExprError,
    dump_yaml,
    evaluate,
    interpolate,
    load_yaml,
)
from anima.loader import LoaderService, load_profile
from anima.registry import RegistryService
from anima.schema import (
    _UNDEFINED,
    Schema,
    SchemaError,
    ValidationError,
    clone,
    deep_equal,
)
from anima.tree import (
    EntryGroup,
    EntryTree,
    Group,
    equal_except_volatile,
    unwrap_exports,
)
from anima.utils import (
    Binary,
    Dict,
    Promisify,
    Time,
    filter_keys,
    hyphenate,
    is_non_nullable,
    is_nullable,
    is_plain_object,
    map_values,
    pick,
    value_map,
)
from anima.volatile import (
    create_volatile,
    is_volatile,
    update_volatile,
    volatile_entries,
)


def _anima_sources() -> list[Path]:
    return sorted(_repo_root().joinpath("src", "anima").rglob("*.py"))


class _OtherService(Service):
    """Unrelated service used for negative isinstance checks."""

    def __init__(self, ctx: Context) -> None:
        super().__init__(ctx, "other_svc")


def test_conformance_f1_fiber_state_names_and_wire_values() -> None:
    """FiberState has the same 6 names; wire values follow the oracle order.

    The oracle declares the states as a const enum in PENDING, LOADING,
    ACTIVE, FAILED, DISPOSED, UNLOADING order (oracle fiber.ts:147-153),
    so the wire numbers are 0–5 in that order. Deliberate divergence,
    ADR 0009: anima is a string enum — states cross any boundary by
    name, never by ordinal — and the numeric values live only in the
    name-based FIBER_STATE_WIRE_VALUES record. No code path compares
    states numerically.

    Audit discrepancy, found by this probe: the audit's "same 6 names"
    holds for the name SET, but anima's declaration order is PENDING,
    LOADING, ACTIVE, UNLOADING, DISPOSED, FAILED — not the oracle's
    order. That order carries no meaning here (comparison is by name),
    so it is pinned as-is below rather than as oracle order.
    """
    assert {state.name for state in FiberState} == {
        "PENDING",
        "LOADING",
        "ACTIVE",
        "FAILED",
        "DISPOSED",
        "UNLOADING",
    }
    assert list(FiberState) == [
        FiberState.PENDING,
        FiberState.LOADING,
        FiberState.ACTIVE,
        FiberState.UNLOADING,
        FiberState.DISPOSED,
        FiberState.FAILED,
    ]
    assert all(state.value == state.name for state in FiberState)
    assert FiberState.ACTIVE.value == "ACTIVE"
    assert FiberState.ACTIVE == "ACTIVE"
    assert FiberState.ACTIVE != 2
    assert FIBER_STATE_WIRE_VALUES == {
        FiberState.PENDING: 0,
        FiberState.LOADING: 1,
        FiberState.ACTIVE: 2,
        FiberState.FAILED: 3,
        FiberState.DISPOSED: 4,
        FiberState.UNLOADING: 5,
    }
    assert set(FIBER_STATE_WIRE_VALUES) == set(FiberState)

    # A fiber's diagnostic snapshot carries the state by name.
    ctx = Context()
    assert Fiber(ctx, lambda c: None, (), {}).status()["state"] == "PENDING"

    # No code path compares states numerically or relies on their order.
    numeric_compare = re.compile(r"FiberState\.[A-Z_]+\s*(?:<=|>=|<|>)")
    int_coerce = re.compile(r"int\(\s*FiberState")
    hits = [
        f"{path.relative_to(_repo_root())}:{n}"
        for path in _anima_sources()
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
        if numeric_compare.search(line) or int_coerce.search(line)
    ]
    assert hits == []


@pytest.mark.asyncio
async def test_conformance_f2_epoch_machine_guards() -> None:
    """The epoch machine guards transitions: missing deps stay PENDING, and
    a re-entrant epoch change while a transition is in flight collapses
    into that transition instead of starting a second one.

    The oracle stores the epoch first and early-returns while a
    transition (inertia) is in flight (oracle fiber.ts:625-640); the
    epoch itself is derived from the uids of the fibers owning the
    required service implementations (oracle fiber.ts:581-593, 597-623).
    Both guards are driven here through public behavior only.
    """
    ctx = Context()
    runs: list[str] = []

    def needs_db(c: Context) -> None:
        runs.append("ran")

    needs_db.inject = ["db"]
    fib = ctx.use(needs_db)
    await ctx.flush()
    # No provider: the epoch stays inactive, the body never runs.
    assert fib.state == FiberState.PENDING
    assert runs == []

    ctx.provide("db", "connected")
    await ctx.flush()
    # Providing the dependency activates the epoch; the body runs once.
    assert fib.state == FiberState.ACTIVE
    assert runs == ["ran"]

    # A restart issued while a load is in flight must not start a second
    # transition or re-run the body: the re-entrant epoch changes are
    # stored and the in-flight reload settles the fiber.
    slow_ctx = Context()
    slow_runs: list[str] = []
    started = asyncio.Event()

    async def slow(c: Context) -> None:
        started.set()
        await asyncio.sleep(0.05)
        slow_runs.append("ran")

    slow_fib = Fiber(slow_ctx, slow, (), {})
    load_task = asyncio.create_task(slow_fib.load())
    await started.wait()
    await slow_fib.restart()
    await load_task
    assert slow_runs == ["ran"]
    assert slow_fib.state == FiberState.ACTIVE


@pytest.mark.asyncio
async def test_conformance_f3_reentrant_load_joins() -> None:
    """Two concurrent load() calls join one in-flight load; the body runs once.

    The oracle assigns the in-flight transition before the LOADING
    state is published and early-returns while it is set (oracle
    fiber.ts:628-633).
    """
    ctx = Context()
    runs: list[str] = []

    class SlowComponent:
        async def init(self) -> None:
            await asyncio.sleep(0.01)
            runs.append("ran")

    fib = Fiber(ctx, SlowComponent, (), {})
    first, second = await asyncio.gather(fib.load(), fib.load())
    assert runs == ["ran"]
    assert first is second
    assert isinstance(first, SlowComponent)
    assert fib.state == FiberState.ACTIVE


@pytest.mark.asyncio
async def test_conformance_f4_dispose_while_loading_never_resurrects() -> None:
    """A dispose issued while a load is in flight ends DISPOSED, never ACTIVE.

    The oracle clears the uid first, drops the epoch to inactive, then
    joins the in-flight transition (oracle fiber.ts:260-300); the
    reload's staleness checkpoint keeps the superseded load from
    re-activating the fiber (oracle fiber.ts:673-684).
    """
    ctx = Context()
    started = asyncio.Event()
    release = asyncio.Event()
    runs: list[str] = []

    async def slow_component(c: Context) -> None:
        started.set()
        await release.wait()
        runs.append("ran")

    fib = Fiber(ctx, slow_component, (), {})
    load_task = asyncio.create_task(fib.load())
    await started.wait()
    dispose_task = asyncio.create_task(fib.dispose())
    await asyncio.sleep(0)
    release.set()
    await load_task
    await dispose_task
    assert runs == ["ran"]
    assert fib.state == FiberState.DISPOSED
    assert fib.uid is None
    # No later load resurrects a disposed fiber.
    assert await fib.load() is None
    assert fib.state == FiberState.DISPOSED


@pytest.mark.asyncio
async def test_conformance_f5_reload_reruns_body_and_staleness_checkpoint() -> None:
    """Restart re-runs the body on an ACTIVE fiber; a reload whose epoch
    moved before its checkpoint never runs the body.

    The oracle snapshots the store, yields, and only executes the body
    when the epoch is unchanged (oracle fiber.ts:673-700).
    """
    ctx = Context()
    runs: list[str] = []

    def component(c: Context) -> None:
        runs.append("ran")

    fib = ctx.use(component)
    await ctx.flush()
    assert runs == ["ran"]
    assert fib.state == FiberState.ACTIVE

    await fib.restart()
    assert runs == ["ran", "ran"]
    assert fib.state == FiberState.ACTIVE

    # Staleness, driven through public behavior: the load is started,
    # then the fiber is disposed before the reload's checkpoint runs,
    # so the body never executes for the superseded epoch.
    stale_ctx = Context()
    stale_runs: list[str] = []

    def stale_component(c: Context) -> None:
        stale_runs.append("ran")

    stale_fib = Fiber(stale_ctx, stale_component, (), {})
    stale_load = asyncio.create_task(stale_fib.load())
    await asyncio.sleep(0)
    await stale_fib.dispose()
    await stale_load
    assert stale_runs == []
    assert stale_fib.state == FiberState.DISPOSED
    assert stale_fib.uid is None


@pytest.mark.asyncio
async def test_conformance_f6_load_failure_stores_error_and_recovers() -> None:
    """A load failure stores the error, the fiber is FAILED, awaiting it
    rethrows, and update() with a valid config recovers it.

    The oracle swallows the failure into the fiber and rethrows it from
    await() (oracle fiber.ts:686-691, 725-735); a later update retries
    (oracle fiber.ts:754-780, deferring resolution while non-ACTIVE).
    """
    ctx = Context()

    class Numbered:
        Config = Schema.object({"num": Schema.integer()})

        def __init__(self, c: Context, config: Any = None) -> None:
            self.config = config

    fib = ctx.use(Numbered, num="not_an_int")
    await ctx.flush()
    assert fib.state == FiberState.FAILED
    err = fib.error
    assert isinstance(err, ValidationError)
    assert err.issues and any(issue.get("path") == ["num"] for issue in err.issues)
    assert "invalid config" in str(err)
    assert await fib.load() is None
    with pytest.raises(ValidationError, match="invalid config"):
        await fib.await_settle()

    await fib.update({"num": 7})
    await fib.await_settle()
    assert fib.state == FiberState.ACTIVE
    assert fib.config == {"num": 7}

    # A body that raises stores the same way and recovers the same way.
    body_ctx = Context()

    def gated(c: Context, config: Any = None) -> None:
        if (config or {}).get("fail"):
            raise RuntimeError("boom")

    bad = body_ctx.use(gated, fail=True)
    await body_ctx.flush()
    assert bad.state == FiberState.FAILED
    assert isinstance(bad.error, RuntimeError)
    assert str(bad.error) == "boom"
    with pytest.raises(RuntimeError, match="boom"):
        await bad.await_settle()

    await bad.update({"fail": False})
    await bad.await_settle()
    assert bad.state == FiberState.ACTIVE
    assert bad.error is None


@pytest.mark.asyncio
async def test_conformance_f7_unload_vs_dispose() -> None:
    """_unload unwinds children first, then disposers in reverse order,
    and clears the store; dispose() drives that unload through the epoch
    machine and ends terminally DISPOSED, while restart() unloads and
    reloads the same fiber.

    The oracle has a single _unload with the same shape (oracle
    fiber.ts:702-721).
    """
    ctx = Context()
    order: list[str] = []

    def child(c: Context) -> Any:
        return lambda: order.append("child-disposer")

    def parent(c: Context) -> Any:
        ctx.use(child)
        c.effect(lambda: lambda: order.append("parent-effect"))
        return lambda: order.append("parent-apply")

    fib = ctx.use(parent)
    await ctx.flush()
    assert fib.state == FiberState.ACTIVE
    assert fib.store is not None
    child_fib = fib.child_fibers[0]
    assert child_fib.state == FiberState.ACTIVE

    await fib.dispose()
    # Children first, then the parent's own disposers last-in-first-out.
    assert order == ["child-disposer", "parent-apply", "parent-effect"]
    assert child_fib.state == FiberState.DISPOSED
    assert fib.state == FiberState.DISPOSED
    assert fib.uid is None
    assert fib.store is None
    assert await fib.load() is None

    # Contrast: restart() unloads without disposing — the same fiber
    # comes back ACTIVE with the same uid and a freshly mounted child.
    ctx2 = Context()
    order2: list[str] = []
    runs2: list[str] = []

    def child2(c: Context) -> Any:
        return lambda: order2.append("child-disposer")

    def parent2(c: Context) -> Any:
        runs2.append("ran")
        ctx2.use(child2)
        return lambda: order2.append("parent-apply")

    fib2 = ctx2.use(parent2)
    await ctx2.flush()
    uid_before = fib2.uid
    old_child = fib2.child_fibers[0]
    await fib2.restart()
    await ctx2.flush()  # the re-mounted child's load settles separately
    assert runs2 == ["ran", "ran"]
    assert fib2.state == FiberState.ACTIVE
    assert fib2.uid == uid_before
    assert fib2.store is not None
    assert old_child.state == FiberState.DISPOSED
    assert len(fib2.child_fibers) == 1
    assert fib2.child_fibers[0] is not old_child
    assert fib2.child_fibers[0].state == FiberState.ACTIVE
    assert order2 == ["child-disposer", "parent-apply"]


@pytest.mark.asyncio
async def test_conformance_f8_restart_update_semantics() -> None:
    """restart() unloads and re-mounts; update() validates the new config
    and restarts. Deliberate divergence, ADR 0001: anima update() is
    async — the waterfall is async throughout — while the oracle
    update() is sync (oracle fiber.ts:754-780). The shape otherwise
    matches: assertActive first, deferred resolution while non-ACTIVE,
    config set inside the internal/update waterfall default, then
    restart().
    """
    assert inspect.iscoroutinefunction(Fiber.update)
    assert inspect.iscoroutinefunction(Fiber.restart)

    ctx = Context()
    runs: list[int] = []

    class Counted:
        def __init__(self, c: Context, config: Any = None) -> None:
            runs.append((config or {}).get("val", 0))

    fib = ctx.use(Counted, val=1)
    await ctx.flush()
    assert runs == [1]
    assert fib.state == FiberState.ACTIVE
    uid = fib.uid

    # update() is a coroutine: awaiting it applies the config and restarts.
    coro = fib.update({"val": 2})
    assert inspect.iscoroutine(coro)
    await coro
    assert runs == [1, 2]
    assert fib.config == {"val": 2}
    assert fib.state == FiberState.ACTIVE
    assert fib.uid == uid

    await fib.restart()
    assert runs == [1, 2, 2]
    assert fib.state == FiberState.ACTIVE
    assert fib.uid == uid

    # A disposed fiber fails loud on every lifecycle entry point.
    await fib.dispose()
    assert fib.state == FiberState.DISPOSED
    with pytest.raises(AnimaError) as excinfo:
        fib.assertActive()
    assert excinfo.value.code == AnimaErrorCode.INACTIVE_EFFECT
    with pytest.raises(AnimaError):
        await fib.restart()
    with pytest.raises(AnimaError):
        await fib.update({"val": 3})


@pytest.mark.asyncio
async def test_conformance_f9_internal_status_exactly_two_args() -> None:
    """internal/status emits exactly (fiber, old_state) on every transition.

    The oracle declares the event with two arguments (oracle
    events.ts:333) and emits it from the state update (oracle
    fiber.ts:579-592). The listener below takes exactly two arguments,
    so any other arity would fail the emit itself.
    """
    ctx = Context()
    events: list[tuple[Any, Any]] = []
    ctx.on("internal/status", lambda f, old: events.append((f, old)))

    fib = ctx.use(lambda c: None)
    await ctx.flush()
    await fib.dispose()

    assert events == [
        (fib, FiberState.PENDING),
        (fib, FiberState.LOADING),
        (fib, FiberState.ACTIVE),
        (fib, FiberState.UNLOADING),
    ]
    assert all(len(e) == 2 for e in events)
    assert all(isinstance(e[0], Fiber) and isinstance(e[1], FiberState) for e in events)


@pytest.mark.asyncio
async def test_conformance_f10_fiber_thenable() -> None:
    """Awaiting a fiber settles it and returns the fiber itself.

    The oracle makes the registry wrapper thenable (oracle
    registry.ts:331-335); anima chose __await__ on the fiber instead,
    so there is no wrapper object and no .then — `await fiber` works
    directly.
    """
    ctx = Context()
    fib = ctx.use(lambda c: None)
    assert hasattr(fib, "__await__")
    assert getattr(fib, "then", None) is None

    result = await fib
    assert result is fib
    assert fib.state == FiberState.ACTIVE

    assert await fib.await_() is fib


@pytest.mark.asyncio
async def test_conformance_f11_async_dispose_out_of_scope() -> None:
    """Async disposal is out of scope on both sides: Fiber has no
    __aenter__/__aexit__, so `async with fiber` fails loud.

    Deliberate exclusion, ADR 0011: no async-dispose protocol is
    invented. The oracle likewise carries no async-dispose symbol on
    its fiber surface.
    """
    assert not hasattr(Fiber, "__aenter__")
    assert not hasattr(Fiber, "__aexit__")

    ctx = Context()
    fib = ctx.use(lambda c: None)
    await ctx.flush()
    assert not hasattr(fib, "__aenter__")
    assert not hasattr(fib, "__aexit__")
    with pytest.raises(TypeError, match="asynchronous context manager"):
        async with fib:
            pass

    adr = _repo_root() / "docs" / "adr" / "0011-async-disposal-out-of-scope.md"
    assert adr.is_file()


@pytest.mark.asyncio
async def test_conformance_f12_service_callable_symbol_invoke() -> None:
    """A service is callable through the symbol-keyed invoke; an async
    invoke is awaited through the bound path.

    The oracle builds a callable wrapper around the symbol-keyed
    invoke (oracle service.ts:50-57, utils.ts:200-215, 226-233).
    Deliberate shape divergence: Python __init__ cannot return a
    replacement object, so no wrapper is built — __call__ on the
    service instance itself is the shape, and the service stays the
    very object that was constructed.
    """
    ctx = Context()

    class SyncService(Service):
        def __init__(self, c: Context) -> None:
            super().__init__(c, "sync_svc")

        def _invoke(self, x: int) -> int:
            return x + 1

    setattr(SyncService, symbols["invoke"], SyncService._invoke)
    svc = SyncService(ctx)
    assert type(svc) is SyncService
    assert callable(svc)
    assert svc(41) == 42

    class AsyncService(Service):
        def __init__(self, c: Context) -> None:
            super().__init__(c, "async_svc")

        async def _invoke(self, x: int) -> int:
            return x * 2

    setattr(AsyncService, symbols["invoke"], AsyncService._invoke)
    async_svc = AsyncService(ctx)
    assert await async_svc(21) == 42

    # The bound path awaits the async invoke with the bound context
    # active, so intercept lookups resolve against it.
    class InterceptService(Service):
        def __init__(self, c: Context, config: Any = None) -> None:
            super().__init__(c, "intercept_svc")

        async def _invoke(self) -> dict[str, Any]:
            return self.resolve_intercept()

    setattr(InterceptService, symbols["invoke"], InterceptService._invoke)
    intercept_svc = InterceptService(ctx)
    child = Context(
        parent=ctx,
        intercept_configs={"intercept_svc": {"level": "debug"}},
    )
    bound = _BoundService(intercept_svc, child)
    assert await bound() == {"level": "debug"}

    # Only the symbol-keyed invoke dispatches: a plain-named invoke is
    # never consulted, even when a symbol-keyed one is also present.
    class BothService(Service):
        def __init__(self, c: Context) -> None:
            super().__init__(c, "both_svc")

        def invoke(self, *args: Any, **kwargs: Any) -> str:
            return "plain"

        def _invoke(self, *args: Any, **kwargs: Any) -> str:
            return "symbol"

    setattr(BothService, symbols["invoke"], BothService._invoke)
    assert BothService(ctx)() == "symbol"

    class PlainInvokeService(Service):
        def __init__(self, c: Context) -> None:
            super().__init__(c, "plain_invokable")

        def invoke(self, *args: Any, **kwargs: Any) -> str:
            return "plain"

    with pytest.raises(TypeError, match="is not callable"):
        PlainInvokeService(ctx)()

    # No wrapper construction exists anywhere in the ported kernel.
    hits = [
        str(path.relative_to(_repo_root()))
        for path in _anima_sources()
        if "createCallable" in path.read_text(encoding="utf-8")
        or "joinPrototype" in path.read_text(encoding="utf-8")
    ]
    assert hits == []


def test_conformance_f13_service_ancillaries() -> None:
    """__instancecheck__ lives on the Service metaclass and sees through
    bound-service facades; _extend has a single branch; Service.__init__
    falls back to the provide class attribute for the name.

    The oracle defaults the name from the static provide field
    (oracle service.ts:43), walks the constructor chain for instance
    checks (oracle service.ts:104-115), and branches extend on
    callability (oracle service.ts:64-72) — the branch collapses here
    because every Python Service defines __call__.
    """
    assert type(Service) is _ServiceMeta

    ctx = Context()
    svc = CoreService(ctx)
    bound = _BoundService(svc, ctx)
    assert isinstance(bound, CoreService)
    assert isinstance(bound, Service)
    assert not isinstance(bound, _OtherService)
    assert type(CoreService).__instancecheck__(CoreService, None) is False
    assert type(CoreService).__instancecheck__(CoreService, 42) is False

    # _extend: one branch — the extension is always an instance of the
    # same class, whether or not the service is callable.
    extended = svc._extend({"extra": "data"})
    assert type(extended) is CoreService
    assert extended.extra == "data"

    class CallableService(Service):
        def __init__(self, c: Context) -> None:
            super().__init__(c, "callable_svc")

        def _invoke(self, x: int) -> int:
            return x * 3

    setattr(CallableService, symbols["invoke"], CallableService._invoke)
    callable_svc = CallableService(ctx)
    extended_callable = callable_svc._extend({"extra": "data"})
    assert type(extended_callable) is CallableService
    assert extended_callable.extra == "data"
    assert extended_callable(14) == 42

    # provide-name fallback, and fail-loud without any name.
    class ProvidedService(Service):
        provide = "provided_svc"

        def __init__(self, c: Context) -> None:
            super().__init__(c)

    assert ProvidedService(ctx).name == "provided_svc"

    class NamelessService(Service):
        def __init__(self, c: Context) -> None:
            super().__init__(c)

    with pytest.raises(TypeError, match="requires a service name"):
        NamelessService(ctx)

    with pytest.raises(TypeError, match="must be a string"):
        CoreService(ctx, {"name": {"not": "a string"}})


@pytest.mark.asyncio
async def test_conformance_f14_resolve_config_waterfall_and_validation() -> None:
    """_resolve_config runs the internal/config waterfall, then sync
    schema validation, at activation time.

    The oracle resolves config synchronously through the schema seam
    at activation (oracle fiber.ts:50-66, 667-671). Deliberate
    divergence, ADR 0008: validation stays sync, but the waterfall
    stays async — anima's waterfall is async throughout.
    """
    assert not inspect.iscoroutinefunction(Fiber._validate_config_value)

    ctx = Context()

    async def repair_level(cfg: Any, next_fn: Any) -> Any:
        if isinstance(cfg, dict) and cfg.get("level") == "bad":
            return await next_fn({**cfg, "level": 3})
        return await next_fn(cfg)

    ctx.on("internal/config", repair_level)

    class Leveled:
        Config = Schema.object({"greeting": Schema.string(), "level": Schema.integer()})

        def __init__(self, c: Context, config: Any = None) -> None:
            self.config = config

    # The waterfall hook runs before validation, so a hook that repairs
    # an invalid value lets activation succeed with the repaired config.
    fib = ctx.use(Leveled, greeting="hi", level="bad")
    await ctx.flush()
    assert fib.state == FiberState.ACTIVE
    assert fib.config == {"greeting": "hi", "level": 3}

    # resolve_config evaluates a candidate without publishing it.
    candidate = await fib.resolve_config({"greeting": "cand", "level": 1})
    assert candidate == {"greeting": "cand", "level": 1}
    assert fib.config == {"greeting": "hi", "level": 3}
    assert fib.raw_config == {"greeting": "hi", "level": "bad"}

    # An invalid candidate fails validation instead of being returned.
    with pytest.raises(ValidationError, match="invalid config"):
        await fib.resolve_config({"greeting": 7, "level": 1})
    assert fib.config == {"greeting": "hi", "level": 3}

    # Without the repair hook, the same invalid config fails activation.
    plain_ctx = Context()
    bad = plain_ctx.use(Leveled, greeting="hi", level="bad")
    await plain_ctx.flush()
    assert bad.state == FiberState.FAILED
    assert isinstance(bad.error, ValidationError)


@pytest.mark.asyncio
async def test_conformance_f15_root_fiber_and_assert_active() -> None:
    """The root fiber has uid 0 and starts ACTIVE; disposing it restarts
    it; assertActive fires once the uid is cleared.

    The oracle gives the root uid 0 and defines dispose as restart for
    it (oracle fiber.ts:320-330); assertActive throws when the uid is
    null (oracle fiber.ts:351-354).
    """
    ctx = Context()
    root = ctx.fiber
    assert root.uid == 0
    assert root.state == FiberState.ACTIVE

    class Core(Service):
        def __init__(self, c: Context) -> None:
            super().__init__(c, "core_svc")

    Core(ctx)
    assert ctx.has("core_svc")
    await root.dispose()
    assert root.uid == 0
    assert root.state == FiberState.ACTIVE
    assert not ctx.has("core_svc")

    fib = ctx.use(lambda c: None)
    await ctx.flush()
    assert fib.state == FiberState.ACTIVE
    await fib.dispose()
    assert fib.uid is None
    with pytest.raises(AnimaError) as excinfo:
        fib.assertActive()
    assert excinfo.value.code == AnimaErrorCode.INACTIVE_EFFECT
    with pytest.raises(AnimaError):
        await fib.restart()
    with pytest.raises(AnimaError):
        await fib.update({"x": 1})


@pytest.mark.asyncio
async def test_conformance_f16_plugin_invocation_shape() -> None:
    """Components run as class (ctx, config), function (ctx, config), or
    object-with-apply (ctx, config); classes never call apply().

    The oracle constructs class components with (ctx, config), runs
    init hooks then the symbol-keyed init, and calls function
    components with (ctx, config) (oracle fiber.ts:250-259). Two
    divergences are pinned here: anima falls back to a plain-named
    init when no symbol-keyed init exists (the oracle only consults
    the symbol-keyed one), and the rename (ADR 0004) is stricter than
    the oracle — there is no `plugin` method anywhere; mounting goes
    through Context.use / RegistryService.use only.
    """
    ctx = Context()
    calls: list[Any] = []

    class ClassComp:
        def __init__(self, c: Context, config: Any = None) -> None:
            calls.append(("init", c is ctx, config))

        def apply(self, c: Context, config: Any = None) -> None:
            calls.append(("apply", config))

    fib = ctx.use(ClassComp, {"k": 1})
    await ctx.flush()
    assert fib.state == FiberState.ACTIVE
    # The constructor ran with (ctx, config); apply() never ran.
    assert calls == [("init", True, {"k": 1})]

    # Init hooks run, then the symbol-keyed init — not the plain one.
    calls.clear()

    class Hooked:
        def __init__(self, c: Context) -> None:
            pass

        def init(self) -> None:
            calls.append("plain-init")

    def symbol_init(self: Hooked) -> None:
        calls.append("symbol-init")

    setattr(Hooked, symbols["initHooks"], [lambda: calls.append("hook")])
    setattr(Hooked, symbols["init"], symbol_init)
    hooked = ctx.use(Hooked)
    await ctx.flush()
    assert hooked.state == FiberState.ACTIVE
    assert calls == ["hook", "symbol-init"]

    # Plain-named init fallback: with no symbol-keyed init, the plain
    # init still runs (anima leniency, not in the oracle).
    calls.clear()

    class PlainInit:
        def __init__(self, c: Context) -> None:
            pass

        def init(self) -> None:
            calls.append("plain-init")

    plain = ctx.use(PlainInit)
    await ctx.flush()
    assert plain.state == FiberState.ACTIVE
    assert calls == ["plain-init"]

    # Function component: called with (ctx, config).
    calls.clear()

    def fncomp(c: Context, config: Any = None) -> None:
        calls.append(("fn", c is ctx, config))

    fn_fib = ctx.use(fncomp, {"y": 3})
    await ctx.flush()
    assert fn_fib.state == FiberState.ACTIVE
    assert calls == [("fn", True, {"y": 3})]

    # Object with apply: apply(ctx, config).
    calls.clear()

    class ObjComp:
        name = "objcomp"

        def apply(self, c: Context, config: Any = None) -> None:
            calls.append(("apply", c is ctx, config))

    obj_fib = ctx.use(ObjComp(), {"x": 2})
    await ctx.flush()
    assert obj_fib.state == FiberState.ACTIVE
    assert calls == [("apply", True, {"x": 2})]

    # The rename is strict: no `plugin` method or attribute exists on
    # either mounting surface, and no `.plugin(` call exists in src.
    # (The internal/plugin event NAME is a different thing — it uses a
    # slash, not a dot-call — and is out of scope here.)
    assert not hasattr(Context, "plugin")
    assert not hasattr(RegistryService, "plugin")
    assert not hasattr(ctx, "plugin")
    assert "plugin" not in dir(Context)
    assert "plugin" not in dir(RegistryService)
    assert callable(Context.use)
    assert callable(RegistryService.use)
    plugin_call = re.compile(r"\.plugin\s*\(")
    hits = [
        f"{path.relative_to(_repo_root())}:{n}"
        for path in _anima_sources()
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
        if plugin_call.search(line)
    ]
    assert hits == []


# ---------------------------------------------------------------------------
# Shared fixtures: a service with a schema Config, and a loader-mounted
# consumer with volatile fields (same mount shape as the acceptance
# suite: service first, then the consumer through LoaderService).
# ---------------------------------------------------------------------------


class SGreeter(Service):
    """Named capability with a schema Config (oracle service.ts:11)."""

    provide = "greeter"
    Config = Schema.object(
        {"greeting": Schema.string(), "level": Schema.integer().required()}
    )

    def __init__(self, ctx: Context, config: Any = None) -> None:
        cfg = dict(config or {})
        super().__init__(ctx, "greeter")
        self.greeting = cfg.get("greeting", "hello")
        self.level = cfg.get("level", 0)

    def greet(self, name: str) -> str:
        return f"{self.greeting}, {name}"


class SConsumer:
    """Object component with volatile fields (oracle schema index.ts:480-482)."""

    name = "s-consumer"
    inject: ClassVar[list[str]] = ["greeter"]
    Config = Schema.object(
        {
            "theme": Schema.string().volatile(),
            "level": Schema.number(),
            "nested": Schema.object(
                {"label": Schema.string().volatile(), "count": Schema.number()}
            ),
        }
    )

    def __init__(self) -> None:
        self.applies: list[dict[str, Any]] = []

    def apply(self, ctx: Context, config: Any) -> Any:
        self.applies.append(
            {"theme": config["theme"].get(), "level": config.get("level")}
        )
        return lambda: None


class EffectProbe:
    """Object component registering labelled effects (oracle fiber.ts:96-100)."""

    name = "effect-probe"

    def __init__(self) -> None:
        self.disposers: dict[str, Any] = {}

    def apply(self, ctx: Context, config: Any) -> Any:
        self.disposers["a"] = ctx.effect(lambda: lambda: None, label="probe-a")
        self.disposers["b"] = ctx.effect(lambda: lambda: None, label="probe-b")
        self.disposers["anon"] = ctx.effect(lambda: lambda: None)
        return None


def _install_fixture_module(
    monkeypatch: pytest.MonkeyPatch, name: str, consumer: SConsumer
) -> None:
    """Expose the shared consumer instance as an importable module attr."""
    module = types.ModuleType(name)
    module.consumer = consumer
    monkeypatch.setitem(sys.modules, name, module)


async def _mount_consumer(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Context, LoaderService, Any, Any, SConsumer]:
    """Mount the greeter service, then the consumer through LoaderService."""
    ctx = Context()
    ctx.use(SGreeter, greeting="hi", level=1)
    await ctx.flush()
    consumer = SConsumer()
    _install_fixture_module(monkeypatch, "conformance_s_fixture_mod", consumer)
    loader = LoaderService(ctx)
    await loader.create(
        {
            "id": "s-row",
            "name": "conformance_s_fixture_mod:consumer",
            "config": {
                "theme": "dark",
                "level": 3,
                "nested": {"label": "L", "count": 1},
            },
        }
    )
    await loader.await_all()
    entry = loader.resolve("s-row")
    fiber = entry.fiber
    assert fiber is not None and fiber.state == FiberState.ACTIVE
    assert len(consumer.applies) == 1
    return ctx, loader, entry, fiber, consumer


def _full_config(theme: Any, level: Any = 3) -> dict[str, Any]:
    """A complete consumer config (nested volatile field included)."""
    return {"theme": theme, "level": level, "nested": {"label": "L", "count": 1}}


# ---------------------------------------------------------------------------
# S1 — ~standard seam + aggregated ValidationError, wired into activation
# ---------------------------------------------------------------------------


def test_conformance_s1_standard_seam_shape() -> None:
    """The ~standard seam is {version 1, vendor "anima", validate}.

    The seam reports ``{'value': ...}`` on success and
    ``{'issues': [{message, path}]}`` on failure (oracle fiber.ts:50-64
    reads the seam at activation; oracle schema index.ts:290-305
    reports one issue per failure).
    """
    seam = getattr(Schema.string(), "~standard")
    assert seam.version == 1
    assert seam.vendor == "anima"
    assert seam.validate("x") == {"value": "x"}
    assert seam.validate(7) == {
        "issues": [{"message": "expected string but got 7", "path": []}]
    }


def test_conformance_s1_validation_error_format() -> None:
    """Aggregated failures render "invalid config" with dot-joined paths."""
    err = ValidationError(
        [
            {"message": "m1", "path": ["a"]},
            {"message": "m2", "path": ["a", "b"]},
            {"message": "m3", "path": []},
        ]
    )
    assert str(err) == ("invalid config:\n  - m1 (at a)\n  - m2 (at a.b)\n  - m3")
    assert err.issues[0]["path"] == ["a"]


@pytest.mark.asyncio
async def test_conformance_s1_activation_wiring() -> None:
    """Activation validates through the seam and aggregates the issues.

    A bad config lands the fiber FAILED with the aggregated message
    (oracle fiber.ts:55-71); the seam reports the first failure in
    declaration order only — one issue, not one per field — and nested
    paths are dot-joined (oracle fiber.ts:24-38).
    """
    ctx = Context()
    fiber = ctx.use(SGreeter, greeting=7, level="x")
    await ctx.flush()

    assert fiber.state == FiberState.FAILED
    err = fiber.error
    assert isinstance(err, ValidationError)
    assert err.issues == [
        {"message": "$.greeting expected string but got 7", "path": ["greeting"]}
    ]
    assert str(err) == (
        "invalid config:\n  - $.greeting expected string but got 7 (at greeting)"
    )
    with pytest.raises(ValidationError, match="invalid config"):
        await fiber.await_settle()

    ctx.use(SGreeter, greeting="hi", level=1)
    await ctx.flush()
    bad = ctx.use(
        SConsumer(), theme="dark", level=3, nested={"label": "L", "count": "x"}
    )
    await ctx.flush()
    assert bad.state == FiberState.FAILED
    nested_err = bad.error
    assert isinstance(nested_err, ValidationError)
    assert nested_err.issues == [
        {
            "message": "$.nested.count expected number but got x",
            "path": ["nested", "count"],
        }
    ]
    assert str(nested_err) == (
        "invalid config:\n"
        "  - $.nested.count expected number but got x (at nested.count)"
    )


# ---------------------------------------------------------------------------
# S2 — Volatile: schema hook, placement, runtime, loader commit path
# ---------------------------------------------------------------------------


def test_conformance_s2_volatile_hook_and_placement() -> None:
    """``.volatile()`` throws when already volatile; placement is validated.

    The hook throws on a second wrap (oracle schema index.ts:480-482),
    and a volatile field must sit at a fixed object path — volatile
    inside an array, a union member, a transform inner, or another
    volatile field is rejected (oracle schema index.ts:521-529).
    """
    with pytest.raises(TypeError, match="already wrapped"):
        Schema.string().volatile().volatile()

    with pytest.raises(SchemaError, match="fixed object path"):
        Schema.list(Schema.string().volatile()).validate(["a"])
    with pytest.raises(SchemaError, match="fixed object path"):
        Schema.union(Schema.string().volatile(), Schema.number()).validate("x")
    with pytest.raises(SchemaError, match="fixed object path"):
        Schema.transform(Schema.string().volatile(), str).validate("x")
    with pytest.raises(SchemaError, match="fixed object path"):
        Schema.object(
            {"a": Schema.object({"b": Schema.string().volatile()}).volatile()}
        ).validate({"a": {"b": "x"}})

    # A volatile field at a fixed object path resolves to a reference.
    resolved = Schema.object({"theme": Schema.string().volatile()}).validate(
        {"theme": "dark"}
    )
    assert is_volatile(resolved["theme"])
    assert resolved["theme"].get() == "dark"


def test_conformance_s2_volatile_runtime_snapshot() -> None:
    """Snapshots are frozen; functions, cycles and non-plain objects fail.

    The snapshot is deep-frozen at creation and isolated from later
    mutation of the input (oracle volatile.ts:19-26 rejects functions,
    cycles and non-plain objects; volatile.ts:62 collects entries).
    """
    ref = create_volatile({"a": {"b": [1, 2]}})
    with pytest.raises(TypeError, match="immutable"):
        ref.get()["a"]["b"].append(3)
    with pytest.raises(TypeError, match="immutable"):
        ref.get()["a"] = {}

    source = {"a": 1}
    isolated = create_volatile(source)
    source["a"] = 2
    assert isolated.get() == {"a": 1}

    with pytest.raises(TypeError, match="cannot contain functions"):
        create_volatile(lambda: 1)
    with pytest.raises(TypeError, match="must be plain objects or arrays"):
        create_volatile(object())
    cyclic: dict[str, Any] = {}
    cyclic["self"] = cyclic
    with pytest.raises(TypeError, match="cannot contain cycles"):
        create_volatile(cyclic)

    config = {
        "theme": create_volatile("dark"),
        "nested": {"label": create_volatile("x")},
        "plain": 1,
    }
    entries = volatile_entries(config)
    assert [entry["path"] for entry in entries] == [["theme"], ["nested", "label"]]
    assert entries[0]["ref"] is config["theme"]

    target = create_volatile("a")
    update_volatile(target, create_volatile("b"))
    assert target.get() == "b"


@pytest.mark.asyncio
async def test_conformance_s2_entry_fast_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A volatile-only entry update commits with no restart.

    Only an active fiber in an unchanged context takes volatile-only
    config changes without a remount (oracle loader entry.ts:141-155);
    the commit swaps the changed snapshots and emits
    ``loader/volatile-update`` with the changed paths (oracle loader
    entry.ts:160-195; event declared at loader index.ts:34).
    """
    ctx, loader, entry, fiber, consumer = await _mount_consumer(monkeypatch)
    theme_ref = fiber.config["theme"]
    assert is_volatile(theme_ref)
    assert theme_ref.get() == "dark"

    volatile_updates: list[list[list[str]]] = []
    partials: list[Any] = []
    entry.ctx.on("loader/volatile-update", lambda paths: volatile_updates.append(paths))
    ctx.on("loader/partial-dispose", lambda *args: partials.append(args))

    await entry.update({"config": _full_config("light")})

    assert len(consumer.applies) == 1  # no reload: the body did not run again
    assert entry.fiber is fiber
    assert fiber.state == FiberState.ACTIVE
    assert partials == []  # the fast path never emits partial-dispose
    assert volatile_updates == [[["theme"]]]
    assert fiber.config["theme"] is theme_ref  # same stable reference
    assert theme_ref.get() == "light"  # live value updated in place
    assert fiber.raw_config == _full_config("light")
    await loader.await_all()


@pytest.mark.asyncio
async def test_conformance_s2_entry_refuses_invalid_volatile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A volatile-only update that fails validation is refused in place.

    The commit re-validates the candidate and leaves the running
    references unchanged when validation fails (oracle loader
    entry.ts:160-175: the invalid candidate is logged and the running
    references are left alone; the raw config stays retained for the
    next activation).
    """
    _ctx, loader, entry, fiber, consumer = await _mount_consumer(monkeypatch)
    theme_ref = fiber.config["theme"]

    volatile_updates: list[list[list[str]]] = []
    entry.ctx.on("loader/volatile-update", lambda paths: volatile_updates.append(paths))

    await entry.update({"config": _full_config(123)})

    assert len(consumer.applies) == 1  # no restart on a refused commit
    assert volatile_updates == []
    assert fiber.config["theme"] is theme_ref
    assert theme_ref.get() == "dark"  # running value unchanged
    assert fiber.state == FiberState.ACTIVE
    await loader.await_all()


@pytest.mark.asyncio
async def test_conformance_s2_entry_ordinary_change_restarts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An ordinary-value change in the same update forces a full restart.

    When an ordinary effective value changed alongside the volatile
    ones, the commit refuses the fast path (oracle loader
    entry.ts:176-180) and the ordinary update lifecycle runs:
    partial-dispose, then a restart through the update waterfall
    (oracle fiber.ts:764-783).
    """
    ctx, loader, entry, fiber, consumer = await _mount_consumer(monkeypatch)
    theme_ref = fiber.config["theme"]

    volatile_updates: list[list[list[str]]] = []
    partials: list[Any] = []
    entry.ctx.on("loader/volatile-update", lambda paths: volatile_updates.append(paths))
    ctx.on("loader/partial-dispose", lambda *args: partials.append(args))

    await entry.update({"config": _full_config("light", level=4)})
    await loader.await_all()

    assert len(consumer.applies) == 2  # full restart: the body ran again
    assert len(partials) == 1
    assert volatile_updates == []  # the fast path was not taken
    assert entry.fiber is fiber  # restart reuses the same fiber
    assert fiber.state == FiberState.ACTIVE
    # The restart re-validates, so the volatile field is a fresh reference.
    assert fiber.config["theme"] is not theme_ref
    assert fiber.config["theme"].get() == "light"
    assert fiber.config["level"] == 4


@pytest.mark.asyncio
async def test_conformance_s2_direct_fiber_update_restarts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CAVEAT (pinned, not fixed): the fast path lives in Entry.update.

    A direct ``fiber.update()`` with a volatile-only change does NOT
    take the fast path — it runs the ordinary update waterfall and
    restarts the body. The loader-driven path (how config changes
    arrive) is the tested one above; this test pins the direct-call
    behaviour so the gap is visible, not silently assumed.
    """
    _ctx, loader, entry, fiber, consumer = await _mount_consumer(monkeypatch)

    volatile_updates: list[list[list[str]]] = []
    entry.ctx.on("loader/volatile-update", lambda paths: volatile_updates.append(paths))

    await fiber.update(_full_config("light"))
    await loader.await_all()

    assert len(consumer.applies) == 2  # the body reran: a restart happened
    assert volatile_updates == []  # no fast-path event on the direct call
    assert fiber.state == FiberState.ACTIVE
    assert fiber.config["theme"].get() == "light"


# ---------------------------------------------------------------------------
# S3 — natural() + step arithmetic
# ---------------------------------------------------------------------------


def test_conformance_s3_natural_shape() -> None:
    """``natural()`` is ``number().step(1).min(0)`` (oracle schema
    index.ts:588-590); 2.0 is accepted as a step-valid float."""
    schema = Schema.natural()
    assert schema.validate(2.0) == 2.0
    assert schema.validate(0) == 0.0
    with pytest.raises(SchemaError, match=r"expected number >= 0 but got -1"):
        schema.validate(-1)
    with pytest.raises(SchemaError, match=r"expected number multiple of 1 but got 1.5"):
        schema.validate(1.5)


def test_conformance_s3_decimal_shift_step() -> None:
    """Decimal steps use decimal-shift integer arithmetic (oracle schema
    index.ts:676-690), so float ``%`` artefacts do not reject 0.3."""
    schema = Schema.number().step(0.1)
    assert schema.validate(0.3) == 0.3
    with pytest.raises(
        SchemaError, match=r"expected number multiple of 0.1 but got 0.35"
    ):
        schema.validate(0.35)
    assert Schema.number().step(0.25).validate(0.5) == 0.5


# ---------------------------------------------------------------------------
# S4 — .pattern()
# ---------------------------------------------------------------------------


def test_conformance_s4_pattern_string_and_compiled() -> None:
    """``.pattern()`` takes a string or a compiled pattern with flags.

    Matching uses unanchored search, like the oracle's ``RegExp.test``
    (oracle schema index.ts:670-673); the failure message renders the
    pattern with its flags (oracle schema index.ts:189, 415-418).
    """
    assert Schema.string().pattern("b").validate("abc") == "abc"
    with pytest.raises(SchemaError, match=r"expect string to match regexp /b/"):
        Schema.string().pattern("b").validate("xyz")

    flagged = Schema.string().pattern(re.compile(r"^a+$", re.IGNORECASE))
    assert flagged.validate("AAA") == "AAA"
    payload = flagged.to_json()
    assert payload["refs"][payload["uid"]]["meta"]["pattern"] == {
        "source": "^a+$",
        "flags": "i",
    }
    with pytest.raises(SchemaError, match=r"expect string to match regexp /\^a\+\$/i"):
        flagged.validate("aab")


def test_conformance_s4_pattern_multiline_dotall_flags() -> None:
    """The ``m`` and ``s`` flags reach the compiled pattern."""
    assert (
        Schema.string().pattern(re.compile(r"^b", re.MULTILINE)).validate("a\nb")
        == "a\nb"
    )
    with pytest.raises(SchemaError, match="match regexp"):
        Schema.string().pattern(re.compile(r"^b")).validate("a\nb")
    assert (
        Schema.string().pattern(re.compile(r"a.b", re.DOTALL)).validate("a\nb")
        == "a\nb"
    )
    with pytest.raises(SchemaError, match="match regexp"):
        Schema.string().pattern(re.compile(r"a.b")).validate("a\nb")


# ---------------------------------------------------------------------------
# S5 — object semantics
# ---------------------------------------------------------------------------


def test_conformance_s5_object_merge_and_omit() -> None:
    """Unknown keys merge when not strict; absent optionals are omitted
    (oracle schema index.ts:813-821: the merge runs unless strict)."""
    schema = Schema.object(
        {"a": Schema.string(), "b": Schema.number()},
    )
    assert schema.validate({"a": "x", "extra": 1}) == {"a": "x", "extra": 1}
    assert schema.validate({"a": "x"}) == {"a": "x"}


def test_conformance_s5_strict_is_internal() -> None:
    """There is no public ``.strict()`` builder on either side: strict
    is an internal resolve parameter (oracle schema index.ts:515, 787,
    800, 821). Transform inners resolve strict, so unknown keys are
    dropped from the value the callback sees."""
    assert not hasattr(Schema, "strict")

    calls: list[dict[str, Any]] = []
    schema = Schema.transform(
        Schema.object({"a": Schema.string()}),
        lambda value: calls.append(dict(value)) or value,
    )
    result = schema.validate({"a": "x", "extra": 1})
    assert result == {"a": "x"}
    # The callback runs twice (value + adapted, oracle schema
    # index.ts:857-870); the first call carries the strict-resolved
    # value, with the unknown key dropped.
    assert calls[0] == {"a": "x"}


# ---------------------------------------------------------------------------
# S6 — union: first-success order, error shape, adapted write-back
# ---------------------------------------------------------------------------


def test_conformance_s6_union_first_success_order() -> None:
    """Union tries members in order and returns the first success
    (oracle schema index.ts:824-834)."""
    calls: list[str] = []
    schema = Schema.union(
        Schema.transform(Schema.string(), lambda v: calls.append("first") or v.upper()),
        Schema.transform(
            Schema.string(), lambda v: calls.append("second") or v.lower()
        ),
    )
    assert schema.validate("x") == "X"
    # Each transform invokes its callback twice (value + adapted, oracle
    # schema index.ts:857-870); the second member never runs at all.
    assert calls == ["first", "first"]
    assert "second" not in calls


def test_conformance_s6_union_error_shape_and_adapted_writeback() -> None:
    """Union failure is ``expected <...> but got <json>``; adapted
    values are written back into the input (oracle schema index.ts:834
    for the shape; index.ts:758-766 for the write-back)."""
    with pytest.raises(SchemaError, match=r"expected string \| integer but got 1.5"):
        Schema.union(Schema.string(), Schema.integer()).validate(1.5)
    with pytest.raises(
        SchemaError, match=r'expected string \| integer but got \{"a":1\}'
    ):
        Schema.union(Schema.string(), Schema.integer()).validate({"a": 1})

    data: dict[str, Any] = {"v": "x"}
    result = Schema.object(
        {
            "v": Schema.union(
                Schema.transform(Schema.string(), str.upper), Schema.string()
            )
        }
    ).validate(data)
    assert result == {"v": "X"}
    assert data == {"v": "X"}  # adapted value written back into the input


# ---------------------------------------------------------------------------
# S7 — number step + array-length min/max
# ---------------------------------------------------------------------------


def test_conformance_s7_number_step_from_min() -> None:
    """Step is measured from ``min`` (oracle schema index.ts:700-705)."""
    schema = Schema.number().step(2).min(10)
    assert schema.validate(12) == 12.0
    with pytest.raises(SchemaError, match=r"expected number multiple of 2 but got 11"):
        schema.validate(11)
    with pytest.raises(SchemaError, match=r"expected number >= 10 but got 8"):
        schema.validate(8)


def test_conformance_s7_array_length_min_max_and_skip_min() -> None:
    """Array length honours min/max, with the skip-min rule when the
    inner has a default (oracle schema index.ts:774-777: the min check
    is skipped when the inner default is non-null)."""
    schema = Schema.list(Schema.string()).min(2).max(3)
    assert schema.validate(["a", "b"]) == ["a", "b"]
    with pytest.raises(SchemaError, match=r"expected array length >= 2 but got 1"):
        schema.validate(["a"])
    with pytest.raises(SchemaError, match=r"expected array length <= 3 but got 4"):
        schema.validate(["a", "b", "c", "d"])

    # Control: without a defaulted inner, an empty list fails the min.
    with pytest.raises(SchemaError, match=r"expected array length >= 2 but got 0"):
        Schema.list(Schema.string()).min(2).validate([])
    # With a defaulted inner, the min check is skipped.
    assert Schema.list(Schema.string().default("x")).min(2).validate([]) == []


# ---------------------------------------------------------------------------
# S8 — dict WITHOUT key-schema (deliberate exclusion)
# ---------------------------------------------------------------------------


def test_conformance_s8_dict_has_no_key_schema() -> None:
    """Dict key-schema is deliberately excluded: no ``sKey`` API exists
    and dict validates values only. The oracle's ``sKey`` (oracle schema
    index.ts:779-790) has zero Config uses, so the port drops it."""
    assert not hasattr(Schema, "sKey")
    assert not hasattr(Schema, "key_schema")

    schema = Schema.dict(Schema.number())
    assert schema.validate({"a": 1, "b": 2}) == {"a": 1, "b": 2}
    with pytest.raises(SchemaError, match="expected number but got x"):
        schema.validate({"a": "x"})
    # Keys pass through untouched (stringified, as dict keys are here).
    assert schema.validate({1: 2}) == {"1": 2}


# ---------------------------------------------------------------------------
# S9 — const via deep equality
# ---------------------------------------------------------------------------


def test_conformance_s9_const_deep_equal_canonical() -> None:
    """``const`` matches by deep equality and returns the canonical
    value (oracle schema index.ts:655-660)."""
    canonical = {"a": [1, 2]}
    assert Schema.const(canonical).validate({"a": [1, 2]}) is canonical
    assert Schema.const([1, {"b": 2}]).validate([1, {"b": 2}]) == [1, {"b": 2}]
    with pytest.raises(SchemaError, match=r"expected 42 but got 43"):
        Schema.const(42).validate(43)


# ---------------------------------------------------------------------------
# S10 — .default() does NOT imply required
# ---------------------------------------------------------------------------


def test_conformance_s10_default_does_not_imply_required() -> None:
    """The plan's ".default() implies required" was corrected by the
    oracle: it throws only when ``meta.required`` is set (oracle schema
    index.ts:531-543). ``default("fallback")`` on None returns the
    fallback with no throw."""
    assert Schema.string().default("fallback").validate(None) == "fallback"
    assert Schema.string().default("fallback").validate("given") == "given"

    # Required is checked before default on null/absent input.
    with pytest.raises(SchemaError, match="missing required value"):
        Schema.string().required().default("x").validate(None)
    with pytest.raises(SchemaError, match="missing required value"):
        Schema.object({"a": Schema.string().required().default("x")}).validate({})


def test_conformance_s10_default_cloned_and_validated() -> None:
    """A default is cloned on each use and validated like input
    (oracle schema index.ts:542-543: ``data = clone(fallback)``)."""
    schema = Schema.list(Schema.number()).default([1, 2])
    first = schema.validate(None)
    first.append(3)
    assert schema.validate(None) == [1, 2]  # the stored default is untouched

    with pytest.raises(SchemaError, match="expected string but got 7"):
        Schema.string().default(7).validate(None)


# ---------------------------------------------------------------------------
# S11 — .role() / .loose() / .extra() / never / transform / to_json
# ---------------------------------------------------------------------------


def test_conformance_s11_role_loose_extra_never() -> None:
    """Role and extra metadata ride in the schema JSON; ``loose``
    returns the default instead of throwing; ``never`` rejects every
    non-null value (oracle schema index.ts:651-654)."""
    role_schema = Schema.string().role("input", {"placeholder": "name"})
    payload = role_schema.to_json()
    assert payload["refs"][payload["uid"]]["meta"]["role"] == "input"
    assert payload["refs"][payload["uid"]]["meta"]["extra"] == {"placeholder": "name"}

    extra_schema = Schema.string().extra("k", "v")
    extra_payload = extra_schema.to_json()
    assert extra_payload["refs"][extra_payload["uid"]]["meta"]["k"] == "v"

    assert Schema.string().loose().default("d").validate(7) == "d"
    assert Schema.string().loose().validate(7) is None

    with pytest.raises(SchemaError, match=r"expected nullable but got 1"):
        Schema.never().validate(1)
    assert Schema.never().validate(None) is None


def test_conformance_s11_transform_preserve_and_writeback() -> None:
    """Transform with ``preserve`` returns only the converted value;
    without it the adapted value is written back into the input
    (oracle schema index.ts:857-870)."""
    seen: list[Any] = []
    preserved = Schema.transform(
        Schema.string(), lambda v: seen.append(v) or v.upper(), preserve=True
    )
    assert preserved.validate("x") == "X"
    assert seen == ["x"]  # preserve: the callback runs once, no adapted value

    data: dict[str, Any] = {"n": "x"}
    result = Schema.object(
        {"n": Schema.transform(Schema.string(), str.upper)}
    ).validate(data)
    assert result == {"n": "X"}
    assert data == {"n": "X"}  # adapted value written back into the input


def test_conformance_s11_to_json_round_trip() -> None:
    """``to_json``/``from_json`` preserves shared references, and a
    transform with a callback fails loud (callbacks cannot be
    serialised)."""
    shared = Schema.string()
    schema = Schema.object({"a": shared, "b": shared})
    payload = schema.to_json()
    # One shared inner serialises once: root + shared inner only.
    assert len(payload["refs"]) == 2
    restored = Schema.from_json(payload)
    assert restored.validate({"a": "x", "b": "y"}) == {"a": "x", "b": "y"}

    with pytest.raises(
        TypeError, match="cannot serialize schema with transform callback"
    ):
        Schema.transform(Schema.number(), lambda x: x).to_json()


# ---------------------------------------------------------------------------
# S12 — string / boolean / any / .hidden
# ---------------------------------------------------------------------------


def test_conformance_s12_string_boolean_any_hidden() -> None:
    """String length bounds, boolean strictness, ``any`` passthrough,
    and ``.hidden()`` (hidden fields are omitted from JSON Schema
    export)."""
    bounded = Schema.string().min(2).max(4)
    assert bounded.validate("abc") == "abc"
    with pytest.raises(SchemaError, match=r"expected string length >= 2 but got 1"):
        bounded.validate("a")
    with pytest.raises(SchemaError, match=r"expected string length <= 4 but got 5"):
        bounded.validate("abcde")

    assert Schema.boolean().validate(True) is True
    with pytest.raises(SchemaError, match=r"expected boolean but got 1"):
        Schema.boolean().validate(1)

    sentinel = object()
    assert Schema.any().validate(sentinel) is sentinel

    exported = Schema.object(
        {"vis": Schema.string(), "hid": Schema.string().hidden()}
    ).to_json_schema()
    assert "vis" in exported["properties"]
    assert "hid" not in exported["properties"]


# ---------------------------------------------------------------------------
# S13 — EffectMeta
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_conformance_s13_effect_meta_tree() -> None:
    """``Fiber.effect`` builds ``{label, children}`` meta and
    ``Context.effect`` attaches it, so ``getEffects()`` returns the
    full diagnostics tree (oracle fiber.ts:96-100, 444, 566-571)."""
    ctx = Context()
    probe = EffectProbe()
    fiber = ctx.use(probe)
    await ctx.flush()

    assert fiber.state == FiberState.ACTIVE
    assert fiber.getEffects() == [
        {"label": "probe-a", "children": []},
        {"label": "probe-b", "children": []},
        {"label": "anonymous", "children": []},
    ]


@pytest.mark.asyncio
async def test_conformance_s13_fiber_effect_nests_children() -> None:
    """A ``Fiber.effect`` wrapping a context-registered disposer nests
    that disposer's meta under its own ``children``."""
    ctx = Context()
    probe = EffectProbe()
    fiber = ctx.use(probe)
    await ctx.flush()

    fiber.effect(lambda: probe.disposers["a"], label="probe-parent")
    assert fiber.getEffects() == [
        {"label": "probe-b", "children": []},
        {"label": "anonymous", "children": []},
        {
            "label": "probe-parent",
            "children": [{"label": "probe-a", "children": []}],
        },
    ]

    fiber.effect(lambda: lambda: None, label="direct")
    assert fiber.getEffects()[-1] == {"label": "direct", "children": []}


# ---------------------------------------------------------------------------
# S14 — excluded constructs fail loud
# ---------------------------------------------------------------------------


def test_conformance_s14_excluded_factories_absent() -> None:
    """No factory exists for any excluded construct: definition time
    fails with ``AttributeError`` rather than silently building
    something else."""
    for name in [
        "intersect",
        "tuple",
        "date",
        "lazy",
        "is",
        "function",
        "regExp",
        "arrayBuffer",
        "bitset",
        "percent",
        "simplify",
        "i18n",
        "autofix",
        "extend",
        "from",
    ]:
        assert not hasattr(Schema, name), f"Schema.{name} must not exist"
        with pytest.raises(AttributeError):
            getattr(Schema, name)


def test_conformance_s14_unported_type_fails_loud() -> None:
    """The resolver raises ``unsupported type "<t>"`` for an unported
    construct (oracle schema index.ts:547-549)."""
    with pytest.raises(SchemaError, match='unsupported type "tuple"'):
        Schema("tuple").validate([1])
    with pytest.raises(SchemaError, match='unsupported type "intersect"'):
        Schema("intersect").validate(1)
    with pytest.raises(SchemaError, match='unsupported type "date"'):
        Schema("date").validate("2026-01-01")


_L_VOLATILE_SCHEMA = Schema.object(
    {
        "theme": Schema.string().volatile(),
        "level": Schema.number(),
        "nested": Schema.object(
            {
                "label": Schema.string().volatile(),
                "count": Schema.number(),
            }
        ),
    }
)


def _l_repo_root() -> Path:
    """Return the anima repo root (two levels above this file)."""
    return Path(__file__).resolve().parents[2]


def _l_install_module(monkeypatch: pytest.MonkeyPatch, name: str, **attrs: Any) -> Any:
    mod = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(mod, key, value)
    monkeypatch.setitem(sys.modules, name, mod)
    return mod


def _l_recorder(calls: list[Any], key: str = "comp") -> Any:
    def comp(ctx: Context, config: Any = None) -> None:
        calls.append((key, dict(config) if isinstance(config, dict) else config))

    return comp


def _l_row(name: str = "lconf_tree_mod:comp", **extra: Any) -> dict[str, Any]:
    row: dict[str, Any] = {"name": name}
    row.update(extra)
    return row


class _LMemoryTree(EntryTree):
    """In-memory tree subclass; counts writes and records commits."""

    def __init__(self, ctx: Context, loader: Any = None) -> None:
        super().__init__(ctx, loader=loader)
        self.writes = 0
        self.commits: list[Any] = []

    def write(self) -> None:
        self.writes += 1

    def commit(self, change: Any) -> None:
        self.commits.append(change)


@pytest.fixture
def l_tree_mod(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Recording component, a failing component, and a non-component."""
    calls: list[Any] = []

    def failing(ctx: Context, config: Any = None) -> None:
        raise RuntimeError("boom")

    module = _l_install_module(
        monkeypatch,
        "lconf_tree_mod",
        comp=_l_recorder(calls, "comp"),
        failing=failing,
        not_a_component=42,
    )
    module.calls = calls  # type: ignore[attr-defined]
    return module


@pytest.fixture
def l_inc_mod(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Recording component for file-backed include tests."""
    calls: list[Any] = []

    def comp(ctx: Context, config: Any = None) -> None:
        calls.append(dict(config) if isinstance(config, dict) else config)

    module = _l_install_module(monkeypatch, "lconf_inc_mod", comp=comp)
    module.calls = calls  # type: ignore[attr-defined]
    return module


def _l_warn_sink(warnings: list[str]) -> Any:
    """Capture warn args as plain text."""

    def _warn(*args: Any) -> None:
        warnings.append(" ".join(str(a) for a in args))

    return _warn


def _l_logger_text(ctx: Context) -> str:
    """All text the loader logger recorded, via the LoggerService buffer."""
    svc = ctx.logger
    return " ".join(str(a) for m in svc.buffer for a in m.args)


def _l_rows(*levels: int) -> str:
    """YAML entry list of recording components with the given levels."""
    return "".join(
        f"- name: lconf_inc_mod:comp\n  id: e{i}\n  config:\n    level: {level}\n"
        for i, level in enumerate(levels)
    )


async def _l_boot_include(
    tmp_path: Path,
    content: str | None,
    *,
    filename: str = "cfg.yaml",
    patches: list[dict[str, Any]] | None = None,
    extra: dict[str, Any] | None = None,
) -> tuple[Context, LoaderService, str, Any, Include]:
    """Mount an Include entry on ``content``; return its live parts.

    ``content`` None leaves an existing file untouched (re-read boot).
    """
    file = tmp_path / filename
    if content is not None:
        file.write_text(content, encoding="utf-8")
    ctx = Context()
    loader = LoaderService(ctx)
    config: dict[str, Any] = {"path": str(file)}
    if patches is not None:
        config["patches"] = patches
    if extra:
        config.update(extra)
    inc_id = await loader.create({"name": "include", "config": config})
    await loader.await_all()
    await ctx.flush()
    entry = loader.resolve(inc_id)
    inc = entry.ctx.get("include", strict=False)
    assert isinstance(inc, Include)
    return ctx, loader, inc_id, entry, inc


async def _l_settle_include(inc: Include, loader: LoaderService) -> None:
    """Wait for the Include drain, its ctx tasks, and the loader tree."""
    await inc.flush()
    await inc.ctx.flush()
    await loader.await_all()


def _l_write(tmp_path: Path, name: str, source: str) -> Path:
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
    return path


def _l_load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, str(path))
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    mod.__spec__ = spec
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _l_bump(path: Path) -> None:
    now = time.time() + 2
    os.utime(path, (now, now))


def _l_pop(*names: str) -> None:
    for name in names:
        sys.modules.pop(name, None)


# ---------------------------------------------------------------------------
# L1 — EntryTree exists
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_conformance_l1_entry_tree_store_root_entries(
    l_tree_mod: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """EntryTree: store, root EntryGroup, recursive entries().

    The tree owns a flat store keyed by short id and a root group
    (oracle tree.ts:9-136); entries() recurses into nested subtrees
    owned by entries.
    """
    ctx = Context()
    tree = _LMemoryTree(ctx)
    assert tree.store == {}
    assert isinstance(tree.root, EntryGroup)
    assert tree.root.data == []
    assert list(tree.entries()) == []

    loader = LoaderService(ctx)
    await loader.create(_l_row(id="a"))
    await loader.create(_l_row(id="b"))
    await loader.await_all()
    assert set(loader.store) == {"a", "b"}
    assert [e.id for e in loader.entries()] == ["a", "b"]

    # A nested subtree contributes its entries with prefixed ids.
    subtrees: dict[str, Any] = {}

    def owner_comp(c: Context, config: Any = None) -> None:
        subtrees["sub"] = _LMemoryTree(c, loader=loader)

    _l_install_module(
        monkeypatch,
        "lconf_nested_mod",
        owner=owner_comp,
        leaf=_l_recorder([], "leaf"),
    )
    await loader.create({"id": "sub", "name": "lconf_nested_mod:owner"})
    await ctx.flush()
    await loader.await_all()
    await subtrees["sub"].create({"id": "deep", "name": "lconf_nested_mod:leaf"})
    await loader.await_all()
    assert [e.id for e in loader.entries()] == ["a", "b", "sub", "sub:deep"]


@pytest.mark.asyncio
async def test_conformance_l1_get_tasks_and_await_all(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """get_tasks() reports in-flight work; await_all() waits for it all.

    While a component body is blocked, the tree reports a pending
    task; once it settles, the task list is empty and a second
    await_all() returns immediately (oracle tree.ts:9-136).
    """
    ctx = Context()
    loader = LoaderService(ctx)
    started = asyncio.Event()
    release = asyncio.Event()

    def slow_comp(c: Context, config: Any = None) -> Any:
        async def _run() -> None:
            started.set()
            await release.wait()

        return _run()

    _l_install_module(monkeypatch, "lconf_slow_mod", slow=slow_comp)
    create_task = asyncio.get_running_loop().create_task(
        loader.create({"id": "slow", "name": "lconf_slow_mod:slow"})
    )
    for _ in range(50):
        await asyncio.sleep(0)
        if started.is_set():
            break
    assert started.is_set()
    assert loader.get_tasks()
    release.set()
    await create_task
    await loader.await_all()
    assert loader.get_tasks() == []
    await loader.await_all()
    assert loader.resolve("slow").fiber is not None
    assert loader.resolve("slow").fiber.state == FiberState.ACTIVE


# ---------------------------------------------------------------------------
# L2 — positions / insert ordering / cross-group move
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_conformance_l2_positions_splice_semantics(
    l_tree_mod: Any,
) -> None:
    """Insert positions follow splice semantics (oracle tree.ts:80).

    None or infinity appends; negatives count from the end;
    out-of-range positions clamp to the ends.
    """
    ctx = Context()
    loader = LoaderService(ctx)
    await loader.create(_l_row(id="a"))
    await loader.create(_l_row(id="b"))
    await loader.create(_l_row(id="c"))
    await loader.create(_l_row(id="front"), position=0)
    assert [r["id"] for r in loader.root.data] == ["front", "a", "b", "c"]
    await loader.create(_l_row(id="second-last"), position=-1)
    assert [r["id"] for r in loader.root.data] == [
        "front",
        "a",
        "b",
        "second-last",
        "c",
    ]
    await loader.create(_l_row(id="clamped"), position=999)
    assert [r["id"] for r in loader.root.data][-1] == "clamped"
    await loader.create(_l_row(id="appended"), position=None)
    assert [r["id"] for r in loader.root.data][-1] == "appended"
    await loader.await_all()


@pytest.mark.asyncio
async def test_conformance_l2_cross_group_move(l_tree_mod: Any) -> None:
    """An entry moves between groups, keeping its fiber (oracle tree.ts:99).

    The row is unlinked from the source group and spliced into the
    target at the given position; the move replays no config, so the
    fiber is retained, not restarted.
    """
    ctx = Context()
    loader = LoaderService(ctx)
    await loader.create(
        {
            "id": "g",
            "name": "group",
            "group": True,
            "config": [_l_row(id="x")],
        }
    )
    await loader.create(_l_row(id="mover"))
    await loader.await_all()
    mover = loader.resolve("mover")
    fiber = mover.fiber
    assert fiber is not None and fiber.state == FiberState.ACTIVE

    await loader.update("mover", {}, parent="g", position=0)
    await loader.await_all()
    group = loader.resolve("g")
    assert group.subgroup is not None
    assert [r["id"] for r in group.subgroup.data] == ["mover", "x"]
    assert [r["id"] for r in loader.root.data] == ["g"]
    assert mover.parent is group.subgroup
    assert mover.fiber is fiber

    await loader.update("mover", {}, parent=None, position=-1)
    await loader.await_all()
    assert [r["id"] for r in loader.root.data] == ["mover", "g"]
    assert [r["id"] for r in group.subgroup.data] == ["x"]
    assert mover.parent is loader.root
    assert mover.fiber is fiber


# ---------------------------------------------------------------------------
# L3 — resolve / create / remove / update / write / enable
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_conformance_l3_resolve_colon_separator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """resolve() splits on ":" and walks subtrees; unresolvable raises.

    Colon ids cross subtrees; group children resolve by short id
    (oracle tree.ts:58). A missing part at any depth raises with the
    full id in the message.
    """
    ctx = Context()
    loader = LoaderService(ctx)
    subtrees: dict[str, Any] = {}

    def owner_comp(c: Context, config: Any = None) -> None:
        subtrees["a"] = _LMemoryTree(c, loader=loader)

    def mid_comp(c: Context, config: Any = None) -> None:
        subtrees["b"] = _LMemoryTree(c, loader=loader)

    _l_install_module(
        monkeypatch,
        "lconf_nested3_mod",
        owner=owner_comp,
        mid=mid_comp,
        leaf=_l_recorder([], "leaf"),
    )
    await loader.create({"id": "a", "name": "lconf_nested3_mod:owner"})
    await ctx.flush()
    await loader.await_all()
    await subtrees["a"].create({"id": "b", "name": "lconf_nested3_mod:mid"})
    await loader.await_all()
    await subtrees["b"].create({"id": "c", "name": "lconf_nested3_mod:leaf"})
    await loader.await_all()

    deep = loader.resolve("a:b:c")
    assert deep.id == "a:b:c"
    assert deep.options["name"] == "lconf_nested3_mod:leaf"
    with pytest.raises(ValueError, match="cannot resolve entry a:b:nope"):
        loader.resolve("a:b:nope")
    with pytest.raises(ValueError, match="cannot resolve entry a:nope:c"):
        loader.resolve("a:nope:c")
    with pytest.raises(ValueError, match="cannot resolve entry nope"):
        loader.resolve("nope")


@pytest.mark.asyncio
async def test_conformance_l3_create_remove_update_and_write(
    l_tree_mod: Any,
) -> None:
    """create / remove / update merge options; write() persists the tree.

    Every mutation goes through the write() abstraction (a no-op
    counter here, the file drain for a file-backed tree). Updating
    merges: a None value deletes its key and is never stored.
    """
    ctx = Context()
    loader = LoaderService(ctx)
    tree = _LMemoryTree(ctx, loader=loader)
    await tree.create(_l_row(id="a", config={"level": 1}))
    await tree.await_all()
    assert tree.writes == 1
    assert tree.store["a"].options["config"] == {"level": 1}
    assert tree.store["a"].fiber is not None
    assert tree.store["a"].fiber.state == FiberState.ACTIVE

    await tree.update("a", {"config": {"level": 2}})
    await loader.await_all()
    assert tree.writes == 2
    assert tree.store["a"].options["config"] == {"level": 2}

    await tree.update("a", {"config": None})
    assert "config" not in tree.store["a"].options

    tree.remove("a")
    assert tree.writes == 4
    assert "a" not in tree.store
    assert tree.root.data == []


@pytest.mark.asyncio
async def test_conformance_l3_enable_via_update_no_duplicates(
    l_tree_mod: Any,
) -> None:
    """enable() goes through update(): no duplicate rows or fibers.

    The old duplicate-append bug is gone: disabling then enabling
    twice leaves exactly one row and one ACTIVE fiber, and the
    component body runs once per mount.
    """
    ctx = Context()
    loader = LoaderService(ctx)
    loader.load([{"id": "toggle", "name": "lconf_tree_mod:comp"}])
    await ctx.flush()
    assert len(loader.root.data) == 1

    for _ in range(2):
        await loader.disable("toggle")
        entry = loader.resolve("toggle")
        assert entry.fiber is not None
        assert entry.fiber.state == FiberState.DISPOSED
        assert len(loader.root.data) == 1
        await loader.enable("toggle")
        assert len(loader.root.data) == 1
        assert entry.fiber is not None
        assert entry.fiber.state == FiberState.ACTIVE

    assert len([e for e in loader.store if e == "toggle"]) == 1
    assert len(l_tree_mod.calls) == 3  # initial mount + two enables


# ---------------------------------------------------------------------------
# L4 — entry identity / ensure_id
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_conformance_l4_ensure_id_random_hex_collision_checked(
    l_tree_mod: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ensure_id assigns a random 8-hex id, collision-checked (oracle tree.ts:52).

    Two anonymous entries get distinct ids; a pre-existing id is left
    untouched; a colliding candidate is retried, never reused.
    """
    ctx = Context()
    loader = LoaderService(ctx)
    first = loader.ensure_id({})
    second = loader.ensure_id({})
    assert re.fullmatch(r"[0-9a-f]{8}", first)
    assert re.fullmatch(r"[0-9a-f]{8}", second)
    assert first != second

    await loader.create(_l_row(id="fixed"))
    assert loader.ensure_id({"id": "fixed"}) == "fixed"

    await loader.create(_l_row(id="aaaaaaaa"))
    candidates = iter(["aaaaaaaa", "bbbbbbbb"])
    monkeypatch.setattr(secrets, "token_hex", lambda n: next(candidates))
    assert loader.ensure_id({}) == "bbbbbbbb"


@pytest.mark.asyncio
async def test_conformance_l4_subtree_entry_id_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Subtree entries prefix the owning entry's id with ":" (oracle entry.ts:62).

    The prefix applies when the entry's tree has an owning entry;
    group children in the same tree keep their short ids.
    """
    ctx = Context()
    loader = LoaderService(ctx)
    subtrees: dict[str, Any] = {}

    def owner_comp(c: Context, config: Any = None) -> None:
        subtrees["a"] = _LMemoryTree(c, loader=loader)

    def mid_comp(c: Context, config: Any = None) -> None:
        subtrees["b"] = _LMemoryTree(c, loader=loader)

    _l_install_module(
        monkeypatch,
        "lconf_nested4_mod",
        owner=owner_comp,
        mid=mid_comp,
        leaf=_l_recorder([], "leaf"),
    )
    await loader.create({"id": "a", "name": "lconf_nested4_mod:owner"})
    await ctx.flush()
    await loader.await_all()
    await subtrees["a"].create({"id": "b", "name": "lconf_nested4_mod:mid"})
    await loader.await_all()
    await subtrees["b"].create({"id": "c", "name": "lconf_nested4_mod:leaf"})
    await loader.await_all()

    deep = loader.resolve("a:b:c")
    assert deep.id == "a:b:c"
    assert deep.options["id"] == "c"
    mid = loader.resolve("a:b")
    assert mid.id == "a:b"
    assert loader.resolve("a").id == "a"


# ---------------------------------------------------------------------------
# L5 — JsExpr detection
# ---------------------------------------------------------------------------


def test_conformance_l5_is_js_expr_strict_string_check() -> None:
    """is_js_expr: a dict with a string __jsExpr (oracle utils.ts:25-32).

    Deliberate strictness: anima also checks the value is a string,
    where the oracle does not. A non-string value is not an
    expression node, so it is never evaluated.
    """
    assert is_js_expr({"__jsExpr": "process.env.X"}) is True
    assert is_js_expr({"__jsExpr": "x", "other": 1}) is True
    assert is_js_expr({"__jsExpr": 42}) is False
    assert is_js_expr({"__jsExpr": None}) is False
    assert is_js_expr({"__jsExpr": ["x"]}) is False
    assert is_js_expr({}) is False
    assert is_js_expr({"__jsExpr2": "x"}) is False
    assert is_js_expr("process.env.X") is False
    assert is_js_expr(None) is False
    assert is_js_expr([]) is False


# ---------------------------------------------------------------------------
# L6 — restricted JsExpr evaluation
# ---------------------------------------------------------------------------


def test_conformance_l6_restricted_evaluator_attested_patterns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The restricted evaluator covers the attested patterns (ADR 0006).

    Deliberate divergence: the oracle evaluates with a full JS engine;
    anima evaluates a restricted set only — environment access,
    context access, process accessors, String/Number/JSON.parse,
    literals, comparisons, ternaries, boolean operators, nullish
    coalescing, optional chaining. No JS engine is embedded.
    """
    ctx = Context()
    monkeypatch.setenv("LCONF_JSEXPR_MODE", "workspace-write")
    assert (
        evaluate(ctx, "process.env.LCONF_JSEXPR_MODE ?? 'workspace-write'")
        == "workspace-write"
    )
    assert (
        evaluate(ctx, "process.env.LCONF_JSEXPR_MISSING ?? process.cwd()")
        == os.getcwd()
    )
    assert evaluate(ctx, "process.platform === 'win32' ? 'a' : 'b'") == (
        "a" if sys.platform == "win32" else "b"
    )
    ctx.provide("profileContext", {"name": "desktop"})
    assert evaluate(ctx, "!ctx.get('profileContext')") is False
    assert evaluate(ctx, "ctx.get('profileContext')?.name !== 'desktop'") is False
    assert evaluate(ctx, "ctx.get('missing') ?? 'fallback'") == "fallback"
    assert evaluate(ctx, "String(42)") == "42"
    assert evaluate(ctx, "Number('3081')") == 3081
    assert evaluate(ctx, "JSON.parse('{\"a\": 1}')") == {"a": 1}
    assert evaluate(ctx, "1 < 2 && 'x' === 'x'") is True


def test_conformance_l6_out_of_scope_fails_loud() -> None:
    """Anything outside the restricted scope fails loud with JsExprError.

    Arithmetic, home-path helpers, array methods, template literals
    and unknown identifiers are all out of scope — never silently
    evaluated, never silently skipped.
    """
    ctx = Context()
    with pytest.raises(JsExprError, match="unexpected character"):
        evaluate(ctx, "1 + 1")
    with pytest.raises(JsExprError, match="unsupported call"):
        evaluate(ctx, "homePath('sessions')")
    with pytest.raises(JsExprError, match="unexpected character"):
        evaluate(ctx, "['a'].includes('a')")
    with pytest.raises(JsExprError, match="template literals"):
        evaluate(ctx, "`hello ${name}`")
    with pytest.raises(JsExprError, match="unknown identifier"):
        evaluate(ctx, "foo")
    with pytest.raises(JsExprError, match="unsupported call"):
        evaluate(ctx, "foo('x')")


def test_conformance_l6_interpolate_no_mutation_and_call_sites(
    l_tree_mod: Any,
) -> None:
    """interpolate() never mutates its input; call sites are selective.

    interpolate() recurses and returns new dicts/lists, so the raw
    nodes stay in the stored options for write-back. The loader's
    internal/config hook evaluates entry configs but skips tree
    carriers and entry-less fibers, and Entry.disabledOf evaluates a
    JsExpr disabled flag (oracle utils.ts:6-22, loader internal/config
    + Entry.disabledOf call sites).
    """
    ctx = Context()
    ctx.provide("profileContext", {"name": "desktop"})
    value = {
        "mode": {"__jsExpr": "process.env.LCONF_MISSING ?? 'workspace-write'"},
        "ports": [{"__jsExpr": "Number('3081')"}, 8080],
        "nested": {"flag": {"__jsExpr": "!ctx.get('profileContext')"}},
        "plain": "keep",
    }
    result = interpolate(ctx, value)
    assert result == {
        "mode": "workspace-write",
        "ports": [3081, 8080],
        "nested": {"flag": False},
        "plain": "keep",
    }
    assert value["mode"] == {
        "__jsExpr": "process.env.LCONF_MISSING ?? 'workspace-write'"
    }
    assert value["ports"][0] == {"__jsExpr": "Number('3081')"}
    assert result is not value
    assert result["ports"] is not value["ports"]
    assert result["nested"] is not value["nested"]


@pytest.mark.asyncio
async def test_conformance_l6_config_hook_evaluates_entries_only(
    l_tree_mod: Any,
) -> None:
    """The internal/config hook evaluates entry configs, nothing else."""
    ctx = Context()
    loader = LoaderService(ctx)
    await loader.create(
        {
            "id": "e",
            "name": "lconf_tree_mod:comp",
            "config": {"mode": {"__jsExpr": "process.env.LCONF_X ?? 'evald'"}},
        }
    )
    await loader.await_all()
    entry = loader.resolve("e")
    assert entry.fiber is not None and entry.fiber.state == FiberState.ACTIVE
    assert entry.fiber.config["mode"] == "evald"
    # The stored options keep the raw node for write-back.
    assert entry.options["config"]["mode"] == {
        "__jsExpr": "process.env.LCONF_X ?? 'evald'"
    }

    # An entry-less fiber keeps its config literal: no entry, no hook.
    calls: list[Any] = []

    def direct_comp(c: Context, config: Any = None) -> None:
        calls.append(dict(config) if isinstance(config, dict) else config)

    fiber = ctx.use(direct_comp, mode={"__jsExpr": "process.env.LCONF_X ?? 'x'"})
    await ctx.flush()
    assert fiber.state == FiberState.ACTIVE
    assert fiber.config["mode"] == {"__jsExpr": "process.env.LCONF_X ?? 'x'"}


@pytest.mark.asyncio
async def test_conformance_l6_disabled_of_evaluates_js_expr(
    l_tree_mod: Any,
) -> None:
    """Entry.disabledOf evaluates a JsExpr disabled flag."""
    ctx = Context()
    loader = LoaderService(ctx)
    await loader.create(
        {
            "id": "d",
            "name": "lconf_tree_mod:comp",
            "disabled": {"__jsExpr": "1 === 1"},
        }
    )
    await loader.await_all()
    entry = loader.resolve("d")
    assert entry.disabled is True
    assert entry.fiber is None  # never mounted while disabled
    assert l_tree_mod.calls == []

    await loader.update("d", {"disabled": False})
    await loader.await_all()
    assert entry.disabled is False
    assert entry.fiber is not None
    assert entry.fiber.state == FiberState.ACTIVE
    assert len(l_tree_mod.calls) == 1


# ---------------------------------------------------------------------------
# L7 — !!js YAML tag + round-trip
# ---------------------------------------------------------------------------


def test_conformance_l7_js_yaml_tag_round_trip() -> None:
    """!!js scalars load to expression nodes and dump back (oracle include:9-23)."""
    text = (
        "entries:\n"
        "  - name: comp\n"
        "    config:\n"
        "      mode: !!js process.env.MODE ?? 'x'\n"
    )
    data = load_yaml(io.StringIO(text))
    assert data == {
        "entries": [
            {
                "name": "comp",
                "config": {"mode": {"__jsExpr": "process.env.MODE ?? 'x'"}},
            }
        ]
    }

    original = [
        {
            "id": "a",
            "name": "comp",
            "config": {
                "mode": {"__jsExpr": "process.env.MODE ?? 'workspace-write'"},
                "port": {"__jsExpr": "Number(process.env.PORT ?? 8080)"},
            },
            "disabled": {"__jsExpr": "ctx.disabled"},
        }
    ]
    buf = io.StringIO()
    dump_yaml(original, buf)
    dumped = buf.getvalue()
    assert "!!js" in dumped
    assert "process.env.MODE" in dumped
    assert load_yaml(io.StringIO(dumped)) == original
    # A second round-trip is stable.
    buf2 = io.StringIO()
    dump_yaml(load_yaml(io.StringIO(dumped)), buf2)
    assert load_yaml(io.StringIO(buf2.getvalue())) == original
    # Only single-key {"__jsExpr": str} dicts become !!js scalars.
    multi = [{"config": {"__jsExpr": "process.env.X", "other": 1}}]
    buf3 = io.StringIO()
    dump_yaml(multi, buf3)
    assert "!!js" not in buf3.getvalue()
    assert load_yaml(io.StringIO(buf3.getvalue())) == multi


@pytest.mark.asyncio
async def test_conformance_l7_file_load_save_reload(
    tmp_path: Path, l_tree_mod: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """File load → save → reload preserves !!js nodes and re-evaluates them."""
    entries = [
        {
            "id": "a",
            "name": "lconf_tree_mod:comp",
            "config": {"mode": {"__jsExpr": "process.env.LCONF_L7_MODE ?? 'dflt'"}},
        }
    ]
    source = tmp_path / "profile.yaml"
    buf = io.StringIO()
    dump_yaml(entries, buf)
    source.write_text(buf.getvalue(), encoding="utf-8")

    ctx = Context()
    loader = load_profile(ctx, source)
    await ctx.flush()
    entry = loader.resolve("a")
    assert entry.fiber is not None and entry.fiber.state == FiberState.ACTIVE
    assert entry.fiber.config["mode"] == "dflt"

    saved = tmp_path / "saved.yaml"
    loader.save(saved)
    reloaded = load_yaml(saved.read_text(encoding="utf-8"))
    assert reloaded[0]["config"]["mode"] == {
        "__jsExpr": "process.env.LCONF_L7_MODE ?? 'dflt'"
    }

    monkeypatch.setenv("LCONF_L7_MODE", "live")
    ctx2 = Context()
    loader2 = load_profile(ctx2, saved)
    await ctx2.flush()
    entry2 = loader2.resolve("a")
    assert entry2.fiber is not None
    assert entry2.fiber.config["mode"] == "live"


# ---------------------------------------------------------------------------
# L8 — Include as file-backed EntryTree
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_conformance_l8_supported_extensions_only(
    tmp_path: Path, l_inc_mod: Any
) -> None:
    """Only .json / .yaml / .yml are supported (oracle include:150-343)."""
    ctx = Context()
    loader = LoaderService(ctx)
    bad = tmp_path / "cfg.txt"
    bad.write_text("[]", encoding="utf-8")
    inc_id = await loader.create({"name": "include", "config": {"path": str(bad)}})
    await loader.await_all()
    await ctx.flush()
    entry = loader.resolve(inc_id)
    assert entry.fiber is not None
    assert entry.fiber.state == FiberState.FAILED
    assert isinstance(entry.fiber.error, IncludeError)
    assert 'extension ".txt" not supported' in str(entry.fiber.error)

    _ctx, _loader, _id, _entry, inc = await _l_boot_include(
        tmp_path,
        json.dumps(
            [{"id": "e0", "name": "lconf_inc_mod:comp", "config": {"level": 1}}]
        ),
        filename="cfg.json",
    )
    assert inc.root.data[0]["config"]["level"] == 1
    _ctx2, _loader2, _id2, _entry2, inc2 = await _l_boot_include(
        tmp_path, _l_rows(2), filename="other.yml"
    )
    assert inc2.root.data[0]["config"]["level"] == 2


@pytest.mark.asyncio
async def test_conformance_l8_read_noop_and_missing_file_fallback(
    tmp_path: Path, l_inc_mod: Any
) -> None:
    """read() is a no-op on unchanged content; only ENOENT falls back.

    A re-read of identical content leaves the tree and the mounted
    components untouched. A missing file falls back to ``initial``
    (and writes it); a missing file without ``initial`` fails loud.
    """
    _ctx, _loader, _inc_id, _entry, inc = await _l_boot_include(tmp_path, _l_rows(1, 2))
    calls_before = list(l_inc_mod.calls)
    await inc.refresh()
    await inc.refresh()
    assert l_inc_mod.calls == calls_before
    assert [e["id"] for e in inc.root.data] == ["e0", "e1"]

    missing = tmp_path / "created.yaml"
    ctx2 = Context()
    loader2 = LoaderService(ctx2)
    inc2_id = await loader2.create(
        {
            "name": "include",
            "config": {
                "path": str(missing),
                "initial": [
                    {"id": "i0", "name": "lconf_inc_mod:comp", "config": {"level": 7}}
                ],
            },
        }
    )
    await loader2.await_all()
    await ctx2.flush()
    assert missing.exists()
    entry2 = loader2.resolve(inc2_id)
    inc2 = entry2.ctx.get("include", strict=False)
    assert isinstance(inc2, Include)
    assert inc2.root.data[0]["config"]["level"] == 7

    ctx3 = Context()
    loader3 = LoaderService(ctx3)
    inc3_id = await loader3.create(
        {"name": "include", "config": {"path": str(tmp_path / "gone.yaml")}}
    )
    await loader3.await_all()
    await ctx3.flush()
    entry3 = loader3.resolve(inc3_id)
    assert entry3.fiber is not None
    assert entry3.fiber.state == FiberState.FAILED
    assert isinstance(entry3.fiber.error, IncludeError)
    assert "config file not found" in str(entry3.fiber.error)


@pytest.mark.asyncio
async def test_conformance_l8_non_array_top_level_fails_loud(
    tmp_path: Path, l_inc_mod: Any
) -> None:
    """A non-array top level fails loud; an invalid file never falls back.

    Only a missing file falls back to ``initial``: an existing but
    invalid file must fail with its real parse error, never be
    mislabelled as absent or silently overwritten.
    """
    ctx = Context()
    loader = LoaderService(ctx)
    file = tmp_path / "cfg.yaml"
    file.write_text("name: not-a-list\n", encoding="utf-8")
    inc_id = await loader.create({"name": "include", "config": {"path": str(file)}})
    await loader.await_all()
    await ctx.flush()
    entry = loader.resolve(inc_id)
    assert entry.fiber is not None
    assert entry.fiber.state == FiberState.FAILED
    assert isinstance(entry.fiber.error, TypeError)
    assert "top-level array" in str(entry.fiber.error)

    bad = tmp_path / "bad.yaml"
    bad.write_text(":\n  bad: [", encoding="utf-8")
    ctx2 = Context()
    loader2 = LoaderService(ctx2)
    inc2_id = await loader2.create(
        {
            "name": "include",
            "config": {
                "path": str(bad),
                "initial": [{"id": "i", "name": "lconf_inc_mod:comp"}],
            },
        }
    )
    await loader2.await_all()
    await ctx2.flush()
    entry2 = loader2.resolve(inc2_id)
    assert entry2.fiber is not None
    assert entry2.fiber.state == FiberState.FAILED
    assert isinstance(entry2.fiber.error, yaml.YAMLError)
    assert bad.read_text(encoding="utf-8") == ":\n  bad: ["


@pytest.mark.asyncio
async def test_conformance_l8_refresh_warns_and_keeps_last_good_tree(
    tmp_path: Path, l_inc_mod: Any
) -> None:
    """refresh() on a bad file warns and keeps the last good tree."""
    ctx, _loader, _inc_id, _entry, inc = await _l_boot_include(tmp_path, _l_rows(1, 2))
    file = tmp_path / "cfg.yaml"
    file.write_text("name: not-a-list\n", encoding="utf-8")
    await inc.refresh()
    assert "top-level array" in _l_logger_text(ctx)
    assert [e["id"] for e in inc.root.data] == ["e0", "e1"]
    assert inc.root.data[0]["config"]["level"] == 1
    assert l_inc_mod.calls == [{"level": 1}, {"level": 2}]


@pytest.mark.asyncio
async def test_conformance_l8_atomic_write_retries_and_no_tmp_left(
    tmp_path: Path, l_inc_mod: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Writes go through .tmp + rename, retrying transient lock errors.

    A rename failing with a transient error (EACCES/EBUSY/EPERM) is
    retried up to 10 times; no temp file is left behind either way.
    """
    _ctx, loader, inc_id, _entry, inc = await _l_boot_include(tmp_path, _l_rows(1, 2))
    real_replace = os.replace
    attempts = {"n": 0}

    def _flaky(src: Any, dst: Any) -> None:
        attempts["n"] += 1
        if attempts["n"] <= 2:
            raise OSError(errno.EBUSY, "resource busy")
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", _flaky)
    await loader.update(f"{inc_id}:e1", {"config": {"level": 20}})
    await _l_settle_include(inc, loader)
    assert attempts["n"] == 3
    file = tmp_path / "cfg.yaml"
    rows = load_yaml(file.read_text(encoding="utf-8"))
    assert rows[1]["config"]["level"] == 20
    assert list(tmp_path.glob(".*.tmp")) == []


@pytest.mark.asyncio
async def test_conformance_l8_write_queue_coalesces_to_final_state(
    tmp_path: Path, l_inc_mod: Any
) -> None:
    """The write queue serializes through one drain: rapid updates coalesce.

    Five sequential runtime updates land as one final file state —
    the last one — never an intermediate state, and never a lost one.
    """
    _ctx, loader, inc_id, _entry, inc = await _l_boot_include(tmp_path, _l_rows(1))
    for level in (10, 20, 30, 40, 50):
        await loader.update(f"{inc_id}:e0", {"config": {"level": level}})
    await _l_settle_include(inc, loader)
    file = tmp_path / "cfg.yaml"
    assert load_yaml(file.read_text(encoding="utf-8"))[0]["config"]["level"] == 50
    assert inc.root.data[0]["config"]["level"] == 50


@pytest.mark.asyncio
async def test_conformance_l8_internal_update_veto(
    tmp_path: Path, l_inc_mod: Any
) -> None:
    """internal/update on the include's own config is adopted, not restarted.

    An update to the include entry itself (same path) validates the
    new patches exactly as construction does, adopts them, and does
    NOT restart the fiber — the children update in place.
    """
    _ctx, loader, inc_id, entry, inc = await _l_boot_include(
        tmp_path,
        _l_rows(1, 2),
        patches=[{"id": "e0", "config": {"level": 9}}],
    )
    assert entry.fiber is not None
    uid_before = entry.fiber.uid
    file = tmp_path / "cfg.yaml"
    await loader.update(
        inc_id,
        {
            "config": {
                "path": str(file),
                "patches": [{"id": "e0", "config": {"level": 7}}],
            }
        },
    )
    await _l_settle_include(inc, loader)
    assert entry.fiber is not None
    assert entry.fiber.uid == uid_before
    assert entry.fiber.state == FiberState.ACTIVE
    assert inc.root.data[0]["config"]["level"] == 7
    assert l_inc_mod.calls[-1] == {"level": 7}


# ---------------------------------------------------------------------------
# L9 — apply_entry_patches
# ---------------------------------------------------------------------------


def test_conformance_l9_patches_never_mutate_input() -> None:
    """Patching never mutates its input (oracle include:66-142).

    The tree owns the patched copies, so repeated application (config
    hot-reloads) can revert a removed or changed patch.
    """
    base = [
        {"id": "a", "name": "logger", "config": {"level": "info"}},
        {"id": "b", "name": "watcher", "disabled": True},
        {"name": "entry_without_id"},
    ]
    snapshot = [
        {"id": "a", "name": "logger", "config": {"level": "info"}},
        {"id": "b", "name": "watcher", "disabled": True},
        {"name": "entry_without_id"},
    ]
    patches = [
        {"id": "a", "config": {"level": "debug"}},
        {"id": "b", "disabled": False},
        {"insert": [{"id": "c", "name": "timer"}]},
        {"id": "missing", "name": "ignored"},
        {"other_key": "ignored_patch"},
    ]
    warnings: list[str] = []
    patched = apply_entry_patches(base, patches, _l_warn_sink(warnings))
    assert len(patched) == 4
    assert patched[0]["config"]["level"] == "debug"
    assert patched[1]["disabled"] is False
    assert patched[2]["name"] == "entry_without_id"
    assert patched[3]["name"] == "timer"
    assert base == snapshot
    assert patched is not base
    assert patched[0] is not base[0]
    assert patched[0]["config"] is not base[0]["config"]
    assert any("missing" in w for w in warnings)
    assert any("id is required" in w for w in warnings)


def test_conformance_l9_insert_indexed_immediately_and_group_recursion() -> None:
    """Inserted rows are indexed at once; patching recurses into groups.

    A later patch in the same list can target a row an earlier patch
    inserted, and patches address group children by id.
    """
    base = [{"id": "a", "name": "comp"}]
    patched = apply_entry_patches(
        base,
        [
            {"insert": [{"id": "b", "name": "other"}]},
            {"id": "b", "config": {"level": 5}},
        ],
    )
    assert [e["id"] for e in patched] == ["a", "b"]
    assert patched[1]["config"] == {"level": 5}

    grouped = [
        {
            "id": "g",
            "name": "group",
            "group": True,
            "config": [{"id": "m", "name": "comp", "config": {"level": 1}}],
        }
    ]
    warnings: list[str] = []
    patched = apply_entry_patches(
        grouped,
        [
            {"id": "m", "config": {"level": 2}},
            {"id": "g", "insert": [{"id": "n", "name": "comp"}]},
            {"id": "m", "insert": [{"id": "x", "name": "comp"}]},
        ],
        _l_warn_sink(warnings),
    )
    group = patched[0]
    assert group["config"][0]["config"]["level"] == 2
    assert [e["id"] for e in group["config"]] == ["m", "n"]
    # Insert into a non-group warns and skips.
    assert any("m" in w and "not a group" in w for w in warnings)


def test_conformance_l9_warn_skip_and_name_excluded() -> None:
    """Missing id / non-group / name-mismatch warn and skip; name is no override.

    A patch that matches nothing warns and is skipped, and ``name``
    is only a match key — it is never written as an override.
    """
    warnings: list[str] = []
    patched = apply_entry_patches(
        [{"id": "a", "name": "comp"}],
        [{"id": "ghost", "insert": [{"id": "b", "name": "comp"}]}],
        _l_warn_sink(warnings),
    )
    assert patched == [{"id": "a", "name": "comp"}]
    assert any("ghost" in w and "not found" in w for w in warnings)

    warnings.clear()
    patched = apply_entry_patches(
        [{"id": "a", "name": "logger"}],
        [{"id": "a", "name": "watcher", "config": {"level": "debug"}}],
        _l_warn_sink(warnings),
    )
    assert patched == [{"id": "a", "name": "logger"}]
    assert any("name mismatch" in w for w in warnings)

    patched = apply_entry_patches(
        [{"id": "a", "name": "logger"}],
        [{"id": "a", "name": "logger", "config": {"level": "debug"}}],
    )
    assert patched[0] == {
        "id": "a",
        "name": "logger",
        "config": {"level": "debug"},
    }


# ---------------------------------------------------------------------------
# L10 — Journal (oracle-verified function by function; see module docstring)
# ---------------------------------------------------------------------------


def test_conformance_l10_diff_options_and_merge() -> None:
    """diffOptions / mergeRecords / merge (journal oracle: diffOptions, mergeRecords, merge).

    diffOptions skips ``id``, compares by deep equality, and yields
    None for a removed key (deletion, never a stored value). Merging
    folds a later record into an earlier one for the same entry.
    """
    older = {"id": "a", "name": "x", "config": {"p": 1}, "gone": True}
    newer = {"id": "a", "name": "x", "config": {"p": 2}}
    assert diff_options(older, newer) == {"config": {"p": 2}, "gone": None}
    # Strict: True is not 1, and the id never diffs.
    assert diff_options({"flag": True}, {"flag": 1}) == {"flag": 1}
    assert diff_options({"id": "a"}, {"id": "b"}) == {}
    assert diff_options({"a": 1}, {"a": 1}) == {}

    upsert = JournalRecord(kind="upsert", created=True, changes={"a": 1})
    assert merge_records(None, upsert) is upsert
    # Created then removed before ever being written: no trace.
    assert merge_records(upsert, JournalRecord(kind="remove")) is None
    # Updated then removed: the removal stands.
    updated = JournalRecord(kind="upsert", created=False, changes={"a": 1})
    removed = merge_records(updated, JournalRecord(kind="remove"))
    assert removed is not None and removed.kind == "remove"
    # Removed then upserted: a fresh upsert, not created.
    revived = merge_records(
        JournalRecord(kind="remove"),
        JournalRecord(kind="upsert", created=True, changes={"a": 2}),
    )
    assert revived is not None
    assert revived.kind == "upsert" and revived.created is False
    assert revived.changes == {"a": 2}
    # Two upserts merge changes; the newer parent/position win only
    # on relocation (parent set), otherwise the older place stands.
    first = JournalRecord(
        kind="upsert", created=True, parent=None, position=0, changes={"a": 1}
    )
    second = JournalRecord(
        kind="upsert",
        created=False,
        parent=_PARENT_UNSET,
        position=9,
        changes={"b": 2},
    )
    merged = merge_records(first, second)
    assert merged is not None
    assert merged.created is True
    assert merged.parent is None and merged.position == 0
    assert merged.changes == {"a": 1, "b": 2}
    relocated = JournalRecord(
        kind="upsert", created=False, parent="g", position=3, changes={"c": 3}
    )
    merged = merge_records(first, relocated)
    assert merged is not None
    assert merged.parent == "g" and merged.position == 3
    assert merged.changes == {"a": 1, "c": 3}

    journal: Journal = {}
    merge(journal, "a", JournalRecord(kind="upsert", created=True, changes={}))
    merge(journal, "a", JournalRecord(kind="remove"))
    assert journal == {}


def test_conformance_l10_record_create_update_remove() -> None:
    """record() (journal oracle: record).

    A creation records created=True with the row's changes minus id;
    an update records created=False with the plain diff and the parent
    only when the change relocated the entry; a removal records a
    remove. The position is the row's index in its group by identity.
    """
    ctx = Context()
    loader = LoaderService(ctx)
    group = loader.root
    journal: Journal = {}

    options = {"id": "a", "name": "x", "config": {"level": 1}}
    group.data.append(options)
    record(
        journal,
        EntryChange(id="a", group=group, options=options),
        None,
    )
    created = journal["a"]
    assert created.kind == "upsert" and created.created is True
    assert created.parent is None and created.position == 0
    assert created.changes == {"name": "x", "config": {"level": 1}}

    legacy = dict(options)
    options["config"] = {"level": 2}
    record(
        journal,
        EntryChange(id="a", group=group, options=options, legacy=legacy),
        None,
    )
    updated = journal["a"]
    assert updated.kind == "upsert" and updated.created is True
    assert updated.parent is None  # no relocation: parent stays unset
    assert updated.changes == {"name": "x", "config": {"level": 2}}

    # A relocation records the new parent.
    record(
        journal,
        EntryChange(
            id="a", group=group, from_group=group, options=options, legacy=legacy
        ),
        "g",
    )
    moved = journal["a"]
    assert moved.parent == "g" and moved.position == 0

    # A removal folds into the creation and leaves no trace.
    record(journal, EntryChange(id="a", group=group, legacy=dict(options)), None)
    assert journal == {}

    # DIVERGENCE (pinned, not hidden): the oracle's indexOf reports -1
    # when the options object is absent from the group data; anima's
    # identity search reports len(data) instead (0 for the empty root).
    journal2: Journal = {}
    ghost = {"id": "ghost", "name": "x"}
    record(
        journal2,
        EntryChange(id="ghost", group=group, options=ghost),
        None,
    )
    assert journal2["ghost"].position == len(group.data)


def test_conformance_l10_flatten_apply_changes_detach_place() -> None:
    """flatten / applyChanges / detach / place (journal oracle).

    flatten indexes every entry including nested group members, with
    parent, position, options and owner list. applyChanges deletes on
    None and honours its filter. detach removes by id and returns the
    row. place inserts clamped to the group's length and returns False
    when the parent group does not exist.
    """
    data = [
        {
            "id": "g",
            "group": True,
            "config": [{"id": "m", "name": "x", "config": {"level": 1}}],
        },
        {"id": "a", "name": "y"},
    ]
    flat = flatten(data)
    assert flat["m"].parent == "g"
    assert flat["m"].position == 0
    assert flat["m"].options is data[0]["config"][0]
    assert flat["m"].owner_list is data[0]["config"]
    assert flat["a"].parent is None and flat["a"].position == 1
    assert flat["g"].position == 0
    # A group without an id parents its children on None.
    assert (
        flatten([{"group": True, "config": [{"id": "k", "name": "x"}]}])["k"].parent
        is None
    )
    # DIVERGENCE (pinned, not hidden): anima fails loud with TypeError
    # on a non-dict row; the oracle reads the property off the value.
    with pytest.raises(TypeError, match="entry must be an object"):
        flatten([{"id": "a"}, "not-a-dict"])  # type: ignore[list-item]

    options: dict[str, Any] = {"a": 1, "b": 2}
    apply_changes(options, {"b": None, "c": 3})
    assert options == {"a": 1, "c": 3}
    apply_changes(options, {"c": 4, "d": 5}, filter_fn=lambda k: k != "c")
    assert options == {"a": 1, "c": 3, "d": 5}

    removed = detach(data, "m")
    assert removed is not None and removed["id"] == "m"
    assert data[0]["config"] == []
    assert detach(data, "nope") is None

    assert place(data, {"id": "n"}, None, 99) is True
    assert data[-1]["id"] == "n"  # clamped to the end
    assert place(data, {"id": "n2"}, "g", 0) is True
    assert data[0]["config"][0]["id"] == "n2"
    assert place(data, {"id": "n3"}, "ghost", 0) is False
    assert place(data, {"id": "n4"}, "a", 0) is False  # "a" is not a group
    # A group with no config list yet gets one created for the insert.
    data.append({"id": "g2", "group": True})
    assert place(data, {"id": "n5"}, "g2", 0) is True
    assert data[-1]["config"] == [{"id": "n5"}]


def test_conformance_l10_apply_journal() -> None:
    """applyJournal (journal oracle: applyJournal).

    Overlays a journal onto data in place: creates place new entries
    under their recorded parent, updates patch existing ones, removes
    detach, and a move to a missing group warns and restores the entry
    to its old place. filter(id, key) restricts what applies; key is
    None when asking about the entry as a whole.
    """
    data: list[dict[str, Any]] = [{"id": "a", "name": "x"}]
    journal: Journal = {
        "b": JournalRecord(
            kind="upsert",
            created=True,
            parent=None,
            position=1,
            changes={"name": "y"},
        ),
        "a": JournalRecord(
            kind="upsert",
            created=False,
            parent=_PARENT_UNSET,
            position=0,
            changes={"name": "z"},
        ),
    }
    apply_journal(data, journal)
    assert [e["id"] for e in data] == ["a", "b"]
    assert data[0]["name"] == "z"
    assert data[1] == {"id": "b", "name": "y"}

    # Removal.
    apply_journal(data, {"a": JournalRecord(kind="remove")})
    assert [e["id"] for e in data] == ["b"]

    # Move into a group.
    data = [
        {"id": "g", "group": True, "config": []},
        {"id": "a", "name": "x"},
    ]
    apply_journal(
        data,
        {
            "a": JournalRecord(
                kind="upsert", created=False, parent="g", position=0, changes={}
            )
        },
    )
    assert data[0]["config"] == [{"id": "a", "name": "x"}]
    assert [e["id"] for e in data] == ["g"]

    # Move to a missing group warns and restores the old place.
    warnings: list[str] = []
    apply_journal(
        data,
        {
            "a": JournalRecord(
                kind="upsert",
                created=False,
                parent="ghost",
                position=0,
                changes={},
            )
        },
        warn=_l_warn_sink(warnings),
    )
    assert data[0]["config"] == [{"id": "a", "name": "x"}]
    assert any("cannot move entry" in w for w in warnings)

    # filter: a rejected whole entry is skipped; a rejected key is kept.
    data = [{"id": "a", "name": "x", "level": 1}]
    apply_journal(
        data,
        {
            "a": JournalRecord(
                kind="upsert",
                created=False,
                parent=_PARENT_UNSET,
                position=0,
                changes={"name": "y", "level": 2},
            )
        },
        filter_fn=lambda entry_id, key: False if key is None else key != "name",
    )
    assert data == [{"id": "a", "name": "x", "level": 1}]
    apply_journal(
        data,
        {
            "a": JournalRecord(
                kind="upsert",
                created=False,
                parent=_PARENT_UNSET,
                position=0,
                changes={"name": "y", "level": 2},
            )
        },
        filter_fn=lambda entry_id, key: key != "name",
    )
    assert data == [{"id": "a", "name": "x", "level": 2}]


def test_conformance_l10_reconcile_file_wins() -> None:
    """reconcile + Conflict (journal oracle: reconcile).

    Three-way reconciliation of a journal against a new file state.
    The file wins every conflict: conflicting keys are dropped from
    the journal so the tree follows the file, and each drop is
    reported as a Conflict.
    """
    base_rows = [{"id": "a", "name": "x", "level": 1}]

    def _case(
        journal: Journal,
        theirs_rows: list[dict[str, Any]],
        file_owned: Any = lambda _id, _key: True,
    ) -> list[Conflict]:
        return reconcile(journal, flatten(base_rows), flatten(theirs_rows), file_owned)

    # Key modified both in file and at runtime: file wins, key dropped.
    journal: Journal = {
        "a": JournalRecord(
            kind="upsert",
            created=False,
            parent=_PARENT_UNSET,
            position=0,
            changes={"level": 2},
        )
    }
    conflicts = _case(journal, [{"id": "a", "name": "x", "level": 9}])
    assert [(c.id, c.reason) for c in conflicts] == [
        ("a", 'key "level" modified both in file and at runtime')
    ]
    assert journal == {}

    # File already agrees with the journaled value: no conflict, and
    # the entry is dropped once created is cleared and nothing moves.
    journal = {
        "a": JournalRecord(
            kind="upsert",
            created=True,
            parent=_PARENT_UNSET,
            position=0,
            changes={"level": 2},
        )
    }
    conflicts = _case(journal, [{"id": "a", "name": "x", "level": 2}])
    assert conflicts == []
    assert journal["a"].created is False

    # Removed at runtime, modified in file: conflict, entry dropped.
    journal = {"a": JournalRecord(kind="remove")}
    conflicts = _case(journal, [{"id": "a", "name": "x", "level": 9}])
    assert [(c.id, c.reason) for c in conflicts] == [
        ("a", "modified in file, removed at runtime")
    ]
    assert journal == {}

    # Removed at runtime, added in file: conflict, entry dropped.
    journal = {"a": JournalRecord(kind="remove")}
    conflicts = reconcile(
        journal,
        flatten([]),
        flatten([{"id": "a", "name": "x"}]),
        lambda _id, _key: True,
    )
    assert [(c.id, c.reason) for c in conflicts] == [
        ("a", "added in file, removed at runtime")
    ]

    # Removed at runtime, gone from the file too: silently dropped.
    journal = {"a": JournalRecord(kind="remove")}
    conflicts = _case(journal, [])
    assert conflicts == []
    assert journal == {}

    # Removed in file, modified at runtime: conflict, entry dropped.
    journal = {
        "a": JournalRecord(
            kind="upsert",
            created=False,
            parent=_PARENT_UNSET,
            position=0,
            changes={"level": 2},
        )
    }
    conflicts = _case(journal, [])
    assert [(c.id, c.reason) for c in conflicts] == [
        ("a", "removed in file, modified at runtime")
    ]
    assert journal == {}

    # Created at runtime, nothing in the file to conflict with: kept.
    journal = {
        "a": JournalRecord(
            kind="upsert",
            created=True,
            parent=None,
            position=0,
            changes={"name": "x"},
        )
    }
    conflicts = reconcile(journal, flatten([]), flatten([]), lambda _id, _key: True)
    assert conflicts == []
    assert journal["a"].created is True

    # Moved both in file and at runtime: conflict, parent reset.
    base_moved = [
        {"id": "g1", "group": True, "config": [{"id": "a", "name": "x"}]},
        {"id": "g2", "group": True, "config": []},
    ]
    theirs_moved = [
        {"id": "g1", "group": True, "config": []},
        {"id": "g2", "group": True, "config": [{"id": "a", "name": "x"}]},
    ]
    # The record also carries a change the file agrees with, so the
    # entry survives the move conflict with its parent reset (an entry
    # with no surviving change and no move is dropped).
    journal = {
        "a": JournalRecord(
            kind="upsert",
            created=False,
            parent="g1",
            position=0,
            changes={"name": "x"},
        )
    }
    conflicts = reconcile(
        journal,
        flatten(base_moved),
        flatten(theirs_moved),
        lambda _id, _key: True,
    )
    assert [(c.id, c.reason) for c in conflicts] == [
        ("a", "moved both in file and at runtime")
    ]
    assert journal["a"].parent is _PARENT_UNSET
    assert journal["a"].changes == {"name": "x"}

    # fileOwned scopes the check: a patch-owned entry is untouched.
    journal = {
        "a": JournalRecord(
            kind="upsert",
            created=False,
            parent=_PARENT_UNSET,
            position=0,
            changes={"level": 2},
        )
    }
    conflicts = _case(
        journal,
        [{"id": "a", "name": "x", "level": 9}],
        file_owned=lambda _id, _key: False,
    )
    assert conflicts == []
    assert journal["a"].changes == {"level": 2}


def test_conformance_l10_conflict_shape() -> None:
    """Conflict carries the entry id and the reason (journal oracle: Conflict)."""
    conflict = Conflict(id="a", reason="removed in file, modified at runtime")
    assert conflict.id == "a"
    assert conflict.reason == "removed in file, modified at runtime"


@pytest.mark.asyncio
async def test_conformance_l10_journal_wired_into_include(
    tmp_path: Path, l_inc_mod: Any
) -> None:
    """The journal is wired, not dead code: commit / write / drain.

    A runtime update is journalled by Include.commit and drained
    back to the file by the write loop; a removal is journalled and
    drained the same way. The commit also emits loader/config-update.
    """
    ctx, loader, inc_id, _entry, inc = await _l_boot_include(tmp_path, _l_rows(1, 2))
    updates: list[Any] = []
    ctx.on("loader/config-update", lambda: updates.append(True))
    await loader.update(f"{inc_id}:e1", {"config": {"level": 20}})
    await _l_settle_include(inc, loader)
    assert updates
    file = tmp_path / "cfg.yaml"
    assert load_yaml(file.read_text(encoding="utf-8"))[1]["config"]["level"] == 20

    loader.remove(f"{inc_id}:e0")
    await _l_settle_include(inc, loader)
    await ctx.flush()
    rows = load_yaml(file.read_text(encoding="utf-8"))
    assert [r["id"] for r in rows] == ["e1"]
    assert rows[0]["config"]["level"] == 20


# ---------------------------------------------------------------------------
# L11 — equal_except_volatile + diff_entries
# ---------------------------------------------------------------------------


def test_conformance_l11_equal_except_volatile() -> None:
    """equal_except_volatile (oracle loader diff.ts; entry.ts:141-147).

    Volatile fields compare equal regardless of value; an absent
    object compares equal to its schema default; JsExpr/opaque values
    keep strict raw equality; nothing is evaluated or mutated.
    """
    older = {"theme": "dark", "level": 3}
    assert (
        equal_except_volatile(older, {"theme": "light", "level": 3}, _L_VOLATILE_SCHEMA)
        is True
    )
    assert (
        equal_except_volatile(older, {"theme": "dark", "level": 4}, _L_VOLATILE_SCHEMA)
        is False
    )
    assert (
        equal_except_volatile(older, {"theme": "light", "level": 4}, _L_VOLATILE_SCHEMA)
        is False
    )
    nested = {"nested": {"label": "a", "count": 1}}
    assert (
        equal_except_volatile(
            nested, {"nested": {"label": "b", "count": 1}}, _L_VOLATILE_SCHEMA
        )
        is True
    )
    assert (
        equal_except_volatile(
            nested, {"nested": {"label": "a", "count": 2}}, _L_VOLATILE_SCHEMA
        )
        is False
    )
    # Absent nested object compares equal to its schema default ({}).
    assert (
        equal_except_volatile(
            {"level": 3}, {"level": 3, "nested": {}}, _L_VOLATILE_SCHEMA
        )
        is True
    )
    assert (
        equal_except_volatile(
            {"level": 3}, {"level": 3, "nested": {"count": 1}}, _L_VOLATILE_SCHEMA
        )
        is False
    )
    # Absent top-level config compares equal to the object default.
    assert equal_except_volatile(None, {}, _L_VOLATILE_SCHEMA) is True
    assert equal_except_volatile(None, {"level": 3}, _L_VOLATILE_SCHEMA) is False
    # A scalar default is NOT substituted: only whole objects get the
    # default treatment.
    scalar_default = Schema.object({"mode": Schema.string().default("plain")})
    assert equal_except_volatile({}, {"mode": "plain"}, scalar_default) is False
    # JsExpr nodes are opaque: identical strings equal, anything else differs.
    schema = Schema.object({"mode": Schema.string()})
    expr = {"mode": {"__jsExpr": "homePath('x')"}}
    assert equal_except_volatile(expr, dict(expr), schema) is True
    assert (
        equal_except_volatile(expr, {"mode": {"__jsExpr": "process.env.Y"}}, schema)
        is False
    )
    assert equal_except_volatile(expr, {"mode": "plain"}, schema) is False
    # Unknown fields are strict; a non-schema falls back to strict deep equal.
    assert (
        equal_except_volatile(
            {"level": 3, "extra": 1}, {"level": 3, "extra": 2}, _L_VOLATILE_SCHEMA
        )
        is False
    )
    assert equal_except_volatile({"flag": True}, {"flag": 1}, None) is False
    assert equal_except_volatile({"flag": True}, {"flag": True}, None) is True


def test_conformance_l11_diff_entries_removed_key_is_deletion() -> None:
    """diff_entries is schema-aware; a removed key yields None = deletion.

    A volatile-only config change produces no diff entry; an
    ordinary change does; expression nodes compare structurally and
    are never evaluated; the id never diffs.
    """
    assert (
        diff_entries(
            {"config": {"theme": "dark", "level": 3}},
            {"config": {"theme": "light", "level": 3}},
            _L_VOLATILE_SCHEMA,
        )
        == {}
    )
    assert diff_entries(
        {"config": {"theme": "dark", "level": 3}},
        {"config": {"theme": "dark", "level": 4}},
        _L_VOLATILE_SCHEMA,
    ) == {"config": {"theme": "dark", "level": 4}}
    # Removed key -> None: in a changes dict None deletes the key.
    assert diff_entries({"id": "a", "gone": True}, {"id": "a"}) == {"gone": None}
    assert diff_entries({"id": "a"}, {"id": "b"}) == {}
    assert diff_entries(
        {"config": {"mode": {"__jsExpr": "homePath('x')"}}},
        {"config": {"mode": {"__jsExpr": "process.env.Y"}}},
        Schema.object({"mode": Schema.string()}),
    ) == {"config": {"mode": {"__jsExpr": "process.env.Y"}}}


# ---------------------------------------------------------------------------
# L12 — Group replaced by EntryGroup
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_conformance_l12_group_is_entrygroup_in_shared_store(
    l_tree_mod: Any,
) -> None:
    """Group entries are Entrys with group:true + subgroup, in one store.

    The old Group component is gone: a group row is an Entry whose
    config is the child-row list, and its children live in the same
    shared store, resolving by short id (oracle group.ts:9-90). Group
    and Include both carry the tree-carrier marker.
    """
    ctx = Context()
    loader = LoaderService(ctx)
    loader.load(
        [
            {
                "id": "g",
                "name": "group",
                "group": True,
                "config": [_l_row(id="a"), _l_row(id="b")],
            }
        ]
    )
    await ctx.flush()
    group_entry = loader.resolve("g")
    assert group_entry.options.get("group") is True
    assert group_entry.subgroup is not None
    assert isinstance(group_entry.subgroup, EntryGroup)
    assert set(loader.store) == {"g", "a", "b"}
    assert loader.resolve("a").parent is group_entry.subgroup
    assert loader.resolve("b").parent is group_entry.subgroup
    assert group_entry.fiber is not None
    assert isinstance(group_entry.fiber._instance, Group)

    assert getattr(Group, EntryGroup.key, False) is True
    assert getattr(Include, EntryGroup.key, False) is True
    assert EntryGroup.is_tree_carrier(Group) is True
    assert EntryGroup.is_tree_carrier(Include) is True
    assert EntryGroup.is_tree_carrier(l_tree_mod.comp) is False


@pytest.mark.asyncio
async def test_conformance_l12_group_update_wholesale_per_entry_catch(
    l_tree_mod: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """Group update replaces data wholesale, catching failures per entry.

    The row list is swapped wholesale and reconciled over the union
    of old and new ids; one bad child is logged and skipped while its
    good sibling still mounts (oracle group.ts:9-90).
    """
    ctx = Context()
    loader = LoaderService(ctx)
    loader.load(
        [
            {
                "id": "g",
                "name": "group",
                "group": True,
                "config": [_l_row(id="a"), _l_row(id="b")],
            }
        ]
    )
    await ctx.flush()
    group_entry = loader.resolve("g")
    assert group_entry.subgroup is not None

    with caplog.at_level(logging.ERROR, logger="anima.loader"):
        await group_entry.subgroup.update(
            [
                _l_row(id="c"),
                {"id": "bad", "name": "lconf_tree_mod:not_a_component"},
            ]
        )
    await loader.await_all()
    assert [r["id"] for r in group_entry.subgroup.data] == ["c", "bad"]
    assert "a" not in loader.store
    assert "b" not in loader.store
    assert loader.resolve("c").fiber is not None
    assert loader.resolve("c").fiber.state == FiberState.ACTIVE
    assert loader.resolve("bad").fiber is None
    assert "Failed to create entry bad" in caplog.text


@pytest.mark.asyncio
async def test_conformance_l12_group_remove_stop_disabled(
    l_tree_mod: Any,
) -> None:
    """Group remove disposes/unlinks/deletes/emits; stop() keeps data rows.

    Removing an entry disposes its fiber, unlinks its row, drops it
    from the store, and emits loader/partial-dispose. stop() disposes
    the child fibers but keeps the data rows, so a later update
    remounts them. Disabling a group disables its descendants, never
    the group row itself (oracle group.ts:9-90).
    """
    ctx = Context()
    loader = LoaderService(ctx)
    loader.load(
        [
            {
                "id": "g",
                "name": "group",
                "group": True,
                "config": [_l_row(id="a"), _l_row(id="b")],
            }
        ]
    )
    await ctx.flush()
    group_entry = loader.resolve("g")
    subgroup = group_entry.subgroup
    assert subgroup is not None
    rows = subgroup.data

    partials: list[Any] = []
    ctx.on("loader/partial-dispose", lambda *args: partials.append(args))
    subgroup.ctx.on("loader/partial-dispose", lambda *args: partials.append(args))
    loader.remove("a")
    await subgroup.ctx.flush()
    await loader.await_all()
    assert "a" not in loader.store
    assert [r["id"] for r in rows] == ["b"]
    # The event carries (entry, options, is_dispose); the removed
    # row's options identify it.
    assert any(
        len(args) >= 2 and isinstance(args[1], dict) and args[1].get("id") == "a"
        for args in partials
    )

    fiber_b = loader.resolve("b").fiber
    assert fiber_b is not None and fiber_b.state == FiberState.ACTIVE
    subgroup.stop()
    await group_entry.ctx.flush()
    assert "b" not in loader.store
    assert fiber_b.state == FiberState.DISPOSED
    assert subgroup.data is rows
    assert [r["id"] for r in rows] == ["b"]
    await subgroup.update(rows)
    await loader.await_all()
    assert loader.resolve("b").fiber is not None
    assert loader.resolve("b").fiber.state == FiberState.ACTIVE
    assert loader.resolve("b").fiber is not fiber_b

    # Disabled walks ancestors: the group row itself stays enabled.
    await loader.disable("g")
    kid = loader.resolve("b")
    assert group_entry.disabled is False
    assert kid.disabled is True
    assert kid.fiber is not None
    assert kid.fiber.state == FiberState.DISPOSED
    await loader.enable("g")
    assert kid.disabled is False
    assert kid.fiber is not None
    assert kid.fiber.state == FiberState.ACTIVE


@pytest.mark.asyncio
async def test_conformance_l12_group_update_identity_path(
    l_tree_mod: Any,
) -> None:
    """The Group internal/update handler reconciles only on config identity.

    The handler reconciles in place when the config is the group's
    own row list by identity, and otherwise passes the update down
    the chain (which restarts the fiber). REFINEMENT of the audit
    caveat, verified by probe: on the default save path the commit
    step adopts the resolved list into the entry's options first, so
    even an equal-content copy reconciles in place with no restart.
    The pass-through only fires when the commit is skipped
    (no_save=True) with a different list object.
    """
    ctx = Context()
    loader = LoaderService(ctx)
    loader.load(
        [
            {
                "id": "g",
                "name": "group",
                "group": True,
                "config": [_l_row(id="a")],
            }
        ]
    )
    await ctx.flush()
    group_entry = loader.resolve("g")
    group_fiber = group_entry.fiber
    assert group_fiber is not None and group_fiber.state == FiberState.ACTIVE
    child_fiber = loader.resolve("a").fiber
    rows = group_entry.options["config"]

    statuses: list[str] = []
    ctx.on(
        "internal/status",
        lambda fiber, old: (
            statuses.append(fiber.state.value) if fiber is group_fiber else None
        ),
    )

    # Same list object: reconciled in place, no restart.
    await group_fiber.update(rows)
    await ctx.flush()
    assert statuses == []
    assert loader.resolve("a").fiber is child_fiber

    # Equal-content copy, default save: the commit adopts the list
    # first, so identity holds and it still reconciles in place.
    await group_fiber.update(list(rows))
    await ctx.flush()
    assert statuses == []
    assert loader.resolve("a").fiber is child_fiber

    # Equal-content copy with the commit skipped: identity fails, the
    # update passes down the chain and the fiber restarts.
    child_entry = loader.resolve("a")
    calls_before = len(l_tree_mod.calls)
    await group_fiber.update(list(group_entry.options["config"]), no_save=True)
    await ctx.flush()
    assert statuses == ["UNLOADING", "LOADING", "ACTIVE"]
    assert group_fiber.state == FiberState.ACTIVE
    # The restart runs the Group's stop disposer first, which drops
    # the child entries from the store (keeping the data rows), so the
    # re-mount creates a fresh Entry with a fresh fiber for the same
    # row — the group fiber itself is reused, the child is not.
    assert loader.resolve("a") is not child_entry
    assert loader.resolve("a").fiber is not child_fiber
    assert loader.resolve("a").fiber is not None
    assert loader.resolve("a").fiber.state == FiberState.ACTIVE
    assert len(l_tree_mod.calls) == calls_before + 1


# ---------------------------------------------------------------------------
# L13 — HMR module-graph traversal
# ---------------------------------------------------------------------------


def test_conformance_l13_analyze_changes_classifies_dependents(
    tmp_path: Path,
) -> None:
    """_analyze_changes: changed + transitive dependents accepted, rest declined.

    A shared dependency's change is accepted together with every
    module that (transitively) imports it; everything else tracked
    is declined (oracle hmr index.ts:31-44).
    """
    shared_path = _l_write(tmp_path, "lconf13_shared.py", "VALUE = 1\n")
    _l_write(
        tmp_path,
        "lconf13_plugin.py",
        "from lconf13_shared import VALUE\nPLUGIN_VALUE = VALUE\n",
    )
    _l_write(tmp_path, "lconf13_other.py", "import lconf13_shared\n")
    _l_write(tmp_path, "lconf13_alone.py", "VALUE = 0\n")
    ctx = Context()
    hmr = HmrService(ctx, {"root": tmp_path})
    try:
        _l_load("lconf13_shared", shared_path)
        _l_load("lconf13_plugin", tmp_path / "lconf13_plugin.py")
        _l_load("lconf13_other", tmp_path / "lconf13_other.py")
        _l_load("lconf13_alone", tmp_path / "lconf13_alone.py")
        accepted, declined = hmr._analyze_changes({"lconf13_shared"})
        assert accepted == {"lconf13_shared", "lconf13_plugin", "lconf13_other"}
        assert declined == {"lconf13_alone"}
    finally:
        _l_pop("lconf13_shared", "lconf13_plugin", "lconf13_other", "lconf13_alone")


def test_conformance_l13_topological_order_dependencies_first() -> None:
    """Reload order is dependencies-first; a cycle falls back to sorted order."""
    graph = {
        "a": {"b", "c"},
        "b": {"d"},
        "c": {"d"},
        "d": set(),
    }
    order = HmrService._topological_order({"a", "b", "c", "d"}, graph)
    assert order[0] == "d"
    assert order[-1] == "a"
    assert set(order) == {"a", "b", "c", "d"}
    assert order.index("b") < order.index("a")
    assert order.index("c") < order.index("a")

    cyclic = {"x": {"y"}, "y": {"x"}}
    assert HmrService._topological_order({"x", "y"}, cyclic) == ["x", "y"]


@pytest.mark.asyncio
async def test_conformance_l13_shared_dependency_reloads_dependents(
    tmp_path: Path,
) -> None:
    """A shared-dependency change reloads its dependents, bindings fresh.

    The dependent that imported the value with ``from`` gets the fresh
    binding because it is reloaded after its dependency; the
    ``import``-style dependent sees the fresh module too; an unrelated
    component is untouched and absent from the reload map.
    """
    shared_path = _l_write(tmp_path, "lconf13s_shared.py", "VALUE = 1\n")
    plugin_path = _l_write(
        tmp_path,
        "lconf13s_plugin.py",
        "from lconf13s_shared import VALUE\n"
        "PLUGIN_VALUE = VALUE\n"
        "class Comp:\n"
        "    def __init__(self, ctx):\n"
        "        self.v = PLUGIN_VALUE\n",
    )
    other_path = _l_write(
        tmp_path,
        "lconf13s_other.py",
        "import lconf13s_shared\n"
        "class Other:\n"
        "    def __init__(self, ctx):\n"
        "        self.v = lconf13s_shared.VALUE\n",
    )
    alone_path = _l_write(
        tmp_path,
        "lconf13s_alone.py",
        "class Alone:\n    def __init__(self, ctx):\n        self.v = 0\n",
    )
    ctx = Context()
    hmr = HmrService(ctx, {"root": tmp_path})
    shared = _l_load("lconf13s_shared", shared_path)
    plugin = _l_load("lconf13s_plugin", plugin_path)
    other = _l_load("lconf13s_other", other_path)
    alone = _l_load("lconf13s_alone", alone_path)
    reloads: list[dict[Any, Reload]] = []
    ctx.on("hmr/reload", lambda m: reloads.append(m))
    try:
        fib = ctx.use(plugin.Comp)
        fib_other = ctx.use(other.Other)
        fib_alone = ctx.use(alone.Alone)
        await ctx.flush()
        assert fib.state == FiberState.ACTIVE
        assert plugin.PLUGIN_VALUE == 1

        shared_path.write_text("VALUE = 2\n")
        _l_bump(shared_path)
        reloaded = await hmr.reload_module("lconf13s_shared", path=shared_path)
        assert reloaded is shared
        assert shared.VALUE == 2
        assert plugin.PLUGIN_VALUE == 2  # fresh `from` binding
        assert fib.target is plugin.Comp
        assert fib.state == FiberState.ACTIVE
        assert fib_other.target is other.Other
        assert fib_other.state == FiberState.ACTIVE
        assert fib_alone.target is alone.Alone
        assert fib_alone.state == FiberState.ACTIVE
        assert len(reloads) == 1
        assert plugin.Comp in reloads[0]
        assert other.Other in reloads[0]
        assert alone.Alone not in reloads[0]
        assert reloads[0][plugin.Comp].filename == plugin_path.resolve().as_uri()
        assert reloads[0][plugin.Comp].runtime is not None
    finally:
        _l_pop("lconf13s_shared", "lconf13s_plugin", "lconf13s_other", "lconf13s_alone")


# ---------------------------------------------------------------------------
# L14 — HMR debounce / externals / full reload / config routing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_conformance_l14_debounce_default_coalesces(tmp_path: Path) -> None:
    """Debounce defaults to 100 ms and coalesces rapid changes into one reload."""
    shared_path = _l_write(tmp_path, "lconf14_shared.py", "VALUE = 1\n")
    plugin_path = _l_write(
        tmp_path,
        "lconf14_plugin.py",
        "from lconf14_shared import VALUE\n"
        "class Comp:\n"
        "    version = VALUE\n"
        "    def __init__(self, ctx):\n"
        "        self.v = VALUE\n",
    )
    ctx = Context()
    hmr = HmrService(ctx, {"root": tmp_path})
    shared = _l_load("lconf14_shared", shared_path)
    plugin = _l_load("lconf14_plugin", plugin_path)
    reloads: list[dict[Any, Reload]] = []
    ctx.on("hmr/reload", lambda m: reloads.append(m))
    try:
        fib = ctx.use(plugin.Comp)
        await ctx.flush()
        for value in (2, 3, 4):
            shared_path.write_text(f"VALUE = {value}\n")
            _l_bump(shared_path)
            await hmr._on_file_change("change", shared_path)
        await asyncio.sleep(0.6)
        assert len(reloads) == 1
        assert shared.VALUE == 4
        assert plugin.Comp.version == 4
        assert fib.target is plugin.Comp
    finally:
        _l_pop("lconf14_shared", "lconf14_plugin")


@pytest.mark.asyncio
async def test_conformance_l14_debounce_window_honored(tmp_path: Path) -> None:
    """The debounce window is honoured: nothing reloads inside it."""
    shared_path = _l_write(tmp_path, "lconf14w_shared.py", "VALUE = 1\n")
    plugin_path = _l_write(
        tmp_path,
        "lconf14w_plugin.py",
        "from lconf14w_shared import VALUE\n"
        "class Comp:\n"
        "    version = VALUE\n"
        "    def __init__(self, ctx):\n"
        "        self.v = VALUE\n",
    )
    ctx = Context()
    hmr = HmrService(ctx, {"root": tmp_path, "debounce": 200})
    shared = _l_load("lconf14w_shared", shared_path)
    plugin = _l_load("lconf14w_plugin", plugin_path)
    reloads: list[dict[Any, Reload]] = []
    ctx.on("hmr/reload", lambda m: reloads.append(m))
    try:
        ctx.use(plugin.Comp)
        await ctx.flush()
        shared_path.write_text("VALUE = 2\n")
        _l_bump(shared_path)
        await hmr._on_file_change("change", shared_path)
        await asyncio.sleep(0.1)
        assert reloads == []  # still inside the 200 ms window
        await asyncio.sleep(0.5)
        assert len(reloads) == 1
        assert shared.VALUE == 2
    finally:
        _l_pop("lconf14w_shared", "lconf14w_plugin")


@pytest.mark.asyncio
async def test_conformance_l14_external_hook_and_unwired_default(
    tmp_path: Path,
    tmp_path_factory: pytest.TempPathFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Externals route to the full-reload hook; unconfigured HMR only warns.

    CAVEAT (pinned, not hidden): the full-reload path is opt-in via
    the ``full_reload`` config callable. Nothing wires the loader's
    exit hook as HMR's hook by default, so an unconfigured HMR only
    warns on an external change. Both halves are pinned here.
    """
    outside = tmp_path_factory.mktemp("outside")
    ext_path = _l_write(outside, "lconf14_ext.py", "VALUE = 1\n")
    ctx = Context()
    hooks: list[str] = []
    hmr = HmrService(
        ctx, {"root": tmp_path, "full_reload": lambda url: hooks.append(url)}
    )
    ext_mod = _l_load("lconf14_ext", ext_path)
    reloads: list[dict[Any, Reload]] = []
    ctx.on("hmr/reload", lambda m: reloads.append(m))
    try:
        ext_path.write_text("VALUE = 2\n")
        _l_bump(ext_path)
        await hmr._on_file_change("change", ext_path)
        assert hooks == [ext_path.resolve().as_uri()]
        assert ext_mod.VALUE == 1  # untouched: externals never reload
        assert reloads == []
    finally:
        _l_pop("lconf14_ext")

    # An async hook is awaited the same way.
    hooks.clear()
    ext2_path = _l_write(outside, "lconf14_ext2.py", "VALUE = 1\n")
    ctx2 = Context()

    async def _hook(url: str) -> None:
        hooks.append(url)

    hmr2 = HmrService(ctx2, {"root": tmp_path, "full_reload": _hook})
    _l_load("lconf14_ext2", ext2_path)
    try:
        await hmr2._on_file_change("change", ext2_path)
        assert hooks == [ext2_path.resolve().as_uri()]
    finally:
        _l_pop("lconf14_ext2")

    # The unwired default: no hook configured, only a warning.
    ext3_path = _l_write(outside, "lconf14_ext3.py", "VALUE = 1\n")
    ctx3 = Context()
    hmr3 = HmrService(ctx3, {"root": tmp_path})
    _l_load("lconf14_ext3", ext3_path)
    try:
        with caplog.at_level(logging.WARNING, logger="anima.hmr"):
            await hmr3._on_file_change("change", ext3_path)
        assert "full reload" in caplog.text
    finally:
        _l_pop("lconf14_ext3")

    # The loader's exit hook exists as a host-wirable no-op, but HMR
    # does not wire it by default.
    loader = LoaderService(Context())
    assert callable(loader.exit)
    assert loader.exit() is None


@pytest.mark.asyncio
async def test_conformance_l14_config_change_refreshes_include(
    tmp_path: Path, l_inc_mod: Any
) -> None:
    """Config-file changes route to Include.refresh(), not to a reload.

    A change to a file backed by a mounted Include refreshes that
    tree in place (no hmr/change, no hmr/reload); a config file with
    no matching Include still emits hmr/change.
    """
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text("- name: lconf_inc_mod:comp\n  id: e0\n  config:\n    level: 1\n")
    ctx = Context()
    loader = LoaderService(ctx)
    inc_id = await loader.create({"name": "include", "config": {"path": str(cfg)}})
    await loader.await_all()
    await ctx.flush()
    entry = loader.resolve(inc_id)
    inc = entry.ctx.get("include", strict=False)
    assert isinstance(inc, Include)
    assert l_inc_mod.calls[-1] == {"level": 1}

    hmr = HmrService(ctx, {"root": tmp_path})
    changes: list[str] = []
    reloads: list[dict[Any, Reload]] = []
    ctx.on("hmr/change", lambda url: changes.append(url))
    ctx.on("hmr/reload", lambda m: reloads.append(m))
    cfg.write_text("- name: lconf_inc_mod:comp\n  id: e0\n  config:\n    level: 2\n")
    await hmr._on_file_change("change", cfg)
    await inc.flush()
    await ctx.flush()
    assert l_inc_mod.calls[-1] == {"level": 2}
    assert changes == []
    assert reloads == []

    other = tmp_path / "other.yaml"
    other.write_text("- name: lconf_inc_mod:comp\n  id: e0\n")
    await hmr._on_file_change("change", other)
    assert changes == [other.resolve().as_uri()]


# ---------------------------------------------------------------------------
# L15 — HMR backup + rollback + error rendering
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_conformance_l15_failed_reload_rolls_back_fibers_intact(
    tmp_path: Path,
) -> None:
    """A failed reload rolls module state back with fibers intact.

    The old code is restored, brand-new modules imported mid-reload
    are dropped from sys.modules, the fiber keeps its old target and
    stays ACTIVE, no reload event fires, and a retry after fixing the
    file succeeds.
    """
    shared_path = _l_write(tmp_path, "lconf15_shared.py", "VALUE = 1\n")
    plugin_path = _l_write(
        tmp_path,
        "lconf15_plugin.py",
        "from lconf15_shared import VALUE\n"
        "class Comp:\n"
        "    version = VALUE\n"
        "    def __init__(self, ctx):\n"
        "        self.v = VALUE\n",
    )
    _l_write(tmp_path, "lconf15_brand_new.py", "MARKER = True\n")
    ctx = Context()
    hmr = HmrService(ctx, {"root": tmp_path})
    shared = _l_load("lconf15_shared", shared_path)
    plugin = _l_load("lconf15_plugin", plugin_path)
    reloads: list[dict[Any, Reload]] = []
    ctx.on("hmr/reload", lambda m: reloads.append(m))
    sys.path.insert(0, str(tmp_path))
    try:
        fib = ctx.use(plugin.Comp)
        await ctx.flush()
        old_target = fib.target
        assert old_target.version == 1

        shared_path.write_text(
            "import lconf15_brand_new\nraise RuntimeError('boom')\nVALUE = 2\n"
        )
        _l_bump(shared_path)
        assert await hmr.reload_module("lconf15_shared", path=shared_path) is None
        assert shared.VALUE == 1
        assert "lconf15_brand_new" not in sys.modules
        assert fib.target is old_target
        assert fib.state == FiberState.ACTIVE
        assert reloads == []

        shared_path.write_text("VALUE = 3\n")
        _l_bump(shared_path)
        assert await hmr.reload_module("lconf15_shared", path=shared_path) is shared
        assert shared.VALUE == 3
        assert plugin.Comp.version == 3
    finally:
        sys.path.remove(str(tmp_path))
        _l_pop("lconf15_shared", "lconf15_plugin", "lconf15_brand_new")


@pytest.mark.asyncio
async def test_conformance_l15_rollback_is_shallow(tmp_path: Path) -> None:
    """CAVEAT (pinned, not hidden): the rollback snapshot is shallow.

    Re-bound names are restored, but in-place mutations of
    pre-existing shared mutable module state survive a rollback —
    the snapshot holds the same objects.
    """
    mod_path = _l_write(tmp_path, "lconf15_shallow.py", "ITEMS = []\nVALUE = 1\n")
    ctx = Context()
    hmr = HmrService(ctx, {"root": tmp_path})
    mod = _l_load("lconf15_shallow", mod_path)
    try:
        items = mod.ITEMS
        mod_path.write_text("ITEMS.append('partial')\nraise RuntimeError('boom')\n")
        _l_bump(mod_path)
        assert await hmr.reload_module("lconf15_shallow", path=mod_path) is None
        assert mod.VALUE == 1  # the re-bound name is restored
        assert mod.ITEMS is items
        assert mod.ITEMS == ["partial"]  # ...but the in-place append survives
    finally:
        _l_pop("lconf15_shallow")


def test_conformance_l15_error_rendering_code_frame(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """handle_error renders syntax errors with a code frame.

    A syntax error is logged with its file location and the
    offending source line plus a caret pointer; any other error is a
    plain warning with no frame.
    """
    with caplog.at_level(logging.WARNING, logger="anima.hmr"):
        handle_error(RuntimeError("plain boom"))
    assert "plain boom" in caplog.text
    assert "File:" not in caplog.text

    src = tmp_path / "lconf15_frame.py"
    src.write_text("a = 1\nb = 2\nc = 3\n")
    err = SyntaxError("oops", (str(src), 2, 5, "b = 2\n"))
    with caplog.at_level(logging.WARNING, logger="anima.hmr"):
        handle_error(err)
    assert "File:" in caplog.text
    assert "b = 2" in caplog.text
    assert "^" in caplog.text


@pytest.mark.asyncio
async def test_conformance_l15_only_oracle_event_names(tmp_path: Path) -> None:
    """Events align to hmr/change(url) and hmr/reload(reloads) only.

    The old anima-only hmr/before-reload, hmr/after-reload and
    hmr/error names are gone: they are absent from the HMR source and
    never emitted across a successful reload and a failed one.
    """
    source = (_l_repo_root() / "src" / "anima" / "hmr.py").read_text(encoding="utf-8")
    assert "hmr/change" in source
    assert "hmr/reload" in source
    assert "hmr/before-reload" not in source
    assert "hmr/after-reload" not in source
    assert "hmr/error" not in source

    mod_path = _l_write(
        tmp_path,
        "lconf15_events.py",
        "class Comp:\n"
        "    version = 1\n"
        "    def __init__(self, ctx):\n"
        "        self.v = 1\n",
    )
    ctx = Context()
    hmr = HmrService(ctx, {"root": tmp_path})
    mod = _l_load("lconf15_events", mod_path)
    seen: dict[str, list[Any]] = {
        "hmr/change": [],
        "hmr/reload": [],
        "hmr/before-reload": [],
        "hmr/after-reload": [],
        "hmr/error": [],
    }
    for name in seen:
        ctx.on(name, lambda *args, _n=name: seen[_n].append(args))
    try:
        fib = ctx.use(mod.Comp)
        await ctx.flush()
        mod_path.write_text(
            "class Comp:\n"
            "    version = 2\n"
            "    def __init__(self, ctx):\n"
            "        self.v = 2\n"
        )
        _l_bump(mod_path)
        assert await hmr.reload_module("lconf15_events", path=mod_path) is mod
        assert len(seen["hmr/reload"]) == 1
        assert seen["hmr/before-reload"] == []
        assert seen["hmr/after-reload"] == []
        assert seen["hmr/error"] == []

        mod_path.write_text("raise RuntimeError('boom')\n")
        _l_bump(mod_path)
        assert await hmr.reload_module("lconf15_events", path=mod_path) is None
        assert fib.target is mod.Comp
        assert fib.state == FiberState.ACTIVE
        assert len(seen["hmr/reload"]) == 1  # no second reload event
        assert seen["hmr/before-reload"] == []
        assert seen["hmr/after-reload"] == []
        assert seen["hmr/error"] == []
    finally:
        _l_pop("lconf15_events")


# ---------------------------------------------------------------------------
# L16 — HMR concurrency dedup
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_conformance_l16_concurrent_reload_deduped(tmp_path: Path) -> None:
    """Concurrent reload_module calls share one drain.

    Two reloads of the same module racing each other share one
    in-flight reload: the reload event fires exactly once and both
    callers get the same module back.
    """
    mod_file = tmp_path / "lconf16_dedup.py"
    mod_file.write_text("VALUE = 1\n")
    ctx = Context()
    hmr = HmrService(ctx, {"root": tmp_path})
    mod_name = "lconf16_dedup"
    spec = importlib.util.spec_from_file_location(mod_name, str(mod_file))
    assert spec is not None
    mod = importlib.util.module_from_spec(spec)
    mod.__spec__ = spec
    sys.modules[mod_name] = mod
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    reloads: list[dict[Any, Reload]] = []
    ctx.on("hmr/reload", lambda m: reloads.append(m))
    try:
        mod_file.write_text("VALUE = 2\n")
        _l_bump(mod_file)
        first, second = await asyncio.gather(
            hmr.reload_module(mod_name, path=mod_file),
            hmr.reload_module(mod_name, path=mod_file),
        )
        assert first is second
        assert first is not None
        assert first.VALUE == 2
        assert len(reloads) == 1
    finally:
        sys.modules.pop(mod_name, None)


# ---------------------------------------------------------------------------
# L17 — loader_utils.py deleted
# ---------------------------------------------------------------------------


def test_conformance_l17_loader_utils_deleted() -> None:
    """loader_utils is deleted: unimportable, zero references in src/ and README.md."""
    with pytest.raises(ImportError):
        importlib.import_module("anima.loader_utils")

    root = _l_repo_root()
    hits: list[str] = []
    for path in sorted(root.joinpath("src").rglob("*.py")):
        if "loader_utils" in path.read_text(encoding="utf-8"):
            hits.append(str(path.relative_to(root)))
    readme = root / "README.md"
    if readme.exists() and "loader_utils" in readme.read_text(encoding="utf-8"):
        hits.append("README.md")
    assert hits == []


def test_conformance_l17_unwrap_exports_home() -> None:
    """unwrap_exports lives in tree.py, with the LoaderService alias."""
    from anima.tree import unwrap_exports as tree_unwrap

    assert unwrap_exports is tree_unwrap
    assert LoaderService.unwrap_exports is tree_unwrap

    assert unwrap_exports(None) is None
    assert unwrap_exports("component") == "component"
    ns = SimpleNamespace(default="my-component", other="value")
    assert unwrap_exports(ns) == "my-component"
    assert unwrap_exports(SimpleNamespace(other="value")) is not None
    esm = SimpleNamespace(default="esm-component", __es_module=True)
    assert unwrap_exports(esm) == "esm-component"
    esm_no_default = SimpleNamespace(__es_module=True)
    assert unwrap_exports(esm_no_default) is esm_no_default


# The original-surface label every anima-original module carries in its
# module docstring (audit §3.6).
_ORIGINAL_LABEL = "Anima-original surface: not part of the ported kernel."

_ORIGINAL_MODULES = [
    "server_echo",
    "server_acl",
    "server_proxy",
    "server_static",
    "http_socks",
    "server",
    "http",
    "pkg",
    "watcher",
    "cli",
    "cli_env",
]


def _repo_root() -> Path:
    """Return the anima repo root (two levels above this file)."""
    return Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# §3.4 — Shared utilities
# ---------------------------------------------------------------------------


def test_conformance_u_dict_alias() -> None:
    """``Dict`` is the plain ``dict`` type, subscriptable and exported.

    The oracle declares it as a string-keyed mapped type
    (oracle misc.ts:2); Python has no mapped-type spelling, so the
    port is the builtin ``dict`` itself, kept subscriptable on
    purpose (see the alias comment in utils.py).
    """
    assert Dict is dict
    assert Dict[str, int] == dict[str, int]
    assert isinstance({"a": 1}, Dict)
    assert "Dict" in utils.__all__
    assert "Dict" in anima.__all__


def test_conformance_u_no_define_property_helper() -> None:
    """No ``define_property`` helper exists; the decision is documented.

    Python has no non-enumerable properties — ``setattr`` lands
    visibly — so a helper by that name would be misleading. The
    utils module docstring records exactly that decision, and no
    attribute by that name exists on the module.
    """
    assert not hasattr(utils, "define_property")
    assert "define_property" not in dir(utils)
    assert "No ``define_property`` helper" in (utils.__doc__ or "")


def test_conformance_u_nullable_none_only() -> None:
    """Nullable means None only; Python has no ``undefined``.

    The oracle's check covers null, undefined and void
    (oracle misc.ts:20-28); the port's only nullable value is None,
    so every other falsy value is non-nullable.
    """
    assert is_nullable(None)
    assert not is_nullable(0)
    assert not is_nullable("")
    assert not is_nullable(False)
    assert not is_nullable([])
    assert is_non_nullable(0)
    assert is_non_nullable("")
    assert not is_non_nullable(None)


def test_conformance_u_deep_equal() -> None:
    """``deep_equal`` is volatile-aware, strict on null-vs-undefined, cycle-safe.

    Two volatile references compare equal regardless of value; a
    volatile never equals a plain value. Non-strict mode treats null
    and undefined as equal, strict mode does not (oracle types.ts:127).
    Distinct cyclic structures compare unequal; the identical object
    compares equal to itself. Booleans never equal numbers.
    """
    assert deep_equal({"a": [1, 2]}, {"a": [1, 2]})
    assert not deep_equal({"a": [1, 2]}, {"a": [1, 3]})
    assert not deep_equal([1, 2], (1, 2, 3))
    assert deep_equal([1, 2], (1, 2))
    assert deep_equal(1, 1.0)
    assert not deep_equal(True, 1)
    assert not deep_equal(False, 0)

    # Null vs undefined: equal non-strict, unequal strict. ``_UNDEFINED``
    # is the port's undefined sentinel (schema.py).
    assert deep_equal(None, _UNDEFINED)
    assert not deep_equal(None, _UNDEFINED, strict=True)
    assert deep_equal(None, None, strict=True)

    # Volatile-aware: any two volatile references are equal, a volatile
    # never equals its plain snapshot.
    first = create_volatile("x")
    second = create_volatile("y")
    assert deep_equal(first, second)
    assert not deep_equal(first, "x")
    assert deep_equal({"t": first}, {"t": second})

    # Cycles: distinct cyclic structures are unequal, the identical
    # object is equal to itself.
    cyclic_a: dict[str, Any] = {}
    cyclic_a["self"] = cyclic_a
    cyclic_b: dict[str, Any] = {}
    cyclic_b["self"] = cyclic_b
    assert not deep_equal(cyclic_a, cyclic_b)
    assert deep_equal(cyclic_a, cyclic_a)


def test_conformance_u_value_map_filter_keys_pick() -> None:
    """``valueMap`` / ``filterKeys`` / ``pick`` transform dicts by key and value.

    The oracle maps values with ``(value, key)`` and filters with
    ``(key, value)`` (oracle misc.ts:39-50); ``pick`` skips null
    values unless forced (oracle misc.ts:52). The port keeps both
    spellings: ``map_values`` and its ``value_map`` alias.
    """
    assert map_values({"a": 1, "b": 2}, lambda v, k: v * 2) == {"a": 2, "b": 4}
    assert value_map({"a": 1}, lambda v, k: f"{k}={v}") == {"a": "a=1"}
    source = {"a": 1}
    mapped = map_values(source, lambda v, k: v)
    assert mapped == source
    assert mapped is not source
    assert filter_keys({"a": 1, "b": 2}, lambda k, v: v > 1) == {"b": 2}
    assert filter_keys({"a": 1}, lambda k, v: k == "z") == {}
    assert pick({"a": 1, "b": None}, ["a", "b"]) == {"a": 1}
    assert pick({"a": 1}, ["a", "b"], forced=True) == {"a": 1, "b": None}
    source = {"a": 1}
    assert pick(source) == source
    assert pick(source) is not source


def test_conformance_u_promisify_type() -> None:
    """``Promisify`` is a coroutine type alias: ``Coroutine[Any, Any, T]``.

    The oracle unwraps an inner promise (oracle misc.ts:10); Python
    has no promise-unwrapping, so the port is always a coroutine.
    The alias subscripts and awaits like a coroutine.
    """
    assert typing.get_origin(Promisify[int]) is Promisify
    assert typing.get_args(Promisify[int]) == (int,)
    assert "Promisify" in utils.__all__

    async def _produce() -> Promisify[int]:
        return 3

    async def _await_it() -> int:
        return await _produce()

    assert asyncio.run(_await_it()) == 3


def test_conformance_u_awaitable() -> None:
    """``Awaitable`` is ``collections.abc.Awaitable`` (core.py).

    The oracle's conditional awaitable type (oracle misc.ts:12) has
    no Python spelling; the port re-exports the abstract base class
    from the core module, and coroutines satisfy it.
    """
    assert core.Awaitable is collections.abc.Awaitable

    async def _coro() -> int:
        return 1

    coro = _coro()
    try:
        assert isinstance(coro, core.Awaitable)
    finally:
        coro.close()


def test_conformance_u_hyphenate() -> None:
    """``hyphenate`` converts text to dash-delimited parameter case.

    Camel humps split, acronym boundaries split before the last
    capital, and repeated delimiters collapse to one
    (oracle string.ts:69).
    """
    assert hyphenate("fooBar") == "foo-bar"
    assert hyphenate("FOOBar") == "foo-bar"
    assert hyphenate("foo_bar") == "foo-bar"
    assert hyphenate("foo--bar") == "foo-bar"
    assert hyphenate("already-hyphenated") == "already-hyphenated"
    assert hyphenate("") == ""


def test_conformance_u_time_constants_and_parse() -> None:
    """``Time`` constants and ``parse_time`` match the recorded values.

    Constants are milliseconds (oracle time.ts:3-9). Durations parse
    in week/day/hour/minute/second order, fractions allowed, and an
    unparseable string parses as 0.
    """
    assert Time.millisecond == 1
    assert Time.second == 1000
    assert Time.minute == 60_000
    assert Time.hour == 3_600_000
    assert Time.day == 86_400_000
    assert Time.week == 604_800_000

    assert Time.parse_time("1w2d3h4m5s") == 788_645_000.0
    assert Time.parse_time("90s") == 90_000.0
    assert Time.parse_time("1.5h") == 5_400_000.0
    assert Time.parse_time("2d") == 172_800_000.0
    assert Time.parse_time("garbage") == 0.0
    assert Time.parse_time("") == 0.0


def test_conformance_u_time_format_and_digits() -> None:
    """``Time.format`` picks the largest fitting unit; ``to_digits`` pads.

    Halves round toward +infinity, as JavaScript ``Math.round`` does
    (Python's ``round`` rounds halves to even), so 90 seconds formats
    as ``2m`` and 1500 ms as ``2s``. Integral milliseconds render
    without a fraction (``500ms``, never ``500.0ms``).
    """
    assert Time.format(2 * Time.day) == "2d"
    assert Time.format(90 * Time.minute) == "2h"
    assert Time.format(90 * Time.second) == "2m"
    assert Time.format(1500) == "2s"
    assert Time.format(500) == "500ms"
    assert Time.format(0) == "0ms"

    assert Time.to_digits(5) == "05"
    assert Time.to_digits(5, 3) == "005"
    assert Time.to_digits(123) == "123"


def test_conformance_u_time_template_and_parse_date() -> None:
    """``Time.template`` fills slots once, in order; ``parse_date`` parses.

    Each placeholder is replaced once, in ``yyyy``/``yy``/``MM``/``dd``/
    ``hh``/``mm``/``ss``/``SSS`` order — so ``"yyyy-yyyy"`` becomes
    ``"2026-26yy"`` (the later ``yy`` sees what the earlier ``yyyy``
    left behind). ``parse_date`` reads ISO dates and ``HH:MM`` times,
    and raises ``ValueError`` where the oracle yields an invalid date
    (Python has no invalid-date value).
    """
    moment = datetime(2026, 11, 12, 14, 30, 5, 123000)
    assert Time.template("yyyy-MM-dd hh:mm:ss", moment) == "2026-11-12 14:30:05"
    assert Time.template("ss.SSS", moment) == "05.123"
    assert Time.template("yyyy-yyyy", datetime(2026, 1, 1)) == "2026-26yy"

    assert Time.parse_date("2026-11-12") == datetime(2026, 11, 12, tzinfo=UTC)
    assert Time.parse_date("09:30").strftime("%H:%M") == "09:30"
    before = datetime.now(UTC)
    parsed = Time.parse_date("1h")
    after = datetime.now(UTC)
    assert before.timestamp() + 3600 <= parsed.timestamp() <= after.timestamp() + 3600
    with pytest.raises(ValueError, match='invalid date "1-2-3:4"'):
        Time.parse_date("1-2-3:4")


def test_conformance_u_time_date_number_round_trip_and_offset() -> None:
    """Date numbers round-trip; the timezone offset is settable state.

    The day number is days since the epoch (oracle time.ts:20-29);
    converting back yields the start of that day. The offset is
    process-wide mutable state, so the test restores it.
    """
    day = datetime(2026, 11, 12, 15, 0, tzinfo=UTC)
    number = Time.get_date_number(day, offset=0)
    assert number == 20769
    assert Time.from_date_number(number, offset=0) == datetime(2026, 11, 12, tzinfo=UTC)
    assert Time.get_date_number(datetime(2026, 11, 13, tzinfo=UTC), offset=0) == (
        number + 1
    )

    previous = Time.get_timezone_offset()
    try:
        Time.set_timezone_offset(42)
        assert Time.get_timezone_offset() == 42
    finally:
        Time.set_timezone_offset(previous)
    assert Time.get_timezone_offset() == previous


def test_conformance_u_binary_partial() -> None:
    """``Binary`` is a documented partial: source helpers ported, base64/hex not.

    ``is_source``/``from_source`` cover bytes-like values (oracle
    types.ts:32-44); ``fromBase64``/``fromHex`` were NOT ported
    (oracle types.ts:56-68) and no consumer exists — the schema
    module never imports ``Binary``. Pinned here as a deliberate
    partial, not a failure: the ported pair works, and the missing
    pair is asserted absent in both spellings.
    """
    assert Binary.is_source(b"x")
    assert Binary.is_source(bytearray(b"x"))
    assert Binary.is_source(memoryview(b"x"))
    assert not Binary.is_source("x")
    assert not Binary.is_source(5)

    assert Binary.from_source(b"x") == b"x"
    assert Binary.from_source(bytearray(b"x")) == b"x"
    assert Binary.from_source(memoryview(b"abcdef")[1:3]) == b"bc"
    assert Binary.from_source(5) == 5

    assert not hasattr(Binary, "from_base64")
    assert not hasattr(Binary, "fromBase64")
    assert not hasattr(Binary, "from_hex")
    assert not hasattr(Binary, "fromHex")
    assert sorted(name for name in dir(Binary) if not name.startswith("_")) == [
        "from_source",
        "is_source",
    ]


def test_conformance_u_clone() -> None:
    """``clone`` deep-copies plain data, preserves cycles, keeps volatiles.

    Volatile references are returned as-is — references are stable
    (oracle types.ts:88-125). Cycles round-trip as cycles, not as
    infinite recursion; plain dicts and lists come back equal but
    independent.
    """
    data = {"x": [1, {"y": 2}]}
    copied = clone(data)
    assert copied == data
    assert copied is not data
    assert copied["x"] is not data["x"]
    copied["x"][1]["y"] = 99
    assert data["x"][1]["y"] == 2

    cyclic: dict[str, Any] = {}
    cyclic["self"] = cyclic
    cloned_cyclic = clone(cyclic)
    assert cloned_cyclic is not cyclic
    assert cloned_cyclic is cloned_cyclic["self"]

    ref = create_volatile("stable")
    assert clone(ref) is ref
    assert clone({"v": ref})["v"] is ref
    assert clone("leaf") == "leaf"
    assert clone(None) is None


def test_conformance_u_is_plain_object_narrow() -> None:
    """``is_plain_object`` is dict-only — narrower than the oracle, documented.

    The oracle accepts any non-array object (oracle misc.ts:30-33);
    the port accepts only dicts: class instances and dates are
    excluded, and the utils docstring says so. Lists are not plain
    objects on either side.
    """
    assert is_plain_object({})
    assert is_plain_object({"a": 1})
    assert not is_plain_object([])
    assert not is_plain_object("text")
    assert not is_plain_object(42)
    assert not is_plain_object(None)

    class _Thing:
        pass

    assert not is_plain_object(_Thing())
    assert not is_plain_object(datetime.now())


def test_conformance_u_volatile_quartet_wired() -> None:
    """The volatile quartet is importable, exported, and wired.

    ``create_volatile`` / ``is_volatile`` / ``update_volatile`` /
    ``volatile_entries`` (oracle volatile.ts) are exported from the
    top-level package as well as from ``anima.volatile``. The
    snapshot is deep-frozen, the reference is stable across commits,
    and entries report object-key paths. The loader commit path that
    consumes these is pinned by the S2 section, not duplicated here.
    """
    for name in (
        "create_volatile",
        "is_volatile",
        "update_volatile",
        "volatile_entries",
    ):
        assert name in anima.__all__
        assert getattr(anima, name) is globals()[name]

    ref = create_volatile({"a": 1, "b": [2, 3]})
    assert is_volatile(ref)
    assert not is_volatile({"a": 1})
    assert ref.get() == {"a": 1, "b": [2, 3]}
    with pytest.raises(TypeError, match="immutable"):
        ref.get()["a"] = 99
    with pytest.raises(TypeError, match="immutable"):
        ref.get()["b"].append(4)

    other = create_volatile({"a": 7})
    update_volatile(ref, other)
    assert ref.get() == {"a": 7}
    assert other.get() == {"a": 7}

    nested = {"x": create_volatile("one"), "y": {"z": create_volatile("two")}}
    found = volatile_entries(nested)
    assert [entry["path"] for entry in found] == [["x"], ["y", "z"]]
    root = create_volatile("root")
    assert volatile_entries(root) == [{"path": [], "ref": root}]


# ---------------------------------------------------------------------------
# §3.5 — Symbols
# ---------------------------------------------------------------------------


def test_conformance_u_symbols_are_plain_strings() -> None:
    """Symbols are plain strings under the ``"anima.*"`` prefix.

    Python has no JS-``Symbol`` equivalent, so the protocol keys are
    ordinary, enumerable, collidable dict keys (oracle utils.ts:50-73
    uses a registry of unique symbols). Every value is a string and
    carries the anima prefix — no value uses any other prefix.
    """
    assert core.symbols
    assert all(isinstance(value, str) for value in core.symbols.values())
    assert all(value.startswith("anima.") for value in core.symbols.values())
    assert all(key in value for key, value in core.symbols.items())
    assert core.symbols["init"] == "anima.init"
    assert core.symbols["effect"] == "anima.effect"
    assert core.symbols["invoke"] == "anima.invoke"


def test_conformance_u_symbols_key_set() -> None:
    """Key-set: ``config``/``resolveConfig`` added, old keys gone, intercepts kept.

    ``config`` and ``resolveConfig`` are present in both
    ``Service.__symbols__`` and ``Context.symbols``. The earlier
    ``caller`` / ``decorator`` / ``root`` keys are gone from every
    symbols dict. ``mergeIntercept`` / ``resolveIntercept`` are kept
    and documented in core.py as anima-only intercept-chain machinery
    with no upstream counterpart.
    """
    for mapping in (core.symbols, Service.__symbols__, Context.symbols):
        assert "config" in mapping
        assert "resolveConfig" in mapping
        assert mapping["config"] == "anima.config"
        assert mapping["resolveConfig"] == "anima.resolveConfig"
        assert "caller" not in mapping
        assert "decorator" not in mapping
        assert "root" not in mapping
        assert mapping["mergeIntercept"] == "anima.mergeIntercept"
        assert mapping["resolveIntercept"] == "anima.resolveIntercept"

    assert Service.__symbols__["init"] == core.symbols["init"]
    assert Context.symbols["effect"] == core.symbols["effect"]

    source = (Path(core.__file__).parent / "core.py").read_text(encoding="utf-8")
    assert "anima-only intercept-chain machinery" in source


def test_conformance_u_fiber_state_wire_values() -> None:
    """FiberState wire values match the oracle's numeric order exactly.

    The oracle declares the states as a const enum in PENDING,
    LOADING, ACTIVE, FAILED, DISPOSED, UNLOADING order (oracle
    fiber.ts:147-153), so the wire numbers are 0–5 in that order.
    Anima compares states by name (ADR 0009); this mapping is the
    name-based wire record, and no code compares numerically.
    """
    assert core.FIBER_STATE_WIRE_VALUES == {
        FiberState.PENDING: 0,
        FiberState.LOADING: 1,
        FiberState.ACTIVE: 2,
        FiberState.FAILED: 3,
        FiberState.DISPOSED: 4,
        FiberState.UNLOADING: 5,
    }
    assert set(core.FIBER_STATE_WIRE_VALUES) == set(FiberState)


# ---------------------------------------------------------------------------
# §3.6 — Other dead / unwired scaffolding
# ---------------------------------------------------------------------------


def test_conformance_u_loader_utils_deleted() -> None:
    """``loader_utils`` is deleted: unimportable, zero references.

    The module was removed in P1-9; its surviving helper lives in
    ``tree.py``. No file under ``src/`` or ``README.md`` may still
    mention it.
    """
    with pytest.raises(ImportError):
        importlib.import_module("anima.loader_utils")

    root = _repo_root()
    hits: list[str] = []
    for path in sorted(root.joinpath("src").rglob("*.py")):
        if "loader_utils" in path.read_text(encoding="utf-8"):
            hits.append(str(path.relative_to(root)))
    readme = root / "README.md"
    if readme.exists() and "loader_utils" in readme.read_text(encoding="utf-8"):
        hits.append("README.md")
    assert hits == []


def test_conformance_u_create_subcommand_via_console_main(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``create [name]`` is reachable through the real CLI entry point.

    The subcommand is registered as ``create [name:string]`` with
    default name ``my-app`` (cli.py), and the console script maps to
    ``console_main`` (pyproject). The scaffold's network-bound start
    is stubbed at the boundary — the wiring under test is entry point
    to action dispatch, not the download.
    """
    calls: list[dict[str, Any]] = []

    def fake_start(self: Scaffold, prompt: Any = input) -> None:
        calls.append(dict(self.options))

    monkeypatch.setattr(Scaffold, "start", fake_start)

    monkeypatch.setattr(sys, "argv", ["anima", "create"])
    assert console_main() == 0
    assert calls == [{"name": "my-app"}]

    calls.clear()
    monkeypatch.setattr(sys, "argv", ["anima", "create", "demo-app"])
    assert console_main() == 0
    assert calls == [{"name": "demo-app"}]


def test_conformance_u_cli_env_public_surface() -> None:
    """``cli_env`` is kept as public surface, exported from the package."""
    assert CliEnv is importlib.import_module("anima.cli_env").CliEnv
    assert "CliEnv" in anima.__all__
    assert CliEnv.name == "cli:env"


@pytest.mark.parametrize("module_name", _ORIGINAL_MODULES)
def test_conformance_u_original_surface_labels(module_name: str) -> None:
    """Each anima-original module labels itself in its module docstring.

    The label is the observable contract for the §3.6 decision: these
    modules are anima-original surface, not part of the ported kernel.
    """
    module = importlib.import_module(f"anima.{module_name}")
    assert module.__doc__ is not None
    assert _ORIGINAL_LABEL in module.__doc__


@pytest.mark.asyncio
async def test_conformance_u_core_service_kept_with_justification() -> None:
    """``CoreService`` is kept, with its justification in its docstring.

    The docstring records why it lives in ``src/`` rather than in a
    test fixture — shared trivial component for anima's own suite and
    downstream suites — and it still mounts and activates as a live
    component.
    """
    assert CoreService.__doc__ is not None
    assert "lives here rather than in a test fixture" in CoreService.__doc__

    ctx = Context()
    fiber = ctx.use(CoreService)
    await ctx.flush()
    assert fiber.state == FiberState.ACTIVE
    assert ctx.has("core_svc")


@pytest.mark.asyncio
async def test_conformance_u_is_js_expr_wired(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``is_js_expr`` is kept and wired: detected, evaluated, loader-driven.

    Detection is stricter than the oracle — the ``__jsExpr`` value
    must be a string. A JsExpr dict flows through the loader path:
    an entry whose ``disabled`` is a JsExpr evaluating true stays
    unmounted, while one evaluating false mounts and activates
    (``Entry._disabled_of`` in tree.py). The wiring itself is pinned
    by the import in all three consuming modules.
    """
    assert is_js_expr({"__jsExpr": "process.platform"})
    assert is_js_expr({"__jsExpr": "x", "extra": 1})
    assert not is_js_expr({"__jsExpr": 5})
    assert not is_js_expr({"other": "process.platform"})
    assert not is_js_expr("process.platform")

    ctx = Context()
    expected = sys.platform  # "linux" / "darwin" / "win32" — never hardcoded
    other = "win32" if expected != "win32" else "linux"
    assert interpolate(ctx, {"__jsExpr": "process.platform"}) == expected
    assert interpolate(
        ctx, {"k": {"__jsExpr": f"process.platform === '{expected}'"}}
    ) == {"k": True}

    root = _repo_root()
    for name in ("tree.py", "jsexpr.py", "include.py"):
        source = (root / "src" / "anima" / name).read_text(encoding="utf-8")
        assert "is_js_expr" in source, name

    ctx2 = Context()
    loader = LoaderService(ctx2)
    module = types.ModuleType("conformance_u_probe_mod")

    class _Dummy:
        name = "dummy"

        def apply(self, ctx: Context, config: Any) -> None:
            return None

    module.Dummy = _Dummy
    monkeypatch.setitem(sys.modules, "conformance_u_probe_mod", module)

    disabled_id = await loader.create(
        {
            "id": "u-disabled",
            "name": "conformance_u_probe_mod:Dummy",
            "disabled": {"__jsExpr": f"process.platform === '{expected}'"},
        }
    )
    disabled_entry = loader.resolve(disabled_id)
    assert disabled_entry.disabled is True
    assert disabled_entry.fiber is None

    enabled_id = await loader.create(
        {
            "id": "u-enabled",
            "name": "conformance_u_probe_mod:Dummy",
            "disabled": {"__jsExpr": f"process.platform === '{other}'"},
        }
    )
    await loader.await_all()
    enabled_entry = loader.resolve(enabled_id)
    assert enabled_entry.disabled is False
    assert enabled_entry.fiber is not None
    assert enabled_entry.fiber.state == FiberState.ACTIVE
