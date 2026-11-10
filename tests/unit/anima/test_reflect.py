"""Unit tests for ReflectService."""

import pytest

from anima.core import Context, FiberState, symbols
from anima.reflect import Impl, Property, ReflectService


class TestImpl:
    def test_impl_creation(self):
        ctx = Context()
        impl = Impl(
            uid=1,
            name="test_svc",
            value="hello",
            ctx=ctx,
            check=None,
            isolate="test_svc",
        )
        assert impl.uid == 1
        assert impl.name == "test_svc"
        assert impl.value == "hello"
        assert impl.ctx is ctx
        assert impl.check is None
        assert impl.isolate == "test_svc"
        assert impl.dependents == set()


class TestProperty:
    def test_property_creation(self):
        prop = Property("service")
        assert prop.type == "service"

        prop2 = Property("accessor", get=lambda: None)
        assert prop2.type == "accessor"


class TestReflectServiceGet:
    def test_get_no_impl(self):
        ctx = Context()
        rs = ReflectService(ctx)
        assert rs.get("nonexistent") is None

    def test_get_with_impl(self):
        ctx = Context()
        rs = ReflectService(ctx)
        rs.provide("my_svc", "hello")
        assert rs.get("my_svc") == "hello"


class TestReflectServiceSet:
    def test_set_no_impl_raises(self):
        ctx = Context()
        rs = ReflectService(ctx)
        with pytest.raises(ValueError):
            rs.set("nonexistent", "value")

    def test_set_existing_impl(self):
        ctx = Context()
        rs = ReflectService(ctx)
        rs.provide("my_svc", "hello")
        rs.set("my_svc", "world")
        assert rs.get("my_svc") == "world"


class TestReflectServiceProvide:
    def test_provide_registers(self):
        ctx = Context()
        rs = ReflectService(ctx)
        disposer = rs.provide("my_svc", "hello")
        assert rs.get("my_svc") == "hello"
        assert callable(disposer)

    def test_provide_disposer_removes(self):
        ctx = Context()
        rs = ReflectService(ctx)
        disposer = rs.provide("my_svc", "hello")
        disposer()
        assert rs.get("my_svc") is None

    def test_provide_with_check(self):
        ctx = Context()
        rs = ReflectService(ctx)

        def check():
            return True

        rs.provide("my_svc", "hello", check=check)
        assert rs.get("my_svc") == "hello"


class TestReflectServiceNotify:
    def test_notify_empty(self):
        ctx = Context()
        rs = ReflectService(ctx)
        # Should not raise
        rs.notify([])

    def test_notify_with_names(self):
        ctx = Context()
        rs = ReflectService(ctx)
        rs.provide("my_svc", "hello")
        # Should not raise
        rs.notify(["my_svc"])


class TestReflectServiceAccessor:
    def test_accessor_registers_and_disposer_removes(self):
        ctx = Context()
        rs = ReflectService(ctx)

        def getter(receiver, error):
            return "value"

        disposer = rs.accessor("my_prop", {"get": getter})
        assert "my_prop" in rs.props
        assert rs.props["my_prop"].type == "accessor"
        disposer()
        assert "my_prop" not in rs.props


class TestReflectServiceMixin:
    def test_mixin_creates_accessors(self):
        ctx = Context()

        class Source:
            def __init__(self):
                self.foo = "bar"
                self.baz = "qux"

        source = Source()
        disposer = None

        rs = ReflectService(ctx)
        disposer = rs.mixin(source, ["foo", "baz"])
        assert "foo" in rs.props
        assert "baz" in rs.props
        disposer()
        assert "foo" not in rs.props
        assert "baz" not in rs.props

    def test_mixin_dict(self):
        ctx = Context()

        class Source:
            def __init__(self):
                self.foo = "bar"

        source = Source()
        rs = ReflectService(ctx)
        disposer = rs.mixin(source, {"foo": "custom_foo"})
        assert "custom_foo" in rs.props
        disposer()
        assert "custom_foo" not in rs.props


class TestReflectServiceTrace:
    def test_trace_returns_value(self):
        ctx = Context()
        rs = ReflectService(ctx)
        val = {"key": "value"}
        assert rs.trace(val) is val


class TestReflectServiceBind:
    def test_bind_returns_callable(self):
        ctx = Context()
        rs = ReflectService(ctx)

        def callback(a, b):
            return a + b

        bound = rs.bind(callback)
        assert callable(bound)
        assert bound(1, 2) == 3

    def test_bind_traces_args(self):
        ctx = Context()
        rs = ReflectService(ctx)
        traced = []

        class Tracked:
            pass

        obj = Tracked()
        obj.__dict__[symbols["tracker"]] = type(
            "Tracker", (), {"property": "ctx", "noShadow": False, "associate": None}
        )()

        def callback(a, b):
            traced.append(a)
            return b

        bound = rs.bind(callback)
        bound(obj, 2)
        assert len(traced) == 1


