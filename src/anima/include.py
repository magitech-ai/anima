"""File-backed configuration inclusion, journaling, and patch routing.

Ports the upstream file-backed ``Include`` entry tree and its journal
module: an entry list backed by a YAML or JSON file, runtime patches
applied on top, and a journal that reconciles tree-side mutations with
file edits (the file wins every conflict) and drains them back to the
file. Patch-owned keys and patch-inserted entries are routed to the
patch list, never written into the file.

Two diffs live here and must not be conflated (see the design doc):

- ``diff_entries`` is the P1-6 schema-aware diff: volatile fields
  compare equal, absent objects compare to schema defaults, JsExpr
  nodes are opaque. It serves the loader's volatile fast path.
- ``diff_options`` is the journal's plain deep-equal diff, used only
  by journal ``record()``. A removed key yields ``None``: in a changes
  dict None deletes the key, it is never stored as a value.
"""

from __future__ import annotations

import asyncio
import copy
import errno
import inspect
import io
import json
import os
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from anima.core import Context, Service, is_js_expr
from anima.events import EventMode
from anima.jsexpr import dump_yaml, load_yaml
from anima.schema import deep_equal
from anima.tree import (
    EntryChange,
    EntryGroup,
    EntryTree,
    equal_except_volatile,
)

__all__ = [
    "Conflict",
    "EntryOwner",
    "FlatEntry",
    "Include",
    "IncludeError",
    "Journal",
    "JournalRecord",
    "PatchIndex",
    "StaleWriteError",
    "apply_changes",
    "apply_entry_patches",
    "apply_journal",
    "detach",
    "diff_entries",
    "diff_options",
    "ensure_ids",
    "ensure_insert_ids",
    "equal_except_volatile",
    "flatten",
    "merge",
    "merge_records",
    "place",
    "reconcile",
    "record",
    "route_journal",
]

# Distinct from None: "the tree did not relocate it" for a journal
# record's parent (None means the root group). The oracle spells this
# state `undefined`; Python has no `undefined`.
_PARENT_UNSET: Any = object()

_WRITE_RETRY_LIMIT = 10
_WRITE_RETRY_DELAY_MS = 20

_SUPPORTED_EXTENSIONS = {".json", ".yaml", ".yml"}


def _noop_warn(*args: Any) -> None:
    """Default warn sink: drop skipped-patch diagnostics."""


class IncludeError(Exception):
    """Raised when profile inclusion or patch resolution fails."""


class StaleWriteError(IncludeError):
    """The file changed under us between the pre-check and the rename."""


@dataclass
class JournalRecord:
    """One tree-side mutation that has not reached the file yet."""

    kind: str  # 'remove' | 'upsert'
    created: bool = False
    parent: Any = _PARENT_UNSET  # str | None | _PARENT_UNSET
    position: int = 0
    changes: dict[str, Any] = field(default_factory=dict)


# The journal is a map keyed by entry id, as in the oracle.
Journal = dict[str, JournalRecord]


def diff_options(legacy: dict[str, Any], options: dict[str, Any]) -> dict[str, Any]:
    """Calculate the plain deep-equal delta between two entry dicts.

    A removed key yields ``None`` (deletion, never a stored value).
    This is the journal's diff: unlike ``diff_entries`` it knows
    nothing about schemas or volatile fields.
    """
    changes: dict[str, Any] = {}
    for key in set(legacy) | set(options):
        if key == "id":
            continue
        if deep_equal(legacy.get(key), options.get(key), strict=True):
            continue
        changes[key] = options.get(key)
    return changes


def diff_entries(
    older: dict[str, Any],
    newer: dict[str, Any],
    config_schema: Any = None,
) -> dict[str, Any]:
    """Calculate the key-level delta between two entry dictionaries.

    A removed key yields ``None``: in a changes dict None deletes the
    key, it is never stored as a value. The ``config`` key compares with
    the schema-aware diff when ``config_schema`` is given, so a
    volatile-only config change produces no diff entry; without a schema
    every key compares by strict raw equality. Expression nodes compare
    structurally and are never evaluated.
    """
    changes: dict[str, Any] = {}
    for key in set(older.keys()) | set(newer.keys()):
        if key == "id":
            continue
        old_value = older.get(key)
        new_value = newer.get(key)
        if key == "config" and config_schema is not None:
            if equal_except_volatile(old_value, new_value, config_schema):
                continue
        elif deep_equal(old_value, new_value, strict=True):
            continue
        changes[key] = new_value
    return changes


def merge_records(
    older: JournalRecord | None, newer: JournalRecord
) -> JournalRecord | None:
    """Fold a later record into an earlier one for the same entry."""
    if older is None:
        return newer
    if newer.kind == "remove":
        # An entry created and removed before ever being written leaves
        # no trace.
        return None if older.kind == "upsert" and older.created else newer
    if older.kind == "remove":
        return JournalRecord(
            kind="upsert",
            created=False,
            parent=newer.parent,
            position=newer.position,
            changes=dict(newer.changes),
        )
    relocated = newer.parent is not _PARENT_UNSET
    return JournalRecord(
        kind="upsert",
        created=older.created,
        parent=newer.parent if relocated else older.parent,
        position=newer.position if relocated else older.position,
        changes={**older.changes, **newer.changes},
    )


