from pathlib import Path

import pytest

from anima.core import Context
from anima.include import (
    IncludeError,
    IncludeService,
    Journal,
    apply_patch,
    diff_entries,
    equal_except_volatile,
)
from anima.schema import Schema


@pytest.mark.asyncio
async def test_include_service_registration():
    ctx = Context()
    inc = IncludeService(ctx)
    assert ctx.has("include")
    assert ctx.include == inc


def test_resolve_single_and_nested_includes(tmp_path: Path):
    # Setup files:
    # main.yaml -> modules/worker.yaml -> common/db.yaml
    modules_dir = tmp_path / "modules"
    modules_dir.mkdir()
    common_dir = tmp_path / "common"
    common_dir.mkdir()

    db_path = common_dir / "db.yaml"
    db_path.write_text("name: db_service\nconfig:\n  port: 5432\n", encoding="utf-8")

    worker_path = modules_dir / "worker.yaml"
    worker_path.write_text(
        "name: worker_service\nconfig:\n  database:\n    $include: ../common/db.yaml\n    pool_size: 10\n",
        encoding="utf-8",
    )

    main_path = tmp_path / "main.yaml"
    main_path.write_text(
        "components:\n  - $include: ./modules/worker.yaml\n  - name: timer\n",
        encoding="utf-8",
    )

    ctx = Context()
    inc = IncludeService(ctx)

    resolved = inc.resolve(main_path)
    assert resolved == {
        "components": [
            {
                "name": "worker_service",
                "config": {
                    "database": {
                        "name": "db_service",
                        "config": {"port": 5432},
                        "pool_size": 10,
                    }
                },
            },
            {"name": "timer"},
        ]
    }


def test_circular_include_detection(tmp_path: Path):
    file_a = tmp_path / "a.yaml"
    file_b = tmp_path / "b.yaml"

    file_a.write_text("$include: ./b.yaml\n", encoding="utf-8")
    file_b.write_text("$include: ./a.yaml\n", encoding="utf-8")

    ctx = Context()
    inc = IncludeService(ctx)

    with pytest.raises(IncludeError) as exc_info:
        inc.resolve(file_a)
    assert "circular include detected" in str(exc_info.value).lower()


def test_diff_entries_and_journal():
    entry_v1 = {"id": "1", "name": "service_a", "config": {"port": 80}}
    entry_v2 = {"id": "1", "name": "service_a", "config": {"port": 443, "ssl": True}}

    changes = diff_entries(entry_v1, entry_v2)
    assert changes == {"config": {"port": 443, "ssl": True}}

    # Journal tracking
    journal = Journal()
    journal.record_upsert("1", {"config": {"port": 8080}})
    journal.record_upsert("1", {"enabled": True})  # Coalesces with created=False
    journal.record_upsert(
        "1", {"updated": True}, created=True
    )  # Coalesces with created=True
    journal.record_upsert("2", {"name": "service_b"}, created=True)
    journal.record_remove("3")
    journal.record_upsert("4", {"name": "not_created"})  # upsert with created=False

    assert "1" in journal.records
    assert journal.records["1"].changes == {
        "config": {"port": 8080},
        "enabled": True,
        "updated": True,
    }
    assert journal.records["1"].created is True
    assert journal.records["2"].created is True
    assert journal.records["3"].kind == "remove"
    assert journal.records["4"].created is False

    # Apply journal to entries
    entries = [
        {"id": "1", "name": "service_a", "config": {"port": 80}},
        {"id": "3", "name": "service_c"},
        {"id": "5", "name": "service_unchanged"},
        {"name": "entry_without_id"},
    ]
    applied = journal.apply(entries)
    # Entry 1 updated, Entry 3 removed, Entry 5 unchanged, Entry without id kept, Entry 2 inserted
    assert len(applied) == 4
    assert applied[0]["id"] == "1"
    assert applied[0]["config"] == {"port": 8080}
    assert applied[0]["enabled"] is True
    assert applied[1]["id"] == "5"
    assert applied[2]["name"] == "entry_without_id"
    assert applied[3]["id"] == "2"
    assert applied[3]["name"] == "service_b"


def test_apply_patch():
    base_entries = [
        {"id": "component-1", "name": "logger", "config": {"level": "info"}},
        {"id": "component-2", "name": "watcher", "disabled": True},
        {"name": "entry_without_id"},
    ]

    patches = [
        {"id": "component-1", "config": {"level": "debug"}},
        {"id": "component-2", "disabled": False},
        {"insert": [{"id": "component-3", "name": "timer"}, "invalid_non_dict"]},
        {"id": "component-nonexistent", "name": "ignored"},
        {"other_key": "ignored_patch"},
    ]

    patched = apply_patch(base_entries, patches)
    assert len(patched) == 3
    assert patched[0]["config"]["level"] == "debug"
    assert patched[1]["disabled"] is False
    assert patched[2]["name"] == "timer"


