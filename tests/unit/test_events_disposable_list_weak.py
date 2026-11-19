"""Tests for DisposableList._weak cleanup on disposer invocation."""

from __future__ import annotations

from anima.events import DisposableList


def test_push_disposer_removes_weak_entry() -> None:
    d = DisposableList()
    dispose = d.push("a")
    assert "a" in d._weak
    dispose()
    assert "a" not in d._weak
    assert d._weak == {}
    assert d.length == 0


def test_unshift_disposer_removes_weak_entry() -> None:
    d = DisposableList()
    dispose = d.unshift("a")
    assert "a" in d._weak
    dispose()
    assert "a" not in d._weak
    assert d._weak == {}
    assert d.length == 0


def test_stale_disposer_does_not_remove_newer_registration() -> None:
    d = DisposableList()
    old_dispose = d.push("a")
    new_dispose = d.push("a")
    latest_sn = d._weak["a"]

    # Calling the old disposer removes only its own _map entry, not _weak.
    old_dispose()
    assert d._weak.get("a") == latest_sn
    assert list(d) == ["a"]

    # Deleting via the newer registration or delete() still works.
    assert d.delete("a") is True
    assert "a" not in d._weak
    assert d.length == 0

    # Calling new_dispose after delete() is a safe no-op.
    new_dispose()
    assert d._weak == {}


def test_stale_unshift_disposer_does_not_remove_newer_unshift() -> None:
    d = DisposableList()
    old_dispose = d.unshift("x")
    new_dispose = d.unshift("x")
    latest_sn = d._weak["x"]

    old_dispose()
    assert d._weak.get("x") == latest_sn
    assert list(d) == ["x"]

    new_dispose()
    assert "x" not in d._weak
    assert d.length == 0


def test_delete_still_works_and_clears_weak() -> None:
    d = DisposableList()
    dispose_a = d.push("a")
    d.unshift("b")

    assert d.delete("a") is True
    assert "a" not in d._weak
    assert d.delete("a") is False
    assert list(d) == ["b"]

    # Disposer for already-deleted item does not affect remaining items.
    dispose_a()
    assert "b" in d._weak
    assert d.delete("b") is True
    assert d._weak == {}
    assert d.length == 0
