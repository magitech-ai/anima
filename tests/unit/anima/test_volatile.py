"""Tests for the Volatile runtime (volatile references + snapshots)."""

from collections import defaultdict

import pytest

from anima.volatile import (
    Volatile,
    create_volatile,
    is_volatile,
    update_volatile,
    volatile_entries,
)


def test_create_volatile_snapshot_is_frozen_and_detached():
    source = {"theme": "dark", "tags": ["a", "b"], "nested": {"n": 1}}
    ref = create_volatile(source)
    assert is_volatile(ref)
    snap = ref.get()
    assert snap == source
    # Frozen at every depth.
    with pytest.raises(TypeError):
        snap["theme"] = "light"
    with pytest.raises(TypeError):
        snap["tags"].append("c")
    with pytest.raises(TypeError):
        snap["nested"]["n"] = 2
    # Detached: later mutation of the input cannot affect the snapshot.
    source["theme"] = "light"
    source["tags"].append("c")
    assert ref.get()["theme"] == "dark"
    assert ref.get()["tags"] == ["a", "b"]


def test_create_volatile_rejects_functions_cycles_and_class_instances():
    with pytest.raises(TypeError, match="cannot contain functions"):
        create_volatile({"fn": len})

    cyclic: dict = {}
    cyclic["self"] = cyclic
    with pytest.raises(TypeError, match="cannot contain cycles"):
        create_volatile(cyclic)

    class Widget:
        pass

    with pytest.raises(TypeError, match="must be plain objects or arrays"):
        create_volatile({"widget": Widget()})


def test_create_volatile_allows_shared_dag_references():
    shared = {"x": 1}
    ref = create_volatile({"a": shared, "b": shared})
    assert ref.get()["a"] == {"x": 1}
    assert ref.get()["b"] == {"x": 1}
    # Snapshotted once per branch (separate frozen copies).
    assert ref.get()["a"] is not ref.get()["b"]


def test_volatile_entries_and_update():
    inner = create_volatile({"level": "debug"})
    outer = create_volatile({"other": 1})
    config = {"log": inner, "plain": {"n": 1}, "root": outer}
    entries = volatile_entries(config)
    assert {tuple(e["path"]) for e in entries} == {("log",), ("root",)}
    assert all(e["ref"] is inner or e["ref"] is outer for e in entries)

    # Root volatile yields an empty path.
    assert volatile_entries(inner) == [{"path": [], "ref": inner}]

    # updateVolatile replaces wholesale (no merge).
    replacement = create_volatile({"level": "info", "extra": True})
    update_volatile(inner, replacement)
    assert inner.get() == {"level": "info", "extra": True}
    # The source snapshot is shared, not copied.
    assert inner.get() is replacement.get()


def test_volatile_entries_skips_non_plain_objects():
    class Holder:
        def __init__(self):
            self.ref = create_volatile(1)

    # A class instance is not descended into.
    assert volatile_entries({"holder": Holder()}) == []
    # Plain values hold no references.
    assert volatile_entries({"a": 1, "b": [1, 2]}) == []
    assert volatile_entries(None) == []


def test_is_volatile_only_matches_references():
    assert is_volatile(create_volatile(1))
    assert not is_volatile({"get": lambda: 1})
    assert not is_volatile(None)
    assert isinstance(create_volatile(1), Volatile)


def test_frozen_dict_rejects_every_mutation():
    snap = create_volatile({"a": 1}).get()
    with pytest.raises(TypeError, match="volatile snapshot is immutable"):
        snap["b"] = 2
    with pytest.raises(TypeError, match="volatile snapshot is immutable"):
        del snap["a"]
    with pytest.raises(TypeError, match="volatile snapshot is immutable"):
        snap.clear()
    with pytest.raises(TypeError, match="volatile snapshot is immutable"):
        snap.pop("a")
    with pytest.raises(TypeError, match="volatile snapshot is immutable"):
        snap.popitem()
    with pytest.raises(TypeError, match="volatile snapshot is immutable"):
        snap.setdefault("b", 2)
    with pytest.raises(TypeError, match="volatile snapshot is immutable"):
        snap.update({"b": 2})


def test_frozen_list_rejects_every_mutation():
    snap = create_volatile([1, 2]).get()
    with pytest.raises(TypeError, match="volatile snapshot is immutable"):
        snap[0] = 9
    with pytest.raises(TypeError, match="volatile snapshot is immutable"):
        del snap[0]
    with pytest.raises(TypeError, match="volatile snapshot is immutable"):
        snap.append(3)
    with pytest.raises(TypeError, match="volatile snapshot is immutable"):
        snap.extend([3])
    with pytest.raises(TypeError, match="volatile snapshot is immutable"):
        snap.insert(0, 3)
    with pytest.raises(TypeError, match="volatile snapshot is immutable"):
        snap.remove(1)
    with pytest.raises(TypeError, match="volatile snapshot is immutable"):
        snap.pop()
    with pytest.raises(TypeError, match="volatile snapshot is immutable"):
        snap.clear()
    with pytest.raises(TypeError, match="volatile snapshot is immutable"):
        snap.sort()
    with pytest.raises(TypeError, match="volatile snapshot is immutable"):
        snap.reverse()
    with pytest.raises(TypeError, match="volatile snapshot is immutable"):
        snap += [3]
    with pytest.raises(TypeError, match="volatile snapshot is immutable"):
        snap *= 2


def test_snapshot_freezes_tuples():
    snap = create_volatile((1, {"a": 2})).get()
    assert snap == (1, {"a": 2})
    with pytest.raises(TypeError, match="volatile snapshot is immutable"):
        snap[1]["a"] = 3


def test_volatile_entries_skips_cycles_and_non_plain_objects():
    cyclic: dict = {}
    cyclic["self"] = cyclic
    assert volatile_entries(cyclic) == []
    mapping = defaultdict(dict, {"a": create_volatile(1)})
    assert volatile_entries(mapping) == []
