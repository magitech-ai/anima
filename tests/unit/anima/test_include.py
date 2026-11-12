"""Include: file-backed entry tree, patch lists, and the journal.

P1-7 replaces the old $include expansion and the old Journal class
with the file-backed Include component, apply_entry_patches, and the
journal module (diff_options / record / apply_journal / reconcile /
route_journal).
"""

import asyncio
import copy
import errno
import json
import os
import re
import sys
import types
from pathlib import Path
from typing import Any

import pytest

from anima.core import Context, FiberState
from anima.include import (
    _PARENT_UNSET,
    Conflict,
    EntryChange,
    EntryOwner,
    FlatEntry,
    Include,
    IncludeError,
    Journal,
    JournalRecord,
    PatchIndex,
    apply_changes,
    apply_entry_patches,
    apply_journal,
    detach,
    diff_entries,
    diff_options,
    ensure_ids,
    ensure_insert_ids,
    equal_except_volatile,
    flatten,
    merge,
    merge_records,
    place,
    reconcile,
    record,
    route_journal,
)
from anima.jsexpr import load_yaml
from anima.loader import LoaderService
from anima.schema import Schema


@pytest.fixture
def mod(monkeypatch: pytest.MonkeyPatch) -> Any:
    """A dummy module with a recording component and a self-disposer."""
    calls: list[Any] = []

    def comp(ctx: Context, config: Any = None) -> None:
        calls.append(dict(config) if isinstance(config, dict) else config)

    def self_disposer(ctx: Context, config: Any = None) -> None:
        async def _later() -> None:
            await ctx.fiber.dispose()

        ctx._schedule_task(_later())

    module = types.ModuleType("inc_test_mod")
    module.comp = comp
    module.self_disposer = self_disposer
    module.calls = calls  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "inc_test_mod", module)
    return module


def _warn_sink(warnings: list[str]) -> Any:
    """Capture warn args as plain text (the loader logger's %C is not %-formattable)."""

    def _warn(*args: Any) -> None:
        warnings.append(" ".join(str(a) for a in args))

    return _warn


def _re_match_hex(value: Any) -> bool:
    return re.fullmatch(r"[0-9a-f]{8}", value) is not None


def _logger_text(ctx: Context) -> str:
    """All text the loader logger recorded, via the LoggerService buffer.

    The loader logger writes to the service buffer, not to stdlib
    logging, so caplog cannot see it.
    """
    svc = ctx.logger
    return " ".join(str(a) for m in svc.buffer for a in m.args)


def _rows(*levels: int) -> str:
    """YAML entry list of recording components with the given levels."""
    return "".join(
        f"- name: inc_test_mod:comp\n  id: e{i}\n  config:\n    level: {level}\n"
        for i, level in enumerate(levels)
    )


async def _boot(
    tmp_path: Path,
    content: str | None,
    *,
    filename: str = "cfg.yaml",
    patches: list[dict[str, Any]] | None = None,
    extra: dict[str, Any] | None = None,
) -> tuple[Context, LoaderService, str, Any, Include]:
    """Mount an Include entry on ``content``; return its live parts.

    ``content`` None leaves an existing file untouched (re-read boot).
    """
    file = tmp_path / filename
    if content is not None:
        file.write_text(content, encoding="utf-8")
    ctx = Context()
    loader = LoaderService(ctx)
    config: dict[str, Any] = {"path": str(file)}
    if patches is not None:
        config["patches"] = patches
    if extra:
        config.update(extra)
    inc_id = await loader.create({"name": "include", "config": config})
    await loader.await_all()
    await ctx.flush()
    entry = loader.resolve(inc_id)
    inc = entry.ctx.get("include", strict=False)
    assert isinstance(inc, Include)
    return ctx, loader, inc_id, entry, inc


async def _settle(inc: Include, loader: LoaderService) -> None:
    """Wait for the Include drain, its ctx tasks, and the loader tree."""
    await inc.flush()
    await inc.ctx.flush()
    await loader.await_all()


# --- P1-5: patches and include config must stay literal ---


def test_apply_entry_patches_rejects_js_expr() -> None:
    entries = [{"id": "a", "name": "comp"}]
    with pytest.raises(IncludeError, match="include patches must be literal mappings"):
        apply_entry_patches(entries, [{"__jsExpr": "process.env.X"}])
    with pytest.raises(IncludeError, match="include patches must be literal mappings"):
        apply_entry_patches(entries, ["not-a-mapping"])  # type: ignore[list-item]
    with pytest.raises(
        IncludeError, match="include patches must be a literal patch list"
    ):
        apply_entry_patches(entries, {"__jsExpr": "process.env.X"})  # type: ignore[arg-type]
    with pytest.raises(IncludeError, match="expected a literal entry list"):
        apply_entry_patches({"__jsExpr": "process.env.X"}, [])  # type: ignore[arg-type]


def test_apply_entry_patches_rejects_js_expr_insert() -> None:
    entries = [{"id": "a", "name": "comp"}]
    with pytest.raises(IncludeError, match="expected a literal entry list"):
        apply_entry_patches(entries, [{"insert": {"__jsExpr": "process.env.X"}}])
    with pytest.raises(IncludeError, match="expected a literal entry list"):
        apply_entry_patches(entries, [{"insert": "not-a-list"}])
    with pytest.raises(IncludeError, match="expected a literal entry list"):
        apply_entry_patches(entries, [{"insert": [{"__jsExpr": "process.env.X"}]}])
    # A literal insert still applies; a JsExpr patch value is data, not
    # an instruction, so it is applied literally and never evaluated.
    patched = apply_entry_patches(entries, [{"insert": [{"id": "b", "name": "other"}]}])
    assert [e["id"] for e in patched] == ["a", "b"]
    patched = apply_entry_patches(
        entries,
        [{"id": "a", "config": {"mode": {"__jsExpr": "process.env.X"}}}],
    )
    assert patched[0]["config"] == {"mode": {"__jsExpr": "process.env.X"}}


def test_diff_treats_js_expr_as_opaque() -> None:
    # Identical expression nodes: no change, and nothing is evaluated
    # (an out-of-scope expression would raise if it were).
    older = {"id": "a", "config": {"mode": {"__jsExpr": "homePath('x')"}}}
    newer = {"id": "a", "config": {"mode": {"__jsExpr": "homePath('x')"}}}
    assert diff_entries(older, newer) == {}
    # A changed expression string is a raw change, still unevaluated.
    changed = {"id": "a", "config": {"mode": {"__jsExpr": "process.env.Y"}}}
    assert diff_entries(older, changed) == {
        "config": {"mode": {"__jsExpr": "process.env.Y"}}
    }
    # An expression node replacing a literal (and vice versa) is a change.
    literal = {"id": "a", "config": {"mode": "plain"}}
    assert diff_entries(literal, older) == {
        "config": {"mode": {"__jsExpr": "homePath('x')"}}
    }
    assert diff_entries(older, literal) == {"config": {"mode": "plain"}}


# ---------------------------------------------------------------------------
# P1-6 — equal_except_volatile (schema-aware config diff)
# ---------------------------------------------------------------------------

_VOLATILE_SCHEMA = Schema.object(
    {
        "theme": Schema.string().volatile(),
        "level": Schema.number(),
        "nested": Schema.object(
            {
                "label": Schema.string().volatile(),
                "count": Schema.number(),
            }
        ),
    }
)


def test_equal_except_volatile_volatile_only_change() -> None:
    older = {"theme": "dark", "level": 3}
    newer = {"theme": "light", "level": 3}
    assert equal_except_volatile(older, newer, _VOLATILE_SCHEMA) is True


def test_equal_except_volatile_ordinary_change() -> None:
    older = {"theme": "dark", "level": 3}
    newer = {"theme": "dark", "level": 4}
    assert equal_except_volatile(older, newer, _VOLATILE_SCHEMA) is False
    # Volatile and ordinary together are still an ordinary change.
    assert (
        equal_except_volatile(older, {"theme": "light", "level": 4}, _VOLATILE_SCHEMA)
        is False
    )


def test_equal_except_volatile_nested_volatile() -> None:
    older = {"nested": {"label": "a", "count": 1}}
    assert (
        equal_except_volatile(
            older, {"nested": {"label": "b", "count": 1}}, _VOLATILE_SCHEMA
        )
        is True
    )
    assert (
        equal_except_volatile(
            older, {"nested": {"label": "a", "count": 2}}, _VOLATILE_SCHEMA
        )
        is False
    )


def test_equal_except_volatile_absent_object_is_schema_default() -> None:
    # An absent nested object compares equal to its schema default ({}).
    assert (
        equal_except_volatile(
            {"level": 3}, {"level": 3, "nested": {}}, _VOLATILE_SCHEMA
        )
        is True
    )
    assert (
        equal_except_volatile(
            {"level": 3}, {"level": 3, "nested": {"count": 1}}, _VOLATILE_SCHEMA
        )
        is False
    )
    # Absent top-level config compares equal to the object default.
    assert equal_except_volatile(None, {}, _VOLATILE_SCHEMA) is True
    assert equal_except_volatile(None, {"level": 3}, _VOLATILE_SCHEMA) is False
    # A scalar default is NOT substituted: only whole objects get the
    # default treatment, per the oracle's `a ?? schema.meta.default`.
    scalar_default = Schema.object({"mode": Schema.string().default("plain")})
    assert equal_except_volatile({}, {"mode": "plain"}, scalar_default) is False


def test_equal_except_volatile_js_expr_is_opaque() -> None:
    schema = Schema.object({"mode": Schema.string()})
    # Identical out-of-scope expressions are equal; nothing is evaluated.
    older = {"mode": {"__jsExpr": "homePath('x')"}}
    assert equal_except_volatile(older, dict(older), schema) is True
    # A changed expression string is a raw change, still unevaluated.
    assert (
        equal_except_volatile(older, {"mode": {"__jsExpr": "process.env.Y"}}, schema)
        is False
    )
    # An expression node replacing a literal (and vice versa) is a change.
    assert equal_except_volatile(older, {"mode": "plain"}, schema) is False
    assert equal_except_volatile({"mode": "plain"}, older, schema) is False


def test_equal_except_volatile_unknown_fields_are_strict() -> None:
    older = {"level": 3, "extra": 1}
    assert equal_except_volatile(older, dict(older), _VOLATILE_SCHEMA) is True
    assert (
        equal_except_volatile(older, {"level": 3, "extra": 2}, _VOLATILE_SCHEMA)
        is False
    )


def test_equal_except_volatile_non_schema_falls_back_to_strict_deep_equal() -> None:
    # No schema, a callable schema, or a non-object schema: strict raw compare.
    assert equal_except_volatile({"a": 1}, {"a": 1}, None) is True
    assert equal_except_volatile({"a": 1}, {"a": 2}, None) is False
    assert equal_except_volatile({"a": True}, {"a": 1}, None) is False
    assert equal_except_volatile("a", "a", Schema.string()) is True
    assert equal_except_volatile("a", "b", Schema.string()) is False
    assert equal_except_volatile({"a": 1}, {"a": 1}, lambda v: v) is True
    # A dict-typed schema is not an object schema: strict raw compare.
    dict_schema = Schema.dict(Schema.string())
    assert equal_except_volatile({"k": "v"}, {"k": "v"}, dict_schema) is True
    assert equal_except_volatile({"k": "v"}, {"k": "w"}, dict_schema) is False


