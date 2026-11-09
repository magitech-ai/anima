"""Module loading utilities for Anima.

Python port of the reference @anima/loader-utils: module namespace export
resolution, module status codes, and load-cache job extraction.
"""

from __future__ import annotations

from enum import Enum
from typing import Any


def is_nullable(value: Any) -> bool:
    """Return True when `value` is None (null/undefined equivalent)."""
    return value is None


def unwrap_exports(exports: Any) -> Any:
    """Get the plugin exported by a module namespace.

    Mirrors the reference unwrapExports: handles CommonJS (module.exports),
    ES modules (__es_module + default), and plain values.
    """
    if is_nullable(exports):
        return exports
    default_val = getattr(exports, "default", None)
    if default_val is not None:
        exports = default_val
    if not getattr(exports, "__es_module", False):
        return exports
    default_val = getattr(exports, "default", None)
    if default_val is not None:
        return default_val
    return exports


class ModuleFormat(str, Enum):
    """Module format categories."""

    BUILTIN = "builtin"
    COMMONJS = "commonjs"
    JSON = "json"
    MODULE = "module"
    WASM = "wasm"


class ModuleStatus(int, Enum):
    """ESM module job status codes (parity)."""

    UNINSTANTIATED = 0
    INSTANTIATING = 1
    INSTANTIATED = 2
    EVALUATING = 3
    EVALUATED = 4
    ERRORED = 5


class ModuleWrap:
    """A loaded ES module wrap."""

    def __init__(self, url: str) -> None:
        self.url = url

    def get_namespace(self) -> Any:
        raise NotImplementedError

    def get_status(self) -> ModuleStatus:
        raise NotImplementedError


class ModuleJob:
    """A module job in the loader's module graph."""

    def __init__(self, url: str, loader: Any) -> None:
        self.url = url
        self.loader = loader
        self.module: ModuleWrap | None = None
        self.import_attributes: dict[str, Any] = {}
        self.module_promise: Any = None
        self.linked: Any = None
        self.instantiated: Any | None = None

    def instantiate(self) -> Any:
        raise NotImplementedError

    def run(self) -> Any:
        raise NotImplementedError


class ModuleLoader:
    """Node ESM module loader interface."""

    @classmethod
    def from_internal(cls) -> ModuleLoader | None:
        """Locate the running Node internal module loader.

        Python has no Node internal ESM loader, so this always returns None.
        Callers must handle the absence gracefully.
        """
        return None


def get_module_jobs(load_cache: dict[str, Any], url: str) -> list[Any]:
    """Get the module jobs cached for `url`.

    A load cache entry holds one job per import attribute type. Reads the
    raw cache entry (bypassing any typed wrapper) so that jobs imported with
    `with { type: 'json' }` are not missed.
    """
    entry: dict[str, Any] | None = load_cache.get(url)
    return [job for job in (entry or {}).values() if job]
