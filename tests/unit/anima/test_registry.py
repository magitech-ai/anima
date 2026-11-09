"""Unit tests for RegistryService."""

import pytest

from anima.core import Context, FiberState
from anima.registry import PluginRuntime, RegistryService


class _DummyPlugin:
    name = "dummy"
    Config = None

    def __init__(self, ctx, **kwargs):
        self.ctx = ctx

    def apply(self, ctx):
        pass


class _AnotherPlugin:
    name = "another"

    def __init__(self, ctx, **kwargs):
        self.ctx = ctx

    def apply(self, ctx):
        pass


class TestPluginRuntime:
    def test_plugin_runtime_creation(self):
        def cb():
            pass

        rt = PluginRuntime("test", cb, Config={"key": "val"})
        assert rt.name == "test"
        assert rt.callback is cb
        assert rt.Config == {"key": "val"}
        assert rt.fibers == []


class TestRegistryServiceCounter:
    def test_counter_increments(self):
        ctx = Context()
        reg = RegistryService(ctx)
        c1 = reg.counter
        c2 = reg.counter
        assert c2 == c1 + 1


class TestRegistryServiceSize:
    def test_size_empty(self):
        ctx = Context()
        reg = RegistryService(ctx)
        assert reg.size == 0

    def test_size_after_register(self):
        ctx = Context()
        reg = RegistryService(ctx)
        reg.plugin(_DummyPlugin)
        assert reg.size == 1


class TestRegistryServiceResolve:
    def test_resolve_callable(self):
        ctx = Context()
        reg = RegistryService(ctx)

        def my_func():
            pass

        assert reg.resolve(my_func) is my_func

    def test_resolve_object_with_apply(self):
        ctx = Context()
        reg = RegistryService(ctx)
        obj = _DummyPlugin(ctx)
        resolved = reg.resolve(obj)
        assert resolved is not None
        assert resolved.__self__ is obj
        assert resolved.__name__ == "apply"

    def test_resolve_neither(self):
        ctx = Context()
        reg = RegistryService(ctx)
        assert reg.resolve(42) is None


class TestRegistryServiceGetHas:
    def test_get_has_registered(self):
        ctx = Context()
        reg = RegistryService(ctx)
        reg.plugin(_DummyPlugin)
        assert reg.has(_DummyPlugin)
        rt = reg.get(_DummyPlugin)
        assert rt is not None
        assert rt.name == "dummy"

    def test_get_has_unregistered(self):
        ctx = Context()
        reg = RegistryService(ctx)
        assert not reg.has(_DummyPlugin)
        assert reg.get(_DummyPlugin) is None


class TestRegistryServicePluginMount:
    def test_plugin_mount_creates_fiber(self):
        ctx = Context()
        reg = RegistryService(ctx)
        fiber = reg.plugin(_DummyPlugin)
        from anima.core import Fiber

        assert isinstance(fiber, Fiber)
        assert fiber.state == FiberState.ACTIVE

    def test_plugin_mount_with_config(self):
        ctx = Context()
        reg = RegistryService(ctx)
        fiber = reg.plugin(_DummyPlugin, {"key": "val"})
        assert fiber.raw_config == {"key": "val"}

    def test_plugin_invalid_target(self):
        ctx = Context()
        reg = RegistryService(ctx)
        with pytest.raises(ValueError):
            reg.plugin(42)


class TestRegistryServiceReplace:
    def test_replace_reloads_fiber(self):
        ctx = Context()
        reg = RegistryService(ctx)
        fiber = reg.plugin(_DummyPlugin)

        class _DummyPluginV2:
            name = "dummy"

            def __init__(self, ctx, **kwargs):
                self.ctx = ctx

            def apply(self, ctx):
                pass

        reg.replace(_DummyPlugin, _DummyPluginV2)
        assert fiber.state in (FiberState.ACTIVE, FiberState.FAILED)

    def test_replace_unknown_plugin(self):
        ctx = Context()
        reg = RegistryService(ctx)
        reg.plugin(_DummyPlugin)
        with pytest.raises(ValueError):
            reg.replace(_DummyPlugin, 42)  # 42 is not a valid plugin


class TestRegistryServiceIteration:
    def test_keys_values_entries_for_each(self):
        ctx = Context()
        reg = RegistryService(ctx)
        reg.plugin(_DummyPlugin)
        reg.plugin(_AnotherPlugin)

        keys = list(reg.keys())
        assert len(keys) == 2

        values = list(reg.values())
        assert len(values) == 2

        entries = list(reg.entries())
        assert len(entries) == 2

        collected = []
        reg.for_each(lambda rt, key: collected.append((rt, key)))
        assert len(collected) == 2


