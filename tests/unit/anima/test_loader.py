"""Unit tests for the loader service surface."""

import asyncio
import logging
import sys
import types
from pathlib import Path
from typing import Any

import pytest
import yaml

from anima.core import Context, FiberState, Service
from anima.loader import (
    LoaderService,
    interpolate_env,
    load_profile,
    resolve_component_target,
    save_profile,
)


def test_resolve_component_target_colon_syntax():
    target = resolve_component_target("anima.events:EventBus")
    from anima.events import EventBus

    assert target is EventBus


def test_resolve_component_target_dot_syntax():
    target = resolve_component_target("anima.core.Context")
    from anima.core import Context as CoreContext

    assert target is CoreContext


def test_resolve_component_target_module_with_apply(monkeypatch):
    dummy_mod = types.ModuleType("dummy_component_mod")

    def apply_fn(ctx):
        return "applied"

    dummy_mod.apply = apply_fn
    monkeypatch.setitem(sys.modules, "dummy_component_mod", dummy_mod)

    target = resolve_component_target("dummy_component_mod")
    assert target is apply_fn


def test_resolve_component_target_module_without_apply():
    target = resolve_component_target("anima")
    import anima

    assert target is anima


def test_interpolate_env():
    custom_env = {
        "BOOL_TRUE": "true",
        "BOOL_FALSE": "FALSE",
        "NUM_INT": "42",
        "NUM_FLOAT": "3.14",
        "STR_VAL": "custom_str",
    }

    # Type preserving full matches
    assert interpolate_env("${BOOL_TRUE}", custom_env) is True
    assert interpolate_env("${BOOL_FALSE}", custom_env) is False
    assert interpolate_env("${NUM_INT}", custom_env) == 42
    assert interpolate_env("${NUM_FLOAT}", custom_env) == 3.14
    assert interpolate_env("${STR_VAL}", custom_env) == "custom_str"
    assert interpolate_env("${MISSING_INT:8080}", custom_env) == 8080
    assert interpolate_env("${env.STR_VAL}", custom_env) == "custom_str"
    assert interpolate_env("${NON_EXISTENT}", custom_env) == ""
    assert interpolate_env("${NON_EXISTENT:default_val}", custom_env) == "default_val"

    # Embedded in string
    assert (
        interpolate_env("http://${STR_VAL}:${NUM_INT}/api", custom_env)
        == "http://custom_str:42/api"
    )
    assert (
        interpolate_env("val is ${NOT_FOUND:fallback}", custom_env) == "val is fallback"
    )
    assert interpolate_env("hello ${MISSING} world", env={}) == "hello  world"

    # Recursive structures
    data = {
        "port": "${NUM_INT}",
        "tags": ["${STR_VAL}", 123, True],
        "nested": {"enabled": "${BOOL_TRUE}"},
    }
    interpolated = interpolate_env(data, custom_env)
    assert interpolated == {
        "port": 42,
        "tags": ["custom_str", 123, True],
        "nested": {"enabled": True},
    }

    # Non-string scalar pass-through
    assert interpolate_env(999, custom_env) == 999


def test_loader_service_registration():
    """The loader registers on the mounting ctx and owns a child tree ctx."""
    ctx = Context()
    loader = LoaderService(ctx)
    assert ctx.has("loader")
    assert loader.ctx is not ctx
    assert loader.service_ctx is ctx
    assert loader.ctx._parent is ctx
    assert isinstance(loader, LoaderService)


@pytest.mark.asyncio
async def test_load_profile_from_list(monkeypatch):
    ctx = Context()
    loaded = []

    dummy_component = types.ModuleType("custom_agent_component")

    class SampleService:
        def __init__(self, c: Context, config: dict | None = None):
            self.c = c
            self.token = (config or {}).get("token", "default")
            loaded.append(self.token)

    dummy_component.SampleService = SampleService
    monkeypatch.setitem(sys.modules, "custom_agent_component", dummy_component)

    entries = [
        # String entry
        "custom_agent_component:SampleService",
        # Dict entry without config
        {"name": "custom_agent_component:SampleService"},
        # Dict entry with config
        {
            "name": "custom_agent_component:SampleService",
            "config": {"token": "secret_xyz"},
        },
    ]

    loader = load_profile(ctx, entries)
    await ctx.flush()

    assert "default" in loaded
    assert "secret_xyz" in loaded
    assert len(loader.store) == 3


def test_load_profile_row_errors():
    """Malformed rows fail loud instead of being silently skipped."""
    ctx = Context()
    with pytest.raises(TypeError, match="must be a string or dict"):
        load_profile(ctx, [12345])
    with pytest.raises(ValueError, match="must specify 'name'"):
        load_profile(ctx, [{"config": {"ignored": True}}])
    with pytest.raises(ValueError, match="conflicting 'name' and 'target'"):
        load_profile(ctx, [{"name": "a", "target": "b"}])
    with pytest.raises(ValueError, match="both 'entries' and 'config'"):
        load_profile(ctx, [{"name": "group", "entries": [], "config": []}])


@pytest.mark.asyncio
async def test_load_profile_from_yaml_file(tmp_path: Path, monkeypatch):
    ctx = Context()
    calls = []

    dummy_mod = types.ModuleType("profile_mod")

    def sample_component(c: Context, config: dict | None = None):
        calls.append((config or {}).get("mode", "fast"))

    dummy_mod.sample_component = sample_component
    monkeypatch.setitem(sys.modules, "profile_mod", dummy_mod)

    yaml_data = [
        "profile_mod:sample_component",
        {"name": "profile_mod:sample_component", "config": {"mode": "turbo"}},
    ]

    yaml_file = tmp_path / "profile.yaml"
    save_profile(yaml_file, yaml_data)

    # Load via Path object
    load_profile(ctx, yaml_file)
    await ctx.flush()

    # Load via string path
    ctx2 = Context()
    load_profile(ctx2, str(yaml_file))
    await ctx2.flush()

    assert calls == ["fast", "turbo", "fast", "turbo"]


