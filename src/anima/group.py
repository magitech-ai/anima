"""
Group component for composite lifecycle management in Anima.
Enables hierarchical grouping of components with collective start, stop, toggle,
and configuration reconciliation.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from typing import Any

from anima.core import Context, Fiber, FiberState
from anima.loader import resolve_component_target


@dataclass
class GroupEntry:
    id: str
    target: Any
    config: dict[str, Any] = field(default_factory=dict)
    disabled: bool = False
    fiber: Fiber | None = None


class Group:
    """Composite component container managing a set of child components."""

    def __init__(
        self,
        ctx: Context,
        entries: list[dict[str, Any]] | None = None,
        *,
        name: str = "group",
    ) -> None:
        self.ctx = ctx
        self.name = name
        self.scope: Context = ctx.extend()
        self.entries: dict[str, GroupEntry] = {}

        # Parse initial entries
        if entries:
            for item in entries:
                self._add_entry_from_dict(item)

        # Register update hook for configuration reconciliation
        ctx.on("internal/update", self._on_update)

    def _add_entry_from_dict(self, item: dict[str, Any]) -> GroupEntry:
        eid = str(item.get("id") or item.get("name") or len(self.entries))
        target_spec = item.get("target") or item.get("name")
        if target_spec is None:
            raise ValueError(f"Group entry {item} must specify 'name' or 'target'")
        target = (
            resolve_component_target(target_spec)
            if isinstance(target_spec, str)
            else target_spec
        )
        cfg = dict(item.get("config", {}))
        disabled = bool(item.get("disabled", False))
        entry = GroupEntry(id=eid, target=target, config=cfg, disabled=disabled)
        self.entries[eid] = entry
        return entry

    async def apply(self, ctx: Context, *args: Any, **kwargs: Any) -> None:
        """Mount active child entries onto the scoped context."""
        ctx.effect(lambda: self.stop)
        for entry in list(self.entries.values()):
            if not entry.disabled and entry.fiber is None:
                entry.fiber = self.scope.use(entry.target, **entry.config)
        await self.scope.flush()

    async def _on_update(self, new_config: Any, next_fn: Any = None) -> Any:
        entries_data = (
            new_config.get("entries") if isinstance(new_config, dict) else new_config
        )
        if isinstance(entries_data, list):
            await self.reconcile(entries_data)
        if callable(next_fn):
            res = next_fn(new_config)
            if inspect.isawaitable(res):
                return await res
            return res
        return None

    async def reconcile(self, new_entries: list[dict[str, Any]]) -> None:
        """Reconcile child entries against updated configuration."""
        seen_ids: set[str] = set()

        for item in new_entries:
            eid = str(item.get("id") or item.get("name") or len(seen_ids))
            seen_ids.add(eid)
            new_cfg = dict(item.get("config", {}))
            new_disabled = bool(item.get("disabled", False))

            if eid in self.entries:
                entry = self.entries[eid]
                entry.config = new_cfg
                if new_disabled != entry.disabled:
                    entry.disabled = new_disabled
                    if entry.disabled:
                        if entry.fiber is not None:
                            await entry.fiber.dispose()
                            entry.fiber = None
                    else:
                        entry.fiber = self.scope.use(entry.target, **entry.config)
                elif entry.fiber is not None:
                    await entry.fiber.update(new_cfg)
            else:
                entry = self._add_entry_from_dict(item)
                if not entry.disabled:
                    entry.fiber = self.scope.use(entry.target, **entry.config)

        # Remove entries that no longer exist
        for eid in list(self.entries.keys()):
            if eid not in seen_ids:
                entry = self.entries.pop(eid)
                if entry.fiber is not None:
                    await entry.fiber.dispose()
                    entry.fiber = None

        await self.scope.flush()

    async def enable(self, entry_id: str) -> None:
        """Enable and start a previously disabled entry."""
        if entry_id in self.entries:
            entry = self.entries[entry_id]
            if entry.disabled:
                entry.disabled = False
                if entry.fiber is None:
                    entry.fiber = self.scope.use(entry.target, **entry.config)
                    await self.scope.flush()

    async def disable(self, entry_id: str) -> None:
        """Disable and unload an entry."""
        if entry_id in self.entries:
            entry = self.entries[entry_id]
            if not entry.disabled:
                entry.disabled = True
                if entry.fiber is not None:
                    await entry.fiber.dispose()
                    entry.fiber = None

    async def stop(self) -> None:
        """Dispose all child fibers in this group."""
        for entry in list(self.entries.values()):
            if entry.fiber is not None:
                await entry.fiber.dispose()
                entry.fiber = None
        self.entries.clear()

    def status(self) -> dict[str, Any]:
        """Return diagnostic status of child group entries."""
        return {
            "name": self.name,
            "entries": {
                eid: {
                    "disabled": e.disabled,
                    "state": e.fiber.state.value
                    if e.fiber
                    else FiberState.DISPOSED.value,
                }
                for eid, e in self.entries.items()
            },
        }
