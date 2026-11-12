"""Restricted JavaScript expression evaluation for loader config values.

Configuration values may be expressions (``!!js`` tag in YAML, stored as
``{"__jsExpr": ...}``), evaluated against the loader context at entry
activation. The full language is evaluated upstream with JavaScript
``eval``; this module ports a restricted evaluator covering the attested
patterns only, per ADR 0006: environment access (``process.env.*``),
loader-context access (``ctx.*``, ``ctx.get(...)``), the process
accessors (``process.platform``, ``process.cwd()``, ``process.execPath``),
the ``String(...)`` / ``Number(...)`` / ``JSON.parse(...)`` call shapes,
literals, comparisons, ternaries, boolean operators, nullish coalescing,
and optional chaining. Anything outside that set fails loud with
``JsExprError`` at load time instead of being silently evaluated or
silently skipped. No JavaScript engine is embedded and no new
dependency is introduced.

This module also owns the ``!!js`` YAML dialect (one place): ``load_yaml``
and ``dump_yaml`` are the load/dump paths every profile file goes
through, so expression nodes round-trip as ``!!js`` scalars.
"""

from __future__ import annotations

import json
import math
import os
import re
import sys
from dataclasses import dataclass
from typing import Any

import yaml

from anima.core import Context, Fiber, is_js_expr

__all__ = ["JsExprError", "dump_yaml", "evaluate", "interpolate", "load_yaml"]

JSEXPR_TAG = "tag:yaml.org,2002:js"

_NAN = float("nan")
# Internal sentinel: an optional chain hit a nullish base, so the rest of
# the chain is skipped. Normalised to None at expression boundaries.
_SHORT_CIRCUIT: Any = object()

# AST node: (kind, ...) with kinds lit, name, not, cond, bin, attr, call.
_Node = tuple[Any, ...]


class JsExprError(ValueError):
    """An expression is outside the restricted evaluator scope, or failed to evaluate."""


class _ProcessNamespace:
    """Pure-accessor view of the host process exposed as ``process``."""

    @property
    def env(self) -> dict[str, str]:
        return dict(os.environ)

    @property
    def platform(self) -> str:
        if sys.platform == "win32":
            return "win32"
        if sys.platform == "darwin":
            return "darwin"
        if sys.platform.startswith("linux"):
            return "linux"
        return sys.platform

    def cwd(self) -> str:
        return os.getcwd()

    def execPath(self) -> str:  # JavaScript spelling
        return sys.executable


_PROCESS = _ProcessNamespace()


# --- Tokenizer ---


@dataclass(frozen=True)
class _Token:
    kind: str  # "num" | "str" | "ident" | "op" | "eof"
    value: Any
    pos: int


_NUMBER_RE = re.compile(r"(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")
_IDENT_RE = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*")
_DECIMAL_RE = re.compile(r"^[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?$")
_HEX_RE = re.compile(r"^[+-]?0[xX][0-9a-fA-F]+$")
_STRING_ESCAPES = {
    "n": "\n",
    "r": "\r",
    "t": "\t",
    "b": "\b",
    "f": "\f",
    "v": "\v",
    "0": "\0",
    "'": "'",
    '"': '"',
    "\\": "\\",
}


def _scan_string(expr: str, start: int) -> tuple[str, int]:
    """Scan a quoted string starting at ``start``; return (value, end index)."""
    quote = expr[start]
    i = start + 1
    out: list[str] = []
    while i < len(expr):
        ch = expr[i]
        if ch == quote:
            return "".join(out), i + 1
        if ch == "\n":
            raise JsExprError(f"unterminated string literal in expression {expr!r}")
        if ch == "\\":
            if i + 1 >= len(expr):
                raise JsExprError(f"unterminated string literal in expression {expr!r}")
            esc = expr[i + 1]
            if esc in _STRING_ESCAPES:
                out.append(_STRING_ESCAPES[esc])
                i += 2
                continue
            if esc in ("x", "u"):
                width = 2 if esc == "x" else 4
                hexpart = expr[i + 2 : i + 2 + width]
                if len(hexpart) != width or not all(
                    c in "0123456789abcdefABCDEF" for c in hexpart
                ):
                    raise JsExprError(
                        f"invalid escape in string literal in expression {expr!r}"
                    )
                out.append(chr(int(hexpart, 16)))
                i += 2 + width
                continue
            # Unknown escape: JavaScript keeps the escaped character.
            out.append(esc)
            i += 2
            continue
        out.append(ch)
        i += 1
    raise JsExprError(f"unterminated string literal in expression {expr!r}")