@pytest.mark.asyncio
async def test_load_twice_reconciles_without_duplicates(monkeypatch):
    """A second load() with the same rows retains entries instead of duplicating."""
    ctx = Context()
    calls: list[Any] = []

    dummy_mod = types.ModuleType("twice_mod")

    def comp(c: Context, config: Any = None):
        calls.append(1)

    dummy_mod.comp = comp
    monkeypatch.setitem(sys.modules, "twice_mod", dummy_mod)

    loader = LoaderService(ctx)
    rows = [{"id": "a", "name": "twice_mod:comp"}]
    loader.load(rows)
    await ctx.flush()
    assert len(loader.root.data) == 1
    first_fiber = loader.resolve("a").fiber
    loader.load([{"id": "a", "name": "twice_mod:comp"}])
    await ctx.flush()
    assert len(loader.root.data) == 1
    assert len(loader.store) == 1
    assert loader.resolve("a").fiber is first_fiber


@pytest.mark.asyncio
async def test_loader_service_isolation_intercept_and_groups():
    ctx = Context()
    loader = LoaderService(ctx)

    intercept_observed = []

    class MockInterceptedService:
        def __init__(self, c: Context):
            intercept_observed.append(c.resolve_intercept("mock_svc"))

    def sub_comp(c: Context, config: Any = None):
        return None

    dummy = types.ModuleType("intercept_mod")
    dummy.MockInterceptedService = MockInterceptedService
    dummy.sub_comp = sub_comp
    sys.modules["intercept_mod"] = dummy

    try:
        profile = {
            "env": {"MODE": "special"},
            "components": [
                {
                    "id": "entry_isolated",
                    "name": "intercept_mod:MockInterceptedService",
                    "isolate": ["unrelated_svc"],
                    "intercept": {
                        "mock_svc": {"mode": "${MODE}"},
                        "bad_svc": "not_a_dict",
                    },
                },
                {
                    "id": "my_group",
                    "target": "group",
                    "entries": [
                        {"id": "sub_a", "target": "intercept_mod:sub_comp"},
                    ],
                },
                {
                    "id": "disabled_entry",
                    "name": "intercept_mod:MockInterceptedService",
                    "disabled": True,
                },
            ],
        }

        loader.load(profile)
        await ctx.flush()

        assert intercept_observed == [{"mode": "special"}]
        assert loader.resolve("entry_isolated").fiber is not None
        assert loader.resolve("my_group").fiber is not None
        assert loader.resolve("disabled_entry").fiber is None
        # Legacy isolate list translated to the dict form.
        assert loader.resolve("entry_isolated").options["isolate"] == {
            "unrelated_svc": True
        }
        # The nested group child mounts in the shared store.
        assert loader.resolve("sub_a").fiber is not None

        # Enable disabled entry
        await loader.enable("disabled_entry")
        assert loader.resolve("disabled_entry").fiber is not None
        assert loader.resolve("disabled_entry").fiber.state == FiberState.ACTIVE

        # Enable already enabled is a no-op (no duplicate row).
        rows_before = len(loader.root.data)
        await loader.enable("disabled_entry")
        assert len(loader.root.data) == rows_before

        # Disable entry
        await loader.disable("disabled_entry")
        assert loader.resolve("disabled_entry").fiber.state == FiberState.DISPOSED

        # Disable already disabled is a no-op.
        await loader.disable("disabled_entry")

        # Enable, disable, update of unknown ids raise.
        with pytest.raises(ValueError, match="cannot resolve entry nonexistent"):
            await loader.enable("nonexistent")
        with pytest.raises(ValueError, match="cannot resolve entry nonexistent"):
            await loader.disable("nonexistent")
        with pytest.raises(ValueError, match="cannot resolve entry nonexistent"):
            await loader.update("nonexistent", {})

        # Update a disabled entry's options (partial merge, no remount).
        await loader.update("disabled_entry", {"config": {"opt": 2}})
        assert loader.resolve("disabled_entry").options["config"] == {"opt": 2}
        assert loader.resolve("disabled_entry").fiber.state == FiberState.DISPOSED

        # Update the group entry with a new (empty) child list.
        await loader.update("my_group", {"config": []})
        assert "sub_a" not in loader.store
    finally:
        sys.modules.pop("intercept_mod", None)


@pytest.mark.asyncio
async def test_loader_persistence_no_auto_save(tmp_path: Path):
    """Runtime fiber.update writes back in memory only; the file is untouched."""
    ctx = Context()
    save_file = tmp_path / "saved_profile.yaml"

    loader = LoaderService(ctx, {"filename": save_file})
    loader.load(
        [
            {"id": "p1", "name": "anima.events:EventBus", "config": {"opt": 1}},
        ]
    )
    await ctx.flush()

    # Manual save
    loader.save()
    assert save_file.exists()
    before = save_file.read_bytes()

    # Save to custom path
    alt_file = tmp_path / "alt_profile.yaml"
    loader.save(alt_file)
    assert alt_file.exists()

    # Save without filename raises
    loader_no_file = LoaderService(ctx)
    with pytest.raises(ValueError, match="No destination path specified"):
        loader_no_file.save()

    # Write-back: options update in memory, file byte-identical.
    fib = loader.resolve("p1").fiber
    assert fib is not None
    await fib.update({"opt": 999})
    assert loader.resolve("p1").options["config"] == {"opt": 999}
    assert save_file.read_bytes() == before

    # Explicit save persists the new config.
    loader.save()
    re_read = yaml.safe_load(save_file.read_text(encoding="utf-8"))
    assert re_read[0]["config"] == {"opt": 999}


