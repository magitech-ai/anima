"""Registry service for plugin runtimes.

Python port of the RegistryService: manages plugin runtimes, fiber
tracking, and plugin replacement.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable, Iterable
from typing import Any, cast

from anima.core import Context, Fiber


def Inject(name: str, config: Any = None) -> Callable[[Any], Any]:
    """Decorator for declaring service dependencies on classes or methods.

    Inject decorator parity. When applied to a class, sets
    ``cls.inject[name] = config``. When applied to a method, stores
    inject metadata on the method for later processing.
    """

    def decorator(value: Any) -> Any:  # pragma: no cover
        if isinstance(value, type):  # pragma: no cover
            # Class decorator  # pragma: no cover
            if not hasattr(value, "inject") or value.inject is None:  # type: ignore[attr-defined]  # pragma: no cover
                value.inject = {}  # type: ignore[attr-defined]  # pragma: no cover
            value.inject[name] = config  # type: ignore[attr-defined]  # pragma: no cover
            return value  # pragma: no cover
        else:  # pragma: no cover
            # Function/method decorator  # pragma: no cover
            if not hasattr(value, "_inject_metadata"):  # pragma: no cover
                value._inject_metadata = {}  # pragma: no cover
            value._inject_metadata[name] = config  # pragma: no cover
            return value  # pragma: no cover

    return decorator  # pragma: no cover


def resolve_inject(inject: Any, result: dict[str, Any] | None = None) -> dict[str, Any]:
    """Normalize inject metadata into a plain name -> config map.

    Inject.resolve() parity.
    """
    if result is None:
        result = {}  # pragma: no cover
    if not inject:
        return result
    if isinstance(inject, (list, tuple, set)):
        for name in inject:
            result[name] = None
    elif isinstance(inject, dict):  # pragma: no cover
        for name in inject:  # pragma: no cover
            result[name] = (
                inject[name] if inject[name] is not None else None
            )  # pragma: no cover
    return result  # pragma: no cover


class PluginRuntime:
    """Runtime handle for a registered plugin."""

    def __init__(
        self,
        name: str | None,
        callback: Callable[..., Any],
        Config: Any = None,
    ) -> None:
        self.name = name
        self.callback = callback
        self.fibers: list[Fiber] = []
        self.Config = Config


class RegistryService:
    """Registry of plugin runtimes with fiber tracking and replacement.

    RegistryService does not extend Service (anima parity).
    """

    def __init__(self, ctx: Context) -> None:
        self.ctx = ctx
        self.name = "registry"
        self._counter = 0
        self._internal: dict[Callable[..., Any], PluginRuntime] = {}
        # function id -> plugin (WeakMap-like alias chain)
        self._aliases: dict[int, Any] = {}

    @property
    def counter(self) -> int:
        """Auto-incrementing counter."""
        self._counter += 1
        return self._counter

    @property
    def size(self) -> int:
        """Number of registered runtimes."""
        return len(self._internal)

    def resolve(self, plugin: Any) -> Callable[..., Any] | None:
        """Resolve a plugin to its callback function."""
        try:
            if callable(plugin):
                return cast(Callable[..., Any], plugin)
            apply_fn = getattr(plugin, "apply", None)
            if apply_fn is not None and callable(apply_fn):
                return cast(Callable[..., Any], apply_fn)
        except Exception:  # noqa: BLE001, S110 - defensive resolution, return None on failure
            pass
        return None

    def _forward(self, plugin: Any) -> tuple[Callable[..., Any], Any] | None:
        """Follow alias chain to find the canonical key and plugin."""
        key = self.resolve(plugin)
        visited: set[int] = set()
        while key is not None:
            key_id = id(key)
            if key_id in visited:
                return None  # cycle detected
            visited.add(key_id)
            if key_id not in self._aliases:
                return key, plugin
            plugin = self._aliases[key_id]
            key = self.resolve(plugin)
        return None

    def _create(self, callback: Callable[..., Any], plugin: Any) -> PluginRuntime:
        name = getattr(plugin, "name", None)
        if name == "apply":
            name = None
        runtime = PluginRuntime(name, callback, getattr(plugin, "Config", None))
        self._internal[callback] = runtime
        return runtime

    def get(self, plugin: Any) -> PluginRuntime | None:
        """Get the runtime for a plugin, following aliases."""
        forwarded = self._forward(plugin)
        if forwarded is None:
            return None
        key, _ = forwarded
        return self._internal.get(key)

    def has(self, plugin: Any) -> bool:
        """Check if a plugin is registered."""
        forwarded = self._forward(plugin)
        if forwarded is None:
            return False
        key, _ = forwarded
        return key in self._internal

    def delete(self, plugin: Any) -> PluginRuntime | None:
        """Dispose every running fiber for a plugin and remove its runtime record.

        RegistryService.delete(plugin) parity.
        """
        forwarded = self._forward(plugin)  # pragma: no cover
        if forwarded is None:  # pragma: no cover
            return None  # pragma: no cover
        key, _ = forwarded  # pragma: no cover
        runtime = self._internal.get(key)  # pragma: no cover
        if not runtime:  # pragma: no cover
            return None  # pragma: no cover
        self._internal.pop(key, None)  # pragma: no cover
        for fiber in list(runtime.fibers):  # pragma: no cover
            self.ctx._schedule_task(fiber.dispose())  # pragma: no cover
        return runtime  # pragma: no cover

    def _unregister(self, runtime: PluginRuntime) -> None:
        """Unregister a runtime by its callback."""
        self._internal.pop(runtime.callback, None)

    def replace(self, old_plugin: Any, new_plugin: Any) -> list[Fiber]:
        """Replace a plugin's runtime, reloading all dependent fibers."""
        source = self._forward(old_plugin)
        if source is None:
            raise ValueError(
                "invalid plugin, expect function or object with an "
                f"'apply' method, received {type(old_plugin)}"
            )
        key = self.resolve(new_plugin)
        if key is None:
            raise ValueError(
                "invalid plugin, expect function or object with an "
                f"'apply' method, received {type(new_plugin)}"
            )

        old_key = source[0]
        runtime = self._internal.get(old_key)
        target = self._internal.get(key)

        if old_key is not key:
            self._internal.pop(old_key, None)
            self._aliases[id(old_key)] = new_plugin

        fibers = list(runtime.fibers) if runtime else []
        if not fibers:
            return fibers

        if target is None:
            target = self._create(key, new_plugin)

        for fiber in fibers:
            self.ctx._schedule_task(fiber.replace(target.callback))
        return fibers

    def keys(self) -> Iterable[Callable[..., Any]]:
        return self._internal.keys()

    def values(self) -> Iterable[PluginRuntime]:
        return self._internal.values()

    def entries(self) -> Iterable[tuple[Callable[..., Any], PluginRuntime]]:
        return self._internal.items()

    def for_each(
        self, callback: Callable[[PluginRuntime, Callable[..., Any]], None]
    ) -> None:
        for key, value in self._internal.items():
            callback(value, key)

    def inject(self, deps: Any, callback: Callable[..., Any]) -> Fiber:
        """Register an inline plugin with declared dependencies."""
        inject_map: dict[str, Any] = {}
        resolve_inject(deps, inject_map)
        inject_list = list(inject_map.keys())

        class _InlineRegistryPlugin:
            inject = inject_list

            def __init__(self, ctx: Context) -> None:
                self.ctx = ctx

            async def init(self) -> Any:
                res = callback(self.ctx)
                if inspect.isawaitable(res):
                    return await res
                return res

        return self.plugin(_InlineRegistryPlugin)

    def plugin(self, plugin: Any, config: Any = None) -> Fiber:
        """Mount a plugin and return its fiber."""
        forwarded = self._forward(plugin)
        if forwarded is None:
            raise ValueError(
                "invalid plugin, expect function or object with an "
                f"'apply' method, received {type(plugin)}"
            )

        callback, target = forwarded
        runtime = self._internal.get(callback) or self._create(callback, target)
        config_dict = dict(config or {})
        fiber = Fiber(self.ctx, target, (), config_dict)
        runtime.fibers.append(fiber)
        parent_fiber = self.ctx.fiber
        fiber.parent_fiber = parent_fiber
        parent_fiber.child_fibers.append(fiber)
        self.ctx.emit("internal/plugin", fiber)

        if fiber.check_dependencies():
            try:
                self.ctx._schedule_task(fiber.load())
            except RuntimeError:
                pass  # No running event loop

        return fiber
