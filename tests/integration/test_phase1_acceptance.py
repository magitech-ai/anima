"""Phase 1 acceptance test (P1-10): recorded-trace conformance suite.

The fixture below is a plugin written once in the upstream shape and
reused by every test in this module: a service subclass with a static
``inject`` declaration and a schema ``Config``, plus an object component
(``{name, inject, apply}``) that consumes it through ``ctx.on`` /
``ctx.provide`` / ``ctx.use`` and exercises waterfall, bail, serial and
parallel dispatch.

Recording basis: the upstream source cannot be executed here — a bare
Node runtime cannot import the vendored TypeScript oracle, so no live
side-by-side run is possible (the P1-0 verdict). Each expectation below
is therefore recorded as data, with a comment citing the oracle
file:line it comes from. The test asserts anima produces exactly that
recorded trace. Where anima's own surface differs from the oracle's,
the difference is pinned as count-only and called out in the comment
instead of being silently asserted as equivalent.
"""

import asyncio
import sys
import types
from typing import Any, ClassVar

import pytest

from anima.core import Context, EventMode, FiberState, Service
from anima.loader import LoaderService
from anima.schema import Schema, ValidationError
from anima.volatile import is_volatile

# ---------------------------------------------------------------------------
# Shared fixture: one plugin, written once, reused by every test.
# ---------------------------------------------------------------------------


class GreeterService(Service):
    """Named capability with a schema Config (oracle service.ts:11).

    The service registers itself under ``"greeter"`` the moment it is
    constructed (oracle service.ts:42-62), and the fiber validates its
    config against ``Config`` before the body runs (oracle fiber.ts:55-71).
    """

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


class ConsumerPlugin:
    """Object component (oracle registry.ts:100-112, ``Plugin.Base``;
    registry.ts:131-133, ``Plugin.Object``).

    Declares its dependency as a plain ``inject`` list (oracle
    registry.ts:75-90, array form) and its config as a schema with a
    volatile field (oracle schema index.ts:480-482). ``apply`` runs
    with ``(ctx, config)`` (oracle fiber.ts:250-259) and registers
    listeners, a provided value, and disposers.
    """

    name = "consumer"
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
        self.state: dict[str, Any] = {}

    def apply(self, ctx: Context, config: Any) -> Any:
        theme = config["theme"]
        self.applies.append({"theme": theme.get(), "level": config.get("level")})
        greeting = ctx.greeter.greet("world")
        state: dict[str, Any] = {"seen": [], "greeting": greeting}
        self.state = state

        ctx.provide("consumer-state", state)

        ctx.on(
            "acceptance/ping",
            lambda payload: state["seen"].append(("ping", payload)),
        )
        ctx.on(
            "acceptance/ping",
            lambda payload: state["seen"].append(("ping-2", payload)),
        )

        ctx.on("acceptance/question", lambda _q: None, mode=EventMode.BAIL)
        ctx.on("acceptance/question", lambda _q: False, mode=EventMode.BAIL)
        ctx.on("acceptance/question", lambda _q: "answer", mode=EventMode.BAIL)
        ctx.on(
            "acceptance/question",
            lambda _q: state["seen"].append(("question-4", None)),
            mode=EventMode.BAIL,
        )

        async def serial_first(_q: Any) -> Any:
            state["seen"].append(("serial-1", None))
            return None

        async def serial_second(_q: Any) -> Any:
            state["seen"].append(("serial-2", None))
            return "serial-win"

        async def serial_third(_q: Any) -> Any:
            state["seen"].append(("serial-3", None))
            return "serial-3"

        ctx.on("acceptance/serial", serial_first, mode=EventMode.SERIAL)
        ctx.on("acceptance/serial", serial_second, mode=EventMode.SERIAL)
        ctx.on("acceptance/serial", serial_third, mode=EventMode.SERIAL)

        async def par_slow(_x: Any) -> Any:
            await asyncio.sleep(0.05)
            state["seen"].append(("par-slow", None))
            return "slow"

        async def par_fast(_x: Any) -> Any:
            state["seen"].append(("par-fast", None))
            return "fast"

        ctx.on("acceptance/work", par_slow, mode=EventMode.PARALLEL)
        ctx.on("acceptance/work", par_fast, mode=EventMode.PARALLEL)

        async def wf_outer(payload: str, next_fn: Any) -> Any:
            state["seen"].append(("wf-outer-before", None))
            out = await next_fn(payload + "!")
            state["seen"].append(("wf-outer-after", None))
            return out + "?"

        async def wf_inner(payload: str, next_fn: Any) -> Any:
            state["seen"].append(("wf-inner", None))
            return await next_fn(payload + "*")

        ctx.on("acceptance/transform", wf_outer, mode=EventMode.WATERFALL)
        ctx.on("acceptance/transform", wf_inner, mode=EventMode.WATERFALL)

        async def wf_veto(payload: str, next_fn: Any) -> Any:
            state["seen"].append(("wf-veto", None))
            return "vetoed"

        ctx.on("acceptance/veto", wf_veto, mode=EventMode.WATERFALL)

        ctx.effect(
            lambda: lambda: state["seen"].append(("effect-a", None)),
            label="acceptance-a",
        )
        ctx.effect(
            lambda: lambda: state["seen"].append(("effect-b", None)),
            label="acceptance-b",
        )

        def _apply_disposer() -> None:
            state["seen"].append(("apply", None))

        return _apply_disposer


