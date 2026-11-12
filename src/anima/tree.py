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
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar, TypedDict, TypeGuard, cast

from anima.core import Context, Fiber, FiberState, is_js_expr
from anima.events import EventMode
from anima.jsexpr import evaluate as evaluate_js_expr
from anima.registry import resolve_inject
from anima.schema import _UNDEFINED, Schema, deep_equal
from anima.volatile import update_volatile, volatile_entries

if TYPE_CHECKING:
    from anima.loader import LoaderService

logger = logging.getLogger("anima.loader")

# Distinct from None: "argument not supplied" for EntryTree.update's parent.
_UNSET: Any = object()


def _is_schema(value: Any) -> bool:
    """Whether ``value`` carries the schema seam.

    The local seam vendor is ``anima``; anything without it compares by
    strict raw equality.
    """
    seam = getattr(value, "~standard", None)
    return seam is not None and getattr(seam, "vendor", None) == "anima"


def _is_record(value: Any) -> TypeGuard[dict[str, Any]]:
    """Whether ``value`` is a plain record (not a JsExpr node)."""
    return isinstance(value, dict) and not is_js_expr(value)


def equal_except_volatile(previous: Any, next_config: Any, schema: Any) -> bool:
    """Compare two raw configs, treating volatile fields as equal.

    Volatile fields at fixed object paths compare equal regardless of
    value; an absent object compares equal to its schema default; schema
    backedges, JsExpr/opaque values, unknown fields, and non-object
    schemas keep strict raw equality. Nothing is evaluated, validated,
    or mutated.

    Moved here from ``anima.include`` in P1-7: ``Include`` must subclass
    ``EntryTree``, and this module is the only runtime consumer, so the
    function lives with the loader tree (the oracle's diff lives in the
    loader package). ``anima.include`` re-exports it unchanged.
    """
    if not _is_schema(schema):
        return deep_equal(previous, next_config, strict=True)

    ancestors: set[int] = set()

    def equal(a: Any, b: Any, node: Schema | None) -> bool:
        if node is not None and node._volatile:
            return True
        if (
            node is None
            or node.type_name != "object"
            or node._properties is None
            or id(node) in ancestors
        ):
            return deep_equal(a, b, strict=True)
        default = node._default if node._default is not _UNDEFINED else None
        left = a if a is not None else default
        right = b if b is not None else default
        if not _is_record(left) or not _is_record(right):
            return deep_equal(left, right, strict=True)
        props = node._properties
        ancestors.add(id(node))
        try:
            return all(
                equal(left.get(key), right.get(key), props.get(key))
                for key in set(left) | set(right)
            )
        finally:
            ancestors.discard(id(node))

    return equal(previous, next_config, schema)


def _members(entry: Entry, tree: EntryTree) -> list[Entry]:
    """Return an entry plus its subgroup members that live in ``tree``.

    Mirrors the oracle's ``members()``: a group entry contributes the
    entries of its subgroup (recursively) when the subgroup belongs to
    the same tree; members of another tree are not collected.
    """
    result = [entry]
    subgroup = entry.subgroup
    if subgroup is None or subgroup.tree is not tree:
        return result
    for row in subgroup.data:
        row_id = row.get("id")
        member = tree.store.get(row_id) if row_id else None
        if member is not None and member.parent is subgroup:
            result.extend(_members(member, tree))
    return result


@dataclass
class EntryChange:
    """One tree-side mutation, reported to ``EntryTree.commit()``.

    ``options`` absent: the entry was removed from ``group``.
    ``legacy`` absent: the entry was created in ``group``. Both present:
    the entry was updated and now lives in ``group``; ``from_group`` is
    the group it left if the update moved it within the same tree. A
    move into another tree is reported as a removal to the source tree
    and a creation to the target tree.

    ``from_group`` is named so because ``from`` is a Python keyword.
    """

    id: str
    group: EntryGroup
    from_group: EntryGroup | None = None
    options: dict[str, Any] | None = None
    legacy: dict[str, Any] | None = None


