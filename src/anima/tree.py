"""Entry tree for the loader: EntryTree, Entry, EntryGroup and Group.

A tree owns a flat store of entries keyed by short id. Groups are entries
whose config is the child-row list; group children live in the same shared
store, so nested groups resolve by short id. Colon-prefixed ids exist only
for entries inside a nested subtree (a separate EntryTree owned by an
entry), never for group children.
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import logging
import math
import secrets
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Iterator
from typing import TYPE_CHECKING, Any, ClassVar, TypedDict, cast

from anima.core import Context, Fiber, FiberState, is_js_expr
from anima.events import EventMode
from anima.registry import resolve_inject
from anima.schema import deep_equal

if TYPE_CHECKING:
    from anima.loader import LoaderService

logger = logging.getLogger("anima.loader")

# Distinct from None: "argument not supplied" for EntryTree.update's parent.
_UNSET: Any = object()

BUILTIN_ALIASES: dict[str, str] = {
    "group": "anima.tree:Group",
    "logger": "anima.logger:LoggerService",
    "timer": "anima.timer:TimerService",
    "watcher": "anima.watcher:WatcherService",
    "hmr": "anima.hmr:HmrService",
    "pkg": "anima.pkg:PkgService",
}


def unwrap_exports(exports: Any) -> Any:
    """Get the component exported by a module namespace.

    Mirrors unwrapExports: handles CommonJS (module.exports), ES modules
    (__es_module + default), and plain values. The flag is spelled
    __es_module here (the oracle spells it __esModule); Python modules
    never carry either flag, so it only appears on synthetic namespace
    objects in tests. This is the single home for the function; the
    loader exposes it as LoaderService.unwrap_exports.
    """
    if exports is None:
        return exports
    default_val = getattr(exports, "default", None)
    if default_val is not None:
        exports = default_val
    if not getattr(exports, "__es_module", False):
        return exports
    default_val = getattr(exports, "default", None)
    if default_val is not None:
        return default_val
    return exports


def resolve_component_target(target_str: str) -> Any:
    """Resolve a component target from a module path, dot-string, or builtin alias."""
    if target_str in BUILTIN_ALIASES:
        target_str = BUILTIN_ALIASES[target_str]

    if ":" in target_str:
        mod_name, attr_name = target_str.split(":", 1)
    elif "." in target_str:
        parts = target_str.rsplit(".", 1)
        mod_name, attr_name = parts[0], parts[1]
    else:
        mod_name = target_str
        attr_name = None

    mod = importlib.import_module(mod_name)
    mod = unwrap_exports(mod)
    if attr_name:
        return getattr(mod, attr_name)
    if hasattr(mod, "apply"):
        return mod.apply
    return mod


def _normalize_position(position: Any, length: int) -> int:
    """Normalise an insert position to splice semantics.

    None or infinity appends; negatives count from the end; out-of-range
    positions clamp to the ends.
    """
    if position is None or position == math.inf:
        return length
    pos = int(position)
    if pos < 0:
        pos = max(length + pos, 0)
    return min(pos, length)


def _sort_options_keys(options: dict[str, Any]) -> None:
    """Sort an options dict in place: id, name first; config last; rest alphabetical."""

    def _rank(key: str) -> tuple[int, str]:
        if key == "id":
            return (0, "")
        if key == "name":
            return (1, "")
        if key == "config":
            return (3, "")
        return (2, key)

    ordered = {key: options[key] for key in sorted(options, key=_rank)}
    options.clear()
    options.update(ordered)


class EntryOptions(TypedDict, total=False):
    """Serialized plugin entry options stored in loader config files."""

    id: str
    name: str
    config: Any
    group: bool | None
    disabled: bool | None
    inject: dict[str, Any] | list[str] | None
    intercept: dict[str, dict[str, Any]] | None
    isolate: dict[str, bool | str] | None


class EntryTree(ABC):
    """Mutable tree of loader entries. Persistence is supplied by subclasses."""

    sep: ClassVar[str] = ":"

    def __init__(self, ctx: Context, loader: LoaderService | None = None) -> None:
        self.ctx = ctx.extend()
        object.__setattr__(self.ctx, "baseUrl", ctx.baseUrl)
        self.store: dict[str, Entry] = {}
        self.builtins: dict[str, str] = {}
        self.enable_logs = False
        self.root = EntryGroup(self.ctx, self)
        self.loader: LoaderService | None = loader
        fiber = ctx.fiber
        self.owner: Entry | None = fiber.entry
        self.owner_fiber: Fiber | None = fiber if fiber.uid != 0 else None
        if self.owner is not None:
            self.owner.subtree = self

    @property
    def context(self) -> Context:
        """Return the tree context."""
        return self.ctx

    def entries(self) -> Iterator[Entry]:
        """Iterate entries in this tree and any nested subtrees."""
        for entry in list(self.store.values()):
            yield entry
            if entry.subtree is not None:
                yield from entry.subtree.entries()

    def get_tasks(self) -> list[Awaitable[Any]]:
        """Return pending import and lifecycle tasks owned by this tree."""
        tasks: list[Awaitable[Any]] = []
        for entry in self.entries():
            task: Awaitable[Any] | None = None
            if entry._init_task is not None:
                task = entry._init_task
            elif entry.fiber is not None:
                task = entry.fiber.inertia
            if task is not None:
                tasks.append(task)
        return tasks

    async def await_all(self) -> None:
        """Wait until this tree has no pending import or lifecycle tasks."""
        while True:
            tasks = self.get_tasks()
            if not tasks:
                return
            await asyncio.gather(*tasks, return_exceptions=True)

    def ensure_id(self, options: dict[str, Any]) -> str:
        """Assign a random 8-hex id to options lacking one, mutating it in place."""
        if not options.get("id"):
            while True:
                candidate = secrets.token_hex(4)
                if candidate not in self.store:
                    break
            options["id"] = candidate
        entry_id: str = options["id"]
        return entry_id

    def resolve(self, entry_id: str) -> Entry:
        """Resolve an entry by id, including nested ids separated by sep."""
        parts = entry_id.split(EntryTree.sep)
        tree: EntryTree = self
        final = parts.pop()
        for part in parts:
            nxt = tree.store.get(part)
            subtree = nxt.subtree if nxt is not None else None
            if subtree is None:
                raise ValueError(f"cannot resolve entry {entry_id}")
            tree = subtree
        entry = tree.store.get(final)
        if entry is None:
            raise ValueError(f"cannot resolve entry {entry_id}")
        return entry

    def resolve_group(self, group_id: str | None) -> EntryGroup:
        """Resolve a group by id; None is the root group."""
        if group_id is None:
            return self.root
        entry = self.resolve(group_id)
        if entry.subgroup is None:
            raise ValueError(f"entry {group_id} is not a group")
        return entry.subgroup

    async def create(
        self,
        options: dict[str, Any],
        parent: str | None = None,
        position: float = math.inf,
    ) -> str:
        """Create an entry in the root group or a nested group; return its full id."""
        group = self.resolve_group(parent)
        group.data.insert(_normalize_position(position, len(group.data)), options)
        group.tree.write()
        return await group.create(options)

    def remove(self, entry_id: str) -> None:
        """Stop and remove an entry from its parent group."""
        entry = self.resolve(entry_id)
        entry.parent.remove(entry_id)
        entry.parent.tree.write()

    async def update(
        self,
        entry_id: str,
        options: dict[str, Any],
        parent: str | None | Any = _UNSET,
        position: float | None = None,
    ) -> Entry:
        """Update an entry and optionally move it to another group."""
        entry = self.resolve(entry_id)
        source = entry.parent
        if parent is not _UNSET:
            target = self.resolve_group(parent)
            source.unlink(entry.options)
            target.data.insert(
                _normalize_position(
                    position if position is not None else math.inf,
                    len(target.data),
                ),
                entry.options,
            )
            target.tree.write()
            entry.parent = target
        source.tree.write()
        await entry.update(options, create=False, force=True)
        return entry

    def import_(self, name: str, outer_stack: list[str] | None = None) -> Any:
        """Import a plugin from a specifier or builtin alias."""
        if name in self.builtins:
            name = self.builtins[name]
        return resolve_component_target(name)

    @abstractmethod
    def write(self) -> None:
        """Persist current tree state. In-memory trees may implement this as a no-op."""
        raise NotImplementedError


class Entry:
    """One configured plugin node inside an EntryTree."""

    key: ClassVar[str] = "anima.entry"

    def __init__(self, loader: LoaderService) -> None:
        self.loader = loader
        self.ctx = loader.ctx.extend()
        self.fiber: Fiber | None = None
        self.module_namespace: Any = None
        self.options: dict[str, Any] = {}
        self.subgroup: EntryGroup | None = None
        self.subtree: EntryTree | None = None
        self._init_task: asyncio.Task[None] | None = None
        self.parent: EntryGroup = None  # type: ignore[assignment]
        # Set by EntryGroup.remove before it schedules this entry's
        # dispose: the disposal is loader-driven, not a self-dispose.
        # A fresh mount clears it; removed entries leave the store, so a
        # set flag can never suppress a genuine self-dispose.
        self._loader_disposing = False
        self.ctx.emit("loader/entry-init", self)

    @property
    def context(self) -> Context:
        """Return the entry context."""
        return self.ctx

    @property
    def id(self) -> str:
        """Short id, prefixed with the owning entry's full id when in a subtree."""
        short: str = self.options["id"]
        owner = self.parent.tree.owner
        if owner is None:
            return short
        return f"{owner.id}{EntryTree.sep}{short}"

    @property
    def disabled(self) -> bool:
        """True when this entry or any owning parent entry is disabled.

        A group entry is always enabled: disabling a group disables its
        descendants, never the group row itself.
        """
        if self.options.get("group"):
            return False
        entry: Entry | None = self
        while entry is not None:
            if self._disabled_of(entry.options):
                return True
            entry = entry.parent.owner
        return False

    def _disabled_of(self, options: dict[str, Any]) -> bool:
        raw = options.get("disabled")
        if is_js_expr(raw):
            return bool(self.evaluate(raw["__jsExpr"]))
        return bool(raw)

    def evaluate(self, expr: str) -> Any:
        """Evaluate a JS expression against this entry's context (P1-5)."""
        raise NotImplementedError("JsExpr evaluation lands in P1-5")

    async def refresh(self) -> None:
        """(Re-)mount this entry when it has no fiber and is not disabled."""
        if self.fiber is not None:
            return
        if self.disabled:
            return
        await self.init()

    async def update(
        self,
        options: dict[str, Any],
        create: bool = False,
        force: bool = False,
    ) -> None:
        """Merge new options, restart as needed, and persist through the parent tree.

        A None value deletes its key; it is never stored.
        """
        legacy = dict(self.options)
        if create:
            self.options = options
        else:
            for key, value in options.items():
                if value is None:
                    self.options.pop(key, None)
                else:
                    self.options[key] = value
        _sort_options_keys(self.options)

        if self.disabled:
            if self.fiber is not None:
                self.ctx._schedule_task(self.fiber.dispose())
            return

        if self.fiber is not None and self.fiber.uid is not None:
            changes = [
                key
                for key in {**self.options, **legacy}
                if not deep_equal(
                    self.options.get(key), legacy.get(key), key == "config"
                )
            ]
            # P1-6 insertion point: volatileOnly is computed here, from
            # changes == ['config'], fiber ACTIVE, prototype check, and
            # equalExceptVolatile; pending becomes [] when volatileOnly
            # and _commit_volatile() succeeds. Until then:
            pending = changes
            if not pending and not force:
                return
            self.ctx.emit("loader/partial-dispose", self, legacy, True)
            await self._patch_context(pending)
        else:
            await self.init()

    async def _patch_context(self, diff: list[str]) -> None:
        """Re-parent the entry ctx and, when the diff touches config, update the fiber."""

        async def _default(entry: Entry) -> None:
            old_parent = self.ctx._parent
            # Defensive guard for hand-built contexts: extend() always
            # registers the child on its parent, so this is always true.
            if (
                old_parent is not None and self.ctx in old_parent._children
            ):  # pragma: no cover
                old_parent._children.remove(self.ctx)
            self.ctx._parent = self.parent.ctx
            self.parent.ctx._children.append(self.ctx)
            if (
                self.fiber is not None
                and self.fiber.uid is not None
                and ("config" in diff or self.options.get("group"))
            ):
                cfg = self.options.get("config")
                if self.options.get("group"):
                    cfg = cfg if isinstance(cfg, list) else []
                elif not isinstance(cfg, dict):
                    cfg = {}
                await self.fiber.update(cfg, no_save=True)

        await self.ctx.waterfall("loader/patch-context", self, default_handler=_default)

    async def init(self) -> None:
        """Import and start the configured plugin if it is not already running."""
        try:
            if self._init_task is None:
                self._init_task = asyncio.get_running_loop().create_task(self._init())
            await self._init_task
        finally:
            self._init_task = None
            self.ctx._schedule_task(self._notify())

    async def _notify(self) -> None:
        """Notify dependents once this entry settles, on success and on failure."""
        try:
            if self.fiber is not None:
                await self.fiber.await_settle()
        except Exception:  # noqa: BLE001, S110 - notify runs on both outcomes
            # Notify runs on both outcomes; a startup failure is already
            # stored on the fiber and must not fail the notification.
            pass
        if not self.loader.get_tasks():
            self.ctx.reflect.notify(["loader"])

    async def _init(self) -> None:
        try:
            namespace = self.parent.tree.import_(
                self.options["name"], self.get_outer_stack()
            )
        except Exception:
            logger.exception(
                "failed to import entry %s:\n%s",
                self.options.get("name"),
                "\n".join(self.get_outer_stack()),
            )
            return
        finally:
            self._init_task = None
        plugin = self.loader.unwrap_exports(namespace)
        await self._patch_context([])
        self.loader.show_log(self, "apply")
        config = self.options.get("config")
        if config is None:
            config = {}
        fiber = self.ctx.registry.use(plugin, config)
        fiber.entry = self
        # A fresh mount clears any loader-disposing mark.
        self._loader_disposing = False
        # Entry-level inject merge: the merge runs synchronously after
        # registry.use returns and before any await, so the scheduled
        # load sees the merged set.
        inject_opt = self.options.get("inject")
        if inject_opt:
            merged = resolve_inject(inject_opt, {})
            names = list(merged)
            fiber.inject = names + [n for n in fiber.inject if n not in names]
            entry_cfg = {k: v for k, v in merged.items() if isinstance(v, dict)}
            base = fiber.inject_config or {}
            fiber.inject_config = {**base, **entry_cfg}
        self.fiber = fiber
        self.module_namespace = namespace

    def get_outer_stack(self) -> list[str]:
        """Return the import-failure stack through owning entries."""
        lines: list[str] = []
        entry: Entry | None = self
        while entry is not None:
            base_url = entry.parent.tree.ctx.baseUrl or ""
            lines.append(f"    at {base_url}#{entry.options['id']}")
            entry = entry.parent.owner
        return lines


