"""Shared small utilities (P1-9).

Every test pins the semantic note on its function, especially where
Python semantics differ from the JavaScript originals.
"""

from collections.abc import Coroutine
from datetime import UTC, datetime, timedelta
from typing import Any, TypeVar, get_args, get_origin

import pytest

from anima.utils import (
    Binary,
    Dict,
    Promisify,
    Time,
    filter_keys,
    hyphenate,
    is_non_nullable,
    is_nullable,
    is_plain_object,
    map_values,
    pick,
    value_map,
)


class TestDictAlias:
    def test_dict_is_dict(self):
        """Dict is the plain dict type, subscriptable as a mapping."""
        assert Dict is dict
        assert Dict[str, int] == dict[str, int]


class TestPromisifyAlias:
    def test_promisify_is_awaited_coroutine(self):
        """Promisify[T] is a coroutine of the awaited value type."""
        assert Promisify[int] != Promisify[str]
        value = Promisify.__value__
        assert get_origin(value) is Coroutine
        args = get_args(value)
        assert args[0] is Any and args[1] is Any
        assert isinstance(args[2], TypeVar)


class TestIsNullable:
    def test_none_is_nullable(self):
        assert is_nullable(None) is True

    @pytest.mark.parametrize("value", [0, "", False, [], {}, 0.0])
    def test_falsy_values_are_not_nullable(self, value: Any):
        """Python has no undefined: falsy is not nullable."""
        assert is_nullable(value) is False
        assert is_non_nullable(value) is True

    def test_non_nullable_none(self):
        assert is_non_nullable(None) is False


class TestIsPlainObject:
    def test_dict_is_plain(self):
        assert is_plain_object({}) is True
        assert is_plain_object({"a": 1}) is True

    @pytest.mark.parametrize("value", [[], (), None, 1, "x", True])
    def test_non_dicts_are_not_plain(self, value: Any):
        assert is_plain_object(value) is False

    def test_class_instance_is_not_plain(self):
        """Divergence: JavaScript counts class instances and dates as
        plain objects; in Python only dicts are plain objects."""

        class Point:
            pass

        assert is_plain_object(Point()) is False
        assert is_plain_object(datetime.now()) is False


class TestMapValues:
    def test_maps_values_with_key(self):
        assert map_values({"a": 1, "b": 2}, lambda v, k: f"{k}={v}") == {
            "a": "a=1",
            "b": "b=2",
        }

    def test_returns_new_dict(self):
        source = {"a": 1}
        result = map_values(source, lambda v, k: v + 1)
        assert result == {"a": 2}
        assert source == {"a": 1}

    def test_empty(self):
        assert map_values({}, lambda v, k: v) == {}


class TestValueMapAlias:
    def test_value_map_is_map_values(self):
        assert value_map({"a": 1}, lambda v, k: v * 3) == {"a": 3}


class TestFilterKeys:
    def test_filters_by_key_and_value(self):
        source = {"a": 1, "b": 2, "c": 3}
        assert filter_keys(source, lambda k, v: v > 1) == {"b": 2, "c": 3}
        assert filter_keys(source, lambda k, v: k != "b") == {"a": 1, "c": 3}

    def test_empty(self):
        assert filter_keys({}, lambda k, v: True) == {}


class TestPick:
    def test_no_keys_returns_shallow_copy(self):
        source = {"a": 1, "b": 2}
        result = pick(source)
        assert result == source
        assert result is not source

    def test_picks_subset(self):
        assert pick({"a": 1, "b": 2, "c": 3}, ["a", "c"]) == {"a": 1, "c": 3}

    def test_skips_none_without_forced(self):
        assert pick({"a": None, "b": 2}, ["a", "b"]) == {"b": 2}

    def test_forced_keeps_none(self):
        assert pick({"a": None, "b": 2}, ["a", "b"], forced=True) == {
            "a": None,
            "b": 2,
        }

    def test_missing_key_skipped_without_forced(self):
        assert pick({"a": 1}, ["a", "missing"]) == {"a": 1}

    def test_forced_includes_missing_key_as_none(self):
        """Python has no undefined: a forced missing key maps to None."""
        assert pick({"a": 1}, ["a", "missing"], forced=True) == {
            "a": 1,
            "missing": None,
        }