BUILTIN_ALIASES: dict[str, str] = {
    "group": "anima.tree:Group",
    "include": "anima.include:Include",
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


def _in_fiber_scope(owner: Context, scope: Context) -> bool:
    """Whether ``owner`` is ``scope`` or descends from it.

    The oracle's volatile-update filter tests ``owner.fiber is fiber``;
    anima never binds a non-root fiber to its context, so scope ancestry
    is the equivalent test: listeners registered on the fiber's context
    or a descendant of it belong to that fiber's scope.
    """
    ctx: Context | None = owner
    while ctx is not None:
        if ctx is scope:
            return True
        ctx = ctx._parent
    return False


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
        entry_id = self.ensure_id(options)
        group.data.insert(_normalize_position(position, len(group.data)), options)
        group.tree.write()
        self.commit(EntryChange(id=entry_id, group=group, options=options))
        return await group.create(options)

    def remove(self, entry_id: str) -> None:
        """Stop and remove an entry from its parent group."""
        entry = self.resolve(entry_id)
        group = entry.parent
        legacy = dict(entry.options)
        group.remove(entry_id)
        group.tree.write()
        group.tree.commit(EntryChange(id=legacy["id"], group=group, legacy=legacy))

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
        legacy = dict(entry.options)
        cross_tree = False
        if parent is not _UNSET:
            target = self.resolve_group(parent)
            # A move into the entry's own subtree would detach the
            # group from itself: reject loud instead of recursing (or
            # silently dropping the entries). A legitimate move can
            # never target a descendant.
            if target.owner is not None and target.owner in _members(
                entry, source.tree
            ):
                raise ValueError(
                    f"cannot move entry {entry_id} into its own descendant "
                    f"group {parent}"
                )
            if target.tree is not source.tree:
                # Cross-tree move: migrate the entry and its subgroup
                # members into the target tree's store first, so the
                # target tree adopts them instead of creating new ones.
                members = _members(entry, source.tree)
                for member in members:
                    member_id = member.options["id"]
                    if member_id in target.tree.store:
                        raise ValueError(
                            f"entry {member_id} already exists in the target tree"
                        )
                source.unlink(entry.options)
                target.data.insert(
                    _normalize_position(
                        position if position is not None else math.inf,
                        len(target.data),
                    ),
                    entry.options,
                )
                entry.parent = target
                for member in members:
                    member_id = member.options["id"]
                    del source.tree.store[member_id]
                    target.tree.store[member_id] = member
                    if (
                        member.subgroup is not None
                        and member.subgroup.tree is source.tree
                    ):
                        member.subgroup.tree = target.tree
                target.tree.write()
                cross_tree = True
            else:
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
        if parent is not _UNSET:
            # A move replays the row's config into the fiber, so sync
            # each moved group member's row config from its subgroup
            # data first: the fiber's config is a copy, so subgroup
            # membership never flows back to the row on its own, and
            # without this the reconcile would drop the children.
            for member in _members(entry, entry.parent.tree):
                if member.subgroup is not None:
                    member.options["config"] = list(member.subgroup.data)
        source.tree.write()
        # The merge runs synchronously before the first await, so the
        # change is fully visible to commit() once the call returns —
        # mirroring the oracle, where Entry.update assigns the new
        # options before its first await and EntryTree.update commits
        # synchronously right after calling it. Committing before
        # awaiting the fiber restart journals the intended state: a
        # stale reconcile applying afterwards can no longer clobber the
        # merge and empty the diff, and a later fiber failure does not
        # un-commit it.
        premerged_legacy = entry._merge_options(options)
        if cross_tree:
            # A move across trees is a removal from the source tree and
            # a creation in the target tree.
            source.tree.commit(
                EntryChange(id=legacy["id"], group=source, legacy=legacy)
            )
            entry.parent.tree.commit(
                EntryChange(
                    id=legacy["id"],
                    group=entry.parent,
                    options=entry.options,
                )
            )
        else:
            from_group = source if entry.parent is not source else None
            entry.parent.tree.commit(
                EntryChange(
                    id=legacy["id"],
                    group=entry.parent,
                    from_group=from_group,
                    options=entry.options,
                    legacy=legacy,
                )
            )
        await entry.update(
            options, create=False, force=True, _premerged_legacy=premerged_legacy
        )
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

    @abstractmethod
    def commit(self, change: EntryChange) -> None:
        """Report a tree-side mutation to the persistence layer.

        Called synchronously by ``create`` / ``remove`` / ``update`` and
        by the loader's ``internal/commit`` handler once a mutation has
        been applied to the entry options / group data. In-memory trees
        implement this as a no-op; file-backed trees record the change
        for write-back.
        """
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
        """Evaluate a JS expression against this entry's context."""
        return evaluate_js_expr(self.ctx, expr)

    async def refresh(self) -> None:
        """(Re-)mount this entry when it has no fiber and is not disabled."""
        if self.fiber is not None:
            return
        if self.disabled:
            return
        await self.init()

    def _merge_options(
        self, options: dict[str, Any], create: bool = False
    ) -> dict[str, Any]:
        """Apply an options merge synchronously; return the pre-merge options.

        A None value deletes its key; it is never stored. ``create``
        adopts the given dict as the options object; otherwise the merge
        mutates the live options in place, so the group data row stays
        the identical object.
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
        return legacy

    async def update(
        self,
        options: dict[str, Any],
        create: bool = False,
        force: bool = False,
        _premerged_legacy: dict[str, Any] | None = None,
    ) -> None:
        """Merge new options, restart as needed, and persist through the parent tree.

        A None value deletes its key; it is never stored.
        ``_premerged_legacy`` is the pre-merge options when the caller
        already merged (EntryTree.update): the merge is skipped and the
        change is diffed against it, so the fiber restart still sees
        the real change.
        """
        if _premerged_legacy is None:
            legacy = self._merge_options(options, create=create)
        else:
            legacy = _premerged_legacy

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
            # Volatile-only fast path: a change confined to volatile
            # fields commits into the running fiber without a restart.
            # The fiber must be ACTIVE and the entry ctx must still be
            # parented to its group ctx (a short-circuited
            # loader/patch-context waterfall leaves it on the loader
            # ctx, which disables the fast path).
            fiber = self.fiber
            volatile_only = (
                changes == ["config"]
                and fiber.state is FiberState.ACTIVE
                and self.ctx._parent is self.parent.ctx
                and equal_except_volatile(
                    legacy.get("config"),
                    self.options.get("config"),
                    fiber.config_schema,
                )
            )
            if volatile_only:
                raw = self.options.get("config")
                fiber.raw_config = dict(raw) if isinstance(raw, dict) else raw
            pending = [] if volatile_only and await self._commit_volatile() else changes
            if not pending and not force:
                return
            self.ctx.emit("loader/partial-dispose", self, legacy, True)
            await self._patch_context(pending)
        else:
            await self.init()

    async def _commit_volatile(self) -> bool:
        """Commit a volatile-only raw config change into the live fiber.

        Re-validates the candidate; refuses it (leaving the running
        references unchanged) when validation fails, and reports False
        when an ordinary effective value changed so the caller runs the
        ordinary update lifecycle. Otherwise commits the changed
        volatile snapshots and emits ``loader/volatile-update`` with the
        changed paths, scoped to this fiber's listeners.
        """
        # The caller guarantees a live fiber (the volatile-only path is
        # only reached with one); the cast mirrors the oracle's
        # non-null assertion.
        fiber = cast(Fiber, self.fiber)
        refs = volatile_entries(fiber.config)
        if not refs:
            return True
        raw = self.options.get("config")
        try:
            candidate = await fiber.resolve_config(raw)
        except Exception as exc:  # noqa: BLE001 - any resolve failure refuses the commit
            logger.warning(
                "volatile config update failed for %s", self.options.get("id")
            )
            logger.warning("%s", exc)
            return True
        if not deep_equal(fiber.config, candidate, strict=True):
            logger.debug(
                "ordinary config values of %s changed with its volatile "
                "values; applying the ordinary update",
                self.options.get("id"),
            )
            return False
        paths: list[list[str]] = []
        for item in refs:
            path = item["path"]
            ref = item["ref"]
            source: Any = candidate
            for key in path:
                source = source[key]
            if deep_equal(ref.get(), source.get(), strict=True):
                continue
            update_volatile(ref, source)
            paths.append(list(path))
        if not paths:
            return True
        # Scope the event to this fiber's listeners. The filtered ctx is
        # detached afterwards: filter() registers it on the parent, and
        # a throwaway ctx per emit would leak one per volatile update.
        scoped = fiber.ctx.filter(lambda owner: _in_fiber_scope(owner, fiber.ctx))
        try:
            scoped.emit("loader/volatile-update", paths)
        finally:
            fiber.ctx._children.remove(scoped)
        return True

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
