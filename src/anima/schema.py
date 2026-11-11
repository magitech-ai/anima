"""Declarative configuration schema AST and validation engine for Anima.

Ports the Config-used subset of the upstream schema library: the
``~standard`` seam, volatile references, ``natural``/``step``, ``pattern``,
object/union error semantics, required-before-default ordering, ``const``
via deep equality, ``role``/``loose``/``extra``/``never``/``transform``,
and schema JSON round-trip. Unported constructs fail loud: the builder
API has no factory for them (``AttributeError`` at definition time) and
the resolver raises ``unsupported type`` for unknown type names.
"""

from __future__ import annotations

import builtins
import copy
import itertools
import json
import math
import re
from typing import Any

from anima.volatile import (
    create_volatile,
    is_volatile,
)

_UNDEFINED = object()

_SUPPORTED_TYPES = frozenset(
    {
        "string",
        "number",
        "integer",
        "boolean",
        "object",
        "list",
        "dict",
        "union",
        "const",
        "any",
        "never",
        "transform",
    }
)

_UID_COUNTER = itertools.count(1)


# ---------------------------------------------------------------------------
# JavaScript value formatting (error messages use JS coercion, not Python's)
# ---------------------------------------------------------------------------


def _js_num_str(value: float) -> str:
    """Format a number the way JavaScript's Number.prototype.toString does."""
    if isinstance(value, int):
        return str(value)
    if math.isnan(value):
        return "NaN"
    if math.isinf(value):
        return "Infinity" if value > 0 else "-Infinity"
    if value == 0:
        return "0"
    if float(value).is_integer() and abs(value) < 1e21:
        return str(int(value))
    text = repr(value)
    if "e" not in text and "E" not in text:
        return text
    mantissa, _, exp_text = text.partition("e")
    exponent = int(exp_text)
    sign = ""
    if mantissa.startswith("-"):
        sign = "-"
        mantissa = mantissa[1:]
    digits = mantissa.replace(".", "")
    # Decimal point position: value = digits * 10**(point - len(digits)).
    # JS renders fixed notation for -6 <= exponent <= 20, exponential
    # otherwise. A positive point cannot reach this branch: an e-notation
    # float with a positive point is integral and was returned above.
    point = exponent + 1
    if -5 <= point <= 0:
        return f"{sign}0.{'0' * (-point)}{digits}"
    if len(digits) == 1:
        return f"{sign}{digits}e{exponent:+d}"
    return f"{sign}{digits[0]}.{digits[1:]}e{exponent:+d}"


def _js_str(value: Any, _seen: set[int] | None = None) -> str:
    """Coerce a value to a string the way a JS template literal does."""
    if value is _UNDEFINED:
        return "undefined"
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, (int, float)):
        return _js_num_str(value)
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)):
        seen = set() if _seen is None else _seen
        if id(value) in seen:
            return ""
        seen.add(id(value))
        try:
            return ",".join(_js_str(item, seen) for item in value)
        finally:
            seen.discard(id(value))
    if isinstance(value, dict):
        return "[object Object]"
    return str(value)


def _js_json(value: Any, _seen: set[int] | None = None) -> str:
    """Serialize a value the way JSON.stringify does.

    NaN/Infinity become ``null``; undefined/functions become ``undefined``
    (top level) or are skipped (object values) / ``null`` (array items).
    Circular input raises TypeError.
    """
    if value is _UNDEFINED or callable(value):
        return "undefined"
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, (int, float)):
        if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
            return "null"
        return _js_num_str(value)
    if isinstance(value, str):
        return json.dumps(value)
    seen = set() if _seen is None else _seen
    if id(value) in seen:
        raise TypeError("Converting circular structure to JSON")
    if isinstance(value, (list, tuple)):
        seen.add(id(value))
        try:
            parts = []
            for item in value:
                rendered = _js_json(item, seen)
                parts.append("null" if rendered == "undefined" else rendered)
            return "[" + ",".join(parts) + "]"
        finally:
            seen.discard(id(value))
    if isinstance(value, dict):
        seen.add(id(value))
        try:
            parts = []
            for key, item in value.items():
                rendered = _js_json(item, seen)
                if rendered == "undefined":
                    continue
                parts.append(f"{json.dumps(str(key))}:{rendered}")
            return "{" + ",".join(parts) + "}"
        finally:
            seen.discard(id(value))
    return json.dumps(str(value))