def merge(journal: Journal, entry_id: str, record_: JournalRecord) -> None:
    """Merge a record into a journal, dropping it when it cancels out."""
    merged = merge_records(journal.get(entry_id), record_)
    if merged is not None:
        journal[entry_id] = merged
    else:
        journal.pop(entry_id, None)


def _index_of(data: list[dict[str, Any]], options: dict[str, Any]) -> int:
    """Position of ``options`` in ``data`` by identity (never equality)."""
    for index, row in enumerate(data):
        if row is options:
            return index
    return len(data)


def record(journal: Journal, change: EntryChange, parent: str | None) -> None:
    """Record one tree-side mutation into a journal.

    ``parent`` is the id of the group entry that owns the changed
    group (``None`` for the root group); it is stored on the record
    only when the change relocated the entry.
    """
    entry_id = change.id
    options = change.options
    if options is None:
        merge(journal, entry_id, JournalRecord(kind="remove"))
        return
    position = _index_of(change.group.data, options)
    if change.legacy is None:
        merge(
            journal,
            entry_id,
            JournalRecord(
                kind="upsert",
                created=True,
                parent=parent,
                position=position,
                changes={k: v for k, v in options.items() if k != "id"},
            ),
        )
        return
    merge(
        journal,
        entry_id,
        JournalRecord(
            kind="upsert",
            created=False,
            parent=parent if change.from_group is not None else _PARENT_UNSET,
            position=position,
            changes=diff_options(change.legacy, options),
        ),
    )


@dataclass
class FlatEntry:
    """One entry in a flattened entry list, with its place in the tree."""

    parent: str | None
    position: int
    options: dict[str, Any]
    owner_list: list[dict[str, Any]]


def flatten(
    data: list[dict[str, Any]],
    parent: str | None = None,
    result: dict[str, FlatEntry] | None = None,
) -> dict[str, FlatEntry]:
    """Index every entry (including nested group members) by id."""
    if result is None:
        result = {}
    for position, options in enumerate(data):
        if not isinstance(options, dict):
            raise TypeError("entry must be an object")
        entry_id = options.get("id")
        if entry_id:
            result[entry_id] = FlatEntry(
                parent=parent,
                position=position,
                options=options,
                owner_list=data,
            )
        if options.get("group") and isinstance(options.get("config"), list):
            flatten(options["config"], options.get("id"), result)
    return result


def apply_changes(
    options: dict[str, Any],
    changes: dict[str, Any],
    filter_fn: Callable[[str], bool] | None = None,
) -> None:
    """Apply a changes dict in place: a None value deletes its key."""
    for key, value in changes.items():
        if filter_fn is not None and not filter_fn(key):
            continue
        if value is None:
            options.pop(key, None)
        else:
            options[key] = value


def detach(data: list[dict[str, Any]], entry_id: str) -> dict[str, Any] | None:
    """Remove an entry from a list by id, returning its options."""
    flat = flatten(data).get(entry_id)
    if flat is None:
        return None
    # flatten guarantees the row is in its owner list, so the identity
    # search always finds it.
    index = next(i for i, row in enumerate(flat.owner_list) if row is flat.options)
    del flat.owner_list[index]
    return flat.options


def place(
    data: list[dict[str, Any]],
    options: dict[str, Any],
    parent: str | None,
    position: int,
) -> bool:
    """Insert ``options`` under ``parent`` (a group id, or None for the root).

    The position is clamped to the group's length. Returns False when
    the parent group does not exist.
    """
    target_list = data
    if parent is not None:
        group = flatten(data).get(parent)
        if group is None or not group.options.get("group"):
            return False
        config = group.options.get("config")
        if config is None:
            config = group.options["config"] = []
        target_list = config
    target_list.insert(min(position, len(target_list)), options)
    return True


def apply_journal(
    data: list[dict[str, Any]],
    journal: Journal,
    filter_fn: Callable[[str, str | None], bool] | None = None,
    warn: Callable[..., None] | None = None,
) -> list[dict[str, Any]]:
    """Overlay a journal onto ``data`` in place.

    ``filter_fn(id, key)`` restricts which changes apply; ``key`` is
    None when asking about the entry as a whole (creation, removal,
    relocation).
    """
    warn_fn = warn or _noop_warn
    allow = filter_fn or (lambda _id, _key: True)
    for entry_id, rec in journal.items():
        if not allow(entry_id, None):
            continue

        def _key_filter(key: str, _id: str = entry_id) -> bool:
            return allow(_id, key)

        if rec.kind == "remove":
            detach(data, entry_id)
            continue
        flat = flatten(data).get(entry_id)
        if flat is None:
            options: dict[str, Any] = {"id": entry_id}
            apply_changes(options, rec.changes, _key_filter)
            parent = rec.parent if rec.parent is not _PARENT_UNSET else None
            if not place(data, options, parent, rec.position):
                warn_fn(
                    "cannot place entry %C: group %C not found",
                    entry_id,
                    parent,
                )
            continue
        apply_changes(flat.options, rec.changes, _key_filter)
        if rec.parent is not _PARENT_UNSET and flat.parent != rec.parent:
            detach(data, entry_id)
            if not place(data, flat.options, rec.parent, rec.position):
                warn_fn(
                    "cannot move entry %C: group %C not found",
                    entry_id,
                    rec.parent,
                )
                place(data, flat.options, flat.parent, flat.position)
    return data