class TestRegistryServiceInject:
    def test_inject_mounts_plugin(self):
        ctx = Context()
        reg = RegistryService(ctx)

        def my_callback(ctx):
            pass

        fiber = reg.inject(["nonexistent"], my_callback)
        assert isinstance(fiber, FiberState) is False
        from anima.core import Fiber

        assert isinstance(fiber, Fiber)


class TestRegistryServiceUnregister:
    def test_unregister(self):
        ctx = Context()
        reg = RegistryService(ctx)
        reg.plugin(_DummyPlugin)
        assert reg.has(_DummyPlugin)

        rt = reg.get(_DummyPlugin)
        reg._unregister(rt)
        assert not reg.has(_DummyPlugin)


class TestRegistryServiceResolveNone:
    def test_get_resolve_none(self):
        """get with plugin that resolves to None returns None."""
        ctx = Context()
        reg = RegistryService(ctx)
        assert reg.get(42) is None

    def test_has_resolve_none(self):
        """has with plugin that resolves to None returns False."""
        ctx = Context()
        reg = RegistryService(ctx)
        assert not reg.has(42)


class TestRegistryServiceReplaceOldInvalid:
    def test_replace_old_invalid(self):
        """replace with old plugin that resolves to None raises ValueError."""
        ctx = Context()
        reg = RegistryService(ctx)
        with pytest.raises(ValueError):
            reg.replace(42, _DummyPlugin)


class TestRegistryServiceReplaceSameKey:
    def test_replace_same_key(self):
        """replace when old_key is key (same plugin)."""
        ctx = Context()
        reg = RegistryService(ctx)

        class PluginA:
            name = "plugin_a"

            def __init__(self, ctx):
                self.ctx = ctx

            def apply(self, ctx):
                pass

        reg.plugin(PluginA)
        result = reg.replace(PluginA, PluginA)
        assert isinstance(result, list)


class TestRegistryServiceReplaceTargetExists:
    def test_replace_target_exists(self):
        """replace when target runtime already exists."""
        ctx = Context()
        reg = RegistryService(ctx)

        class PluginA:
            name = "plugin_a"

            def __init__(self, ctx):
                self.ctx = ctx

            def apply(self, ctx):
                pass

        class PluginB:
            name = "plugin_b"

            def __init__(self, ctx):
                self.ctx = ctx

            def apply(self, ctx):
                pass

        reg.plugin(PluginA)
        reg.plugin(PluginB)
        # Replace PluginA with PluginB - target (PluginB runtime) already exists
        result = reg.replace(PluginA, PluginB)
        assert isinstance(result, list)


class TestRegistryServiceInjectList:
    def test_inject_with_list_deps(self):
        ctx = Context()
        reg = RegistryService(ctx)

        def my_callback(ctx):
            pass

        fiber = reg.inject(["server", "timer"], my_callback)
        from anima.core import Fiber

        assert isinstance(fiber, Fiber)


class TestRegistryServiceResolveExceptions:
    def test_resolve_exception(self):
        """resolve catches exceptions from callable/getattr."""
        ctx = Context()
        reg = RegistryService(ctx)

        class Broken:
            __name__ = "broken"

            @property
            def apply(self):
                raise RuntimeError("broken")

        obj = Broken()
        assert reg.resolve(obj) is None


class TestRegistryServiceAliasChain:
    def test_forward_with_alias(self):
        ctx = Context()
        reg = RegistryService(ctx)

        class PluginA:
            name = "plugin_a"

            def __init__(self, ctx):
                self.ctx = ctx

            def apply(self, ctx):
                pass

        class PluginB:
            name = "plugin_b"

            def __init__(self, ctx):
                self.ctx = ctx

            def apply(self, ctx):
                pass

        reg.plugin(PluginA)
        # Create alias: PluginB -> PluginA
        reg._aliases[id(PluginB)] = PluginA

        # Resolving PluginB should follow alias to PluginA
        result = reg._forward(PluginB)
        assert result is not None
        key, plugin = result
        assert key is PluginA
        assert plugin is PluginA

    def test_get_has_with_alias(self):
        ctx = Context()
        reg = RegistryService(ctx)

        class PluginA:
            name = "plugin_a"

            def __init__(self, ctx):
                self.ctx = ctx

            def apply(self, ctx):
                pass

        class PluginB:
            name = "plugin_b"

            def __init__(self, ctx):
                self.ctx = ctx

            def apply(self, ctx):
                pass

        reg.plugin(PluginA)
        reg._aliases[id(PluginB)] = PluginA

        assert reg.has(PluginB)
        rt = reg.get(PluginB)
        assert rt is not None
        assert rt.name == "plugin_a"


