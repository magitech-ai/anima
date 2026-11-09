"""Registry service for plugin runtimes.

Python port of the reference RegistryService: manages plugin runtimes, fiber
tracking, and plugin replacement.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any, cast

from anima.core import Context, Fiber, FiberState, Service


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


class RegistryService(Service):
    """Registry of plugin runtimes with fiber tracking and replacement."""

    def __init__(self, ctx: Context) -> None:
        super().__init__(ctx, "registry")
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
        inject_list: list[str] = []
        if isinstance(deps, dict):
            inject_list = list(deps.keys())
        elif isinstance(deps, (list, tuple, set)):
            inject_list = list(deps)

        target = type(
            "_InlineRegistryPlugin",
            (),
            {
                "inject": inject_list,
                "apply": callback,
                "name": callback.__name__,
                "__init__": lambda self, ctx: setattr(self, "ctx", ctx),
            },
        )
        return self.plugin(target)

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
        fiber = Fiber(self.ctx, target, (), {})
        fiber.raw_config = dict(config or {})
        fiber.config = dict(config or {})
        runtime.fibers.append(fiber)
        self.ctx.fiber.child_fibers.append(fiber)
        self.ctx.emit("internal/plugin", fiber)

        if fiber.state != FiberState.FAILED and fiber.check_dependencies():
            try:
                self.ctx._schedule_task(fiber.load())
            except RuntimeError:
                pass  # No running event loop

        return fiber