# ---------------------------------------------------------------------------
# Recorded oracle traces.
#
# Each tuple is (event, fiber name, new state, old state) for status,
# (event, fiber name, uid) for plugin, or (event, service name, value
# is None) for service. Citations follow each trace.
# ---------------------------------------------------------------------------

# Mount of a service-providing component (oracle fiber.ts:302 emits
# internal/plugin at construction; fiber.ts:579-592 emits internal/status
# on LOADING then ACTIVE, and notifies the reflection layer on the ACTIVE
# transition, which emits internal/service — oracle reflect.ts:321-336;
# the provide itself does NOT notify while the fiber is still loading —
# oracle reflect.ts:294-296).
GREETER_MOUNT_EVENTS = [
    ("plugin", "GreeterService", 1),
    ("status", "GreeterService", "LOADING", "PENDING"),
    ("status", "GreeterService", "ACTIVE", "LOADING"),
    ("service", "greeter", False),
]

# Mount of the object component: plugin + status only. Its provided
# value is registered through the context-level provide, which notifies
# at registration time — count-only below, not position-pinned (see the
# test). Its status sequence is the same shape as the greeter's
# (oracle fiber.ts:302, fiber.ts:579-592).
CONSUMER_MOUNT_STATUS = [
    ("plugin", "consumer", 2),
    ("status", "consumer", "LOADING", "PENDING"),
    ("status", "consumer", "ACTIVE", "LOADING"),
]

# Disposal of the object component (oracle fiber.ts:265-300: uid is
# cleared first, then internal/plugin is emitted, then the epoch goes
# inactive; fiber.ts:702-722 runs the disposers, and the fiber settles
# DISPOSED — oracle fiber.ts:579-592 emits both status transitions).
CONSUMER_DISPOSE_EVENTS = [
    ("plugin", "consumer", None),
    ("status", "consumer", "UNLOADING", "ACTIVE"),
    ("status", "consumer", "DISPOSED", "UNLOADING"),
]

# Disposal of the service component. The two service events bracket the
# unload: the ACTIVE -> UNLOADING transition notifies the still-present
# service (oracle fiber.ts:579-592 + reflect.ts:321-336), then the
# provide disposer deletes it and notifies again with no value
# (oracle reflect.ts:297-303).
GREETER_DISPOSE_EVENTS = [
    ("plugin", "GreeterService", None),
    ("status", "GreeterService", "UNLOADING", "ACTIVE"),
    ("service", "greeter", False),
    ("service", "greeter", True),
    ("status", "GreeterService", "DISPOSED", "UNLOADING"),
]