class TestHyphenate:
    @pytest.mark.parametrize(
        ("source", "expected"),
        [
            ("fooBar", "foo-bar"),
            ("FooBar", "foo-bar"),
            ("FOOBar", "foo-bar"),
            ("fooBarBaz", "foo-bar-baz"),
            ("foo_bar", "foo-bar"),
            ("foo-bar", "foo-bar"),
            ("fooBar_bar", "foo-bar-bar"),
            ("", ""),
            ("already-hyphenated", "already-hyphenated"),
            ("ABC", "abc"),
            ("a--b", "a-b"),
            ("foo123Bar", "foo123-bar"),
        ],
    )
    def test_hyphenate(self, source: str, expected: str):
        assert hyphenate(source) == expected


class TestTimeConstants:
    def test_constants(self):
        assert Time.millisecond == 1
        assert Time.second == 1000
        assert Time.minute == 60_000
        assert Time.hour == 3_600_000
        assert Time.day == 86_400_000
        assert Time.week == 604_800_000


class TestTimeParseTime:
    @pytest.mark.parametrize(
        ("source", "expected"),
        [
            ("1w", 604_800_000),
            ("2d", 172_800_000),
            ("12h", 43_200_000),
            ("90s", 90_000),
            ("1m30s", 90_000),
            ("1.5h", 5_400_000),
            ("1w2d3h4m5s", 788_645_000),
            ("2weeks", 1_209_600_000),
            ("1day", 86_400_000),
            ("1min", 60_000),
            ("1sec", 1_000),
            ("", 0),
            ("nonsense", 0),
            ("1x", 0),
            ("1s1m", 0),
        ],
    )
    def test_parse_time(self, source: str, expected: float):
        assert Time.parse_time(source) == expected

    def test_parse_time_trailing_newline_is_not_end(self):
        """``$`` would match before a trailing newline in Python; the
        oracle's pattern matches only at end of input."""
        assert Time.parse_time("1h\n") == 0.0


class TestTimeFormat:
    @pytest.mark.parametrize(
        ("ms", "expected"),
        [
            (0, "0ms"),
            (500, "500ms"),
            (500.0, "500ms"),
            (0.0, "0ms"),
            (12.5, "12.5ms"),
            (999, "999ms"),
            (1_000, "1s"),
            (1_500, "2s"),
            (90_000, "2m"),
            (3_600_000, "1h"),
            (86_400_000, "1d"),
            (172_800_000, "2d"),
            (-1_500, "-1s"),
        ],
    )
    def test_format(self, ms: float, expected: str):
        assert Time.format(ms) == expected

    def test_format_rounds_halves_toward_positive_infinity(self):
        """JavaScript Math.round(-1.5) is -1; Python round(-1.5) is -2."""
        assert Time.format(-90_000) == "-1m"


class TestTimeToDigits:
    def test_to_digits(self):
        assert Time.to_digits(5) == "05"
        assert Time.to_digits(42) == "42"
        assert Time.to_digits(5, 3) == "005"
        assert Time.to_digits(123) == "123"


class TestTimeTemplate:
    def test_template_full(self):
        moment = datetime(2026, 11, 12, 23, 59, 58, 42000)
        assert (
            Time.template("yyyy-MM-dd hh:mm:ss.SSS", moment)
            == "2026-11-12 23:59:58.042"
        )

    def test_template_two_digit_year(self):
        moment = datetime(2026, 1, 2, 3, 4, 5)
        assert Time.template("yy", moment) == "26"

    def test_template_replaces_each_placeholder_once(self):
        """JavaScript String.replace with a string pattern replaces once,
        and the yyyy-before-yy order is visible: the leftover yyyy
        loses its first two characters to the yy pass."""
        moment = datetime(2026, 1, 2, 3, 4, 5)
        assert Time.template("yyyy-yyyy", moment) == "2026-26yy"

    def test_template_milliseconds(self):
        """SSS is uppercase and distinct from the lowercase ss slot."""
        moment = datetime(2026, 1, 2, 3, 4, 5, 42000)
        assert Time.template("SSS", moment) == "042"