class EntryGroup:
    """Runtime owner for a list of child loader entries."""

    key: ClassVar[str] = "anima.group"

    def __init__(self, ctx: Context, tree: EntryTree) -> None:
        self.ctx = ctx
        self.tree = tree
        self.data: list[dict[str, Any]] = []
        entry = ctx.fiber.entry
        self.owner: Entry | None = entry
        if entry is not None:
            entry.subgroup = self

    @property
    def context(self) -> Context:
        """Return the group context."""
        return self.ctx

    @staticmethod
    def is_tree_carrier(component: Any) -> bool:
        """True when a component carries the tree-carrier marker (Group, Include)."""
        return bool(getattr(component, EntryGroup.key, False))

    async def create(self, options: dict[str, Any]) -> str:
        """Create (or re-parent) the entry for a row; return its full id."""
        entry_id = self.tree.ensure_id(options)
        entry = self.tree.store.get(entry_id)
        if entry is None:
            if self.tree.loader is None:
                raise RuntimeError("EntryTree has no loader")
            entry = Entry(self.tree.loader)
            self.tree.store[entry_id] = entry
        # Entry may be moved from another group: update the parent reference.
        entry.parent = self
        await entry.update(options, create=True, force=True)
        return entry.id

    def unlink(self, options: dict[str, Any]) -> None:
        """Remove a row object from this group's data by identity."""
        for i, row in enumerate(self.data):
            if row is options:
                del self.data[i]
                return

    def remove(self, entry_id: str, is_dispose: bool = False) -> None:
        """Dispose an entry's fiber, unlink its row, and drop it from the store."""
        entry = self.tree.store.get(entry_id)
        if entry is None:
            return
        if entry.fiber is not None:
            if entry.fiber.uid is not None:
                # Loader-driven disposal: mark first, so the
                # internal/plugin handler does not mistake it for a
                # self-dispose and poison the (retained) row with disabled.
                entry._loader_disposing = True
            self.ctx._schedule_task(entry.fiber.dispose())
        if not is_dispose:
            self.unlink(entry.options)
        del self.tree.store[entry_id]
        self.context.emit("loader/partial-dispose", entry, entry.options, False)

    async def update(self, config: list[dict[str, Any]]) -> None:
        """Replace data wholesale and reconcile entries, keeping fibers on reorder."""
        old_config = self.data
        self.data = config
        old_map = {row["id"]: row for row in old_config if "id" in row}
        new_map: dict[Any, dict[str, Any]] = {}
        for row in config:
            key = row.get("id") or object()
            new_map[key] = row
        ids = list({**old_map, **new_map})

        async def _apply(entry_id: Any) -> None:
            if entry_id in new_map:
                try:
                    await self.create(new_map[entry_id])
                except Exception:
                    logger.exception("Failed to create entry %s", entry_id)
            else:
                self.remove(entry_id)

        await asyncio.gather(*(_apply(i) for i in ids))

    def stop(self) -> None:
        """Dispose child fibers and drop store entries, but keep the data rows."""
        for options in list(self.data):
            entry_id = options.get("id")
            if entry_id is not None:
                self.remove(entry_id, is_dispose=True)


