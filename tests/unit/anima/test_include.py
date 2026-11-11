from pathlib import Path

import pytest

from anima.core import Context
from anima.include import (
    IncludeError,
    IncludeService,
    Journal,
    apply_patch,
    diff_entries,
)


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
