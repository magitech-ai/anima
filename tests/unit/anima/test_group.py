import asyncio

import pytest

from anima.core import Context, FiberState, Service
from anima.group import Group


class DummyService(Service):
    def __init__(self, ctx: Context, **cfg):
        super().__init__(ctx, "dummy")
        self.cfg = cfg


@pytest.mark.asyncio
async def test_group_lifecycle_and_status():
    ctx = Context()
    active_runs = []

    def plugin_a(c: Context, **cfg):
        active_runs.append(f"a_{cfg.get('val', 0)}")

    def plugin_b(c: Context, **cfg):
        active_runs.append("b")

    group = Group(
        ctx,
        entries=[
            {"id": "p_a", "target": plugin_a, "config": {"val": 10}},
            {"id": "p_b", "target": plugin_b, "disabled": True},
        ],
    )

    fib = ctx.plugin(group)
    await ctx.flush()

    assert active_runs == ["a_10"]
    assert group.entries["p_a"].fiber is not None
    assert group.entries["p_b"].fiber is None

    st = group.status()
    assert st["name"] == "group"
    assert st["entries"]["p_a"]["disabled"] is False
    assert st["entries"]["p_a"]["state"] == FiberState.ACTIVE.value
    assert st["entries"]["p_b"]["disabled"] is True
    assert st["entries"]["p_b"]["state"] == FiberState.DISPOSED.value

    # Enable p_b
    await group.enable("p_b")
    assert "b" in active_runs
    assert group.entries["p_b"].fiber is not None

    # Enable already enabled is no-op
    await group.enable("p_b")

    # Enable nonexistent is no-op
    await group.enable("nonexistent")

    # Disable p_a
    await group.disable("p_a")
    assert group.entries["p_a"].disabled is True
    assert group.entries["p_a"].fiber is None

    # Disable already disabled is no-op
    await group.disable("p_a")

    # Disable nonexistent is no-op
    await group.disable("nonexistent")

    # Dispose group fiber
    await fib.dispose()
    await ctx.flush()
    assert len(group.entries) == 0


@pytest.mark.asyncio
async def test_group_reconciliation_and_updates():
    ctx = Context()
    load_events = []

    class TrackedPlugin:
        def __init__(self, c: Context, **cfg):
            load_events.append(f"loaded_{cfg.get('tag', 'v0')}")

    entries = [
        {"id": "t1", "target": TrackedPlugin, "config": {"tag": "v1"}},
        {"id": "t2", "target": TrackedPlugin, "config": {"tag": "v2"}},
    ]

    group = Group(ctx, entries=entries)
    ctx.plugin(group)
    await ctx.flush()
    assert load_events == ["loaded_v1", "loaded_v2"]

    # Reconcile: update t1, disable t2, add t3
    def plugin_three(c: Context):
        load_events.append("loaded_v3")

    next_called = []

    async def async_next(cfg):
        await asyncio.sleep(0.001)
        next_called.append(True)

    await group._on_update(
        {
            "entries": [
                {"id": "t1", "target": TrackedPlugin, "config": {"tag": "v1_updated"}},
                {"id": "t2", "target": TrackedPlugin, "disabled": True},
                {"id": "t3", "target": plugin_three},
            ]
        },
        next_fn=async_next,
    )
    assert next_called == [True]
    assert "loaded_v3" in load_events
    assert group.entries["t2"].disabled is True
    assert group.entries["t2"].fiber is None

    # Reconcile with list directly, re-enabling t2 and removing t3
    await group._on_update(
        [
            {"id": "t1", "target": TrackedPlugin, "config": {"tag": "v1_final"}},
            {
                "id": "t2",
                "target": TrackedPlugin,
                "config": {"tag": "v2_restored"},
                "disabled": False,
            },
        ],
        next_fn=lambda cfg: "sync_done",
    )
    assert "t3" not in group.entries
    assert group.entries["t2"].disabled is False
    assert group.entries["t2"].fiber is not None

    # Call _on_update without next_fn and add disabled new entry
    res = await group._on_update(
        [
            {"id": "t1", "target": TrackedPlugin, "config": {"tag": "v1_final"}},
            {"id": "t2", "target": TrackedPlugin, "disabled": True},
            {"id": "t4_disabled", "target": TrackedPlugin, "disabled": True},
        ]
    )
    assert res is None
    assert group.entries["t4_disabled"].fiber is None

    # Reconcile keeping t4_disabled disabled (entry.fiber is None, new_disabled == entry.disabled)
    await group.reconcile(
        [
            {"id": "t1", "target": TrackedPlugin, "config": {"tag": "v1_final"}},
            {"id": "t4_disabled", "target": TrackedPlugin, "disabled": True},
        ]
    )
    assert "t2" not in group.entries

    # Reconcile when entry.fiber was manually cleared to None and disabled becomes True
    group.entries["t1"].fiber = None
    await group.reconcile(
        [
            {"id": "t1", "target": TrackedPlugin, "disabled": True},
            {"id": "t4_disabled", "target": TrackedPlugin, "disabled": True},
        ]
    )
    assert group.entries["t1"].disabled is True

    # Enable and disable when fiber is already in target state
    group.entries["t1"].disabled = False
    group.entries["t1"].fiber = None
    await group.disable("t1")  # not entry.disabled is True, but fiber is None
    group.entries["t1"].disabled = True
    group.entries["t1"].fiber = ctx.plugin(lambda c: None)
    await group.enable("t1")  # entry.disabled is True, but fiber is not None


def test_group_add_entry_validation():
    ctx = Context()
    group = Group(ctx)

    # Missing target and name
    with pytest.raises(ValueError, match="must specify 'name' or 'target'"):
        group._add_entry_from_dict({"config": {}})

    # Valid name resolving to target
    entry = group._add_entry_from_dict({"name": "anima.group:Group"})
    assert entry.id == "anima.group:Group"
    assert entry.target is Group
