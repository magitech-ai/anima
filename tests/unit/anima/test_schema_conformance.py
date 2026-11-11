"""Conformance tests for the schema subset port (P1-3).

Each test pins one construct against the reference semantics: the exact
error strings, the exact output shapes, and the exact edge cases where a
naive Python translation would differ.
"""

import json
import math
import re

import pytest

from anima.core import Context, FiberState
from anima.schema import (
    _UNDEFINED,
    Schema,
    SchemaError,
    ValidationError,
    clone,
    deep_equal,
)
from anima.volatile import create_volatile, is_volatile

# ---------------------------------------------------------------------------
# S1 — ~standard + aggregated ValidationError
# ---------------------------------------------------------------------------


def test_standard_seam_success_and_single_issue():
    schema = Schema.object(
        {"port": Schema.integer().required(), "host": Schema.string()}
    )
    standard = getattr(schema, "~standard")
    assert standard.version == 1
    assert standard.vendor == "anima"

    assert standard.validate({"port": 8080}) == {"value": {"port": 8080}}

    result = standard.validate({"port": "bad"})
    assert result == {
        "issues": [
            {
                "message": "$.port expected integer, got str",
                "path": ["port"],
            }
        ]
    }


def test_standard_seam_nested_path_and_index():
    schema = Schema.object({"items": Schema.list(Schema.string())})
    result = getattr(schema, "~standard").validate({"items": ["ok", 7]})
    assert result == {
        "issues": [
            {
                "message": "$.items[1] expected string but got 7",
                "path": ["items", 1],
            }
        ]
    }


def test_standard_seam_reraises_non_validation_errors():
    def boom(value):
        raise TypeError("callback exploded")

    schema = Schema.transform(Schema.string(), boom)
    with pytest.raises(TypeError, match="callback exploded"):
        getattr(schema, "~standard").validate("x")


@pytest.mark.asyncio
async def test_config_resolution_aggregates_issues():
    schema = Schema.object(
        {
            "port": Schema.integer().required(),
            "host": Schema.string().min(3),
        }
    )

    class BadComponent:
        Config = schema

        def __init__(self, ctx, **cfg):
            self.cfg = cfg

    ctx = Context()
    fib = ctx.use(BadComponent, port="nope", host="ab")
    # Validation is lazy; the failure lands when the async load runs.
    await ctx.flush()
    assert fib.state == FiberState.FAILED
    err = fib.error
    assert isinstance(err, ValidationError)
    assert isinstance(err, SchemaError)
    assert str(err) == (
        "invalid config:\n  - $.port expected integer, got str (at port)"
    )
    assert err.issues == [
        {"message": "$.port expected integer, got str", "path": ["port"]}
    ]


def test_config_resolution_top_level_issue_has_no_path_suffix():
    # A top-level failure carries an empty path, so the aggregated line
    # has no "(at ...)" suffix. (Non-dict configs cannot flow through
    # ctx.use today — the registry coerces config to a dict — so the seam
    # is exercised directly here.)
    result = getattr(Schema.integer().required(), "~standard").validate("not_an_int")
    assert result == {"issues": [{"message": "expected integer, got str", "path": []}]}
    err = ValidationError(result["issues"])
    assert isinstance(err, SchemaError)
    assert str(err) == "invalid config:\n  - expected integer, got str"


# ---------------------------------------------------------------------------
# S2 (schema half) — .volatile() hook + placement validation
# ---------------------------------------------------------------------------


def test_volatile_field_validates_to_reference():
    schema = Schema.object(
        {"theme": Schema.string().volatile(), "level": Schema.number()}
    )
    out = schema.validate({"theme": "dark", "level": 3})
    assert is_volatile(out["theme"])
    assert out["theme"].get() == "dark"
    assert out["level"] == 3


def test_volatile_absent_field_yields_reference_to_none():
    schema = Schema.object({"theme": Schema.string().volatile()})
    out = schema.validate({})
    assert is_volatile(out["theme"])
    assert out["theme"].get() is None


def test_volatile_double_wrap_throws():
    with pytest.raises(TypeError, match="already wrapped"):
        Schema.string().volatile().volatile()