@dataclass
class Conflict:
    """One file-vs-runtime conflict found by ``reconcile``."""

    id: str
    reason: str


def reconcile(
    journal: Journal,
    base: dict[str, FlatEntry],
    theirs: dict[str, FlatEntry],
    file_owned: Callable[[str, str | None], bool],
) -> list[Conflict]:
    """Reconcile a journal against a new file state; the file wins.

    Conflicting keys are dropped from the journal so the tree follows
    the file, and each drop is reported. ``file_owned(id, key)`` scopes
    the check to what the file can actually express (patch-owned keys
    and patch-inserted entries live elsewhere).
    """
    conflicts: list[Conflict] = []
    for entry_id, rec in list(journal.items()):
        if not file_owned(entry_id, None):
            continue
        b = base.get(entry_id)
        t = theirs.get(entry_id)
        if rec.kind == "remove":
            if t is None:
                del journal[entry_id]
            elif b is None or not deep_equal(t.options, b.options, strict=True):
                conflicts.append(
                    Conflict(
                        id=entry_id,
                        reason=(
                            "added in file, removed at runtime"
                            if b is None
                            else "modified in file, removed at runtime"
                        ),
                    )
                )
                del journal[entry_id]
            continue
        if t is None:
            if b is not None:
                conflicts.append(
                    Conflict(
                        id=entry_id,
                        reason="removed in file, modified at runtime",
                    )
                )
                del journal[entry_id]
            # Else: created at runtime, nothing in the file to conflict.
            continue
        if rec.created:
            rec.created = False
        for key in list(rec.changes):
            if not file_owned(entry_id, key):
                continue
            bv = b.options.get(key) if b is not None else None
            tv = t.options.get(key)
            if deep_equal(bv, tv, strict=True) or deep_equal(
                tv, rec.changes[key], strict=True
            ):
                continue
            conflicts.append(
                Conflict(
                    id=entry_id,
                    reason=f'key "{key}" modified both in file and at runtime',
                )
            )
            del rec.changes[key]
        if (
            rec.parent is not _PARENT_UNSET
            and b is not None
            and t.parent != b.parent
            and t.parent != rec.parent
        ):
            conflicts.append(
                Conflict(id=entry_id, reason="moved both in file and at runtime")
            )
            rec.parent = _PARENT_UNSET
        moved = rec.parent is not _PARENT_UNSET and rec.parent != t.parent
        if not rec.changes and not moved:
            del journal[entry_id]
    return conflicts


# ---------------------------------------------------------------------------
# Patches
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EntryOwner:
    """Who supplies an entry, or one key of it, in the patched tree."""

    kind: str  # 'file' | 'patch' | 'embedded'
    index: int | None = None
    key: str | None = None  # 'insert' | 'config' for embedded owners


_FILE_OWNER = EntryOwner(kind="file")


class PatchIndex:
    """Where every entry and key of a patched tree comes from."""

    def __init__(
        self,
        owners: dict[str, EntryOwner],
        keys: dict[str, dict[str, int]],
    ) -> None:
        self._owners = owners
        self._keys = keys

    def entry(self, entry_id: str | None) -> EntryOwner:
        """Owner of the entry as a whole."""
        if entry_id is None:
            return _FILE_OWNER
        return self._owners.get(entry_id, _FILE_OWNER)

    def key(self, entry_id: str, key: str) -> EntryOwner:
        """Owner of one key of an entry."""
        index = self._keys.get(entry_id, {}).get(key)
        if index is None:
            return self.entry(entry_id)
        return EntryOwner(kind="patch", index=index)

    def file_owned(self, entry_id: str, key: str | None) -> bool:
        """Whether the file supplies the entry (or one key of it)."""
        owner = self.entry(entry_id) if key is None else self.key(entry_id, key)
        return owner.kind == "file"


def ensure_ids(entries: list[dict[str, Any]], used: Callable[[str], bool]) -> None:
    """Give every entry (nested groups included) an id, in place."""
    for entry in entries:
        if not entry.get("id"):
            while True:
                candidate = secrets.token_hex(4)
                if not used(candidate):
                    break
            entry["id"] = candidate
        if entry.get("group") and isinstance(entry.get("config"), list):
            ensure_ids(entry["config"], used)


def ensure_insert_ids(
    patches: list[dict[str, Any]] | None, used: Callable[[str], bool]
) -> None:
    """Give every patch-inserted entry an id, in place.

    Runtime changes are routed back to an inserted entry by its id, so
    every insert needs one.
    """
    for patch in patches or []:
        insert = patch.get("insert") if isinstance(patch, dict) else None
        if insert:
            ensure_ids(insert, used)


