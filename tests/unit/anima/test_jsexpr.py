"""Unit tests for the restricted JavaScript expression evaluator."""

import io
import math
import os
import sys

import pytest
import yaml

from anima.core import Context
from anima.jsexpr import (
    JsExprError,
    _eval,
    _js_number,
    _js_number_string,
    _js_string,
    dump_yaml,
    evaluate,
    interpolate,
    load_yaml,
)


def _ctx() -> Context:
    return Context()


# --- Literals and names ---


def test_literal_values() -> None:
    ctx = _ctx()
    assert evaluate(ctx, "42") == 42
    assert evaluate(ctx, "3.5") == 3.5
    assert evaluate(ctx, "'hello'") == "hello"
    assert evaluate(ctx, '"hello"') == "hello"
    assert evaluate(ctx, "true") is True
    assert evaluate(ctx, "false") is False
    assert evaluate(ctx, "null") is None
    assert evaluate(ctx, "undefined") is None
    assert evaluate(ctx, ".5") == 0.5
    assert evaluate(ctx, "1e3") == 1000.0
    assert evaluate(ctx, "(42)") == 42


def test_string_escapes() -> None:
    ctx = _ctx()
    assert evaluate(ctx, r"'a\nb'") == "a\nb"
    assert evaluate(ctx, r"'a\rb'") == "a\rb"
    assert evaluate(ctx, r"'a\tb'") == "a\tb"
    assert evaluate(ctx, r"'a\bb'") == "a\bb"
    assert evaluate(ctx, r"'a\fb'") == "a\fb"
    assert evaluate(ctx, r"'a\vb'") == "a\vb"
    assert evaluate(ctx, r"'a\0b'") == "a\0b"
    assert evaluate(ctx, r"'a\'b'") == "a'b"
    assert evaluate(ctx, r'"a\"b"') == 'a"b'
    assert evaluate(ctx, r"'a\\b'") == "a\\b"
    assert evaluate(ctx, r"'\x41'") == "A"
    assert evaluate(ctx, r"'\u0041'") == "A"
    # Unknown escapes keep the escaped character, as in JavaScript.
    assert evaluate(ctx, r"'a\db'") == "adb"


def test_string_escape_errors() -> None:
    ctx = _ctx()
    with pytest.raises(JsExprError, match="invalid escape"):
        evaluate(ctx, r"'\x4'")
    with pytest.raises(JsExprError, match="invalid escape"):
        evaluate(ctx, r"'\xZZ'")
    with pytest.raises(JsExprError, match="invalid escape"):
        evaluate(ctx, r"'\u041'")
    with pytest.raises(JsExprError, match="invalid escape"):
        evaluate(ctx, r"'\u00ZZ'")
    with pytest.raises(JsExprError, match="unterminated string"):
        evaluate(ctx, "'abc")
    with pytest.raises(JsExprError, match="unterminated string"):
        evaluate(ctx, "'abc\\")
    with pytest.raises(JsExprError, match="unterminated string"):
        evaluate(ctx, "'ab\nc'")


def test_unknown_identifier_fails_loud() -> None:
    ctx = _ctx()
    with pytest.raises(JsExprError, match="unknown identifier"):
        evaluate(ctx, "homePath")
    with pytest.raises(JsExprError, match="unknown identifier"):
        evaluate(ctx, "foo")
    with pytest.raises(JsExprError, match="unknown identifier"):
        evaluate(ctx, "JSON")


def test_non_string_expression_fails_loud() -> None:
    ctx = _ctx()
    with pytest.raises(JsExprError, match="must be a string"):
        evaluate(ctx, 42)  # type: ignore[arg-type]


def test_empty_expression_fails_loud() -> None:
    ctx = _ctx()
    with pytest.raises(JsExprError, match="empty expression"):
        evaluate(ctx, "")
    with pytest.raises(JsExprError, match="empty expression"):
        evaluate(ctx, "   ")


# --- process.* ---


