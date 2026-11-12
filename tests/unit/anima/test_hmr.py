"""HMR: dependency-graph reload, debounce, externals, rollback, oracle events.

P1-8 replaces the single-module reload with the graph reload: changing a
shared dependency reloads its plugin dependents, a failed reload rolls
back to the old code, externals trigger the full-reload hook, rapid
changes debounce into one reload, and config-file changes refresh the
include tree. Events align to the oracle: ``hmr/change(url)`` and
``hmr/reload(reloads)`` only.
"""

import asyncio
import importlib.util
import logging
import os
import sys
import time
from functools import partial
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest.mock import patch

import pytest

from anima.core import Context, FiberState
from anima.hmr import HmrService, Reload, handle_error
from anima.loader import LoaderService
from anima.watcher import WatcherService

# --- helpers ----------------------------------------------------------------


def _write(tmp_path: Path, name: str, source: str) -> Path:
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
    return path


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, str(path))
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    mod.__spec__ = spec
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _bump(path: Path) -> None:
    now = time.time() + 2
    os.utime(path, (now, now))


def _pop(*names: str) -> None:
    for name in names:
        sys.modules.pop(name, None)


# --- lifecycle / lookup (unchanged behaviour) --------------------------------


@pytest.mark.asyncio
async def test_hmr_service_lifecycle_and_start_stop(tmp_path: Path):
    ctx = Context()
    ctx.use(WatcherService)
    ctx.use(HmrService, root=tmp_path, interval=0.01)
    await ctx.flush()

    hmr: HmrService = ctx.hmr
    assert hmr._active is True

    # Calling start when already active is a no-op
    hmr.start()
    assert hmr._active is True

    # Calling stop
    hmr.stop()
    assert hmr._active is False
    assert hmr._disposer is None

    # Calling stop again is safe
    hmr.stop()


@pytest.mark.asyncio
async def test_hmr_config_validation(tmp_path: Path):
    ctx = Context()
    with pytest.raises(ValueError, match="debounce"):
        HmrService(ctx, {"root": tmp_path, "debounce": -1})
    with pytest.raises(TypeError, match="debounce"):
        HmrService(ctx, {"root": tmp_path, "debounce": "100"})
    with pytest.raises(TypeError, match="full_reload"):
        HmrService(ctx, {"root": tmp_path, "full_reload": "not-callable"})
    with pytest.raises(TypeError, match="externals"):
        HmrService(ctx, {"root": tmp_path, "externals": "not-a-list"})
    with pytest.raises(TypeError, match="externals"):
        HmrService(ctx, {"root": tmp_path, "externals": [42]})


def test_hmr_make_reload_filename_variants(tmp_path: Path):
    ctx = Context()
    hmr = HmrService(ctx, {"root": tmp_path})

    def _comp(ctx: Context) -> None:
        pass

    # Unknown module: no file, no runtime.
    assert hmr._make_reload(_comp, "no_such_module").filename == ""
    assert hmr._make_reload(_comp, "no_such_module").runtime is None

    # Module with no __file__: no filename.
    none_mod = ModuleType("p18_nofile")
    none_mod.__file__ = None  # type: ignore[assignment]
    sys.modules["p18_nofile"] = none_mod
    # Module with an unresolvable __file__: the raw value is kept.
    bad_mod = ModuleType("p18_badfile")
    bad_mod.__file__ = "\0invalid"
    sys.modules["p18_badfile"] = bad_mod
    try:
        assert hmr._make_reload(_comp, "p18_nofile").filename == ""
        assert hmr._make_reload(_comp, "p18_badfile").filename == "\0invalid"
    finally:
        _pop("p18_nofile", "p18_badfile")


@pytest.mark.asyncio
async def test_hmr_find_module_by_path(tmp_path: Path):
    ctx = Context()
    hmr = HmrService(ctx, {"root": tmp_path})

    # Create a dynamic module
    mod_path = tmp_path / "dynamic_pkg" / "mod_a.py"
    mod_path.parent.mkdir(parents=True, exist_ok=True)
    mod_path.write_text("VALUE = 10\n")

    mod_name = "test_dynamic_pkg_mod_a"
    spec = importlib.util.spec_from_file_location(mod_name, str(mod_path))
    assert spec is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    assert spec.loader is not None
    spec.loader.exec_module(mod)

    try:
        # Match via mod.__file__
        found_name, found_mod = hmr._find_module_by_path(mod_path)  # type: ignore[misc]
        assert found_name == mod_name
        assert found_mod is mod

        # Match via relative path when __file__ is None
        mod.__file__ = None  # type: ignore[assignment]
        # Register under relative module name
        rel_mod_name = "dynamic_pkg.mod_a"
        sys.modules[rel_mod_name] = mod

        found_rel_name, found_rel_mod = hmr._find_module_by_path(mod_path)  # type: ignore[misc]
        assert found_rel_name == rel_mod_name
        assert found_rel_mod is mod

        # Module with None in sys.modules
        sys.modules["none_mod"] = None  # type: ignore[assignment]
        assert hmr._find_module_by_path(mod_path) == (rel_mod_name, mod)
        sys.modules.pop("none_mod", None)

        # Module with invalid __file__ raising ValueError on Path resolve
        dummy_mod = ModuleType("bad_file_mod")
        dummy_mod.__file__ = "\0invalid_path"
        sys.modules["bad_file_mod"] = dummy_mod
        assert hmr._find_module_by_path(mod_path) == (rel_mod_name, mod)
        sys.modules.pop("bad_file_mod", None)

        # Path not in sys.modules
        assert hmr._find_module_by_path(tmp_path / "unknown.py") is None

        # Path outside root raising ValueError in relative_to
        assert hmr._find_module_by_path(Path("/other/path/mod.py")) is None

        # Exception when resolving __file__
        with patch.object(Path, "resolve", side_effect=OSError("disk error")):
            assert hmr._find_module_by_path(mod_path) is None

    finally:
        sys.modules.pop(mod_name, None)
        sys.modules.pop("dynamic_pkg.mod_a", None)


# --- dependency graph --------------------------------------------------------


