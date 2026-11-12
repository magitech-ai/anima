"""Shared small utilities.

Nullable and plain-object checks, dict transforms, case conversion,
time helpers, and binary-source helpers used across the kernel.

Each function documents where Python semantics differ from the
JavaScript originals it mirrors.
"""

from __future__ import annotations

import math
import re
from collections.abc import Coroutine
from datetime import UTC, datetime, timedelta
from typing import Any, TypeAlias, TypeGuard, TypeVar

__all__ = [
    "Binary",
    "Dict",
    "Promisify",
    "Time",
    "filter_keys",
    "hyphenate",
    "is_non_nullable",
    "is_nullable",
    "is_plain_object",
    "map_values",
    "pick",
    "value_map",
]

T = TypeVar("T")

# A plain `dict` is the Python spelling of a string-keyed dictionary.
# Old-style alias on purpose: `type Dict = dict` cannot be subscripted,
# and callers subscript this alias (Dict[str, int]).
Dict: TypeAlias = dict  # noqa: UP040

# A promise of the awaited value: always a coroutine in Python.
type Promisify[T] = Coroutine[Any, Any, T]


def is_nullable(value: Any) -> TypeGuard[None]:
    """Return True only when `value` is None.

    Python has no ``undefined``, so None is the only nullable value.
    Falsy values such as ``0``, ``""``, and ``False`` are not nullable.
    """
    return value is None


def is_non_nullable(value: Any) -> bool:
    """Return True when `value` is not None."""
    return not is_nullable(value)


def is_plain_object(value: Any) -> TypeGuard[dict[Any, Any]]:
    """Return True for plain-object config data.

    Python has no JavaScript-style "any non-array object": class
    instances and dates are not plain objects here. Only dicts count.
    """
    return isinstance(value, dict)


def map_values(mapping: dict[str, Any], transform: Any) -> dict[str, Any]:
    """Return a new dict with ``transform(value, key)`` applied to each entry."""
    return {key: transform(value, key) for key, value in mapping.items()}


def value_map(mapping: dict[str, Any], transform: Any) -> dict[str, Any]:
    """Alias for ``map_values``."""
    return map_values(mapping, transform)


def filter_keys(mapping: dict[str, Any], predicate: Any) -> dict[str, Any]:
    """Return a new dict with only the entries where ``predicate(key, value)`` holds."""
    return {key: value for key, value in mapping.items() if predicate(key, value)}


def pick(
    source: dict[str, Any],
    keys: Any = None,
    forced: bool = False,
) -> dict[str, Any]:
    """Pick selected keys from ``source``.

    With no ``keys``, returns a shallow copy. Without ``forced``, keys
    whose value is None are skipped. With ``forced``, None values are
    kept, and missing keys are included with value None (Python has no
    ``undefined`` to stand in for a missing value).
    """
    if keys is None:
        return dict(source)
    result: dict[str, Any] = {}
    for key in keys:
        if key in source:
            if forced or source[key] is not None:
                result[key] = source[key]
        elif forced:
            result[key] = None
    return result


_DELIM = 0
_UPPER = 1
_LOWER = 2


def _tokenize(source: str, delimiters: tuple[int, ...], delimiter: int) -> str:
    """Split ``source`` into words, joining them with ``delimiter``."""
    output: list[int] = []
    state = _DELIM
    for i, char in enumerate(source):
        code = ord(char)
        if 65 <= code <= 90:  # A-Z
            if state == _UPPER:
                nxt = ord(source[i + 1]) if i + 1 < len(source) else -1
                if 97 <= nxt <= 122:
                    output.append(delimiter)
                output.append(code + 32)
            else:
                if state != _DELIM:
                    output.append(delimiter)
                output.append(code + 32)
            state = _UPPER
        elif 97 <= code <= 122:  # a-z
            output.append(code)
            state = _LOWER
        elif code in delimiters:
            if state != _DELIM:
                output.append(delimiter)
            state = _DELIM
        else:
            output.append(code)
    return "".join(chr(c) for c in output)