def test_volatile_placement_rules():
    # Allowed: fixed object paths, including nested under plain objects.
    ok = Schema.object({"a": Schema.object({"b": Schema.string().volatile()})})
    assert is_volatile(ok.validate({"a": {"b": "x"}})["a"]["b"])

    # Allowed: an entire object marked volatile.
    whole = Schema.object({"a": Schema.string()}).volatile()
    ref = whole.validate({"a": "x"})
    assert is_volatile(ref)
    assert ref.get() == {"a": "x"}

    # Rejected: inside an array, a dict, a union member, a transform inner,
    # or another volatile field.
    bad_cases = [
        Schema.list(Schema.string().volatile()),
        Schema.dict(Schema.string().volatile()),
        Schema.union(Schema.string().volatile(), Schema.number()),
        Schema.transform(Schema.string().volatile(), str.upper),
        Schema.object({"v": Schema.string().volatile()}).volatile(),
    ]
    for bad in bad_cases:
        with pytest.raises(
            SchemaError,
            match="volatile fields require a fixed object path",
        ):
            bad.validate({})


def test_volatile_snapshot_rejection_at_validation():
    schema = Schema.object({"fn": Schema.any().volatile()})
    with pytest.raises(SchemaError, match="cannot contain functions"):
        schema.validate({"fn": len})


def test_update_volatile_replaces_wholesale():
    target = create_volatile({"a": 1})
    source = create_volatile({"b": 2})
    from anima.volatile import update_volatile

    update_volatile(target, source)
    assert target.get() == {"b": 2}
    assert target.get() is source.get()


# ---------------------------------------------------------------------------
# S3 — natural + step (decimal-shift arithmetic)
# ---------------------------------------------------------------------------


def test_natural():
    natural = Schema.natural()
    assert natural.validate(0) == 0
    assert natural.validate(2) == 2
    assert natural.validate(2.0) == 2.0
    with pytest.raises(SchemaError):
        natural.validate(-1)
    with pytest.raises(SchemaError):
        natural.validate(1.5)
    with pytest.raises(SchemaError):
        natural.validate("2")


def test_step_decimal_shift():
    stepped = Schema.number().step(0.1)
    # Float % would reject these (0.3 % 0.1 == 0.0999...); decimal-shift
    # accepts them.
    assert stepped.validate(0.3) == 0.3
    assert stepped.validate(1.1) == 1.1
    with pytest.raises(SchemaError):
        stepped.validate(0.35)

    hundredth = Schema.number().step(0.01)
    assert hundredth.validate(0.29) == 0.29
    with pytest.raises(SchemaError):
        hundredth.validate(0.295)

    # Step is measured from min, not from zero.
    offset = Schema.number().min(5).step(2)
    assert offset.validate(7) == 7
    with pytest.raises(SchemaError):
        offset.validate(6)

    # Falsy step skips the check; a negative step is absolutized.
    assert Schema.number().step(0).validate(0.33) == 0.33
    assert Schema.number().step(-0.1).validate(0.3) == 0.3


def test_nan_and_infinity_step_edges():
    assert math.isnan(Schema.number().validate(float("nan")))
    with pytest.raises(SchemaError):
        Schema.natural().validate(float("nan"))
    assert Schema.number().validate(float("inf")) == float("inf")
    with pytest.raises(SchemaError):
        Schema.natural().validate(float("inf"))


# ---------------------------------------------------------------------------
# S4 — pattern
# ---------------------------------------------------------------------------


def test_pattern():
    url = Schema.string().pattern(r"^https?://\S+$")
    assert url.validate("https://example.com/x") == "https://example.com/x"
    with pytest.raises(SchemaError) as exc:
        url.validate("ftp://example.com")
    assert str(exc.value) == ("expect string to match regexp /^https?://\\S+$/")

    # Unanchored: .test is a substring search.
    assert Schema.string().pattern("ell").validate("hello") == "hello"

    # The pattern check runs before the length check.
    short = Schema.string().pattern(r"^a+$").min(5)
    with pytest.raises(SchemaError, match="expect string to match regexp"):
        short.validate("b")

    # Compiled patterns keep their flags in the message.
    flagged = Schema.string().pattern(re.compile(r"^abc$", re.IGNORECASE))
    assert flagged.validate("ABC") == "ABC"
    with pytest.raises(SchemaError) as exc:
        flagged.validate("abd")
    assert "/^abc$/i" in str(exc.value)