def test_equal_except_volatile_whole_schema_volatile() -> None:
    schema = Schema.object({"level": Schema.number()}).volatile()
    assert equal_except_volatile({"level": 1}, {"level": 2}, schema) is True
    assert equal_except_volatile({"level": 1}, {"other": 9}, schema) is True
    assert (
        equal_except_volatile({"level": 1}, {"level": 2}, Schema.string().volatile())
        is True
    )


def test_equal_except_volatile_recursive_schema_backedge() -> None:
    props: dict[str, Schema] = {}
    recursive = Schema.object(props)
    props["child"] = recursive
    # The backedge falls back to strict deepEqual for the nested value.
    older = {"child": {"child": {"v": 1}}}
    assert (
        equal_except_volatile(older, {"child": {"child": {"v": 1}}}, recursive) is True
    )
    assert (
        equal_except_volatile(older, {"child": {"child": {"v": 2}}}, recursive) is False
    )


# ---------------------------------------------------------------------------
# P1-6 — diff_entries is schema-aware; deletion round-trips through the journal
# ---------------------------------------------------------------------------


def test_diff_entries_schema_aware_volatile_only_is_no_diff() -> None:
    older = {"id": "a", "config": {"theme": "dark", "level": 3}}
    newer = {"id": "a", "config": {"theme": "light", "level": 3}}
    assert diff_entries(older, newer, _VOLATILE_SCHEMA) == {}
    # Without a schema the same change is a diff (strict raw compare).
    assert diff_entries(older, newer) == {"config": {"theme": "light", "level": 3}}
    # An ordinary change is a diff even with a schema.
    changed = {"id": "a", "config": {"theme": "dark", "level": 4}}
    assert diff_entries(older, changed, _VOLATILE_SCHEMA) == {
        "config": {"theme": "dark", "level": 4}
    }


def test_diff_entries_strict_bool_vs_int() -> None:
    older = {"id": "a", "config": {"flag": True}}
    newer = {"id": "a", "config": {"flag": 1}}
    assert diff_entries(older, newer) == {"config": {"flag": 1}}


def test_diff_entries_deletion_round_trips_through_journal() -> None:
    older = {
        "id": "a",
        "name": "x",
        "config": {"p": 1},
        "disabled": False,
    }
    newer = {"id": "a", "name": "x"}
    changes = diff_entries(older, newer)
    assert changes == {"config": None, "disabled": None}
    journal: Journal = {}
    merge(
        journal,
        "a",
        JournalRecord(kind="upsert", created=False, changes=changes),
    )
    # None deletes the key; it is never stored as a value.
    assert apply_journal([dict(older)], journal) == [{"id": "a", "name": "x"}]
    # A newly created entry skips None-valued changes entirely.
    journal2: Journal = {}
    merge(
        journal2,
        "b",
        JournalRecord(
            kind="upsert", created=True, changes={"name": "y", "config": None}
        ),
    )
    assert apply_journal([], journal2) == [{"id": "b", "name": "y"}]


def test_equal_except_volatile_non_record_object_value() -> None:
    # A nested object-typed field holding a non-dict (or an expression
    # node) falls back to strict deepEqual for that field.
    older = {"nested": "flat"}
    newer = {"nested": {"label": "a", "count": 1}}
    assert equal_except_volatile(older, newer, _VOLATILE_SCHEMA) is False
    assert equal_except_volatile(newer, older, _VOLATILE_SCHEMA) is False
    assert equal_except_volatile(older, dict(older), _VOLATILE_SCHEMA) is True
    expr = {"nested": {"__jsExpr": "homePath('x')"}}
    assert equal_except_volatile(expr, dict(expr), _VOLATILE_SCHEMA) is True
    assert equal_except_volatile(expr, newer, _VOLATILE_SCHEMA) is False


# ---------------------------------------------------------------------------
# P1-7 — apply_entry_patches
# ---------------------------------------------------------------------------


def test_apply_entry_patches_basic_and_input_never_mutated() -> None:
    base = [
        {"id": "a", "name": "logger", "config": {"level": "info"}},
        {"id": "b", "name": "watcher", "disabled": True},
        {"name": "entry_without_id"},
    ]
    snapshot = [
        {"id": "a", "name": "logger", "config": {"level": "info"}},
        {"id": "b", "name": "watcher", "disabled": True},
        {"name": "entry_without_id"},
    ]
    patches = [
        {"id": "a", "config": {"level": "debug"}},
        {"id": "b", "disabled": False},
        {"insert": [{"id": "c", "name": "timer"}]},
        {"id": "missing", "name": "ignored"},
        {"other_key": "ignored_patch"},
    ]
    warnings: list[str] = []
    patched = apply_entry_patches(base, patches, _warn_sink(warnings))
    assert len(patched) == 4
    assert patched[0]["config"]["level"] == "debug"
    assert patched[1]["disabled"] is False
    assert patched[2]["name"] == "entry_without_id"
    assert patched[3]["name"] == "timer"
    # The input rows are untouched: the tree owns the patched copies.
    assert base == snapshot
    # The missing id and the id-less patch both warn and are skipped.
    assert any("missing" in w for w in warnings)
    assert any("id is required" in w for w in warnings)


def test_apply_entry_patches_later_patch_targets_inserted_row() -> None:
    base = [{"id": "a", "name": "comp"}]
    patched = apply_entry_patches(
        base,
        [
            {"insert": [{"id": "b", "name": "other"}]},
            {"id": "b", "config": {"level": 5}},
        ],
    )
    assert [e["id"] for e in patched] == ["a", "b"]
    assert patched[1]["config"] == {"level": 5}


def test_apply_entry_patches_group_recursion_and_insert_into_group() -> None:
    base = [
        {
            "id": "g",
            "name": "group",
            "group": True,
            "config": [{"id": "m", "name": "comp", "config": {"level": 1}}],
        }
    ]
    warnings: list[str] = []
    patched = apply_entry_patches(
        base,
        [
            {"id": "m", "config": {"level": 2}},
            {"id": "g", "insert": [{"id": "n", "name": "comp"}]},
            {"id": "m", "insert": [{"id": "x", "name": "comp"}]},
        ],
        _warn_sink(warnings),
    )
    group = patched[0]
    assert group["config"][0]["config"]["level"] == 2
    assert [e["id"] for e in group["config"]] == ["m", "n"]
    # Insert into a non-group warns and skips.
    assert any("m" in w and "not a group" in w for w in warnings)


def test_apply_entry_patches_insert_into_missing_group_warns() -> None:
    warnings: list[str] = []
    patched = apply_entry_patches(
        [{"id": "a", "name": "comp"}],
        [{"id": "ghost", "insert": [{"id": "b", "name": "comp"}]}],
        _warn_sink(warnings),
    )
    assert patched == [{"id": "a", "name": "comp"}]
    assert any("ghost" in w and "not found" in w for w in warnings)


def test_apply_entry_patches_name_mismatch_warns_and_skips() -> None:
    warnings: list[str] = []
    patched = apply_entry_patches(
        [{"id": "a", "name": "logger"}],
        [{"id": "a", "name": "watcher", "config": {"level": "debug"}}],
        _warn_sink(warnings),
    )
    assert patched == [{"id": "a", "name": "logger"}]
    assert any("name mismatch" in w for w in warnings)
    # A matching name applies; the name itself is never an override.
    patched = apply_entry_patches(
        [{"id": "a", "name": "logger"}],
        [{"id": "a", "name": "logger", "config": {"level": "debug"}}],
    )
    assert patched[0] == {
        "id": "a",
        "name": "logger",
        "config": {"level": "debug"},
    }


def test_apply_entry_patches_group_config_replacement() -> None:
    base = [
        {
            "id": "g",
            "name": "group",
            "group": True,
            "config": [{"id": "old", "name": "comp"}],
        }
    ]
    patched = apply_entry_patches(
        base,
        [
            {
                "id": "g",
                "config": [{"name": "comp", "config": {"level": 3}}],
            },
            {"id": "old", "config": {"level": 9}},
        ],
    )
    # The old child is gone with the replaced config; the patch-supplied
    # child is mounted with an assigned id and is itself patchable.
    group = patched[0]
    assert len(group["config"]) == 1
    assert group["config"][0]["config"] == {"level": 3}
    assert _re_match_hex(group["config"][0]["id"])
    # The patch aimed at the removed child warned and skipped.
    assert group["config"][0]["id"] != "old"


def test_apply_entry_patches_no_patches_returns_deep_copy() -> None:
    base = [{"id": "a", "name": "comp", "config": {"level": 1}}]
    patched = apply_entry_patches(base)
    assert patched == base
    assert patched is not base
    assert patched[0] is not base[0]
    assert patched[0]["config"] is not base[0]["config"]
    # Empty insert is falsy: it falls through to the non-insert path,
    # and a patch with no overrides and no id still warns and skips.
    warnings: list[str] = []
    patched = apply_entry_patches(
        base,
        [{"insert": []}],
        _warn_sink(warnings),
    )
    assert patched == base
    assert any("id is required" in w for w in warnings)


def test_apply_entry_patches_rejects_non_list_and_non_dict_rows() -> None:
    with pytest.raises(IncludeError, match="expected a literal entry list"):
        apply_entry_patches("not-a-list")  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="entry must be an object"):
        apply_entry_patches([{"id": "a"}, "not-a-dict"])  # type: ignore[list-item]


def test_ensure_ids_and_insert_ids() -> None:
    entries: list[dict[str, Any]] = [
        {"name": "a"},
        {
            "name": "g",
            "group": True,
            "config": [{"name": "child"}],
        },
    ]
    ensure_ids(entries, lambda i: i in {"taken"})
    assert all(e["id"] for e in entries)
    assert entries[1]["config"][0]["id"]
    assert len({e["id"] for e in entries}) == 2
    patches = [{"insert": [{"name": "x"}, {"id": "fixed", "name": "y"}]}]
    ensure_insert_ids(patches, lambda i: False)
    assert _re_match_hex(patches[0]["insert"][0]["id"])
    assert patches[0]["insert"][1]["id"] == "fixed"
    ensure_insert_ids(None, lambda i: False)


# ---------------------------------------------------------------------------
# P1-7 — journal primitives: diff_options, merge_records, record
# ---------------------------------------------------------------------------


def test_diff_options_plain_deep_equal_and_none_deletes() -> None:
    older = {"id": "a", "name": "x", "config": {"p": 1}, "gone": True}
    newer = {"id": "a", "name": "x", "config": {"p": 2}}
    assert diff_options(older, newer) == {"config": {"p": 2}, "gone": None}
    # Strict: True is not 1, and the id never diffs.
    assert diff_options({"flag": True}, {"flag": 1}) == {"flag": 1}
    assert diff_options({"id": "a"}, {"id": "b"}) == {}
    assert diff_options({"a": 1}, {"a": 1}) == {}


