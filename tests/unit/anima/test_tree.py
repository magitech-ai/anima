"""Unit tests for the loader entry tree (EntryTree, Entry, EntryGroup, Group)."""

import asyncio
import logging
import math
import re
import secrets
import sys
import types
from types import SimpleNamespace
from typing import Any

import pytest

from anima.core import Context, FiberState
from anima.events import EventMode
from anima.loader import LoaderService
from anima.schema import Schema
from anima.tree import (
    BUILTIN_ALIASES,
    Entry,
    EntryGroup,
    EntryTree,
    Group,
    resolve_component_target,
    unwrap_exports,
)
from anima.volatile import is_volatile


class MemoryTree(EntryTree):
    """In-memory tree subclass for subtree tests; counts writes."""

    def __init__(self, ctx: Context, loader: Any = None) -> None:
        super().__init__(ctx, loader=loader)
        self.writes = 0

    def write(self) -> None:
        self.writes += 1


def _install_module(monkeypatch: pytest.MonkeyPatch, name: str, **attrs: Any) -> Any:
    mod = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(mod, key, value)
    monkeypatch.setitem(sys.modules, name, mod)
    return mod


def _recorder(calls: list[Any], key: str = "comp") -> Any:
    def comp(ctx: Context, config: Any = None) -> None:
        calls.append((key, dict(config) if isinstance(config, dict) else config))

    return comp


@pytest.fixture
def mod(monkeypatch: pytest.MonkeyPatch) -> Any:
    """A dummy module with a recording component and a failing component."""
    calls: list[Any] = []

    def failing(ctx: Context, config: Any = None) -> None:
        raise RuntimeError("boom")

    module = _install_module(
        monkeypatch,
        "tree_test_mod",
        comp=_recorder(calls, "comp"),
        failing=failing,
        not_a_component=42,
    )
    module.calls = calls  # type: ignore[attr-defined]
    return module


def _row(name: str = "tree_test_mod:comp", **extra: Any) -> dict[str, Any]:
    row: dict[str, Any] = {"name": name}
    row.update(extra)
    return row


# --- unwrap_exports (moved from loader_utils) ---


def test_unwrap_exports_nullable():
    """unwrap_exports passes through None."""
    assert unwrap_exports(None) is None


def test_unwrap_exports_plain_value():
    """A non-module value is returned as-is."""
    assert unwrap_exports("component") == "component"
    assert unwrap_exports(42) == 42


def test_unwrap_exports_no_es_module():
    """A namespace without __es_module returns its default or itself."""
    ns = SimpleNamespace(default="my-component", other="value")
    assert unwrap_exports(ns) == "my-component"

    ns_no_default = SimpleNamespace(other="value")
    assert unwrap_exports(ns_no_default) is ns_no_default


def test_unwrap_exports_with_es_module():
    """A namespace with __es_module returns its default export, or itself."""
    ns = SimpleNamespace(default="esm-component", __es_module=True)
    assert unwrap_exports(ns) == "esm-component"

    ns_no_default = SimpleNamespace(__es_module=True)
    assert unwrap_exports(ns_no_default) is ns_no_default


def test_unwrap_exports_es_module_with_default():
    """__es_module + default returns the default."""
    ns = SimpleNamespace(default="my-component", __es_module=True)
    assert unwrap_exports(ns) == "my-component"


def test_unwrap_exports_nested_default():
    """Nested __es_module: inner namespace's default is returned."""
    inner = SimpleNamespace(default="inner-component", __es_module=True)
    ns = SimpleNamespace(default=inner, __es_module=True)
    assert unwrap_exports(ns) == "inner-component"


def test_unwrap_exports_falsy_default_kept():
    """A default of 0 or False is kept (null-only ?? rule)."""
    assert unwrap_exports(SimpleNamespace(default=0)) == 0
    assert unwrap_exports(SimpleNamespace(default=False)) is False


def test_loader_unwrap_exports_alias():
    """LoaderService.unwrap_exports is the same single implementation."""
    assert LoaderService.unwrap_exports(None) is None
    assert LoaderService.unwrap_exports("component") == "component"


# --- resolve_component_target / builtins (moved from loader) ---


def test_resolve_component_target_builtin_aliases():
    from anima.hmr import HmrService
    from anima.logger import LoggerService
    from anima.pkg import PkgService
    from anima.timer import TimerService
    from anima.watcher import WatcherService

    assert resolve_component_target("group") is Group
    assert resolve_component_target("logger") is LoggerService
    assert resolve_component_target("timer") is TimerService
    assert resolve_component_target("watcher") is WatcherService
    assert resolve_component_target("hmr") is HmrService
    assert resolve_component_target("pkg") is PkgService


def test_builtin_aliases_map():
    assert BUILTIN_ALIASES["group"] == "anima.tree:Group"
    assert BUILTIN_ALIASES["logger"] == "anima.logger:LoggerService"


# --- ensure_id / resolve ---


@pytest.mark.asyncio
async def test_ensure_id_random_hex_and_stable(mod: Any) -> None:
    ctx = Context()
    loader = LoaderService(ctx)
    first = loader.ensure_id({})
    second = loader.ensure_id({})
    assert re.fullmatch(r"[0-9a-f]{8}", first)
    assert re.fullmatch(r"[0-9a-f]{8}", second)
    assert first != second
    # A pre-existing id is left untouched, even on collision.
    await loader.create(_row(id="fixed"))
    assert loader.ensure_id({"id": "fixed"}) == "fixed"


@pytest.mark.asyncio
async def test_resolve_errors_verbatim(mod: Any) -> None:
    ctx = Context()
    loader = LoaderService(ctx)
    await loader.create(_row(id="a"))
    with pytest.raises(ValueError, match="cannot resolve entry nope"):
        loader.resolve("nope")
    with pytest.raises(ValueError, match="entry a is not a group"):
        loader.resolve_group("a")