# ---------------------------------------------------------------------------
# S5 — object semantics (merge / omit / strict)
# ---------------------------------------------------------------------------


def test_object_merges_unknown_keys_and_omits_absent_optionals():
    schema = Schema.object({"a": Schema.number()})
    out = schema.validate({"a": 1, "extra": "kept", "n": None})
    assert out == {"a": 1.0, "extra": "kept", "n": None}
    # Absent optional is omitted; explicitly present null is kept.
    assert schema.validate({}) == {}
    assert schema.validate({"a": None}) == {"a": None}


def test_object_factory_defaults_for_nested_containers():
    schema = Schema.object(
        {
            "child": Schema.object({"x": Schema.number().default(1)}),
            "tags": Schema.list(Schema.string()),
        }
    )
    # Absent optional object/list fields become {} / [], not omitted.
    assert schema.validate({}) == {"child": {"x": 1}, "tags": []}


def test_strict_mode_drops_unknown_keys_via_transform_inner():
    inner = Schema.object({"a": Schema.number()})
    transformed = Schema.transform(inner, lambda v: v)
    # Transform inners resolve strict, so unknown keys are dropped.
    assert transformed.validate({"a": 1, "extra": "gone"}) == {"a": 1.0}


# ---------------------------------------------------------------------------
# S6 — union error shape + adapted values
# ---------------------------------------------------------------------------


def test_union_error_shape():
    union = Schema.union(Schema.string(), Schema.integer())
    with pytest.raises(SchemaError) as exc:
        union.validate(3.14)
    assert str(exc.value) == "expected string | integer but got 3.14"
    assert exc.value.path == []


def test_union_of_consts_enum_idiom():
    enum = Schema.union(Schema.const("none"), Schema.const("deflate"))
    assert enum.validate("none") == "none"
    assert enum.validate("deflate") == "deflate"
    with pytest.raises(SchemaError) as exc:
        enum.validate("gzip")
    assert str(exc.value) == 'expected "none" | "deflate" but got "gzip"'


def test_union_returns_adapted_value():
    union = Schema.union(Schema.transform(Schema.string(), str.upper), Schema.number())
    assert union.validate("a") == "A"
    assert union.validate(5) == 5.0


def test_union_json_stringify_edges():
    union = Schema.union(Schema.string(), Schema.integer())
    with pytest.raises(SchemaError) as exc:
        union.validate(float("nan"))
    assert str(exc.value) == "expected string | integer but got null"
    with pytest.raises(TypeError, match="circular"):
        circular: list = []
        circular.append(circular)
        union.validate(circular)


def test_union_accepts_list_argument():
    # A single list argument is unwrapped, matching the varargs form.
    union = Schema.union([Schema.string(), Schema.integer()])
    assert union.validate("a") == "a"
    assert union.validate(2) == 2
    with pytest.raises(SchemaError) as exc:
        union.validate(3.14)
    assert str(exc.value) == "expected string | integer but got 3.14"
    # The list form of the enum idiom.
    enum = Schema.union(
        [Schema.const("darwin"), Schema.const("win32"), Schema.const(None)]
    )
    assert enum.validate("darwin") == "darwin"
    assert enum.validate(None) is None


# ---------------------------------------------------------------------------
# S7 — array-length min/max
# ---------------------------------------------------------------------------


def test_array_length_min_max():
    bounded = Schema.list(Schema.string()).min(2).max(3)
    assert bounded.validate(["a", "b"]) == ["a", "b"]
    with pytest.raises(SchemaError) as exc:
        bounded.validate(["a"])
    assert str(exc.value) == "expected array length >= 2 but got 1"
    with pytest.raises(SchemaError) as exc:
        bounded.validate(["a", "b", "c", "d"])
    assert str(exc.value) == "expected array length <= 3 but got 4"


