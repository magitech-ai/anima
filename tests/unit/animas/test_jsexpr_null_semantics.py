"""JavaScript null-vs-undefined semantics in the restricted evaluator.

The evaluator maps ``undefined`` to Python ``None`` (ADR 0006), so the
``null`` literal carries its own sentinel internally: collapsing both onto
``None`` made ``String(null)`` return ``"undefined"``, ``Number(null)``
return NaN, ``null === undefined`` return true and ``null < 1`` return
false -- four behaviours plain JavaScript gets the other way around.
"""

import math

import pytest

from animas.core import Context
from animas.jsexpr import (
    JsExprError,
    _js_loose_equal,
    _js_number,
    _js_strict_equal,
    _js_string,
    evaluate,
    interpolate,
)


def _ctx() -> Context:
    return Context()


def test_string_null_is_null_not_undefined() -> None:
    ctx = _ctx()
    assert evaluate(ctx, "String(null)") == "null"
    assert evaluate(ctx, "String(undefined)") == "undefined"
    # JSON.parse has no undefined, so a JSON null is the null literal too.
    assert evaluate(ctx, "String(JSON.parse('null'))") == "null"
    assert evaluate(ctx, "JSON.parse('null')") is None


def test_number_null_is_zero() -> None:
    ctx = _ctx()
    assert evaluate(ctx, "Number(null)") == 0
    assert math.isnan(evaluate(ctx, "Number(undefined)"))
    # null coerces to 0 in comparisons, undefined does not coerce at all.
    assert evaluate(ctx, "null < 1") is True
    assert evaluate(ctx, "undefined < 1") is False
    assert evaluate(ctx, "null == 0") is False
    assert evaluate(ctx, "0 ?? 'd'") == 0


def test_null_and_undefined_are_distinct() -> None:
    ctx = _ctx()
    assert evaluate(ctx, "null === null") is True
    assert evaluate(ctx, "undefined === undefined") is True
    assert evaluate(ctx, "null === undefined") is False
    assert evaluate(ctx, "null == undefined") is True
    assert evaluate(ctx, "undefined == null") is True


def test_null_is_falsy_and_nullish() -> None:
    ctx = _ctx()
    assert evaluate(ctx, "!null") is True
    assert evaluate(ctx, "null || 'x'") == "x"
    assert evaluate(ctx, "null && 1") is None
    assert evaluate(ctx, "null ?? 'd'") == "d"
    assert evaluate(ctx, "undefined ?? 'd'") == "d"
    assert evaluate(ctx, "true ? null : 1") is None


def test_null_optional_chain_and_property_error() -> None:
    ctx = _ctx()
    assert evaluate(ctx, "null?.name") is None
    with pytest.raises(JsExprError, match="cannot read property 'name' of null"):
        evaluate(ctx, "null.name")
    with pytest.raises(JsExprError, match="cannot read property 'name' of undefined"):
        evaluate(ctx, "undefined.name")


def test_interpolate_normalises_null_to_none() -> None:
    ctx = _ctx()
    assert interpolate(ctx, {"__jsExpr": "null"}) is None
    assert interpolate(ctx, {"__jsExpr": "String(null)"}) == "null"


def test_null_helper_coercions() -> None:
    ctx = _ctx()
    # The public boundary still shows one shape: both nullish literals are None.
    assert evaluate(ctx, "null ?? undefined") is None
    assert evaluate(ctx, "undefined") is None
    assert _js_string(None) == "undefined"
    assert math.isnan(_js_number(None))
    # Arrays keep the JavaScript join: null and undefined both render empty.
    assert evaluate(ctx, "String(JSON.parse('[null, null]'))") == ","
    assert _js_strict_equal(None, None) is True
    assert _js_loose_equal(None, 0) is False
    assert _js_loose_equal(None, None) is True
