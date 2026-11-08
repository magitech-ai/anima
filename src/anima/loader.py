"""
YAML Profile and Patch Loader in Python.
Enables dynamic configuration-driven composition without changing core code.
"""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

import yaml

from anima.core import Context


def resolve_plugin_target(target_str: str) -> Any:
    """Resolve a plugin target from a module path or dot-string."""
    if ":" in target_str:
        mod_name, attr_name = target_str.split(":", 1)
    elif "." in target_str:
        parts = target_str.rsplit(".", 1)
        mod_name, attr_name = parts[0], parts[1]
    else:
        mod_name = target_str
        attr_name = None

    mod = importlib.import_module(mod_name)
    if attr_name:
        return getattr(mod, attr_name)
    if hasattr(mod, "apply"):
        return mod.apply
    return mod


def load_profile(
    ctx: Context, config_source: str | Path | list[dict[str, Any]]
) -> None:
    """Load a profile list of plugin configurations into a Context."""
    if isinstance(config_source, (str, Path)):
        path = Path(config_source)
        if not path.exists():
            raise FileNotFoundError(f"Profile config file not found: {path}")
        with open(path, encoding="utf-8") as f:
            entries = yaml.safe_load(f)
    else:
        entries = config_source

    if not isinstance(entries, list):
        raise ValueError(  # noqa: TRY004
            "Profile configuration must be a list of plugin entries"
        )

    for entry in entries:
        if isinstance(entry, str):
            target = resolve_plugin_target(entry)
            ctx.plugin(target)
        elif isinstance(entry, dict):
            name = entry.get("name")
            if not name:
                continue
            target = resolve_plugin_target(name)
            config = entry.get("config", {})
            ctx.plugin(target, **config)