def test_array_length_min_skipped_when_inner_has_default():
    schema = Schema.list(Schema.string().default("x")).min(2)
    assert schema.validate([]) == []


def test_range_error_messages():
    with pytest.raises(SchemaError) as exc:
        Schema.number().max(50).validate(60)
    assert str(exc.value) == "expected number <= 50 but got 60"
    with pytest.raises(SchemaError) as exc:
        Schema.string().min(3).validate("hi")
    assert str(exc.value) == "expected string length >= 3 but got 2"


# ---------------------------------------------------------------------------
# S10 — required-before-default ordering
# ---------------------------------------------------------------------------


def test_required_before_default():
    # Required throws even when a default exists.
    schema = Schema.string().required().default("fallback")
    with pytest.raises(SchemaError, match="missing required value"):
        schema.validate(None)
    assert schema.validate("ok") == "ok"


def test_default_does_not_imply_required():
    schema = Schema.string().default("fallback")
    assert schema.validate(None) == "fallback"
    assert schema.validate("ok") == "ok"


def test_default_none_returns_null():
    # A None default on null input returns null without dispatching.
    assert Schema.string().default(None).validate(None) is None
    schema = Schema.object({"a": Schema.string().default(None)})
    # An absent property with a None default stays absent.
    assert schema.validate({}) == {}
    # Explicit null on the property is kept.
    assert schema.validate({"a": None}) == {"a": None}


def test_default_is_validated_and_cloned():
    with pytest.raises(SchemaError):
        Schema.integer().default("not_an_int").validate(None)

    schema = Schema.object({"a": Schema.number()}).default({"a": 1})
    first = schema.validate(None)
    first["a"] = 99
    assert schema.validate(None) == {"a": 1.0}


# ---------------------------------------------------------------------------
# S9 — const via deepEqual
# ---------------------------------------------------------------------------


def test_const_uses_deep_equal_and_returns_canonical():
    const_val = {"a": [1, 2]}
    schema = Schema.const(const_val)
    result = schema.validate({"a": [1, 2]})
    assert result is const_val
    with pytest.raises(SchemaError) as exc:
        schema.validate({"a": [1, 3]})
    assert "expected [object Object] but got [object Object]" in str(exc.value)


def test_const_primitive_edges():
    assert Schema.const(1).validate(1) == 1
    with pytest.raises(SchemaError):
        Schema.const(1).validate(True)
    with pytest.raises(SchemaError):
        Schema.const(float("nan")).validate(float("nan"))
    # Explicit null with no default passes the nullable branch as null.
    assert Schema.const("x").validate(None) is None


# ---------------------------------------------------------------------------
# S11 — role / loose / extra / never / transform / toJSON
# ---------------------------------------------------------------------------


def test_role_and_extra_metadata():
    schema = Schema.string().role("secret").extra("link", "https://example.com")
    assert schema._role == "secret"
    assert schema._extra_meta == {"link": "https://example.com"}
    assert schema.validate("s3cr3t") == "s3cr3t"
    meta = schema.to_json()["refs"][schema.to_json()["uid"]]["meta"]
    assert meta["role"] == "secret"
    assert meta["link"] == "https://example.com"

    credential = Schema.string().role("credential-ref").default("MY_API_KEY")
    assert credential._role == "credential-ref"


def test_loose_fallback():
    schema = Schema.string().loose().default("fallback")
    assert schema.validate(123) == "fallback"
    # Without a default, loose falls back to undefined (None at the top).
    assert Schema.string().loose().validate(123) is None
    # Loose does not catch the required throw.
    with pytest.raises(SchemaError, match="missing required value"):
        Schema.string().required().loose().validate(None)


def test_loose_does_not_catch_unsupported_type():
    # The unsupported-type error is raised before the loose fallback.
    with pytest.raises(SchemaError, match="unsupported type"):
        Schema("unknown_type").loose().validate("x")


