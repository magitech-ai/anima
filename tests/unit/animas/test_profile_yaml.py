"""Profile YAML load/dump and location maps."""

from __future__ import annotations

import io

from animas.profile_yaml import dump_yaml, load_yaml, load_yaml_with_locations

_CORPUS = [
    "- id: a\n  name: mod:A\n  config: {}\n",
    "- id: a\n  disabled_on: [win32]\n  config:\n    port: 8080\n",
    '- name: x\n  config:\n    a: [1, 2, {k: v}]\n    b: "quoted"\n',
    "a: 1\nb:\n  - x\n  - y\n",
]


def test_load_dump_round_trip() -> None:
    for text in _CORPUS:
        loaded = load_yaml(text)
        with_locations, _ = load_yaml_with_locations(text)
        assert with_locations == loaded
        out = io.StringIO()
        dump_yaml(loaded, out)
        assert load_yaml(out.getvalue()) == loaded


def test_location_maps_are_1_based() -> None:
    data, locations = load_yaml_with_locations("a: 1\nb:\n  - x\n  - y\n")
    assert data == {"a": 1, "b": ["x", "y"]}
    assert locations[("a",)] == (1, 4)
    assert locations[("b", 0)] == (3, 5)
    assert locations[("b", 1)] == (4, 5)