def _validate_patch_list(patches: Any) -> None:
    """Reject a patch list (or a patch, or an insert) that is not literal.

    Patches are literal: a patch list, a patch, or an insert that is a
    config expression is rejected, never evaluated. A JsExpr carried as
    a patch VALUE is data, applied literally and never evaluated.
    A missing patch list (None) is valid: no patches.
    """
    if patches is None:
        return
    if is_js_expr(patches) or not isinstance(patches, list):
        raise IncludeError(
            "include patches must be a literal patch list; "
            "config expressions are not evaluated"
        )
    for patch in patches:
        if not isinstance(patch, dict) or is_js_expr(patch):
            raise IncludeError(
                "include patches must be literal mappings; "
                "config expressions are not evaluated"
            )
        insert = patch.get("insert")
        if insert is not None:
            if is_js_expr(insert) or not isinstance(insert, list):
                raise IncludeError(
                    "expected a literal entry list; "
                    "config expressions are not evaluated"
                )
            for item in insert:
                if not isinstance(item, dict) or is_js_expr(item):
                    raise IncludeError(
                        "expected a literal entry list; "
                        "config expressions are not evaluated"
                    )


def _apply_patches(
    data: list[dict[str, Any]],
    patches: list[dict[str, Any]] | None,
    warn: Callable[..., None],
) -> tuple[list[dict[str, Any]], PatchIndex]:
    """Overlay ``patches`` onto a fresh clone of ``data``.

    Entry lists from patches are cloned too, so the tree owns every
    entry it mounts. The index is built in the same pass, so a patch
    that does not apply owns nothing. The input is never mutated.
    """
    data = copy.deepcopy(data)
    entries: dict[str, dict[str, Any]] = {}
    owners: dict[str, EntryOwner] = {}
    keys: dict[str, dict[str, int]] = {}

    def mount(rows: list[dict[str, Any]], owner: EntryOwner) -> None:
        for options in rows:
            entry_id = options.get("id")
            if entry_id:
                entries[entry_id] = options
                owners[entry_id] = owner
                # Earlier overrides were aimed at the entry this one
                # replaces.
                keys.pop(entry_id, None)
            if options.get("group") and isinstance(options.get("config"), list):
                mount(options["config"], owner)

    def unmount(rows: list[dict[str, Any]]) -> None:
        for options in rows:
            entry_id = options.get("id")
            if entry_id:
                entries.pop(entry_id, None)
                owners.pop(entry_id, None)
                keys.pop(entry_id, None)
            if options.get("group") and isinstance(options.get("config"), list):
                unmount(options["config"])

    mount(data, _FILE_OWNER)

    for index, patch in enumerate(patches or []):
        patch_id = patch.get("id")
        insert = patch.get("insert")
        name = patch.get("name")
        overrides = {
            k: v for k, v in patch.items() if k not in ("id", "insert", "name")
        }
        target = entries.get(patch_id) if patch_id else None

        if insert:
            if patch_id and target is None:
                warn("patch insert: entry %C not found", patch_id)
                continue
            if target is not None and not target.get("group"):
                warn("patch insert: entry %C is not a group", patch_id)
                continue
            clone = copy.deepcopy(insert)
            if target is not None:
                if not isinstance(target.get("config"), list):
                    target["config"] = []
                target["config"].extend(clone)
            else:
                data.extend(clone)
            mount(clone, EntryOwner(kind="embedded", index=index, key="insert"))
            continue

        if not patch_id:
            warn("patch: id is required for non-insert patches")
            continue
        if target is None:
            warn("patch: entry %C not found", patch_id)
            continue
        if name and name != target.get("name"):
            warn(
                "patch: name mismatch for %C (expected %C, got %C), skipping",
                patch_id,
                target.get("name"),
                name,
            )
            continue

        # The entries of a group config the patch replaces go away with it.
        if (
            "config" in overrides
            and target.get("group")
            and isinstance(target.get("config"), list)
        ):
            unmount(target["config"])

        overridden = keys.setdefault(patch_id, {})
        for key, value in overrides.items():
            target[key] = value
            overridden[key] = index

        # Entries of a group config the patch supplies can be patched in
        # turn.
        if (
            "config" in overrides
            and target.get("group")
            and isinstance(target.get("config"), list)
        ):
            # Ids go on the patch itself, before the tree takes its own copy.
            ensure_ids(target["config"], lambda i: i in entries)
            target["config"] = copy.deepcopy(target["config"])
            mount(
                target["config"],
                EntryOwner(kind="embedded", index=index, key="config"),
            )

    return data, PatchIndex(owners, keys)


def apply_entry_patches(
    data: list[dict[str, Any]],
    patches: list[dict[str, Any]] | None = None,
    warn: Callable[..., None] | None = None,
) -> list[dict[str, Any]]:
    """Apply patch lists to an entry list — the patch semantics of Include.

    The input is never mutated: patching shared entry objects would bake
    earlier patch values into the cached parse, so repeated application
    (config hot-reloads) could never revert a removed or changed patch.
    Inserted entries are indexed as they are added, so a later patch in
    the same list can target a row an earlier patch inserted. A patch
    that matches nothing warns and is skipped. Patches are literal (see
    ``_validate_patch_list``); a JsExpr carried as a patch VALUE is data,
    applied literally and never evaluated.
    """
    if is_js_expr(data) or not isinstance(data, list):
        raise IncludeError(
            "expected a literal entry list; config expressions are not evaluated"
        )
    for row in data:
        if not isinstance(row, dict):
            raise TypeError("entry must be an object")
    _validate_patch_list(patches if patches is not None else [])
    result, _index = _apply_patches(data, patches, warn or _noop_warn)
    return result