def test_never_tombstone():
    tombstone = Schema.never().description("Removed. Use the new field.")
    # Absent: omitted; explicit null: kept; any other value: fails loud.
    assert Schema.object({"old": tombstone}).validate({}) == {}
    assert Schema.object({"old": tombstone}).validate({"old": None}) == {"old": None}
    with pytest.raises(SchemaError) as exc:
        tombstone.validate("still here")
    assert str(exc.value) == "expected nullable but got still here"


def test_transform_preserve_flag():
    calls: list = []

    def upper(value):
        calls.append(value)
        return value.upper()

    plain = Schema.transform(Schema.string(), upper)
    assert plain.validate("a") == "A"
    assert calls == ["a", "a"]  # once on result, once on adapted input

    calls.clear()
    preserved = Schema.transform(Schema.string(), upper, preserve=True)
    assert preserved.validate("a") == "A"
    assert calls == ["a"]  # once only, no adapted value


def test_transform_adapted_value_written_back():
    schema = Schema.object({"v": Schema.transform(Schema.string(), str.upper)})
    data = {"v": "a"}
    out = schema.validate(data)
    assert out == {"v": "A"}
    assert data == {"v": "A"}


def test_to_json_round_trip():
    schema = Schema.object(
        {
            "name": Schema.string().required().description("A name"),
            "level": Schema.number().min(0).max(10).step(0.5).default(1),
            "mode": Schema.union(Schema.const("a"), Schema.const("b")),
            "secret": Schema.string().role("secret"),
            "tags": Schema.list(Schema.string()),
        }
    ).description("Settings")
    blob = schema.to_json()
    restored = Schema.from_json(json.loads(json.dumps(blob)))
    assert restored.validate({"name": "x"}) == schema.validate({"name": "x"})
    with pytest.raises(SchemaError):
        restored.validate({"name": "x", "level": 99})
    assert restored.to_json()["uid"] != schema.to_json()["uid"]


def test_to_json_shared_reference_preserved():
    shared = Schema.string().min(2)
    schema = Schema.object({"a": shared, "b": shared})
    blob = schema.to_json()
    assert len(blob["refs"]) == 2  # root + one shared inner
    restored = Schema.from_json(json.loads(json.dumps(blob)))
    assert restored.validate({"a": "xy", "b": "zw"}) == {
        "a": "xy",
        "b": "zw",
    }


def test_to_json_transform_callback_fails_loud():
    schema = Schema.transform(Schema.string(), str.upper)
    with pytest.raises(TypeError, match="cannot serialize"):
        schema.to_json()


def test_to_string_renderings():
    assert str(Schema.string()) == "string"
    assert str(Schema.union(Schema.string(), Schema.integer())) == ("string | integer")
    assert (
        str(Schema.object({"a": Schema.string().required(), "b": Schema.number()}))
        == "{ a: string, b?: number }"
    )
    assert str(Schema.list(Schema.string())) == "string[]"
    assert str(Schema.const("x")) == '"x"'


# ---------------------------------------------------------------------------
# deepEqual / clone
# ---------------------------------------------------------------------------


def test_deep_equal():
    assert deep_equal({"a": 1}, {"a": 1, "b": None})
    assert deep_equal([1, {"x": 2}], [1, {"x": 2}])
    assert not deep_equal({"a": 1}, {"a": 2})
    assert not deep_equal([1, 2], [1, 2, 3])
    assert not deep_equal(1, True)
    assert deep_equal(1, 1.0)
    assert not deep_equal(float("nan"), float("nan"))
    assert deep_equal(-0.0, 0.0)

    a: dict = {}
    a["self"] = a
    assert deep_equal(a, a)
    b: dict = {}
    b["self"] = b
    assert not deep_equal(a, b)

    v1 = create_volatile({"x": 1})
    v2 = create_volatile({"x": 2})
    assert deep_equal(v1, v2)
    assert not deep_equal(v1, {"x": 1})


def test_clone():
    from anima.schema import clone

    original = {"a": [1, {"b": 2}]}
    copied = clone(original)
    assert copied == original
    assert copied is not original
    copied["a"].append(3)
    assert original == {"a": [1, {"b": 2}]}

    cyclic: dict = {}
    cyclic["self"] = cyclic
    cloned = clone(cyclic)
    assert cloned["self"] is cloned

    ref = create_volatile({"x": 1})
    assert clone(ref) is ref