def test_hmr_build_graph_edges(tmp_path: Path):
    ctx = Context()
    hmr = HmrService(ctx, {"root": tmp_path})

    pkg = tmp_path / "p18_pkg"
    pkg.mkdir()
    _write(tmp_path, "p18_pkg/__init__.py", "")
    _write(tmp_path, "p18_pkg/sib.py", "VALUE = 1\n")
    _write(tmp_path, "p18_pkg/mod.py", "from . import sib\nfrom .sib import VALUE\n")
    _write(tmp_path, "p18_g_b.py", "VALUE = 1\n")
    _write(
        tmp_path,
        "p18_g_a.py",
        "import p18_g_b\nimport not_loaded_xyz\n"
        "from not_loaded_xyz import thing\nfrom p18_g_b import *\n",
    )
    # A relative import that escapes the package yields no edges.
    _write(tmp_path, "p18_g_esc.py", "from .. import os\n")
    _write(tmp_path, "p18_g_broken.py", "def broken(:\n")

    names = {
        "p18_pkg": tmp_path / "p18_pkg/__init__.py",
        "p18_pkg.sib": tmp_path / "p18_pkg/sib.py",
        "p18_pkg.mod": tmp_path / "p18_pkg/mod.py",
        "p18_g_b": tmp_path / "p18_g_b.py",
        "p18_g_a": tmp_path / "p18_g_a.py",
        "p18_g_esc": tmp_path / "p18_g_esc.py",
        "p18_g_broken": tmp_path / "p18_g_broken.py",
    }
    try:
        for name, path in names.items():
            mod = ModuleType(name)
            mod.__file__ = str(path)
            mod.__package__ = name.rpartition(".")[0]
            sys.modules[name] = mod
        # A module whose file cannot be resolved is skipped, not fatal.
        badfile = ModuleType("p18_g_badfile")
        badfile.__file__ = "\0invalid.py"
        sys.modules["p18_g_badfile"] = badfile

        graph = hmr._build_graph()
        assert graph["p18_g_a"] == {"p18_g_b"}
        assert graph["p18_g_b"] == set()
        assert graph["p18_pkg.mod"] == {"p18_pkg.sib"}
        assert graph["p18_pkg.sib"] == set()
        assert graph["p18_pkg"] == set()
        assert graph["p18_g_esc"] == set()
        assert "p18_g_badfile" not in graph
        # Unparseable source yields no edges instead of raising.
        assert graph["p18_g_broken"] == set()
        # Second build reuses the parse cache.
        assert hmr._build_graph() == graph
    finally:
        _pop(*names, "p18_g_badfile")


def test_hmr_analyze_changes_classifies_dependents(tmp_path: Path):
    ctx = Context()
    hmr = HmrService(ctx, {"root": tmp_path})
    _write(tmp_path, "p18_c_shared.py", "VALUE = 1\n")
    _write(tmp_path, "p18_c_mid.py", "import p18_c_shared\n")
    _write(tmp_path, "p18_c_top.py", "import p18_c_mid\n")
    _write(tmp_path, "p18_c_alone.py", "VALUE = 0\n")
    names = ["p18_c_shared", "p18_c_mid", "p18_c_top", "p18_c_alone"]
    try:
        for name in names:
            mod = ModuleType(name)
            mod.__file__ = str(tmp_path / f"{name}.py")
            mod.__package__ = ""
            sys.modules[name] = mod
        accepted, declined = hmr._analyze_changes({"p18_c_shared"})
        assert accepted == {"p18_c_shared", "p18_c_mid", "p18_c_top"}
        assert "p18_c_alone" in declined
        assert "p18_c_shared" not in declined
    finally:
        _pop(*names)


def test_hmr_topological_order_diamond_and_cycle():
    # A module with two dependencies becomes ready only after the second
    # one is emitted; a cycle falls back to deterministic sorted order.
    ctx = Context()
    hmr = HmrService(ctx, {})
    order = hmr._topological_order(
        {"a", "b", "c"},
        {"a": {"b", "c"}, "b": set(), "c": set()},
    )
    assert order == ["b", "c", "a"]
    cyclic = hmr._topological_order({"x", "y"}, {"x": {"y"}, "y": {"x"}})
    assert cyclic == ["x", "y"]


def test_hmr_target_stat_unknown_and_missing_file(tmp_path: Path):
    ctx = Context()
    hmr = HmrService(ctx, {"root": tmp_path})
    assert hmr._target_stat("no_such_module_xyz") is None
    ghost = ModuleType("p18_ghost")
    ghost.__file__ = str(tmp_path / "p18_ghost.py")  # never created
    sys.modules["p18_ghost"] = ghost
    try:
        assert hmr._target_stat("p18_ghost") is None
    finally:
        _pop("p18_ghost")


# --- acceptance 1: shared dependency reloads dependents ----------------------


@pytest.mark.asyncio
async def test_hmr_shared_dependency_reloads_dependents(tmp_path: Path):
    shared_path = _write(tmp_path, "p18_shared.py", "VALUE = 1\n")
    plugin_path = _write(
        tmp_path,
        "p18_plugin.py",
        "from p18_shared import VALUE\n"
        "PLUGIN_VALUE = VALUE\n"
        "class Comp:\n"
        "    def __init__(self, ctx):\n"
        "        self.v = PLUGIN_VALUE\n",
    )
    other_path = _write(
        tmp_path,
        "p18_other.py",
        "import p18_shared\n"
        "class Other:\n"
        "    def __init__(self, ctx):\n"
        "        self.v = p18_shared.VALUE\n",
    )
    alone_path = _write(
        tmp_path,
        "p18_alone.py",
        "class Alone:\n    def __init__(self, ctx):\n        self.v = 0\n",
    )
    ctx = Context()
    hmr = HmrService(ctx, {"root": tmp_path})
    shared = _load("p18_shared", shared_path)
    plugin = _load("p18_plugin", plugin_path)
    other = _load("p18_other", other_path)
    alone = _load("p18_alone", alone_path)

    reloads: list[dict[Any, Reload]] = []
    ctx.on("hmr/reload", lambda m: reloads.append(m))
    try:
        fib = ctx.use(plugin.Comp)
        fib_other = ctx.use(other.Other)
        fib_alone = ctx.use(alone.Alone)
        await ctx.flush()
        assert fib.state == FiberState.ACTIVE
        assert fib.target is plugin.Comp
        assert plugin.PLUGIN_VALUE == 1

        shared_path.write_text("VALUE = 2\n")
        _bump(shared_path)
        reloaded = await hmr.reload_module("p18_shared", path=shared_path)
        assert reloaded is shared
        assert shared.VALUE == 2
        # The dependent reloaded after its dependency: fresh `from` binding.
        assert plugin.PLUGIN_VALUE == 2
        assert fib.target is plugin.Comp
        assert fib.state == FiberState.ACTIVE
        # The `import shared` dependent sees the fresh value too.
        assert fib_other.target is other.Other
        assert other.Other is not None
        # An unrelated component is untouched and not in the reload map.
        assert fib_alone.target is alone.Alone
        assert fib_alone.state == FiberState.ACTIVE
        # One reload event, keyed by the reloaded components.
        assert len(reloads) == 1
        assert plugin.Comp in reloads[0]
        assert other.Other in reloads[0]
        assert alone.Alone not in reloads[0]
        assert reloads[0][plugin.Comp].filename == plugin_path.resolve().as_uri()
        assert reloads[0][plugin.Comp].runtime is not None
    finally:
        _pop("p18_shared", "p18_plugin", "p18_other", "p18_alone")