def _record(ctx: Context) -> list[tuple[Any, ...]]:
    """Record internal/plugin, internal/status and internal/service."""
    events: list[tuple[Any, ...]] = []
    ctx.on(
        "internal/plugin",
        lambda fiber: events.append(("plugin", fiber.name, fiber.uid)),
    )
    ctx.on(
        "internal/status",
        lambda fiber, old: events.append(
            ("status", fiber.name, fiber.state.value, old.value)
        ),
    )
    ctx.on(
        "internal/service",
        lambda name, value: events.append(("service", name, value is None)),
    )
    return events


async def _mount(ctx: Context) -> tuple[Any, ConsumerPlugin, Any]:
    """Mount the shared fixture: greeter service, then consumer."""
    greeter_fiber = ctx.use(GreeterService, greeting="hi", level=1)
    await ctx.flush()
    consumer = ConsumerPlugin()
    consumer_fiber = ctx.use(consumer, theme="dark", level=3)
    await ctx.flush()
    return greeter_fiber, consumer, consumer_fiber


def _install_fixture_module(
    monkeypatch: pytest.MonkeyPatch, name: str, consumer: ConsumerPlugin
) -> None:
    """Expose the shared consumer instance as an importable module attr."""
    module = types.ModuleType(name)
    module.consumer = consumer
    monkeypatch.setitem(sys.modules, name, module)


# ---------------------------------------------------------------------------
# Part 1 — mount / disposal / dispatch / validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_acceptance_mount_event_sequence() -> None:
    """The fixture mounts with exactly the recorded oracle event sequence."""
    ctx = Context()
    events = _record(ctx)
    greeter_fiber, consumer, consumer_fiber = await _mount(ctx)

    assert greeter_fiber.state == FiberState.ACTIVE
    assert consumer_fiber.state == FiberState.ACTIVE
    assert ctx.greeter.greet("world") == "hi, world"
    assert ctx.has("consumer-state")
    assert consumer.state["greeting"] == "hi, world"
    assert consumer.applies == [{"theme": "dark", "level": 3}]
    assert is_volatile(consumer_fiber.config["theme"])
    assert consumer_fiber.config["theme"].get() == "dark"

    # Greeter (reflection-provided service): full exact sequence.
    assert events[:4] == GREETER_MOUNT_EVENTS
    # Consumer: plugin + status subsequence exactly, in order.
    rest = [e for e in events[4:] if e[0] in ("plugin", "status")]
    assert rest == CONSUMER_MOUNT_STATUS
    # Consumer-state is registered through the context-level provide,
    # which notifies at registration time rather than at activation
    # (the oracle has a single provide path, so its notification lands
    # after the ACTIVE status — oracle reflect.ts:294-296). Pin the
    # count, not the position: exactly one service event for it.
    service_events = [e for e in events if e[0] == "service"]
    assert service_events == [
        ("service", "greeter", False),
        ("service", "consumer-state", False),
    ]


@pytest.mark.asyncio
async def test_acceptance_disposal_order() -> None:
    """Disposal runs effects last-in-first-out, then unregisters services.

    Disposers run in reverse registration order (oracle utils.ts:27-31
    reverses the list in clear(); fiber.ts:702-703 runs them on unload),
    and the fiber's uid is already cleared when internal/plugin fires
    (oracle fiber.ts:265-276).
    """
    ctx = Context()
    events = _record(ctx)
    greeter_fiber, consumer, consumer_fiber = await _mount(ctx)
    state = consumer.state
    events.clear()

    await consumer_fiber.dispose()
    await ctx.flush()

    assert events == CONSUMER_DISPOSE_EVENTS
    # Last registered runs first: the apply disposer, then effect-b,
    # then effect-a (registration order was a, b, apply-return).
    assert [mark for mark, _ in state["seen"]] == [
        "apply",
        "effect-b",
        "effect-a",
    ]
    # The listener and the provided value are gone with the fiber.
    assert not ctx.has("consumer-state")
    ctx.emit("acceptance/ping", "after")
    assert state["seen"] == [
        ("apply", None),
        ("effect-b", None),
        ("effect-a", None),
    ]
    # The dependency is untouched by the dependent's disposal.
    assert greeter_fiber.state == FiberState.ACTIVE
    assert ctx.has("greeter")

    events.clear()
    await greeter_fiber.dispose()
    await ctx.flush()

    assert events == GREETER_DISPOSE_EVENTS
    assert not ctx.has("greeter")