def _tokenize(expr: str) -> list[_Token]:
    """Split an expression into tokens; anything unrecognised fails loud."""
    tokens: list[_Token] = []
    i = 0
    n = len(expr)
    while i < n:
        ch = expr[i]
        if ch.isspace():
            i += 1
            continue
        if ch.isdigit() or (ch == "." and i + 1 < n and expr[i + 1].isdigit()):
            match = _NUMBER_RE.match(expr, i)
            # The guard above guarantees a match.
            assert match is not None
            text = match.group(0)
            if "." in text or "e" in text or "E" in text:
                value: Any = float(text)
            else:
                value = int(text)
            tokens.append(_Token("num", value, i))
            i = match.end()
            continue
        if ch in ("'", '"'):
            value, i = _scan_string(expr, i)
            tokens.append(_Token("str", value, i))
            continue
        if ch == "`":
            raise JsExprError(
                "template literals are outside the restricted evaluator "
                f"scope in expression {expr!r}"
            )
        match = _IDENT_RE.match(expr, i)
        if match is not None:
            tokens.append(_Token("ident", match.group(0), i))
            i = match.end()
            continue
        three = expr[i : i + 3]
        if three in ("===", "!=="):
            tokens.append(_Token("op", three, i))
            i += 3
            continue
        two = expr[i : i + 2]
        if two in ("==", "!=", "<=", ">=", "&&", "||", "??"):
            tokens.append(_Token("op", two, i))
            i += 2
            continue
        if two == "?." and (i + 2 >= n or not expr[i + 2].isdigit()):
            # "?." starts optional chaining unless followed by a digit:
            # "a?.5:3" is the ternary with numeric literal .5.
            tokens.append(_Token("op", "?.", i))
            i += 2
            continue
        if ch in "?:.,()<>!":
            tokens.append(_Token("op", ch, i))
            i += 1
            continue
        raise JsExprError(f"unexpected character {ch!r} in expression {expr!r}")
    tokens.append(_Token("eof", None, n))
    return tokens


# --- Parser (recursive descent; precedence: ternary > ?? > || > && >
# equality > relational > unary (!) > postfix > primary) ---