@pytest.mark.asyncio
async def test_hmr_cyclic_imports_reload_without_hang(tmp_path: Path):
    a_path = _write(
        tmp_path,
        "p18_cyc_a.py",
        "import p18_cyc_b as b\nVALUE = 1\nA_VAL = b.VALUE + 1\n",
    )
    b_path = _write(tmp_path, "p18_cyc_b.py", "import p18_cyc_a as a\nVALUE = 10\n")
    ctx = Context()
    hmr = HmrService(ctx, {"root": tmp_path})
    sys.path.insert(0, str(tmp_path))
    try:
        a_mod = _load("p18_cyc_a", a_path)
        b_mod = _load("p18_cyc_b", b_path)
    finally:
        sys.path.remove(str(tmp_path))
    assert a_mod.A_VAL == 11
    reloads: list[dict[Any, Reload]] = []
    ctx.on("hmr/reload", lambda m: reloads.append(m))
    try:
        a_path.write_text("import p18_cyc_b as b\nVALUE = 2\nA_VAL = b.VALUE + 1\n")
        _bump(a_path)
        reloaded = await hmr.reload_module("p18_cyc_a", path=a_path)
        assert reloaded is a_mod
        assert a_mod.VALUE == 2
        assert b_mod.VALUE == 10
        assert len(reloads) == 1
    finally:
        _pop("p18_cyc_a", "p18_cyc_b")


# --- acceptance 2: failed reload rolls back ----------------------------------


@pytest.mark.asyncio
async def test_hmr_failed_reload_rolls_back(tmp_path: Path):
    shared_path = _write(tmp_path, "p18_rb_shared.py", "VALUE = 1\n")
    plugin_path = _write(
        tmp_path,
        "p18_rb_plugin.py",
        "from p18_rb_shared import VALUE\n"
        "class Comp:\n"
        "    version = VALUE\n"
        "    def __init__(self, ctx):\n"
        "        self.v = VALUE\n",
    )
    _write(tmp_path, "p18_rb_brand_new.py", "MARKER = True\n")
    ctx = Context()
    hmr = HmrService(ctx, {"root": tmp_path})
    shared = _load("p18_rb_shared", shared_path)
    plugin = _load("p18_rb_plugin", plugin_path)
    reloads: list[dict[Any, Reload]] = []
    ctx.on("hmr/reload", lambda m: reloads.append(m))
    sys.path.insert(0, str(tmp_path))
    try:
        fib = ctx.use(plugin.Comp)
        await ctx.flush()
        old_target = fib.target
        assert old_target.version == 1

        # Runtime failure mid-reload, after importing a brand-new module.
        shared_path.write_text(
            "import p18_rb_brand_new\nraise RuntimeError('boom')\nVALUE = 2\n"
        )
        _bump(shared_path)
        assert await hmr.reload_module("p18_rb_shared", path=shared_path) is None
        # Old code restored, fibers intact, no reload event.
        assert shared.VALUE == 1
        assert "VALUE" in shared.__dict__ and shared.__dict__["VALUE"] == 1
        assert "p18_rb_brand_new" not in sys.modules
        assert fib.target is old_target
        assert fib.state == FiberState.ACTIVE
        assert reloads == []
        # A retry after fixing the file succeeds.
        shared_path.write_text("VALUE = 3\n")
        _bump(shared_path)
        assert await hmr.reload_module("p18_rb_shared", path=shared_path) is shared
        assert shared.VALUE == 3
        assert plugin.Comp.version == 3
    finally:
        sys.path.remove(str(tmp_path))
        _pop("p18_rb_shared", "p18_rb_plugin", "p18_rb_brand_new")


@pytest.mark.asyncio
async def test_hmr_rollback_keeps_in_place_mutations(tmp_path: Path):
    """Rollback restores bindings, not in-place mutations (documented boundary).

    The snapshot holds the same mutable objects, so an in-place append
    made by the new top-level code survives a rollback. Rebinding the
    name first is shielded (the append then hits the new list).
    """
    mod_path = _write(tmp_path, "p18_shallow_rb.py", "ITEMS = []\nVALUE = 1\n")
    ctx = Context()
    hmr = HmrService(ctx, {"root": tmp_path})
    mod = _load("p18_shallow_rb", mod_path)
    try:
        items = mod.ITEMS
        mod_path.write_text("ITEMS.append('partial')\nraise RuntimeError('boom')\n")
        _bump(mod_path)
        assert await hmr.reload_module("p18_shallow_rb", path=mod_path) is None
        # The re-bound name is restored, but the in-place append on the
        # same list object survives the rollback.
        assert mod.VALUE == 1
        assert mod.ITEMS is items
        assert mod.ITEMS == ["partial"]
    finally:
        _pop("p18_shallow_rb")


@pytest.mark.asyncio
async def test_hmr_reload_same_second_same_size_recompiles(tmp_path: Path):
    """Two same-size edits inside one second still reload fresh code.

    reload reuses __pycache__ bytecode when the source mtime (integer
    seconds) and size match, so the reload deletes the cached bytecode
    first. The mtime is pinned with utime so the test does not depend
    on wall-clock timing: the second write keeps the same integer
    second (stale bytecode would apply) but a different float mtime
    (so the stash still counts as fresh).
    """
    mod_path = _write(tmp_path, "p18_stale_pyc.py", "VALUE = 1\n")
    ctx = Context()
    hmr = HmrService(ctx, {"root": tmp_path})
    mod = _load("p18_stale_pyc", mod_path)
    try:
        cache = Path(importlib.util.cache_from_source(str(mod_path)))
        assert cache.exists()  # the initial load wrote bytecode
        first_mtime = mod_path.stat().st_mtime
        # Pin the mtime to a base with a small fraction, so +0.5 s
        # stays inside the same integer second.
        base = int(first_mtime) + 0.1
        os.utime(mod_path, (base, base))
        mod_path.write_text("VALUE = 2\n")
        # Same integer second and same size (stale bytecode would
        # apply), but a different float mtime so the change is fresh.
        os.utime(mod_path, (base, base + 0.5))
        assert int(mod_path.stat().st_mtime) == int(base)
        assert mod_path.stat().st_size == len("VALUE = 2\n")
        assert await hmr.reload_module("p18_stale_pyc", path=mod_path) is mod
        assert mod.VALUE == 2
    finally:
        _pop("p18_stale_pyc")