@pytest.mark.asyncio
async def test_acceptance_cascade_unload_and_recovery() -> None:
    """Losing a required service unloads the dependent; re-mount recovers it.

    A dependency disappearing re-checks the dependent's implementations
    and drops its epoch to inactive (oracle fiber.ts:597-623), so the
    dependent unloads to PENDING instead of being disposed; providing
    the service again reactivates the same fiber (oracle reflect.ts:321-336
    notifies dependents, fiber.ts:625-640 drives the reload).
    """
    ctx = Context()
    greeter_fiber, consumer, consumer_fiber = await _mount(ctx)

    await greeter_fiber.dispose()
    await consumer_fiber.await_settle()

    assert not ctx.has("greeter")
    assert consumer_fiber.state == FiberState.PENDING
    assert not ctx.has("consumer-state")

    ctx.use(GreeterService, greeting="yo", level=2)
    await ctx.flush()
    await consumer_fiber.await_settle()

    assert ctx.has("greeter")
    assert consumer_fiber.state == FiberState.ACTIVE
    assert len(consumer.applies) == 2
    assert consumer.applies[1] == {"theme": "dark", "level": 3}
    assert consumer.state["greeting"] == "yo, world"


@pytest.mark.asyncio
async def test_acceptance_dispatch_modes() -> None:
    """Waterfall, bail, serial and parallel dispatch match the oracle.

    Bail stops on the first bailed value (oracle events.ts:8-11 defines
    bailed as non-null, non-false; events.ts:217-222 runs bail in order).
    Serial awaits in order until one bails (events.ts:204-210). Parallel
    runs all listeners concurrently and returns results in registration
    order (events.ts:183-188). Waterfall composes listeners outermost-first
    around the default; not calling next vetoes the rest (events.ts:234-247).
    """
    ctx = Context()
    _greeter_fiber, consumer, _consumer_fiber = await _mount(ctx)
    seen = consumer.state["seen"]

    # Emit: synchronous broadcast, registration order.
    ctx.emit("acceptance/ping", "hello")
    assert ("ping", "hello") in seen
    assert ("ping-2", "hello") in seen
    assert seen.index(("ping", "hello")) < seen.index(("ping-2", "hello"))

    # Bail: None and False do not bail; the first real value wins and
    # the listener after it never runs.
    assert ctx.bail("acceptance/question", "q") == "answer"
    assert ("question-4", None) not in seen

    # Serial: awaited in order, first bailed value wins, rest skipped.
    assert await ctx.serial("acceptance/serial", "q") == "serial-win"
    assert ("serial-1", None) in seen
    assert ("serial-2", None) in seen
    assert ("serial-3", None) not in seen

    # Parallel: results in registration order even though the fast
    # listener finishes first; both ran.
    assert await ctx.parallel("acceptance/work", "w") == ["slow", "fast"]
    assert ("par-slow", None) in seen
    assert ("par-fast", None) in seen

    # Parallel failure: every listener still runs to completion (the
    # oracle waits for all of them — events.ts:183-188 uses all-settled
    # semantics — and then reports the failures as one aggregate error).
    # Anima raises the first error instead of an aggregate; pin the
    # shared half here (all listeners settle) and the first error.
    failing_ctx = Context()
    settled: list[str] = []

    async def boom(_x: Any) -> Any:
        raise RuntimeError("kaboom")

    async def slow_ok(_x: Any) -> Any:
        await asyncio.sleep(0.05)
        settled.append("slow-ok")
        return "slow-ok"

    failing_ctx.on("work", boom, mode=EventMode.PARALLEL)
    failing_ctx.on("work", slow_ok, mode=EventMode.PARALLEL)
    with pytest.raises(RuntimeError, match="kaboom"):
        await failing_ctx.parallel("work", "w")
    await asyncio.sleep(0.1)
    assert settled == ["slow-ok"]

    # Waterfall: outermost wraps the chain; the default runs innermost.
    result = await ctx.waterfall(
        "acceptance/transform", "x", default_handler=lambda p: f"default({p})"
    )
    assert result == "default(x!*)?"
    assert [mark for mark, _ in seen if mark.startswith("wf-")] == [
        "wf-outer-before",
        "wf-inner",
        "wf-outer-after",
    ]

    # Waterfall veto: returning without calling next skips the default.
    default_calls: list[str] = []
    vetoed = await ctx.waterfall(
        "acceptance/veto",
        "x",
        default_handler=lambda p: default_calls.append(p) or "default-ran",
    )
    assert vetoed == "vetoed"
    assert default_calls == []


