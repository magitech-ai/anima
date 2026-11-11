"""Cascading environment variable loader and variable expansion extension."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from anima.core import Context, Disposer

VAR_EXPAND_PATTERN = re.compile(
    r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::[-]?([^}]*))?\}|\$([A-Za-z_][A-Za-z0-9_]*)"
)


def parse_dotenv(content: str) -> dict[str, str]:
    """Parse .env formatted text into key-value pairs."""
    result: dict[str, str] = {}
    lines = content.splitlines()
    i = 0
    n = len(lines)

    while i < n:
        raw_line = lines[i].strip()
        i += 1

        if not raw_line or raw_line.startswith("#"):
            continue

        if raw_line.startswith("export "):
            raw_line = raw_line[7:].strip()

        if "=" not in raw_line:
            continue

        key, val = raw_line.split("=", 1)
        key = key.strip()
        val = val.strip()

        # Handle quoted or multiline values
        if val.startswith('"'):
            # Double quotes
            val_chars = [val[1:]]
            if not val.endswith('"') or len(val) == 1:
                while i < n:
                    next_line = lines[i]
                    i += 1
                    if next_line.endswith('"'):
                        val_chars.append(next_line[:-1])
                        break
                    val_chars.append(next_line)
                full_val = "\n".join(val_chars)
            else:
                full_val = val[1:-1]
            # Replace escape sequences
            full_val = (
                full_val.replace('\\"', '"')
                .replace("\\n", "\n")
                .replace("\\r", "\r")
                .replace("\\t", "\t")
            )
            result[key] = full_val
        elif val.startswith("'"):
            # Single quotes (no escape processing)
            val_chars = [val[1:]]
            if not val.endswith("'") or len(val) == 1:
                while i < n:
                    next_line = lines[i]
                    i += 1
                    if next_line.endswith("'"):
                        val_chars.append(next_line[:-1])
                        break
                    val_chars.append(next_line)
                full_val = "\n".join(val_chars)
            else:
                full_val = val[1:-1]
            result[key] = full_val
        else:
            # Unquoted: strip inline comments if any
            if " #" in val:
                val = val.split(" #", 1)[0].strip()
            result[key] = val

    return result


def expand_variables(
    env: dict[str, str],
    process_env: dict[str, str] | None = None,
) -> dict[str, str]:
    """Recursively expand ${VAR} and $VAR references within environment dictionary."""
    p_env = process_env if process_env is not None else dict(os.environ)
    resolved: dict[str, str] = dict(env)

    def _replace_match(match: re.Match[str]) -> str:
        var_name = match.group(1) or match.group(3)
        default_val = match.group(2)

        if var_name in p_env:
            return p_env[var_name]
        if var_name in resolved:
            return resolved[var_name]
        if default_val is not None:
            return default_val
        return ""

    changed = True
    iterations = 0
    while changed and iterations < 5:
        changed = False
        iterations += 1
        for k, v in list(resolved.items()):
            new_v = VAR_EXPAND_PATTERN.sub(_replace_match, v)
            if new_v != v:
                resolved[k] = new_v
                changed = True

    return resolved


def load_env_cascade(
    base_dir: Path | str | None = None,
    mode: str | None = None,
    process_env: dict[str, str] | None = None,
) -> dict[str, str]:
    """Load cascading .env files in order: .env -> .env.local -> .env.{mode} -> .env.{mode}.local."""
    root = Path(base_dir).resolve() if base_dir else Path.cwd()
    eff_mode = mode or os.environ.get("NODE_ENV") or "development"

    filenames = [
        ".env",
        ".env.local",
        f".env.{eff_mode}",
        f".env.{eff_mode}.local",
    ]

    merged: dict[str, str] = {}
    for fname in filenames:
        fpath = root / fname
        if fpath.is_file():
            try:
                content = fpath.read_text(encoding="utf-8")
                parsed = parse_dotenv(content)
                merged.update(parsed)
            except OSError:  # pragma: no cover
                pass

    return expand_variables(merged, process_env)


class CliEnv:
    """Cascading dotenv loader plugin registering ctx.env and updating os.environ."""

    name = "cli:env"

    def __init__(
        self,
        ctx: Context,
        config: dict[str, Any] | None = None,
    ) -> None:
        cfg = dict(config or {})
        base_dir = cfg.get("base_dir")
        mode = cfg.get("mode")
        self.ctx = ctx
        self.base_dir = (
            Path(base_dir).resolve()
            if base_dir
            else getattr(ctx, "base_dir", Path.cwd())
        )
        self.mode = mode
        self.apply(ctx)

    def apply(self, ctx: Context) -> Disposer:
        """Load cascading environment and inject onto ctx.env and os.environ."""
        merged = load_env_cascade(self.base_dir, self.mode)

        # Apply to os.environ without overwriting existing keys
        added_keys: list[str] = []
        for k, v in merged.items():
            if k not in os.environ:
                os.environ[k] = v
                added_keys.append(k)

        # Register os.environ as the `env` service on the context
        provide_disposer = ctx.provide("env", os.environ)

        def _dispose() -> None:
            for k in added_keys:
                os.environ.pop(k, None)
            provide_disposer()

        ctx.effect(lambda: _dispose)
        return _dispose


def apply(
    ctx: Context,
    config: dict[str, Any] | None = None,
) -> CliEnv:
    """Mount CliEnv plugin onto context."""
    return CliEnv(ctx, config)
