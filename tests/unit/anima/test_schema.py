import pytest

from anima.core import Context, FiberState
from anima.schema import Schema, SchemaError, z


def test_schema_string_and_constraints():
    s = Schema.string().default("fallback").description("User name")
    assert s.validate(None) == "fallback"
    assert s.validate("Alice") == "Alice"

    with pytest.raises(SchemaError) as exc:
        s.validate(123)
    assert "expected string" in str(exc.value)

    # String with min/max length
    bounded = Schema.string().min(3).max(6)
    assert bounded.validate("hello") == "hello"
    with pytest.raises(SchemaError):
        bounded.validate("hi")
    with pytest.raises(SchemaError):
        bounded.validate("too_long_string")


def test_schema_numbers_and_booleans():
    int_schema = Schema.integer().min(0).max(100).default(10)
    assert int_schema.validate(None) == 10
    assert int_schema.validate(42) == 42

    with pytest.raises(SchemaError):
        int_schema.validate("not_an_int")

    with pytest.raises(SchemaError):
        int_schema.validate(-1)

    with pytest.raises(SchemaError):
        int_schema.validate(101)

    # Float number
    num_schema = Schema.number()
    assert num_schema.validate(3.14) == 3.14
    assert num_schema.validate(42) == 42.0

    # Boolean
    bool_schema = Schema.boolean()
    assert bool_schema.validate(True) is True
    assert bool_schema.validate(False) is False
    with pytest.raises(SchemaError):
        bool_schema.validate("true")


def test_schema_object_and_nesting():
    user_schema = Schema.object(
        {
            "name": Schema.string().required(),
            "age": Schema.integer().default(18),
            "tags": Schema.list(Schema.string()).default([]),
            "meta": Schema.object(
                {
                    "active": Schema.boolean().default(True),
                }
            ).default({}),
        }
    )

    # Validate with defaults filled
    res = user_schema.validate({"name": "Bob"})
    assert res == {
        "name": "Bob",
        "age": 18,
        "tags": [],
        "meta": {"active": True},
    }

    # Missing required field
    with pytest.raises(SchemaError) as exc:
        user_schema.validate({"age": 25})
    assert str(exc.value) == "$.name missing required value"
    assert exc.value.path == ["name"]

    # Non-dict value
    with pytest.raises(SchemaError):
        user_schema.validate("not_a_dict")


def test_schema_list_and_dict():
    # List schema
    list_s = Schema.list(Schema.integer())
    assert list_s.validate([1, 2, 3]) == [1, 2, 3]

    with pytest.raises(SchemaError):
        list_s.validate("not_a_list")

    with pytest.raises(SchemaError):
        list_s.validate([1, "bad", 3])

    # Dict schema
    dict_s = Schema.dict(Schema.number())
    assert dict_s.validate({"a": 1, "b": 2.5}) == {"a": 1.0, "b": 2.5}

    with pytest.raises(SchemaError):
        dict_s.validate("not_a_dict")

    with pytest.raises(SchemaError):
        dict_s.validate({"a": "not_num"})


def test_schema_union_and_const():
    u = Schema.union(Schema.string(), Schema.integer())
    assert u.validate("text") == "text"
    assert u.validate(123) == 123
    with pytest.raises(SchemaError):
        u.validate(3.14)

    # Const schema
    c = Schema.const("exact_value")
    assert c.validate("exact_value") == "exact_value"
    with pytest.raises(SchemaError):
        c.validate("other_value")

    # Any schema
    a = Schema.any()
    assert a.validate({"anything": True}) == {"anything": True}


def test_schema_additional_branches_and_json_schemas():
    # 1. Array alias and None handling
    arr = Schema.array(Schema.string())
    assert arr.validate(["a", "b"]) == ["a", "b"]

    req_str = Schema.string().required()
    with pytest.raises(SchemaError):
        req_str.validate(None)

    opt_str = Schema.string()
    assert opt_str.validate(None) is None

    # 2. Number min/max bounds
    num_bound = Schema.number().min(10.0).max(50.0)
    assert num_bound.validate(25.0) == 25.0
    with pytest.raises(SchemaError):
        num_bound.validate(5.0)
    with pytest.raises(SchemaError):
        num_bound.validate(60.0)

    # 3. Object with optional field without default: absent means omitted
    obj_opt = Schema.object({"opt_field": Schema.string()})
    assert obj_opt.validate({}) == {}

    # ... but an explicitly present null is kept as null
    assert obj_opt.validate({"opt_field": None}) == {"opt_field": None}

    # Object with default dict validated on None
    obj_def = Schema.object({"name": Schema.string().default("auto")}).default({})
    assert obj_def.validate(None) == {"name": "auto"}

    # 4. JSON schema generation for all types
    assert Schema.integer().to_json_schema() == {"type": "integer"}
    assert Schema.number().to_json_schema() == {"type": "number"}
    assert Schema.boolean().to_json_schema() == {"type": "boolean"}
    assert Schema.dict(Schema.integer()).to_json_schema() == {
        "type": "object",
        "additionalProperties": {"type": "integer"},
    }
    assert Schema.list(Schema.string()).to_json_schema() == {
        "type": "array",
        "items": {"type": "string"},
    }
    assert Schema.union(Schema.string(), Schema.integer()).to_json_schema() == {
        "anyOf": [{"type": "string"}, {"type": "integer"}]
    }
    assert Schema.const("exact").to_json_schema() == {"const": "exact"}
    assert Schema.any().to_json_schema() == {}

    # Unknown type fails loud instead of passing through
    with pytest.raises(SchemaError) as exc:
        Schema("unknown_type").validate("passthrough")
    assert 'unsupported type "unknown_type"' in str(exc.value)

    # List, dict, object without inner/required in to_json_schema
    assert Schema("list").to_json_schema() == {"type": "array"}
    assert Schema("dict").to_json_schema() == {"type": "object"}
    assert Schema.object({"optional": Schema.string()}).to_json_schema() == {
        "type": "object",
        "properties": {"optional": {"type": "string"}},
    }


def test_schema_json_schema_generation():
    obj_schema = z.object(
        {
            "title": z.string().description("Book title").required(),
            "copies": z.integer().default(1),
            "secret": z.string().hidden(),
        }
    ).description("Book entity")

    json_schema = obj_schema.to_json_schema()
    assert json_schema["type"] == "object"
    assert json_schema["description"] == "Book entity"
    assert "title" in json_schema["properties"]
    assert json_schema["properties"]["title"]["type"] == "string"
    assert json_schema["properties"]["title"]["description"] == "Book title"
    assert "copies" in json_schema["properties"]
    assert json_schema["properties"]["copies"]["default"] == 1
    assert "secret" not in json_schema["properties"]  # hidden


@pytest.mark.asyncio
async def test_schema_fiber_config_validation():
    ctx = Context()

    class ConfiguredComponent:
        Config = Schema.object(
            {
                "host": Schema.string().default("127.0.0.1"),
                "port": Schema.integer().required(),
            }
        )

        def __init__(self, c: Context, **cfg):
            self.cfg = cfg

    # 1. Valid config with defaults filled
    fib = ctx.use(ConfiguredComponent, port=8080)
    await ctx.flush()
    assert fib.state == FiberState.ACTIVE
    assert fib.config == {"host": "127.0.0.1", "port": 8080}

    # 2. Invalid config transitions fiber to FAILED (validation is lazy,
    # so the failure lands when the async load runs)
    fib_bad = ctx.use(ConfiguredComponent, port="invalid_port")
    await ctx.flush()
    assert fib_bad.state == FiberState.FAILED
    assert isinstance(fib_bad.error, SchemaError)