def _decimal_shift(data: float, digits: int) -> float:
    """Shift a number's decimal point right by ``digits`` places, by string."""
    text = _js_num_str(data)
    if "e" in text:
        return data * (10.0**digits)
    index = text.find(".")
    if index == -1:
        return data * (10.0**digits)
    frac = text[index + 1 :]
    integer = text[:index]
    if len(frac) <= digits:
        return float(integer + frac.ljust(digits, "0"))
    return float(integer + frac[:digits] + "." + frac[digits:])


def _is_multiple_of(data: float, min_value: float, step: float) -> bool:
    """Whether ``data`` is a multiple of ``step`` measured from ``min_value``.

    Decimal steps use decimal-shift integer arithmetic (float ``%`` gives
    wrong answers, e.g. ``0.3 % 0.1`` is ``0.0999...``); integer steps use
    float ``%``.
    """
    step = abs(step)
    if not re.fullmatch(r"\d+\.\d+", _js_num_str(step)):
        return (data - min_value) % step == 0
    digits = len(_js_num_str(step).split(".")[1])
    return (
        abs(_decimal_shift(data, digits) - _decimal_shift(min_value, digits))
        % _decimal_shift(step, digits)
        == 0
    )


# ---------------------------------------------------------------------------
# Deep equality and cloning (plain config data)
# ---------------------------------------------------------------------------


def deep_equal(a: Any, b: Any, strict: bool = False) -> bool:
    """Compare values recursively, mirroring the oracle's deepEqual.

    Two volatile references compare equal regardless of value. Non-strict
    mode treats null and undefined as equal. Cycles compare unequal unless
    identical. Plain dicts/lists compare by content; other object types
    compare by identity only.
    """
    ancestors: set[int] = set()

    def compare(left: Any, right: Any) -> bool:
        if left is right:
            return True
        if is_volatile(left) or is_volatile(right):
            return is_volatile(left) and is_volatile(right)
        if (
            not strict
            and (left is None or left is _UNDEFINED)
            and (right is None or right is _UNDEFINED)
        ):
            return True
        if isinstance(left, bool) or isinstance(right, bool):
            return isinstance(left, bool) and isinstance(right, bool) and left == right
        if isinstance(left, (int, float)) and isinstance(right, (int, float)):
            return left == right
        if isinstance(left, str) and isinstance(right, str):
            return left == right
        if not (
            isinstance(left, (dict, list, tuple))
            and isinstance(right, (dict, list, tuple))
        ):
            return False
        if id(left) in ancestors:
            return False
        ancestors.add(id(left))
        try:
            if isinstance(left, (list, tuple)) and isinstance(right, (list, tuple)):
                if len(left) != len(right):
                    return False
                return all(
                    compare(left_item, right_item)
                    for left_item, right_item in zip(left, right)
                )
            if isinstance(left, dict) and isinstance(right, dict):
                keys = set(left) | set(right)
                return all(compare(left.get(key), right.get(key)) for key in keys)
            return False
        finally:
            ancestors.discard(id(left))

    return compare(a, b)


def clone(value: Any, _refs: dict[int, Any] | None = None) -> Any:
    """Deep-clone plain config data, preserving cycles.

    Volatile references are returned as-is (references are stable).
    Prototypes and property descriptors are not preserved; plain dicts,
    lists, tuples, and primitives round-trip exactly.
    """
    if is_volatile(value):
        return value
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    refs = {} if _refs is None else _refs
    if id(value) in refs:
        return refs[id(value)]
    if isinstance(value, (list, tuple)):
        result: list[Any] = []
        refs[id(value)] = result
        result.extend(clone(item, refs) for item in value)
        return result if isinstance(value, list) else tuple(result)
    if isinstance(value, dict):
        result_dict: dict[Any, Any] = {}
        refs[id(value)] = result_dict
        for key, item in value.items():
            result_dict[clone(key, refs)] = clone(item, refs)
        return result_dict
    return copy.deepcopy(value)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