# ---------------------------------------------------------------------------
# Fail loud on unported constructs
# ---------------------------------------------------------------------------


def test_unported_constructs_fail_loud():
    for type_name in (
        "intersect",
        "tuple",
        "date",
        "lazy",
        "is",
        "function",
        "regExp",
        "arrayBuffer",
        "bitset",
        "percent",
    ):
        with pytest.raises(SchemaError, match="unsupported type"):
            Schema(type_name).validate("x")
    for name in ("simplify", "i18n", "autofix", "extend", "from"):
        assert not hasattr(Schema("string"), name)
        assert not hasattr(Schema, name)
    # Unknown type names fail at validation, never pass through.
    with pytest.raises(SchemaError, match="unsupported type"):
        Schema("bogus").validate(1)
    # from_json rejects unknown types too.
    with pytest.raises(TypeError, match="unsupported type"):
        Schema.from_json({"uid": 1, "refs": {1: {"type": "tuple", "list": []}}})


# ---------------------------------------------------------------------------
# JS number formatting (error messages use JS coercion, not Python's)
# ---------------------------------------------------------------------------


def test_js_number_formatting_in_range_errors():
    # 0.0 renders as "0", not "0.0".
    with pytest.raises(SchemaError) as exc:
        Schema.number().max(0.0).validate(1)
    assert str(exc.value) == "expected number <= 0 but got 1"
    # Integral floats render without ".0".
    with pytest.raises(SchemaError) as exc:
        Schema.number().max(60).validate(61)
    assert str(exc.value) == "expected number <= 60 but got 61"
    # Small decimals render fixed down to 1e-6.
    with pytest.raises(SchemaError) as exc:
        Schema.number().max(1e-6).validate(2e-6)
    assert str(exc.value) == "expected number <= 0.000001 but got 0.000002"
    # Below 1e-6 renders exponential, with the sign on the mantissa.
    with pytest.raises(SchemaError) as exc:
        Schema.number().min(-1e-7).validate(-2e-7)
    assert str(exc.value) == "expected number >= -1e-7 but got -2e-7"
    with pytest.raises(SchemaError) as exc:
        Schema.number().min(-1.5e-7).validate(-2e-7)
    assert str(exc.value) == "expected number >= -1.5e-7 but got -2e-7"
    # 1e21 and above render exponential.
    with pytest.raises(SchemaError) as exc:
        Schema.number().max(1e21).validate(2e21)
    assert str(exc.value) == "expected number <= 1e+21 but got 2e+21"
    with pytest.raises(SchemaError) as exc:
        Schema.number().max(1.5e21).validate(2e21)
    assert str(exc.value) == "expected number <= 1.5e+21 but got 2e+21"
    # Large fixed values render without an exponent below 1e21.
    with pytest.raises(SchemaError) as exc:
        Schema.number().max(1.23456e17).validate(2e17)
    assert str(exc.value) == (
        "expected number <= 123456000000000000 but got 200000000000000000"
    )


def test_step_error_uses_js_number_formatting():
    with pytest.raises(SchemaError) as exc:
        Schema.number().step(0.1).validate(0.25)
    assert str(exc.value) == "expected number multiple of 0.1 but got 0.25"


def test_bool_bounds_and_step_fail_loud():
    # A bool is not a number; it must not silently act as 1.
    with pytest.raises(TypeError, match="step must be a number"):
        Schema.number().step(True)
    with pytest.raises(TypeError, match="min must be a number"):
        Schema.number().min(True)
    with pytest.raises(TypeError, match="max must be a number"):
        Schema.number().max(True)
    with pytest.raises(TypeError, match="step must be a number"):
        Schema.number().step("0.1")


def test_step_decimal_shift_with_exponential_data():
    # 1e-7 is not a multiple of 0.01; the shift runs on its exponential
    # form rather than the float modulo.
    with pytest.raises(SchemaError):
        Schema.number().step(0.01).validate(1e-7)