def route_journal(
    journal: Journal,
    data: list[dict[str, Any]],
    patches: list[dict[str, Any]],
    warn: Callable[..., None] | None = None,
) -> bool:
    """Route a journal to its owners: file-owned changes to ``data``.

    Patch-owned changes are applied to ``patches`` instead. Both are
    mutated in place, so pass clones. Returns whether any patch changed.
    """
    warn_fn = warn or _noop_warn
    _, index = _apply_patches(data, patches, _noop_warn)
    patched = False

    def reindex() -> None:
        nonlocal index
        _, index = _apply_patches(data, patches, _noop_warn)

    def container(
        owner: EntryOwner,
    ) -> tuple[list[dict[str, Any]], str | None]:
        if owner.kind == "file":
            return data, None
        patch = patches[owner.index]  # type: ignore[index]
        return patch[owner.key], patch.get("id")  # type: ignore[index]

    def children(parent_id: str) -> list[dict[str, Any]] | None:
        owner = index.key(parent_id, "config")
        if owner.kind == "file":
            return None
        if owner.kind == "patch":
            config = patches[owner.index].get("config")  # type: ignore[index]
            return config if isinstance(config, list) else None
        found = flatten(container(owner)[0]).get(parent_id)
        # The index only yields embedded owners for entries mounted
        # from that container, so the lookup usually hits — but a move
        # can detach the container first (a group moved under its own
        # descendant), and then it misses: fall back to place() + warn,
        # exactly as the oracle does.
        if found is None:
            return None
        config = found.options.get("config")
        if config is None:
            config = found.options["config"] = []
        return config

    def place_at(options: dict[str, Any], parent: str | None, position: int) -> None:
        nonlocal patched
        target_list = children(parent) if parent is not None else None
        if target_list is not None:
            target_list.insert(min(position, len(target_list)), options)
            patched = True
        elif not place(data, options, parent, position):
            warn_fn(
                "cannot place entry %C: group %C not found",
                options.get("id"),
                parent,
            )

    for entry_id, rec in list(journal.items()):
        owner = index.entry(entry_id)
        owner_list, base = container(owner)
        embedded = owner.kind == "embedded"

        if rec.kind == "remove":
            detach(owner_list, entry_id)
            patched = patched or embedded
            reindex()
            continue

        current = flatten(owner_list).get(entry_id)
        if current is None:
            options: dict[str, Any] = {"id": entry_id}
            apply_changes(options, rec.changes)
            parent = rec.parent if rec.parent is not _PARENT_UNSET else None
            place_at(options, parent, rec.position)
            reindex()
            continue

        # An entry at the top of a patch list belongs to the group the
        # patch targets.
        current_parent = current.parent if current.parent is not None else base
        if rec.parent is not _PARENT_UNSET and current_parent != rec.parent:
            # Moving across owners: carry the full options over.
            detach(owner_list, entry_id)
            patched = patched or embedded
            apply_changes(current.options, rec.changes)
            place_at(current.options, rec.parent, rec.position)
            reindex()
            continue

        patch_changes: dict[int, dict[str, Any]] = {}

        def _filter(
            key: str,
            _id: str = entry_id,
            _rec: JournalRecord = rec,
            _changes: dict[int, dict[str, Any]] = patch_changes,
        ) -> bool:
            key_owner = index.key(_id, key)
            if key_owner.kind != "patch":
                return True
            patch_index = key_owner.index
            if patch_index is None:  # pragma: no cover - index always carries an int
                return True
            _changes.setdefault(patch_index, {})[key] = _rec.changes[key]
            return False

        apply_changes(current.options, rec.changes, _filter)
        patched = patched or embedded
        for patch_index, changes in patch_changes.items():
            apply_changes(patches[patch_index], changes)
            patched = True

    return patched


# ---------------------------------------------------------------------------
# Include
# ---------------------------------------------------------------------------


def _is_retryable_write_error(error: OSError) -> bool:
    """Whether a write error is a transient lock worth retrying."""
    return error.errno in (errno.EACCES, errno.EBUSY, errno.EPERM)