@pytest.mark.asyncio
async def test_hmr_reload_error_handling(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
):
    ctx = Context()
    hmr = HmrService(ctx, {"root": tmp_path})

    # Non-existent module
    assert await hmr.reload_module("non_existent_module_xyz") is None

    # Module with path=None and no __file__
    synth_mod = ModuleType("synthetic_mod")
    sys.modules["synthetic_mod"] = synth_mod
    try:
        assert (
            await hmr.reload_module("synthetic_mod", mod=synth_mod, path=None) is None
        )
    finally:
        sys.modules.pop("synthetic_mod", None)

    # Syntax error during reload: code frame in the log, module untouched.
    mod_file = tmp_path / "broken_component.py"
    mod_file.write_text("VALID = 1\n")

    mod_name = "broken_component"
    mod = _load(mod_name, mod_file)
    try:
        mod_file.write_text("class Broken:\n  syntax error here :(\n")
        _bump(mod_file)
        with caplog.at_level(logging.WARNING, logger="anima.hmr"):
            res = await hmr.reload_module(mod_name, mod=mod, path=mod_file)
        assert res is None
        assert mod.VALID == 1
        assert "File:" in caplog.text
        assert str(mod_file) in caplog.text
        assert "^" in caplog.text
    finally:
        _pop(mod_name)


def test_handle_error_variants(tmp_path: Path, caplog: pytest.LogCaptureFixture):
    # Non-syntax error: plain warning, no frame.
    with caplog.at_level(logging.WARNING, logger="anima.hmr"):
        handle_error(RuntimeError("plain boom"))
    assert "plain boom" in caplog.text
    assert "File:" not in caplog.text

    # SyntaxError without a location: warning, no frame.
    with caplog.at_level(logging.WARNING, logger="anima.hmr"):
        handle_error(SyntaxError("no location"))
    assert "no location" in caplog.text

    # SyntaxError naming an unreadable file: warning, no frame, no raise.
    err = SyntaxError("bad", ("/nonexistent/p18_missing.py", 3, 5, "x = (\n"))
    with caplog.at_level(logging.WARNING, logger="anima.hmr"):
        handle_error(err)
    assert "bad" in caplog.text

    # SyntaxError with a location and no column: frame without a pointer.
    src = tmp_path / "p18_frame.py"
    src.write_text("a = 1\nb = 2\nc = 3\n")
    err = SyntaxError("oops", (str(src), 2, None, "b = 2\n"))
    with caplog.at_level(logging.WARNING, logger="anima.hmr"):
        handle_error(err)
    assert "File:" in caplog.text
    assert "b = 2" in caplog.text


# --- acceptance 3: externals trigger the full-reload hook --------------------


@pytest.mark.asyncio
async def test_hmr_external_change_triggers_full_reload_hook(
    tmp_path: Path, tmp_path_factory: pytest.TempPathFactory
):
    outside = tmp_path_factory.mktemp("outside")
    ext_path = _write(outside, "p18_ext.py", "VALUE = 1\n")
    ctx = Context()
    hooks: list[str] = []
    hmr = HmrService(
        ctx, {"root": tmp_path, "full_reload": lambda url: hooks.append(url)}
    )
    ext_mod = _load("p18_ext", ext_path)
    reloads: list[dict[Any, Reload]] = []
    ctx.on("hmr/reload", lambda m: reloads.append(m))
    try:
        ext_path.write_text("VALUE = 2\n")
        _bump(ext_path)
        await hmr._on_file_change("change", ext_path)
        assert hooks == [ext_path.resolve().as_uri()]
        # Nothing reloaded, module untouched.
        assert ext_mod.VALUE == 1
        assert reloads == []
    finally:
        _pop("p18_ext")


@pytest.mark.asyncio
async def test_hmr_external_change_without_hook_warns(
    tmp_path: Path,
    tmp_path_factory: pytest.TempPathFactory,
    caplog: pytest.LogCaptureFixture,
):
    outside = tmp_path_factory.mktemp("outside2")
    ext_path = _write(outside, "p18_ext2.py", "VALUE = 1\n")
    ctx = Context()
    hmr = HmrService(ctx, {"root": tmp_path})
    _load("p18_ext2", ext_path)
    try:
        with caplog.at_level(logging.WARNING, logger="anima.hmr"):
            await hmr._on_file_change("change", ext_path)
        assert "full reload" in caplog.text
    finally:
        _pop("p18_ext2")


@pytest.mark.asyncio
async def test_hmr_external_change_async_hook(
    tmp_path: Path, tmp_path_factory: pytest.TempPathFactory
):
    outside = tmp_path_factory.mktemp("outside3")
    ext_path = _write(outside, "p18_ext3.py", "VALUE = 1\n")
    ctx = Context()
    hooks: list[str] = []

    async def _hook(url: str) -> None:
        hooks.append(url)

    hmr = HmrService(ctx, {"root": tmp_path, "full_reload": _hook})
    _load("p18_ext3", ext_path)
    try:
        await hmr._on_file_change("change", ext_path)
        assert hooks == [ext_path.resolve().as_uri()]
    finally:
        _pop("p18_ext3")


@pytest.mark.asyncio
async def test_hmr_explicit_externals_config(tmp_path: Path):
    # A module under root can still be declared external by name, by bare
    # filename (resolved against root), or by absolute path.
    mod_path = _write(tmp_path, "p18_decl_ext.py", "VALUE = 1\n")
    bare_path = _write(tmp_path, "p18_bare_ext.py", "VALUE = 1\n")
    abs_path = _write(tmp_path, "p18_abs_ext.py", "VALUE = 1\n")
    ctx = Context()
    hooks: list[str] = []
    hmr = HmrService(
        ctx,
        {
            "root": tmp_path,
            "externals": ["p18_decl_ext", "p18_bare_ext.py", str(abs_path)],
            "full_reload": lambda url: hooks.append(url),
        },
    )
    mod = _load("p18_decl_ext", mod_path)
    bare = _load("p18_bare_ext", bare_path)
    absolute = _load("p18_abs_ext", abs_path)
    try:
        await hmr._on_file_change("change", mod_path)
        await hmr._on_file_change("change", bare_path)
        await hmr._on_file_change("change", abs_path)
        assert hooks == [
            mod_path.resolve().as_uri(),
            bare_path.resolve().as_uri(),
            abs_path.resolve().as_uri(),
        ]
        assert mod.VALUE == 1
        assert bare.VALUE == 1
        assert absolute.VALUE == 1
    finally:
        _pop("p18_decl_ext", "p18_bare_ext", "p18_abs_ext")