# ---------------------------------------------------------------------------
# JS template coercion in error messages
# ---------------------------------------------------------------------------


def test_const_error_coerces_values_like_js():
    # Null input never reaches the const resolver: the nullable branch
    # returns it as-is (the oracle intercepts const(null) the same way).
    assert Schema.const(5).validate(None) is None
    with pytest.raises(SchemaError) as exc:
        Schema.const(5).validate(False)
    assert str(exc.value) == "expected 5 but got false"
    with pytest.raises(SchemaError) as exc:
        Schema.const([1, 2]).validate([1, 3])
    assert str(exc.value) == "expected 1,2 but got 1,3"
    with pytest.raises(SchemaError) as exc:
        Schema.const({"a": 1}).validate({"a": 2})
    assert str(exc.value) == "expected [object Object] but got [object Object]"
    with pytest.raises(SchemaError) as exc:
        Schema.const(5).validate(object())
    assert str(exc.value).startswith("expected 5 but got <object object at ")


def test_union_error_json_renders_nested_values():
    union = Schema.union(Schema.integer())
    with pytest.raises(SchemaError) as exc:
        union.validate("x")
    assert str(exc.value) == 'expected integer but got "x"'
    with pytest.raises(SchemaError) as exc:
        union.validate({"a": None, "b": True, "c": False})
    assert str(exc.value) == ('expected integer but got {"a":null,"b":true,"c":false}')
    with pytest.raises(SchemaError) as exc:
        union.validate([1, lambda: None])
    assert str(exc.value) == "expected integer but got [1,null]"
    with pytest.raises(SchemaError) as exc:
        union.validate({"f": lambda: None})
    assert str(exc.value) == "expected integer but got {}"
    with pytest.raises(SchemaError) as exc:
        union.validate(object())
    assert str(exc.value).startswith('expected integer but got "<object object at ')


# ---------------------------------------------------------------------------
# Rendering, definition errors, and JSON round-trip completeness
# ---------------------------------------------------------------------------


def test_validate_accepts_list_path():
    with pytest.raises(SchemaError) as exc:
        Schema.integer().validate("x", ["a", 0])
    assert str(exc.value) == "$.a[0] expected integer, got str"
    assert exc.value.path == ["a", 0]


def test_pattern_round_trip_without_flags_and_with_m_only():
    plain = Schema.from_json(Schema.string().pattern("x").to_json())
    assert plain.validate("x") == "x"
    with pytest.raises(SchemaError):
        plain.validate("y")
    multi = Schema.from_json(
        Schema.string().pattern(re.compile("^b", re.MULTILINE)).to_json()
    )
    assert multi.validate("a\nb") == "a\nb"
    with pytest.raises(SchemaError):
        multi.validate("ab")


def test_to_string_covers_all_types():
    assert Schema.boolean().to_string() == "boolean"
    assert Schema.any().to_string() == "any"
    assert Schema.never().to_string() == "never"
    assert Schema.const(42).to_string() == "42"
    assert Schema.const("x").to_string() == '"x"'
    assert Schema.dict(Schema.string()).to_string() == "{ [key: string]: string }"
    assert Schema.object({}).to_string() == "{}"
    assert Schema("const").to_string() == "undefined"
    assert Schema.const(None).to_string() == "null"
    assert Schema.transform(Schema.string(), str.upper).to_string() == "string"
    assert Schema("bogus").to_string() == "Schema<bogus>"


def test_transform_definition_errors():
    with pytest.raises(TypeError, match="transform inner must be a Schema"):
        Schema.transform("not-a-schema", str.upper)
    with pytest.raises(TypeError, match="transform callback must be callable"):
        Schema.transform(Schema.string(), "not-callable")
    with pytest.raises(SchemaError, match="transform schema has no callback"):
        Schema("transform").validate(1)


def test_array_constructor_alias():
    assert Schema("array").type_name == "list"