class Include(Service, EntryTree):
    """Loader entry tree backed by a YAML or JSON file.

    A component mounted as an entry (tree-carrier marker, like Group):
    its config is ``{path, initial?, patches?, enableLogs?}``. The file
    supplies the entry list; ``patches`` overlay it; the journal
    reconciles tree-side mutations with file edits and drains them back
    to the file. Only ``.json`` / ``.yaml`` / ``.yml`` are supported.
    """

    def __init__(self, ctx: Context, config: dict[str, Any] | None = None) -> None:
        if config is None:
            cfg: dict[str, Any] = {}
        elif isinstance(config, dict):
            # The fiber's config object. (The registry copies the dict
            # the entry row holds, so this is not the row's dict
            # itself; the update veto keeps it current by reassignment.)
            cfg = config
        else:
            raise IncludeError("include config must be a mapping")
        # 1. Tree first: sets self.ctx to the extended child ctx, plus
        #    root/store/owner/owner_fiber.
        loader = ctx.get("loader", default=None, strict=False)
        EntryTree.__init__(self, ctx, loader=loader)
        tree_ctx = self.ctx
        # 2. Service second, on the ORIGINAL ctx: registers "include"
        #    where consumers look for it. This overwrites self.ctx —
        #    restore it next.
        Service.__init__(self, ctx, cfg.get("name", "include"))
        self.ctx = tree_ctx
        self.service_ctx = ctx
        self.config = cfg

        path = cfg.get("path")
        if not isinstance(path, str) or not path:
            raise IncludeError("include requires a 'path' string")
        # baseUrl is a plain attribute in anima (unlike the oracle,
        # where it is inherited through the context chain), so walk up
        # to the nearest ancestor that sets one: a nested include
        # resolves its relative path against the outer file's dir.
        base_url = ctx.baseUrl
        ancestor = ctx._parent
        while base_url is None and ancestor is not None:
            base_url = ancestor.baseUrl
            ancestor = ancestor._parent
        self.filename = self._resolve_filename(path, base_url)
        ext = self.filename.suffix.lower()
        if ext not in _SUPPORTED_EXTENSIONS:
            raise IncludeError(f'extension "{ext}" not supported')
        self._type = "application/json" if ext == ".json" else "application/yaml"
        object.__setattr__(self.ctx, "baseUrl", str(self.filename.parent))

        if "enableLogs" in cfg:
            self.enable_logs = bool(cfg["enableLogs"])
        elif self.owner is not None:
            self.enable_logs = self.owner.parent.tree.enable_logs

        _validate_patch_list(cfg.get("patches"))
        ensure_insert_ids(cfg.get("patches"), lambda i: i in self.store)
        self._check_cycle()

        ctx.on("internal/update", self._on_internal_update, mode=EventMode.WATERFALL)

        self._cache: dict[str, Any] | None = None
        self._journal: Journal = {}
        self._index: PatchIndex | None = None
        self._pending_tree: list[dict[str, Any]] | None = None
        self._apply_task: asyncio.Task[None] | None = None
        self._draining: asyncio.Task[None] | None = None
        self._dirty_read = False
        self._dirty_write = False
        self._disposed = False
        self._seq = 0

    @staticmethod
    def _resolve_filename(path: str, base_url: str | None) -> Path:
        """Resolve an include path against the base URL (or the cwd)."""
        candidate = Path(path)
        if candidate.is_absolute():
            return candidate.resolve()
        if base_url:
            base = base_url.removeprefix("file://")
            return (Path(base) / candidate).resolve()
        return candidate.resolve()

    def _check_cycle(self) -> None:
        """Fail loud when this file is already mounted in the ancestor chain."""
        seen = {self.filename}
        entry = self.owner
        while entry is not None:
            tree = entry.parent.tree
            if isinstance(tree, Include):
                if tree.filename in seen:
                    raise IncludeError(f"circular include detected: {self.filename}")
                seen.add(tree.filename)
            entry = tree.owner

    def _warn(self, message: str, *args: Any) -> None:
        """Warn through the loader logger (printf-style, ``%C`` = code)."""
        self.ctx.root.logger("loader").warn(message, *args)

    def _error(self, message: str, *args: Any) -> None:
        """Log an error through the loader logger."""
        self.ctx.root.logger("loader").error(message, *args)

    def _start_task(self, coro: Any) -> asyncio.Task[Any] | None:
        """Schedule a coroutine on the running loop, if there is one.

        With no running loop the caller keeps its dirty flags and the
        work is flushed on the next trigger; nothing is dropped.
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # Close the never-started coroutine so it is not reported
            # as never awaited; the work is deferred, not dropped.
            coro.close()
            return None
        task = loop.create_task(coro)
        self.ctx._pending_tasks.add(task)
        task.add_done_callback(self.ctx._pending_tasks.discard)
        return task

    async def _on_internal_update(self, config: Any, next_fn: Any = None) -> Any:
        """Adopt an update to this include's own config; veto the restart.

        When the fiber's own config updates (same path), validate the new
        patches exactly as construction does, adopt the new config
        (including new patches), re-apply, and do NOT restart the fiber
        — the children update in place. Returns the pending apply task,
        so a direct caller can await the re-apply; through the fiber
        update the return is discarded, so awaiting the fiber update
        does NOT await the re-apply. Any other update passes down the
        chain.
        """
        if not isinstance(config, dict) or config.get("path") != self.config.get(
            "path"
        ):
            if callable(next_fn):
                res = next_fn(config)
                if inspect.isawaitable(res):
                    return await res
                return res
            return None
        _validate_patch_list(config.get("patches"))
        self.config = config
        ensure_insert_ids(config.get("patches"), lambda i: i in self.store)
        self._schedule_apply()
        return self._apply_task

    async def init(self) -> None:
        """Read the file (or fall back to ``initial``) and mount the tree."""
        self.ctx.effect(lambda: self.stop)
        try:
            await self._read()
        except FileNotFoundError:
            # Only a missing file falls back to `initial` (or the
            # not-found error): an existing-but-invalid file must fail
            # loud with its real parse error, never be mislabelled as
            # absent or silently overwritten.
            if self.config.get("initial") is not None:
                initial = copy.deepcopy(self.config["initial"])
                await self._write_file(initial)
                await self._read(forced=True)
            else:
                raise IncludeError(f"config file not found: {self.filename}") from None
        await self.flush()

    async def stop(self) -> None:
        """Flush what the user last did, then tear the tree down."""
        self._disposed = True
        self._pending_tree = None
        # Flush what the user last did before the tree goes away; this is
        # also the last attempt for changes a failed write left in the
        # journal.
        if self._journal:
            self._dirty_write = True
            self._drain()
        await self.flush()
        self.root.stop()

    async def flush(self) -> None:
        """Wait until every pending read and write has been processed."""
        while True:
            draining = self._draining
            applying = self._apply_task
            if draining is None and applying is None:
                return
            if draining is not None:
                await draining
            if applying is not None:
                await applying

    async def refresh(self) -> None:
        """Re-read the file and reconcile the tree with it."""
        self._dirty_read = True
        self._drain()
        await self.flush()

    def commit(self, change: EntryChange) -> None:
        """Record one tree-side mutation into the journal for write-back."""
        self.ctx.emit("loader/config-update")
        parent = (
            None
            if change.group is self.root
            else (
                change.group.owner.options["id"]
                if change.group.owner is not None
                else None
            )
        )
        record(self._journal, change, parent)
        self._dirty_write = True
        self._drain()

    def write(self) -> None:
        """Schedule a write of the current file-owned tree state.

        Emits ``loader/config-update`` and drains the journal, which
        writes the merged file state. A direct write of ``root.data``
        would bake patch-owned values into the file, so the drain —
        which routes patch-owned changes to the patch list — is the
        write path.
        """
        self.ctx.emit("loader/config-update")
        self._dirty_write = True
        self._drain()

    def _drain(self) -> None:
        """Start the drain loop unless one is already running."""
        if self._draining is not None:
            return
        task = self._start_task(self._loop())
        if task is not None:
            self._draining = task

    async def _loop(self) -> None:
        """Drain pending reads and writes until both are settled."""
        try:
            while self._dirty_read or self._dirty_write:
                if self._dirty_read:
                    self._dirty_read = False
                    try:
                        await self._read()
                    except Exception as exc:  # noqa: BLE001 - half-written files are routine
                        # A half-written file is routine while the user is
                        # editing, hence only a warning. With the file in
                        # an unknown state there is nothing safe to
                        # write: unless another read is due, stop here
                        # and leave `dirty_write` set for the next
                        # trigger.
                        self._warn("%s", exc)
                        if self._dirty_read:
                            continue
                        return
                if self._dirty_write:
                    self._dirty_write = False
                    try:
                        await self._write()
                    except Exception as exc:  # noqa: BLE001 - a failed write must not kill the drain
                        self._error("%s", exc)
        finally:
            self._draining = None

    def _parse(self, content: str) -> list[dict[str, Any]]:
        """Parse file content; reject every non-array shape as one signal."""
        if self._type == "application/json":
            data = json.loads(content)
        else:
            data = load_yaml(content)
        # An empty or truncated file parses to None, not an error; reject
        # every non-array shape here so callers see one "invalid file"
        # signal.
        if not isinstance(data, list):
            raise TypeError(
                f"config file must be a top-level array of entries: {self.filename}"
            )
        for row in data:
            if not isinstance(row, dict):
                raise TypeError("entry must be an object")
        return data

    def _dump(self, data: list[dict[str, Any]]) -> str:
        """Serialize an entry list in the file's dialect."""
        if self._type == "application/json":
            return json.dumps(data, indent=2)
        stream = io.StringIO()
        dump_yaml(data, stream)
        return stream.getvalue()

    async def _read(self, forced: bool = False) -> bool:
        """Read the file; return False when the content is unchanged."""
        content = self.filename.read_text(encoding="utf-8")
        if not forced and self._cache is not None and content == self._cache["content"]:
            return False
        data = self._parse(content)
        if self._cache is not None:
            _patched, index = _apply_patches(
                data, self.config.get("patches"), _noop_warn
            )
            conflicts = reconcile(
                self._journal,
                flatten(self._cache["data"]),
                flatten(data),
                index.file_owned,
            )
            for conflict in conflicts:
                self._error(
                    "config conflict in %C: entry %C %s; file wins",
                    self.filename,
                    conflict.id,
                    conflict.reason,
                )
        self._cache = {"content": content, "data": data}
        self._schedule_apply()
        # The file changed while tree-side changes were pending: write
        # the merge.
        if self._journal:
            self._dirty_write = True
        return True

    async def _write(self) -> None:
        """Drain the journal into the file (or the patch list)."""
        if self._cache is None or not self._journal:
            return
        try:
            current = self.filename.read_text(encoding="utf-8")
        except FileNotFoundError:
            current = None
        if current is None:
            self._warn(
                "config file %C is missing, %d pending change(s) kept in memory",
                self.filename,
                len(self._journal),
            )
            # The journal still holds the live change, but no newer
            # tree is scheduled by a write that never happened: a
            # stale tree already in flight would apply unopposed and
            # clobber it. Schedule a fresh tree from the current
            # cache + journal so it supersedes the stale one.
            self._schedule_apply()
            return
        if current != self._cache["content"]:
            # Someone else wrote the file: merge first, then come back.
            self._dirty_read = True
            self._dirty_write = True
            return

        # Snapshot and swap: changes reported while we are writing
        # belong to the next round.
        batch = self._journal
        self._journal = {}

        def restore() -> None:
            for entry_id, rec in self._journal.items():
                merge(batch, entry_id, rec)
            self._journal = batch

        data = copy.deepcopy(self._cache["data"])
        patches = copy.deepcopy(self.config.get("patches") or [])
        patched = route_journal(batch, data, patches, self._warn)
        if patched:
            # Take the new patches now so that the tree scheduled below
            # already reflects them; the round trip through the parent
            # only persists them.
            new_config = {**self.config, "patches": patches}
            self.config = new_config
            # The owning fiber (not ctx.fiber: that walks up to the
            # root fiber, which has no entry).
            fiber = self.owner_fiber
            if fiber is not None:
                # Not awaited: the returned coroutine is the tree
                # reconciliation, and the drain loop must not block on
                # fibers.
                self._start_task(self._adopt_patches(fiber, new_config))
            else:
                self._warn(
                    "patches of %C changed at runtime but cannot be persisted",
                    self.filename,
                )

        # Nothing for the file itself (patch-only changes, or ids
        # assigned at mount that are already part of the cache).
        if deep_equal(data, self._cache["data"], strict=True):
            self._schedule_apply()
            return

        if not os.access(self.filename, os.W_OK):
            restore()
            self._warn(
                "config file %C is read-only, %d pending change(s) kept in memory",
                self.filename,
                len(self._journal),
            )
            # As above: no write means no fresh tree, so schedule one
            # from the restored journal before a stale in-flight tree
            # can clobber the live change.
            self._schedule_apply()
            return

        text = self._dump(data)
        try:
            await self._write_text(text, expected=self._cache["content"])
        except StaleWriteError:
            restore()
            self._dirty_read = True
            self._dirty_write = True
            return
        except Exception:
            restore()
            self._warn(
                "failed to write config file %C, %d pending change(s) kept in memory",
                self.filename,
                len(self._journal),
            )
            # As above: schedule a fresh tree from the restored
            # journal before a stale in-flight tree can clobber it.
            self._schedule_apply()
            raise
        self._cache = {"content": text, "data": data}
        self._schedule_apply()

    async def _adopt_patches(self, fiber: Any, new_config: dict[str, Any]) -> None:
        """Push adopted patches through the fiber update (errors logged)."""
        try:
            await fiber.update(new_config)
        except Exception as exc:  # noqa: BLE001 - adoption must not kill the drain
            self._error("%s", exc)

    async def _write_text(self, text: str, expected: str | None = None) -> None:
        """Write through a temp file and a rename.

        When ``expected`` is given, the rename (and every retry of it)
        only goes ahead while the file still holds that content.
        """
        tmp = (
            self.filename.parent
            / f".{self.filename.name}.{os.getpid()}.{self._seq}.tmp"
        )
        self._seq += 1
        try:
            tmp.write_text(text, encoding="utf-8")
            last_error: OSError | None = None
            for attempt in range(_WRITE_RETRY_LIMIT + 1):
                if expected is not None:
                    try:
                        current = self.filename.read_text(encoding="utf-8")
                    except FileNotFoundError:
                        current = None
                    if current != expected:
                        raise StaleWriteError(
                            f"config file changed during write: {self.filename}"
                        )
                try:
                    os.replace(tmp, self.filename)
                    return
                except OSError as exc:
                    if not _is_retryable_write_error(exc):
                        raise
                    last_error = exc
                if attempt < _WRITE_RETRY_LIMIT:
                    await asyncio.sleep(_WRITE_RETRY_DELAY_MS * (attempt + 1) / 1000)
            # The loop only exits via return, a raise, or a recorded
            # failure, so a failure is always recorded here.
            raise cast(OSError, last_error)
        except Exception:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise

    async def _write_file(self, data: list[dict[str, Any]]) -> None:
        """Dump an entry list and write it atomically (no journal)."""
        await self._write_text(self._dump(data))

    def _schedule_apply(self) -> None:
        """Apply the latest patched + journalled tree; older ones are skipped."""
        if self._disposed or self._cache is None:
            return
        patched, index = _apply_patches(
            self._cache["data"], self.config.get("patches"), self._warn
        )
        self._index = index
        self._pending_tree = apply_journal(patched, self._journal, warn=self._warn)
        if self._apply_task is None:
            self._apply_task = self._start_task(self._apply_loop())

    async def _apply_loop(self) -> None:
        """Apply pending trees in order until none is left."""
        try:
            while self._pending_tree is not None:
                tree = self._pending_tree
                self._pending_tree = None
                await self.root.update(tree)
        except Exception as exc:  # noqa: BLE001 - a failed apply must not kill the loop silently
            self._error("%s", exc)
        finally:
            self._apply_task = None


setattr(Include, EntryGroup.key, True)