def test_load_profile_error_handling(tmp_path: Path):
    ctx = Context()

    # Nonexistent file
    with pytest.raises(FileNotFoundError, match="Profile config file not found"):
        load_profile(ctx, tmp_path / "not_found.yaml")

    # Non list/dict raw data
    with pytest.raises(TypeError, match="must be a list or dict"):
        LoaderService(ctx).load(12345)  # type: ignore[arg-type]

    # Components not a list
    with pytest.raises(TypeError, match="components must resolve to a list"):
        LoaderService(ctx).load({"components": "not_a_list"})


# --- A2: nesting-bug regression ---


@pytest.mark.asyncio
async def test_a2_nesting_bug_regression(tmp_path: Path, monkeypatch):
    """The §5.5 profile: nested group entries survive load, save, and reload."""
    ctx = Context()
    calls: list[Any] = []

    dummy_mod = types.ModuleType("nest_mod")

    def comp(c: Context, config: Any = None):
        calls.append(1)

    dummy_mod.comp = comp
    monkeypatch.setitem(sys.modules, "nest_mod", dummy_mod)

    profile = [
        {
            "id": "my_group",
            "target": "group",
            "entries": [
                {"id": "sub_a", "target": "nest_mod:comp"},
                {
                    "id": "sub_group",
                    "target": "group",
                    "entries": [
                        {"id": "deep", "target": "nest_mod:comp"},
                    ],
                },
            ],
        },
    ]
    loader = load_profile(ctx, profile)
    await ctx.flush()

    assert set(loader.store) == {"my_group", "sub_a", "sub_group", "deep"}
    deep = loader.resolve("deep")
    assert deep.fiber is not None
    assert deep.fiber.state == FiberState.ACTIVE
    assert calls == [1, 1]

    saved = tmp_path / "nested.yaml"
    loader.save(saved)
    text = saved.read_text(encoding="utf-8")
    assert text.count("deep") == 1
    assert text.count("sub_a") == 1

    ctx2 = Context()
    loader2 = load_profile(ctx2, saved)
    await ctx2.flush()
    assert set(loader2.store) == {"my_group", "sub_a", "sub_group", "deep"}
    assert loader2.resolve("deep").fiber is not None
    assert loader2.resolve("deep").fiber.state == FiberState.ACTIVE


# --- A5: enable-after-disable ---


@pytest.mark.asyncio
async def test_a5_enable_after_disable_no_duplicates(tmp_path: Path, monkeypatch):
    ctx = Context()
    calls: list[Any] = []

    dummy_mod = types.ModuleType("toggle_mod")

    def comp(c: Context, config: Any = None):
        calls.append(1)

    dummy_mod.comp = comp
    monkeypatch.setitem(sys.modules, "toggle_mod", dummy_mod)

    loader = LoaderService(ctx)
    loader.load([{"id": "toggle_entry", "name": "toggle_mod:comp"}])
    await ctx.flush()
    assert len(loader.root.data) == 1

    saved = tmp_path / "toggle.yaml"
    for _ in range(2):
        await loader.disable("toggle_entry")
        entry = loader.resolve("toggle_entry")
        assert entry.fiber is not None
        assert entry.fiber.state == FiberState.DISPOSED
        assert len(loader.root.data) == 1
        await loader.enable("toggle_entry")
        assert len(loader.root.data) == 1
        assert entry.fiber is not None
        assert entry.fiber.state == FiberState.ACTIVE

    fibers = [
        loader.resolve(eid).fiber
        for eid in loader.store
        if loader.resolve(eid).fiber is not None
    ]
    assert len(fibers) == 1
    loader.save(saved)
    assert saved.read_text(encoding="utf-8").count("toggle_entry") == 1


# --- A7: isolate dict ---


@pytest.mark.asyncio
async def test_a7_isolate_dict(monkeypatch):
    ctx = Context()
    loader = LoaderService(ctx)
    calls: list[Any] = []

    dummy_mod = types.ModuleType("iso_mod")

    def comp(c: Context, config: Any = None):
        calls.append(1)

    dummy_mod.comp = comp
    monkeypatch.setitem(sys.modules, "iso_mod", dummy_mod)

    loader.load(
        [
            {"id": "a", "name": "iso_mod:comp", "isolate": {"svc": True}},
            {"id": "b", "name": "iso_mod:comp", "isolate": {"svc": True}},
            {"id": "c", "name": "iso_mod:comp", "isolate": {"svc": "shared"}},
            {"id": "d", "name": "iso_mod:comp", "isolate": {"svc": "shared"}},
            {
                "id": "e",
                "name": "iso_mod:comp",
                "intercept": {"svc": {"mode": "x"}},
            },
        ]
    )
    await ctx.flush()
    entry_a = loader.resolve("a")
    entry_b = loader.resolve("b")
    entry_c = loader.resolve("c")
    entry_d = loader.resolve("d")
    entry_e = loader.resolve("e")
    label_a = entry_a.ctx._isolated_services["svc"]
    label_b = entry_b.ctx._isolated_services["svc"]
    assert label_a == "__entry__:a:svc"
    assert label_b == "__entry__:b:svc"
    assert label_a != label_b
    assert entry_c.ctx._isolated_services["svc"] == "shared"
    assert entry_d.ctx._isolated_services["svc"] == "shared"
    assert entry_e.ctx._intercept_configs["svc"] == {"mode": "x"}
    with pytest.raises(ValueError, match="must be true or a label string"):
        await loader.update("a", {"isolate": {"svc": 42}})