def test_pattern_flags_multiline_dotall_and_invalid():
    multiline = Schema.string().pattern(re.compile("^b", re.MULTILINE))
    assert multiline.validate("a\nb") == "a\nb"
    with pytest.raises(SchemaError):
        Schema.string().pattern(re.compile("^b")).validate("a\nb")
    dotall = Schema.string().pattern(re.compile("a.b", re.DOTALL))
    assert dotall.validate("a\nb") == "a\nb"
    with pytest.raises(SchemaError):
        Schema.string().pattern(re.compile("a.b")).validate("a\nb")
    data = Schema.string().pattern(re.compile("x")).to_json()
    assert data["refs"][data["uid"]]["meta"]["pattern"] == {
        "source": "x",
        "flags": "",
    }
    with pytest.raises(TypeError, match="pattern must be a string or compiled pattern"):
        Schema.string().pattern(123)


def test_json_round_trip_preserves_all_meta():
    schema = (
        Schema.string()
        .pattern(re.compile("^a", re.IGNORECASE | re.MULTILINE | re.DOTALL))
        .default("a")
        .description("doc")
        .hidden()
        .required()
        .min(1)
        .max(5)
        .role("secret", "extra-value")
        .extra("custom", 7)
    )
    data = schema.to_json()
    node = data["refs"][data["uid"]]
    assert node["meta"]["pattern"] == {"source": "^a", "flags": "ims"}
    assert node["meta"]["default"] == "a"
    assert node["meta"]["description"] == "doc"
    assert node["meta"]["hidden"] is True
    assert node["meta"]["required"] is True
    assert node["meta"]["role"] == "secret"
    assert node["meta"]["extra"] == "extra-value"
    assert node["meta"]["min"] == 1
    assert node["meta"]["max"] == 5
    assert node["meta"]["custom"] == 7
    rebuilt = Schema.from_json(data)
    rebuilt_data = rebuilt.to_json()
    assert rebuilt_data["refs"][rebuilt_data["uid"]]["meta"] == node["meta"]
    assert rebuilt.validate("A") == "A"
    with pytest.raises(SchemaError):
        rebuilt.validate("b")


def test_json_round_trip_preserves_loose():
    schema = Schema.string().loose().default("fallback")
    data = schema.to_json()
    assert data["refs"][data["uid"]]["meta"]["loose"] is True
    rebuilt = Schema.from_json(data)
    assert rebuilt.validate(123) == "fallback"


def test_json_round_trip_preserves_volatile():
    schema = Schema.string().volatile()
    data = schema.to_json()
    assert data["refs"][data["uid"]]["meta"]["volatile"] is True
    rebuilt = Schema.from_json(data)
    assert is_volatile(rebuilt.validate("x"))


def test_json_round_trip_transform_without_callback():
    schema = Schema("transform")
    data = schema.to_json()
    assert data["refs"][data["uid"]]["preserve"] is False
    rebuilt = Schema.from_json(data)
    assert rebuilt.type_name == "transform"
    with pytest.raises(SchemaError, match="transform schema has no callback"):
        rebuilt.validate(1)


def test_from_json_rejects_invalid_reference():
    with pytest.raises(TypeError, match="invalid schema reference"):
        Schema.from_json(
            {"uid": 1, "refs": {1: {"type": "object", "dict": {"a": "bogus"}}}}
        )


def test_to_json_schema_covers_pattern_never_and_transform():
    assert Schema.string().pattern("^a$").to_json_schema() == {
        "type": "string",
        "pattern": "^a$",
    }
    assert Schema.never().to_json_schema() == {"not": {}}
    assert Schema.transform(Schema.string(), str.upper).to_json_schema() == {
        "type": "string"
    }


# ---------------------------------------------------------------------------
# deep_equal / clone edges
# ---------------------------------------------------------------------------


def test_deep_equal_nullish_and_type_edges():
    assert deep_equal(None, _UNDEFINED) is True
    assert deep_equal(None, 1, strict=True) is False
    assert deep_equal(1, "a") is False
    assert deep_equal({}, []) is False
    assert deep_equal({"a": 1}, {"a": 1, "b": None}) is True


def test_clone_deep_copies_non_plain_values():
    original = {1, 2, 3}
    copied = clone(original)
    assert copied == original
    assert copied is not original