class TestReflectServiceTraceAdvanced:
    def test_trace_none(self):
        ctx = Context()
        rs = ReflectService(ctx)
        assert rs.trace(None) is None

    def test_trace_primitives(self):
        ctx = Context()
        rs = ReflectService(ctx)
        assert rs.trace("hello") == "hello"
        assert rs.trace(42) == 42
        assert rs.trace(3.14) == 3.14
        assert rs.trace(True) is True

    def test_trace_dict(self):
        ctx = Context()
        rs = ReflectService(ctx)
        val = {"key": "value"}
        assert rs.trace(val) is val

    def test_trace_with_tracker(self):
        ctx = Context()
        rs = ReflectService(ctx)

        class Tracked:
            pass

        obj = Tracked()
        tracker = type(
            "Tracker", (), {"property": "ctx", "noShadow": False, "associate": None}
        )()
        setattr(obj, symbols["tracker"], tracker)

        result = rs.trace(obj)
        assert result is not obj
        assert result.ctx is ctx
        assert result[symbols["original"]] is obj

    def test_trace_regular_attribute(self):
        ctx = Context()
        rs = ReflectService(ctx)

        class Tracked:
            name = "tracked"

        obj = Tracked()
        tracker = type(
            "Tracker", (), {"property": "ctx", "noShadow": False, "associate": None}
        )()
        setattr(obj, symbols["tracker"], tracker)

        result = rs.trace(obj)
        assert result.name == "tracked"

    def test_trace_set_attribute(self):
        ctx = Context()
        rs = ReflectService(ctx)

        class Tracked:
            pass

        obj = Tracked()
        tracker = type(
            "Tracker", (), {"property": "ctx", "noShadow": False, "associate": None}
        )()
        setattr(obj, symbols["tracker"], tracker)

        result = rs.trace(obj)
        result.new_attr = "hello"
        assert obj.new_attr == "hello"

    def test_trace_invalid_key(self):
        ctx = Context()
        rs = ReflectService(ctx)

        class Tracked:
            pass

        obj = Tracked()
        tracker = type(
            "Tracker", (), {"property": "ctx", "noShadow": False, "associate": None}
        )()
        setattr(obj, symbols["tracker"], tracker)

        result = rs.trace(obj)
        with pytest.raises(KeyError):
            result["nonexistent"]

    def test_trace_with_shadow(self):
        ctx = Context()
        rs = ReflectService(ctx)

        class WithShadow:
            pass

        obj = WithShadow()
        setattr(obj, symbols["shadow"], "some_shadow")

        result = rs.trace(obj)
        assert result is type(obj)

    def test_trace_getattr_original(self):
        ctx = Context()
        rs = ReflectService(ctx)

        class Tracked:
            pass

        obj = Tracked()
        tracker = type(
            "Tracker", (), {"property": "ctx", "noShadow": False, "associate": None}
        )()
        setattr(obj, symbols["tracker"], tracker)

        result = rs.trace(obj)
        assert getattr(result, symbols["original"]) is obj

    def test_trace_getattr_dunder(self):
        ctx = Context()
        rs = ReflectService(ctx)

        class Tracked:
            pass

        obj = Tracked()
        tracker = type(
            "Tracker", (), {"property": "ctx", "noShadow": False, "associate": None}
        )()
        setattr(obj, symbols["tracker"], tracker)

        result = rs.trace(obj)
        with pytest.raises(AttributeError):
            result.__nonexistent__  # noqa: B018 - testing AttributeError is raised

    def test_trace_getattr_callable(self):
        ctx = Context()
        rs = ReflectService(ctx)

        class Tracked:
            def greet(self):
                return "hello"

        obj = Tracked()
        tracker = type(
            "Tracker", (), {"property": "ctx", "noShadow": False, "associate": None}
        )()
        setattr(obj, symbols["tracker"], tracker)

        result = rs.trace(obj)
        assert result.greet() == "hello"

    def test_trace_setattr_internal(self):
        ctx = Context()
        rs = ReflectService(ctx)

        class Tracked:
            pass

        obj = Tracked()
        tracker = type(
            "Tracker", (), {"property": "ctx", "noShadow": False, "associate": None}
        )()
        setattr(obj, symbols["tracker"], tracker)

        result = rs.trace(obj)
        result._ctx = ctx  # Should go through __setattr__ internal path