@pytest.mark.asyncio
async def test_resolve_nested_subtree_ids(
    mod: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Colon ids cross subtrees; group children resolve by short id."""
    ctx = Context()
    loader = LoaderService(ctx)
    subtrees: dict[str, Any] = {}

    def owner_comp(c: Context, config: Any = None) -> None:
        subtrees["a"] = MemoryTree(c, loader=loader)

    def mid_comp(c: Context, config: Any = None) -> None:
        subtrees["b"] = MemoryTree(c, loader=loader)

    _install_module(
        monkeypatch,
        "nested_mod",
        owner=owner_comp,
        mid=mid_comp,
        leaf=_recorder([], "leaf"),
    )
    await loader.create({"id": "a", "name": "nested_mod:owner"})
    await ctx.flush()
    await loader.await_all()
    sub_a = subtrees["a"]
    assert isinstance(sub_a, MemoryTree)
    await sub_a.create({"id": "b", "name": "nested_mod:mid"})
    await loader.await_all()
    sub_b = subtrees["b"]
    await sub_b.create({"id": "c", "name": "nested_mod:leaf"})
    await loader.await_all()
    deep = loader.resolve("a:b:c")
    assert deep.id == "a:b:c"
    assert deep.options["name"] == "nested_mod:leaf"
    assert deep.fiber is not None and deep.fiber.state == FiberState.ACTIVE
    with pytest.raises(ValueError, match="cannot resolve entry a:b:nope"):
        loader.resolve("a:b:nope")
    with pytest.raises(ValueError, match="cannot resolve entry a:nope:c"):
        loader.resolve("a:nope:c")


# --- A1: scripted sequence ---


@pytest.mark.asyncio
async def test_a1_scripted_sequence(mod: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = Context()
    loader = LoaderService(ctx)
    events: list[tuple[Any, Any, bool]] = []
    ctx.on(
        "loader/partial-dispose",
        lambda entry, legacy, active: events.append((entry, legacy, active)),
    )
    writes = {"count": 0}
    monkeypatch.setattr(
        loader, "write", lambda: writes.__setitem__("count", writes["count"] + 1)
    )

    g1_id = await loader.create(
        {"id": "g1", "name": "group", "group": True, "config": []}
    )
    g2_id = await loader.create(
        {"id": "g2", "name": "group", "group": True, "config": []}
    )
    assert (g1_id, g2_id) == ("g1", "g2")
    await loader.await_all()

    a_id = await loader.create(_row(id="a"), parent=None, position=0)
    await loader.create(_row(id="b"), parent=None, position=1)
    c_id = await loader.create(_row(id="c"), parent=None, position=1)
    assert [r["id"] for r in loader.root.data if r["id"] in {"a", "b", "c"}] == [
        "a",
        "c",
        "b",
    ]
    assert loader.root.data[0]["id"] == "a"
    d_id = await loader.create(_row(id="d"), parent="g1", position=math.inf)
    g1 = loader.resolve("g1")
    assert g1.subgroup is not None
    assert [r["id"] for r in g1.subgroup.data] == ["d"]
    await loader.await_all()

    # Move C into g1 at position 0, disabling it; same Entry and fiber objects.
    entry_c = loader.resolve(c_id)
    fiber_c = entry_c.fiber
    await loader.update(c_id, {"disabled": True}, parent="g1", position=0)
    assert loader.resolve(c_id) is entry_c
    assert entry_c.fiber is fiber_c
    assert entry_c.parent is g1.subgroup
    assert [r["id"] for r in g1.subgroup.data] == ["c", "d"]
    assert [r["id"] for r in loader.root.data] == ["a", "b", "g1", "g2"]

    # Resolve checks.
    assert loader.resolve("g1") is g1
    with pytest.raises(ValueError, match="entry a is not a group"):
        loader.resolve_group("a")

    # Remove D: disposed after settle, gone from store and data, event observed.
    entry_d = loader.resolve(d_id)
    writes["count"] = 0
    loader.remove(d_id)
    assert d_id not in loader.store
    assert all(r.get("id") != "d" for r in g1.subgroup.data)
    assert writes["count"] == 1
    # The dispose is scheduled on the parent group's ctx.
    await entry_d.parent.ctx.flush()
    assert entry_d.fiber is not None
    assert entry_d.fiber.state == FiberState.DISPOSED
    removals = [e for e in events if e[0] is entry_d and e[2] is False]
    assert len(removals) == 1
    assert removals[0][1] is entry_d.options

    # Null deletes keys.
    await loader.update(a_id, {"config": {"x": 1}, "disabled": None})
    entry_a = loader.resolve(a_id)
    assert entry_a.options["config"] == {"x": 1}
    assert "disabled" not in entry_a.options
    await loader.update(a_id, {"config": None})
    assert "config" not in entry_a.options


# --- A3: group reorder ---


@pytest.mark.asyncio
async def test_a3_group_reorder_retains_fibers(mod: Any) -> None:
    ctx = Context()
    loader = LoaderService(ctx)
    loader.load(
        [
            {
                "id": "g",
                "name": "group",
                "group": True,
                "config": [_row(id="a"), _row(id="b")],
            }
        ]
    )
    await ctx.flush()
    group = loader.resolve("g")
    assert group.subgroup is not None
    rows = list(group.subgroup.data)
    fiber_a = loader.resolve("a").fiber
    fiber_b = loader.resolve("b").fiber
    assert fiber_a is not None and fiber_b is not None
    await group.subgroup.update([rows[1], rows[0]])
    await loader.await_all()
    assert [r["id"] for r in group.subgroup.data] == ["b", "a"]
    assert loader.resolve("a").fiber is fiber_a
    assert loader.resolve("b").fiber is fiber_b


@pytest.mark.asyncio
async def test_same_group_move_reorders(mod: Any) -> None:
    ctx = Context()
    loader = LoaderService(ctx)
    await loader.create(_row(id="a"))
    await loader.create(_row(id="b"))
    await loader.create(_row(id="c"))
    await loader.await_all()
    await loader.update("a", {}, parent=None, position=2)
    assert [r["id"] for r in loader.root.data] == ["b", "c", "a"]


# --- A4: anonymous-entry stable identity ---


@pytest.mark.asyncio
async def test_a4_anonymous_entry_stable_identity(mod: Any) -> None:
    ctx = Context()
    loader = LoaderService(ctx)
    loader.load([_row(), _row()])
    await ctx.flush()
    rows = list(loader.root.data)
    assert len(rows) == 2
    assert all(re.fullmatch(r"[0-9a-f]{8}", r["id"]) for r in rows)
    first_id, second_id = rows[0]["id"], rows[1]["id"]
    fiber_first = loader.resolve(first_id).fiber
    fiber_second = loader.resolve(second_id).fiber
    third = _row()
    await loader.root.update([rows[0], rows[1], third])
    await loader.await_all()
    assert loader.resolve(first_id).fiber is fiber_first
    assert loader.resolve(second_id).fiber is fiber_second
    assert re.fullmatch(r"[0-9a-f]{8}", third["id"])
    await loader.root.update([rows[1], third])
    await loader.await_all()
    assert first_id not in loader.store
    assert loader.resolve(second_id).fiber is fiber_second


# --- A6: positions ---


@pytest.mark.asyncio
async def test_a6_positions(mod: Any) -> None:
    ctx = Context()
    loader = LoaderService(ctx)
    await loader.create(_row(id="a"), position=0)
    await loader.create(_row(id="b"), position=math.inf)
    await loader.create(_row(id="c"), position=1)
    assert [r["id"] for r in loader.root.data] == ["a", "c", "b"]
    # Beyond the end clamps to the end.
    await loader.create(_row(id="d"), position=99)
    assert [r["id"] for r in loader.root.data] == ["a", "c", "b", "d"]
    # -1 inserts before the last row (splice parity).
    await loader.create(_row(id="e"), position=-1)
    assert [r["id"] for r in loader.root.data] == ["a", "c", "b", "e", "d"]
    # None appends.
    await loader.create(_row(id="f"), position=None)
    assert [r["id"] for r in loader.root.data] == ["a", "c", "b", "e", "d", "f"]


# --- Entry.update semantics ---


@pytest.mark.asyncio
async def test_options_keys_sorted(mod: Any) -> None:
    ctx = Context()
    loader = LoaderService(ctx)
    await loader.create(
        {
            "zebra": 1,
            "config": {"x": 1},
            "name": "tree_test_mod:comp",
            "id": "s",
            "apple": 2,
        }
    )
    entry = loader.resolve("s")
    assert list(entry.options.keys()) == ["id", "name", "apple", "zebra", "config"]


@pytest.mark.asyncio
async def test_update_noop_without_force_emits_nothing(mod: Any) -> None:
    ctx = Context()
    loader = LoaderService(ctx)
    events: list[Any] = []
    ctx.on("loader/partial-dispose", lambda *args: events.append(args))
    await loader.create(_row(id="a", config={"x": 1}))
    await loader.await_all()
    entry = loader.resolve("a")
    fiber = entry.fiber
    await entry.update({"config": {"x": 1}})
    assert events == []
    assert entry.fiber is fiber
    await entry.update({"config": {"x": 2}})
    assert len(events) == 1
    assert events[0][0] is entry
    assert events[0][1] == {"id": "a", "name": "tree_test_mod:comp", "config": {"x": 1}}
    assert events[0][2] is True


@pytest.mark.asyncio
async def test_update_disabled_short_circuit(mod: Any) -> None:
    ctx = Context()
    loader = LoaderService(ctx)
    events: list[Any] = []
    ctx.on("loader/partial-dispose", lambda *args: events.append(args))
    await loader.create(_row(id="a"))
    await loader.await_all()
    entry = loader.resolve("a")
    await entry.update({"disabled": True})
    assert events == []
    await entry.ctx.flush()
    assert entry.fiber is not None
    assert entry.fiber.state == FiberState.DISPOSED


def test_entry_evaluate_attested_patterns(monkeypatch: pytest.MonkeyPatch) -> None:
    from anima.jsexpr import JsExprError

    ctx = Context()
    loader = LoaderService(ctx)
    fresh = Entry(loader)
    monkeypatch.setenv("ANIMA_TREE_TEST", "on")
    assert fresh.evaluate("process.env.ANIMA_TREE_TEST") == "on"
    assert fresh.evaluate("process.env.ANIMA_TREE_MISSING") is None
    assert fresh.evaluate("process.platform === 'win32'") == (sys.platform == "win32")
    assert fresh.evaluate("1 === 1") is True
    assert fresh.evaluate("ctx.get('answer')") is None
    with pytest.raises(JsExprError, match="unsupported call"):
        fresh.evaluate("homePath('sessions')")


@pytest.mark.asyncio
async def test_disabled_js_expr(mod: Any) -> None:
    ctx = Context()
    loader = LoaderService(ctx)
    ctx.provide("flag", True)
    loader.load(
        [
            _row(
                id="a",
                disabled={"__jsExpr": "ctx.get('flag')"},
            ),
            _row(
                id="b",
                disabled={"__jsExpr": "!ctx.get('flag')"},
            ),
        ]
    )
    await ctx.flush()
    entry_a = loader.resolve("a")
    entry_b = loader.resolve("b")
    assert entry_a.disabled is True
    assert entry_a.fiber is None
    assert entry_b.disabled is False
    assert entry_b.fiber is not None
    assert entry_b.fiber.state == FiberState.ACTIVE


@pytest.mark.asyncio
async def test_disabled_js_expr_inherits_through_group(mod: Any) -> None:
    ctx = Context()
    loader = LoaderService(ctx)
    ctx.provide("flag", True)
    loader.load(
        [
            {
                "id": "g",
                "name": "group",
                "group": True,
                "disabled": {"__jsExpr": "ctx.get('flag')"},
                "config": [_row(id="kid")],
            }
        ]
    )
    await ctx.flush()
    group_entry = loader.resolve("g")
    kid = loader.resolve("kid")
    # A group entry is always enabled itself; its descendants inherit.
    assert group_entry.disabled is False
    assert kid.disabled is True
    assert kid.fiber is None
    # Flipping the flag remounts the descendant.
    ctx.provide("flag", False)
    await loader.update("g", {"disabled": {"__jsExpr": "ctx.get('flag')"}})
    await loader.await_all()
    assert kid.disabled is False
    assert kid.fiber is not None
    assert kid.fiber.state == FiberState.ACTIVE


@pytest.mark.asyncio
async def test_disabled_js_expr_out_of_scope_fails_loud(
    mod: Any, caplog: pytest.LogCaptureFixture
) -> None:
    from anima.jsexpr import JsExprError

    ctx = Context()
    loader = LoaderService(ctx)
    loader.load([_row(id="a", disabled={"__jsExpr": "homePath('sessions')"})])
    await ctx.flush()
    entry = loader.resolve("a")
    # The entry never mounts, and the failure is logged, not swallowed.
    assert entry.fiber is None
    assert "Failed to create entry" in caplog.text
    with pytest.raises(JsExprError, match="unsupported call"):
        _ = entry.disabled


@pytest.mark.asyncio
async def test_refresh_retries_failed_import(
    mod: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = Context()
    loader = LoaderService(ctx)
    await loader.create({"id": "a", "name": "missing_mod_xyz:comp"})
    await loader.await_all()
    entry = loader.resolve("a")
    assert entry.fiber is None
    # Disabled entries do not refresh.
    await entry.update({"disabled": True})
    await entry.refresh()
    assert entry.fiber is None
    await entry.update({"disabled": False})
    assert entry.fiber is None  # still no module
    _install_module(monkeypatch, "missing_mod_xyz", comp=_recorder([], "comp"))
    await entry.refresh()
    await loader.await_all()
    assert entry.fiber is not None
    assert entry.fiber.state == FiberState.ACTIVE


@pytest.mark.asyncio
async def test_refresh_noop_when_fiber_present(mod: Any) -> None:
    ctx = Context()
    loader = LoaderService(ctx)
    await loader.create(_row(id="a"))
    await loader.await_all()
    entry = loader.resolve("a")
    fiber = entry.fiber
    await entry.refresh()
    assert entry.fiber is fiber


# --- entries() / get_tasks() / await_all() ---


@pytest.mark.asyncio
async def test_entries_recurses_subtrees(
    mod: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = Context()
    loader = LoaderService(ctx)
    subtrees: dict[str, Any] = {}

    def owner_comp(c: Context, config: Any = None) -> None:
        subtrees["a"] = MemoryTree(c, loader=loader)

    _install_module(
        monkeypatch, "nested_mod2", owner=owner_comp, leaf=_recorder([], "leaf")
    )
    await loader.create({"id": "a", "name": "nested_mod2:owner"})
    await loader.create(_row(id="top"))
    await ctx.flush()
    await loader.await_all()
    await subtrees["a"].create({"id": "deep", "name": "nested_mod2:leaf"})
    await loader.await_all()
    assert [e.id for e in loader.entries()] == ["a", "a:deep", "top"]


@pytest.mark.asyncio
async def test_a11_get_tasks_and_await_all(
    mod: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = Context()
    loader = LoaderService(ctx)
    started = asyncio.Event()
    release = asyncio.Event()

    def slow_comp(c: Context, config: Any = None) -> Any:
        async def _run() -> None:
            started.set()
            await release.wait()

        return _run()

    _install_module(monkeypatch, "slow_mod", slow=slow_comp)
    create_task = asyncio.get_running_loop().create_task(
        loader.create({"id": "slow", "name": "slow_mod:slow"})
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
    # A second await_all with nothing pending returns immediately.
    await loader.await_all()
    assert loader.resolve("slow").fiber is not None
    assert loader.resolve("slow").fiber.state == FiberState.ACTIVE


# --- A12: stop retains data ---


@pytest.mark.asyncio
async def test_a12_group_stop_retains_data(mod: Any) -> None:
    ctx = Context()
    loader = LoaderService(ctx)
    loader.load(
        [
            {
                "id": "g",
                "name": "group",
                "group": True,
                "config": [_row(id="a"), _row(id="b")],
            }
        ]
    )
    await ctx.flush()
    group_entry = loader.resolve("g")
    assert group_entry.subgroup is not None
    rows = group_entry.subgroup.data
    fiber_a = loader.resolve("a").fiber
    assert fiber_a is not None
    group_entry.subgroup.stop()
    await group_entry.ctx.flush()
    assert "a" not in loader.store
    assert "b" not in loader.store
    assert group_entry.subgroup.data is rows
    assert [r["id"] for r in rows] == ["a", "b"]
    await group_entry.subgroup.update(rows)
    await loader.await_all()
    assert loader.resolve("a").fiber is not None
    assert loader.resolve("a").fiber.state == FiberState.ACTIVE
    assert loader.resolve("a").fiber is not fiber_a


# --- A13: error isolation + listener scoping ---


@pytest.mark.asyncio
async def test_a13_per_entry_error_isolation(
    mod: Any, caplog: pytest.LogCaptureFixture
) -> None:
    ctx = Context()
    loader = LoaderService(ctx)
    loader.load(
        [
            {
                "id": "g",
                "name": "group",
                "group": True,
                "config": [
                    _row(id="good"),
                    {"id": "bad", "name": "tree_test_mod:not_a_component"},
                    _row(id="also_good"),
                ],
            }
        ]
    )
    await ctx.flush()
    assert loader.resolve("good").fiber is not None
    assert loader.resolve("good").fiber.state == FiberState.ACTIVE
    assert loader.resolve("also_good").fiber is not None
    assert loader.resolve("also_good").fiber.state == FiberState.ACTIVE
    assert loader.resolve("bad").fiber is None
    assert "Failed to create entry bad" in caplog.text


@pytest.mark.asyncio
async def test_a13_group_update_scoping(mod: Any) -> None:
    """Updating group A must not reconcile group B (config-identity check)."""
    ctx = Context()
    loader = LoaderService(ctx)
    loader.load(
        [
            {
                "id": "ga",
                "name": "group",
                "group": True,
                "config": [_row(id="a1")],
            },
            {
                "id": "gb",
                "name": "group",
                "group": True,
                "config": [_row(id="b1")],
            },
        ]
    )
    await ctx.flush()
    fiber_b1 = loader.resolve("b1").fiber
    assert fiber_b1 is not None
    # Replace ga's child list with a new row; gb must be untouched.
    await loader.update("ga", {"config": [_row(id="a2")]})
    await loader.await_all()
    assert "a1" not in loader.store
    assert loader.resolve("a2").fiber is not None
    assert loader.resolve("a2").fiber.state == FiberState.ACTIVE
    assert loader.resolve("b1").fiber is fiber_b1


# --- A14: disabled inheritance ---


@pytest.mark.asyncio
async def test_a14_disabled_inheritance(mod: Any) -> None:
    ctx = Context()
    loader = LoaderService(ctx)
    loader.load(
        [
            {
                "id": "g",
                "name": "group",
                "group": True,
                "config": [_row(id="kid")],
            }
        ]
    )
    await ctx.flush()
    group_entry = loader.resolve("g")
    kid = loader.resolve("kid")
    assert group_entry.disabled is False
    assert kid.disabled is False
    assert kid.fiber is not None and kid.fiber.state == FiberState.ACTIVE
    await loader.disable("g")
    assert group_entry.disabled is False
    assert kid.disabled is True
    assert kid.fiber is not None
    assert kid.fiber.state == FiberState.DISPOSED
    # A disabled child does not remount on update.
    old_fiber = kid.fiber
    await loader.root.update(list(loader.root.data))
    await loader.await_all()
    assert kid.fiber is old_fiber
    assert kid.fiber.state == FiberState.DISPOSED
    # Re-enable: descendants mount again with fresh fibers.
    await loader.enable("g")
    assert kid.disabled is False
    assert kid.fiber is not None
    assert kid.fiber is not old_fiber
    assert kid.fiber.state == FiberState.ACTIVE


# --- Group construction guards / status / marker ---


def test_group_outside_entry_raises() -> None:
    ctx = Context()
    with pytest.raises(RuntimeError, match="mounted outside an entry"):
        Group(ctx, [])


@pytest.mark.asyncio
async def test_group_status(mod: Any) -> None:
    ctx = Context()
    loader = LoaderService(ctx)
    loader.load(
        [
            {
                "id": "g",
                "name": "group",
                "group": True,
                "config": [_row(id="a"), _row(id="b", disabled=True)],
            }
        ]
    )
    await ctx.flush()
    group_entry = loader.resolve("g")
    assert group_entry.fiber is not None
    group = group_entry.fiber._instance
    assert isinstance(group, Group)
    status = group.status()
    assert status["name"] == "g"
    assert status["entries"]["a"]["disabled"] is False
    assert status["entries"]["a"]["state"] == FiberState.ACTIVE.value
    assert status["entries"]["b"]["disabled"] is True
    assert status["entries"]["b"]["state"] == FiberState.DISPOSED.value
    assert EntryGroup.is_tree_carrier(Group) is True
    assert EntryGroup.is_tree_carrier(object()) is False


@pytest.mark.asyncio
async def test_entry_group_create_without_loader_raises() -> None:
    ctx = Context()
    tree = MemoryTree(ctx)
    with pytest.raises(RuntimeError, match="no loader"):
        await tree.create(_row(id="x"))


def test_tree_write_abstract() -> None:
    ctx = Context()
    with pytest.raises(TypeError):
        EntryTree(ctx)  # type: ignore[abstract]


# --- Coverage: context properties, id collision, abstract write ---


def test_tree_and_entry_context_properties() -> None:
    ctx = Context()
    tree = MemoryTree(ctx)
    assert tree.context is tree.ctx
    loader = LoaderService(ctx)
    assert loader.context is loader.ctx
    entry = Entry(loader)
    assert entry.context is entry.ctx


def test_ensure_id_collision_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = Context()
    tree = MemoryTree(ctx)
    tree.store["deadbeef"] = None  # type: ignore[assignment]
    candidates = iter(["deadbeef", "cafef00d"])
    monkeypatch.setattr(secrets, "token_hex", lambda n: next(candidates))
    assert tree.ensure_id({}) == "cafef00d"


def test_entry_tree_write_base_raises() -> None:
    ctx = Context()
    tree = MemoryTree(ctx)
    with pytest.raises(NotImplementedError):
        EntryTree.write(tree)


@pytest.mark.asyncio
async def test_js_expr_disabled_evaluates(mod: Any) -> None:
    """A JsExpr disabled flag evaluates: "true" disables the entry."""
    ctx = Context()
    loader = LoaderService(ctx)
    loader.load(
        [
            {
                "id": "jx",
                "name": "tree_test_mod:comp",
                "disabled": {"__jsExpr": "true"},
            }
        ]
    )
    await ctx.flush()
    assert loader.resolve("jx").disabled is True
    assert loader.resolve("jx").fiber is None


@pytest.mark.asyncio
async def test_concurrent_refresh_shares_init_task(mod: Any) -> None:
    """Two concurrent refreshes of an unmounted entry share one init task."""
    ctx = Context()
    loader = LoaderService(ctx)
    loader.load([{"id": "bad", "name": "tree_test_mod:not_a_component"}])
    await ctx.flush()
    entry = loader.resolve("bad")
    assert entry.fiber is None
    results = await asyncio.gather(
        entry.refresh(), entry.refresh(), return_exceptions=True
    )
    assert all(isinstance(res, ValueError) for res in results)


@pytest.mark.asyncio
async def test_notify_runs_on_failed_fiber(mod: Any) -> None:
    """A component that fails during activation still notifies without raising."""
    ctx = Context()
    loader = LoaderService(ctx)
    loader.load([{"id": "f", "name": "tree_test_mod:failing"}])
    await ctx.flush()
    entry = loader.resolve("f")
    assert entry.fiber is not None
    assert entry.fiber.state == FiberState.FAILED
    assert entry.fiber.error is not None


@pytest.mark.asyncio
async def test_remove_unknown_entry_is_noop(mod: Any) -> None:
    ctx = Context()
    loader = LoaderService(ctx)
    loader.load([_row(id="a")])
    await ctx.flush()
    loader.root.remove("nonexistent")
    assert "a" in loader.store


@pytest.mark.asyncio
async def test_remove_entry_without_fiber(mod: Any) -> None:
    """Removing a never-mounted (disabled) entry skips the dispose scheduling."""
    ctx = Context()
    loader = LoaderService(ctx)
    loader.load([_row(id="d", disabled=True)])
    await ctx.flush()
    assert loader.resolve("d").fiber is None
    loader.remove("d")
    assert "d" not in loader.store


@pytest.mark.asyncio
async def test_stop_and_status_skip_id_less_rows(mod: Any) -> None:
    """Rows without an id are no-ops in stop() and invisible to status()."""
    ctx = Context()
    loader = LoaderService(ctx)
    loader.load([{"id": "g", "name": "group", "group": True, "config": [_row(id="a")]}])
    await ctx.flush()
    group = loader.resolve("g").fiber._instance
    group.data.append({"name": "no_id"})
    assert set(group.status()["entries"]) == {"a"}
    group.stop()
    assert "a" not in loader.store
    assert {"name": "no_id"} in group.data


@pytest.mark.asyncio
async def test_case5_owner_fiber_disposed_returns(mod: Any) -> None:
    """A subtree entry's self-dispose is ignored once its tree-owner fiber is gone."""
    ctx = Context()
    loader = LoaderService(ctx)
    release = asyncio.Event()
    trees: dict[str, Any] = {}

    def self_disposer(c: Context, config: Any = None) -> None:
        async def _later() -> None:
            await release.wait()
            await c.fiber.dispose()

        c._schedule_task(_later())

    def owner_comp(c: Context, config: Any = None) -> None:
        trees["tree"] = MemoryTree(c, loader=loader)

    mod.self_disposer = self_disposer
    mod.owner_comp = owner_comp
    loader.load([{"id": "owner", "name": "tree_test_mod:owner_comp"}])
    await ctx.flush()
    tree = trees["tree"]
    # Remove the owner through the loader: its dispose is loader-driven,
    # so the owner entry is not marked disabled and the subtree stays usable.
    owner_entry = loader.resolve("owner")
    owner_fiber = owner_entry.fiber
    loader.remove("owner")
    await loader.ctx.flush()
    assert owner_fiber.state == FiberState.DISPOSED
    assert "disabled" not in owner_entry.options
    await tree.create({"id": "sub", "name": "tree_test_mod:self_disposer"})
    sub = tree.store["sub"]
    assert sub.fiber is not None
    # Settle the fiber itself: await_all would also wait on the gated
    # self-dispose task, which only completes after release is set.
    await sub.fiber.await_settle()
    assert sub.fiber.state == FiberState.ACTIVE
    release.set()
    await sub.ctx.flush()
    assert sub.fiber.state == FiberState.DISPOSED
    assert "disabled" not in sub.options


@pytest.mark.asyncio
async def test_case5_owner_fiber_alive_marks_disabled(mod: Any) -> None:
    """A subtree entry's self-dispose marks it disabled while its owner lives."""
    ctx = Context()
    loader = LoaderService(ctx)
    release = asyncio.Event()
    trees: dict[str, Any] = {}

    def self_disposer(c: Context, config: Any = None) -> None:
        async def _later() -> None:
            await release.wait()
            await c.fiber.dispose()

        c._schedule_task(_later())

    def owner_comp(c: Context, config: Any = None) -> None:
        trees["tree"] = MemoryTree(c, loader=loader)

    mod.self_disposer = self_disposer
    mod.owner_comp = owner_comp
    loader.load([{"id": "owner", "name": "tree_test_mod:owner_comp"}])
    await ctx.flush()
    tree = trees["tree"]
    await tree.create({"id": "sub", "name": "tree_test_mod:self_disposer"})
    sub = tree.store["sub"]
    assert sub.fiber is not None
    await sub.fiber.await_settle()
    assert sub.fiber.state == FiberState.ACTIVE
    release.set()
    await sub.ctx.flush()
    assert sub.fiber.state == FiberState.DISPOSED
    assert sub.options.get("disabled") is True


# ---------------------------------------------------------------------------
# P1-6 — Volatile live-update commit path
# ---------------------------------------------------------------------------


@pytest.fixture
def volatile_mod(monkeypatch: pytest.MonkeyPatch) -> Any:
    """A dummy module with components whose Config carries volatile fields."""
    calls: list[Any] = []

    def comp(ctx: Context, config: Any = None) -> None:
        calls.append(config)

    comp.Config = Schema.object(  # type: ignore[attr-defined]
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

    def root_comp(ctx: Context, config: Any = None) -> None:
        calls.append(("root", config))

    root_comp.Config = Schema.object({"level": Schema.number()}).volatile()  # type: ignore[attr-defined]

    def strict_comp(ctx: Context, config: Any = None) -> None:
        calls.append(("strict", config))

    strict_comp.Config = Schema.object(  # type: ignore[attr-defined]
        {
            "score": Schema.number().volatile().min(0).max(10),
            "level": Schema.number(),
        }
    )

    module = _install_module(
        monkeypatch,
        "volatile_test_mod",
        comp=comp,
        root_comp=root_comp,
        strict_comp=strict_comp,
    )
    module.calls = calls  # type: ignore[attr-defined]
    return module


@pytest.mark.asyncio
async def test_volatile_only_update_no_reload(volatile_mod: Any) -> None:
    ctx = Context()
    loader = LoaderService(ctx)
    await loader.create(
        {
            "id": "a",
            "name": "volatile_test_mod:comp",
            "config": {"theme": "dark", "level": 3},
        }
    )
    await loader.await_all()
    entry = loader.resolve("a")
    fiber = entry.fiber
    assert fiber is not None and fiber.state == FiberState.ACTIVE
    ref = fiber.config["theme"]
    assert is_volatile(ref)
    assert ref.get() == "dark"
    volatile_updates: list[list[list[str]]] = []
    partials: list[Any] = []
    entry.ctx.on("loader/volatile-update", lambda paths: volatile_updates.append(paths))
    ctx.on("loader/partial-dispose", lambda *args: partials.append(args))
    children_before = len(entry.ctx._children)
    inits_before = len(volatile_mod.calls)

    await entry.update({"config": {"theme": "light", "level": 3}})

    assert len(volatile_mod.calls) == inits_before  # no reload
    assert entry.fiber is fiber
    assert fiber.state == FiberState.ACTIVE
    assert partials == []  # the volatile-only path never emits partial-dispose
    assert volatile_updates == [[["theme"]]]
    assert fiber.config["theme"] is ref  # same stable reference
    assert ref.get() == "light"  # live value updated in place
    assert fiber.raw_config == {"theme": "light", "level": 3}
    assert entry.options["config"] == {"theme": "light", "level": 3}
    assert len(entry.ctx._children) == children_before  # no leaked filter ctx


@pytest.mark.asyncio
async def test_volatile_nested_only_update_no_reload(volatile_mod: Any) -> None:
    ctx = Context()
    loader = LoaderService(ctx)
    await loader.create(
        {
            "id": "a",
            "name": "volatile_test_mod:comp",
            "config": {
                "theme": "dark",
                "level": 3,
                "nested": {"label": "a", "count": 1},
            },
        }
    )
    await loader.await_all()
    entry = loader.resolve("a")
    fiber = entry.fiber
    assert fiber is not None
    label_ref = fiber.config["nested"]["label"]
    theme_ref = fiber.config["theme"]
    volatile_updates: list[list[list[str]]] = []
    entry.ctx.on("loader/volatile-update", lambda paths: volatile_updates.append(paths))
    inits_before = len(volatile_mod.calls)

    await entry.update(
        {
            "config": {
                "theme": "dark",
                "level": 3,
                "nested": {"label": "b", "count": 1},
            }
        }
    )

    assert len(volatile_mod.calls) == inits_before
    assert volatile_updates == [[["nested", "label"]]]
    assert fiber.config["nested"]["label"] is label_ref
    assert label_ref.get() == "b"
    assert theme_ref.get() == "dark"  # unchanged snapshot is not recommitted


@pytest.mark.asyncio
async def test_volatile_root_config_update_no_reload(volatile_mod: Any) -> None:
    ctx = Context()
    loader = LoaderService(ctx)
    await loader.create(
        {"id": "a", "name": "volatile_test_mod:root_comp", "config": {"level": 3}}
    )
    await loader.await_all()
    entry = loader.resolve("a")
    fiber = entry.fiber
    assert fiber is not None and fiber.state == FiberState.ACTIVE
    ref = fiber.config
    assert is_volatile(ref)
    assert ref.get() == {"level": 3}
    volatile_updates: list[list[list[str]]] = []
    entry.ctx.on("loader/volatile-update", lambda paths: volatile_updates.append(paths))
    inits_before = len(volatile_mod.calls)

    await entry.update({"config": {"level": 4}})

    assert len(volatile_mod.calls) == inits_before
    assert volatile_updates == [[[]]]  # the root reference has an empty path
    assert fiber.config is ref
    assert ref.get() == {"level": 4}


@pytest.mark.asyncio
async def test_volatile_and_ordinary_change_restarts(volatile_mod: Any) -> None:
    ctx = Context()
    loader = LoaderService(ctx)
    await loader.create(
        {
            "id": "a",
            "name": "volatile_test_mod:comp",
            "config": {"theme": "dark", "level": 3},
        }
    )
    await loader.await_all()
    entry = loader.resolve("a")
    volatile_updates: list[list[list[str]]] = []
    partials: list[Any] = []
    entry.ctx.on("loader/volatile-update", lambda paths: volatile_updates.append(paths))
    ctx.on("loader/partial-dispose", lambda *args: partials.append(args))

    await entry.update({"config": {"theme": "light", "level": 4}})
    await loader.await_all()

    assert len(volatile_mod.calls) == 2  # full restart: the component ran again
    assert len(partials) == 1
    assert volatile_updates == []  # the fast path was not taken
    assert entry.fiber is not None and entry.fiber.state == FiberState.ACTIVE
    assert entry.fiber.config["theme"].get() == "light"
    assert entry.fiber.config["level"] == 4


@pytest.mark.asyncio
async def test_volatile_update_failing_validation_is_refused(
    volatile_mod: Any, caplog: pytest.LogCaptureFixture
) -> None:
    ctx = Context()
    loader = LoaderService(ctx)
    await loader.create(
        {
            "id": "a",
            "name": "volatile_test_mod:strict_comp",
            "config": {"score": 5, "level": 1},
        }
    )
    await loader.await_all()
    entry = loader.resolve("a")
    fiber = entry.fiber
    assert fiber is not None and fiber.state == FiberState.ACTIVE
    ref = fiber.config["score"]
    assert ref.get() == 5
    volatile_updates: list[list[list[str]]] = []
    partials: list[Any] = []
    entry.ctx.on("loader/volatile-update", lambda paths: volatile_updates.append(paths))
    ctx.on("loader/partial-dispose", lambda *args: partials.append(args))

    with caplog.at_level(logging.WARNING, logger="anima.loader"):
        await entry.update({"config": {"score": 99, "level": 1}})

    assert len(volatile_mod.calls) == 1  # no restart
    assert partials == []
    assert volatile_updates == []
    assert ref.get() == 5  # live value untouched, not half-applied
    assert fiber.state == FiberState.ACTIVE
    # The raw config stays retained for the next activation.
    assert fiber.raw_config == {"score": 99, "level": 1}
    assert "volatile config update failed for a" in caplog.text


@pytest.mark.asyncio
async def test_volatile_js_expr_to_literal_is_no_change(
    volatile_mod: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VOL_THEME", "dark")
    ctx = Context()
    loader = LoaderService(ctx)
    await loader.create(
        {
            "id": "a",
            "name": "volatile_test_mod:comp",
            "config": {"theme": {"__jsExpr": "process.env.VOL_THEME"}, "level": 3},
        }
    )
    await loader.await_all()
    entry = loader.resolve("a")
    fiber = entry.fiber
    assert fiber is not None
    ref = fiber.config["theme"]
    assert ref.get() == "dark"
    volatile_updates: list[list[list[str]]] = []
    partials: list[Any] = []
    entry.ctx.on("loader/volatile-update", lambda paths: volatile_updates.append(paths))
    ctx.on("loader/partial-dispose", lambda *args: partials.append(args))

    # The raw form changed (expression to literal) but the resolved
    # snapshot is identical, so no path changed and no event fires.
    await entry.update({"config": {"theme": "dark", "level": 3}})

    assert len(volatile_mod.calls) == 1
    assert partials == []
    assert volatile_updates == []
    assert ref.get() == "dark"


@pytest.mark.asyncio
async def test_volatile_update_event_scoped_to_fiber(
    mod: Any, volatile_mod: Any
) -> None:
    ctx = Context()
    loader = LoaderService(ctx)
    await loader.create(
        {
            "id": "a",
            "name": "volatile_test_mod:comp",
            "config": {"theme": "dark", "level": 3},
        }
    )
    await loader.create({"id": "b", "name": "tree_test_mod:comp"})
    await loader.await_all()
    entry_a = loader.resolve("a")
    entry_b = loader.resolve("b")
    on_a: list[list[list[str]]] = []
    on_child: list[list[list[str]]] = []
    on_b: list[list[list[str]]] = []
    on_root: list[list[list[str]]] = []
    on_global: list[list[list[str]]] = []
    entry_a.ctx.on("loader/volatile-update", lambda paths: on_a.append(paths))
    child = entry_a.ctx.extend()
    child.on("loader/volatile-update", lambda paths: on_child.append(paths))
    entry_b.ctx.on("loader/volatile-update", lambda paths: on_b.append(paths))
    ctx.on("loader/volatile-update", lambda paths: on_root.append(paths))
    ctx.on(
        "loader/volatile-update",
        lambda paths: on_global.append(paths),
        global_=True,
    )

    await entry_a.update({"config": {"theme": "light", "level": 3}})

    assert on_a == [[["theme"]]]
    assert on_child == [[["theme"]]]  # descendants of the fiber ctx receive it
    assert on_b == []  # another fiber's listeners do not
    assert on_root == []  # ancestors do not
    assert on_global == [[["theme"]]]  # global listeners always receive it


@pytest.mark.asyncio
async def test_volatile_only_update_no_reload_in_group(volatile_mod: Any) -> None:
    # The prototype check is meaningful for group children: a mounted
    # child is parented to its group ctx, not the loader ctx.
    ctx = Context()
    loader = LoaderService(ctx)
    await loader.create({"id": "g1", "name": "group", "group": True, "config": []})
    await loader.await_all()
    await loader.create(
        {
            "id": "a",
            "name": "volatile_test_mod:comp",
            "config": {"theme": "dark", "level": 3},
        },
        parent="g1",
    )
    await loader.await_all()
    entry = loader.resolve("a")
    fiber = entry.fiber
    assert fiber is not None and fiber.state == FiberState.ACTIVE
    assert entry.ctx._parent is entry.parent.ctx
    ref = fiber.config["theme"]
    assert ref.get() == "dark"
    volatile_updates: list[list[list[str]]] = []
    entry.ctx.on("loader/volatile-update", lambda paths: volatile_updates.append(paths))
    inits_before = len(volatile_mod.calls)

    await entry.update({"config": {"theme": "light", "level": 3}})

    assert len(volatile_mod.calls) == inits_before
    assert volatile_updates == [[["theme"]]]
    assert ref.get() == "light"


@pytest.mark.asyncio
async def test_volatile_fast_path_disabled_when_patch_context_short_circuited(
    volatile_mod: Any,
) -> None:
    ctx = Context()
    loader = LoaderService(ctx)
    # Swallow every loader/patch-context waterfall before mounting, so
    # the child entry ctx is never re-parented to its group ctx.
    ctx.on(
        "loader/patch-context",
        lambda entry, next_fn: None,
        mode=EventMode.WATERFALL,
    )
    await loader.create({"id": "g1", "name": "group", "group": True, "config": []})
    await loader.await_all()
    await loader.create(
        {
            "id": "a",
            "name": "volatile_test_mod:comp",
            "config": {"theme": "dark", "level": 3},
        },
        parent="g1",
    )
    await loader.await_all()
    entry = loader.resolve("a")
    fiber = entry.fiber
    assert fiber is not None and fiber.state == FiberState.ACTIVE
    assert entry.ctx._parent is loader.ctx  # still the tree ctx...
    assert entry.ctx._parent is not entry.parent.ctx  # ...not the group ctx
    ref = fiber.config["theme"]
    assert ref.get() == "dark"
    volatile_updates: list[list[list[str]]] = []
    partials: list[Any] = []
    entry.ctx.on("loader/volatile-update", lambda paths: volatile_updates.append(paths))
    ctx.on("loader/partial-dispose", lambda *args: partials.append(args))

    await entry.update({"config": {"theme": "light", "level": 3}})

    # The fast path was not taken: partial-dispose fired, but the
    # short-circuited patch-context never ran fiber.update, so the live
    # value is unchanged while the raw config is retained.
    assert len(partials) == 1
    assert volatile_updates == []
    assert ref.get() == "dark"
    assert entry.options["config"] == {"theme": "light", "level": 3}


@pytest.mark.asyncio
async def test_volatile_fast_path_skipped_when_fiber_not_active(
    volatile_mod: Any,
) -> None:
    ctx = Context()
    loader = LoaderService(ctx)
    await loader.create(
        {
            "id": "a",
            "name": "volatile_test_mod:comp",
            "config": {"theme": "dark", "level": "not-a-number"},
        }
    )
    await loader.await_all()
    entry = loader.resolve("a")
    fiber = entry.fiber
    assert fiber is not None and fiber.state == FiberState.FAILED
    assert len(volatile_mod.calls) == 0  # validation failed before the body ran
    volatile_updates: list[list[list[str]]] = []
    entry.ctx.on("loader/volatile-update", lambda paths: volatile_updates.append(paths))

    await entry.update({"config": {"theme": "dark", "level": 3}})
    await loader.await_all()

    assert volatile_updates == []
    assert len(volatile_mod.calls) == 1  # recovered through the ordinary path
    assert entry.fiber is not None and entry.fiber.state == FiberState.ACTIVE
    assert entry.fiber.config["theme"].get() == "dark"
    assert entry.fiber.config["level"] == 3


@pytest.mark.asyncio
async def test_commit_volatile_no_refs_returns_true(mod: Any) -> None:
    ctx = Context()
    loader = LoaderService(ctx)
    await loader.create(_row(id="a", config={"x": 1}))
    await loader.await_all()
    entry = loader.resolve("a")
    volatile_updates: list[list[list[str]]] = []
    entry.ctx.on("loader/volatile-update", lambda paths: volatile_updates.append(paths))

    assert await entry._commit_volatile() is True
    assert volatile_updates == []


@pytest.mark.asyncio
async def test_volatile_ordinary_value_changed_via_env_restarts(
    volatile_mod: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The raw configs differ only in the volatile field, but the
    # candidate re-resolves an ordinary JsExpr field against changed
    # environment, so the effective configs differ ordinarily: the
    # commit is refused and the ordinary update lifecycle restarts.
    monkeypatch.setenv("VOL_LVL", "3")
    ctx = Context()
    loader = LoaderService(ctx)
    expr = {"__jsExpr": "Number(process.env.VOL_LVL)"}
    await loader.create(
        {
            "id": "a",
            "name": "volatile_test_mod:comp",
            "config": {"theme": "dark", "level": expr},
        }
    )
    await loader.await_all()
    entry = loader.resolve("a")
    fiber = entry.fiber
    assert fiber is not None and fiber.state == FiberState.ACTIVE
    assert fiber.config["level"] == 3
    volatile_updates: list[list[list[str]]] = []
    partials: list[Any] = []
    entry.ctx.on("loader/volatile-update", lambda paths: volatile_updates.append(paths))
    ctx.on("loader/partial-dispose", lambda *args: partials.append(args))

    monkeypatch.setenv("VOL_LVL", "4")
    await entry.update({"config": {"theme": "light", "level": dict(expr)}})
    await loader.await_all()

    assert len(volatile_mod.calls) == 2  # full restart, not a commit
    assert len(partials) == 1
    assert volatile_updates == []
    assert entry.fiber is not None and entry.fiber.state == FiberState.ACTIVE
    assert entry.fiber.config["theme"].get() == "light"
    assert entry.fiber.config["level"] == 4
