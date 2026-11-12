"""
Profile and configuration loader for Anima.
Enables dynamic configuration-driven composition, environment interpolation, and persistence.
"""

from __future__ import annotations

import inspect
import logging
import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

from anima.core import Context, Fiber, FiberState, Service
from anima.events import Disposer, EventMode
from anima.tree import (
    _UNSET,
    BUILTIN_ALIASES,
    Entry,
    EntryTree,
    resolve_component_target,
)
from anima.tree import (
    unwrap_exports as _tree_unwrap_exports,
)

__all__ = [
    "LoaderService",
    "interpolate_env",
    "load_profile",
    "resolve_component_target",
    "save_profile",
]

logger = logging.getLogger("anima.loader")

ENV_PATTERN = re.compile(r"\$\{(?:env\.)?([A-Za-z_][A-Za-z0-9_]*)(?::([^}]*))?\}")

_BUILTIN_ALIASES: dict[str, str] = BUILTIN_ALIASES


def interpolate_env(value: Any, env: Mapping[str, str] | None = None) -> Any:
    """Recursively interpolate environment variables in string, dict, or list."""
    environ = env if env is not None else os.environ

    if isinstance(value, str):
        full_match = ENV_PATTERN.fullmatch(value)
        if full_match:
            var_name = full_match.group(1)
            default_val = full_match.group(2)
            raw = environ.get(var_name, default_val if default_val is not None else "")
            if raw.lower() == "true":
                return True
            if raw.lower() == "false":
                return False
            try:
                return int(raw)
            except ValueError:
                pass
            try:
                return float(raw)
            except ValueError:
                pass
            return raw

        def _replace(match: re.Match[str]) -> str:
            var_name = match.group(1)
            default_val = match.group(2)
            if var_name in environ:
                return environ[var_name]
            if default_val is not None:
                return default_val
            return ""

        return ENV_PATTERN.sub(_replace, value)

    if isinstance(value, dict):
        return {k: interpolate_env(v, environ) for k, v in value.items()}

    if isinstance(value, list):
        return [interpolate_env(v, environ) for v in value]

    return value


def save_profile(path: Path | str, entries: list[dict[str, Any]]) -> None:
    """Save component configuration entries to a YAML file."""
    p = Path(path).resolve()
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        yaml.safe_dump(entries, f, sort_keys=False)


def _normalise_row(item: Any) -> dict[str, Any]:
    """Translate one raw profile row into EntryOptions shape; fail loud on ambiguity."""
    if isinstance(item, str):
        return {"name": item}
    if not isinstance(item, dict):
        raise TypeError(
            f"Profile entry must be a string or dict, got {type(item).__name__}"
        )
    row = dict(item)
    # "target" is the legacy alias for "name".
    if "target" in row:
        if "name" in row and row["name"] != row["target"]:
            raise ValueError("entry has conflicting 'name' and 'target'")
        row.setdefault("name", row.pop("target"))
    # Legacy group shape: "entries" key -> "config" list + group flag.
    if "entries" in row:
        if "config" in row:
            raise ValueError("entry has both 'entries' and 'config'")
        row["config"] = row.pop("entries")
        row.setdefault("name", "group")
        row["group"] = True
    if "name" not in row:
        raise ValueError("entry must specify 'name'")
    # Legacy isolate list -> dict form (each service entry-local).
    if isinstance(row.get("isolate"), list):
        row["isolate"] = {name: True for name in row["isolate"]}
    # Group children are entry rows too: normalise them recursively so a
    # legacy nested profile mounts at every depth.
    if row.get("group") and isinstance(row.get("config"), list):
        row["config"] = [_normalise_row(child) for child in row["config"]]
    return row