class TestReflectServiceBindAdvanced:
    def test_bind_traces_args(self):
        ctx = Context()
        rs = ReflectService(ctx)
        traced = []

        def callback(a, b):
            traced.append(a)
            return b

        bound = rs.bind(callback)
        bound(obj := object(), 2)
        assert len(traced) == 1
        assert traced[0] is obj


class TestReflectServicePropertyKwargs:
    def test_property_with_kwargs(self):
        prop = Property("accessor", get=lambda: None, custom="val")
        assert prop.type == "accessor"
        assert prop.custom == "val"


class TestReflectServiceGetImpl:
    def test_get_impl_not_found(self):
        ctx = Context()
        rs = ReflectService(ctx)
        assert rs._get_impl("nonexistent") is None

    def test_get_impl_strict_inactive(self):
        ctx = Context()
        rs = ReflectService(ctx)
        rs.provide("my_svc", "hello")
        impl = rs._get_impl("my_svc", strict=True)
        assert impl is not None
        # Set fiber to non-active state
        impl.ctx.fiber._set_state(FiberState.PENDING)
        assert rs._get_impl("my_svc", strict=True) is None
        assert rs._get_impl("my_svc", strict=False) is not None


class TestReflectServiceSetErrors:
    def test_set_no_impl(self):
        ctx = Context()
        rs = ReflectService(ctx)
        with pytest.raises(ValueError):
            rs.set("nonexistent", "value")

    def test_set_fiber_mismatch(self):
        ctx = Context()
        rs = ReflectService(ctx)
        rs.provide("my_svc", "hello")
        # Create a different context
        other_ctx = Context()
        impl = rs._get_impl("my_svc", strict=False)
        assert impl is not None
        impl.ctx = other_ctx
        with pytest.raises(ValueError):
            rs.set("my_svc", "world")


class TestReflectServiceProvideDupType:
    def test_provide_dup_type(self):
        ctx = Context()
        rs = ReflectService(ctx)
        rs.accessor("my_prop", {"get": lambda r, e: None})
        with pytest.raises(ValueError):
            rs.provide("my_prop", "value")


class TestReflectServiceNotifyFilter:
    def test_notify_with_filter(self):
        ctx = Context()
        rs = ReflectService(ctx)
        rs.provide("my_svc", "hello")
        # Filter that excludes all
        rs.notify(["my_svc"], filter_fn=lambda ctx, name: False)
        # Filter that includes all
        rs.notify(["my_svc"], filter_fn=lambda ctx, name: True)


class TestReflectServiceAccessorDup:
    def test_accessor_duplicate(self):
        ctx = Context()
        rs = ReflectService(ctx)
        rs.accessor("my_prop", {"get": lambda r, e: None})
        with pytest.raises(ValueError):
            rs.accessor("my_prop", {"get": lambda r, e: None})


class TestReflectServiceMixinDict:
    def test_mixin_dict_entries(self):
        ctx = Context()

        class Source:
            def __init__(self):
                self.foo = "bar"

        source = Source()
        rs = ReflectService(ctx)
        disposer = rs.mixin(source, {"foo": "custom_name"})
        assert "custom_name" in rs.props
        assert rs.props["custom_name"].type == "accessor"
        disposer()
        assert "custom_name" not in rs.props


class TestReflectServiceGetImplDependents:
    def test_impl_dependents(self):
        ctx = Context()
        impl = Impl(1, "test", "val", ctx, None, "test")
        assert impl.dependents == set()
        impl.dependents.add("test_dep")
        assert "test_dep" in impl.dependents


class TestReflectServiceProvideInactive:
    def test_provide_inactive_fiber(self):
        """provide when fiber is not active - notify not called."""
        ctx = Context()
        rs = ReflectService(ctx)
        # Manually set fiber to non-active
        ctx.fiber._set_state(FiberState.PENDING)
        disposer = rs.provide("my_svc", "hello")
        assert rs.get("my_svc") == "hello"
        disposer()


class TestReflectServiceDisposerAlreadyRemoved:
    def test_disposer_already_removed(self):
        """disposer when impl was already removed from store."""
        ctx = Context()
        rs = ReflectService(ctx)
        disposer = rs.provide("my_svc", "hello")
        del rs.store["my_svc"]
        # store.get("my_svc") is None, impl is the original impl - not the same
        disposer()

    def test_disposer_already_removed2(self):
        """disposer when impl was already removed - False branch of is check."""
        ctx = Context()
        rs = ReflectService(ctx)
        rs.provide("my_svc", "hello")
        disposer = rs.provide("my_svc", "world")
        disposer()  # store.get("my_svc") is impl2 (same object), enters del branch