class _Parser:
    def __init__(self, tokens: list[_Token], expr: str) -> None:
        self._tokens = tokens
        self._pos = 0
        self._expr = expr

    def parse(self) -> _Node:
        if self._tokens[0].kind == "eof":
            raise JsExprError(f"empty expression {self._expr!r}")
        node = self._parse_ternary()
        if self._peek().kind != "eof":
            raise JsExprError(
                f"unexpected token {self._peek().value!r} in expression {self._expr!r}"
            )
        return node

    def _peek(self) -> _Token:
        return self._tokens[self._pos]

    def _next(self) -> _Token:
        tok = self._tokens[self._pos]
        self._pos += 1
        return tok

    def _match_op(self, *ops: str) -> str | None:
        tok = self._peek()
        if tok.kind == "op" and tok.value in ops:
            self._pos += 1
            return str(tok.value)
        return None

    def _expect_op(self, op: str) -> None:
        if self._match_op(op) is None:
            raise JsExprError(f"expected {op!r} in expression {self._expr!r}")

    def _expect_ident(self) -> str:
        tok = self._next()
        if tok.kind != "ident":
            raise JsExprError(f"expected identifier in expression {self._expr!r}")
        return str(tok.value)

    def _parse_ternary(self) -> _Node:
        cond = self._parse_nullish()
        if self._match_op("?") is not None:
            then = self._parse_ternary()
            self._expect_op(":")
            otherwise = self._parse_ternary()
            return ("cond", cond, then, otherwise)
        return cond

    def _parse_nullish(self) -> _Node:
        node = self._parse_or()
        while self._match_op("??") is not None:
            node = ("bin", "??", node, self._parse_or())
        return node

    def _parse_or(self) -> _Node:
        node = self._parse_and()
        while self._match_op("||") is not None:
            node = ("bin", "||", node, self._parse_and())
        return node

    def _parse_and(self) -> _Node:
        node = self._parse_equality()
        while self._match_op("&&") is not None:
            node = ("bin", "&&", node, self._parse_equality())
        return node

    def _parse_equality(self) -> _Node:
        node = self._parse_relational()
        while True:
            op = self._match_op("===", "!==", "==", "!=")
            if op is None:
                return node
            node = ("bin", op, node, self._parse_relational())

    def _parse_relational(self) -> _Node:
        node = self._parse_unary()
        while True:
            op = self._match_op("<=", ">=", "<", ">")
            if op is None:
                return node
            node = ("bin", op, node, self._parse_unary())

    def _parse_unary(self) -> _Node:
        if self._match_op("!") is not None:
            return ("not", self._parse_unary())
        return self._parse_postfix()

    def _parse_postfix(self) -> _Node:
        node = self._parse_primary()
        while True:
            if self._match_op(".") is not None:
                node = ("attr", node, self._expect_ident(), False)
                continue
            if self._match_op("?.") is not None:
                if self._peek().kind == "op" and self._peek().value == "(":
                    self._next()
                    node = ("call", node, self._parse_args(), True)
                    continue
                node = ("attr", node, self._expect_ident(), True)
                continue
            if self._peek().kind == "op" and self._peek().value == "(":
                self._next()
                node = ("call", node, self._parse_args(), False)
                continue
            return node

    def _parse_args(self) -> list[_Node]:
        args: list[_Node] = []
        if self._peek().kind == "op" and self._peek().value == ")":
            self._next()
            return args
        while True:
            args.append(self._parse_ternary())
            if self._match_op(",") is not None:
                if self._peek().kind == "op" and self._peek().value == ")":
                    self._next()
                    return args
                continue
            self._expect_op(")")
            return args

    def _parse_primary(self) -> _Node:
        tok = self._next()
        if tok.kind in ("num", "str"):
            return ("lit", tok.value)
        if tok.kind == "ident":
            if tok.value == "true":
                return ("lit", True)
            if tok.value == "false":
                return ("lit", False)
            if tok.value in ("null", "undefined"):
                return ("lit", None)
            return ("name", tok.value)
        if tok.kind == "op" and tok.value == "(":
            node = self._parse_ternary()
            self._expect_op(")")
            return node
        raise JsExprError(
            f"unexpected token {tok.value!r} in expression {self._expr!r}"
        )


# --- JavaScript value semantics ---


def _is_nan(value: Any) -> bool:
    return isinstance(value, float) and math.isnan(value)


def _js_truthy(value: Any) -> bool:
    """JavaScript truthiness: false, 0, "", null/undefined and NaN are falsy."""
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0 and not _is_nan(value)
    if isinstance(value, str):
        return len(value) > 0
    return True


def _js_number_string(value: float) -> str:
    """Format a number the way JavaScript String(number) does."""
    if isinstance(value, float) and math.isnan(value):
        return "NaN"
    if isinstance(value, float) and math.isinf(value):
        return "Infinity" if value > 0 else "-Infinity"
    if isinstance(value, float) and value.is_integer() and abs(value) < 1e21:
        return str(int(value))
    if isinstance(value, int):
        return str(value)
    text = repr(float(value))
    if "e" in text:
        mantissa, exp = text.split("e")
        return f"{mantissa}e{int(exp):+d}"
    return text


