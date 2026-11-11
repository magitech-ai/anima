"""
Hot Module Replacement (HMR) service for dynamic live reloading.
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import sys
from pathlib import Path
from types import ModuleType
from typing import Any, ClassVar

from anima.core import Context, FiberState, Service
from anima.events import Disposer

logger = logging.getLogger("anima.hmr")


class HmrService(Service):
    """Hot Module Replacement service coordinating watcher events and fiber reloads."""

    inject: ClassVar[list[str]] = ["watcher"]

    def __init__(
        self,
        ctx: Context,
        config: dict[str, Any] | None = None,
    ) -> None:
        cfg = dict(config or {})
        root = cfg.get("root")
        ignored = cfg.get("ignored")
        interval = cfg.get("interval", 0.5)
        super().__init__(ctx, "hmr")
        self.root = Path(root).resolve() if root else Path.cwd().resolve()
        self.ignored = (
            ignored
            if ignored is not None
            else ["*.pyc", "__pycache__", ".git", ".pytest_cache", ".venv", "tests"]
        )
        self.interval = interval
        self._disposer: Disposer | None = None
        self._active = False
        self._inflight: dict[
            str, tuple[asyncio.Task[ModuleType | None], tuple[float, int] | None]
        ] = {}
        self.ctx.effect(lambda: self.stop)

    def init(self) -> None:
        """Lifecycle hook when mounted as a component."""
        self.start()

    def start(self) -> None:
        """Start watching root directory for Python module changes."""
        if self._active:
            return

        watcher = self.ctx.watcher
        self._disposer = watcher.watch(
            self.root,
            self._on_file_change,
            interval=self.interval,
            ignored=self.ignored,
            ignore_initial=True,
        )
        self._active = True

    def stop(self) -> None:
        """Stop watching and cleanup active handlers."""
        if self._disposer is not None:
            self._disposer()
            self._disposer = None
        self._active = False

    def _find_module_by_path(self, path: Path) -> tuple[str, ModuleType] | None:
        """Resolve loaded sys.modules entry corresponding to a filesystem path."""
        try:
            res_path = path.resolve()
        except OSError:
            return None
        for name, mod in list(sys.modules.items()):
            if mod is None:
                continue
            mod_file = getattr(mod, "__file__", None)
            if mod_file is not None:
                try:
                    if Path(mod_file).resolve() == res_path:
                        return name, mod
                except (ValueError, OSError):
                    pass

        try:
            rel = res_path.relative_to(self.root)
            mod_candidate = ".".join(rel.with_suffix("").parts)
            if mod_candidate in sys.modules and sys.modules[mod_candidate] is not None:
                return mod_candidate, sys.modules[mod_candidate]
        except ValueError:
            pass

        return None

    async def _on_file_change(self, action: str, path: Path) -> None:
        """Process file change events emitted by the watcher."""
        if action not in ("change", "add"):
            return
        if path.suffix != ".py":
            return

        match = self._find_module_by_path(path)
        if match is None:
            return

        mod_name, mod = match
        await self.reload_module(mod_name, mod=mod, path=path)

    def _target_stat(
        self,
        mod_name: str,
        *,
        mod: ModuleType | None = None,
        path: Path | None = None,
    ) -> tuple[float, int] | None:
        """(mtime, size) of the module file, or None if it cannot be read."""
        target_path = path
        if target_path is None:
            target_mod = mod if mod is not None else sys.modules.get(mod_name)
            if target_mod is None:
                return None
            m_file = getattr(target_mod, "__file__", None)
            if not m_file:
                return None
            target_path = Path(m_file)
        try:
            st = target_path.stat()
        except OSError:
            return None
        return (st.st_mtime, st.st_size)

    async def reload_module(
        self,
        mod_name: str,
        *,
        mod: ModuleType | None = None,
        path: Path | None = None,
    ) -> ModuleType | None:
        """Reload a Python module and update associated active fibers.

        Concurrent reloads of the same module share one in-flight reload:
        a file-watcher event racing an explicit reload must not reload the
        module twice or emit the lifecycle events twice. But a reload that
        started before the file changed again returns the old content, so
        a caller whose file moved on waits for the in-flight reload and
        then reloads the current content instead of joining the stale one.

        Do not call this reentrantly from an HMR lifecycle handler
        (``hmr/before-reload``, ``hmr/after-reload``, ``hmr/reload``,
        ``hmr/error``) for the same module: the handler would await the
        in-flight reload it is running inside, and deadlock. Reloading a
        different module from a handler is safe.
        """
        stat = self._target_stat(mod_name, mod=mod, path=path)
        existing = self._inflight.get(mod_name)
        if existing is not None:
            task, old_stat = existing
            if stat is not None and old_stat is not None and stat != old_stat:
                # The file changed since the in-flight reload started; its
                # result is stale. Wait for it, then reload the current
                # content below instead of joining the stale one.
                await task
            else:
                return await task
        loop = asyncio.get_running_loop()
        task = loop.create_task(self._reload_module(mod_name, mod=mod, path=path))
        self._inflight[mod_name] = (task, stat)
        try:
            return await task
        finally:
            current = self._inflight.get(mod_name)
            if current is not None and current[0] is task:
                self._inflight.pop(mod_name, None)

    async def _reload_module(
        self,
        mod_name: str,
        *,
        mod: ModuleType | None = None,
        path: Path | None = None,
    ) -> ModuleType | None:
        """Perform one reload of a Python module and reconcile its fibers."""
        target_mod = mod if mod is not None else sys.modules.get(mod_name)
        if target_mod is None:
            return None

        await self.ctx.parallel("hmr/before-reload", mod_name, path)

        target_path = path
        if target_path is None:
            m_file = getattr(target_mod, "__file__", None)
            if m_file:
                target_path = Path(m_file)

        parent_dir = str(target_path.parent) if target_path else None
        added_to_path = False
        if parent_dir and parent_dir not in sys.path:
            sys.path.insert(0, parent_dir)
            added_to_path = True

        try:
            importlib.invalidate_caches()
            reloaded = importlib.reload(target_mod)
        except Exception as exc:
            logger.exception("Failed to reload module %s", mod_name)
            await self.ctx.parallel("hmr/error", exc, mod_name, path)
            return None
        finally:
            if added_to_path and parent_dir in sys.path:
                sys.path.remove(parent_dir)

        await self.ctx.parallel("hmr/after-reload", mod_name, path)

        # Reconcile fibers bound to targets from this module
        all_fibers = self.ctx.root.all_fibers()
        for fiber in all_fibers:
            if fiber.state == FiberState.DISPOSED:
                continue

            target = fiber.target
            target_mod_name: str | None = None
            is_class_or_func = isinstance(target, type) or callable(target)

            if is_class_or_func:
                target_mod_name = getattr(target, "__module__", None)
            else:
                target_mod_name = getattr(target.__class__, "__module__", None)

            if target_mod_name == mod_name:
                if is_class_or_func:
                    attr_name = getattr(target, "__name__", None)
                    new_target: Any = (
                        getattr(reloaded, attr_name, None) if attr_name else None
                    )
                    if new_target is not None:
                        await fiber.replace(new_target)
                    else:
                        await fiber.restart()
                else:
                    await fiber.restart()

        await self.ctx.parallel("hmr/reload", mod_name, path)
        return reloaded
