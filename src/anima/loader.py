"""
Profile and configuration loader for Anima.
Enables dynamic configuration-driven composition, environment interpolation, and persistence.
"""

from __future__ import annotations

import importlib
import logging
import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

from anima.core import Context, Fiber, Service

logger = logging.getLogger("anima.loader")

ENV_PATTERN = re.compile(r"\$\{(?:env\.)?([A-Za-z_][A-Za-z0-9_]*)(?::([^}]*))?\}")

_BUILTIN_ALIASES: dict[str, str] = {
    "group": "anima.group:Group",
    "logger": "anima.logger:LoggerService",
    "timer": "anima.timer:TimerService",
    "watcher": "anima.watcher:WatcherService",
    "hmr": "anima.hmr:HmrService",
    "pkg": "anima.pkg:PkgService",
}


def interpolate_env(value: Any, env: Mapping[str, str] | None = None) -> Any:
    """Recursively interpolate environment variables in string, dict, or list."""
    environ = env if env is not None else os.environ

    if isinstance(value, str):
        full_match = ENV_PATTERN.fullmatch(value)
        if full_match:
            var_name = full_match.group(1)
            default_val = full_match.group(2)
            raw = environ.get(var_name, default_val if default_val is not None else "")
            if raw.lower() == "true":
                return True
            if raw.lower() == "false":
                return False
            try:
                return int(raw)
            except ValueError:
                pass
            try:
                return float(raw)
            except ValueError:
                pass
            return raw

        def _replace(match: re.Match[str]) -> str:
            var_name = match.group(1)
            default_val = match.group(2)
            if var_name in environ:
                return environ[var_name]
            if default_val is not None:
                return default_val
            return ""

        return ENV_PATTERN.sub(_replace, value)

    if isinstance(value, dict):
        return {k: interpolate_env(v, environ) for k, v in value.items()}

    if isinstance(value, list):
        return [interpolate_env(v, environ) for v in value]

    return value


def resolve_plugin_target(target_str: str) -> Any:
    """Resolve a plugin target from a module path, dot-string, or builtin alias."""
    if target_str in _BUILTIN_ALIASES:
        target_str = _BUILTIN_ALIASES[target_str]

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


def save_profile(path: Path | str, entries: list[dict[str, Any]]) -> None:
    """Save plugin configuration entries to a YAML file."""
    p = Path(path).resolve()
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        yaml.safe_dump(entries, f, sort_keys=False)


