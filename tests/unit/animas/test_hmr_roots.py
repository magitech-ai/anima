"""Multi-root watching and ignore patterns for the HMR service."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from animas.core import Context
from animas.fs import FsService, LocalFs
from animas.hmr import HmrService


def test_root_configuration(tmp_path: Any) -> None:
    ctx = Context()
    service = HmrService(
        ctx,
        {
            "root": str(tmp_path),
            "roots": [".", "plugins"],
            "ignored": ["**/node_modules", "*.pyc"],
        },
    )
    assert service.root == tmp_path.resolve()
    assert service.roots == (tmp_path.resolve(), (tmp_path / "plugins").resolve())
    with pytest.raises(TypeError, match="roots must be a non-empty list"):
        HmrService(ctx, {"roots": []})
    with pytest.raises(TypeError, match="roots must be a non-empty list"):
        HmrService(ctx, {"roots": "plugins"})


def test_classification_uses_every_root_and_ignores(tmp_path: Any) -> None:
    ctx = Context()
    service = HmrService(
        ctx,
        {
            "root": str(tmp_path),
            "roots": [".", "plugins"],
            "ignored": ["**/node_modules", "*.pyc", "cache", "sub/inner.py"],
        },
    )
    inside = tmp_path / "mod.py"
    second = tmp_path / "plugins" / "mod.py"
    outside = tmp_path.parent / "other.py"
    assert service._is_external_path(inside.resolve()) is False
    assert service._is_external_path(second.resolve()) is False
    assert service._is_external_path(outside.resolve()) is True
    assert service._is_ignored(tmp_path / "mod.py") is False
    assert service._is_ignored(tmp_path / "mod.pyc") is True
    assert service._is_ignored(tmp_path / "node_modules" / "x.py") is True
    assert service._is_ignored(tmp_path / "cache" / "x.py") is True
    assert service._is_ignored(tmp_path / "sub" / "inner.py") is True
    assert service._is_ignored(outside) is False


@pytest.mark.asyncio
async def test_multi_root_watch_lifecycle(tmp_path: Any) -> None:
    ctx = Context()
    ctx.use(FsService)
    await ctx.flush()
    LocalFs(ctx)
    service = HmrService(
        ctx,
        {
            "root": str(tmp_path),
            "roots": [".", "plugins"],
            "ignored": ["*.pyc"],
        },
    )
    service.start()
    assert service._active is True
    assert len(service._disposers) == 2
    service.stop()
    assert service._active is False
    assert service._disposers == []

    from animas.fs import WatchOptions

    watch_cfg: dict[str, Any] = {
        "enabled": True,
        "files": ["."],
        "debounce": 0.0,
    }
    service._watch_config = watch_cfg
    service._start_fs_watch()
    assert len(service._fs_watches) == 2
    service._stop_fs_watch(service._fs_watches, None)
    assert service._fs_watches == []
    assert WatchOptions(filter=["."]) is not None


@pytest.mark.asyncio
async def test_watch_ignores_filtered_changes(tmp_path: Any) -> None:
    ctx = Context()
    ctx.use(FsService)
    await ctx.flush()
    LocalFs(ctx)
    service = HmrService(ctx, {"root": str(tmp_path), "ignored": ["*.pyc"]})
    service._watch_config = {
        "enabled": True,
        "files": ["."],
        "debounce": 0.0,
    }
    service._start_fs_watch()
    service._fs_watches[0].emit("change", (tmp_path / "skip.pyc").as_uri())
    await asyncio.sleep(0)
    assert service._stashed == {}
    service._stop_fs_watch(service._fs_watches, None)


def test_disjoint_roots_ignore_and_graph(tmp_path: Any) -> None:
    import sys
    from types import ModuleType

    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    (first / "pkg_a.py").write_text("VALUE = 1\n", encoding="utf-8")
    (second / "pkg_b.py").write_text("import pkg_a\n", encoding="utf-8")
    for name, path in (
        ("pkg_a", first / "pkg_a.py"),
        ("pkg_b", second / "pkg_b.py"),
    ):
        module = ModuleType(name)
        module.__file__ = str(path)
        sys.modules[name] = module

    ctx = Context()
    service = HmrService(
        ctx,
        {
            "root": str(first),
            "roots": [str(first), str(second)],
            "ignored": ["*.pyc", "cache"],
        },
    )
    try:
        assert service._is_ignored(second / "skip.pyc") is True
        assert service._is_ignored(second / "cache" / "mod.py") is True
        graph = service._build_graph()
        assert "pkg_a" in graph and "pkg_b" in graph
    finally:
        sys.modules.pop("pkg_a", None)
        sys.modules.pop("pkg_b", None)


@pytest.mark.asyncio
async def test_unloaded_file_in_secondary_root(tmp_path: Any) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    candidate = second / "unloaded.py"
    candidate.write_text("VALUE = 1\n", encoding="utf-8")
    ctx = Context()
    service = HmrService(ctx, {"root": str(first), "roots": [str(first), str(second)]})
    await service._on_file_change("change", candidate)
    assert service._stashed == {}


def test_resolve_errors_are_contained(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = Context()
    service = HmrService(ctx, {"root": str(tmp_path)})
    original = Path.resolve

    def always_fail(self: Path, *args: Any, **kwargs: Any) -> Path:
        raise OSError("gone")

    monkeypatch.setattr(Path, "resolve", always_fail)
    assert service._root_for(tmp_path / "x.py") is None

    calls = 0

    def second_fails(self: Path, *args: Any, **kwargs: Any) -> Path:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("gone")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", second_fails)
    assert service._is_ignored(tmp_path / "x.py") is False
