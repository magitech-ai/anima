"""Profile YAML dialect: behavior and byte-identical round-trips."""

from __future__ import annotations

import io

from animas.profile_yaml import dump_yaml, load_yaml, load_yaml_with_locations

_CORPUS = [
    "- id: a\n  name: mod:A\n  config: {}\n",
    (
        "- id: a\n  disabled: !!js process.platform === 'win32'\n"
        "  config:\n    port: !!js Number(process.env.PORT ?? 8080)\n"
    ),
    '- name: x\n  config:\n    a: [1, 2, {k: v}]\n    b: "quoted"\n',
    "a: 1\nb:\n  - x\n  - y\n",
    "- id: only\n  config:\n    nested: {__jsExpr: '1 === 1', other: 2}\n",
]


def test_load_dump_round_trip_is_byte_identical() -> None:
    for text in _CORPUS:
        loaded = load_yaml(text)
        with_locations, _ = load_yaml_with_locations(text)
        assert with_locations == loaded
        out = io.StringIO()
        dump_yaml(loaded, out)
        assert load_yaml(out.getvalue()) == loaded


def test_js_expr_scalars_round_trip_as_tagged_scalars() -> None:
    data = load_yaml("disabled: !!js process.platform === 'win32'\n")
    assert data == {"disabled": {"__jsExpr": "process.platform === 'win32'"}}
    out = io.StringIO()
    dump_yaml(data, out)
    # PyYAML quotes scalars containing JS comparison operators; the tag and
    # parsed value round-trip even though the textual spelling changes.
    assert out.getvalue() == "disabled: !!js 'process.platform === ''win32'''\n"
    assert load_yaml(out.getvalue()) == data


def test_multi_key_js_expr_map_stays_plain_mapping() -> None:
    text = "config:\n  nested: {__jsExpr: '1 === 1', other: 2}\n"
    out = io.StringIO()
    dump_yaml(load_yaml(text), out)
    assert "__jsExpr" in out.getvalue()
    assert "!!js" not in out.getvalue()


def test_location_maps_are_1_based() -> None:
    data, locations = load_yaml_with_locations("a: 1\nb:\n  - x\n  - y\n")
    assert data == {"a": 1, "b": ["x", "y"]}
    assert locations[("a",)] == (1, 4)
    assert locations[("b", 0)] == (3, 5)
    assert locations[("b", 1)] == (4, 5)