def _js_string(value: Any) -> str:
    """JavaScript String() coercion (undefined becomes "undefined")."""
    if value is None:
        return "undefined"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return _js_number_string(value)
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)):
        return ",".join("" if item is None else _js_string(item) for item in value)
    if isinstance(value, dict):
        return "[object Object]"
    return str(value)


def _js_number(value: Any) -> Any:
    """JavaScript Number() coercion; unparseable input becomes NaN."""
    if value is None:
        return _NAN
    if isinstance(value, bool):
        return 1 if value else 0
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return value
        return int(value) if value.is_integer() else value
    if isinstance(value, str):
        text = value.strip()
        if text == "":
            return 0
        if text in ("Infinity", "+Infinity"):
            return math.inf
        if text == "-Infinity":
            return -math.inf
        if text == "NaN":
            return _NAN
        if _HEX_RE.match(text):
            # The regex already guarantees a valid hex literal.
            return int(text, 16)
        if _DECIMAL_RE.match(text):
            num = float(text)
            if math.isnan(num) or math.isinf(num):
                return num
            return int(num) if num.is_integer() else num
        return _NAN
    return _NAN


def _reject_json_constant(name: str) -> Any:
    raise ValueError(f"invalid JSON constant {name}")


def _js_json_parse(value: Any, expr: str) -> Any:
    """JavaScript JSON.parse(); a parse failure fails loud."""
    text = value if isinstance(value, str) else _js_string(value)
    try:
        return json.loads(text, parse_constant=_reject_json_constant)
    except ValueError as exc:
        raise JsExprError(f"JSON.parse failed in expression {expr!r}: {exc}") from exc


def _js_strict_equal(left: Any, right: Any) -> bool:
    """JavaScript ===: bool is never equal to a number; NaN is never equal."""
    if isinstance(left, bool) or isinstance(right, bool):
        return isinstance(left, bool) and isinstance(right, bool) and left == right
    if left is None or right is None:
        return left is None and right is None
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        if _is_nan(left) or _is_nan(right):
            return False
        return left == right
    if isinstance(left, str) and isinstance(right, str):
        return left == right
    return left is right


def _js_loose_equal(left: Any, right: Any) -> bool:
    """JavaScript == over the primitive shapes the evaluator produces."""
    if left is None or right is None:
        return left is None and right is None
    if isinstance(left, bool) or isinstance(right, bool):
        return _js_loose_equal(
            _js_number(left) if isinstance(left, bool) else left,
            _js_number(right) if isinstance(right, bool) else right,
        )
    if isinstance(left, (int, float)) and isinstance(right, str):
        return _js_loose_equal(left, _js_number(right))
    if isinstance(left, str) and isinstance(right, (int, float)):
        return _js_loose_equal(_js_number(left), right)
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        if _is_nan(left) or _is_nan(right):
            return False
        return left == right
    if isinstance(left, str) and isinstance(right, str):
        return left == right
    return left is right


def _js_compare(op: str, left: Any, right: Any) -> bool:
    """JavaScript relational comparison; any NaN operand makes it false."""
    if isinstance(left, str) and isinstance(right, str):
        if op == "<":
            return left < right
        if op == "<=":
            return left <= right
        if op == ">":
            return left > right
        return left >= right
    num_left = _js_number(left)
    num_right = _js_number(right)
    if _is_nan(num_left) or _is_nan(num_right):
        return False
    if op == "<":
        return bool(num_left < num_right)
    if op == "<=":
        return bool(num_left <= num_right)
    if op == ">":
        return bool(num_left > num_right)
    return bool(num_left >= num_right)