def hyphenate(source: str) -> str:
    """Convert text to dash-delimited parameter case.

    ``"fooBar"`` becomes ``"foo-bar"``; acronym boundaries split before
    the last capital (``"FOOBar"`` becomes ``"foo-bar"``); repeated
    delimiters collapse to one.
    """
    return _tokenize(source, (45, 95), 45)


def _js_round(value: float) -> int:
    """Round like JavaScript ``Math.round``: halves round toward +infinity."""
    return math.floor(value + 0.5)


def _local_timezone_offset() -> int:
    """Local UTC offset in minutes, positive west of Greenwich."""
    offset = datetime.now().astimezone().utcoffset()
    if offset is None:
        # astimezone() always yields an aware datetime, so
        # utcoffset() is never None in practice.
        return 0  # pragma: no cover
    return int(-offset.total_seconds() // 60)


_TIME_NUMERIC = r"\d+(?:\.\d+)?"
_TIME_REGEXP = re.compile(
    "^"
    + "".join(
        f"({_TIME_NUMERIC}{unit})?"
        for unit in (
            r"w(?:eek(?:s)?)?",
            r"d(?:ay(?:s)?)?",
            r"h(?:our(?:s)?)?",
            r"m(?:in(?:ute)?(?:s)?)?",
            r"s(?:ec(?:ond)?(?:s)?)?",
        )
    )
    + "$"
)


class Time:
    """Time constants plus parsing and formatting helpers."""

    millisecond = 1
    second = 1000
    minute = second * 60
    hour = minute * 60
    day = hour * 24
    week = day * 7

    _timezone_offset = _local_timezone_offset()

    @classmethod
    def set_timezone_offset(cls, offset: int) -> None:
        """Set the process-wide timezone offset in minutes."""
        cls._timezone_offset = offset

    @classmethod
    def get_timezone_offset(cls) -> int:
        """Return the process-wide timezone offset in minutes."""
        return cls._timezone_offset

    @classmethod
    def get_date_number(
        cls,
        date: datetime | float | None = None,
        offset: int | None = None,
    ) -> int:
        """Return the day number of ``date`` (days since the epoch).

        ``date`` is a datetime (naive datetimes are read as UTC), a
        millisecond epoch value, or None for now.
        """
        if date is None:
            moment = datetime.now(UTC)
        elif isinstance(date, (int, float)):
            moment = datetime.fromtimestamp(date / 1000, tz=UTC)
        elif date.tzinfo is None:
            moment = date.replace(tzinfo=UTC)
        else:
            moment = date
        if offset is None:
            offset = cls._timezone_offset
        ms = moment.timestamp() * 1000
        return math.floor((ms / cls.minute - offset) / 1440)

    @classmethod
    def from_date_number(cls, value: int, offset: int | None = None) -> datetime:
        """Return the datetime at the start of day number ``value``."""
        if offset is None:
            offset = cls._timezone_offset
        ms = value * cls.day + offset * cls.minute
        return datetime.fromtimestamp(ms / 1000, tz=UTC)

    @classmethod
    def parse_time(cls, source: str) -> float:
        """Parse a duration such as ``"1w2d3h4m5s"`` into milliseconds.

        Units must appear in week/day/hour/minute/second order; an
        unparseable string parses as 0.
        """
        match = _TIME_REGEXP.match(source)
        if match is None:
            return 0.0
        scales = (cls.week, cls.day, cls.hour, cls.minute, cls.second)
        total = 0.0
        for group, scale in zip(match.groups(), scales):
            if group is None:
                continue
            number = re.match(r"\d+(?:\.\d+)?", group)
            # The outer regex only matches groups starting with a
            # number, so this always matches.
            if number is not None:  # pragma: no cover
                total += float(number.group()) * scale
        return total

    @classmethod
    def parse_date(cls, date: str) -> datetime:
        """Parse a date string into a datetime.

        A duration parses as now plus that duration; ``"HH:MM"`` (or
        ``"HH:MM:SS"``) parses as today at that time; an ISO date parses
        directly. The remaining branch prepends the current year to a
        ``"M-D-Y"``-shaped string, which never yields a valid date;
        that raises ``ValueError`` (fail loud — Python has no
        invalid-date value). An empty string returns now.
        """
        parsed = cls.parse_time(date)
        if parsed:
            return datetime.now(UTC) + timedelta(milliseconds=parsed)
        if re.fullmatch(r"\d{1,2}(:\d{1,2}){1,2}", date):
            parts = [int(p) for p in date.split(":")]
            today = datetime.now(UTC).date()
            return datetime(
                today.year,
                today.month,
                today.day,
                parts[0],
                parts[1],
                parts[2] if len(parts) > 2 else 0,
                tzinfo=UTC,
            )
        if re.fullmatch(r"\d{1,2}-\d{1,2}-\d{1,2}(:\d{1,2}){1,2}", date):
            # Prepending the current year never yields a valid ISO
            # date (the oracle yields Invalid Date here); fail loud —
            # Python has no invalid-date value. Note: fromisoformat
            # must not be used here, it accepts "-" as a date/time
            # separator and would silently misparse the result.
            raise ValueError(f'invalid date "{date}"')
        if not date:
            return datetime.now(UTC)
        try:
            parsed_date = datetime.fromisoformat(date)
        except ValueError:
            raise ValueError(f'invalid date "{date}"') from None
        if parsed_date.tzinfo is None:
            parsed_date = parsed_date.replace(tzinfo=UTC)
        return parsed_date

    @classmethod
    def format(cls, ms: float) -> str:
        """Format a millisecond duration as ``"2d"``, ``"3h"``, ``"90s"``.

        Rounds halves toward +infinity, as JavaScript ``Math.round``
        does (Python's ``round`` rounds halves to even).
        """
        magnitude = abs(ms)
        if magnitude >= cls.day - cls.hour / 2:
            return f"{_js_round(ms / cls.day)}d"
        if magnitude >= cls.hour - cls.minute / 2:
            return f"{_js_round(ms / cls.hour)}h"
        if magnitude >= cls.minute - cls.second / 2:
            return f"{_js_round(ms / cls.minute)}m"
        if magnitude >= cls.second:
            return f"{_js_round(ms / cls.second)}s"
        return f"{ms}ms"

    @staticmethod
    def to_digits(source: int, length: int = 2) -> str:
        """Zero-pad ``source`` to ``length`` digits (longer values pass through)."""
        return str(source).rjust(length, "0")

    @classmethod
    def template(cls, template: str, time: datetime | None = None) -> str:
        """Fill ``yyyy``/``yy``/``MM``/``dd``/``hh``/``mm``/``ss``/``SSS`` slots.

        Each placeholder is replaced once, in that order. Note the
        order matters: a later ``yy`` replacement sees what an earlier
        ``yyyy`` replacement left behind (``"yyyy-yyyy"`` becomes
        ``"2026-26yy"``). ``ss`` (lowercase) and ``SSS`` (uppercase)
        are distinct placeholders and never collide.
        Naive datetimes are read as local wall time.
        """
        moment = time if time is not None else datetime.now()
        return (
            template.replace("yyyy", str(moment.year), 1)
            .replace("yy", str(moment.year)[2:], 1)
            .replace("MM", cls.to_digits(moment.month), 1)
            .replace("dd", cls.to_digits(moment.day), 1)
            .replace("hh", cls.to_digits(moment.hour), 1)
            .replace("mm", cls.to_digits(moment.minute), 1)
            .replace("ss", cls.to_digits(moment.second), 1)
            .replace("SSS", cls.to_digits(moment.microsecond // 1000, 3), 1)
        )


class Binary:
    """Binary source detection and conversion helpers."""

    @staticmethod
    def is_source(value: Any) -> bool:
        """Return True for bytes-like values (bytes, bytearray, memoryview)."""
        return isinstance(value, (bytes, bytearray, memoryview))

    @staticmethod
    def from_source(source: Any) -> Any:
        """Return the plain bytes behind a binary source.

        Views are copied down to the viewed range; ``bytes`` pass
        through unchanged; anything else is returned as-is.
        """
        if isinstance(source, memoryview):
            return source.tobytes()
        if isinstance(source, bytearray):
            return bytes(source)
        return source