@pytest.mark.asyncio
async def test_isolate_only_change_re_resolves(monkeypatch):
    """An isolate-only update notifies dependents so the fiber re-resolves.

    The service is visible before the update (no isolation) and out of
    scope after it; nothing but the hook's notify re-checks the fiber,
    since the patch-context default does not restart on isolate-only
    diffs. The fiber must end PENDING, not ACTIVE.
    """
    ctx = Context()
    loader = LoaderService(ctx)

    dep_mod = types.ModuleType("dep_mod")

    def dep(c: Context, config: Any = None):
        pass

    dep_mod.dep = dep
    monkeypatch.setitem(sys.modules, "dep_mod", dep_mod)

    ctx.provide("svc", object())
    loader.load([{"id": "d", "name": "dep_mod:dep", "inject": ["svc"]}])
    await ctx.flush()
    entry = loader.resolve("d")
    assert entry.fiber is not None
    assert entry.fiber.state == FiberState.ACTIVE
    assert "config" not in entry.options
    await loader.update("d", {"isolate": {"svc": True}})
    await entry.ctx.flush()
    await loader.await_all()
    assert entry.fiber is not None
    assert entry.fiber.state == FiberState.PENDING


@pytest.mark.asyncio
async def test_entry_inject_missing_service_stays_pending(monkeypatch):
    """Entry-level inject merges before the scheduled load checks it.

    The merge runs synchronously in _init right after registry.use, so the
    load task's _checkImpl sees the merged inject list; a missing service
    leaves the fiber PENDING instead of activating without the dependency.
    """
    ctx = Context()
    loader = LoaderService(ctx)

    dep_mod = types.ModuleType("inj_mod")

    def dep(c: Context, config: Any = None):
        pass

    dep_mod.dep = dep
    monkeypatch.setitem(sys.modules, "inj_mod", dep_mod)

    loader.load([{"id": "d", "name": "inj_mod:dep", "inject": ["missing_svc"]}])
    await ctx.flush()
    entry = loader.resolve("d")
    assert entry.fiber is not None
    assert "missing_svc" in entry.fiber.inject
    assert entry.fiber.state == FiberState.PENDING


# --- A8: write-back ---


@pytest.mark.asyncio
async def test_a8_write_back_group_fiber(monkeypatch):
    ctx = Context()
    loader = LoaderService(ctx)
    calls: list[Any] = []

    dummy_mod = types.ModuleType("wb_mod")

    def comp(c: Context, config: Any = None):
        calls.append(1)

    dummy_mod.comp = comp
    monkeypatch.setitem(sys.modules, "wb_mod", dummy_mod)

    loader.load(
        [
            {
                "id": "g",
                "name": "group",
                "group": True,
                "config": [{"id": "k", "name": "wb_mod:comp"}],
            }
        ]
    )
    await ctx.flush()
    group_fiber = loader.resolve("g").fiber
    assert group_fiber is not None
    new_rows = [{"id": "k2", "name": "wb_mod:comp"}]
    await group_fiber.update(new_rows)
    assert loader.resolve("g").options["config"] == new_rows
    assert loader.resolve("k2").fiber is not None
    assert "k" not in loader.store


# --- A9: self-dispose ---


@pytest.mark.asyncio
async def test_a9_self_dispose_marks_disabled(monkeypatch):
    ctx = Context()
    loader = LoaderService(ctx)
    writes = {"count": 0}
    monkeypatch.setattr(
        loader, "write", lambda: writes.__setitem__("count", writes["count"] + 1)
    )

    dummy_mod = types.ModuleType("sd_mod")
    release = asyncio.Event()

    def self_disposer(c: Context, config: Any = None):
        async def _later():
            await release.wait()
            await c.fiber.dispose()

        c._schedule_task(_later())

    def parent_comp(c: Context, config: Any = None):
        def child(c2: Context, config: Any = None):
            c2._schedule_task(c2.fiber.dispose())

        c.use(child)

    dummy_mod.self_disposer = self_disposer
    dummy_mod.parent_comp = parent_comp
    monkeypatch.setitem(sys.modules, "sd_mod", dummy_mod)

    loader.load(
        [
            {"id": "doomed", "name": "sd_mod:self_disposer"},
            {"id": "parent", "name": "sd_mod:parent_comp"},
        ]
    )
    await ctx.flush()
    doomed = loader.resolve("doomed")
    assert doomed.fiber is not None
    assert doomed.fiber.state == FiberState.ACTIVE
    before = writes["count"]
    release.set()
    await doomed.ctx.flush()
    assert doomed.options.get("disabled") is True
    assert writes["count"] > before
    # A child plugin disposing itself does not disable its entry.
    parent = loader.resolve("parent")
    await parent.ctx.flush()
    assert parent.options.get("disabled") is not True
    assert parent.fiber is not None
    assert parent.fiber.state == FiberState.ACTIVE


@pytest.mark.asyncio
async def test_self_dispose_ignored_after_registry_delete(monkeypatch):
    """Disposal driven by registry deletion (HMR path) does not disable the entry."""
    ctx = Context()
    loader = LoaderService(ctx)
    calls: list[Any] = []

    dummy_mod = types.ModuleType("del_mod")

    def comp(c: Context, config: Any = None):
        calls.append(1)

    dummy_mod.comp = comp
    monkeypatch.setitem(sys.modules, "del_mod", dummy_mod)

    loader.load([{"id": "a", "name": "del_mod:comp"}])
    await ctx.flush()
    entry = loader.resolve("a")
    assert entry.fiber is not None
    fiber = entry.fiber
    entry.ctx.registry.delete(fiber.target)
    await entry.ctx.flush()
    assert fiber.state == FiberState.DISPOSED
    assert entry.options.get("disabled") is not True