@pytest.mark.asyncio
async def test_acceptance_config_waterfall_transform() -> None:
    """The internal/config waterfall can transform config before validation.

    Config resolution runs the internal/config waterfall first, then
    validates against the schema (oracle fiber.ts:667-671; the event is
    declared waterfall-mode at events.ts:334-340).
    """
    ctx = Context()

    async def uppercase_greeting(cfg: Any, next_fn: Any) -> Any:
        if isinstance(cfg, dict) and "greeting" in cfg:
            return await next_fn({**cfg, "greeting": cfg["greeting"].upper()})
        return await next_fn(cfg)

    ctx.on("internal/config", uppercase_greeting)
    fiber = ctx.use(GreeterService, greeting="hi", level=1)
    await ctx.flush()

    assert fiber.state == FiberState.ACTIVE
    assert fiber.config["greeting"] == "HI"
    assert ctx.greeter.greet("world") == "HI, world"


@pytest.mark.asyncio
async def test_acceptance_validation_errors() -> None:
    """Invalid config fails with the aggregated issue list, then recovers.

    Validation runs through the ``~standard`` seam (oracle fiber.ts:55-71),
    which reports a single issue per failure (oracle schema
    index.ts:290-305); the fiber aggregates the issue list into one
    message with dot-joined paths (oracle fiber.ts:24-38). The failure
    lands on the fiber and is rethrown by awaiting it (oracle
    fiber.ts:731-739); a later valid update recovers the fiber (oracle
    fiber.ts:764-783 defers resolution while non-ACTIVE).
    """
    ctx = Context()
    fiber = ctx.use(GreeterService, greeting=7, level="x")
    await ctx.flush()

    assert fiber.state == FiberState.FAILED
    err = fiber.error
    assert isinstance(err, ValidationError)
    # Two fields are wrong; the seam reports the first in declaration
    # order only — one issue, not one per field.
    assert err.issues == [
        {"message": "$.greeting expected string but got 7", "path": ["greeting"]}
    ]
    assert str(err) == (
        "invalid config:\n  - $.greeting expected string but got 7 (at greeting)"
    )
    with pytest.raises(ValidationError, match="invalid config"):
        await fiber.await_settle()

    # Nested paths are dot-joined in the aggregated line.
    _greeter_fiber, _consumer, consumer_fiber = await _mount(ctx)
    assert consumer_fiber.state == FiberState.ACTIVE
    bad_fiber = ctx.use(ConsumerPlugin(), theme="dark", level=3, nested={"count": "x"})
    await ctx.flush()
    assert bad_fiber.state == FiberState.FAILED
    nested_err = bad_fiber.error
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

    # A valid update recovers the failed fiber in place.
    await fiber.update({"greeting": "hey", "level": 2})
    await ctx.flush()
    assert fiber.state == FiberState.ACTIVE
    assert fiber.config == {"greeting": "hey", "level": 2}