class TestTimeTimezoneOffset:
    def test_offset_round_trip(self):
        original = Time.get_timezone_offset()
        try:
            Time.set_timezone_offset(-300)
            assert Time.get_timezone_offset() == -300
        finally:
            Time.set_timezone_offset(original)

    def test_date_number_round_trip(self):
        moment = datetime(2026, 11, 12, 15, 30, tzinfo=UTC)
        number = Time.get_date_number(moment, offset=0)
        assert Time.from_date_number(number, offset=0) == datetime(
            2026, 11, 12, tzinfo=UTC
        )

    def test_get_date_number_epoch_ms(self):
        assert Time.get_date_number(0, offset=0) == 0
        assert Time.get_date_number(86_400_000, offset=0) == 1

    def test_get_date_number_none_means_now(self):
        before = Time.get_date_number(datetime.now(UTC))
        result = Time.get_date_number()
        after = Time.get_date_number(datetime.now(UTC))
        assert before <= result <= after

    def test_get_date_number_naive_read_as_utc(self):
        naive = datetime(2026, 11, 12, 15, 30)
        aware = datetime(2026, 11, 12, 15, 30, tzinfo=UTC)
        assert Time.get_date_number(naive, offset=0) == Time.get_date_number(
            aware, offset=0
        )

    def test_get_date_number_default_offset(self):
        moment = datetime(2026, 11, 12, 15, 30, tzinfo=UTC)
        assert Time.get_date_number(moment) == Time.get_date_number(
            moment, offset=Time.get_timezone_offset()
        )

    def test_from_date_number_default_offset(self):
        number = Time.get_date_number(datetime(2026, 11, 12, tzinfo=UTC), offset=0)
        assert Time.from_date_number(number) == Time.from_date_number(
            number, offset=Time.get_timezone_offset()
        )


class TestTimeParseDate:
    def test_duration_is_now_plus_duration(self):
        before = datetime.now(UTC)
        result = Time.parse_date("1h")
        after = datetime.now(UTC)
        assert before + timedelta(hours=1) - timedelta(seconds=5) <= result
        assert result <= after + timedelta(hours=1) + timedelta(seconds=5)

    def test_time_of_day_is_today(self):
        result = Time.parse_date("23:59")
        today = datetime.now(UTC).date()
        assert result.date() == today
        assert (result.hour, result.minute) == (23, 59)

    def test_empty_is_now(self):
        before = datetime.now(UTC)
        result = Time.parse_date("")
        after = datetime.now(UTC)
        assert before - timedelta(seconds=5) <= result <= after + timedelta(seconds=5)

    def test_iso_date(self):
        assert Time.parse_date("2026-11-12") == datetime(2026, 11, 12, tzinfo=UTC)

    def test_iso_with_timezone_kept(self):
        assert Time.parse_date("2026-11-12T10:30:00+02:00").isoformat() == (
            "2026-11-12T10:30:00+02:00"
        )

    def test_month_day_year_branch_fails_loud(self):
        """The original yields an invalid date here; Python has no
        invalid-date value, so this fails loud instead."""
        with pytest.raises(ValueError, match='invalid date "11-12-13"'):
            Time.parse_date("11-12-13")

    def test_mdy_with_time_branch_fails_loud(self):
        """The M-D-Y branch (with a time part) prepends the current
        year; the result is never valid ISO, so this fails loud."""
        with pytest.raises(ValueError, match='invalid date "11-12-13:10:30"'):
            Time.parse_date("11-12-13:10:30")

    def test_unparseable_fails_loud(self):
        with pytest.raises(ValueError, match='invalid date "not-a-date"'):
            Time.parse_date("not-a-date")


class TestBinary:
    @pytest.mark.parametrize("value", [b"", b"abc", bytearray(b"x"), memoryview(b"y")])
    def test_is_source(self, value: Any):
        assert Binary.is_source(value) is True

    @pytest.mark.parametrize("value", ["str", None, 123, ["a"], {"a": 1}])
    def test_is_not_source(self, value: Any):
        assert Binary.is_source(value) is False

    def test_from_source_bytes_passthrough(self):
        data = b"abc"
        assert Binary.from_source(data) is data

    def test_from_source_bytearray_copies(self):
        result = Binary.from_source(bytearray(b"abc"))
        assert result == b"abc"
        assert type(result) is bytes

    def test_from_source_memoryview_slice(self):
        view = memoryview(b"abcdef")[2:4]
        assert Binary.from_source(view) == b"cd"

    def test_from_source_non_source_returned_as_is(self):
        assert Binary.from_source(5) == 5


class TestDefinePropertyDecision:
    def test_setattr_lands_visibly(self):
        """Python has no non-enumerable properties: setattr lands in
        __dict__ and dir(), so no defineProperty helper is ported —
        plain setattr is the direct equivalent."""

        class Target:
            pass

        target = Target()
        target.hidden = 1
        assert target.__dict__["hidden"] == 1
        assert "hidden" in dir(target)