@pytest.mark.asyncio
async def test_internal_plugin_case3_and_case5_synthetic(monkeypatch):
    """Direct handler checks for the oracle's case 3 and case 5 guards.

    Case 3 (child plugin under the entry) is unreachable through
    Entry._init, which sets fiber.entry only on entry fibers; case 5's
    early return needs a tree-owner fiber in UNLOADING. Both are covered
    here synthetically so the guards stay honest.
    """
    from anima.core import FiberState as _State
    from anima.tree import EntryTree as _EntryTree

    ctx = Context()
    loader = LoaderService(ctx)
    dummy_mod = types.ModuleType("syn_mod")

    def comp(c: Context, config: Any = None):
        pass

    dummy_mod.comp = comp
    monkeypatch.setitem(sys.modules, "syn_mod", dummy_mod)

    await loader.create({"id": "e", "name": "syn_mod:comp"})
    await loader.await_all()
    entry = loader.resolve("e")
    assert entry.fiber is not None

    # Case 3: fiber.entry set, parent fiber's entry is the same entry.
    child_fiber = entry.ctx.use(lambda c: None)
    child_fiber.entry = entry
    child_fiber.parent_fiber = entry.fiber
    child_fiber.uid = None
    loader._on_internal_plugin(child_fiber)
    assert entry.options.get("disabled") is not True

    # Case 5: the owner fiber is UNLOADING, so a subtree entry's disposal
    # is ignored. Build the subtree inside the owner component's body,
    # force the owner into UNLOADING, then dispose the subtree entry.
    subtrees: dict[str, Any] = {}

    class _SubTree(_EntryTree):
        def write(self) -> None:
            pass

    def owner_comp(c: Context, config: Any = None) -> None:
        subtrees["tree"] = _SubTree(c, loader=loader)

    dummy_mod.owner_comp = owner_comp
    await loader.create({"id": "own", "name": "syn_mod:owner_comp"})
    await loader.await_all()
    owner_entry = loader.resolve("own")
    assert owner_entry.fiber is not None
    subtree = subtrees["tree"]
    assert subtree.owner_fiber is owner_entry.fiber
    await subtree.create({"id": "s", "name": "syn_mod:comp"})
    await loader.await_all()
    sub_entry = subtree.resolve("s")
    assert sub_entry.fiber is not None
    owner_entry.fiber.state = _State.UNLOADING
    await sub_entry.fiber.dispose()
    assert sub_entry.options.get("disabled") is not True


# --- A15: locate / exit ---


@pytest.mark.asyncio
async def test_a15_locate_and_exit(monkeypatch):
    ctx = Context()
    loader = LoaderService(ctx)
    seen: list[Any] = []

    dummy_mod = types.ModuleType("loc_mod")

    def comp(c: Context, config: Any = None):
        seen.append(loader.locate())

    dummy_mod.comp = comp
    monkeypatch.setitem(sys.modules, "loc_mod", dummy_mod)

    loader.load([{"id": "here", "name": "loc_mod:comp"}])
    await ctx.flush()
    assert seen == ["here"]
    assert loader.locate(ctx.fiber) is None
    assert loader.exit() is None


# --- loader check (await intercept) ---


@pytest.mark.asyncio
async def test_loader_check_blocks_while_tasks_pending(monkeypatch):
    """The re-provided bound check reports False while the loader has tasks."""
    ctx = Context()
    intercepted = ctx.intercept("loader", {"await": True})
    loader = LoaderService(intercepted)
    assert loader._loader_check() is True

    started = asyncio.Event()
    release = asyncio.Event()

    def slow_comp(c: Context, config: Any = None) -> Any:
        async def _run() -> None:
            started.set()
            await release.wait()

        return _run()

    dummy_mod = types.ModuleType("chk_mod")
    dummy_mod.slow = slow_comp
    monkeypatch.setitem(sys.modules, "chk_mod", dummy_mod)

    create_task = asyncio.get_running_loop().create_task(
        loader.create({"id": "slow", "name": "chk_mod:slow"})
    )
    for _ in range(50):
        await asyncio.sleep(0)
        if started.is_set():
            break
    assert started.is_set()
    assert loader.get_tasks()
    assert loader._loader_check() is False
    # The reflect impl carries the bound check.
    impl = intercepted.reflect._get_impl("loader", strict=False)
    assert impl is not None and impl.check is not None
    assert impl.check() is False
    release.set()
    await create_task
    await loader.await_all()
    assert loader._loader_check() is True


@pytest.mark.asyncio
async def test_loader_update_moves_entry(monkeypatch):
    ctx = Context()
    loader = LoaderService(ctx)
    calls: list[Any] = []

    dummy_mod = types.ModuleType("mv_mod")

    def comp(c: Context, config: Any = None):
        calls.append(1)

    dummy_mod.comp = comp
    monkeypatch.setitem(sys.modules, "mv_mod", dummy_mod)

    await loader.create({"id": "g", "name": "group", "group": True, "config": []})
    await loader.create({"id": "a", "name": "mv_mod:comp"})
    await loader.await_all()
    entry = await loader.update("a", {}, parent="g", position=0)
    assert entry.parent is loader.resolve("g").subgroup
    assert loader.resolve("a").fiber is not None
    assert loader.resolve("a").fiber.state == FiberState.ACTIVE


