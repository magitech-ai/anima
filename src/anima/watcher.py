"""
Watcher service for filesystem change detection.
"""

from __future__ import annotations

import asyncio
import fnmatch
import inspect
import logging
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from anima.core import Context, Service
from anima.events import Disposer

logger = logging.getLogger("anima.watcher")


class WatchEntry:
    """Descriptor for an active filesystem watch configuration."""

    def __init__(
        self,
        target_path: Path,
        callback: Callable[[str, Path], Any] | None,
        interval: float,
        ignored: list[str] | Callable[[Path], bool] | None,
        ignore_initial: bool,
    ) -> None:
        self.target_path = target_path.resolve()
        self.callback = callback
        self.interval = interval
        self.ignored = ignored
        self.ignore_initial = ignore_initial
        self.task: asyncio.Task[None] | None = None
        self.stopped = False
        self.states: dict[Path, tuple[float, int]] = {}


class WatcherService(Service):
    """Filesystem watcher providing change detection via cooperative polling."""

    def __init__(self, ctx: Context) -> None:
        super().__init__(ctx, "watcher")
        self._entries: set[WatchEntry] = set()
        self.ctx.effect(lambda: self.stop)

    def _is_ignored(self, path: Path, entry: WatchEntry) -> bool:
        """Check if a path matches the entry ignore rules."""
        if entry.ignored is None:
            return False
        if callable(entry.ignored):
            return entry.ignored(path)
        for pattern in entry.ignored:
            if fnmatch.fnmatch(path.name, pattern) or fnmatch.fnmatch(
                str(path), pattern
            ):
                return True
        return False

    def _stat_file(self, path: Path) -> os.stat_result:
        """Fetch stat metrics for a single file."""
        return path.stat()

    def _scan(self, root: Path, entry: WatchEntry) -> dict[Path, tuple[float, int]]:
        """Scan target file or directory tree for modification timestamps and sizes."""
        result: dict[Path, tuple[float, int]] = {}
        if not root.exists():
            return result

        if root.is_file():
            if not self._is_ignored(root, entry):
                try:
                    st = self._stat_file(root)
                    result[root] = (st.st_mtime, st.st_size)
                except (FileNotFoundError, PermissionError):
                    pass
            return result

        try:
            for dirpath, dirnames, filenames in os.walk(root):
                d_path = Path(dirpath)
                dirnames[:] = [
                    d for d in dirnames if not self._is_ignored(d_path / d, entry)
                ]
                for fname in filenames:
                    f_path = d_path / fname
                    if not self._is_ignored(f_path, entry):
                        try:
                            st = self._stat_file(f_path)
                            result[f_path] = (st.st_mtime, st.st_size)
                        except (FileNotFoundError, PermissionError):
                            pass
        except (FileNotFoundError, PermissionError):
            pass

        return result

    async def _poll_once(self, entry: WatchEntry, initial: bool = False) -> None:
        """Execute a single polling pass across target path."""
        current_states = self._scan(entry.target_path, entry)
        if initial:
            entry.states = current_states
            return

        events: list[tuple[str, Path]] = []
        for p, st in current_states.items():
            if p not in entry.states:
                events.append(("add", p))
            elif entry.states[p] != st:
                events.append(("change", p))

        for p in list(entry.states.keys()):
            if p not in current_states:
                events.append(("unlink", p))

        entry.states = current_states

        for ev_type, p in events:
            if entry.stopped:
                break
            if entry.callback is not None:
                try:
                    res = entry.callback(ev_type, p)
                    if inspect.isawaitable(res):
                        await res
                except Exception:
                    logger.exception("Error executing watcher callback")
            try:
                await self.ctx.parallel(f"watcher/{ev_type}", p)
                await self.ctx.parallel("watcher/all", ev_type, p)
            except Exception:
                logger.exception("Error broadcasting watcher event")

    async def _poll_loop(self, entry: WatchEntry) -> None:
        """Continuous polling task loop."""
        try:
            await self._poll_once(entry, initial=entry.ignore_initial)
            while not entry.stopped:
                await asyncio.sleep(entry.interval)
                if entry.stopped:
                    break
                await self._poll_once(entry, initial=False)
        except asyncio.CancelledError:
            pass

    def watch(
        self,
        path: str | Path,
        callback: Callable[[str, Path], Any] | None = None,
        *,
        interval: float = 0.5,
        ignored: list[str] | Callable[[Path], bool] | None = None,
        ignore_initial: bool = True,
    ) -> Disposer:
        """Watch a path and trigger callbacks on changes."""
        target = Path(path).resolve()
        entry = WatchEntry(
            target_path=target,
            callback=callback,
            interval=interval,
            ignored=ignored,
            ignore_initial=ignore_initial,
        )
        self._entries.add(entry)

        try:
            loop = asyncio.get_running_loop()
            entry.task = loop.create_task(self._poll_loop(entry))
        except RuntimeError:
            pass

        def _cancel() -> None:
            entry.stopped = True
            if entry.task is not None and not entry.task.done():
                entry.task.cancel()
            self._entries.discard(entry)

        effect_disposer = self.ctx.effect(lambda: _cancel)

        def dispose() -> None:
            _cancel()
            effect_disposer()

        return dispose

    def watch_file(
        self,
        path: str | Path,
        callback: Callable[[str, Path], Any] | None = None,
        *,
        interval: float = 0.5,
    ) -> Disposer:
        """Convenience method to watch a single file."""
        return self.watch(path, callback, interval=interval)

    def watch_dir(
        self,
        path: str | Path,
        callback: Callable[[str, Path], Any] | None = None,
        *,
        interval: float = 0.5,
        ignored: list[str] | Callable[[Path], bool] | None = None,
        ignore_initial: bool = True,
    ) -> Disposer:
        """Convenience method to watch an entire directory tree."""
        return self.watch(
            path,
            callback,
            interval=interval,
            ignored=ignored,
            ignore_initial=ignore_initial,
        )

    def stop(self) -> None:
        """Stop all active watchers and terminate polling tasks."""
        for entry in list(self._entries):
            entry.stopped = True
            if entry.task is not None and not entry.task.done():
                entry.task.cancel()
        self._entries.clear()
