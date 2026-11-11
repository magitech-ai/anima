"""Unit tests for RegistryService."""

import pytest

from anima.core import Context, FiberState
from anima.registry import ComponentRuntime, RegistryService


class _DummyComponent:
    name = "dummy"
    Config = None

    def __init__(self, ctx, **kwargs):
        self.ctx = ctx

    def apply(self, ctx):
        pass


class _AnotherComponent:
    name = "another"

    def __init__(self, ctx, **kwargs):
        self.ctx = ctx

    def apply(self, ctx):
        pass


class TestComponentRuntime:
    def test_component_runtime_creation(self):
        def cb():
            pass

        rt = ComponentRuntime("test", cb, Config={"key": "val"})
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
        reg.use(_DummyComponent)
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
        obj = _DummyComponent(ctx)
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
        reg.use(_DummyComponent)
        assert reg.has(_DummyComponent)
        rt = reg.get(_DummyComponent)
        assert rt is not None
        assert rt.name == "dummy"

    def test_get_has_unregistered(self):
        ctx = Context()
        reg = RegistryService(ctx)
        assert not reg.has(_DummyComponent)
        assert reg.get(_DummyComponent) is None


class TestRegistryServiceComponentMount:
    def test_component_mount_creates_fiber(self):
        ctx = Context()
        reg = RegistryService(ctx)
        fiber = reg.use(_DummyComponent)
        from anima.core import Fiber

        assert isinstance(fiber, Fiber)
        assert fiber.state == FiberState.ACTIVE

    def test_component_mount_with_config(self):
        ctx = Context()
        reg = RegistryService(ctx)
        fiber = reg.use(_DummyComponent, {"key": "val"})
        assert fiber.raw_config == {"key": "val"}

    def test_component_invalid_target(self):
        ctx = Context()
        reg = RegistryService(ctx)
        with pytest.raises(ValueError):
            reg.use(42)


class TestRegistryServiceReplace:
    def test_replace_reloads_fiber(self):
        ctx = Context()
        reg = RegistryService(ctx)
        fiber = reg.use(_DummyComponent)

        class _DummyComponentV2:
            name = "dummy"

            def __init__(self, ctx, **kwargs):
                self.ctx = ctx

            def apply(self, ctx):
                pass

        reg.replace(_DummyComponent, _DummyComponentV2)
        assert fiber.state in (FiberState.ACTIVE, FiberState.FAILED)

    def test_replace_unknown_component(self):
        ctx = Context()
        reg = RegistryService(ctx)
        reg.use(_DummyComponent)
        with pytest.raises(ValueError):
            reg.replace(_DummyComponent, 42)  # 42 is not a valid component


class TestRegistryServiceIteration:
    def test_keys_values_entries_for_each(self):
        ctx = Context()
        reg = RegistryService(ctx)
        reg.use(_DummyComponent)
        reg.use(_AnotherComponent)

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
    def test_inject_mounts_component(self):
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
        reg.use(_DummyComponent)
        assert reg.has(_DummyComponent)

        rt = reg.get(_DummyComponent)
        reg._unregister(rt)
        assert not reg.has(_DummyComponent)


class TestRegistryServiceResolveNone:
    def test_get_resolve_none(self):
        """get with component that resolves to None returns None."""
        ctx = Context()
        reg = RegistryService(ctx)
        assert reg.get(42) is None

    def test_has_resolve_none(self):
        """has with component that resolves to None returns False."""
        ctx = Context()
        reg = RegistryService(ctx)
        assert not reg.has(42)


class TestRegistryServiceReplaceOldInvalid:
    def test_replace_old_invalid(self):
        """replace with old component that resolves to None raises ValueError."""
        ctx = Context()
        reg = RegistryService(ctx)
        with pytest.raises(ValueError):
            reg.replace(42, _DummyComponent)


class TestRegistryServiceReplaceSameKey:
    def test_replace_same_key(self):
        """replace when old_key is key (same component)."""
        ctx = Context()
        reg = RegistryService(ctx)

        class ComponentA:
            name = "component_a"

            def __init__(self, ctx):
                self.ctx = ctx

            def apply(self, ctx):
                pass

        reg.use(ComponentA)
        result = reg.replace(ComponentA, ComponentA)
        assert isinstance(result, list)