def test_merge_records_branches() -> None:
    upsert = JournalRecord(kind="upsert", created=True, changes={"a": 1})
    # No earlier record: the newer one stands.
    assert merge_records(None, upsert) is upsert
    # Created then removed before ever being written: no trace.
    assert merge_records(upsert, JournalRecord(kind="remove")) is None
    # Updated then removed: the removal stands.
    updated = JournalRecord(kind="upsert", created=False, changes={"a": 1})
    removed = merge_records(updated, JournalRecord(kind="remove"))
    assert removed is not None and removed.kind == "remove"
    # Removed then upserted: a fresh upsert, not created.
    revived = merge_records(
        JournalRecord(kind="remove"),
        JournalRecord(kind="upsert", created=True, changes={"a": 2}),
    )
    assert revived is not None
    assert revived.kind == "upsert" and revived.created is False
    assert revived.changes == {"a": 2}
    # Two upserts merge changes; the newer parent/position win only on
    # relocation (parent set), otherwise the older place stands.
    first = JournalRecord(
        kind="upsert",
        created=True,
        parent=None,
        position=0,
        changes={"a": 1},
    )
    second = JournalRecord(
        kind="upsert",
        created=False,
        parent=_PARENT_UNSET,
        position=9,
        changes={"b": 2},
    )
    merged = merge_records(first, second)
    assert merged is not None
    assert merged.created is True
    assert merged.parent is None and merged.position == 0
    assert merged.changes == {"a": 1, "b": 2}
    relocated = JournalRecord(
        kind="upsert",
        created=False,
        parent="g",
        position=3,
        changes={"c": 3},
    )
    merged = merge_records(first, relocated)
    assert merged is not None
    assert merged.parent == "g" and merged.position == 3
    assert merged.changes == {"a": 1, "c": 3}


def test_merge_drops_cancelled_records() -> None:
    journal: Journal = {}
    merge(journal, "a", JournalRecord(kind="upsert", created=True, changes={}))
    merge(journal, "a", JournalRecord(kind="remove"))
    assert journal == {}


def _group_with_row() -> tuple[Context, LoaderService, dict[str, Any]]:
    ctx = Context()
    loader = LoaderService(ctx)
    row = {"id": "a", "name": "x", "config": {"p": 1}}
    loader.root.data.append(row)
    return ctx, loader, row


def test_record_create_update_remove_and_relocation() -> None:
    _, loader, row = _group_with_row()
    journal: Journal = {}
    # Creation: legacy None, all options except id become changes.
    # The options object is the row itself, so its position is 0.
    record(
        journal,
        EntryChange(id="a", group=loader.root, options=row),
        None,
    )
    rec = journal["a"]
    assert rec.kind == "upsert" and rec.created is True
    assert rec.parent is None and rec.position == 0
    assert rec.changes == {"name": "x", "config": {"p": 1}}
    # Update: diff against legacy, parent unset without relocation.
    journal.clear()
    record(
        journal,
        EntryChange(
            id="a",
            group=loader.root,
            options={"id": "a", "name": "x", "config": {"p": 2}},
            legacy=dict(row),
        ),
        None,
    )
    rec = journal["a"]
    assert rec.created is False
    assert rec.parent is _PARENT_UNSET
    assert rec.changes == {"config": {"p": 2}}
    # Relocation: from_group set, parent recorded.
    journal.clear()
    record(
        journal,
        EntryChange(
            id="a",
            group=loader.root,
            from_group=loader.root,
            options={"id": "a", "name": "x", "config": {"p": 2}},
            legacy=dict(row),
        ),
        "g1",
    )
    rec = journal["a"]
    assert rec.parent == "g1"
    # Removal: options None.
    journal.clear()
    record(journal, EntryChange(id="a", group=loader.root), None)
    assert journal["a"].kind == "remove"


# ---------------------------------------------------------------------------
# P1-7 — flatten / apply_changes / detach / place / apply_journal
# ---------------------------------------------------------------------------


def test_flatten_nested_groups_and_non_dict_row() -> None:
    data = [
        {"id": "a", "name": "x"},
        {
            "id": "g",
            "name": "group",
            "group": True,
            "config": [{"id": "m", "name": "y"}],
        },
        {"name": "anonymous"},
    ]
    flat = flatten(data)
    assert isinstance(flat["a"], FlatEntry)
    assert flat["a"].parent is None and flat["a"].position == 0
    assert flat["m"].parent == "g" and flat["m"].position == 0
    assert "anonymous" not in [e.options.get("name") for e in flat.values()]
    with pytest.raises(TypeError, match="entry must be an object"):
        flatten([{"id": "a"}, 42])  # type: ignore[list-item]


def test_apply_changes_none_deletes_and_filter() -> None:
    options = {"a": 1, "b": 2}
    apply_changes(options, {"b": None, "c": 3})
    assert options == {"a": 1, "c": 3}
    apply_changes(options, {"a": 9, "c": 8}, lambda key: key == "a")
    assert options == {"a": 9, "c": 3}


def test_detach_and_place() -> None:
    data: list[dict[str, Any]] = [
        {"id": "a"},
        {
            "id": "g",
            "name": "group",
            "group": True,
            "config": [{"id": "m"}],
        },
    ]
    assert detach(data, "ghost") is None
    removed = detach(data, "m")
    assert removed == {"id": "m"}
    assert data[1]["config"] == []
    assert place(data, {"id": "m"}, "g", 0) is True
    assert data[1]["config"] == [{"id": "m"}]
    # Position clamps to the group length.
    assert place(data, {"id": "z"}, "g", 99) is True
    assert [e["id"] for e in data[1]["config"]] == ["m", "z"]
    # Root placement and missing groups.
    assert place(data, {"id": "r"}, None, 0) is True
    assert data[0] == {"id": "r"}
    assert place(data, {"id": "q"}, "ghost", 0) is False
    assert place(data, {"id": "q"}, "a", 0) is False


def test_apply_journal_create_update_remove_move_and_filter() -> None:
    data = [
        {"id": "a", "name": "x", "config": {"p": 1}},
        {
            "id": "g",
            "name": "group",
            "group": True,
            "config": [{"id": "m", "name": "y"}],
        },
        {"id": "c", "name": "z"},
    ]
    journal: Journal = {
        "a": JournalRecord(kind="upsert", changes={"config": {"p": 2}}),
        "c": JournalRecord(kind="remove"),
        "n": JournalRecord(
            kind="upsert",
            created=True,
            parent="g",
            position=1,
            changes={"name": "new"},
        ),
        "m": JournalRecord(
            kind="upsert",
            created=False,
            parent=None,
            position=0,
            changes={"name": "moved"},
        ),
    }
    result = apply_journal(data, journal)
    assert [e["id"] for e in result] == ["m", "a", "g"]
    assert result[1]["config"] == {"p": 2}
    assert result[2]["config"] == [{"id": "n", "name": "new"}]
    # "m" moved out of the group to the root, keeping its changes.
    assert result[0] == {"id": "m", "name": "moved"}
    # A filter can hold back one key while the rest applies.
    data2 = [{"id": "a", "name": "x", "config": {"p": 1}}]
    journal2: Journal = {
        "a": JournalRecord(kind="upsert", changes={"name": "y", "config": {"p": 2}})
    }
    apply_journal(data2, journal2, lambda _id, key: key != "config")
    assert data2[0] == {"id": "a", "name": "y", "config": {"p": 1}}
    # An unplaceable creation warns instead of dropping silently.
    warnings: list[str] = []
    apply_journal(
        [],
        {
            "u": JournalRecord(
                kind="upsert",
                created=True,
                parent="ghost",
                position=0,
                changes={"name": "lost"},
            )
        },
        warn=_warn_sink(warnings),
    )
    assert any("cannot place entry" in w and "u" in w for w in warnings)


# ---------------------------------------------------------------------------
# P1-7 — reconcile (file wins)
# ---------------------------------------------------------------------------


def _flat(data: list[dict[str, Any]]) -> dict[str, FlatEntry]:
    return flatten(data)


def test_reconcile_key_conflict_file_wins() -> None:
    base = [{"id": "a", "name": "x", "config": {"p": 1}}]
    theirs = [{"id": "a", "name": "x", "config": {"p": 9}}]
    journal: Journal = {
        "a": JournalRecord(kind="upsert", created=False, changes={"config": {"p": 2}})
    }
    conflicts = reconcile(journal, _flat(base), _flat(theirs), lambda _id, _key: True)
    assert [c.reason for c in conflicts] == [
        'key "config" modified both in file and at runtime'
    ]
    assert all(isinstance(c, Conflict) and c.id == "a" for c in conflicts)
    # The only change was dropped, so the empty record is deleted.
    assert "a" not in journal
    # A key the file did not touch survives.
    journal = {
        "a": JournalRecord(
            kind="upsert",
            created=False,
            changes={"config": {"p": 2}, "name": "y"},
        )
    }
    conflicts = reconcile(journal, _flat(base), _flat(theirs), lambda _id, _key: True)
    assert len(conflicts) == 1
    assert journal["a"].changes == {"name": "y"}


def test_reconcile_create_remove_and_move_conflicts() -> None:
    base = [{"id": "a", "name": "x"}, {"id": "b", "name": "y"}]
    # Runtime-created entry the file also added: created demotes, kept.
    journal: Journal = {
        "n": JournalRecord(kind="upsert", created=True, changes={"name": "n"}),
    }
    theirs = [*base, {"id": "n", "name": "n"}]
    conflicts = reconcile(journal, _flat(base), _flat(theirs), lambda _id, _key: True)
    assert conflicts == []
    assert journal["n"].created is False
    # Runtime removal vs file modification: conflict, removal dropped.
    journal = {"b": JournalRecord(kind="remove")}
    theirs = [{"id": "a", "name": "x"}, {"id": "b", "name": "changed"}]
    conflicts = reconcile(journal, _flat(base), _flat(theirs), lambda _id, _key: True)
    assert [c.reason for c in conflicts] == ["modified in file, removed at runtime"]
    assert journal == {}
    # Runtime removal vs file removal: no conflict, record dropped.
    journal = {"b": JournalRecord(kind="remove")}
    conflicts = reconcile(
        journal, _flat(base), _flat([base[0]]), lambda _id, _key: True
    )
    assert conflicts == [] and journal == {}
    # Runtime modification vs file removal: conflict, change dropped.
    journal = {"b": JournalRecord(kind="upsert", created=False, changes={"name": "z"})}
    conflicts = reconcile(
        journal, _flat(base), _flat([base[0]]), lambda _id, _key: True
    )
    assert [c.reason for c in conflicts] == ["removed in file, modified at runtime"]
    assert journal == {}
    # Moved both in file and at runtime, to different groups: parent
    # reset, changes kept.
    base_g = [
        {
            "id": "g1",
            "name": "group",
            "group": True,
            "config": [{"id": "m", "name": "y"}],
        },
        {"id": "g2", "name": "group", "group": True, "config": []},
        {"id": "g3", "name": "group", "group": True, "config": []},
    ]
    theirs_g = [
        {"id": "g1", "name": "group", "group": True, "config": []},
        {"id": "g2", "name": "group", "group": True, "config": []},
        {
            "id": "g3",
            "name": "group",
            "group": True,
            "config": [{"id": "m", "name": "y"}],
        },
    ]
    journal = {
        "m": JournalRecord(
            kind="upsert",
            created=False,
            parent="g2",
            position=0,
            changes={"name": "z"},
        )
    }
    conflicts = reconcile(
        journal, _flat(base_g), _flat(theirs_g), lambda _id, _key: True
    )
    assert [c.reason for c in conflicts] == ["moved both in file and at runtime"]
    assert journal["m"].parent is _PARENT_UNSET
    assert journal["m"].changes == {"name": "z"}
    # A record with no changes left and no move is dropped.
    journal = {
        "a": JournalRecord(kind="upsert", created=False, changes={}),
    }
    conflicts = reconcile(journal, _flat(base), _flat(base), lambda _id, _key: True)
    assert conflicts == [] and journal == {}