def test_resolve_json_and_scalar_includes(tmp_path: Path):
    json_path = tmp_path / "data.json"
    json_path.write_text('["item1", "item2"]', encoding="utf-8")

    main_yaml = tmp_path / "main.yaml"
    main_yaml.write_text(
        "items:\n  $include: ./data.json\ncount: 42\nenabled: true\n",
        encoding="utf-8",
    )

    ctx = Context()
    inc = IncludeService(ctx)

    # Resolve using str path directly at top level
    resolved = inc.resolve(str(main_yaml))
    assert resolved == {
        "items": ["item1", "item2"],
        "count": 42,
        "enabled": True,
    }


def test_resolve_missing_file(tmp_path: Path):
    ctx = Context()
    inc = IncludeService(ctx)
    with pytest.raises(IncludeError) as exc_info:
        inc.resolve(tmp_path / "nonexistent.yaml")
    assert "does not exist" in str(exc_info.value)


# --- P1-5: patches and include config must stay literal ---


def test_apply_patch_rejects_js_expr() -> None:
    entries = [{"id": "a", "name": "comp"}]
    with pytest.raises(IncludeError, match="include patches must be literal mappings"):
        apply_patch(entries, [{"__jsExpr": "process.env.X"}])
    with pytest.raises(IncludeError, match="include patches must be literal mappings"):
        apply_patch(entries, ["not-a-mapping"])
    with pytest.raises(
        IncludeError, match="include patches must be a literal patch list"
    ):
        apply_patch(entries, {"__jsExpr": "process.env.X"})  # type: ignore[arg-type]
    with pytest.raises(IncludeError, match="expected a literal entry list"):
        apply_patch({"__jsExpr": "process.env.X"}, [])  # type: ignore[arg-type]


def test_apply_patch_rejects_js_expr_insert() -> None:
    entries = [{"id": "a", "name": "comp"}]
    with pytest.raises(IncludeError, match="expected a literal entry list"):
        apply_patch(entries, [{"insert": {"__jsExpr": "process.env.X"}}])
    with pytest.raises(IncludeError, match="expected a literal entry list"):
        apply_patch(entries, [{"insert": "not-a-list"}])
    with pytest.raises(IncludeError, match="expected a literal entry list"):
        apply_patch(entries, [{"insert": [{"__jsExpr": "process.env.X"}]}])
    # A literal insert still applies; a JsExpr patch value is data, not
    # an instruction, so it is applied literally and never evaluated.
    patched = apply_patch(entries, [{"insert": [{"id": "b", "name": "other"}]}])
    assert [e["id"] for e in patched] == ["a", "b"]
    patched = apply_patch(
        entries,
        [{"id": "a", "config": {"mode": {"__jsExpr": "process.env.X"}}}],
    )
    assert patched[0]["config"] == {"mode": {"__jsExpr": "process.env.X"}}


def test_resolve_include_path_must_be_literal(tmp_path: Path) -> None:
    ctx = Context()
    inc = IncludeService(ctx)
    with pytest.raises(IncludeError, match="include path must be literal"):
        inc.resolve({"$include": {"__jsExpr": "process.env.X"}})
    with pytest.raises(IncludeError, match="include path must be literal"):
        inc.resolve({"$include": 42})
    # A literal string path still resolves.
    target = tmp_path / "data.yaml"
    target.write_text("name: db\n", encoding="utf-8")
    assert inc.resolve({"$include": str(target)}) == {"name": "db"}


def test_resolve_file_top_level_js_expr_rejected(tmp_path: Path) -> None:
    ctx = Context()
    inc = IncludeService(ctx)
    expr_file = tmp_path / "expr.yaml"
    expr_file.write_text("!!js process.env.X\n", encoding="utf-8")
    with pytest.raises(IncludeError, match="expected a literal entry list"):
        inc.resolve(expr_file)


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
# P1-6 — diff_entries is schema-aware; deletion round-trips through Journal
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
    journal = Journal()
    journal.record_upsert("a", changes)
    # None deletes the key; it is never stored as a value.
    assert journal.apply([older]) == [{"id": "a", "name": "x"}]
    # A newly created entry skips None-valued changes entirely.
    journal2 = Journal()
    journal2.record_upsert("b", {"name": "y", "config": None}, created=True)
    assert journal2.apply([]) == [{"id": "b", "name": "y"}]


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
