"""Declarative configuration schema AST and validation engine for Anima."""

from __future__ import annotations

import builtins
from typing import Any

_UNDEFINED = object()


class SchemaError(ValueError):
    """Raised when schema validation fails."""

    def __init__(self, message: str, path: str = "") -> None:
        full_msg = f"{message} at '{path}'" if path else message
        super().__init__(full_msg)
        self.message = message
        self.path = path


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
        self.type_name = type_name
        self._inner = inner
        self._properties = properties
        self._schemas = schemas
        self._const_val = const_val
        self._default: Any = _UNDEFINED
        self._description: str | None = None
        self._hidden: bool = False
        self._required: bool = False
        self._min: float | None = None
        self._max: float | None = None

    @classmethod
    def string(cls) -> Schema:
        """Create a string schema."""
        return cls("string")

    @classmethod
    def number(cls) -> Schema:
        """Create a floating-point number schema."""
        return cls("number")

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
        return cls("object", properties=properties)

    @classmethod
    def list(cls, inner: Schema) -> Schema:
        """Create a list array schema."""
        return cls("list", inner=inner)

    @classmethod
    def array(cls, inner: Schema) -> Schema:
        """Alias for list schema."""
        return cls.list(inner)

    @classmethod
    def dict(cls, inner: Schema) -> Schema:
        """Create a key-value dictionary schema with typed values."""
        return cls("dict", inner=inner)

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

    def default(self, val: Any) -> Schema:
        """Assign default fallback value."""
        self._default = val
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
        """Assign minimum numerical value or string length."""
        self._min = val
        return self

    def max(self, val: float) -> Schema:
        """Assign maximum numerical value or string length."""
        self._max = val
        return self

    def validate(self, data: Any, path: str = "") -> Any:
        """Validate input data against schema constraints."""
        if data is None:
            if self._default is not _UNDEFINED:
                if self.type_name in ("object", "list", "dict") and isinstance(
                    self._default, (dict, list)
                ):
                    return self.validate(self._default, path=path)
                return self._default
            if self._required:
                raise SchemaError("missing required value", path)
            return None

        match self.type_name:
            case "any":
                return data

            case "string":
                if not isinstance(data, str):
                    raise SchemaError(
                        f"expected string, got {type(data).__name__}", path
                    )
                if self._min is not None and len(data) < self._min:
                    raise SchemaError(
                        f"string length {len(data)} < minimum {self._min}", path
                    )
                if self._max is not None and len(data) > self._max:
                    raise SchemaError(
                        f"string length {len(data)} > maximum {self._max}", path
                    )
                return data

            case "integer":
                if isinstance(data, bool) or not isinstance(data, int):
                    raise SchemaError(
                        f"expected integer, got {type(data).__name__}", path
                    )
                if self._min is not None and data < self._min:
                    raise SchemaError(f"value {data} < minimum {self._min}", path)
                if self._max is not None and data > self._max:
                    raise SchemaError(f"value {data} > maximum {self._max}", path)
                return data

            case "number":
                if isinstance(data, bool) or not isinstance(data, (int, float)):
                    raise SchemaError(
                        f"expected number, got {type(data).__name__}", path
                    )
                float_val = float(data)
                if self._min is not None and float_val < self._min:
                    raise SchemaError(f"value {float_val} < minimum {self._min}", path)
                if self._max is not None and float_val > self._max:
                    raise SchemaError(f"value {float_val} > maximum {self._max}", path)
                return float_val

            case "boolean":
                if not isinstance(data, bool):
                    raise SchemaError(
                        f"expected boolean, got {type(data).__name__}", path
                    )
                return data

            case "const":
                if data != self._const_val:
                    raise SchemaError(
                        f"expected constant value {self._const_val!r}, got {data!r}",
                        path,
                    )
                return data

            case "list":
                if not isinstance(data, list):
                    raise SchemaError(f"expected list, got {type(data).__name__}", path)
                inner = self._inner or Schema.any()
                res_list: list[Any] = []
                for i, item in enumerate(data):
                    p = f"{path}[{i}]" if path else f"[{i}]"
                    res_list.append(inner.validate(item, path=p))
                return res_list

            case "dict":
                if not isinstance(data, dict):
                    raise SchemaError(f"expected dict, got {type(data).__name__}", path)
                inner = self._inner or Schema.any()
                res_dict: dict[str, Any] = {}
                for k, v in data.items():
                    p = f"{path}.{k}" if path else str(k)
                    res_dict[str(k)] = inner.validate(v, path=p)
                return res_dict

            case "object":
                if not isinstance(data, dict):
                    raise SchemaError(f"expected dict, got {type(data).__name__}", path)
                props = self._properties or {}
                out_obj: dict[str, Any] = {}
                for k, prop_schema in props.items():
                    p = f"{path}.{k}" if path else str(k)
                    if k not in data or data[k] is None:
                        if prop_schema._default is not _UNDEFINED:
                            out_obj[k] = prop_schema.validate(
                                prop_schema._default, path=p
                            )
                        elif prop_schema._required:
                            raise SchemaError(f"missing required field '{k}'", path)
                        else:
                            out_obj[k] = None
                    else:
                        out_obj[k] = prop_schema.validate(data[k], path=p)
                return out_obj

            case "union":
                schemas = self._schemas or []
                errors: list[str] = []
                for s in schemas:
                    try:
                        return s.validate(data, path=path)
                    except SchemaError as err:
                        errors.append(str(err))
                raise SchemaError(
                    f"no union variant matched: {'; '.join(errors)}", path
                )

            case _:
                return data

    def to_json_schema(self) -> builtins.dict[str, Any]:
        """Convert schema AST to standard JSON Schema dictionary."""
        js: builtins.dict[str, Any] = {}
        if self._description:
            js["description"] = self._description
        if self._default is not _UNDEFINED:
            js["default"] = self._default

        match self.type_name:
            case "string":
                js["type"] = "string"
            case "integer":
                js["type"] = "integer"
            case "number":
                js["type"] = "number"
            case "boolean":
                js["type"] = "boolean"
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
            case _:
                pass

        return js


z = Schema