@pytest.mark.asyncio
async def test_hmr_external_by_name_unloaded_fires_hook(tmp_path: Path):
    # A module declared external by name fires the hook even when it was
    # never imported: the candidate name is derived from the path, not
    # from a loaded sys.modules entry.
    mod_path = _write(tmp_path, "p18_unloaded_ext.py", "VALUE = 1\n")
    ctx = Context()
    hooks: list[str] = []
    hmr = HmrService(
        ctx,
        {
            "root": tmp_path,
            "externals": ["p18_unloaded_ext"],
            "full_reload": lambda url: hooks.append(url),
        },
    )
    assert "p18_unloaded_ext" not in sys.modules
    await hmr._on_file_change("change", mod_path)
    assert hooks == [mod_path.resolve().as_uri()]


# --- acceptance 4: debounce coalesces rapid changes --------------------------


@pytest.mark.asyncio
async def test_hmr_debounce_coalesces_rapid_changes(tmp_path: Path):
    shared_path = _write(tmp_path, "p18_db_shared.py", "VALUE = 1\n")
    plugin_path = _write(
        tmp_path,
        "p18_db_plugin.py",
        "from p18_db_shared import VALUE\n"
        "class Comp:\n"
        "    version = VALUE\n"
        "    def __init__(self, ctx):\n"
        "        self.v = VALUE\n",
    )
    ctx = Context()
    hmr = HmrService(ctx, {"root": tmp_path, "debounce": 50})
    shared = _load("p18_db_shared", shared_path)
    plugin = _load("p18_db_plugin", plugin_path)
    reloads: list[dict[Any, Reload]] = []
    ctx.on("hmr/reload", lambda m: reloads.append(m))
    try:
        fib = ctx.use(plugin.Comp)
        await ctx.flush()
        # Three rapid changes inside one debounce window.
        for value in (2, 3, 4):
            shared_path.write_text(f"VALUE = {value}\n")
            _bump(shared_path)
            await hmr._on_file_change("change", shared_path)
        await asyncio.sleep(0.5)
        assert len(reloads) == 1
        assert shared.VALUE == 4
        assert plugin.Comp.version == 4
        assert fib.target is plugin.Comp
    finally:
        _pop("p18_db_shared", "p18_db_plugin")


@pytest.mark.asyncio
async def test_hmr_stop_cancels_pending_debounce(tmp_path: Path):
    shared_path = _write(tmp_path, "p18_stop_shared.py", "VALUE = 1\n")
    ctx = Context()
    ctx.use(WatcherService)
    ctx.use(HmrService, root=tmp_path, interval=0.01, debounce=200)
    await ctx.flush()
    hmr: HmrService = ctx.hmr
    shared = _load("p18_stop_shared", shared_path)
    reloads: list[dict[Any, Reload]] = []
    ctx.on("hmr/reload", lambda m: reloads.append(m))
    try:
        shared_path.write_text("VALUE = 2\n")
        _bump(shared_path)
        await hmr._on_file_change("change", shared_path)
        hmr.stop()
        await asyncio.sleep(0.5)
        assert reloads == []
        assert shared.VALUE == 1
    finally:
        _pop("p18_stop_shared")


# --- acceptance 5: config-file changes refresh the include tree --------------


def _inc_module() -> tuple[ModuleType, list[Any]]:
    calls: list[Any] = []

    def comp(ctx: Context, config: Any = None) -> None:
        calls.append(dict(config) if isinstance(config, dict) else config)

    module = ModuleType("hmr_inc_test_mod")
    module.comp = comp  # type: ignore[attr-defined]
    module.calls = calls  # type: ignore[attr-defined]
    sys.modules["hmr_inc_test_mod"] = module
    return module, calls


@pytest.mark.asyncio
async def test_hmr_config_change_refreshes_include(tmp_path: Path):
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text("- name: hmr_inc_test_mod:comp\n  id: e0\n  config:\n    level: 1\n")
    _module, calls = _inc_module()
    ctx = Context()
    loader = LoaderService(ctx)
    inc_id = await loader.create({"name": "include", "config": {"path": str(cfg)}})
    await loader.await_all()
    await ctx.flush()
    entry = loader.resolve(inc_id)
    inc = entry.ctx.get("include", strict=False)
    assert calls and calls[-1] == {"level": 1}

    hmr = HmrService(ctx, {"root": tmp_path})
    changes: list[str] = []
    reloads: list[dict[Any, Reload]] = []
    ctx.on("hmr/change", lambda url: changes.append(url))
    ctx.on("hmr/reload", lambda m: reloads.append(m))
    try:
        cfg.write_text(
            "- name: hmr_inc_test_mod:comp\n  id: e0\n  config:\n    level: 2\n"
        )
        await hmr._on_file_change("change", cfg)
        await inc.flush()
        await ctx.flush()
        assert calls[-1] == {"level": 2}
        assert changes == []
        assert reloads == []
    finally:
        _pop("hmr_inc_test_mod")


@pytest.mark.asyncio
async def test_hmr_config_change_no_match_emits_change(tmp_path: Path):
    cfg = tmp_path / "other.yaml"
    cfg.write_text("- name: hmr_inc_test_mod:comp\n  id: e0\n")
    ctx = Context()
    loader = LoaderService(ctx)
    # An include backing a different file: this change matches nothing.
    other_cfg = tmp_path / "cfg.yaml"
    other_cfg.write_text("- name: hmr_inc_test_mod:comp\n  id: e0\n")
    _inc_module()
    inc_id = await loader.create(
        {"name": "include", "config": {"path": str(other_cfg)}}
    )
    await loader.await_all()
    await ctx.flush()
    entry = loader.resolve(inc_id)
    assert entry.ctx.get("include", strict=False) is not None

    hmr = HmrService(ctx, {"root": tmp_path})
    changes: list[str] = []
    ctx.on("hmr/change", lambda url: changes.append(url))
    try:
        await hmr._on_file_change("change", cfg)
        assert changes == [cfg.resolve().as_uri()]
    finally:
        _pop("hmr_inc_test_mod")


@pytest.mark.asyncio
async def test_hmr_config_change_without_loader_emits_change(tmp_path: Path):
    cfg = tmp_path / "lonely.yaml"
    cfg.write_text("- name: x\n")
    ctx = Context()
    hmr = HmrService(ctx, {"root": tmp_path})
    changes: list[str] = []
    ctx.on("hmr/change", lambda url: changes.append(url))
    await hmr._on_file_change("change", cfg)
    assert changes == [cfg.resolve().as_uri()]


# --- watcher routing / change events ------------------------------------------