class Group(EntryGroup):
    """Component that mounts a nested loader entry group."""

    initial: ClassVar[list[dict[str, Any]]] = []

    def __init__(
        self, ctx: Context, config: list[dict[str, Any]] | None = None
    ) -> None:
        entry = ctx.fiber.entry
        if entry is None:
            raise RuntimeError("Group mounted outside an entry")
        super().__init__(ctx, entry.parent.tree)
        self.entry = entry
        self._name = entry.id
        self.config: list[dict[str, Any]] = config if config is not None else []
        ctx.on("internal/update", self._on_update, mode=EventMode.WATERFALL)

    async def init(self) -> None:
        """Register stop as the fiber disposer, then mount the child entries."""
        self.ctx.effect(lambda: self.stop)
        await self.update(self.config)

    async def update(self, config: list[dict[str, Any]]) -> None:
        """Reconcile the child list, keeping this component's config current."""
        self.config = config
        await super().update(config)

    async def _on_update(self, config: Any, next_fn: Any = None) -> Any:
        """Reconcile own list updates in place; pass other updates down the chain."""
        if isinstance(config, list) and self.entry.options.get("config") is config:
            # The identity check guarantees this is the group's own row list.
            rows = cast("list[dict[str, Any]]", config)
            await self.update(rows)
            return None
        if callable(next_fn):
            res = next_fn(config)
            if inspect.isawaitable(res):
                return await res
            return res
        return None

    def status(self) -> dict[str, Any]:
        """Return diagnostic status of child group entries."""
        entries: dict[str, dict[str, Any]] = {}
        for row in self.data:
            row_id = row.get("id")
            if row_id is None:
                continue
            entry = self.tree.store.get(row_id)
            if entry is None:
                entries[row_id] = {
                    "disabled": bool(row.get("disabled")),
                    "state": FiberState.DISPOSED.value,
                }
            else:
                entries[row_id] = {
                    "disabled": entry.disabled,
                    "state": (
                        entry.fiber.state.value
                        if entry.fiber is not None
                        else FiberState.DISPOSED.value
                    ),
                }
        return {"name": self._name, "entries": entries}


setattr(Group, EntryGroup.key, True)