class LoaderService(Service):
    """Configuration-driven loader service managing plugin lifecycles and persistence."""

    def __init__(self, ctx: Context, *, filename: str | Path | None = None) -> None:
        super().__init__(ctx, "loader")
        self.filename = Path(filename).resolve() if filename else None
        self.entries: list[dict[str, Any]] = []
        self.fibers: dict[str, Fiber] = {}
        self.ctx.on("internal/commit", self._on_commit)

    def load(
        self,
        config_source: str | Path | list[Any] | dict[str, Any],
        *,
        env: dict[str, str] | None = None,
    ) -> None:
        """Parse configuration, interpolate environment variables, and mount plugins."""
        if isinstance(config_source, (str, Path)):
            path = Path(config_source).resolve()
            if not path.exists():
                raise FileNotFoundError(f"Profile config file not found: {path}")
            self.filename = path
            with open(path, encoding="utf-8") as f:
                raw_data = yaml.safe_load(f)
        else:
            raw_data = config_source

        custom_env = env
        if isinstance(raw_data, dict):
            if "env" in raw_data and isinstance(raw_data["env"], dict):
                custom_env = dict(env) if env else dict(os.environ)
                custom_env.update({str(k): str(v) for k, v in raw_data["env"].items()})
            entries_data = raw_data.get("plugins", [])
        elif isinstance(raw_data, list):
            entries_data = raw_data
        else:
            raise TypeError("Profile configuration must be a list or dict")

        interpolated_entries = interpolate_env(entries_data, env=custom_env)
        if not isinstance(interpolated_entries, list):
            raise TypeError("Profile configuration plugins must resolve to a list")

        for item in interpolated_entries:
            if isinstance(item, str):
                entry_dict: dict[str, Any] = {"name": item}
            elif isinstance(item, dict):
                entry_dict = item
            else:
                continue

            self._mount_entry(entry_dict)

    def _mount_entry(self, item: dict[str, Any]) -> None:
        """Mount a single configuration entry onto the context tree."""
        name = item.get("name") or item.get("target")
        if not name:
            return

        base_id = str(item.get("id") or name)
        eid = base_id
        idx = 1
        while eid in self.fibers:
            eid = f"{base_id}_{idx}"
            idx += 1
        item["id"] = eid
        self.entries.append(item)

        if item.get("disabled", False):
            return

        entry_ctx = self.ctx
        if "isolate" in item and isinstance(item["isolate"], list):
            for svc in item["isolate"]:
                entry_ctx = entry_ctx.isolate(svc)

        if "intercept" in item and isinstance(item["intercept"], dict):
            for svc, cfg in item["intercept"].items():
                if isinstance(cfg, dict):
                    entry_ctx = entry_ctx.intercept(svc, cfg)

        config = dict(item.get("config", {}))

        if name == "group" or "entries" in item:
            nested_entries = item.get("entries", [])
            group_cls = resolve_plugin_target("group")
            group_inst = group_cls(entry_ctx, entries=nested_entries, name=eid)
            fib = entry_ctx.plugin(group_inst)
        else:
            target = resolve_plugin_target(str(name))
            fib = entry_ctx.plugin(target, **config)

        self.fibers[eid] = fib

    async def enable(self, entry_id: str) -> None:
        """Enable and mount a previously disabled entry."""
        for item in self.entries:
            eid = str(item.get("id") or item.get("name") or item.get("target"))
            if eid == entry_id:
                if item.get("disabled"):
                    item["disabled"] = False
                    self._mount_entry(item)
                    await self.ctx.flush()
                return

    async def disable(self, entry_id: str) -> None:
        """Disable and unload an active entry."""
        for item in self.entries:
            eid = str(item.get("id") or item.get("name") or item.get("target"))
            if eid == entry_id:
                item["disabled"] = True
                if entry_id in self.fibers:
                    fib = self.fibers.pop(entry_id)
                    await fib.dispose()
                return

    async def update(self, entry_id: str, new_config: dict[str, Any]) -> None:
        """Update configuration for an active entry."""
        for item in self.entries:
            eid = str(item.get("id") or item.get("name") or item.get("target"))
            if eid == entry_id:
                item["config"] = dict(new_config)
                if entry_id in self.fibers:
                    await self.fibers[entry_id].update(new_config)
                return

    def save(self, path: Path | str | None = None) -> None:
        """Persist current configuration entries to disk."""
        target_path = Path(path).resolve() if path else self.filename
        if target_path is None:
            raise ValueError("No destination path specified to save profile")
        save_profile(target_path, self.entries)

    def _on_commit(self, fiber: Fiber, config: dict[str, Any]) -> None:
        """Handle internal/commit event when a plugin's configuration changes."""
        for item in self.entries:
            eid = str(item.get("id") or item.get("name") or item.get("target"))
            if eid in self.fibers and self.fibers[eid] is fiber:
                item["config"] = dict(config)
                if self.filename is not None:
                    try:
                        self.save()
                    except Exception:
                        logger.exception("Failed to auto-save committed profile")
                break


def load_profile(
    ctx: Context,
    config_source: str | Path | list[Any] | dict[str, Any],
    *,
    env: dict[str, str] | None = None,
) -> LoaderService:
    """Load a profile of plugin configurations into a Context."""
    filename = config_source if isinstance(config_source, (str, Path)) else None
    loader = LoaderService(ctx, filename=filename)
    loader.load(config_source, env=env)
    return loader
