"""Multi-file configuration inclusion, journaling, and patch routing for Anima."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from anima.core import Context, Service, is_js_expr
from anima.jsexpr import load_yaml


class IncludeError(Exception):
    """Raised when profile inclusion or patch resolution fails."""


@dataclass
class JournalRecord:
    """Record of a pending configuration change for a single profile entry."""

    kind: str  # 'upsert' | 'remove'
    created: bool = False
    changes: dict[str, Any] = field(default_factory=dict)


def diff_entries(older: dict[str, Any], newer: dict[str, Any]) -> dict[str, Any]:
    """Calculate the key-level delta between two entry dictionaries."""
    changes: dict[str, Any] = {}
    for key in set(older.keys()) | set(newer.keys()):
        if key == "id":
            continue
        if older.get(key) != newer.get(key):
            changes[key] = newer.get(key)
    return changes


class Journal:
    """Tracks and coalesces live runtime configuration updates across profile entries."""

    def __init__(self) -> None:
        self.records: dict[str, JournalRecord] = {}

    def record_upsert(
        self, entry_id: str, changes: dict[str, Any], created: bool = False
    ) -> None:
        """Record or coalesce an update to an entry."""
        if entry_id in self.records:
            rec = self.records[entry_id]
            rec.changes.update(changes)
            if created:
                rec.created = True
        else:
            self.records[entry_id] = JournalRecord(
                kind="upsert", created=created, changes=dict(changes)
            )

    def record_remove(self, entry_id: str) -> None:
        """Record the removal of an entry."""
        self.records[entry_id] = JournalRecord(kind="remove")

    def apply(self, entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Apply all recorded journal changes to a list of entries."""
        result: list[dict[str, Any]] = []
        seen_ids: set[str] = set()

        for entry in entries:
            eid = entry.get("id")
            if eid and eid in self.records:
                rec = self.records[eid]
                if rec.kind == "remove":
                    continue
                # Apply changes
                updated = dict(entry)
                updated.update(rec.changes)
                result.append(updated)
                seen_ids.add(eid)
            else:
                result.append(dict(entry))
                if eid:
                    seen_ids.add(eid)

        # Append newly created entries from journal
        for eid, rec in self.records.items():
            if rec.created and eid not in seen_ids and rec.kind == "upsert":
                new_entry: dict[str, Any] = {"id": eid}
                new_entry.update(rec.changes)
                result.append(new_entry)

        return result


def apply_patch(
    entries: list[dict[str, Any]], patches: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Apply declarative patch operations over an entries list.

    Patches are literal: an entry list, a patch list, or a patch that is
    a config expression is rejected, never evaluated.
    """
    if not isinstance(entries, list):
        raise IncludeError(
            "expected a literal entry list; config expressions are not evaluated"
        )
    if not isinstance(patches, list):
        raise IncludeError(
            "include patches must be a literal patch list; "
            "config expressions are not evaluated"
        )
    id_map = {e.get("id"): dict(e) for e in entries if e.get("id")}
    ordered_ids = [e.get("id") for e in entries if e.get("id")]
    additional_entries: list[dict[str, Any]] = []

    for patch in patches:
        if not isinstance(patch, dict) or is_js_expr(patch):
            raise IncludeError(
                "include patches must be literal mappings; "
                "config expressions are not evaluated"
            )
        if "insert" in patch:
            inserted = patch["insert"]
            if not isinstance(inserted, list):
                raise IncludeError(
                    "expected a literal entry list; "
                    "config expressions are not evaluated"
                )
            for item in inserted:
                if is_js_expr(item):
                    raise IncludeError(
                        "expected a literal entry list; "
                        "config expressions are not evaluated"
                    )
                if isinstance(item, dict):
                    additional_entries.append(dict(item))
            continue

        pid = patch.get("id")
        if pid and pid in id_map:
            target = id_map[pid]
            for k, v in patch.items():
                if k != "id":
                    target[k] = v

    result = [id_map[eid] for eid in ordered_ids]
    result.extend(additional_entries)
    return result


class IncludeService(Service):
    """Subsystem for multi-file configuration resolution and profile synchronization."""

    def __init__(self, ctx: Context, config: dict[str, Any] | None = None) -> None:
        cfg = dict(config or {})
        super().__init__(ctx, cfg.get("name", "include"))
        self.journal = Journal()

    def resolve_file(
        self,
        file_path: Path | str,
        base_dir: Path | None = None,
        seen: set[Path] | None = None,
    ) -> Any:
        """Resolve and load a YAML or JSON file from the filesystem."""
        active_seen = set(seen) if seen is not None else set()

        p = Path(file_path)
        if not p.is_absolute() and base_dir is not None:
            p = (base_dir / p).resolve()
        else:
            p = p.resolve()

        if p in active_seen:
            raise IncludeError(f"Circular include detected: {p}")

        if not p.exists():
            raise IncludeError(f"Included file does not exist: {p}")

        active_seen.add(p)
        current_dir = p.parent

        with open(p, encoding="utf-8") as f:
            if p.suffix in (".json",):
                raw_data = json.load(f)
            else:
                raw_data = load_yaml(f)

        if is_js_expr(raw_data):
            raise IncludeError(
                "expected a literal entry list; config expressions are not evaluated"
            )

        return self.resolve(raw_data, base_dir=current_dir, seen=active_seen)

    def resolve(
        self,
        source: Path | str | dict[str, Any] | list[Any],
        base_dir: Path | None = None,
        seen: set[Path] | None = None,
    ) -> Any:
        """Recursively expand all `$include` directives into a unified data structure."""
        active_seen = set(seen) if seen is not None else set()

        # 1. Source is a filesystem path or path string at top level
        if isinstance(source, Path):
            return self.resolve_file(source, base_dir=base_dir, seen=active_seen)
        if isinstance(source, str) and base_dir is None:
            return self.resolve_file(source, base_dir=base_dir, seen=active_seen)

        # 2. Source is a dictionary
        if isinstance(source, dict):
            if "$include" in source:
                include_target = source["$include"]
                if is_js_expr(include_target) or not isinstance(
                    include_target, (str, Path)
                ):
                    raise IncludeError(
                        "include path must be literal; "
                        "config expressions are not evaluated"
                    )
                resolved_include = self.resolve_file(
                    include_target, base_dir=base_dir, seen=active_seen
                )
                # Merge other keys over included data if included data is dict
                if isinstance(resolved_include, dict):
                    merged = dict(resolved_include)
                    for k, v in source.items():
                        if k != "$include":
                            merged[k] = self.resolve(
                                v, base_dir=base_dir, seen=active_seen
                            )
                    return merged
                return resolved_include

            # Process all dict values recursively
            return {
                k: self.resolve(v, base_dir=base_dir, seen=active_seen)
                for k, v in source.items()
            }

        # 3. Source is a list
        if isinstance(source, list):
            res_list: list[Any] = []
            for item in source:
                resolved_item = self.resolve(item, base_dir=base_dir, seen=active_seen)
                res_list.append(resolved_item)
            return res_list

        # Scalar primitive pass-through (str, int, float, bool, None)
        return source