class TestRegistryServiceCreateNameApply:
    def test_create_with_name_apply(self):
        ctx = Context()
        reg = RegistryService(ctx)

        class PluginApply:
            name = "apply"

            def __init__(self, ctx):
                self.ctx = ctx

            def apply(self, ctx):
                pass

        reg.plugin(PluginApply)
        rt = reg.get(PluginApply)
        assert rt.name is None  # name "apply" is converted to None


class TestRegistryServiceReplaceEmpty:
    def test_replace_returns_fibers(self):
        ctx = Context()
        reg = RegistryService(ctx)

        class PluginA:
            name = "plugin_a"

            def __init__(self, ctx):
                self.ctx = ctx

            def apply(self, ctx):
                pass

        class PluginB:
            name = "plugin_b"

            def __init__(self, ctx):
                self.ctx = ctx

            def apply(self, ctx):
                pass

        fiber = reg.plugin(PluginA)
        result = reg.replace(PluginA, PluginB)
        assert result == [fiber]


class TestRegistryServiceInjectDict:
    def test_inject_with_dict_deps(self):
        ctx = Context()
        reg = RegistryService(ctx)

        def my_callback(ctx):
            pass

        fiber = reg.inject({"server": None}, my_callback)
        from anima.core import Fiber

        assert isinstance(fiber, Fiber)


class TestRegistryServiceCreateAlias:
    def test_create_with_alias(self):
        ctx = Context()
        reg = RegistryService(ctx)

        class PluginA:
            name = "plugin_a"

            def __init__(self, ctx):
                self.ctx = ctx

            def apply(self, ctx):
                pass

        reg.plugin(PluginA)
        # Setting up alias directly
        reg._aliases[id(PluginA)] = PluginA
        # Forward should detect cycle and return None
        result = reg._forward(PluginA)
        assert result is None


class TestRegistryServiceReplaceEdgeCases:
    def test_replace_no_runtime(self):
        """replace when old plugin has no runtime (fibers empty)."""
        ctx = Context()
        reg = RegistryService(ctx)

        class PluginA:
            name = "plugin_a"

            def __init__(self, ctx):
                self.ctx = ctx

            def apply(self, ctx):
                pass

        reg.plugin(PluginA)
        # Remove the runtime from _internal so fibers list is empty
        rt = reg.get(PluginA)
        reg._internal.pop(rt.callback, None)

        # Now replace should find runtime is None, fibers = [], return []
        class PluginB:
            name = "plugin_b"

            def __init__(self, ctx):
                self.ctx = ctx

            def apply(self, ctx):
                pass

        result = reg.replace(PluginA, PluginB)
        assert result == []


class TestRegistryServiceInjectTuple:
    def test_inject_with_tuple_deps(self):
        """inject with tuple deps."""
        ctx = Context()
        reg = RegistryService(ctx)

        def my_callback(ctx):
            pass

        fiber = reg.inject(("server", "timer"), my_callback)
        from anima.core import Fiber

        assert isinstance(fiber, Fiber)

    def test_inject_with_none_deps(self):
        """inject with None deps - neither dict nor list/tuple/set."""
        ctx = Context()
        reg = RegistryService(ctx)

        def my_callback(ctx, *args, **kwargs):
            pass

        fiber = reg.inject(None, my_callback)
        from anima.core import Fiber

        assert isinstance(fiber, Fiber)


class TestRegistryServicePluginRuntimeError:
    def test_plugin_schedule_task_runtime_error(self):
        """plugin() when _schedule_task raises RuntimeError (covers lines 196-197)."""
        ctx = Context()
        reg = RegistryService(ctx)

        # Make _schedule_task raise RuntimeError
        def _raise_runtime_error(coro):
            raise RuntimeError("no running event loop")

        ctx._schedule_task = _raise_runtime_error

        # Should not raise - RuntimeError is caught and passed
        fiber = reg.plugin(_DummyPlugin)
        from anima.core import Fiber

        assert isinstance(fiber, Fiber)