def _format_path(path: list[str | int]) -> str:
    """Render a path segment list as the ``$``-prefixed oracle prefix."""
    prefix = "$"
    for segment in path:
        if isinstance(segment, str):
            prefix += "." + segment
        else:
            prefix += f"[{segment}]"
    return prefix


class SchemaError(ValueError):
    """Raised when schema validation fails.

    ``message`` is the bare failure text; ``path`` is the list of path
    segments (string keys, integer indices). ``str(error)`` renders the
    oracle's ``$.path message`` form.
    """

    def __init__(self, message: str, path: list[str | int] | None = None) -> None:
        self.message = message
        self.path: list[str | int] = list(path) if path else []
        prefix = _format_path(self.path)
        full = message if prefix == "$" else f"{prefix} {message}"
        super().__init__(full)


class ValidationError(SchemaError):
    """Aggregated config validation failure (the ``~standard`` issue list).

    Raised by config resolution when ``~standard`` validation reports
    issues. Subclasses ``SchemaError`` so existing failure checks still
    match.
    """

    def __init__(self, issues: list[dict[str, Any]]) -> None:
        self.issues = issues
        lines = []
        for issue in issues:
            message = issue["message"]
            path = issue.get("path")
            if path:
                dotted = ".".join(str(segment) for segment in path)
                lines.append(f"  - {message} (at {dotted})")
            else:
                lines.append(f"  - {message}")
        super().__init__("invalid config:\n" + "\n".join(lines))


class _StandardProps:
    """The ``~standard`` seam: ``{version, vendor, validate}``."""

    version = 1
    vendor = "anima"

    def __init__(self, schema: Schema) -> None:
        self._schema = schema

    def validate(self, value: Any) -> dict[str, Any]:
        """Validate and return ``{'value': ...}`` or ``{'issues': [...]}``."""
        try:
            return {"value": self._schema.validate(value)}
        except SchemaError as err:
            return {"issues": [{"message": str(err), "path": list(err.path)}]}


def _validate_volatile_schema(
    schema: Schema,
    path: list[str | int] | None = None,
    blocked: bool = False,
    seen: set[tuple[int, bool]] | None = None,
) -> None:
    """Reject volatile fields without a fixed object path.

    A volatile field must sit at a fixed object path, or mark an entire
    object/array volatile. Volatile inside an array, dict, union member,
    transform inner, or another volatile field is rejected.
    """
    seen_set = set() if seen is None else seen
    key = (id(schema), blocked)
    if key in seen_set:
        return
    seen_set.add(key)
    current_path = list(path) if path else []
    if schema._volatile and blocked:
        raise SchemaError(
            "volatile fields require a fixed object path without an "
            "enclosing volatile field",
            current_path,
        )
    nested = blocked or schema._volatile
    for prop_key, child in (schema._properties or {}).items():
        _validate_volatile_schema(child, [*current_path, prop_key], nested, seen_set)
    if schema._inner is not None:
        _validate_volatile_schema(schema._inner, [*current_path, "*"], True, seen_set)
    for index, child in enumerate(schema._schemas or []):
        _validate_volatile_schema(child, [*current_path, str(index)], True, seen_set)


def _resolve_property(
    data: Any,
    key: Any,
    schema: Schema,
    path: list[str | int],
    strict: bool,
    present: bool = True,
) -> Any:
    """Resolve one object/list/dict entry, writing adapted values back."""
    raw = data[key] if present else _UNDEFINED
    value, adapted = schema._resolve(raw, [*path, key], strict)
    if adapted is not _UNDEFINED:
        data[key] = adapted
    return value