@pytest.mark.asyncio
async def test_show_log_group_entries_silent(monkeypatch, caplog):
    ctx = Context()
    loader = LoaderService(ctx)
    loader.enable_logs = True
    calls: list[Any] = []

    dummy_mod = types.ModuleType("log_mod")

    def comp(c: Context, config: Any = None):
        calls.append(1)

    dummy_mod.comp = comp
    monkeypatch.setitem(sys.modules, "log_mod", dummy_mod)
    loader.load(
        [
            {"id": "a", "name": "log_mod:comp"},
            {"id": "g", "name": "group", "group": True, "config": []},
        ]
    )
    await ctx.flush()
    assert loader.resolve("a").fiber is not None
    assert loader.resolve("g").fiber is not None
    with caplog.at_level("INFO", logger="anima.loader"):
        loader.show_log(loader.resolve("a"), "apply")
        loader.show_log(loader.resolve("g"), "apply")
    assert "apply plugin log_mod:comp" in caplog.text
    assert "apply plugin group" not in caplog.text


# --- Service mirror: entry-provided services visible tree-wide ---


def _mirror_module(monkeypatch: pytest.MonkeyPatch) -> Any:
    """A dummy module with a providing service and a consumer probe."""

    class ProvidingService(Service):
        def __init__(self, ctx: Context, config: Any = None) -> None:
            cfg = dict(config or {})
            super().__init__(ctx, cfg.get("provide", "xsvc"))
            self.marker = cfg.get("marker", "x")

    seen: dict[str, Any] = {}

    def consumer(ctx: Context, config: Any = None) -> None:
        seen["has"] = ctx.has("xsvc")
        seen["get"] = ctx.get("xsvc", strict=False) is not None

    mod = types.ModuleType("mirror_mod")
    mod.ProvidingService = ProvidingService
    mod.consumer = consumer
    mod.seen = seen
    monkeypatch.setitem(sys.modules, "mirror_mod", mod)
    return mod


@pytest.mark.asyncio
async def test_mirror_entry_service_visible_mounting_and_sibling(
    monkeypatch,
) -> None:
    """A service provided by one entry is visible from the mounting ctx and siblings."""
    mod = _mirror_module(monkeypatch)
    ctx = Context()
    loader = LoaderService(ctx)
    loader.load(
        [
            {"id": "svc", "name": "mirror_mod:ProvidingService"},
            {"id": "con", "name": "mirror_mod:consumer"},
        ]
    )
    await ctx.flush()
    assert ctx.has("xsvc")
    assert ctx.get("xsvc", strict=False) is loader.resolve("svc").fiber._instance
    assert mod.seen == {"has": True, "get": True}
    assert loader.resolve("svc").ctx.has("xsvc")


@pytest.mark.asyncio
async def test_mirror_isolated_service_not_mirrored(monkeypatch) -> None:
    """An isolated service stays inside its entry: no mirror, no sibling sight."""
    mod = _mirror_module(monkeypatch)
    ctx = Context()
    loader = LoaderService(ctx)
    loader.load(
        [
            {
                "id": "svc",
                "name": "mirror_mod:ProvidingService",
                "isolate": {"xsvc": True},
            },
            {"id": "con", "name": "mirror_mod:consumer"},
        ]
    )
    await ctx.flush()
    assert not ctx.has("xsvc")
    assert loader.resolve("svc").ctx.has("xsvc")
    assert mod.seen == {"has": False, "get": False}


@pytest.mark.asyncio
async def test_mirror_removed_when_entry_disabled(monkeypatch) -> None:
    """Disabling the providing entry removes the mirror; re-enabling restores it."""
    _mirror_module(monkeypatch)
    ctx = Context()
    loader = LoaderService(ctx)
    loader.load([{"id": "svc", "name": "mirror_mod:ProvidingService"}])
    await ctx.flush()
    assert ctx.has("xsvc")
    await loader.disable("svc")
    assert not ctx.has("xsvc")
    await loader.enable("svc")
    assert ctx.has("xsvc")


@pytest.mark.asyncio
async def test_mirror_last_provider_wins(monkeypatch) -> None:
    """Two entries providing one name: the last wins; losing it falls back."""
    _mirror_module(monkeypatch)
    ctx = Context()
    loader = LoaderService(ctx)
    loader.load(
        [
            {
                "id": "first",
                "name": "mirror_mod:ProvidingService",
                "config": {"marker": "first"},
            },
            {
                "id": "second",
                "name": "mirror_mod:ProvidingService",
                "config": {"marker": "second"},
            },
        ]
    )
    await ctx.flush()
    assert ctx.get("xsvc", strict=False).marker == "second"
    await loader.disable("second")
    assert ctx.get("xsvc", strict=False).marker == "first"
    await loader.enable("second")
    assert ctx.get("xsvc", strict=False).marker == "second"


@pytest.mark.asyncio
async def test_mirror_no_error_records_on_disable_remove_reenable(
    monkeypatch, caplog
) -> None:
    """Disabling, removing and re-enabling a provider logs no emit errors.

    Regression test: _remirror_service used to re-enter itself through
    the disposer's notify, recursing until RecursionError and leaving
    KeyError traces that the event bus swallowed.
    """
    _mirror_module(monkeypatch)
    ctx = Context()
    loader = LoaderService(ctx)
    loader.load(
        [
            {
                "id": "first",
                "name": "mirror_mod:ProvidingService",
                "config": {"marker": "first"},
            },
            {
                "id": "second",
                "name": "mirror_mod:ProvidingService",
                "config": {"marker": "second"},
            },
        ]
    )
    await ctx.flush()
    assert ctx.get("xsvc", strict=False).marker == "second"
    with caplog.at_level(logging.ERROR):
        await loader.disable("second")
        assert ctx.get("xsvc", strict=False).marker == "first"
        await loader.enable("second")
        assert ctx.get("xsvc", strict=False).marker == "second"
        await loader.disable("second")
        assert ctx.get("xsvc", strict=False).marker == "first"
        entry = loader.resolve("second")
        loader.remove("second")
        await entry.parent.ctx.flush()
        await ctx.flush()
        assert ctx.get("xsvc", strict=False).marker == "first"
        entry = loader.resolve("first")
        loader.remove("first")
        await entry.parent.ctx.flush()
        await ctx.flush()
        assert not ctx.has("xsvc")
    assert "Error in emit listener" not in caplog.text
    assert "RecursionError" not in caplog.text
    assert "KeyError" not in caplog.text


