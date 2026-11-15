"""Tests for DisposableList (events.py) - push / unshift / delete / clear order."""

from __future__ import annotations

import pytest

from anima.events import DisposableList


def test_empty() -> None:
    d = DisposableList()
    assert d.length == 0
    assert len(d) == 0
    assert list(d) == []
    assert d.clear() == []


def test_push_appends_in_order() -> None:
    d = DisposableList()
    for v in "abc":
        d.push(v)
    assert list(d) == ["a", "b", "c"]
    assert d.length == 3


def test_unshift_prepends_in_order() -> None:
    d = DisposableList()
    d.push("b")
    d.unshift("a")
    d.push("c")
    d.unshift("z")
    assert list(d) == ["z", "a", "b", "c"]
    assert d.length == 4


def test_unshift_returns_working_disposer() -> None:
    d = DisposableList()
    dispose = d.unshift("a")
    d.push("b")
    dispose()
    dispose()  # idempotent
    assert list(d) == ["b"]
    assert d.length == 1


def test_push_disposer_removes_value() -> None:
    d = DisposableList()
    dispose = d.push("a")
    d.push("b")
    dispose()
    assert list(d) == ["b"]
    assert d.delete("a") is False


def test_delete_by_value() -> None:
    d = DisposableList()
    for v in "abc":
        d.push(v)
    assert d.delete("b") is True
    assert d.delete("b") is False
    assert d.delete("missing") is False
    assert list(d) == ["a", "c"]
    # re-adding a deleted value works
    d.push("b")
    assert list(d) == ["a", "c", "b"]


def test_clear_returns_front_first_order_and_empties() -> None:
    d = DisposableList()
    d.push(1)
    d.push(2)
    d.unshift(0)
    d.push(3)
    assert d.clear() == [0, 1, 2, 3]
    assert d.length == 0
    assert list(d) == []
    assert d.clear() == []


def test_clear_invalidates_prior_disposers() -> None:
    d = DisposableList()
    d.push("a")
    dispose = d.push("b")
    d.clear()
    dispose()
    d.push("c")
    assert list(d) == ["c"]


def test_iteration_after_mutation_uses_new_order() -> None:
    d = DisposableList()
    d.push("a")
    d.unshift("z")
    assert list(d) == ["z", "a"]


def test_mutation_during_iteration_raises() -> None:
    """Same contract as dict: structural mutation while iterating is an error."""
    d = DisposableList()
    d.push("a")
    it = iter(d)
    d.push("b")
    with pytest.raises(RuntimeError):
        next(it)
