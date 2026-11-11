"""Volatile config references: stable references to immutable snapshots.

A volatile field holds live settings data that can be replaced wholesale
without restarting the owning component. The snapshot is deep-frozen at
creation; later mutation of the input cannot affect it, and the snapshot
itself rejects mutation.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Self, SupportsIndex


class _FrozenDict(dict):
    """A dict that rejects mutation (deep-frozen snapshot)."""

    def __setitem__(self, key: Any, value: Any) -> None:
        raise TypeError("volatile snapshot is immutable")

    def __delitem__(self, key: Any) -> None:
        raise TypeError("volatile snapshot is immutable")

    def clear(self) -> None:
        raise TypeError("volatile snapshot is immutable")

    def pop(self, *args: Any, **kwargs: Any) -> Any:
        raise TypeError("volatile snapshot is immutable")

    def popitem(self) -> Any:
        raise TypeError("volatile snapshot is immutable")

    def setdefault(self, *args: Any, **kwargs: Any) -> Any:
        raise TypeError("volatile snapshot is immutable")

    def update(self, *args: Any, **kwargs: Any) -> None:
        raise TypeError("volatile snapshot is immutable")


class _FrozenList(list):
    """A list that rejects mutation (deep-frozen snapshot)."""

    def __setitem__(self, key: Any, value: Any) -> None:
        raise TypeError("volatile snapshot is immutable")

    def __delitem__(self, key: Any) -> None:
        raise TypeError("volatile snapshot is immutable")

    def append(self, value: Any) -> None:
        raise TypeError("volatile snapshot is immutable")

    def extend(self, values: Any) -> None:
        raise TypeError("volatile snapshot is immutable")

    def insert(self, index: SupportsIndex, value: Any) -> None:
        raise TypeError("volatile snapshot is immutable")

    def remove(self, value: Any) -> None:
        raise TypeError("volatile snapshot is immutable")

    def pop(self, *args: Any, **kwargs: Any) -> Any:
        raise TypeError("volatile snapshot is immutable")

    def clear(self) -> None:
        raise TypeError("volatile snapshot is immutable")

    def sort(self, *args: Any, **kwargs: Any) -> None:
        raise TypeError("volatile snapshot is immutable")

    def reverse(self) -> None:
        raise TypeError("volatile snapshot is immutable")

    def __iadd__(self, values: Iterable[Any]) -> Self:  # type: ignore[misc]
        # mypy requires __iadd__ to match list.__add__'s return type, but
        # this override always raises: the snapshot can never be extended.
        raise TypeError("volatile snapshot is immutable")

    def __imul__(self, count: SupportsIndex) -> Self:
        raise TypeError("volatile snapshot is immutable")


def _is_plain_dict(value: Any) -> bool:
    """Whether the value is a plain dict (or a frozen snapshot of one)."""
    return type(value) is dict or isinstance(value, _FrozenDict)


def _snapshot(value: Any, ancestors: set[int] | None = None) -> Any:
    """Deep-freeze a value into an immutable snapshot copy.

    Rejects functions, cycles, and non-plain objects. Shared references on
    different branches are snapshotted once per branch.
    """
    if callable(value):
        raise TypeError("volatile config cannot contain functions")
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    seen = set() if ancestors is None else ancestors
    if id(value) in seen:
        raise TypeError("volatile config cannot contain cycles")
    seen.add(id(value))
    try:
        if isinstance(value, list):
            frozen = _FrozenList.__new__(_FrozenList)
            list.extend(frozen, [_snapshot(item, seen) for item in value])
            return frozen
        if isinstance(value, tuple):
            return tuple(_snapshot(item, seen) for item in value)
        if not _is_plain_dict(value):
            raise TypeError("volatile config objects must be plain objects or arrays")
        frozen_dict = _FrozenDict.__new__(_FrozenDict)
        dict.update(frozen_dict, {k: _snapshot(v, seen) for k, v in value.items()})
        return frozen_dict
    finally:
        seen.discard(id(value))


class Volatile:
    """A stable reference to an immutable config snapshot.

    Only ``get()`` is public; the owning runtime replaces the snapshot via
    :func:`update_volatile`.
    """

    __slots__ = ("_current",)

    def __init__(self, snapshot: Any) -> None:
        self._current = snapshot

    def get(self) -> Any:
        """Return the current immutable snapshot."""
        return self._current

    def _write(self, value: Any) -> None:
        """Replace the snapshot. Internal; use :func:`update_volatile`."""
        self._current = value


def create_volatile(value: Any) -> Volatile:
    """Create a detached reference holding an immutable copy of ``value``."""
    return Volatile(_snapshot(value))


def is_volatile(value: Any) -> bool:
    """Whether the value is a volatile reference."""
    return isinstance(value, Volatile)


def volatile_entries(value: Any) -> list[dict[str, Any]]:
    """Collect volatile references and their object-key paths.

    Does not descend into a volatile's snapshot or into non-plain objects.
    A root volatile yields an empty path. Paths are string-key lists.
    """
    entries: list[dict[str, Any]] = []
    ancestors: set[int] = set()

    def visit(current: Any, path: list[str]) -> None:
        if is_volatile(current):
            entries.append({"path": list(path), "ref": current})
            return
        if not current or not isinstance(current, (dict, list, tuple)):
            return
        if id(current) in ancestors:
            return
        if isinstance(current, dict) and not _is_plain_dict(current):
            return
        ancestors.add(id(current))
        try:
            if isinstance(current, dict):
                for key, child in current.items():
                    visit(child, [*path, key])
            else:
                for index, child in enumerate(current):
                    visit(child, [*path, str(index)])
        finally:
            ancestors.discard(id(current))

    visit(value, [])
    return entries


def update_volatile(target: Volatile, source: Volatile) -> None:
    """Commit an already validated immutable snapshot from another reference."""
    target._write(source.get())