@pytest.mark.asyncio
async def test_hmr_on_file_change_filtering(tmp_path: Path):
    ctx = Context()
    hmr = HmrService(ctx, {"root": tmp_path})
    changes: list[str] = []
    ctx.on("hmr/change", lambda url: changes.append(url))

    # Action not change or add: ignored entirely.
    await hmr._on_file_change("unlink", tmp_path / "foo.py")
    assert changes == []

    # Unresolvable path: ignored entirely.
    with patch.object(Path, "resolve", side_effect=OSError("disk error")):
        await hmr._on_file_change("change", tmp_path / "foo.py")
    assert changes == []

    # Untracked non-Python file: change event, no reload.
    await hmr._on_file_change("change", tmp_path / "foo.txt")
    assert changes == [(tmp_path / "foo.txt").resolve().as_uri()]

    # Untracked Python file: change event, no reload.
    await hmr._on_file_change("change", tmp_path / "untracked.py")
    assert changes == [
        (tmp_path / "foo.txt").resolve().as_uri(),
        (tmp_path / "untracked.py").resolve().as_uri(),
    ]

    # Module successfully matched and reloaded via _on_file_change
    valid_file = tmp_path / "valid_change.py"
    valid_file.write_text("X = 1\n")
    mod_name = "valid_change"
    spec = importlib.util.spec_from_file_location(mod_name, str(valid_file))
    assert spec is not None
    vmod = importlib.util.module_from_spec(spec)
    vmod.__spec__ = spec
    sys.modules[mod_name] = vmod
    assert spec.loader is not None
    spec.loader.exec_module(vmod)

    reloads: list[dict[Any, Reload]] = []
    ctx.on("hmr/reload", lambda m: reloads.append(m))
    try:
        valid_file.write_text("X = 2\n")
        _bump(valid_file)
        await hmr._on_file_change("change", valid_file)
        # Debounced: nothing yet.
        assert reloads == []
        await asyncio.sleep(0.3)
        assert len(reloads) == 1
        assert vmod.X == 2
    finally:
        sys.modules.pop(mod_name, None)


@pytest.mark.asyncio
async def test_hmr_reload_class_component(tmp_path: Path):
    # Write version 1 before the watcher starts, so its initial scan
    # records it silently (ignore_initial) and only the version 2 change
    # triggers a reload. Otherwise a watcher reload of version 1 can race
    # the explicit reload and emit a second lifecycle cycle.
    mod_file = tmp_path / "sample_component.py"
    mod_file.write_text(
        "class MyComponent:\n"
        "    version = 1\n"
        "    def __init__(self, ctx):\n"
        "        self.v = 1\n"
    )

    ctx = Context()
    ctx.use(WatcherService)
    ctx.use(HmrService, root=tmp_path, interval=0.01)
    await ctx.flush()

    hmr: HmrService = ctx.hmr

    mod_name = "sample_component"
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
        # Mount component
        fib = ctx.use(mod.MyComponent)
        await ctx.flush()
        assert fib.state == FiberState.ACTIVE

        # Update file on disk
        mod_file.write_text(
            "class MyComponent:\n"
            "    version = 2\n"
            "    def __init__(self, ctx):\n"
            "        self.v = 2\n"
        )
        _bump(mod_file)

        reloaded = await hmr.reload_module(mod_name, path=mod_file)
        assert reloaded is not None
        assert reloaded.MyComponent.version == 2
        assert fib.target.version == 2

        assert len(reloads) == 1
        assert mod.MyComponent in reloads[0]
        assert reloads[0][mod.MyComponent].filename == mod_file.resolve().as_uri()

        # Reload with path=None and directory already on sys.path
        sys.path.insert(0, str(tmp_path))
        try:
            reloaded2 = await hmr.reload_module(mod_name, path=None)
            assert reloaded2 is not None
        finally:
            if str(tmp_path) in sys.path:
                sys.path.remove(str(tmp_path))

    finally:
        sys.modules.pop(mod_name, None)


@pytest.mark.asyncio
async def test_hmr_reload_concurrent_deduped(tmp_path: Path):
    # Two reloads of the same module racing each other (e.g. a watcher
    # event firing while an explicit reload is in flight) share one
    # in-flight reload: the reload event fires exactly once.
    ctx = Context()
    hmr = HmrService(ctx, {"root": tmp_path})

    mod_file = tmp_path / "dedup_component.py"
    mod_file.write_text("VALUE = 1\n")

    mod_name = "dedup_component"
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
        _bump(mod_file)

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


@pytest.mark.asyncio
async def test_hmr_reload_instance_and_function_components(tmp_path: Path):
    ctx = Context()
    ctx.use(WatcherService)
    hmr = HmrService(ctx, {"root": tmp_path})

    mod_file = tmp_path / "variants_component.py"
    mod_file.write_text(
        "class ServiceObj:\n"
        "    val = 1\n"
        "    def apply(self, ctx):\n"
        "        pass\n"
        "def func_component(ctx):\n"
        "    pass\n"
    )

    mod_name = "variants_component"
    spec = importlib.util.spec_from_file_location(mod_name, str(mod_file))
    assert spec is not None
    mod = importlib.util.module_from_spec(spec)
    mod.__spec__ = spec
    sys.modules[mod_name] = mod
    assert spec.loader is not None
    spec.loader.exec_module(mod)

    try:
        instance_target = mod.ServiceObj()
        fib_inst = ctx.use(instance_target)
        fib_func = ctx.use(mod.func_component)

        # A component with no __module__ is skipped by reconcile.
        class NoMod:
            __module__ = None  # type: ignore[assignment]

            def __init__(self, ctx: Context) -> None:
                pass

        fib_nomod = ctx.use(NoMod)

        # A partial component resolves to its functools module: skipped too.
        fib_partial = ctx.use(partial(mod.func_component))

        # Disposed fiber should be ignored
        fib_disposed = ctx.use(lambda c: None)
        await ctx.flush()
        await fib_disposed.dispose()

        # Modify fib_func target __name__ so it is not in reloaded module (triggers fallback restart)
        mod.func_component.__name__ = "non_existent_func"

        # Reload with target removed in new code (falls back to restart)
        mod_file.write_text(
            "class ServiceObj:\n"
            "    val = 2\n"
            "    def apply(self, ctx):\n"
            "        pass\n"
            "# func_component deleted\n"
        )

        reloaded = await hmr.reload_module(mod_name, mod=mod, path=mod_file)
        assert reloaded is not None
        assert fib_inst.state == FiberState.ACTIVE
        assert fib_func.state == FiberState.ACTIVE
        assert fib_nomod.state == FiberState.ACTIVE
        assert fib_nomod.target is NoMod
        assert fib_partial.state == FiberState.ACTIVE

    finally:
        sys.modules.pop(mod_name, None)