class LoaderService(Service, EntryTree):
    """Configuration-driven loader service; the loader IS the entry tree."""

    # Single home for unwrap_exports is anima.tree; this is the public alias.
    unwrap_exports = staticmethod(_tree_unwrap_exports)

    def __init__(self, ctx: Context, config: dict[str, Any] | None = None) -> None:
        cfg = dict(config or {})
        # 1. Tree first: sets self.ctx to the extended child ctx, plus
        #    root/store/owner/owner_fiber/builtins.
        EntryTree.__init__(self, ctx, loader=self)
        tree_ctx = self.ctx
        # 2. Service second, on the ORIGINAL ctx: registers "loader" where
        #    consumers look for it. This overwrites self.ctx — restore it next.
        Service.__init__(self, ctx, "loader")
        self.ctx = tree_ctx
        self.service_ctx = ctx
        # Mirrored entry services: (disposer, value, impl) per name.
        # See _remirror_service.
        self._mirrors: dict[str, tuple[Disposer, Any, Any]] = {}
        self.config = cfg
        self.filename = Path(cfg["filename"]).resolve() if cfg.get("filename") else None
        self.enable_logs = bool(cfg.get("enableLogs", False))
        self.builtins = dict(BUILTIN_ALIASES)
        if cfg.get("baseUrl"):
            self.ctx.baseUrl = cfg["baseUrl"]
        # 3. Hooks, registered on the ORIGINAL ctx.
        ctx.on("internal/commit", self._on_commit)
        ctx.on(
            "loader/patch-context",
            self._on_patch_context,
            mode=EventMode.WATERFALL,
        )
        ctx.on("internal/plugin", self._on_internal_plugin)
        ctx.on("internal/service", self._on_internal_service)
        # 4. Bound check re-provide: reflect calls the stored check with no
        #    arguments, so a class-level def check(self) cannot work.
        ctx.reflect.provide("loader", self, check=self._loader_check)

    def write(self) -> None:
        """Root tree writes are no-ops; persistence is explicit save() only."""
        return

    def show_log(self, entry: Entry, type: str) -> None:
        """Log a loader lifecycle event for an entry, when logging is enabled."""
        if entry.options.get("group") or not self.enable_logs:
            return
        logger.info("%s plugin %s", type, entry.options.get("name"))

    def locate(self, fiber: Fiber | None = None) -> str | None:
        """Return the loader entry id that owns fiber, if any."""
        if fiber is None:
            fiber = self.ctx.fiber
        while True:
            if fiber.entry is not None:
                return fiber.entry.id
            nxt = fiber.parent_fiber
            if nxt is None or nxt is fiber:
                return None
            fiber = nxt

    def exit(self) -> None:
        """Hook for hosts that can restart the process on full-reload requests."""
        return

    def _loader_check(self) -> bool:
        """False while config.await is set and the loader still has tasks."""
        cfg = self.resolve_config()
        return not (cfg.get("await") and self.get_tasks())

    def load(
        self,
        config_source: str | Path | list[Any] | dict[str, Any],
        *,
        env: dict[str, str] | None = None,
    ) -> None:
        """Parse configuration, interpolate environment variables, and mount components."""
        if isinstance(config_source, (str, Path)):
            path = Path(config_source).resolve()
            if not path.exists():
                raise FileNotFoundError(f"Profile config file not found: {path}")
            self.filename = path
            with open(path, encoding="utf-8") as f:
                raw_data = yaml.safe_load(f)
        else:
            raw_data = config_source

        custom_env = env
        if isinstance(raw_data, dict):
            if "env" in raw_data and isinstance(raw_data["env"], dict):
                custom_env = dict(env) if env else dict(os.environ)
                custom_env.update({str(k): str(v) for k, v in raw_data["env"].items()})
            entries_data = raw_data.get("components", [])
        elif isinstance(raw_data, list):
            entries_data = raw_data
        else:
            raise TypeError("Profile configuration must be a list or dict")

        interpolated_entries = interpolate_env(entries_data, env=custom_env)
        if not isinstance(interpolated_entries, list):
            raise TypeError("Profile configuration components must resolve to a list")

        entries = [_normalise_row(item) for item in interpolated_entries]
        # Scheduled on the service ctx (not the tree ctx) so the established
        # `load(); await ctx.flush()` contract settles the load: a parent
        # flush does not observe tasks scheduled on child contexts, and the
        # tree ctx is a child of the ctx callers flush.
        self.service_ctx._schedule_task(self._load_entries(entries))

    async def _load_entries(self, entries: list[dict[str, Any]]) -> None:
        """Reconcile the root group with the loaded rows, then settle the mounts."""
        await self.root.update(entries)
        await self.await_all()
        await self.ctx.flush()

    async def enable(self, entry_id: str) -> None:
        """Enable and mount a previously disabled entry."""
        entry = self.resolve(entry_id)
        if entry.options.get("disabled"):
            await self.update(entry_id, {"disabled": False})
        await self.await_all()

    async def disable(self, entry_id: str) -> None:
        """Disable and unload an active entry."""
        entry = self.resolve(entry_id)
        if not entry.options.get("disabled"):
            await self.update(entry_id, {"disabled": True})
        # The dispose is scheduled on the entry ctx by Entry.update; flush
        # that ctx so the disposal is observed before returning. await_all
        # alone cannot see a scheduled dispose whose task has not started.
        await entry.ctx.flush()
        await self.await_all()

    async def update(
        self,
        entry_id: str,
        new_config: dict[str, Any],
        parent: str | None | Any = _UNSET,
        position: float | None = None,
    ) -> Entry:
        """Apply a partial options update to an entry (None deletes keys).

        The argument is a partial OPTIONS dict, not a whole config: pass
        {"config": {...}} for a whole-config replacement. An optional
        parent move follows the EntryTree.update tri-state.
        """
        return await EntryTree.update(self, entry_id, new_config, parent, position)

    def save(self, path: Path | str | None = None) -> None:
        """Persist current configuration entries to disk."""
        target_path = Path(path).resolve() if path else self.filename
        if target_path is None:
            raise ValueError("No destination path specified to save profile")
        save_profile(target_path, self.root.data)

    def _on_commit(self, fiber: Fiber, config: Any) -> None:
        """Write a runtime config change back into the owning entry's options.

        Persistence is the tree's job; for the root loader tree write() is
        a no-op, so runtime changes are memory-only until explicit save().
        """
        entry = fiber.entry
        if entry is None:
            return
        # Child plugins of an entry must not overwrite the entry's config.
        # Vacuous in anima: child fibers carry entry=None (unlike the
        # oracle, where the entry marker is inherited through the ctx),
        # so the entry-None return above already subsumes this guard.
        if (  # pragma: no cover
            fiber.parent_fiber is not None and fiber.parent_fiber.entry is entry
        ):
            return
        entry.options["config"] = config
        entry.parent.tree.write()
        self.show_log(entry, "reload")

    async def _on_patch_context(self, entry: Entry, next_fn: Any) -> Any:
        """Apply an entry's isolate/intercept options, then delegate down the chain."""
        # Step 1: build the new isolate map from the parent group's map,
        # then apply this entry's options.
        new_map = dict(entry.parent.ctx._isolated_services)
        for name, label in (entry.options.get("isolate") or {}).items():
            if label is True:
                new_map[name] = f"__entry__:{entry.id}:{name}"
            elif isinstance(label, str):
                new_map[name] = label
            else:
                raise ValueError(f"isolate[{name!r}] must be true or a label string")
        old_map = dict(entry.ctx._isolated_services)
        entry.ctx._isolated_services.clear()
        entry.ctx._isolated_services.update(new_map)
        # Intercept: replace the entry ctx's map wholesale with the entry's
        # own intercept dict (parent-chain resolution supplies inheritance).
        entry.ctx._intercept_configs = dict(entry.options.get("intercept") or {})
        # Notify when the isolate map actually changed, so dependent fibers
        # re-resolve through the epoch machinery.
        changed = {
            k for k in set(new_map) | set(old_map) if new_map.get(k) != old_map.get(k)
        }
        if changed:
            entry.ctx.reflect.notify(sorted(changed))
        res = next_fn()
        if inspect.isawaitable(res):
            return await res
        return res

    def _on_internal_plugin(self, fiber: Fiber) -> None:
        """Mark an entry disabled when its fiber self-disposes."""
        # Case 1: fiber is created (uid set) — only disposals are of interest.
        if fiber.uid is not None:
            return
        # Case 2: fiber is not tracked by the loader.
        entry = fiber.entry
        if entry is None:
            return
        # Case 3: child plugin under the entry (not the entry's root fiber).
        if fiber.parent_fiber is not None and fiber.parent_fiber.entry is entry:
            return
        # Loader-driven disposal: EntryGroup.remove marks the entry before
        # scheduling its dispose, so a removal or stop never masquerades
        # as a self-dispose and poisons the retained row with disabled
        # (case 6 only covers entries already disabled). A disposal
        # cascading from an unloading ancestor — e.g. a group teardown
        # disposing its children — is loader-driven too.
        if entry._loader_disposing:
            return
        ancestor = fiber.parent_fiber
        while ancestor is not None:
            if ancestor.state == FiberState.UNLOADING:
                return
            ancestor = ancestor.parent_fiber
        # Case 4: fiber is disposed on behalf of plugin deletion (e.g. HMR):
        # the runtime no longer holds the callback. The runtime lives on
        # the fiber's own ctx registry (registries are per-context), so
        # that registry — not the tree ctx's — is the faithful predicate.
        # get and has agree here: both follow alias chains.
        if fiber.ctx.registry.get(fiber.target) is None:
            return
        # Case 5: the entry's tree is being disposed. A None owner fiber
        # means no tree-owner fiber exists (loader constructed directly),
        # so there is no tree teardown to guard against — fall through.
        owner_fiber = entry.parent.tree.owner_fiber
        if owner_fiber is not None and (
            owner_fiber.uid is None or owner_fiber.state == FiberState.UNLOADING
        ):
            return
        self.show_log(entry, "unload")
        # Case 6: fiber is disposed by loader behaviour (inject checker,
        # config update, ancestor disable) — already disabled, nothing to do.
        if entry.disabled:
            return
        entry.options["disabled"] = True
        entry.parent.tree.write()

    def _on_internal_service(self, name: str, _value: Any) -> None:
        """Mirror an entry-provided service onto the mounting ctx."""
        self._remirror_service(name)

    def _remirror_service(self, name: str) -> None:
        """Keep the mounting ctx's view of one entry-provided service current.

        Entries mount on private entry ctxs, so a service they provide is
        invisible from the mounting ctx and from sibling entries — unlike
        the pre-P1-4 loader, which mounted entries on the mounting ctx,
        and unlike the oracle, whose reflect store is shared across the
        whole context tree. The mirror restores that visibility: the
        last ACTIVE entry providing a non-isolated `name` is re-provided
        on the mounting ctx (last wins, matching the old loader's silent
        overwrite). Isolated names are never mirrored. A service provided
        directly on the mounting ctx owns its slot and is never clobbered.
        The mirror disposer is tracked here and released when no entry
        provides the name anymore; `provide` also registers it on the
        providing fiber's effects, so an entry unload removes the mirror.
        """
        store = self.service_ctx.reflect.store
        tracked = self._mirrors.get(name)
        current_impl = store.get(name)
        if current_impl is not None and (
            tracked is None or tracked[2] is not current_impl
        ):
            # Owned by a direct provide on the mounting ctx: never
            # clobber it. Drop any stale mirror record so the mirror is
            # re-applied if the direct provide later goes away.
            if tracked is not None:
                del self._mirrors[name]
            return
        candidate = None
        for entry in self.entries():
            if name in entry.ctx._isolated_services:
                continue
            impl = entry.ctx.reflect.store.get(name)
            if impl is None:
                continue
            provider = impl.fiber if impl.fiber is not None else impl.ctx.fiber
            if provider.state != FiberState.ACTIVE:
                continue
            candidate = impl.value
        changed = False
        if candidate is None:
            if tracked is not None:
                tracked[0]()
                del self._mirrors[name]
                changed = True
        else:
            if tracked is None or tracked[1] is not candidate:
                if tracked is not None:
                    tracked[0]()
                disposer = self.service_ctx.reflect.provide(name, candidate)
                self._mirrors[name] = (
                    disposer,
                    candidate,
                    self.service_ctx.reflect.store.get(name),
                )
                changed = True
        if changed:
            # The mirror's own notify refreshes only fibers in the
            # mounting registry, but dependents live in entry registries:
            # fan the change out so an injecting entry fiber re-checks
            # its deps (and goes PENDING again when the service goes away).
            for entry in self.entries():
                entry.ctx.reflect.notify([name])


def load_profile(
    ctx: Context,
    config_source: str | Path | list[Any] | dict[str, Any],
    *,
    env: dict[str, str] | None = None,
) -> LoaderService:
    """Load a profile of component configurations into a Context."""
    filename = config_source if isinstance(config_source, (str, Path)) else None
    loader = LoaderService(ctx, {"filename": filename})
    loader.load(config_source, env=env)
    return loader
