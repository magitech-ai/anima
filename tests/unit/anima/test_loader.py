import sys
import types
from pathlib import Path

import pytest
import yaml

from anima.core import Context
from anima.loader import load_profile, resolve_plugin_target


def test_resolve_plugin_target_colon_syntax():
    target = resolve_plugin_target("anima.events:EventBus")
    from anima.events import EventBus

    assert target is EventBus


def test_resolve_plugin_target_dot_syntax():
    target = resolve_plugin_target("anima.core.Context")
    from anima.core import Context as CoreContext

    assert target is CoreContext


def test_resolve_plugin_target_module_with_apply(monkeypatch):
    dummy_mod = types.ModuleType("dummy_plugin_mod")

    def apply_fn(ctx):
        return "applied"

    dummy_mod.apply = apply_fn
    monkeypatch.setitem(sys.modules, "dummy_plugin_mod", dummy_mod)

    target = resolve_plugin_target("dummy_plugin_mod")
    assert target is apply_fn


def test_resolve_plugin_target_module_without_apply():
    target = resolve_plugin_target("anima")
    import anima

    assert target is anima


@pytest.mark.asyncio
async def test_load_profile_from_list_and_dict_entries(monkeypatch):
    ctx = Context()
    loaded = []

    dummy_plugin = types.ModuleType("custom_agent_plugin")

    class SampleService:
        def __init__(self, c: Context, token: str = "default"):
            self.c = c
            self.token = token
            loaded.append(token)

    dummy_plugin.SampleService = SampleService
    monkeypatch.setitem(sys.modules, "custom_agent_plugin", dummy_plugin)

    entries = [
        # Non-str, non-dict entry (branch coverage)
        12345,
        # String entry
        "custom_agent_plugin:SampleService",
        # Dict entry without config
        {"name": "custom_agent_plugin:SampleService"},
        # Dict entry with config
        {
            "name": "custom_agent_plugin:SampleService",
            "config": {"token": "secret_xyz"},
        },
        # Dict entry without name (skipped)
        {"config": {"ignored": True}},
    ]

    load_profile(ctx, entries)
    await ctx.flush()

    assert "default" in loaded
    assert "secret_xyz" in loaded
    assert len(ctx.dump_tree()["fibers"]) == 3


@pytest.mark.asyncio
async def test_load_profile_from_yaml_file(tmp_path: Path, monkeypatch):
    ctx = Context()
    calls = []

    dummy_mod = types.ModuleType("profile_mod")

    def sample_plugin(c: Context, mode: str = "fast"):
        calls.append(mode)

    dummy_mod.sample_plugin = sample_plugin
    monkeypatch.setitem(sys.modules, "profile_mod", dummy_mod)

    yaml_data = [
        "profile_mod:sample_plugin",
        {"name": "profile_mod:sample_plugin", "config": {"mode": "turbo"}},
    ]

    yaml_file = tmp_path / "profile.yaml"
    yaml_file.write_text(yaml.dump(yaml_data), encoding="utf-8")

    # Load via Path object
    load_profile(ctx, yaml_file)
    await ctx.flush()

    # Load via string path
    ctx2 = Context()
    load_profile(ctx2, str(yaml_file))
    await ctx2.flush()

    assert calls == ["fast", "turbo", "fast", "turbo"]


def test_load_profile_missing_file():
    ctx = Context()
    with pytest.raises(FileNotFoundError, match="Profile config file not found"):
        load_profile(ctx, "/path/to/nonexistent/profile.yaml")


def test_load_profile_invalid_structure():
    ctx = Context()
    with pytest.raises(ValueError, match="must be a list of plugin entries"):
        load_profile(ctx, {"invalid": "object"})