@pytest.mark.asyncio
async def test_hmr_reload_callable_instance_component(tmp_path: Path):
    # A callable instance has no __name__, so the reconcile cannot look
    # its attribute up on the reloaded module: it restarts in place with
    # the same instance. (The instance mounts via apply(), not __call__.)
    ctx = Context()
    hmr = HmrService(ctx, {"root": tmp_path})
    mod_path = _write(
        tmp_path,
        "p18_callable_inst.py",
        "class Handler:\n"
        "    def apply(self, ctx):\n"
        "        pass\n"
        "    def __call__(self, ctx):\n"
        "        pass\n",
    )
    mod = _load("p18_callable_inst", mod_path)
    reloads: list[dict] = []
    ctx.on("hmr/reload", lambda r: reloads.append(r))
    try:
        handler = mod.Handler()
        assert callable(handler)
        assert getattr(handler, "__name__", None) is None
        fib = ctx.use(handler)
        await ctx.flush()
        assert fib.state == FiberState.ACTIVE
        mod_path.write_text(
            "class Handler:\n"
            "    def apply(self, ctx):\n"
            "        pass\n"
            "    def __call__(self, ctx):\n"
            "        pass\n"
            "# v2\n"
        )
        reloaded = await hmr.reload_module("p18_callable_inst", path=mod_path)
        assert reloaded is not None
        assert fib.state == FiberState.ACTIVE
        assert fib.target is handler
        assert handler in reloads[0]
    finally:
        _pop("p18_callable_inst")


@pytest.mark.asyncio
async def test_hmr_reload_joins_stale_inflight_then_reloads(tmp_path: Path):
    """A reload racing a stale in-flight reload returns the current content.

    A slow effect disposer holds the first reload in flight; the file is
    updated while it runs. The second reload must not join the stale one
    (which would return version 1); it waits, then reloads version 2.

    A reschedule arriving after the debounce task has joined the drain
    must not cancel the drain: cancelling a task awaiting the drain
    cancels the drain itself (asyncio cancels the awaited task too).

    The service is constructed directly (no watcher): the race is between
    the debounce task and the drain, and watcher polls would only add
    timing noise to the handover wait below.
    """
    ctx = Context()
    hmr = HmrService(ctx, {"root": tmp_path})
    mod_file = tmp_path / "sample_component.py"
    mod_file.write_text(
        "import asyncio\n"
        "class MyComponent:\n"
        "    version = 1\n"
        "    def __init__(self, ctx):\n"
        "        self.v = 1\n"
        "        ctx.effect(lambda: self._slow_disposer)\n"
        "    async def _slow_disposer(self):\n"
        "        await asyncio.sleep(0.5)\n"
    )
    mod_name = "sample_component"
    spec = importlib.util.spec_from_file_location(mod_name, str(mod_file))
    assert spec is not None
    mod = importlib.util.module_from_spec(spec)
    mod.__spec__ = spec
    sys.modules[mod_name] = mod
    assert spec.loader is not None
    spec.loader.exec_module(mod)

    try:
        fib = ctx.use(mod.MyComponent)
        await ctx.flush()
        assert fib.state == FiberState.ACTIVE

        # Start a reload of version 1; the slow disposer holds it in flight.
        first = asyncio.create_task(hmr.reload_module(mod_name, path=mod_file))
        await asyncio.sleep(0.1)

        # Update the file while the first reload is still running.
        mod_file.write_text(
            "class MyComponent:\n"
            "    version = 2\n"
            "    def __init__(self, ctx):\n"
            "        self.v = 2\n"
        )
        _bump(mod_file)

        # Route the change through the watcher path: this schedules a
        # debounce task, which joins the in-flight drain once its 100 ms
        # window elapses. The drain runs ~1 s (two slow-disposer cycles),
        # so after 300 ms the task is joined but the drain is still
        # running; all three delays are loop timers, so they scale
        # together under load and the ordering holds.
        await hmr._on_file_change("change", mod_file)
        await asyncio.sleep(0.3)

        # Reschedule while the debounce task is joined to the drain.
        # Cancelling that task must not cancel the drain.
        await hmr._on_file_change("change", mod_file)

        reloaded = await hmr.reload_module(mod_name, path=mod_file)
        assert reloaded is not None
        assert reloaded.MyComponent.version == 2
        assert fib.target.version == 2
        assert await first is not None
    finally:
        sys.modules.pop(mod_name, None)


@pytest.mark.asyncio
async def test_hmr_reload_missing_file_returns_none(tmp_path: Path):
    """reload_module on a missing file returns None instead of raising."""
    ctx = Context()
    ctx.use(WatcherService)
    ctx.use(HmrService, root=tmp_path, interval=0.01)
    await ctx.flush()
    hmr: HmrService = ctx.hmr
    assert await hmr.reload_module("missing_mod", path=tmp_path / "nope.py") is None


@pytest.mark.asyncio
async def test_hmr_reload_broken_new_component_rolls_back_fiber(tmp_path: Path):
    """A new component that fails at init leaves the fiber on old code.

    The module reload itself succeeds, but the fiber re-mount fails, so
    the module state is rolled back and the fiber keeps its old target.
    """
    mod_file = tmp_path / "flaky_component.py"
    mod_file.write_text(
        "class Comp:\n"
        "    version = 1\n"
        "    def __init__(self, ctx):\n"
        "        self.v = 1\n"
    )
    ctx = Context()
    hmr = HmrService(ctx, {"root": tmp_path})
    mod = _load("flaky_component", mod_file)
    reloads: list[dict[Any, Reload]] = []
    ctx.on("hmr/reload", lambda m: reloads.append(m))
    try:
        fib = ctx.use(mod.Comp)
        await ctx.flush()
        old_target = fib.target
        assert old_target.version == 1

        mod_file.write_text(
            "class Comp:\n"
            "    version = 2\n"
            "    def __init__(self, ctx):\n"
            "        raise RuntimeError('init boom')\n"
        )
        _bump(mod_file)
        reloaded = await hmr.reload_module("flaky_component", path=mod_file)
        # The cycle failed, so reload_module reports None even though the
        # module itself reloaded: the reconcile failed and rolled back.
        assert reloaded is None
        # Module state rolled back to the old code.
        assert mod.Comp.version == 1
        # Fiber keeps its old target and stays active.
        assert fib.target is old_target
        assert fib.state == FiberState.ACTIVE
        assert reloads == []
    finally:
        _pop("flaky_component")