def test_reconcile_scopes_to_file_owned() -> None:
    # Patch-owned entries and keys never conflict: the file cannot
    # express them, so reconcile skips them entirely.
    base = [{"id": "a", "name": "x", "config": {"p": 1}}]
    theirs = [{"id": "a", "name": "x", "config": {"p": 9}}]
    journal: Journal = {
        "a": JournalRecord(kind="upsert", created=False, changes={"config": {"p": 2}}),
        "p": JournalRecord(kind="upsert", created=True, changes={"name": "n"}),
    }
    conflicts = reconcile(
        journal,
        _flat(base),
        _flat(theirs),
        lambda entry_id, _key: entry_id == "a" and _key is None,
    )
    # The "a" record is file-owned as a whole, but its config key is
    # patch-owned, so the key conflict does not fire and the changes
    # survive untouched; the patch-owned record is skipped entirely.
    assert conflicts == []
    assert journal["a"].changes == {"config": {"p": 2}}
    assert "p" in journal


# ---------------------------------------------------------------------------
# P1-7 — PatchIndex and route_journal
# ---------------------------------------------------------------------------


def test_patch_index_owners() -> None:
    index = PatchIndex(
        {"a": EntryOwner(kind="file")},
        {"a": {"config": 0}},
    )
    assert index.entry("a") == EntryOwner(kind="file")
    assert index.key("a", "config") == EntryOwner(kind="patch", index=0)
    assert index.key("a", "name") == EntryOwner(kind="file")
    assert index.file_owned("a", "config") is False
    assert index.file_owned("a", "name") is True
    assert index.file_owned("a", None) is True
    assert index.entry("ghost") == EntryOwner(kind="file")
    assert index.entry(None) == EntryOwner(kind="file")


def test_route_journal_file_owned_key_goes_to_file() -> None:
    data = [{"id": "a", "name": "x", "config": {"p": 1}}]
    patches = [{"id": "a", "config": {"p": 2}}]
    journal: Journal = {
        "a": JournalRecord(kind="upsert", created=False, changes={"name": "y"})
    }
    patched = route_journal(journal, data, patches)
    assert patched is False
    assert data[0]["name"] == "y"
    assert patches[0] == {"id": "a", "config": {"p": 2}}


def test_route_journal_patch_owned_key_goes_to_patches() -> None:
    data = [{"id": "a", "name": "x", "config": {"p": 1}}]
    patches = [{"id": "a", "config": {"p": 2}}]
    journal: Journal = {
        "a": JournalRecord(kind="upsert", created=False, changes={"config": {"p": 5}})
    }
    patched = route_journal(journal, data, patches)
    assert patched is True
    assert data[0]["config"] == {"p": 1}
    assert patches[0]["config"] == {"p": 5}


def test_route_journal_inserted_entry_goes_to_patch_insert() -> None:
    data = [{"id": "a", "name": "x"}]
    patches = [{"insert": [{"id": "b", "name": "y"}]}]
    journal: Journal = {
        "b": JournalRecord(kind="upsert", created=False, changes={"name": "z"})
    }
    patched = route_journal(journal, data, patches)
    assert patched is True
    assert data == [{"id": "a", "name": "x"}]
    assert patches[0]["insert"] == [{"id": "b", "name": "z"}]


def test_route_journal_remove_and_cross_owner_move() -> None:
    data = [
        {"id": "a", "name": "x"},
        {
            "id": "g",
            "name": "group",
            "group": True,
            "config": [{"id": "m", "name": "y"}],
        },
    ]
    patches = [{"insert": [{"id": "b", "name": "y"}]}]
    journal: Journal = {
        "b": JournalRecord(kind="remove"),
        "m": JournalRecord(
            kind="upsert",
            created=False,
            parent=None,
            position=0,
            changes={"name": "moved"},
        ),
    }
    patched = route_journal(journal, data, patches)
    assert patched is True
    assert patches[0]["insert"] == []
    assert [e["id"] for e in data] == ["m", "a", "g"]
    assert data[0] == {"id": "m", "name": "moved"}
    assert data[2]["config"] == []


# ---------------------------------------------------------------------------
# P1-7 — Include: file-backed tree
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_include_round_trip_load_patch_write_reread(
    tmp_path: Path, mod: Any
) -> None:
    ctx, loader, inc_id, _entry, inc = await _boot(
        tmp_path,
        _rows(1, 2),
        patches=[
            {"id": "e0", "config": {"level": 10}},
            {
                "insert": [
                    {"id": "e2", "name": "inc_test_mod:comp", "config": {"level": 3}}
                ]
            },
            {"id": "e2", "config": {"level": 30}},
        ],
    )
    assert inc.root.data[0]["config"]["level"] == 10
    assert [e["id"] for e in inc.root.data] == ["e0", "e1", "e2"]
    assert inc.root.data[2]["config"]["level"] == 30
    assert mod.calls == [{"level": 10}, {"level": 2}, {"level": 30}]
    # Runtime update writes back through the journal.
    await loader.update(f"{inc_id}:e1", {"config": {"level": 20}})
    await _settle(inc, loader)
    file = tmp_path / "cfg.yaml"
    rows = load_yaml(file.read_text(encoding="utf-8"))
    assert [r["id"] for r in rows] == ["e0", "e1"]
    assert rows[0]["config"]["level"] == 1
    assert rows[1]["config"]["level"] == 20
    # The patch-inserted row stays in the patch list, not the file.
    assert loader.resolve(inc_id).options["config"]["patches"][1]["insert"] == [
        {"id": "e2", "name": "inc_test_mod:comp", "config": {"level": 3}}
    ]
    # A fresh boot from the same file re-reads the persisted state.
    _ctx2, _loader2, _id2, _e2, inc2 = await _boot(tmp_path, None)
    assert [e["id"] for e in inc2.root.data] == ["e0", "e1"]
    assert inc2.root.data[1]["config"]["level"] == 20
    await ctx.flush()


@pytest.mark.asyncio
async def test_include_missing_file_with_initial_creates_file(
    tmp_path: Path, mod: Any
) -> None:
    file = tmp_path / "fresh.yaml"
    ctx = Context()
    loader = LoaderService(ctx)
    initial = [
        {"id": "e0", "name": "inc_test_mod:comp", "config": {"level": 1}},
    ]
    inc_id = await loader.create(
        {
            "name": "include",
            "config": {"path": str(file), "initial": initial},
        }
    )
    await loader.await_all()
    await ctx.flush()
    entry = loader.resolve(inc_id)
    assert entry.fiber is not None
    assert entry.fiber.state == FiberState.ACTIVE
    assert file.exists()
    inc = entry.ctx.get("include", strict=False)
    assert isinstance(inc, Include)
    assert [e["id"] for e in inc.root.data] == ["e0"]
    assert mod.calls == [{"level": 1}]


@pytest.mark.asyncio
async def test_include_missing_file_empty_initial_counts(tmp_path: Path) -> None:
    # initial: [] is a real initial (checked with `is not None`, not
    # truthiness): the file is created empty and the tree stays empty.
    file = tmp_path / "fresh.yaml"
    ctx = Context()
    loader = LoaderService(ctx)
    inc_id = await loader.create(
        {"name": "include", "config": {"path": str(file), "initial": []}}
    )
    await loader.await_all()
    await ctx.flush()
    entry = loader.resolve(inc_id)
    assert entry.fiber is not None
    assert entry.fiber.state == FiberState.ACTIVE
    assert file.exists()
    assert load_yaml(file.read_text(encoding="utf-8")) == []


@pytest.mark.asyncio
async def test_include_json_round_trip(tmp_path: Path, mod: Any) -> None:
    _ctx, loader, inc_id, _entry, inc = await _boot(
        tmp_path,
        json.dumps(
            [
                {
                    "id": "e0",
                    "name": "inc_test_mod:comp",
                    "config": {"level": 1},
                }
            ]
        ),
        filename="cfg.json",
    )
    assert inc.root.data[0]["config"]["level"] == 1
    await loader.update(f"{inc_id}:e0", {"config": {"level": 4}})
    await _settle(inc, loader)
    file = tmp_path / "cfg.json"
    rows = json.loads(file.read_text(encoding="utf-8"))
    assert rows == [{"id": "e0", "name": "inc_test_mod:comp", "config": {"level": 4}}]
    assert mod.calls[-1] == {"level": 4}


@pytest.mark.asyncio
async def test_include_missing_file_without_initial_fails_loud(
    tmp_path: Path,
) -> None:
    ctx = Context()
    loader = LoaderService(ctx)
    inc_id = await loader.create(
        {"name": "include", "config": {"path": str(tmp_path / "gone.yaml")}}
    )
    await loader.await_all()
    await ctx.flush()
    entry = loader.resolve(inc_id)
    assert entry.fiber is not None
    assert entry.fiber.state == FiberState.FAILED
    assert isinstance(entry.fiber.error, IncludeError)
    assert "config file not found" in str(entry.fiber.error)


@pytest.mark.asyncio
async def test_include_non_array_top_level_fails_loud(tmp_path: Path) -> None:
    ctx = Context()
    loader = LoaderService(ctx)
    file = tmp_path / "cfg.yaml"
    file.write_text("name: not-a-list\n", encoding="utf-8")
    inc_id = await loader.create({"name": "include", "config": {"path": str(file)}})
    await loader.await_all()
    await ctx.flush()
    entry = loader.resolve(inc_id)
    assert entry.fiber is not None
    assert entry.fiber.state == FiberState.FAILED
    assert isinstance(entry.fiber.error, TypeError)
    assert "top-level array" in str(entry.fiber.error)


@pytest.mark.asyncio
async def test_include_unsupported_extension_fails_loud(tmp_path: Path) -> None:
    ctx = Context()
    loader = LoaderService(ctx)
    file = tmp_path / "cfg.txt"
    file.write_text("[]", encoding="utf-8")
    inc_id = await loader.create({"name": "include", "config": {"path": str(file)}})
    await loader.await_all()
    await ctx.flush()
    entry = loader.resolve(inc_id)
    assert entry.fiber is not None
    assert entry.fiber.state == FiberState.FAILED
    assert isinstance(entry.fiber.error, IncludeError)
    assert 'extension ".txt" not supported' in str(entry.fiber.error)


def test_include_config_must_be_mapping_and_path_required() -> None:
    ctx = Context()
    with pytest.raises(IncludeError, match="must be a mapping"):
        Include(ctx, "not-a-mapping")  # type: ignore[arg-type]
    with pytest.raises(IncludeError, match="requires a 'path' string"):
        Include(ctx, {})
    with pytest.raises(IncludeError, match="requires a 'path' string"):
        Include(ctx, {"path": ""})