def test_process_env_read(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = _ctx()
    monkeypatch.setenv("ANIMA_JSEXPR_TEST", "hello")
    assert evaluate(ctx, "process.env.ANIMA_JSEXPR_TEST") == "hello"
    assert evaluate(ctx, "process.env.ANIMA_JSEXPR_MISSING") is None


def test_process_platform() -> None:
    ctx = _ctx()
    expected = sys.platform
    if expected.startswith("linux"):
        expected = "linux"
    assert evaluate(ctx, "process.platform") == expected
    assert evaluate(ctx, "process.platform === 'win32'") == (expected == "win32")
    assert evaluate(ctx, "process.platform !== 'win32'") == (expected != "win32")


def test_process_cwd_and_exec_path(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = _ctx()
    assert evaluate(ctx, "process.cwd()") == os.getcwd()
    assert evaluate(ctx, "process.cwd") == os.getcwd()
    assert evaluate(ctx, "process.execPath") == sys.executable
    monkeypatch.setattr(os, "getcwd", lambda: "/fake/cwd")
    assert evaluate(ctx, "process.cwd()") == "/fake/cwd"
    assert evaluate(ctx, "process.cwd") == "/fake/cwd"


def test_process_platform_mapping(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = _ctx()
    monkeypatch.setattr(sys, "platform", "win32")
    assert evaluate(ctx, "process.platform") == "win32"
    monkeypatch.setattr(sys, "platform", "darwin")
    assert evaluate(ctx, "process.platform") == "darwin"
    monkeypatch.setattr(sys, "platform", "linux2")
    assert evaluate(ctx, "process.platform") == "linux"
    monkeypatch.setattr(sys, "platform", "freebsd13")
    assert evaluate(ctx, "process.platform") == "freebsd13"


def test_process_unsupported_member_fails_loud() -> None:
    ctx = _ctx()
    with pytest.raises(JsExprError, match="unsupported process member"):
        evaluate(ctx, "process.getBuiltinModule")
    with pytest.raises(JsExprError, match="unsupported process member"):
        evaluate(ctx, "process.versions")


def test_process_env_bare() -> None:
    ctx = _ctx()
    env = evaluate(ctx, "process.env")
    assert isinstance(env, dict)
    assert env.get("PATH") == os.environ.get("PATH")


# --- ctx.* ---


def test_ctx_get_missing_is_none() -> None:
    ctx = _ctx()
    assert evaluate(ctx, "ctx.get('profileContext')") is None
    assert evaluate(ctx, "ctx.get('answer')") is None
    assert evaluate(ctx, "!ctx.get('profileContext')") is True


def test_ctx_get_with_default() -> None:
    ctx = _ctx()
    assert evaluate(ctx, "ctx.get('missing', 'fallback')") == "fallback"
    ctx.provide("present", "value")
    assert evaluate(ctx, "ctx.get('present', 'fallback')") == "value"


def test_ctx_get_profile_context() -> None:
    ctx = _ctx()
    ctx.provide("profileContext", {"name": "desktop"})
    assert evaluate(ctx, "ctx.get('profileContext')") == {"name": "desktop"}
    assert evaluate(ctx, "ctx.get('profileContext')?.name !== 'desktop'") is False
    assert evaluate(ctx, "ctx.get('profileContext').name") == "desktop"


def test_ctx_get_optional_chain_on_missing() -> None:
    ctx = _ctx()
    assert evaluate(ctx, "ctx.get('profileContext')?.name") is None
    assert evaluate(ctx, "ctx.get('profileContext')?.name !== 'desktop'") is True
    assert evaluate(ctx, "ctx.get('profileContext')?.name === 'desktop'") is False


def test_ctx_attribute_service_access() -> None:
    ctx = _ctx()
    ctx.provide("headlessStartup", {"host": "127.0.0.1"})
    assert evaluate(ctx, "ctx.headlessStartup.host") == "127.0.0.1"
    ctx.provide("webStartup", {})
    assert evaluate(ctx, "ctx.webStartup.host ?? 'fallback'") == "fallback"
    assert evaluate(ctx, "ctx.missingService") is None
    assert evaluate(ctx, "ctx.baseUrl") is None
    # A missing service's attribute throws, as in JavaScript: the ?? only
    # guards the value, not the access.
    ctx2 = _ctx()
    with pytest.raises(JsExprError, match="cannot read property"):
        evaluate(ctx2, "ctx.webStartup.host ?? 'fallback'")


def test_ctx_disabled_is_absent_and_falsy() -> None:
    # No "disabled" attribute exists on a context: the attested
    # `ctx.disabled` form evaluates to undefined (None), hence falsy.
    ctx = _ctx()
    assert evaluate(ctx, "ctx.disabled") is None
    assert evaluate(ctx, "!ctx.disabled") is True
    assert evaluate(ctx, "ctx.disabled ?? 'off'") == "off"


def test_ctx_get_arity_and_name_errors() -> None:
    ctx = _ctx()
    with pytest.raises(JsExprError, match="ctx.get expects 1 or 2 arguments"):
        evaluate(ctx, "ctx.get()")
    with pytest.raises(JsExprError, match="ctx.get expects 1 or 2 arguments"):
        evaluate(ctx, "ctx.get('a', 'b', 'c')")
    with pytest.raises(JsExprError, match="ctx.get expects a string name"):
        evaluate(ctx, "ctx.get(42)")


def test_attribute_of_undefined_fails_loud() -> None:
    ctx = _ctx()
    with pytest.raises(JsExprError, match="cannot read property"):
        evaluate(ctx, "ctx.get('missing').name")
    with pytest.raises(JsExprError, match="cannot read property"):
        evaluate(ctx, "ctx.missingService.deep")


def test_optional_chain_short_circuits_rest_of_chain() -> None:
    ctx = _ctx()
    # The ?. short-circuits the whole chain once the base is nullish.
    assert evaluate(ctx, "ctx.get('missing')?.name.length") is None
    assert evaluate(ctx, "ctx.get('missing')?.name === undefined") is True
    # But a plain access after an optional one still throws on undefined.
    ctx.provide("holder", {"inner": None})
    with pytest.raises(JsExprError, match="cannot read property"):
        evaluate(ctx, "ctx.holder.inner.deep")
    with pytest.raises(JsExprError, match="cannot read property"):
        evaluate(ctx, "ctx.get('holder')?.inner.deep")


def test_string_and_list_length() -> None:
    ctx = _ctx()
    assert evaluate(ctx, "'hello'.length") == 5
    assert evaluate(ctx, "JSON.parse('[1, 2, 3]').length") == 3
    with pytest.raises(JsExprError, match="unsupported string member"):
        evaluate(ctx, "'hello'.toUpperCase")
    with pytest.raises(JsExprError, match="unsupported list member"):
        evaluate(ctx, "JSON.parse('[1]').push")
    with pytest.raises(JsExprError, match="unsupported call"):
        evaluate(ctx, "'hello'.toUpperCase()")
    with pytest.raises(JsExprError, match="unsupported call"):
        evaluate(ctx, "JSON.parse('[1]').push(2)")


def test_dict_attribute_access() -> None:
    ctx = _ctx()
    ctx.provide("holder", {"present": 7})
    assert evaluate(ctx, "ctx.holder.present") == 7
    assert evaluate(ctx, "ctx.holder.absent") is None


# --- Operators ---


def test_strict_equality_bool_before_number() -> None:
    ctx = _ctx()
    # JavaScript: true === 1 is false, unlike Python ==.
    assert evaluate(ctx, "true === 1") is False
    assert evaluate(ctx, "true !== 1") is True
    assert evaluate(ctx, "1 === 1") is True
    assert evaluate(ctx, "'a' === 'a'") is True
    assert evaluate(ctx, "'a' !== 'b'") is True
    assert evaluate(ctx, "null === undefined") is True
    assert evaluate(ctx, "null === null") is True
    assert evaluate(ctx, "ctx === ctx") is True
    assert evaluate(ctx, "ctx !== process") is True


def test_loose_equality() -> None:
    ctx = _ctx()
    assert evaluate(ctx, "true == 1") is True
    assert evaluate(ctx, "true == '1'") is True
    assert evaluate(ctx, "'42' == 42") is True
    assert evaluate(ctx, "42 == '42'") is True
    assert evaluate(ctx, "null == undefined") is True
    assert evaluate(ctx, "null == 0") is False
    assert evaluate(ctx, "0 == false") is True
    assert evaluate(ctx, "'a' == 'a'") is True
    assert evaluate(ctx, "'a' != 'b'") is True
    assert evaluate(ctx, "1 != 2") is True


def test_nan_never_equals() -> None:
    ctx = _ctx()
    assert evaluate(ctx, "Number(undefined) === Number(undefined)") is False
    assert evaluate(ctx, "Number(undefined) !== Number(undefined)") is True
    assert evaluate(ctx, "Number(undefined) == Number(undefined)") is False
    assert evaluate(ctx, "Number('nope') < 1") is False
    assert evaluate(ctx, "Number('nope') >= 1") is False


def test_relational_operators() -> None:
    ctx = _ctx()
    assert evaluate(ctx, "1 < 2") is True
    assert evaluate(ctx, "2 <= 2") is True
    assert evaluate(ctx, "3 > 2") is True
    assert evaluate(ctx, "3 >= 4") is False
    assert evaluate(ctx, "'a' < 'b'") is True
    assert evaluate(ctx, "'b' <= 'b'") is True
    assert evaluate(ctx, "'c' > 'b'") is True
    assert evaluate(ctx, "'a' >= 'b'") is False
    assert evaluate(ctx, "true < 2") is True
    assert evaluate(ctx, "ctx.get('missing') < 1") is False
    assert evaluate(ctx, "ctx.get('missing') >= 1") is False


def test_boolean_operators_return_operands() -> None:
    ctx = _ctx()
    assert evaluate(ctx, "'' || 'fallback'") == "fallback"
    assert evaluate(ctx, "0 || 'fallback'") == "fallback"
    assert evaluate(ctx, "false || 'fallback'") == "fallback"
    assert evaluate(ctx, "null || 'fallback'") == "fallback"
    assert evaluate(ctx, "'value' || 'fallback'") == "value"
    assert evaluate(ctx, "'a' && 'b'") == "b"
    assert evaluate(ctx, "'' && 'b'") == ""
    assert evaluate(ctx, "0 && 'b'") == 0
    assert evaluate(ctx, "false && 'b'") is False
    assert evaluate(ctx, "!''") is True
    assert evaluate(ctx, "!0") is True
    assert evaluate(ctx, "!'x'") is False
    assert evaluate(ctx, "!!'x'") is True
    # [] and {} are truthy in JavaScript.
    assert evaluate(ctx, "JSON.parse('[]') || 'fallback'") == []
    assert evaluate(ctx, "JSON.parse('{}') && 'yes'") == "yes"


def test_nullish_coalescing_is_nullish_only() -> None:
    ctx = _ctx()
    assert evaluate(ctx, "null ?? 'd'") == "d"
    assert evaluate(ctx, "undefined ?? 'd'") == "d"
    assert evaluate(ctx, "0 ?? 'd'") == 0
    assert evaluate(ctx, "'' ?? 'd'") == ""
    assert evaluate(ctx, "false ?? 'd'") is False
    assert evaluate(ctx, "'v' ?? 'd'") == "v"


def test_ternaries() -> None:
    ctx = _ctx()
    assert evaluate(ctx, "process.platform === 'win32' ? 'node.exe' : 'node'") in (
        "node.exe",
        "node",
    )
    assert evaluate(ctx, "true ? 1 : 2") == 1
    assert evaluate(ctx, "false ? 1 : 2") == 2
    assert evaluate(ctx, "1 < 2 ? 'yes' : 'no'") == "yes"
    assert evaluate(ctx, "true ? false ? 1 : 2 : 3") == 2
    assert evaluate(ctx, "false ? 1 : true ? 2 : 3") == 2
    assert evaluate(ctx, "ctx.get('x')?.y ? 'a' : 'b'") == "b"


def test_ternary_with_leading_dot_number() -> None:
    ctx = _ctx()
    # "a?.5:3" is the ternary with numeric literal .5, not optional chaining.
    assert evaluate(ctx, "false ?.5:3") == 3
    assert evaluate(ctx, "true ?.5:3") == 0.5


def test_precedence_and_parentheses() -> None:
    ctx = _ctx()
    assert evaluate(ctx, "(1 === 1) && (2 === 2)") is True
    assert evaluate(ctx, "true || false && false") is True
    assert evaluate(ctx, "(true || false) && false") is False
    assert evaluate(ctx, "1 < 2 === true") is True
    assert evaluate(ctx, "null ?? 'a' || 'b'") == "a"


# --- Attested call shapes ---


def test_number_coercion(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = _ctx()
    monkeypatch.setenv("ANIMA_JSEXPR_PORT", "3081")
    assert evaluate(ctx, "Number(process.env.ANIMA_JSEXPR_PORT ?? 8080)") == 3081
    assert evaluate(ctx, "Number(process.env.ANIMA_JSEXPR_MISSING ?? 8080)") == 8080
    assert evaluate(ctx, "Number('42')") == 42
    assert evaluate(ctx, "Number('3.5')") == 3.5
    assert evaluate(ctx, "Number(true)") == 1
    assert evaluate(ctx, "Number(false)") == 0
    assert evaluate(ctx, "Number('')") == 0
    assert evaluate(ctx, "Number('  12  ')") == 12
    assert evaluate(ctx, "Number('0x10')") == 16
    assert evaluate(ctx, "Number('-0x10')") == -16
    assert evaluate(ctx, "Number('Infinity')") == math.inf
    assert evaluate(ctx, "Number('-Infinity')") == -math.inf
    assert math.isnan(evaluate(ctx, "Number('nope')"))
    assert math.isnan(evaluate(ctx, "Number('NaN')"))
    assert evaluate(ctx, "Number('1e999')") == math.inf
    assert evaluate(ctx, "Number(undefined)")
    assert math.isnan(evaluate(ctx, "Number('1_0')"))
    assert math.isnan(evaluate(ctx, "Number('inf')"))
    assert math.isnan(evaluate(ctx, "Number(JSON.parse('{}'))"))


def test_string_coercion() -> None:
    ctx = _ctx()
    assert evaluate(ctx, "String(process.env.ANIMA_JSEXPR_MISSING ?? '')") == ""
    assert evaluate(ctx, "String(42)") == "42"
    assert evaluate(ctx, "String(3.5)") == "3.5"
    assert evaluate(ctx, "String(true)") == "true"
    assert evaluate(ctx, "String(false)") == "false"
    assert evaluate(ctx, "String(null)") == "undefined"
    assert evaluate(ctx, "String(undefined)") == "undefined"
    assert evaluate(ctx, "String(1e21)") == "1e+21"
    assert evaluate(ctx, "String(1e-7)") == "1e-7"
    assert evaluate(ctx, "String(1e309)") == "Infinity"
    assert _js_number_string(-math.inf) == "-Infinity"
    assert evaluate(ctx, "String(Number('nope'))") == "NaN"
    assert evaluate(ctx, "String(1e20)") == "100000000000000000000"
    assert evaluate(ctx, "String(JSON.parse('[1, 2]'))") == "1,2"
    assert evaluate(ctx, "String(JSON.parse('[null, 1]'))") == ",1"
    assert evaluate(ctx, "String(JSON.parse('{}'))") == "[object Object]"
    assert evaluate(ctx, "String(ctx)") == str(ctx)


def test_json_parse() -> None:
    ctx = _ctx()
    assert evaluate(ctx, "JSON.parse(process.env.ANIMA_JSEXPR_MISSING ?? '[]')") == []
    assert evaluate(ctx, "JSON.parse('[1, 2]')") == [1, 2]
    assert evaluate(ctx, "JSON.parse('{\"a\": 1}')") == {"a": 1}
    assert evaluate(ctx, "JSON.parse('\"x\"')") == "x"
    assert evaluate(ctx, "JSON.parse('42')") == 42
    assert evaluate(ctx, "JSON.parse('true')") is True
    assert evaluate(ctx, "JSON.parse('null')") is None
    with pytest.raises(JsExprError, match="JSON.parse failed"):
        evaluate(ctx, "JSON.parse('{bad')")
    with pytest.raises(JsExprError, match="JSON.parse failed"):
        evaluate(ctx, "JSON.parse(undefined)")
    with pytest.raises(JsExprError, match="JSON.parse failed"):
        evaluate(ctx, "JSON.parse('NaN')")
    with pytest.raises(JsExprError, match="JSON.parse failed"):
        evaluate(ctx, "JSON.parse('Infinity')")


def test_json_parse_identity_semantics() -> None:
    ctx = _ctx()
    # Distinct objects are not === even with equal content; the same
    # service value is.
    assert evaluate(ctx, "JSON.parse('{\"a\":1}') === JSON.parse('{\"a\":1}')") is False
    assert evaluate(ctx, "JSON.parse('{\"a\":1}') !== JSON.parse('{\"a\":1}')") is True
    ctx.provide("shared", {"a": 1})
    assert evaluate(ctx, "ctx.get('shared') === ctx.get('shared')") is True
    assert evaluate(ctx, "ctx.get('shared') == ctx.get('shared')") is True


def test_attested_compound_patterns(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = _ctx()
    monkeypatch.setenv("ANIMA_JSEXPR_MODE", "workspace-write")
    assert (
        evaluate(ctx, "process.env.ANIMA_JSEXPR_MODE ?? 'workspace-write'")
        == "workspace-write"
    )
    assert (
        evaluate(ctx, "process.env.ANIMA_JSEXPR_MISSING ?? process.cwd()")
        == os.getcwd()
    )
    assert (
        evaluate(
            ctx,
            "process.env.ANIMA_JSEXPR_MODE ?? 'workspace-write'",
        )
        == "workspace-write"
    )
    ctx.provide("profileContext", {"name": "desktop"})
    assert evaluate(ctx, "!ctx.get('profileContext')") is False
    assert evaluate(ctx, "ctx.get('profileContext')?.name !== 'desktop'") is False
    assert evaluate(ctx, "process.platform === 'win32' ? 'a' : 'b'") == (
        "a" if sys.platform == "win32" else "b"
    )


def test_call_arity_errors() -> None:
    ctx = _ctx()
    with pytest.raises(JsExprError, match="expected 1 argument"):
        evaluate(ctx, "String()")
    with pytest.raises(JsExprError, match="expected 1 argument"):
        evaluate(ctx, "String('a', 'b')")
    with pytest.raises(JsExprError, match="expected 1 argument"):
        evaluate(ctx, "Number()")
    with pytest.raises(JsExprError, match="expected 1 argument"):
        evaluate(ctx, "JSON.parse()")
    with pytest.raises(JsExprError, match="expected 0 argument"):
        evaluate(ctx, "process.cwd('/x')")
    with pytest.raises(JsExprError, match="expected 0 argument"):
        evaluate(ctx, "process.execPath('/x')")


def test_trailing_comma_in_call() -> None:
    ctx = _ctx()
    assert evaluate(ctx, "Number('42',)") == 42


# --- Fail loud: out of scope ---


def test_out_of_scope_calls_fail_loud() -> None:
    ctx = _ctx()
    with pytest.raises(JsExprError, match="unsupported call"):
        evaluate(ctx, "homePath('sessions')")
    with pytest.raises(JsExprError, match="unsupported call"):
        evaluate(ctx, "process.getBuiltinModule('node:path').join('a', 'b')")
    with pytest.raises(JsExprError, match="unsupported call"):
        evaluate(ctx, "process.getBuiltinModule('node:path')")
    with pytest.raises(JsExprError, match="unsupported call"):
        evaluate(ctx, "foo('x')")
    with pytest.raises(JsExprError, match="unsupported call"):
        evaluate(ctx, "ctx.get('x').doThing()")
    # An optional chain on a nullish callee short-circuits to undefined.
    assert evaluate(ctx, "ctx.get('x')?.()") is None
    assert evaluate(ctx, "ctx.get('x')?.doThing()") is None
    # The same chain on a present value reaches the call and fails loud.
    ctx.provide("holder", {"name": "h"})
    with pytest.raises(JsExprError, match="unsupported optional call"):
        evaluate(ctx, "ctx.get('holder')?.()")
    with pytest.raises(JsExprError, match="unsupported call"):
        evaluate(ctx, "ctx.get('holder')?.doThing()")


def test_out_of_scope_syntax_fails_loud() -> None:
    ctx = _ctx()
    with pytest.raises(JsExprError, match="template literals"):
        evaluate(ctx, "`hello ${name}`")
    with pytest.raises(JsExprError, match="unexpected character"):
        evaluate(ctx, "1 + 1")
    with pytest.raises(JsExprError, match="unexpected character"):
        evaluate(ctx, "a - b")
    with pytest.raises(JsExprError, match="unexpected character"):
        evaluate(ctx, "a * b")
    with pytest.raises(JsExprError, match="unexpected character"):
        evaluate(ctx, "a / b")
    with pytest.raises(JsExprError, match="unexpected character"):
        evaluate(ctx, "a % b")
    with pytest.raises(JsExprError, match="unexpected character"):
        evaluate(ctx, "a = 1")
    with pytest.raises(JsExprError, match="unexpected character"):
        evaluate(ctx, "a; b")
    with pytest.raises(JsExprError, match="unexpected character"):
        evaluate(ctx, "a[0]")
    with pytest.raises(JsExprError, match="unexpected character"):
        evaluate(ctx, "a & b")
    with pytest.raises(JsExprError, match="unexpected character"):
        evaluate(ctx, "a | b")
    with pytest.raises(JsExprError, match="expected"):
        evaluate(ctx, "(1")
    with pytest.raises(JsExprError, match="unexpected token"):
        evaluate(ctx, "1 2")
    with pytest.raises(JsExprError, match="expected"):
        evaluate(ctx, "true ? 1")
    with pytest.raises(JsExprError, match="unexpected token"):
        evaluate(ctx, "()")
    with pytest.raises(JsExprError, match="expected identifier"):
        evaluate(ctx, "ctx..x")
    with pytest.raises(JsExprError, match="expected identifier"):
        evaluate(ctx, "ctx?.")
    with pytest.raises(JsExprError, match="expected"):
        evaluate(ctx, "String('x'")


def test_eval_unknown_node_kind() -> None:
    ctx = _ctx()
    with pytest.raises(JsExprError, match="unsupported expression"):
        _eval(("bogus",), ctx, "bogus")


# --- interpolate ---


def test_interpolate_replaces_nodes_and_preserves_input() -> None:
    ctx = _ctx()
    ctx.provide("profileContext", {"name": "desktop"})
    value = {
        "mode": {"__jsExpr": "process.env.ANIMA_JSEXPR_MISSING ?? 'workspace-write'"},
        "ports": [{"__jsExpr": "Number('3081')"}, 8080],
        "nested": {"flag": {"__jsExpr": "!ctx.get('profileContext')"}},
        "plain": "keep",
        "num": 3,
    }
    result = interpolate(ctx, value)
    assert result == {
        "mode": "workspace-write",
        "ports": [3081, 8080],
        "nested": {"flag": False},
        "plain": "keep",
        "num": 3,
    }
    # The input is never mutated: the raw nodes stay for write-back.
    assert value["mode"] == {
        "__jsExpr": "process.env.ANIMA_JSEXPR_MISSING ?? 'workspace-write'"
    }
    assert value["ports"][0] == {"__jsExpr": "Number('3081')"}
    assert result is not value
    assert result["ports"] is not value["ports"]
    assert result["nested"] is not value["nested"]


def test_interpolate_scalars_pass_through() -> None:
    ctx = _ctx()
    assert interpolate(ctx, 42) == 42
    assert interpolate(ctx, "text") == "text"
    assert interpolate(ctx, None) is None
    assert interpolate(ctx, True) is True
    assert interpolate(ctx, []) == []
    assert interpolate(ctx, {}) == {}


def test_interpolate_out_of_scope_fails_loud() -> None:
    ctx = _ctx()
    with pytest.raises(JsExprError, match="unsupported call"):
        interpolate(ctx, {"__jsExpr": "homePath('sessions')"})


# --- !!js YAML dialect ---


def test_yaml_tag_loads_to_expression_node() -> None:
    text = "entries:\n  - name: comp\n    config:\n      mode: !!js process.env.MODE ?? 'x'\n"
    data = load_yaml(io.StringIO(text))
    assert data == {
        "entries": [
            {
                "name": "comp",
                "config": {"mode": {"__jsExpr": "process.env.MODE ?? 'x'"}},
            }
        ]
    }


def test_yaml_tag_quoted_scalar() -> None:
    text = "value: !!js \"process.env.X ?? 'a: b'\"\n"
    data = load_yaml(io.StringIO(text))
    assert data == {"value": {"__jsExpr": "process.env.X ?? 'a: b'"}}


def test_yaml_round_trip_preserves_expression_nodes() -> None:
    original = [
        {
            "id": "a",
            "name": "comp",
            "config": {
                "mode": {"__jsExpr": "process.env.MODE ?? 'workspace-write'"},
                "port": {"__jsExpr": "Number(process.env.PORT ?? 8080)"},
                "flag": {"__jsExpr": "!ctx.get('profileContext')"},
            },
            "disabled": {"__jsExpr": "ctx.disabled"},
        }
    ]
    buf = io.StringIO()
    dump_yaml(original, buf)
    dumped = buf.getvalue()
    assert "!!js" in dumped
    assert "process.env.MODE" in dumped
    reloaded = load_yaml(io.StringIO(dumped))
    assert reloaded == original
    # A second round-trip is stable.
    buf2 = io.StringIO()
    dump_yaml(reloaded, buf2)
    assert load_yaml(io.StringIO(buf2.getvalue())) == original


def test_yaml_dump_keeps_multi_key_dict_a_mapping() -> None:
    data = [{"config": {"__jsExpr": "process.env.X", "other": 1}}]
    buf = io.StringIO()
    dump_yaml(data, buf)
    # Only single-key {"__jsExpr": str} dicts become !!js scalars.
    assert "!!js" not in buf.getvalue()
    assert load_yaml(io.StringIO(buf.getvalue())) == data


def test_yaml_dump_plain_entries_unchanged() -> None:
    data = [{"id": "a", "name": "comp", "config": {"port": 8080}}]
    buf = io.StringIO()
    dump_yaml(data, buf)
    assert load_yaml(io.StringIO(buf.getvalue())) == data
    assert "!!js" not in buf.getvalue()


def test_yaml_js_tag_on_sequence_fails() -> None:
    with pytest.raises(yaml.YAMLError):
        load_yaml(io.StringIO("value: !!js [1, 2]\n"))


def test_js_number_and_string_helpers() -> None:
    # Direct unit coverage of the coercion helpers' remaining branches.
    assert _js_number(3.0) == 3
    assert _js_number(3.5) == 3.5
    assert math.isnan(_js_number(math.nan))
    assert _js_number(math.inf) == math.inf
    assert math.isnan(_js_number("0x"))
    assert _js_number("  ") == 0
    assert _js_number("+0x1F") == 31
    assert math.isnan(_js_number([]))
    assert _js_string(0) == "0"
    assert _js_string(-1.5) == "-1.5"
    assert _js_string(1e21) == "1e+21"
    assert _js_string((1, 2)) == "1,2"
    assert _js_string(object()).startswith("<object object")


def test_strict_and_loose_equal_edge_branches() -> None:
    ctx = _ctx()
    assert evaluate(ctx, "1 === '1'") is False
    assert evaluate(ctx, "null === 0") is False
    assert evaluate(ctx, "0 === false") is False
    assert evaluate(ctx, "'a' === 1") is False
    assert evaluate(ctx, "1 == true") is True
    assert evaluate(ctx, "'1' == true") is True
    assert evaluate(ctx, "null == null") is True
    assert evaluate(ctx, "undefined == null") is True
    assert evaluate(ctx, "Number('x') == Number('x')") is False
    assert evaluate(ctx, "Number('x') != Number('x')") is True