# ---------------------------------------------------------------------------
# Part 2 — volatile config updates without reload (P1-6 acceptance, re-pinned)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_acceptance_volatile_update_no_reload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A volatile-only change updates the live value with no restart.

    Only an active fiber in an unchanged context takes volatile-only
    config changes without a remount (oracle loader entry.ts:141-155);
    the commit re-validates the candidate and swaps the changed
    snapshots, emitting the update event with the changed paths (oracle
    loader entry.ts:160-195; event declared at loader index.ts:34).
    """
    ctx = Context()
    events = _record(ctx)
    greeter_fiber = ctx.use(GreeterService, greeting="hi", level=1)
    await ctx.flush()
    assert greeter_fiber.state == FiberState.ACTIVE

    consumer = ConsumerPlugin()
    _install_fixture_module(monkeypatch, "acceptance_fixture_mod", consumer)
    loader = LoaderService(ctx)
    await loader.create(
        {
            "id": "acceptance",
            "name": "acceptance_fixture_mod:consumer",
            "config": {"theme": "dark", "level": 3},
        }
    )
    await loader.await_all()
    entry = loader.resolve("acceptance")
    fiber = entry.fiber
    assert fiber is not None and fiber.state == FiberState.ACTIVE
    assert len(consumer.applies) == 1
    theme_ref = fiber.config["theme"]
    assert is_volatile(theme_ref)
    assert theme_ref.get() == "dark"

    volatile_updates: list[list[list[str]]] = []
    partials: list[Any] = []
    entry.ctx.on("loader/volatile-update", lambda paths: volatile_updates.append(paths))
    ctx.on("loader/partial-dispose", lambda *args: partials.append(args))
    events.clear()

    await entry.update({"config": {"theme": "light", "level": 3}})

    assert len(consumer.applies) == 1  # no reload: the body did not run again
    assert entry.fiber is fiber
    assert fiber.state == FiberState.ACTIVE
    assert events == []  # no status transition at all
    assert partials == []  # the fast path never emits partial-dispose
    assert volatile_updates == [[["theme"]]]
    assert fiber.config["theme"] is theme_ref  # same stable reference
    assert theme_ref.get() == "light"  # live value updated in place
    assert fiber.raw_config == {"theme": "light", "level": 3}


@pytest.mark.asyncio
async def test_acceptance_volatile_and_ordinary_change_restarts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An ordinary-value change in the same update forces a full restart.

    When an ordinary effective value changed alongside the volatile
    ones, the commit refuses the fast path (oracle loader entry.ts:176-180)
    and the ordinary update lifecycle runs: partial-dispose, then a
    restart through the update waterfall (oracle fiber.ts:764-783).
    """
    ctx = Context()
    events = _record(ctx)
    ctx.use(GreeterService, greeting="hi", level=1)
    await ctx.flush()

    consumer = ConsumerPlugin()
    _install_fixture_module(monkeypatch, "acceptance_fixture_mod", consumer)
    loader = LoaderService(ctx)
    await loader.create(
        {
            "id": "acceptance",
            "name": "acceptance_fixture_mod:consumer",
            "config": {"theme": "dark", "level": 3},
        }
    )
    await loader.await_all()
    entry = loader.resolve("acceptance")
    fiber = entry.fiber
    assert fiber is not None and fiber.state == FiberState.ACTIVE
    theme_ref = fiber.config["theme"]
    assert theme_ref.get() == "dark"

    volatile_updates: list[list[list[str]]] = []
    partials: list[Any] = []
    entry.ctx.on("loader/volatile-update", lambda paths: volatile_updates.append(paths))
    ctx.on("loader/partial-dispose", lambda *args: partials.append(args))
    events.clear()

    await entry.update({"config": {"theme": "light", "level": 4}})
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
    # The restart's lifecycle transitions, in order.
    states = [e[2] for e in events if e[0] == "status"]
    assert states == ["UNLOADING", "LOADING", "ACTIVE"]