class TestReflectServiceNotifyWithFibers:
    def test_notify_with_matching_fiber(self):
        """notify with a fiber that has matching inject."""
        ctx = Context()
        rs = ReflectService(ctx)
        rs.provide("my_svc", "hello")

        # Create a fiber that injects "my_svc"
        class TestPlugin:
            name = "test_plugin"

            def __init__(self, ctx):
                self.ctx = ctx

            def apply(self, ctx):
                pass

        reg = ctx.registry
        fiber = reg.plugin(TestPlugin)
        # Manually add "my_svc" to fiber.inject
        fiber.inject = ["my_svc"]

        # Notify should call _validate_config on the fiber
        rs.notify(["my_svc"])
        # If we get here without error, the notify worked


class TestReflectServiceMixinGetattr:
    def test_mixin_getattr_works(self):
        """mixin getattr works when source attribute exists on ctx."""
        ctx = Context()
        ctx.my_value = "test_value"

        rs = ReflectService(ctx)
        disposer = rs.mixin(ctx, ["my_value"])
        assert "my_value" in rs.props
        disposer()
        assert "my_value" not in rs.props


class TestReflectServiceGetterSetterCalled:
    def test_getter_and_setter_called(self):
        """getter and setter are actually invoked."""
        ctx = Context()

        class Source:
            def __init__(self):
                self.foo = "original"

        source = Source()
        ctx.source = source

        rs = ReflectService(ctx)
        disposer = rs.mixin("source", ["foo"])

        # Getter should return ctx.source.foo
        prop = rs.props["foo"]
        getter = prop.get
        result = getter(ctx, None)
        assert result == "original"

        # Setter should set ctx.source.foo
        setter = prop.set
        setter("new_value", ctx, None)
        assert source.foo == "new_value"

        disposer()


class TestReflectServiceNotifyNoFibers:
    def test_notify_no_fibers(self):
        """notify when there are no fibers."""
        ctx = Context()
        rs = ReflectService(ctx)
        rs.provide("my_svc", "hello")
        # Should not raise even with no fibers
        rs.notify(["my_svc"])


class TestReflectServiceNotifyNoMatch:
    def test_notify_no_matching_inject(self):
        """notify when no fiber has matching inject."""
        ctx = Context()
        rs = ReflectService(ctx)
        rs.provide("my_svc", "hello")

        class TestPlugin:
            name = "test_plugin"

            def __init__(self, ctx):
                self.ctx = ctx

            def apply(self, ctx):
                pass

        reg = ctx.registry
        reg.plugin(TestPlugin)
        # Fiber.inject is empty, so notify should not match
        rs.notify(["my_svc"])

    def test_notify_filter_false_for_matching_fiber(self):
        """notify with filter_fn returning False for a matching fiber (covers line 135-136)."""
        ctx = Context()
        rs = ReflectService(ctx)
        rs.provide("my_svc", "hello")

        class TestPlugin:
            name = "test_plugin"

            def __init__(self, ctx):
                self.ctx = ctx

            def apply(self, ctx):
                pass

        reg = ctx.registry
        fiber = reg.plugin(TestPlugin)
        fiber.inject = ["my_svc"]

        # Filter returns False, so the fiber should be skipped (line 135-136)
        rs.notify(["my_svc"], filter_fn=lambda ctx, name: False)

    def test_notify_runtime_without_fibers(self):
        """notify with a runtime object that has no 'fibers' attribute (covers line 128-129)."""
        ctx = Context()
        rs = ReflectService(ctx)

        # Add a non-PluginRuntime object to _internal - it won't have 'fibers'
        ctx.registry._internal["fake_key"] = object()

        # Should not raise
        rs.notify(["my_svc"])

    def test_notify_impl_none(self):
        """notify where _get_impl returns None (covers line 140->132 branch fall-through)."""
        ctx = Context()
        rs = ReflectService(ctx)
        rs.provide("my_svc", "hello")

        class TestPlugin:
            name = "test_plugin"

            def __init__(self, ctx):
                self.ctx = ctx

            def apply(self, ctx):
                pass

        reg = ctx.registry
        fiber = reg.plugin(TestPlugin)
        fiber.inject = ["my_svc"]

        # Remove the impl from the store so _get_impl returns None
        del rs.store["my_svc"]

        # Should not raise - impl is None, falls through to line 142
        rs.notify(["my_svc"])