class TestRegistryServiceReplaceTargetExists:
    def test_replace_target_exists(self):
        """replace when target runtime already exists."""
        ctx = Context()
        reg = RegistryService(ctx)

        class ComponentA:
            name = "component_a"

            def __init__(self, ctx):
                self.ctx = ctx

            def apply(self, ctx):
                pass

        class ComponentB:
            name = "component_b"

            def __init__(self, ctx):
                self.ctx = ctx

            def apply(self, ctx):
                pass

        reg.use(ComponentA)
        reg.use(ComponentB)
        # Replace ComponentA with ComponentB - target (ComponentB runtime) already exists
        result = reg.replace(ComponentA, ComponentB)
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

        class ComponentA:
            name = "component_a"

            def __init__(self, ctx):
                self.ctx = ctx

            def apply(self, ctx):
                pass

        class ComponentB:
            name = "component_b"

            def __init__(self, ctx):
                self.ctx = ctx

            def apply(self, ctx):
                pass

        reg.use(ComponentA)
        # Create alias: ComponentB -> ComponentA
        reg._aliases[id(ComponentB)] = ComponentA

        # Resolving ComponentB should follow alias to ComponentA
        result = reg._forward(ComponentB)
        assert result is not None
        key, component = result
        assert key is ComponentA
        assert component is ComponentA

    def test_get_has_with_alias(self):
        ctx = Context()
        reg = RegistryService(ctx)

        class ComponentA:
            name = "component_a"

            def __init__(self, ctx):
                self.ctx = ctx

            def apply(self, ctx):
                pass

        class ComponentB:
            name = "component_b"

            def __init__(self, ctx):
                self.ctx = ctx

            def apply(self, ctx):
                pass

        reg.use(ComponentA)
        reg._aliases[id(ComponentB)] = ComponentA

        assert reg.has(ComponentB)
        rt = reg.get(ComponentB)
        assert rt is not None
        assert rt.name == "component_a"


class TestRegistryServiceCreateNameApply:
    def test_create_with_name_apply(self):
        ctx = Context()
        reg = RegistryService(ctx)

        class ComponentApply:
            name = "apply"

            def __init__(self, ctx):
                self.ctx = ctx

            def apply(self, ctx):
                pass

        reg.use(ComponentApply)
        rt = reg.get(ComponentApply)
        assert rt.name is None  # name "apply" is converted to None


class TestRegistryServiceReplaceEmpty:
    def test_replace_returns_fibers(self):
        ctx = Context()
        reg = RegistryService(ctx)

        class ComponentA:
            name = "component_a"

            def __init__(self, ctx):
                self.ctx = ctx

            def apply(self, ctx):
                pass

        class ComponentB:
            name = "component_b"

            def __init__(self, ctx):
                self.ctx = ctx

            def apply(self, ctx):
                pass

        fiber = reg.use(ComponentA)
        result = reg.replace(ComponentA, ComponentB)
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

        class ComponentA:
            name = "component_a"

            def __init__(self, ctx):
                self.ctx = ctx

            def apply(self, ctx):
                pass

        reg.use(ComponentA)
        # Setting up alias directly
        reg._aliases[id(ComponentA)] = ComponentA
        # Forward should detect cycle and return None
        result = reg._forward(ComponentA)
        assert result is None


class TestRegistryServiceReplaceEdgeCases:
    def test_replace_no_runtime(self):
        """replace when old component has no runtime (fibers empty)."""
        ctx = Context()
        reg = RegistryService(ctx)

        class ComponentA:
            name = "component_a"

            def __init__(self, ctx):
                self.ctx = ctx

            def apply(self, ctx):
                pass

        reg.use(ComponentA)
        # Remove the runtime from _internal so fibers list is empty
        rt = reg.get(ComponentA)
        reg._internal.pop(rt.callback, None)

        # Now replace should find runtime is None, fibers = [], return []
        class ComponentB:
            name = "component_b"

            def __init__(self, ctx):
                self.ctx = ctx

            def apply(self, ctx):
                pass

        result = reg.replace(ComponentA, ComponentB)
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


class TestRegistryServiceComponentRuntimeError:
    def test_component_schedule_task_runtime_error(self):
        """use() when _schedule_task raises RuntimeError (covers lines 196-197)."""
        ctx = Context()
        reg = RegistryService(ctx)

        # Make _schedule_task raise RuntimeError
        def _raise_runtime_error(coro):
            raise RuntimeError("no running event loop")

        ctx._schedule_task = _raise_runtime_error

        # Should not raise - RuntimeError is caught and passed
        fiber = reg.use(_DummyComponent)
        from anima.core import Fiber

        assert isinstance(fiber, Fiber)