class Schema:
    """Composable AST schema node for declarative data validation and JSON Schema export."""

    def __init__(
        self,
        type_name: str,
        *,
        inner: Schema | None = None,
        properties: builtins.dict[str, Schema] | None = None,
        schemas: list[Schema] | None = None,
        const_val: Any = _UNDEFINED,
    ) -> None:
        if type_name == "array":
            type_name = "list"
        self.type_name = type_name
        self._inner = inner
        self._properties = properties
        self._schemas = schemas
        self._const_val = const_val
        self._uid = next(_UID_COUNTER)
        self._default: Any = _UNDEFINED
        self._default_set = False
        self._description: str | None = None
        self._hidden: bool = False
        self._required: bool = False
        self._min: float | None = None
        self._max: float | None = None
        self._step: float | None = None
        self._pattern_source: str | None = None
        self._pattern_flags: str = ""
        self._pattern_re_flags: int = re.ASCII
        self._volatile: bool = False
        self._loose: bool = False
        self._role: str | None = None
        self._role_extra: Any = _UNDEFINED
        self._extra_meta: builtins.dict[str, Any] = {}
        self._callback: Any = None
        self._preserve: bool = False

    # -- factories ---------------------------------------------------------

    @classmethod
    def string(cls) -> Schema:
        """Create a string schema."""
        return cls("string")

    @classmethod
    def number(cls) -> Schema:
        """Create a floating-point number schema."""
        return cls("number")

    @classmethod
    def natural(cls) -> Schema:
        """Create a non-negative integer-valued number schema."""
        return cls.number().step(1).min(0)

    @classmethod
    def integer(cls) -> Schema:
        """Create an integer number schema."""
        return cls("integer")

    @classmethod
    def boolean(cls) -> Schema:
        """Create a boolean schema."""
        return cls("boolean")

    @classmethod
    def object(cls, properties: builtins.dict[str, Schema]) -> Schema:
        """Create a structured object dictionary schema."""
        schema = cls("object", properties=properties)
        schema._default = {}
        return schema

    @classmethod
    def list(cls, inner: Schema) -> Schema:
        """Create a list array schema."""
        schema = cls("list", inner=inner)
        schema._default = []
        return schema

    @classmethod
    def array(cls, inner: Schema) -> Schema:
        """Alias for list schema."""
        return cls.list(inner)

    @classmethod
    def dict(cls, inner: Schema) -> Schema:
        """Create a key-value dictionary schema with typed values."""
        schema = cls("dict", inner=inner)
        schema._default = {}
        return schema

    @classmethod
    def union(cls, *schemas: Schema) -> Schema:
        """Create a union schema accepting any matching variant."""
        return cls("union", schemas=list(schemas))

    @classmethod
    def const(cls, val: Any) -> Schema:
        """Create a constant value schema."""
        return cls("const", const_val=val)

    @classmethod
    def any(cls) -> Schema:
        """Create an unconstrained schema accepting any value."""
        return cls("any")

    @classmethod
    def never(cls) -> Schema:
        """Create a schema that rejects every non-null value."""
        return cls("never")

    @classmethod
    def transform(cls, inner: Schema, callback: Any, preserve: bool = False) -> Schema:
        """Create a schema that validates with ``inner`` then converts."""
        if not isinstance(inner, Schema):
            raise TypeError("transform inner must be a Schema")
        if not callable(callback):
            raise TypeError("transform callback must be callable")
        schema = cls("transform", inner=inner)
        schema._callback = callback
        schema._preserve = preserve
        return schema

    # -- builders (mutate in place, return self for chaining) --------------

    def default(self, val: Any) -> Schema:
        """Assign default fallback value."""
        self._default = val
        self._default_set = True
        return self

    def description(self, text: str) -> Schema:
        """Assign schema documentation string."""
        self._description = text
        return self

    def hidden(self, val: bool = True) -> Schema:
        """Mark schema field as hidden from UI / documentation."""
        self._hidden = val
        return self

    def required(self, val: bool = True) -> Schema:
        """Mark field as required."""
        self._required = val
        return self

    def min(self, val: float) -> Schema:
        """Assign minimum numerical value or string/array length."""
        if isinstance(val, bool) or not isinstance(val, (int, float)):
            raise TypeError("min must be a number")
        self._min = val
        return self

    def max(self, val: float) -> Schema:
        """Assign maximum numerical value or string/array length."""
        if isinstance(val, bool) or not isinstance(val, (int, float)):
            raise TypeError("max must be a number")
        self._max = val
        return self

    def step(self, val: float) -> Schema:
        """Assign numeric increment constraint."""
        if isinstance(val, bool) or not isinstance(val, (int, float)):
            raise TypeError("step must be a number")
        self._step = val
        return self

    def pattern(self, regexp: Any) -> Schema:
        """Require strings to match a regular expression."""
        if isinstance(regexp, str):
            self._pattern_source = regexp
            self._pattern_flags = ""
            self._pattern_re_flags = re.ASCII
        elif isinstance(regexp, re.Pattern):
            self._pattern_source = regexp.pattern
            flags = ""
            re_flags = re.ASCII
            if regexp.flags & re.IGNORECASE:
                flags += "i"
                re_flags |= re.IGNORECASE
            if regexp.flags & re.MULTILINE:
                flags += "m"
                re_flags |= re.MULTILINE
            if regexp.flags & re.DOTALL:
                flags += "s"
                re_flags |= re.DOTALL
            self._pattern_flags = flags
            self._pattern_re_flags = re_flags
        else:
            raise TypeError("pattern must be a string or compiled pattern")
        return self

    def volatile(self) -> Schema:
        """Mark this node as a volatile (live-updating) reference."""
        if self._volatile:
            raise TypeError("volatile schema is already wrapped")
        self._volatile = True
        return self

    def loose(self, val: bool = True) -> Schema:
        """Return the default instead of throwing when validation fails."""
        self._loose = val
        return self

    def role(self, text: str, extra: Any = _UNDEFINED) -> Schema:
        """Attach a renderer role and optional role-specific metadata."""
        self._role = text
        self._role_extra = extra
        return self

    def extra(self, key: str, value: Any) -> Schema:
        """Attach arbitrary metadata consumed by renderers and tools."""
        self._extra_meta[key] = value
        return self

    # -- validation ---------------------------------------------------------

    def validate(self, data: Any, path: str | builtins.list[str | int] = "") -> Any:
        """Validate input data against schema constraints."""
        segments: builtins.list[str | int] = []
        if isinstance(path, str):
            segments.extend(path.split(".") if path else [])
        else:
            segments.extend(path)
        _validate_volatile_schema(self)
        value, _adapted = self._resolve(data, segments)
        return None if value is _UNDEFINED else value

    def _check_range(
        self,
        value: float,
        description: str,
        path: builtins.list[str | int],
        skip_min: bool = False,
    ) -> None:
        """Enforce min/max bounds; max is checked before min."""
        maximum = self._max if self._max is not None else math.inf
        minimum = self._min if self._min is not None else -math.inf
        if value > maximum:
            raise SchemaError(
                f"expected {description} <= {_js_num_str(maximum)} "
                f"but got {_js_num_str(value)}",
                path,
            )
        if value < minimum and not skip_min:
            raise SchemaError(
                f"expected {description} >= {_js_num_str(minimum)} "
                f"but got {_js_num_str(value)}",
                path,
            )

    def _resolve(
        self, data: Any, path: builtins.list[str | int], strict: bool = False
    ) -> tuple[Any, Any]:
        """Resolve a value, returning ``(value, adapted)``.

        ``adapted`` is ``_UNDEFINED`` unless a transform produced an adapted
        input. Raises ``SchemaError`` on failure.
        """
        if self._volatile:
            inner = copy.copy(self)
            inner._volatile = False
            value, adapted = inner._resolve(data, path, strict)
            try:
                # Absent input resolves to undefined; the snapshot holds
                # None at the boundary (the closest Python equivalent).
                snapshot = None if value is _UNDEFINED else value
                return create_volatile(snapshot), adapted
            except (TypeError, ValueError) as err:
                raise SchemaError(str(err), path) from err

        if data is _UNDEFINED or data is None:
            if self._required:
                raise SchemaError("missing required value", path)
            if self._default is _UNDEFINED:
                return data, _UNDEFINED
            data = clone(self._default)

        try:
            return self._dispatch(data, path, strict)
        except SchemaError:
            if not self._loose:
                raise
            fallback = self._default if self._default is not _UNDEFINED else _UNDEFINED
            return fallback, _UNDEFINED

    def _dispatch(
        self, data: Any, path: builtins.list[str | int], strict: bool
    ) -> tuple[Any, Any]:
        """Run the type resolver for non-null data; return ``(value, adapted)``."""
        match self.type_name:
            case "any":
                return data, _UNDEFINED

            case "string":
                if not isinstance(data, str):
                    raise SchemaError(f"expected string but got {_js_str(data)}", path)
                if self._pattern_source is not None:
                    regexp = re.compile(self._pattern_source, self._pattern_re_flags)
                    if not regexp.search(data):
                        rendered = f"/{self._pattern_source}/{self._pattern_flags}"
                        raise SchemaError(
                            f"expect string to match regexp {rendered}", path
                        )
                self._check_range(len(data), "string length", path)
                return data, _UNDEFINED

            case "integer":
                if isinstance(data, bool) or not isinstance(data, int):
                    raise SchemaError(
                        f"expected integer, got {type(data).__name__}", path
                    )
                self._check_range(data, "integer", path)
                return data, _UNDEFINED

            case "number":
                if isinstance(data, bool) or not isinstance(data, (int, float)):
                    raise SchemaError(f"expected number but got {_js_str(data)}", path)
                float_val = float(data)
                self._check_range(float_val, "number", path)
                if self._step and not _is_multiple_of(
                    float_val, self._min if self._min is not None else 0, self._step
                ):
                    raise SchemaError(
                        f"expected number multiple of "
                        f"{_js_num_str(self._step)} but got "
                        f"{_js_num_str(float_val)}",
                        path,
                    )
                return float_val, _UNDEFINED

            case "boolean":
                if not isinstance(data, bool):
                    raise SchemaError(f"expected boolean but got {_js_str(data)}", path)
                return data, _UNDEFINED

            case "const":
                if deep_equal(data, self._const_val):
                    return self._const_val, _UNDEFINED
                raise SchemaError(
                    f"expected {_js_str(self._const_val)} but got {_js_str(data)}",
                    path,
                )

            case "never":
                raise SchemaError(f"expected nullable but got {_js_str(data)}", path)

            case "list":
                if not isinstance(data, list):
                    raise SchemaError(f"expected array but got {_js_str(data)}", path)
                inner = self._inner or Schema.any()
                skip_min = (
                    inner._default is not _UNDEFINED and inner._default is not None
                )
                self._check_range(len(data), "array length", path, skip_min=skip_min)
                return [
                    _resolve_property(data, i, inner, path, strict)
                    for i in range(len(data))
                ], _UNDEFINED

            case "dict":
                if not isinstance(data, dict):
                    raise SchemaError(f"expected object but got {_js_str(data)}", path)
                inner = self._inner or Schema.any()
                result_dict: builtins.dict[str, Any] = {}
                for key in data:
                    result_dict[str(key)] = _resolve_property(
                        data, key, inner, path, strict
                    )
                return result_dict, _UNDEFINED

            case "object":
                if not isinstance(data, dict):
                    raise SchemaError(f"expected object but got {_js_str(data)}", path)
                props = self._properties or {}
                out_obj: builtins.dict[str, Any] = {}
                for key, prop_schema in props.items():
                    value = _resolve_property(
                        data, key, prop_schema, path, strict, key in data
                    )
                    if value is not _UNDEFINED or key in data:
                        out_obj[key] = None if value is _UNDEFINED else value
                if not strict:
                    for key, item in data.items():
                        if key not in out_obj:
                            out_obj[key] = item
                return out_obj, _UNDEFINED

            case "union":
                for inner_schema in self._schemas or []:
                    try:
                        return inner_schema._resolve(data, path, strict)
                    except SchemaError:
                        continue
                raise SchemaError(
                    f"expected {self.to_string()} but got {_js_json(data)}",
                    path,
                )

            case "transform":
                if self._callback is None:
                    raise SchemaError("transform schema has no callback", path)
                inner = self._inner or Schema.any()
                result, adapted = inner._resolve(data, path, True)
                if self._preserve:
                    return self._callback(result), _UNDEFINED
                adapted_in = data if adapted is _UNDEFINED else adapted
                return self._callback(result), self._callback(adapted_in)

            case _:
                raise SchemaError(f'unsupported type "{self.type_name}"', path)

    # -- rendering -----------------------------------------------------------

    def to_string(self, inline: bool = False) -> str:
        """Format this schema as a compact type string."""
        match self.type_name:
            case "string":
                return "string"
            case "number":
                return "number"
            case "integer":
                return "integer"
            case "boolean":
                return "boolean"
            case "any":
                return "any"
            case "never":
                return "never"
            case "const":
                if isinstance(self._const_val, str):
                    return json.dumps(self._const_val)
                return _js_str(self._const_val)
            case "list":
                inner = self._inner or Schema.any()
                return f"{inner.to_string(True)}[]"
            case "dict":
                inner = self._inner or Schema.any()
                return f"{{ [key: string]: {inner.to_string()} }}"
            case "object":
                props = self._properties or {}
                if not props:
                    return "{}"
                parts = [
                    f"{key}{'' if prop._required else '?'}: {prop.to_string()}"
                    for key, prop in props.items()
                ]
                return "{ " + ", ".join(parts) + " }"
            case "union":
                joined = " | ".join(
                    schema.to_string() for schema in (self._schemas or [])
                )
                return f"({joined})" if inline else joined
            case "transform":
                inner = self._inner or Schema.any()
                return inner.to_string(inline)
            case _:
                return f"Schema<{self.type_name}>"

    def __str__(self) -> str:
        return self.to_string()

    # -- JSON round-trip ------------------------------------------------------

    def _meta_json(self) -> builtins.dict[str, Any]:
        """Serialize the set metadata fields to a plain dict."""
        meta: builtins.dict[str, Any] = {}
        if self._default is not _UNDEFINED:
            meta["default"] = self._default
        if self._description is not None:
            meta["description"] = self._description
        if self._hidden:
            meta["hidden"] = True
        if self._required:
            meta["required"] = True
        if self._volatile:
            meta["volatile"] = True
        if self._loose:
            meta["loose"] = True
        if self._role is not None:
            meta["role"] = self._role
            if self._role_extra is not _UNDEFINED:
                meta["extra"] = self._role_extra
        if self._min is not None:
            meta["min"] = self._min
        if self._max is not None:
            meta["max"] = self._max
        if self._step is not None:
            meta["step"] = self._step
        if self._pattern_source is not None:
            meta["pattern"] = {
                "source": self._pattern_source,
                "flags": self._pattern_flags,
            }
        meta.update(self._extra_meta)
        return meta

    def _to_json_into(self, table: builtins.dict[int, builtins.dict[str, Any]]) -> int:
        """Serialize into the refs table; return this schema's uid."""
        if self._uid in table:
            return self._uid
        table[self._uid] = {}
        node: builtins.dict[str, Any] = {"type": self.type_name}
        meta = self._meta_json()
        if meta:
            node["meta"] = meta
        if self._inner is not None:
            node["inner"] = self._inner._to_json_into(table)
        if self._schemas:
            node["list"] = [s._to_json_into(table) for s in self._schemas]
        if self._properties:
            node["dict"] = {
                key: child._to_json_into(table)
                for key, child in self._properties.items()
            }
        if self._const_val is not _UNDEFINED:
            node["value"] = self._const_val
        if self.type_name == "transform":
            if self._callback is not None:
                raise TypeError("cannot serialize schema with transform callback")
            node["preserve"] = self._preserve
        table[self._uid] = node
        return self._uid

    def to_json(self) -> builtins.dict[str, Any]:
        """Serialize this schema, preserving shared references."""
        table: builtins.dict[int, builtins.dict[str, Any]] = {}
        uid = self._to_json_into(table)
        return {"uid": uid, "refs": table}

    @classmethod
    def from_json(cls, data: builtins.dict[str, Any]) -> Schema:
        """Rebuild a schema from :meth:`to_json` output."""
        refs = data["refs"]
        built: builtins.dict[int, Schema] = {}
        for raw_uid, node in refs.items():
            uid = int(raw_uid)
            schema = object.__new__(cls)
            schema.type_name = node["type"]
            if schema.type_name not in _SUPPORTED_TYPES:
                raise TypeError(f'unsupported type "{schema.type_name}"')
            schema._inner = None
            schema._properties = None
            schema._schemas = None
            schema._const_val = _UNDEFINED
            schema._uid = next(_UID_COUNTER)
            schema._default = _UNDEFINED
            schema._default_set = False
            schema._description = None
            schema._hidden = False
            schema._required = False
            schema._min = None
            schema._max = None
            schema._step = None
            schema._pattern_source = None
            schema._pattern_flags = ""
            schema._pattern_re_flags = re.ASCII
            schema._volatile = False
            schema._loose = False
            schema._role = None
            schema._role_extra = _UNDEFINED
            schema._extra_meta = {}
            schema._callback = None
            schema._preserve = False
            built[uid] = schema

        def ref_of(value: Any) -> Schema:
            if isinstance(value, int):
                return built[value]
            raise TypeError(f"invalid schema reference {value!r}")

        for raw_uid, node in refs.items():
            schema = built[int(raw_uid)]
            meta = node.get("meta", {})
            if "default" in meta:
                schema._default = meta["default"]
                schema._default_set = True
            if "description" in meta:
                schema._description = meta["description"]
            if meta.get("hidden"):
                schema._hidden = True
            if meta.get("required"):
                schema._required = True
            if meta.get("volatile"):
                schema._volatile = True
            if meta.get("loose"):
                schema._loose = True
            if "role" in meta:
                schema._role = meta["role"]
                if "extra" in meta:
                    schema._role_extra = meta["extra"]
            if "min" in meta:
                schema._min = meta["min"]
            if "max" in meta:
                schema._max = meta["max"]
            if "step" in meta:
                schema._step = meta["step"]
            if "pattern" in meta:
                schema._pattern_source = meta["pattern"]["source"]
                schema._pattern_flags = meta["pattern"].get("flags", "")
                re_flags = re.ASCII
                if "i" in schema._pattern_flags:
                    re_flags |= re.IGNORECASE
                if "m" in schema._pattern_flags:
                    re_flags |= re.MULTILINE
                if "s" in schema._pattern_flags:
                    re_flags |= re.DOTALL
                schema._pattern_re_flags = re_flags
            for key, value in meta.items():
                if key not in {
                    "default",
                    "description",
                    "hidden",
                    "required",
                    "volatile",
                    "loose",
                    "role",
                    "extra",
                    "min",
                    "max",
                    "step",
                    "pattern",
                }:
                    schema._extra_meta[key] = value
            if "inner" in node:
                schema._inner = ref_of(node["inner"])
            if "list" in node:
                schema._schemas = [ref_of(uid) for uid in node["list"]]
            if "dict" in node:
                schema._properties = {
                    key: ref_of(uid) for key, uid in node["dict"].items()
                }
            if "value" in node:
                schema._const_val = node["value"]
            if node.get("type") == "transform":
                schema._preserve = bool(node.get("preserve", False))
        return built[int(data["uid"])]

    # -- JSON Schema export ----------------------------------------------------

    def to_json_schema(self) -> builtins.dict[str, Any]:
        """Convert schema AST to standard JSON Schema dictionary."""
        js: builtins.dict[str, Any] = {}
        if self._description:
            js["description"] = self._description
        if self._default_set:
            js["default"] = self._default

        match self.type_name:
            case "string":
                js["type"] = "string"
                if self._pattern_source is not None:
                    js["pattern"] = self._pattern_source
            case "integer":
                js["type"] = "integer"
            case "number":
                js["type"] = "number"
            case "boolean":
                js["type"] = "boolean"
            case "never":
                js["not"] = {}
            case "list":
                js["type"] = "array"
                if self._inner:
                    js["items"] = self._inner.to_json_schema()
            case "object":
                js["type"] = "object"
                properties: builtins.dict[str, Any] = {}
                required: list[str] = []
                for k, prop in (self._properties or {}).items():
                    if prop._hidden:
                        continue
                    properties[k] = prop.to_json_schema()
                    if prop._required:
                        required.append(k)
                js["properties"] = properties
                if required:
                    js["required"] = required
            case "dict":
                js["type"] = "object"
                if self._inner:
                    js["additionalProperties"] = self._inner.to_json_schema()
            case "union":
                js["anyOf"] = [s.to_json_schema() for s in (self._schemas or [])]
            case "const":
                js["const"] = self._const_val
            case "transform":
                inner = self._inner or Schema.any()
                return inner.to_json_schema()
            case _:
                pass

        return js


def _standard_property(self: Schema) -> _StandardProps:
    """Return the ``~standard`` seam for this schema."""
    return _StandardProps(self)


setattr(Schema, "~standard", property(_standard_property))

z = Schema