@pytest.mark.asyncio
async def test_hmr_failed_reload_restores_after_rollback(tmp_path: Path):
    """A failed reload restores fibers against rolled-back module state.

    The old component's re-mount reads module-level state, so the
    restore must run after the rollback: restoring first would re-run
    the old __init__ against the new (broken) state and fail the fiber
    spuriously even though the old code against the old state mounts
    fine.
    """
    shared_path = _write(tmp_path, "p18_rb2_shared.py", "VALUE = 1\n")
    plugin_path = _write(
        tmp_path,
        "p18_rb2_plugin.py",
        "from p18_rb2_shared import VALUE\n"
        "class Comp:\n"
        "    def __init__(self, ctx):\n"
        "        self.v = 10 // VALUE\n",
    )
    ctx = Context()
    hmr = HmrService(ctx, {"root": tmp_path})
    shared = _load("p18_rb2_shared", shared_path)
    plugin = _load("p18_rb2_plugin", plugin_path)
    sys.path.insert(0, str(tmp_path))
    try:
        fib = ctx.use(plugin.Comp)
        await ctx.flush()
        old_target = fib.target
        assert fib.state == FiberState.ACTIVE
        assert fib.error is None

        # New shared value breaks the old __init__ (10 // 0).
        shared_path.write_text("VALUE = 0\n")
        _bump(shared_path)
        assert await hmr.reload_module("p18_rb2_shared", path=shared_path) is None
        # Module state rolled back, and the fiber is intact: the restore
        # re-ran the old __init__ against VALUE == 1, not 0.
        assert shared.VALUE == 1
        assert plugin.VALUE == 1
        assert fib.target is old_target
        assert fib.state == FiberState.ACTIVE
        assert fib.error is None
        # A retry after fixing the file succeeds with the fresh value.
        shared_path.write_text("VALUE = 2\n")
        _bump(shared_path)
        assert await hmr.reload_module("p18_rb2_shared", path=shared_path) is shared
        assert shared.VALUE == 2
        assert plugin.VALUE == 2
        assert fib.state == FiberState.ACTIVE
        assert fib.target is not old_target
    finally:
        sys.path.remove(str(tmp_path))
        _pop("p18_rb2_shared", "p18_rb2_plugin")


@pytest.mark.asyncio
async def test_hmr_none_module_entry_restarts_fiber(tmp_path: Path):
    """A stashed module whose sys.modules entry became None is skipped.

    The reload loop cannot reload it, so the fiber restarts on its old
    target instead of crashing, and the cycle still emits hmr/reload.
    """
    mod_file = _write(
        tmp_path,
        "p18_none_entry.py",
        "class Comp:\n"
        "    version = 1\n"
        "    def __init__(self, ctx):\n"
        "        self.v = 1\n",
    )
    ctx = Context()
    hmr = HmrService(ctx, {"root": tmp_path, "debounce": 50})
    mod = _load("p18_none_entry", mod_file)
    reloads: list[dict[Any, Reload]] = []
    ctx.on("hmr/reload", lambda m: reloads.append(m))
    try:
        fib = ctx.use(mod.Comp)
        await ctx.flush()
        old_target = fib.target
        mod_file.write_text(
            "class Comp:\n"
            "    version = 2\n"
            "    def __init__(self, ctx):\n"
            "        self.v = 2\n"
        )
        _bump(mod_file)
        await hmr._on_file_change("change", mod_file)
        # The entry disappears after the change was stashed.
        sys.modules["p18_none_entry"] = None  # type: ignore[assignment]
        await asyncio.sleep(0.4)
        assert len(reloads) == 1
        assert fib.target is old_target
        assert fib.state == FiberState.ACTIVE
    finally:
        _pop("p18_none_entry")


@pytest.mark.asyncio
async def test_hmr_reload_path_cleanup_tolerates_removal(tmp_path: Path):
    """A reloaded module that removes its own dir from sys.path is fine.

    The cycle only removes directories it added when they are still
    present at cleanup time.
    """
    mod_file = _write(tmp_path, "p18_pathmod.py", "VALUE = 1\n")
    ctx = Context()
    hmr = HmrService(ctx, {"root": tmp_path})
    mod = _load("p18_pathmod", mod_file)
    assert str(tmp_path) not in sys.path
    try:
        mod_file.write_text("import sys\nsys.path.remove(sys.path[0])\nVALUE = 2\n")
        _bump(mod_file)
        reloaded = await hmr.reload_module("p18_pathmod", path=mod_file)
        assert reloaded is mod
        assert mod.VALUE == 2
        assert str(tmp_path) not in sys.path
    finally:
        _pop("p18_pathmod")
        if str(tmp_path) in sys.path:
            sys.path.remove(str(tmp_path))


@pytest.mark.asyncio
async def test_hmr_restore_failure_leaves_fiber_failed(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
):
    """When even restoring the old target fails, the failure is logged.

    The module state is still rolled back; the fiber keeps the old
    target and its FAILED state instead of crashing the cycle. The
    restore re-runs the old __init__ after the rollback, so its
    COUNT side effect lingers (2, not 1).
    """
    mod_file = _write(
        tmp_path,
        "p18_restore_fail.py",
        "COUNT = 0\n"
        "class Comp:\n"
        "    version = 1\n"
        "    def __init__(self, ctx):\n"
        "        import sys\n"
        "        mod = sys.modules[__name__]\n"
        "        mod.COUNT += 1\n"
        "        if mod.COUNT > 1:\n"
        "            raise RuntimeError('second init boom')\n"
        "        self.v = 1\n",
    )
    ctx = Context()
    hmr = HmrService(ctx, {"root": tmp_path})
    mod = _load("p18_restore_fail", mod_file)
    try:
        fib = ctx.use(mod.Comp)
        await ctx.flush()
        assert mod.COUNT == 1
        old_target = fib.target

        mod_file.write_text(
            "COUNT = 5\n"
            "class Comp:\n"
            "    version = 2\n"
            "    def __init__(self, ctx):\n"
            "        raise RuntimeError('init boom')\n"
        )
        _bump(mod_file)
        with caplog.at_level(logging.WARNING, logger="anima.hmr"):
            reloaded = await hmr.reload_module("p18_restore_fail", path=mod_file)
        assert reloaded is None
        # Module state rolled back to the old code.
        assert mod.Comp.version == 1
        # The restore re-ran the old __init__ after the rollback, so
        # its COUNT side effect lingers (the old order erased it with
        # the later rollback).
        assert mod.COUNT == 2
        # The restore assigned the old target back (replace() assigns
        # before restarting), but its restart failed too, so the fiber
        # keeps the old target and its FAILED state.
        assert fib.target is old_target
        assert fib.state == FiberState.FAILED
        assert "Failed to restore fiber" in caplog.text
    finally:
        _pop("p18_restore_fail")