def _get_attr(value: Any, name: str, expr: str) -> Any:
    """Read a property: dict key, string/list length, or safe attribute.

    A missing key or attribute is undefined (None), matching JavaScript
    property access on an existing object. The process namespace answers
    its pure accessors as values, so bare ``process.cwd`` and
    ``process.execPath`` read the same as their call forms.
    """
    if isinstance(value, dict):
        return value.get(name)
    if isinstance(value, _ProcessNamespace):
        if name == "env":
            return value.env
        if name == "platform":
            return value.platform
        if name == "cwd":
            return value.cwd()
        if name == "execPath":
            return value.execPath()
        raise JsExprError(f"unsupported process member {name!r} in expression {expr!r}")
    if isinstance(value, str):
        if name == "length":
            return len(value)
        raise JsExprError(f"unsupported string member {name!r} in expression {expr!r}")
    if isinstance(value, (list, tuple)):
        if name == "length":
            return len(value)
        raise JsExprError(f"unsupported list member {name!r} in expression {expr!r}")
    return Fiber._safe_getattr(value, name)


# --- Evaluation ---


def _eval_norm(node: _Node, ctx: Context, expr: str) -> Any:
    """Evaluate a node, normalising an optional-chain short-circuit to None."""
    result = _eval(node, ctx, expr)
    return None if result is _SHORT_CIRCUIT else result


def _eval_bin(node: _Node, ctx: Context, expr: str) -> Any:
    op = node[1]
    if op == "&&":
        left = _eval_norm(node[2], ctx, expr)
        if not _js_truthy(left):
            return left
        return _eval_norm(node[3], ctx, expr)
    if op == "||":
        left = _eval_norm(node[2], ctx, expr)
        if _js_truthy(left):
            return left
        return _eval_norm(node[3], ctx, expr)
    if op == "??":
        left = _eval_norm(node[2], ctx, expr)
        if left is not None:
            return left
        return _eval_norm(node[3], ctx, expr)
    left = _eval_norm(node[2], ctx, expr)
    right = _eval_norm(node[3], ctx, expr)
    if op == "===":
        return _js_strict_equal(left, right)
    if op == "!==":
        return not _js_strict_equal(left, right)
    if op == "==":
        return _js_loose_equal(left, right)
    if op == "!=":
        return not _js_loose_equal(left, right)
    return _js_compare(op, left, right)


def _check_arity(args: list[Any], expected: int, expr: str) -> None:
    if len(args) != expected:
        raise JsExprError(f"expected {expected} argument(s) in expression {expr!r}")


def _eval_call(node: _Node, ctx: Context, expr: str) -> Any:
    func = node[1]
    if func[0] == "attr" and func[3]:
        # Optional member callee: a nullish base short-circuits the call.
        base = _eval(func[1], ctx, expr)
        if base is _SHORT_CIRCUIT or base is None:
            return _SHORT_CIRCUIT
    if node[3]:
        # Optional call "?.(...)": only a nullish callee short-circuits.
        func_value = _eval(func, ctx, expr)
        if func_value is _SHORT_CIRCUIT or func_value is None:
            return _SHORT_CIRCUIT
        raise JsExprError(f"unsupported optional call in expression {expr!r}")
    args = [_eval_norm(arg, ctx, expr) for arg in node[2]]
    if func[0] == "name" and func[1] == "String":
        _check_arity(args, 1, expr)
        return _js_string(args[0])
    if func[0] == "name" and func[1] == "Number":
        _check_arity(args, 1, expr)
        return _js_number(args[0])
    if (
        func[0] == "attr"
        and func[1][0] == "name"
        and func[1][1] == "JSON"
        and func[2] == "parse"
    ):
        _check_arity(args, 1, expr)
        return _js_json_parse(args[0], expr)
    if (
        func[0] == "attr"
        and func[1][0] == "name"
        and func[1][1] == "process"
        and func[2] in ("cwd", "execPath")
    ):
        _check_arity(args, 0, expr)
        return os.getcwd() if func[2] == "cwd" else sys.executable
    if (
        func[0] == "attr"
        and func[1][0] == "name"
        and func[1][1] == "ctx"
        and func[2] == "get"
    ):
        if not 1 <= len(args) <= 2:
            raise JsExprError(
                f"ctx.get expects 1 or 2 arguments in expression {expr!r}"
            )
        if not isinstance(args[0], str):
            raise JsExprError(f"ctx.get expects a string name in expression {expr!r}")
        default = args[1] if len(args) == 2 else None
        return ctx.get(args[0], default, strict=False)
    raise JsExprError(f"unsupported call in expression {expr!r}")


