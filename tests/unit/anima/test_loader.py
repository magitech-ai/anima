import sys
import types
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from anima.core import Context
from anima.loader import (
    LoaderService,
    interpolate_env,
    load_profile,
    resolve_plugin_target,
    save_profile,
)


def test_resolve_plugin_target_colon_syntax():
    target = resolve_plugin_target("anima.events:EventBus")
    from anima.events import EventBus

    assert target is EventBus


def test_resolve_plugin_target_dot_syntax():
    target = resolve_plugin_target("anima.core.Context")
    from anima.core import Context as CoreContext

    assert target is CoreContext


def test_resolve_plugin_target_builtin_aliases():
    from anima.group import Group
    from anima.hmr import HmrService
    from anima.logger import LoggerService
    from anima.pkg import PkgService
    from anima.timer import TimerService
    from anima.watcher import WatcherService

    assert resolve_plugin_target("group") is Group
    assert resolve_plugin_target("logger") is LoggerService
    assert resolve_plugin_target("timer") is TimerService
    assert resolve_plugin_target("watcher") is WatcherService
    assert resolve_plugin_target("hmr") is HmrService
    assert resolve_plugin_target("pkg") is PkgService


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


@pytest.mark.asyncio
async def test_load_profile_from_list_and_dict_entries(monkeypatch):
    ctx = Context()
    loaded = []

    dummy_plugin = types.ModuleType("custom_agent_plugin")

    class SampleService:
        def __init__(self, c: Context, config: dict | None = None):
            self.c = c
            self.token = (config or {}).get("token", "default")
            loaded.append(self.token)

    dummy_plugin.SampleService = SampleService
    monkeypatch.setitem(sys.modules, "custom_agent_plugin", dummy_plugin)

    entries = [
        # Non-str, non-dict entry (skipped)
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

    loader = load_profile(ctx, entries)
    await ctx.flush()

    assert "default" in loaded
    assert "secret_xyz" in loaded
    assert len(loader.fibers) == 3


@pytest.mark.asyncio
async def test_load_profile_from_yaml_file(tmp_path: Path, monkeypatch):
    ctx = Context()
    calls = []

    dummy_mod = types.ModuleType("profile_mod")

    def sample_plugin(c: Context, config: dict | None = None):
        calls.append((config or {}).get("mode", "fast"))

    dummy_mod.sample_plugin = sample_plugin
    monkeypatch.setitem(sys.modules, "profile_mod", dummy_mod)

    yaml_data = [
        "profile_mod:sample_plugin",
        {"name": "profile_mod:sample_plugin", "config": {"mode": "turbo"}},
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
async def test_loader_service_isolation_intercept_and_groups():
    ctx = Context()
    loader = LoaderService(ctx)

    intercept_observed = []

    class MockInterceptedService:
        def __init__(self, c: Context):
            intercept_observed.append(c.resolve_intercept("mock_svc"))

    import sys

    dummy = types.ModuleType("intercept_mod")
    dummy.MockInterceptedService = MockInterceptedService
    sys.modules["intercept_mod"] = dummy

    try:
        profile = {
            "env": {"MODE": "special"},
            "plugins": [
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
                        {"id": "sub_a", "target": lambda c: None},
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
        assert "entry_isolated" in loader.fibers
        assert "my_group" in loader.fibers
        assert "disabled_entry" not in loader.fibers

        # Enable disabled entry
        await loader.enable("disabled_entry")
        assert "disabled_entry" in loader.fibers

        # Enable already enabled
        await loader.enable("disabled_entry")

        # Disable entry
        await loader.disable("disabled_entry")
        assert "disabled_entry" not in loader.fibers

        # Disable already disabled
        await loader.disable("disabled_entry")

        # Enable, disable, update nonexistent entry (loop exits)
        await loader.enable("nonexistent")
        await loader.disable("nonexistent")
        await loader.update("nonexistent", {})

        # Update disabled entry (not in loader.fibers)
        await loader.update("disabled_entry", {"opt": 2})

        # Update entry config
        await loader.update("my_group", {"entries": []})

    finally:
        sys.modules.pop("intercept_mod", None)


@pytest.mark.asyncio
async def test_loader_persistence_and_commit(tmp_path: Path):
    ctx = Context()
    save_file = tmp_path / "saved_profile.yaml"

    loader = LoaderService(ctx, filename=save_file)
    loader.load(
        [
            {"id": "p1", "name": "anima.events:EventBus", "config": {"opt": 1}},
        ]
    )
    await ctx.flush()

    # Manual save
    loader.save()
    assert save_file.exists()

    # Save to custom path
    alt_file = tmp_path / "alt_profile.yaml"
    loader.save(alt_file)
    assert alt_file.exists()

    # Save without filename raises
    loader_no_file = LoaderService(ctx)
    with pytest.raises(ValueError, match="No destination path specified"):
        loader_no_file.save()

    # Auto commit hook
    fib = loader.fibers["p1"]
    await fib.update({"opt": 999})
    re_read = yaml.safe_load(save_file.read_text(encoding="utf-8"))
    assert re_read[0]["config"] == {"opt": 999}

    # Auto commit error handling
    with patch.object(loader, "save", side_effect=OSError("disk error")):
        await fib.update({"opt": 1000})


def test_load_profile_error_handling(tmp_path: Path):
    ctx = Context()

    # Nonexistent file
    with pytest.raises(FileNotFoundError, match="Profile config file not found"):
        load_profile(ctx, tmp_path / "not_found.yaml")

    # Non list/dict raw data
    with pytest.raises(TypeError, match="must be a list or dict"):
        LoaderService(ctx).load(12345)  # type: ignore[arg-type]

    # Plugins not a list
    with pytest.raises(TypeError, match="plugins must resolve to a list"):
        LoaderService(ctx).load({"plugins": "not_a_list"})