@pytest.mark.asyncio
async def test_mirror_does_not_clobber_direct_provide(monkeypatch) -> None:
    """A service provided directly on the mounting ctx keeps its slot."""
    mod = _mirror_module(monkeypatch)
    ctx = Context()
    direct_fiber = ctx.use(mod.ProvidingService, {"marker": "direct"})
    await ctx.flush()
    assert ctx.get("xsvc", strict=False) is direct_fiber._instance
    loader = LoaderService(ctx)
    loader.load(
        [
            {
                "id": "svc",
                "name": "mirror_mod:ProvidingService",
                "config": {"marker": "entry"},
            }
        ]
    )
    await ctx.flush()
    assert ctx.get("xsvc", strict=False) is direct_fiber._instance
    assert loader.resolve("svc").ctx.get("xsvc", strict=False).marker == "entry"


# --- Coverage: baseUrl, locate walk-up, commit guards, patch-context ---


def test_loader_service_base_url_config() -> None:
    ctx = Context()
    loader = LoaderService(ctx, {"baseUrl": "file:///tmp/"})
    assert loader.ctx.baseUrl == "file:///tmp/"


@pytest.mark.asyncio
async def test_locate_child_fiber(monkeypatch) -> None:
    """locate() walks up to the owning entry for a child-plugin fiber."""
    ctx = Context()
    loader = LoaderService(ctx)
    children: list[Any] = []

    dummy_mod = types.ModuleType("loc_mod")

    def child(c: Context, config: Any = None) -> None:
        return None

    def parent(c: Context, config: Any = None) -> None:
        children.append(c.use(child))

    dummy_mod.child = child
    dummy_mod.parent = parent
    monkeypatch.setitem(sys.modules, "loc_mod", dummy_mod)
    loader.load([{"id": "p", "name": "loc_mod:parent"}])
    await ctx.flush()
    assert children
    assert loader.locate(children[0]) == "p"
    assert loader.locate() is None


@pytest.mark.asyncio
async def test_on_commit_untracked_fiber_is_noop(monkeypatch) -> None:
    """A config change on a fiber the loader does not track writes nothing."""
    ctx = Context()
    loader = LoaderService(ctx)

    dummy_mod = types.ModuleType("commit_mod")

    def comp(c: Context, config: Any = None) -> None:
        return None

    dummy_mod.comp = comp
    monkeypatch.setitem(sys.modules, "commit_mod", dummy_mod)
    fiber = ctx.use(comp, {"v": 1})
    await ctx.flush()
    assert fiber.state == FiberState.ACTIVE
    await fiber.update({"v": 2})
    assert loader.store == {}


@pytest.mark.asyncio
async def test_on_patch_context_sync_next_fn(monkeypatch) -> None:
    """A synchronous patch-context delegate returns its result unchanged."""
    ctx = Context()
    loader = LoaderService(ctx)

    dummy_mod = types.ModuleType("patch_mod")

    def comp(c: Context, config: Any = None) -> None:
        return None

    dummy_mod.comp = comp
    monkeypatch.setitem(sys.modules, "patch_mod", dummy_mod)
    loader.load([{"id": "a", "name": "patch_mod:comp"}])
    await ctx.flush()
    entry = loader.resolve("a")
    assert await loader._on_patch_context(entry, lambda: "sync_done") == "sync_done"


# --- P1-5: JsExpr end-to-end (internal/config interpolation) ---


def _install_jsexpr_mod(monkeypatch: pytest.MonkeyPatch) -> Any:
    """A dummy module with a recording component and a child-mounting one."""
    calls: list[Any] = []
    child_calls: list[Any] = []

    def child(c: Context, config: Any = None) -> None:
        child_calls.append(dict(config) if isinstance(config, dict) else config)

    def comp(c: Context, config: Any = None) -> None:
        calls.append(dict(config) if isinstance(config, dict) else config)

    def parent(c: Context, config: Any = None) -> None:
        calls.append(dict(config) if isinstance(config, dict) else config)
        c.use(child, {"mode": {"__jsExpr": "process.env.ANIMA_LOADER_MISSING ?? 'x'"}})

    mod = types.ModuleType("loader_jsexpr_mod")
    mod.comp = comp
    mod.parent = parent
    mod.child = child
    mod.calls = calls  # type: ignore[attr-defined]
    mod.child_calls = child_calls  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "loader_jsexpr_mod", mod)
    return mod


@pytest.mark.asyncio
async def test_internal_config_interpolates_entry_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mod = _install_jsexpr_mod(monkeypatch)
    monkeypatch.setenv("ANIMA_LOADER_TEST", "from-env")
    ctx = Context()
    loader = LoaderService(ctx)
    loader.load(
        [
            {
                "id": "a",
                "name": "loader_jsexpr_mod:comp",
                "config": {
                    "mode": {"__jsExpr": "process.env.ANIMA_LOADER_TEST"},
                    "port": {
                        "__jsExpr": "Number(process.env.ANIMA_LOADER_MISSING ?? 9090)"
                    },
                    "plain": "keep",
                },
            }
        ]
    )
    await ctx.flush()
    assert mod.calls == [{"mode": "from-env", "port": 9090, "plain": "keep"}]
    entry = loader.resolve("a")
    assert entry.fiber is not None
    assert entry.fiber.config == {"mode": "from-env", "port": 9090, "plain": "keep"}
    # The stored options keep the raw nodes for write-back.
    assert entry.options["config"]["mode"] == {
        "__jsExpr": "process.env.ANIMA_LOADER_TEST"
    }
    assert entry.options["config"]["port"] == {
        "__jsExpr": "Number(process.env.ANIMA_LOADER_MISSING ?? 9090)"
    }