@pytest.mark.asyncio
async def test_include_refresh_on_bad_file_warns_and_keeps_tree(
    tmp_path: Path, mod: Any
) -> None:
    ctx, _loader, _inc_id, _entry, inc = await _boot(tmp_path, _rows(1, 2))
    file = tmp_path / "cfg.yaml"
    file.write_text("name: not-a-list\n", encoding="utf-8")
    await inc.refresh()
    assert "top-level array" in _logger_text(ctx)
    assert [e["id"] for e in inc.root.data] == ["e0", "e1"]
    assert inc.root.data[0]["config"]["level"] == 1
    assert mod.calls == [{"level": 1}, {"level": 2}]
    # The file is still bad on the next refresh: the warning repeats,
    # and the tree still stands.
    file.write_text("still: bad\n", encoding="utf-8")
    await inc.refresh()
    assert [e["id"] for e in inc.root.data] == ["e0", "e1"]


@pytest.mark.asyncio
async def test_include_write_retries_transient_rename_failure(
    tmp_path: Path, mod: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ctx, loader, inc_id, _entry, inc = await _boot(tmp_path, _rows(1, 2))
    real_replace = os.replace
    attempts = {"n": 0}

    def _flaky(src: Any, dst: Any) -> None:
        attempts["n"] += 1
        if attempts["n"] <= 2:
            raise OSError(errno.EBUSY, "resource busy")
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", _flaky)
    await loader.update(f"{inc_id}:e1", {"config": {"level": 20}})
    await _settle(inc, loader)
    assert attempts["n"] == 3
    file = tmp_path / "cfg.yaml"
    rows = load_yaml(file.read_text(encoding="utf-8"))
    assert rows[1]["config"]["level"] == 20


@pytest.mark.asyncio
async def test_include_terminal_write_failure_keeps_journal_and_warns(
    tmp_path: Path, mod: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx, loader, inc_id, _entry, inc = await _boot(tmp_path, _rows(1, 2))
    file = tmp_path / "cfg.yaml"
    before = file.read_text(encoding="utf-8")
    failing = {"on": True}
    real_replace = os.replace

    def _failing(src: Any, dst: Any) -> None:
        if failing["on"]:
            raise OSError(errno.EACCES, "permission denied")
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", _failing)
    await loader.update(f"{inc_id}:e1", {"config": {"level": 20}})
    await _settle(inc, loader)
    assert "failed to write config file" in _logger_text(ctx)
    assert file.read_text(encoding="utf-8") == before
    # The change is kept, not dropped: once writes work again, removing
    # the entry flushes it to the file through stop().
    failing["on"] = False
    loader.remove(inc_id)
    await loader.ctx.flush()  # runs the scheduled dispose -> stop() flush
    await _settle(inc, loader)
    await ctx.flush()
    rows = load_yaml(file.read_text(encoding="utf-8"))
    assert rows[1]["config"]["level"] == 20


@pytest.mark.asyncio
async def test_include_stale_write_is_retried_not_lost(
    tmp_path: Path, mod: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ctx, loader, inc_id, _entry, inc = await _boot(tmp_path, _rows(1, 2))
    file = tmp_path / "cfg.yaml"
    real_read = Path.read_text
    calls = {"n": 0}

    def _tampered(self: Path, *args: Any, **kwargs: Any) -> str:
        calls["n"] += 1
        text = real_read(self, *args, **kwargs)
        # The second read is the stale pre-check inside the write:
        # report a file that changed under the writer.
        if calls["n"] == 2:
            return text.replace("level: 1", "level: 99")
        return text

    monkeypatch.setattr(Path, "read_text", _tampered)
    await loader.update(f"{inc_id}:e1", {"config": {"level": 20}})
    await _settle(inc, loader)
    assert calls["n"] >= 2
    rows = load_yaml(file.read_text(encoding="utf-8"))
    assert rows[1]["config"]["level"] == 20
    assert all(r["config"]["level"] != 99 for r in rows)


@pytest.mark.asyncio
async def test_include_readonly_file_warns_and_keeps_journal(
    tmp_path: Path, mod: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx, loader, inc_id, _entry, inc = await _boot(tmp_path, _rows(1, 2))
    monkeypatch.setattr(os, "access", lambda *a: False)
    await loader.update(f"{inc_id}:e1", {"config": {"level": 20}})
    await _settle(inc, loader)
    assert "read-only" in _logger_text(ctx)
    file = tmp_path / "cfg.yaml"
    rows = load_yaml(file.read_text(encoding="utf-8"))
    assert rows[1]["config"]["level"] == 2


@pytest.mark.asyncio
async def test_include_circular_self_include_fails_loud(
    tmp_path: Path,
) -> None:
    file = tmp_path / "cfg.yaml"
    file.write_text(
        f"- name: include\n  id: inner\n  config:\n    path: {file}\n",
        encoding="utf-8",
    )
    ctx = Context()
    loader = LoaderService(ctx)
    inc_id = await loader.create({"name": "include", "config": {"path": str(file)}})
    await loader.await_all()
    await ctx.flush()
    entry = loader.resolve(inc_id)
    inc = entry.ctx.get("include", strict=False)
    assert isinstance(inc, Include)
    inner = inc.resolve("inner")
    assert inner.fiber is not None
    assert inner.fiber.state == FiberState.FAILED
    assert isinstance(inner.fiber.error, IncludeError)
    assert "circular include detected" in str(inner.fiber.error)
    # The outer tree still stands.
    assert loader.resolve(inc_id).fiber is not None
    assert loader.resolve(inc_id).fiber.state == FiberState.ACTIVE


@pytest.mark.asyncio
async def test_include_circular_mutual_include_fails_loud(
    tmp_path: Path,
) -> None:
    file_a = tmp_path / "a.yaml"
    file_b = tmp_path / "b.yaml"
    file_a.write_text(
        f"- name: include\n  id: b-inc\n  config:\n    path: {file_b}\n",
        encoding="utf-8",
    )
    file_b.write_text(
        f"- name: include\n  id: a-inc\n  config:\n    path: {file_a}\n",
        encoding="utf-8",
    )
    ctx = Context()
    loader = LoaderService(ctx)
    inc_id = await loader.create({"name": "include", "config": {"path": str(file_a)}})
    await loader.await_all()
    await ctx.flush()
    entry = loader.resolve(inc_id)
    inc = entry.ctx.get("include", strict=False)
    assert isinstance(inc, Include)
    b_entry = inc.resolve("b-inc")
    assert b_entry.fiber is not None
    assert b_entry.fiber.state == FiberState.ACTIVE
    b_inc = b_entry.ctx.get("include", strict=False)
    assert isinstance(b_inc, Include)
    a_entry = b_inc.resolve("a-inc")
    assert a_entry.fiber is not None
    assert a_entry.fiber.state == FiberState.FAILED
    assert "circular include detected" in str(a_entry.fiber.error)


@pytest.mark.asyncio
async def test_include_patch_on_inserted_row_and_missing_id_warns(
    tmp_path: Path, mod: Any
) -> None:
    ctx, _loader, _inc_id, _entry, inc = await _boot(
        tmp_path,
        _rows(1),
        patches=[
            {
                "insert": [
                    {"id": "e1", "name": "inc_test_mod:comp", "config": {"level": 2}}
                ]
            },
            {"id": "e1", "config": {"level": 20}},
            {"id": "ghost", "config": {"level": 9}},
        ],
    )
    assert "ghost" in _logger_text(ctx) and "not found" in _logger_text(ctx)
    assert [e["id"] for e in inc.root.data] == ["e0", "e1"]
    assert inc.root.data[1]["config"]["level"] == 20


# ---------------------------------------------------------------------------
# P1-7 — Include: journal write-back and reconciliation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_include_journal_file_wins_conflict(tmp_path: Path, mod: Any) -> None:
    ctx, loader, inc_id, _entry, inc = await _boot(tmp_path, _rows(1, 2))
    file = tmp_path / "cfg.yaml"
    # The file changes underneath the runtime change; the drain must
    # reconcile file-wins. The edit lands BEFORE the update, matching
    # the oracle's own conflict test: with commit-before-await (B1) the
    # journal is recorded and the drain settles inside the update await,
    # so an edit after the await is only seen via refresh/watch — the
    # deterministic conflict window is an edit that lands before (or
    # during) the update, which the drain's stale check catches.
    file.write_text(_rows(9, 2), encoding="utf-8")
    await loader.update(f"{inc_id}:e0", {"config": {"level": 2}})
    await _settle(inc, loader)
    assert "config conflict" in _logger_text(ctx)
    assert "file wins" in _logger_text(ctx)
    assert inc.root.data[0]["config"]["level"] == 9
    assert file.read_text(encoding="utf-8") == _rows(9, 2)
    assert mod.calls[-1] == {"level": 9}


@pytest.mark.asyncio
async def test_include_post_await_external_edit_picked_up_via_refresh(
    tmp_path: Path, mod: Any
) -> None:
    """Item 10: an external edit made AFTER `await loader.update(...)`
    is not seen until a refresh — the drain already settled inside the
    update await — but refresh() picks it up."""
    _ctx, loader, inc_id, _entry, inc = await _boot(tmp_path, _rows(1, 2))
    file = tmp_path / "cfg.yaml"
    await loader.update(f"{inc_id}:e0", {"config": {"level": 2}})
    await _settle(inc, loader)
    assert inc.root.data[0]["config"]["level"] == 2
    # The edit lands after the await: nothing re-reads the file until
    # refresh, so the live tree still holds the runtime value.
    file.write_text(_rows(9, 2), encoding="utf-8")
    assert inc.root.data[0]["config"]["level"] == 2
    await inc.refresh()
    await _settle(inc, loader)
    assert inc.root.data[0]["config"]["level"] == 9
    assert mod.calls[-1] == {"level": 9}


@pytest.mark.asyncio
async def test_include_runtime_create_placed_at_parent_position(
    tmp_path: Path, mod: Any
) -> None:
    _ctx, loader, _inc_id, _entry, inc = await _boot(tmp_path, _rows(1, 2))
    await inc.create(
        {
            "name": "inc_test_mod:comp",
            "id": "e2",
            "config": {"level": 3},
        },
        position=1,
    )
    await _settle(inc, loader)
    file = tmp_path / "cfg.yaml"
    rows = load_yaml(file.read_text(encoding="utf-8"))
    assert [r["id"] for r in rows] == ["e0", "e2", "e1"]
    assert rows[1]["config"]["level"] == 3


@pytest.mark.asyncio
async def test_include_runtime_create_inside_group(tmp_path: Path, mod: Any) -> None:
    content = (
        "- name: group\n  id: g\n  group: true\n  config:\n"
        "  - name: inc_test_mod:comp\n    id: m0\n    config:\n      level: 1\n"
        "- name: inc_test_mod:comp\n  id: e0\n  config:\n    level: 2\n"
    )
    _ctx, loader, _inc_id, _entry, inc = await _boot(tmp_path, content)
    await inc.create(
        {
            "name": "inc_test_mod:comp",
            "id": "m1",
            "config": {"level": 5},
        },
        parent="g",
        position=0,
    )
    await _settle(inc, loader)
    file = tmp_path / "cfg.yaml"
    rows = load_yaml(file.read_text(encoding="utf-8"))
    group = rows[0]
    assert [r["id"] for r in group["config"]] == ["m1", "m0"]
    assert group["config"][0]["config"]["level"] == 5


@pytest.mark.asyncio
async def test_include_runtime_removal_persisted(tmp_path: Path, mod: Any) -> None:
    _ctx, loader, _inc_id, _entry, inc = await _boot(tmp_path, _rows(1, 2))
    inc.remove("e1")
    await _settle(inc, loader)
    file = tmp_path / "cfg.yaml"
    rows = load_yaml(file.read_text(encoding="utf-8"))
    assert [r["id"] for r in rows] == ["e0"]
    assert "e1" not in inc.store


@pytest.mark.asyncio
async def test_include_patch_owned_change_routed_not_written_to_file(
    tmp_path: Path, mod: Any
) -> None:
    _ctx, loader, inc_id, _entry, inc = await _boot(
        tmp_path,
        _rows(1, 2),
        patches=[{"id": "e0", "config": {"level": 9}}],
    )
    file = tmp_path / "cfg.yaml"
    before = file.read_text(encoding="utf-8")
    assert inc.root.data[0]["config"]["level"] == 9
    await loader.update(f"{inc_id}:e0", {"config": {"level": 5}})
    await _settle(inc, loader)
    # The file is byte-identical: the patch-owned key never lands there.
    assert file.read_text(encoding="utf-8") == before
    # The change lives in the patch list on the loader row instead.
    patches = loader.resolve(inc_id).options["config"]["patches"]
    assert patches == [{"id": "e0", "config": {"level": 5}}]
    assert inc.root.data[0]["config"]["level"] == 5


@pytest.mark.asyncio
async def test_include_internal_update_veto_adopts_patches_without_restart(
    tmp_path: Path, mod: Any
) -> None:
    _ctx, loader, inc_id, entry, inc = await _boot(
        tmp_path,
        _rows(1, 2),
        patches=[{"id": "e0", "config": {"level": 9}}],
    )
    assert entry.fiber is not None
    uid_before = entry.fiber.uid
    file = tmp_path / "cfg.yaml"
    await loader.update(
        inc_id,
        {
            "config": {
                "path": str(file),
                "patches": [{"id": "e0", "config": {"level": 7}}],
            }
        },
    )
    await _settle(inc, loader)
    assert entry.fiber is not None
    assert entry.fiber.uid == uid_before
    assert entry.fiber.state == FiberState.ACTIVE
    assert inc.root.data[0]["config"]["level"] == 7
    assert mod.calls[-1] == {"level": 7}


@pytest.mark.asyncio
async def test_include_remove_right_after_update_does_not_lose_write(
    tmp_path: Path, mod: Any
) -> None:
    ctx, loader, inc_id, _entry, inc = await _boot(tmp_path, _rows(1, 2))
    await loader.update(f"{inc_id}:e1", {"config": {"level": 20}})
    # No settle: the change is still journalled when the tree goes away.
    loader.remove(inc_id)
    await _settle(inc, loader)
    await ctx.flush()
    file = tmp_path / "cfg.yaml"
    rows = load_yaml(file.read_text(encoding="utf-8"))
    assert rows[1]["config"]["level"] == 20


@pytest.mark.asyncio
async def test_include_apply_loop_failure_is_logged(
    tmp_path: Path, mod: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx, _loader, _inc_id, _entry, inc = await _boot(tmp_path, _rows(1, 2))
    file = tmp_path / "cfg.yaml"

    async def _failing(_tree: Any) -> None:
        raise RuntimeError("apply boom")

    monkeypatch.setattr(inc.root, "update", _failing)
    file.write_text(_rows(9, 2), encoding="utf-8")
    await inc.refresh()
    assert "apply boom" in _logger_text(ctx)
    # The failed apply leaves the old tree in place.
    assert inc.root.data[0]["config"]["level"] == 1


# ---------------------------------------------------------------------------
# P1-7 review fixes (PR #40 re-review)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_include_rapid_updates_coalesce_to_final_state(
    tmp_path: Path, mod: Any
) -> None:
    """B1: five sequential runtime updates; the file must end at the LAST one.

    The commit is journalled before the fiber restart is awaited, so a
    stale reconcile applying afterwards cannot clobber the merge.
    """
    _ctx, loader, inc_id, _entry, inc = await _boot(tmp_path, _rows(1))
    for level in (10, 20, 30, 40, 50):
        await loader.update(f"{inc_id}:e0", {"config": {"level": level}})
    await _settle(inc, loader)
    file = tmp_path / "cfg.yaml"
    assert load_yaml(file.read_text(encoding="utf-8"))[0]["config"]["level"] == 50
    assert inc.root.data[0]["config"]["level"] == 50


@pytest.mark.asyncio
async def test_include_cross_entry_update_while_apply_in_flight(
    tmp_path: Path, mod: Any
) -> None:
    """B1: an update to e1 made while the e0 apply is in flight is not lost."""
    _ctx, loader, inc_id, _entry, inc = await _boot(tmp_path, _rows(1, 2))
    await loader.update(f"{inc_id}:e0", {"config": {"level": 10}})
    await asyncio.sleep(0)
    await loader.update(f"{inc_id}:e1", {"config": {"level": 20}})
    await _settle(inc, loader)
    file = tmp_path / "cfg.yaml"
    rows = load_yaml(file.read_text(encoding="utf-8"))
    assert rows[1]["config"]["level"] == 20
    assert inc.root.data[1]["config"]["level"] == 20


@pytest.mark.asyncio
async def test_include_stale_pending_tree_healed_when_file_missing(
    tmp_path: Path, mod: Any
) -> None:
    """F1: an adopt stages a stale pending tree (e1 patched to 8, e0 at
    1); a live update to e0=50 races it while the file is missing, so
    the drain cannot write and — before this fix — scheduled no fresh
    tree, leaving the stale apply final. The failed write now schedules
    a fresh tree from cache + journal, so the live value survives."""
    _ctx, loader, inc_id, _entry, inc = await _boot(tmp_path, _rows(1, 2))
    file = tmp_path / "cfg.yaml"
    task = await inc._on_internal_update(
        {"path": str(file), "patches": [{"id": "e1", "config": {"level": 8}}]},
        None,
    )
    assert isinstance(task, asyncio.Task)
    await asyncio.sleep(0)
    file.unlink()
    await loader.update(f"{inc_id}:e0", {"config": {"level": 50}})
    await task
    await _settle(inc, loader)
    assert inc.root.data[0]["config"]["level"] == 50
    assert inc.root.data[1]["config"]["level"] == 8
    assert not file.exists()


@pytest.mark.asyncio
async def test_include_stale_pending_tree_healed_when_file_read_only(
    tmp_path: Path, mod: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F1, read-only variant: the same race, but the drain cannot write
    because the file is read-only. The sandbox runs as root, so a real
    chmod would not block the write; os.access is stubbed instead, the
    same technique as test_include_readonly_file_warns_and_keeps_journal."""
    _ctx, loader, inc_id, _entry, inc = await _boot(tmp_path, _rows(1, 2))
    file = tmp_path / "cfg.yaml"
    task = await inc._on_internal_update(
        {"path": str(file), "patches": [{"id": "e1", "config": {"level": 8}}]},
        None,
    )
    assert isinstance(task, asyncio.Task)
    await asyncio.sleep(0)
    monkeypatch.setattr(os, "access", lambda *a: False)
    await loader.update(f"{inc_id}:e0", {"config": {"level": 50}})
    await task
    await _settle(inc, loader)
    assert inc.root.data[0]["config"]["level"] == 50
    assert inc.root.data[1]["config"]["level"] == 8
    rows = load_yaml(file.read_text(encoding="utf-8"))
    assert [row["config"]["level"] for row in rows] == [1, 2]


@pytest.mark.asyncio
async def test_include_on_internal_update_sync_next_fn(
    tmp_path: Path, mod: Any
) -> None:
    """B3: a foreign config with a synchronous delegate returns its result.

    Mirrors Group._on_update's direct-call test; the waterfall always
    passes an async next_fn, but the branch executes on direct call.
    """
    _ctx, _loader, _inc_id, _entry, inc = await _boot(tmp_path, _rows(1))
    result = await inc._on_internal_update(
        {"path": str(tmp_path / "other.yaml")}, lambda cfg: "sync_done"
    )
    assert result == "sync_done"
    assert inc.root.data[0]["config"]["level"] == 1


@pytest.mark.asyncio
async def test_include_on_internal_update_without_next_fn_is_noop(
    tmp_path: Path, mod: Any
) -> None:
    """B3: a foreign config with no delegate is a no-op returning None."""
    _ctx, _loader, _inc_id, _entry, inc = await _boot(tmp_path, _rows(1))
    result = await inc._on_internal_update({"path": str(tmp_path / "other.yaml")}, None)
    assert result is None
    assert inc.root.data[0]["config"]["level"] == 1


@pytest.mark.asyncio
async def test_include_on_internal_update_returns_apply_task(
    tmp_path: Path, mod: Any
) -> None:
    """NB3: adopting an update returns the pending apply task, so a direct
    caller awaiting it awaits the re-apply (through the fiber update the
    return is discarded)."""
    _ctx, _loader, _inc_id, _entry, inc = await _boot(
        tmp_path, _rows(1, 2), patches=[{"id": "e0", "config": {"level": 9}}]
    )
    file = tmp_path / "cfg.yaml"
    task = await inc._on_internal_update(
        {"path": str(file), "patches": [{"id": "e0", "config": {"level": 7}}]},
        None,
    )
    assert isinstance(task, asyncio.Task)
    await task
    assert inc.root.data[0]["config"]["level"] == 7
    assert mod.calls[-1] == {"level": 7}


@pytest.mark.asyncio
async def test_include_internal_update_veto_rejects_js_expr_patches(
    tmp_path: Path, mod: Any
) -> None:
    """B4: the veto path validates patches exactly as construction does.

    Both shapes fail loud via fiber.update: a JsExpr patch, and a JsExpr
    insert item. Neither is adopted, mounted, or persisted; the include
    fiber stays ACTIVE.
    """
    _ctx, loader, _inc_id, entry, inc = await _boot(tmp_path, _rows(1))
    file = tmp_path / "cfg.yaml"
    before = file.read_bytes()
    assert entry.fiber is not None
    fiber = entry.fiber
    with pytest.raises(IncludeError, match="include patches must be literal mappings"):
        await fiber.update({"path": str(file), "patches": [{"__jsExpr": "1 + 1"}]})
    with pytest.raises(IncludeError, match="expected a literal entry list"):
        await fiber.update(
            {"path": str(file), "patches": [{"insert": [{"__jsExpr": "1 + 1"}]}]}
        )
    await _settle(inc, loader)
    assert fiber.state == FiberState.ACTIVE
    assert inc.config.get("patches") is None
    assert inc.root.data == [
        {"id": "e0", "name": "inc_test_mod:comp", "config": {"level": 1}}
    ]
    assert file.read_bytes() == before


# ---------------------------------------------------------------------------
# P1-7 — commit protocol: direct fiber update and self-dispose
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_include_direct_fiber_update_records_through_on_commit(
    tmp_path: Path, mod: Any
) -> None:
    _ctx, loader, inc_id, _entry, inc = await _boot(tmp_path, _rows(1, 2))
    child = loader.resolve(f"{inc_id}:e1")
    assert child.fiber is not None
    await child.fiber.update({"level": 99})
    await _settle(inc, loader)
    file = tmp_path / "cfg.yaml"
    rows = load_yaml(file.read_text(encoding="utf-8"))
    assert rows[1]["config"]["level"] == 99
    assert mod.calls[-1] == {"level": 99}


@pytest.mark.asyncio
async def test_include_self_dispose_marks_disabled_and_journals(
    tmp_path: Path, mod: Any
) -> None:
    content = (
        "- name: inc_test_mod:self_disposer\n  id: doomed\n"
        "- name: inc_test_mod:comp\n  id: e0\n  config:\n    level: 1\n"
    )
    _ctx, loader, _inc_id, _entry, inc = await _boot(tmp_path, content)
    await _settle(inc, loader)
    doomed = inc.resolve("doomed")
    assert doomed.options.get("disabled") is True
    assert doomed.fiber is not None
    assert doomed.fiber.state == FiberState.DISPOSED
    file = tmp_path / "cfg.yaml"
    rows = load_yaml(file.read_text(encoding="utf-8"))
    assert rows[0] == {
        "name": "inc_test_mod:self_disposer",
        "id": "doomed",
        "disabled": True,
    }
    assert rows[1]["config"]["level"] == 1
    # The surviving entry is untouched.
    assert inc.resolve("e0").fiber is not None
    assert inc.resolve("e0").fiber.state == FiberState.ACTIVE


# ---------------------------------------------------------------------------
# P1-7 — coverage: edge branches of the patch / journal / write paths
# ---------------------------------------------------------------------------


def test_apply_journal_filter_rejects_whole_entry() -> None:
    data = [
        {"id": "a", "name": "x"},
        {"id": "b", "name": "y"},
    ]
    journal: Journal = {
        "a": JournalRecord(kind="upsert", changes={"name": "a2"}),
        "b": JournalRecord(kind="upsert", changes={"name": "b2"}),
    }
    apply_journal(data, journal, lambda _id, _key: _id != "a")
    assert data[0] == {"id": "a", "name": "x"}
    assert data[1] == {"id": "b", "name": "b2"}


def test_apply_journal_move_to_missing_group_restores() -> None:
    data: list[dict[str, Any]] = [
        {
            "id": "g",
            "name": "group",
            "group": True,
            "config": [{"id": "m", "name": "y"}],
        }
    ]
    warnings: list[str] = []
    apply_journal(
        data,
        {
            "m": JournalRecord(
                kind="upsert",
                created=False,
                parent="ghost",
                position=0,
                changes={"name": "moved"},
            )
        },
        warn=_warn_sink(warnings),
    )
    assert any("cannot move entry" in w and "m" in w for w in warnings)
    # The move failed, so the entry is restored to its old place,
    # keeping the applied changes.
    assert data[0]["config"] == [{"id": "m", "name": "moved"}]


def test_place_creates_missing_group_config() -> None:
    data: list[dict[str, Any]] = [
        {"id": "g", "name": "group", "group": True},
    ]
    assert place(data, {"id": "x"}, "g", 0) is True
    assert data[0]["config"] == [{"id": "x"}]


def test_ensure_ids_collision_retries() -> None:
    calls = {"n": 0}

    def _used(candidate: str) -> bool:
        calls["n"] += 1
        return calls["n"] == 1

    entries: list[dict[str, Any]] = [{"name": "a"}]
    ensure_ids(entries, _used)
    assert calls["n"] == 2
    assert _re_match_hex(entries[0]["id"])


def test_apply_entry_patches_unmount_anonymous_and_nested_group() -> None:
    base = [
        {
            "id": "g",
            "name": "group",
            "group": True,
            "config": [
                {"name": "anonymous"},
                {
                    "id": "sub",
                    "name": "group",
                    "group": True,
                    "config": [{"id": "deep", "name": "comp"}],
                },
            ],
        }
    ]
    warnings: list[str] = []
    patched = apply_entry_patches(
        base,
        [
            {"id": "g", "config": [{"id": "fresh", "name": "comp"}]},
            {"id": "deep", "config": {"level": 9}},
        ],
        _warn_sink(warnings),
    )
    assert patched[0]["config"] == [{"id": "fresh", "name": "comp"}]
    # The unmounted nested child is gone, so the patch aimed at it
    # warns and skips.
    assert any("deep" in w and "not found" in w for w in warnings)


def test_apply_entry_patches_insert_into_group_with_non_list_config() -> None:
    base = [
        {
            "id": "g",
            "name": "group",
            "group": True,
            "config": {"level": 1},
        }
    ]
    patched = apply_entry_patches(
        base, [{"id": "g", "insert": [{"id": "n", "name": "comp"}]}]
    )
    assert patched[0]["config"] == [{"id": "n", "name": "comp"}]


def test_reconcile_remove_unchanged_and_added_in_file() -> None:
    base = [{"id": "a", "name": "x"}, {"id": "b", "name": "y"}]
    # The file did not touch the removed entry: the removal stands.
    journal: Journal = {"b": JournalRecord(kind="remove")}
    conflicts = reconcile(journal, _flat(base), _flat(base), lambda _id, _key: True)
    assert conflicts == []
    assert journal["b"].kind == "remove"
    # The file added the entry the runtime removed: conflict, drop.
    journal = {"n": JournalRecord(kind="remove")}
    theirs = [*base, {"id": "n", "name": "new"}]
    conflicts = reconcile(journal, _flat(base), _flat(theirs), lambda _id, _key: True)
    assert [c.reason for c in conflicts] == ["added in file, removed at runtime"]
    assert journal == {}
    # A runtime-created entry the file never saw: no conflict, kept.
    journal = {"n": JournalRecord(kind="upsert", created=True, changes={"name": "n"})}
    conflicts = reconcile(journal, _flat(base), _flat(base), lambda _id, _key: True)
    assert conflicts == []
    assert journal["n"].created is True


def test_route_journal_create_under_patch_config_group() -> None:
    data = [
        {
            "id": "g",
            "name": "group",
            "group": True,
            "config": [{"id": "m0", "name": "comp"}],
        }
    ]
    patches = [{"id": "g", "config": [{"id": "m1", "name": "comp"}]}]
    journal: Journal = {
        "n": JournalRecord(
            kind="upsert",
            created=True,
            parent="g",
            position=1,
            changes={"name": "new"},
        )
    }
    patched = route_journal(journal, data, patches)
    assert patched is True
    assert patches[0]["config"] == [
        {"id": "m1", "name": "comp"},
        {"id": "n", "name": "new"},
    ]
    assert data[0]["config"] == [{"id": "m0", "name": "comp"}]


def test_route_journal_create_under_patch_dict_config_falls_back_to_file() -> None:
    data = [
        {
            "id": "g",
            "name": "group",
            "group": True,
            "config": [{"id": "m0", "name": "comp"}],
        }
    ]
    patches = [{"id": "g", "config": {"level": 1}}]
    journal: Journal = {
        "n": JournalRecord(
            kind="upsert",
            created=True,
            parent="g",
            position=1,
            changes={"name": "new"},
        )
    }
    patched = route_journal(journal, data, patches)
    assert patched is False
    assert data[0]["config"] == [
        {"id": "m0", "name": "comp"},
        {"id": "n", "name": "new"},
    ]
    assert patches == [{"id": "g", "config": {"level": 1}}]


def test_route_journal_create_under_inserted_group() -> None:
    data = [{"id": "a", "name": "x"}]
    patches = [
        {"insert": [{"id": "ig", "name": "group", "group": True}]},
    ]
    journal: Journal = {
        "n": JournalRecord(
            kind="upsert",
            created=True,
            parent="ig",
            position=0,
            changes={"name": "new"},
        )
    }
    patched = route_journal(journal, data, patches)
    assert patched is True
    assert patches[0]["insert"][0]["config"] == [{"id": "n", "name": "new"}]
    assert data == [{"id": "a", "name": "x"}]


def test_route_journal_create_under_file_group() -> None:
    data = [
        {
            "id": "g",
            "name": "group",
            "group": True,
            "config": [{"id": "a", "name": "x"}],
        }
    ]
    patches: list[dict[str, Any]] = []
    journal: Journal = {
        "n": JournalRecord(
            kind="upsert",
            created=True,
            parent="g",
            position=1,
            changes={"name": "new"},
        )
    }
    patched = route_journal(journal, data, patches)
    assert patched is False
    assert data[0]["config"] == [
        {"id": "a", "name": "x"},
        {"id": "n", "name": "new"},
    ]
    assert patches == []


def test_route_journal_create_under_inserted_group_with_config() -> None:
    data = [{"id": "a", "name": "x"}]
    patches = [
        {
            "insert": [
                {
                    "id": "ig",
                    "name": "group",
                    "group": True,
                    "config": [{"id": "c", "name": "x"}],
                }
            ]
        },
    ]
    journal: Journal = {
        "n": JournalRecord(
            kind="upsert",
            created=True,
            parent="ig",
            position=1,
            changes={"name": "new"},
        )
    }
    patched = route_journal(journal, data, patches)
    assert patched is True
    assert patches[0]["insert"][0]["config"] == [
        {"id": "c", "name": "x"},
        {"id": "n", "name": "new"},
    ]
    assert data == [{"id": "a", "name": "x"}]


def test_route_journal_create_under_missing_group_warns() -> None:
    data = [{"id": "a", "name": "x"}]
    patches: list[dict[str, Any]] = []
    warnings: list[str] = []
    journal: Journal = {
        "n": JournalRecord(
            kind="upsert",
            created=True,
            parent="ghost",
            position=0,
            changes={"name": "new"},
        )
    }
    patched = route_journal(journal, data, patches, _warn_sink(warnings))
    assert patched is False
    assert any("cannot place entry" in w and "n" in w for w in warnings)
    assert data == [{"id": "a", "name": "x"}]


def test_route_journal_descendant_move_warns_and_drops() -> None:
    """B2: moving a group under its own descendant detaches it first, so
    the flatten lookup for the target misses; the entry warns and drops
    instead of placing."""
    data: list[dict[str, Any]] = []
    patches = [
        {
            "insert": [
                {
                    "id": "g",
                    "name": "group",
                    "group": True,
                    "config": [
                        {"id": "p", "name": "group", "group": True, "config": []}
                    ],
                }
            ]
        }
    ]
    journal: Journal = {
        "g": JournalRecord(
            kind="upsert", created=False, parent="p", position=0, changes={}
        )
    }
    warnings: list[str] = []
    route_journal(journal, data, patches, _warn_sink(warnings))
    assert any("cannot place entry" in w and "g" in w for w in warnings)
    assert data == []
    assert patches[0]["insert"] == []


# ---------------------------------------------------------------------------
# P1-7 — coverage: Include edge branches
# ---------------------------------------------------------------------------


def test_include_config_none_is_empty() -> None:
    ctx = Context()
    with pytest.raises(IncludeError, match="requires a 'path' string"):
        Include(ctx)


@pytest.mark.asyncio
async def test_include_enable_logs_from_config(tmp_path: Path, mod: Any) -> None:
    _ctx, _loader, _inc_id, _entry, inc = await _boot(
        tmp_path, _rows(1), extra={"enableLogs": True}
    )
    assert inc.enable_logs is True
    _ctx2, _loader2, _inc_id2, _entry2, inc2 = await _boot(tmp_path, None)
    assert inc2.enable_logs is False


@pytest.mark.asyncio
async def test_include_nested_relative_path_resolves_against_outer_file(
    tmp_path: Path, mod: Any
) -> None:
    inner = tmp_path / "inner.yaml"
    inner.write_text(_rows(7), encoding="utf-8")
    outer = tmp_path / "outer.yaml"
    outer.write_text(
        "- name: include\n  id: inner\n  config:\n    path: ./inner.yaml\n",
        encoding="utf-8",
    )
    ctx = Context()
    loader = LoaderService(ctx)
    inc_id = await loader.create({"name": "include", "config": {"path": str(outer)}})
    await loader.await_all()
    await ctx.flush()
    entry = loader.resolve(inc_id)
    inc = entry.ctx.get("include", strict=False)
    assert isinstance(inc, Include)
    inner_entry = inc.resolve("inner")
    assert inner_entry.fiber is not None
    assert inner_entry.fiber.state == FiberState.ACTIVE
    inner_inc = inner_entry.ctx.get("include", strict=False)
    assert isinstance(inner_inc, Include)
    assert inner_inc.root.data[0]["config"]["level"] == 7


@pytest.mark.asyncio
async def test_include_relative_path_without_base_url_resolves_against_cwd(
    tmp_path: Path, mod: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    file = tmp_path / "cfg.yaml"
    file.write_text(_rows(3), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    ctx = Context()
    loader = LoaderService(ctx)
    inc_id = await loader.create({"name": "include", "config": {"path": "cfg.yaml"}})
    await loader.await_all()
    await ctx.flush()
    entry = loader.resolve(inc_id)
    assert entry.fiber is not None
    assert entry.fiber.state == FiberState.ACTIVE
    inc = entry.ctx.get("include", strict=False)
    assert isinstance(inc, Include)
    assert inc.root.data[0]["config"]["level"] == 3


@pytest.mark.asyncio
async def test_include_parse_non_dict_row_fails_loud(tmp_path: Path) -> None:
    ctx = Context()
    loader = LoaderService(ctx)
    file = tmp_path / "cfg.yaml"
    file.write_text("- just-a-string\n", encoding="utf-8")
    inc_id = await loader.create({"name": "include", "config": {"path": str(file)}})
    await loader.await_all()
    await ctx.flush()
    entry = loader.resolve(inc_id)
    assert entry.fiber is not None
    assert entry.fiber.state == FiberState.FAILED
    assert isinstance(entry.fiber.error, TypeError)
    assert "entry must be an object" in str(entry.fiber.error)


@pytest.mark.asyncio
async def test_include_read_with_pending_journal_writes_merge(
    tmp_path: Path, mod: Any
) -> None:
    _ctx, loader, inc_id, _entry, inc = await _boot(tmp_path, _rows(1, 2))
    file = tmp_path / "cfg.yaml"
    # A runtime change merges with a file edit to a different entry.
    # The edit lands BEFORE the update, matching the oracle's staging:
    # with commit-before-await (B1) the drain settles inside the update
    # await, so the drain's stale check sees the edit and reconciles.
    file.write_text(_rows(1, 99), encoding="utf-8")
    await loader.update(f"{inc_id}:e0", {"config": {"level": 20}})
    await inc.refresh()
    await _settle(inc, loader)
    rows = load_yaml(file.read_text(encoding="utf-8"))
    assert rows[0]["config"]["level"] == 20
    assert rows[1]["config"]["level"] == 99
    assert inc.root.data[0]["config"]["level"] == 20
    assert inc.root.data[1]["config"]["level"] == 99


@pytest.mark.asyncio
async def test_include_write_with_missing_file_warns_and_keeps_journal(
    tmp_path: Path, mod: Any
) -> None:
    ctx, loader, inc_id, _entry, inc = await _boot(tmp_path, _rows(1, 2))
    file = tmp_path / "cfg.yaml"
    # The file goes missing BEFORE the update: with commit-before-await
    # (B1) the drain settles inside the update await, so deleting after
    # it would miss the write entirely. The drain warns and keeps the
    # journal; once the file is back, an explicit write drains it (a
    # refresh alone would no-op on identical content).
    file.unlink()
    await loader.update(f"{inc_id}:e1", {"config": {"level": 20}})
    await _settle(inc, loader)
    assert "is missing" in _logger_text(ctx)
    assert not file.exists()
    file.write_text(_rows(1, 2), encoding="utf-8")
    inc.write()
    await _settle(inc, loader)
    rows = load_yaml(file.read_text(encoding="utf-8"))
    assert rows[1]["config"]["level"] == 20


@pytest.mark.asyncio
async def test_include_restore_merges_mid_write_changes(
    tmp_path: Path, mod: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx, loader, inc_id, _entry, inc = await _boot(tmp_path, _rows(1, 2))
    file = tmp_path / "cfg.yaml"
    real_replace = os.replace
    failing = {"on": True}

    def _failing(src: Any, dst: Any) -> None:
        if failing["on"]:
            raise OSError(errno.EACCES, "permission denied")
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", _failing)
    # The first update's write retries; the second update lands in the
    # journal mid-write and must be merged back on failure, not lost.
    await loader.update(f"{inc_id}:e0", {"config": {"level": 20}})
    await loader.update(f"{inc_id}:e1", {"config": {"level": 30}})
    await _settle(inc, loader)
    assert "failed to write config file" in _logger_text(ctx)
    failing["on"] = False
    loader.remove(inc_id)
    await loader.ctx.flush()
    await _settle(inc, loader)
    await ctx.flush()
    rows = load_yaml(file.read_text(encoding="utf-8"))
    assert rows[0]["config"]["level"] == 20
    assert rows[1]["config"]["level"] == 30


@pytest.mark.asyncio
async def test_include_stale_precheck_missing_file_retries(
    tmp_path: Path, mod: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ctx, loader, inc_id, _entry, inc = await _boot(tmp_path, _rows(1, 2))
    file = tmp_path / "cfg.yaml"
    real_read = Path.read_text
    calls = {"n": 0}

    def _vanishing(self: Path, *args: Any, **kwargs: Any) -> str:
        calls["n"] += 1
        # The second read is the stale pre-check inside the write: the
        # file looks deleted from under the writer.
        if calls["n"] == 2:
            raise FileNotFoundError(str(self))
        return real_read(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", _vanishing)
    await loader.update(f"{inc_id}:e1", {"config": {"level": 20}})
    await _settle(inc, loader)
    assert calls["n"] >= 2
    rows = load_yaml(file.read_text(encoding="utf-8"))
    assert rows[1]["config"]["level"] == 20


@pytest.mark.asyncio
async def test_include_adopt_patches_error_is_logged(
    tmp_path: Path, mod: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed patch adoption is logged, not raised out of the drain."""
    ctx, loader, inc_id, _entry, inc = await _boot(
        tmp_path, _rows(1), patches=[{"id": "e0", "config": {"level": 9}}]
    )
    fiber = inc.owner_fiber
    assert fiber is not None

    async def _boom(config: Any, no_save: bool = False) -> None:
        raise RuntimeError("adopt boom")

    monkeypatch.setattr(fiber, "update", _boom)
    await loader.update(f"{inc_id}:e0", {"config": {"level": 5}})
    await _settle(inc, loader)
    assert "adopt boom" in _logger_text(ctx)


@pytest.mark.asyncio
async def test_include_unlink_failure_is_swallowed(
    tmp_path: Path, mod: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx, loader, inc_id, _entry, inc = await _boot(tmp_path, _rows(1, 2))
    file = tmp_path / "cfg.yaml"
    before = file.read_text(encoding="utf-8")

    def _non_retryable(src: Any, dst: Any) -> None:
        raise OSError(errno.EINVAL, "invalid argument")

    def _failing_unlink(self: Path, *args: Any, **kwargs: Any) -> None:
        raise OSError(errno.EACCES, "permission denied")

    monkeypatch.setattr(os, "replace", _non_retryable)
    monkeypatch.setattr(Path, "unlink", _failing_unlink)
    await loader.update(f"{inc_id}:e1", {"config": {"level": 20}})
    await _settle(inc, loader)
    assert "failed to write config file" in _logger_text(ctx)
    assert file.read_text(encoding="utf-8") == before


@pytest.mark.asyncio
async def test_include_loop_continues_when_read_fails_again(
    tmp_path: Path, mod: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx, _loader, _inc_id, _entry, inc = await _boot(tmp_path, _rows(1, 2))
    file = tmp_path / "cfg.yaml"
    attempts = {"n": 0}
    real_read = inc._read

    async def _failing_read(forced: bool = False) -> bool:
        attempts["n"] += 1
        if attempts["n"] == 1:
            # A second read is due while the first one fails: the loop
            # goes around instead of stopping.
            inc._dirty_read = True
            raise RuntimeError("read boom")
        return await real_read(forced=forced)

    monkeypatch.setattr(inc, "_read", _failing_read)
    file.write_text(_rows(9, 2), encoding="utf-8")
    await inc.refresh()
    assert attempts["n"] == 2
    assert "read boom" in _logger_text(ctx)
    assert inc.root.data[0]["config"]["level"] == 9


@pytest.mark.asyncio
async def test_include_commit_without_running_loop_defers_drain(
    tmp_path: Path, mod: Any
) -> None:
    _ctx, loader, _inc_id, _entry, inc = await _boot(tmp_path, _rows(1, 2))
    file = tmp_path / "cfg.yaml"
    row = inc.root.data[0]
    legacy = copy.deepcopy(row)
    row["config"] = {"level": 7}
    # No event loop runs in the worker thread, so the drain is
    # deferred, not dropped: the next trigger flushes it.
    await asyncio.to_thread(
        inc.commit,
        EntryChange(id="e0", group=inc.root, options=row, legacy=legacy),
    )
    assert file.read_text(encoding="utf-8") == _rows(1, 2)
    await inc.refresh()
    await _settle(inc, loader)
    rows = load_yaml(file.read_text(encoding="utf-8"))
    assert rows[0]["config"]["level"] == 7
    assert inc.root.data[0]["config"]["level"] == 7


@pytest.mark.asyncio
async def test_include_direct_construction_cannot_persist_patches(
    tmp_path: Path, mod: Any
) -> None:
    # Built directly, without a fiber, the Include has no owning entry:
    # patch-owned changes still route to its config, but there is no
    # parent row to persist them through.
    file = tmp_path / "cfg.yaml"
    file.write_text(_rows(1), encoding="utf-8")
    ctx = Context()
    loader = LoaderService(ctx)
    inc = Include(
        ctx,
        {
            "path": str(file),
            "patches": [{"id": "e0", "config": {"level": 9}}],
        },
    )
    assert inc.owner is None and inc.owner_fiber is None
    await inc.init()
    await inc.flush()
    await loader.await_all()
    assert inc.root.data[0]["config"]["level"] == 9
    await inc.update("e0", {"config": {"level": 5}})
    await inc.flush()
    await inc.ctx.flush()
    await loader.await_all()
    assert inc.root.data[0]["config"]["level"] == 5
    assert inc.config["patches"] == [{"id": "e0", "config": {"level": 5}}]
    assert "cannot be persisted" in _logger_text(ctx)
    assert file.read_text(encoding="utf-8") == _rows(1)
    await inc.stop()
