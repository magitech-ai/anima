"""Unit tests for the Group component (tree carrier mounted inside an entry)."""

import sys
import types
from typing import Any

import pytest

from anima.core import Context, FiberState
from anima.loader import LoaderService
from anima.tree import Group


def _install_module(monkeypatch: pytest.MonkeyPatch, name: str, **attrs: Any) -> Any:
    mod = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(mod, key, value)
    monkeypatch.setitem(sys.modules, name, mod)
    return mod


@pytest.fixture
def mod(monkeypatch: pytest.MonkeyPatch) -> Any:
    """A dummy module with a recording component."""

    def comp(ctx: Context, config: Any = None) -> None:
        return None

    return _install_module(monkeypatch, "group_test_mod", comp=comp)


def _row(name: str = "group_test_mod:comp", **extra: Any) -> dict[str, Any]:
    row: dict[str, Any] = {"name": name}
    row.update(extra)
    return row


def _load_group(ctx: Context, rows: list[dict[str, Any]]) -> Any:
    loader = LoaderService(ctx)
    loader.load([{"id": "g", "name": "group", "group": True, "config": rows}])
    return loader


def _group_of(loader: Any) -> Group:
    entry = loader.resolve("g")
    assert entry.fiber is not None
    group = entry.fiber._instance
    assert isinstance(group, Group)
    return group


@pytest.mark.asyncio
async def test_group_lifecycle_via_loader(mod: Any) -> None:
    ctx = Context()
    loader = _load_group(ctx, [_row(id="a"), _row(id="b", disabled=True)])
    await ctx.flush()

    group = _group_of(loader)
    assert loader.resolve("a").fiber is not None
    assert loader.resolve("b").fiber is None
    assert group.config == group.data

    # Reconcile through the component: drop a, enable b, add c.
    new_rows = [_row(id="b"), _row(id="c")]
    await group.update(new_rows)
    assert group.config is new_rows
    assert "a" not in loader.store
    assert loader.resolve("b").fiber is not None
    assert loader.resolve("c").fiber is not None
    assert loader.resolve("c").fiber.state == FiberState.ACTIVE


@pytest.mark.asyncio
async def test_group_on_update_short_circuits_own_list(mod: Any) -> None:
    """loader.update with the entry's own config list reconciles in place."""
    ctx = Context()
    loader = _load_group(ctx, [_row(id="a")])
    await ctx.flush()

    group = _group_of(loader)
    old_fiber = loader.resolve("g").fiber
    old_child = loader.resolve("a").fiber
    new_rows = [_row(id="a"), _row(id="b")]
    await loader.update("g", {"config": new_rows})
    # Same group fiber (no restart) and same child fiber; b mounted.
    assert loader.resolve("g").fiber is old_fiber
    assert loader.resolve("a").fiber is old_child
    assert loader.resolve("b").fiber is not None
    assert group.config is new_rows


@pytest.mark.asyncio
async def test_group_on_update_passes_through_other_list(mod: Any) -> None:
    """A foreign waterfall payload delegates down the chain and restarts."""
    ctx = Context()
    loader = _load_group(ctx, [_row(id="a")])
    await ctx.flush()

    group = _group_of(loader)
    group_fiber = loader.resolve("g").fiber
    old_child = loader.resolve("a").fiber
    foreign_rows = [_row(id="c")]

    async def _default(cfg: Any) -> None:
        # Mirror Fiber.update: raw_config is set before the waterfall,
        # config is set by the default handler, then the fiber restarts.
        group_fiber.raw_config = cfg
        group_fiber.config = cfg
        await group_fiber.restart()

    await group_fiber.ctx.waterfall(
        "internal/update", foreign_rows, default_handler=_default
    )
    await ctx.flush()
    # Restart: a new Group instance mounted the foreign child list.
    assert loader.resolve("g").fiber._instance is not group
    assert "a" not in loader.store
    assert loader.resolve("c").fiber is not None
    assert loader.resolve("c").fiber is not old_child
    assert loader.resolve("g").fiber._instance.config == foreign_rows


@pytest.mark.asyncio
async def test_group_stop_effect_on_fiber_dispose(mod: Any) -> None:
    """Disposing the group fiber stops its children and retains the data."""
    ctx = Context()
    loader = _load_group(ctx, [_row(id="a"), _row(id="b")])
    await ctx.flush()

    group = _group_of(loader)
    group_fiber = loader.resolve("g").fiber
    child_a = loader.resolve("a").fiber
    await group_fiber.dispose()
    await ctx.flush()
    assert child_a.state == FiberState.DISPOSED
    assert "a" not in loader.store
    assert "b" not in loader.store
    assert [row.get("id") for row in group.data] == ["a", "b"]
    assert group.status()["entries"]["a"]["state"] == FiberState.DISPOSED.value
    # A directly disposed group fiber looks like a self-dispose: the entry
    # is marked disabled. Re-enabling remounts the retained children.
    assert loader.resolve("g").options.get("disabled") is True
    await loader.enable("g")
    assert loader.resolve("a").fiber is not None
    assert loader.resolve("a").fiber is not child_a
    assert loader.resolve("a").fiber.state == FiberState.ACTIVE
    assert loader.resolve("b").fiber is not None


@pytest.mark.asyncio
async def test_group_status_missing_store_entry(mod: Any) -> None:
    """status() reports a data row with no store entry as disposed."""
    ctx = Context()
    loader = _load_group(ctx, [_row(id="a")])
    await ctx.flush()

    group = _group_of(loader)
    group.data.append({"id": "ghost", "name": "group_test_mod:comp"})
    group.data.append(
        {"id": "ghost_disabled", "name": "group_test_mod:comp", "disabled": True}
    )
    status = group.status()
    assert status["entries"]["ghost"] == {
        "disabled": False,
        "state": FiberState.DISPOSED.value,
    }
    assert status["entries"]["ghost_disabled"] == {
        "disabled": True,
        "state": FiberState.DISPOSED.value,
    }


@pytest.mark.asyncio
async def test_group_on_update_without_next_fn_is_noop(mod: Any) -> None:
    """A foreign list with no delegate leaves the child list untouched."""
    ctx = Context()
    loader = _load_group(ctx, [_row(id="a")])
    await ctx.flush()

    group = _group_of(loader)
    result = await group._on_update([_row(id="z")])
    assert result is None
    assert [row.get("id") for row in group.data] == ["a"]
    assert loader.resolve("a").fiber is not None


@pytest.mark.asyncio
async def test_group_on_update_sync_next_fn(mod: Any) -> None:
    """A foreign list with a synchronous delegate returns the delegate's result."""
    ctx = Context()
    loader = _load_group(ctx, [_row(id="a")])
    await ctx.flush()

    group = _group_of(loader)
    assert (
        await group._on_update([_row(id="z")], next_fn=lambda cfg: "sync_done")
        == "sync_done"
    )
    assert [row.get("id") for row in group.data] == ["a"]
