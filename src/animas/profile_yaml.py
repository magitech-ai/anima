"""YAML load/dump for Animas profiles with the ``!!js`` expression dialect.

PyYAML is the parser; this module owns the small dialect layered on it:
``!!js`` scalars become ``{"__jsExpr": ...}`` nodes on load and round-trip
back to ``!!js`` scalars on dump. It also exposes PyYAML node start marks as
config-path line/column locations for error reporting.

Expression *evaluation* lives in :mod:`animas.jsexpr`; this module does not
depend on it, so the profile format stays independent of the evaluator.
"""

from __future__ import annotations

from typing import Any

import yaml

__all__ = ["dump_yaml", "load_yaml", "load_yaml_with_locations"]

JSEXPR_TAG = "tag:yaml.org,2002:js"


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


def load_yaml_with_locations(
    stream: Any,
) -> tuple[Any, dict[tuple[Any, ...], tuple[int, int]]]:
    """Load YAML and map value paths to their 1-based line and column.

    PyYAML keeps ``start_mark`` on every composed node; this exposes those
    marks so the loader can report where an expression sits in the config
    file (upstream loader/config/tree.ts:254-260 and Include's override).
    """
    loader = _JsExprLoader(stream)
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
    """Dump YAML in the entry-list dialect (expression nodes become ``!!js`` scalars)."""
    yaml.dump(data, stream, Dumper=_JsExprDumper, sort_keys=False)
