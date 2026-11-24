"""YAML load/dump for Animas profiles.

PyYAML is the parser; this module provides the profile entry-list dialect
plus node start marks mapped to config-path line/column locations for error
reporting.
"""

from __future__ import annotations

from typing import Any

import yaml

__all__ = ["dump_yaml", "load_yaml", "load_yaml_with_locations"]


def load_yaml(stream: Any) -> Any:
    """Load a profile YAML document."""
    return yaml.safe_load(stream)


def load_yaml_with_locations(
    stream: Any,
) -> tuple[Any, dict[tuple[Any, ...], tuple[int, int]]]:
    """Load YAML and map value paths to their 1-based line and column.

    PyYAML keeps ``start_mark`` on every composed node; this exposes those
    marks so the loader can report where an expression sits in the config
    file (upstream loader/config/tree.ts:254-260 and Include's override).
    """
    loader = yaml.SafeLoader(stream)
    try:
        node = loader.get_single_node()
        data = loader.construct_document(node) if node is not None else None
    finally:
        loader.dispose()
    locations: dict[tuple[Any, ...], tuple[int, int]] = {}

    def walk(current: Any, path: tuple[Any, ...]) -> None:
        if isinstance(current, yaml.MappingNode):
            for key_node, value_node in current.value:
                key = loader.construct_object(key_node)
                walk(value_node, path + (key,))
        elif isinstance(current, yaml.SequenceNode):
            for index, child in enumerate(current.value):
                walk(child, path + (index,))
        else:
            locations[path] = (
                current.start_mark.line + 1,
                current.start_mark.column + 1,
            )

    if node is not None:
        walk(node, ())
    return data, locations


def dump_yaml(data: Any, stream: Any) -> None:
    """Dump a profile YAML document."""
    yaml.safe_dump(data, stream, sort_keys=False)