@pytest.mark.asyncio
async def test_internal_config_skips_fiber_without_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mod = _install_jsexpr_mod(monkeypatch)
    ctx = Context()
    LoaderService(ctx)
    fiber = ctx.use(
        mod.comp, {"mode": {"__jsExpr": "process.env.ANIMA_LOADER_MISSING ?? 'x'"}}
    )
    await ctx.flush()
    assert fiber.state == FiberState.ACTIVE
    assert mod.calls == [
        {"mode": {"__jsExpr": "process.env.ANIMA_LOADER_MISSING ?? 'x'"}}
    ]


@pytest.mark.asyncio
async def test_internal_config_skips_tree_carriers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from anima.tree import EntryGroup

    calls: list[Any] = []

    class Carrier:
        def __init__(self, c: Context, config: Any = None) -> None:
            calls.append(config)

    setattr(Carrier, EntryGroup.key, True)
    mod = types.ModuleType("loader_carrier_mod")
    mod.carrier = Carrier
    monkeypatch.setitem(sys.modules, "loader_carrier_mod", mod)
    ctx = Context()
    loader = LoaderService(ctx)
    loader.load(
        [
            {
                "id": "c",
                "name": "loader_carrier_mod:carrier",
                "config": {
                    "mode": {"__jsExpr": "process.env.ANIMA_LOADER_MISSING ?? 'x'"}
                },
            }
        ]
    )
    await ctx.flush()
    assert calls == [{"mode": {"__jsExpr": "process.env.ANIMA_LOADER_MISSING ?? 'x'"}}]


@pytest.mark.asyncio
async def test_internal_config_skips_entry_children(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mod = _install_jsexpr_mod(monkeypatch)
    ctx = Context()
    loader = LoaderService(ctx)
    loader.load([{"id": "p", "name": "loader_jsexpr_mod:parent", "config": {}}])
    await ctx.flush()
    # The child mounts on the entry ctx, so flush that ctx too: the
    # mounting ctx's flush cannot see the child's load task.
    await loader.resolve("p").ctx.flush()
    assert mod.calls == [{}]
    assert mod.child_calls == [
        {"mode": {"__jsExpr": "process.env.ANIMA_LOADER_MISSING ?? 'x'"}}
    ]


@pytest.mark.asyncio
async def test_internal_config_without_current_fiber() -> None:
    ctx = Context()
    LoaderService(ctx)
    resolved = await ctx.waterfall("internal/config", {"mode": {"__jsExpr": "1 === 1"}})
    assert resolved == {"mode": {"__jsExpr": "1 === 1"}}


@pytest.mark.asyncio
async def test_internal_config_group_keeps_child_rows_literal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mod = _install_jsexpr_mod(monkeypatch)
    ctx = Context()
    loader = LoaderService(ctx)
    loader.load(
        [
            {
                "id": "g",
                "name": "group",
                "group": True,
                "config": [
                    {
                        "id": "kid",
                        "name": "loader_jsexpr_mod:comp",
                        "config": {
                            "mode": {
                                "__jsExpr": "process.env.ANIMA_LOADER_MISSING ?? 'kid'"
                            }
                        },
                    }
                ],
            }
        ]
    )
    await ctx.flush()
    # The group fiber keeps the child rows literal; the child fiber
    # interpolates its own config.
    group_entry = loader.resolve("g")
    assert group_entry.fiber is not None
    assert group_entry.fiber.config[0]["config"]["mode"] == {
        "__jsExpr": "process.env.ANIMA_LOADER_MISSING ?? 'kid'"
    }
    assert mod.calls == [{"mode": "kid"}]


@pytest.mark.asyncio
async def test_js_expr_yaml_round_trip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from anima.jsexpr import load_yaml

    _install_jsexpr_mod(monkeypatch)
    profile = tmp_path / "profile.yaml"
    profile.write_text(
        "- id: a\n"
        "  name: loader_jsexpr_mod:comp\n"
        "  config:\n"
        "    mode: !!js process.env.ANIMA_LOADER_MISSING ?? 'workspace-write'\n"
        "    port: !!js Number(process.env.ANIMA_LOADER_MISSING ?? 8080)\n"
        "  disabled: !!js ctx.disabled\n",
        encoding="utf-8",
    )
    ctx = Context()
    loader = LoaderService(ctx)
    loader.load(profile)
    await ctx.flush()
    out = tmp_path / "out.yaml"
    loader.save(out)
    dumped = out.read_text(encoding="utf-8")
    assert "!!js" in dumped
    reloaded = load_yaml(dumped)
    assert reloaded == loader.root.data
    assert reloaded[0]["config"]["mode"] == {
        "__jsExpr": "process.env.ANIMA_LOADER_MISSING ?? 'workspace-write'"
    }
    assert reloaded[0]["config"]["port"] == {
        "__jsExpr": "Number(process.env.ANIMA_LOADER_MISSING ?? 8080)"
    }
    assert reloaded[0]["disabled"] == {"__jsExpr": "ctx.disabled"}
    # A fresh loader reading the saved file sees the same rows.
    ctx2 = Context()
    loader2 = LoaderService(ctx2)
    loader2.load(out)
    await ctx2.flush()
    assert loader2.root.data == loader.root.data


@pytest.mark.asyncio
async def test_on_internal_config_sync_next_fn() -> None:
    """A synchronous delegate returns its result unchanged."""
    ctx = Context()
    loader = LoaderService(ctx)
    assert await loader._on_internal_config({"a": 1}, lambda: {"a": 1}) == {"a": 1}