def _eval(node: _Node, ctx: Context, expr: str) -> Any:
    kind = node[0]
    if kind == "lit":
        return node[1]
    if kind == "name":
        if node[1] == "ctx":
            return ctx
        if node[1] == "process":
            return _PROCESS
        raise JsExprError(f"unknown identifier {node[1]!r} in expression {expr!r}")
    if kind == "not":
        return not _js_truthy(_eval_norm(node[1], ctx, expr))
    if kind == "cond":
        branch = node[2] if _js_truthy(_eval_norm(node[1], ctx, expr)) else node[3]
        return _eval(branch, ctx, expr)
    if kind == "bin":
        return _eval_bin(node, ctx, expr)
    if kind == "attr":
        base = _eval(node[1], ctx, expr)
        if base is _SHORT_CIRCUIT:
            return _SHORT_CIRCUIT
        if base is None:
            if node[3]:
                return _SHORT_CIRCUIT
            raise JsExprError(
                f"cannot read property {node[2]!r} of undefined in expression {expr!r}"
            )
        return _get_attr(base, node[2], expr)
    if kind == "call":
        return _eval_call(node, ctx, expr)
    raise JsExprError(f"unsupported expression in {expr!r}")


def evaluate(ctx: Context, expr: str) -> Any:
    """Evaluate a restricted JavaScript expression against a loader context.

    Only the attested patterns evaluate; anything else raises
    :class:`JsExprError`. ``undefined`` becomes ``None``.
    """
    if not isinstance(expr, str):
        raise JsExprError(f"expression must be a string, got {type(expr).__name__}")
    node = _Parser(_tokenize(expr), expr).parse()
    result = _eval(node, ctx, expr)
    return None if result is _SHORT_CIRCUIT else result


def interpolate(ctx: Context, value: Any) -> Any:
    """Recursively replace expression nodes with evaluated values.

    Returns new dicts/lists; the input is never mutated, so the raw
    ``{"__jsExpr": ...}`` nodes stay in the stored options for write-back.
    """
    if is_js_expr(value):
        return evaluate(ctx, value["__jsExpr"])
    if isinstance(value, dict):
        return {key: interpolate(ctx, item) for key, item in value.items()}
    if isinstance(value, list):
        return [interpolate(ctx, item) for item in value]
    return value


# --- !!js YAML dialect ---


def _construct_js_expr(
    loader: yaml.SafeLoader, node: yaml.ScalarNode
) -> dict[str, str]:
    return {"__jsExpr": loader.construct_scalar(node)}


class _JsExprLoader(yaml.SafeLoader):
    """YAML loader for the entry-list dialect: ``!!js`` scalars become expression nodes."""


_JsExprLoader.add_constructor(JSEXPR_TAG, _construct_js_expr)


def _represent_dict(dumper: yaml.SafeDumper, data: dict[str, Any]) -> yaml.Node:
    if len(data) == 1 and isinstance(data.get("__jsExpr"), str):
        return dumper.represent_scalar(JSEXPR_TAG, data["__jsExpr"])
    return dumper.represent_mapping(
        yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, data
    )


class _JsExprDumper(yaml.SafeDumper):
    """YAML dumper for the entry-list dialect: expression nodes become ``!!js`` scalars."""


_JsExprDumper.add_representer(dict, _represent_dict)


def load_yaml(stream: Any) -> Any:
    """Load YAML in the entry-list dialect (``!!js`` tags become expression nodes)."""
    return yaml.load(stream, Loader=_JsExprLoader)


def dump_yaml(data: Any, stream: Any) -> None:
    """Dump YAML in the entry-list dialect (expression nodes become ``!!js`` scalars)."""
    yaml.dump(data, stream, Dumper=_JsExprDumper, sort_keys=False)
