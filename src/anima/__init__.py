"""
Anima: The Composable Micro-Kernel by Magitech.
"""

__version__ = "0.2.0"

import importlib
from typing import Any

from anima.cli_env import CliEnv
from anima.core import Context, Fiber, FiberState, Service, inject
from anima.events import DisposableList, Disposer, EventBus, EventMode, is_bailed
from anima.hmr import HmrService, Reload, handle_error
from anima.http import HttpConfig, HttpError, HttpRequest, HttpResponse, HttpService
from anima.include import (
    Conflict,
    EntryOwner,
    FlatEntry,
    Include,
    IncludeError,
    Journal,
    JournalRecord,
    PatchIndex,
    StaleWriteError,
    apply_changes,
    apply_entry_patches,
    apply_journal,
    detach,
    diff_entries,
    diff_options,
    ensure_ids,
    ensure_insert_ids,
    equal_except_volatile,
    flatten,
    merge,
    merge_records,
    place,
    reconcile,
    record,
    route_journal,
)
from anima.jsexpr import JsExprError, evaluate, interpolate
from anima.loader import (
    LoaderService,
    interpolate_env,
    load_profile,
    resolve_component_target,
    save_profile,
)
from anima.logger import (
    ConsoleExporter,
    Exporter,
    Logger,
    LoggerLevel,
    LoggerService,
    Message,
)
from anima.pkg import PkgResult, PkgService
from anima.reflect import ReflectService
from anima.registry import Inject, RegistryService, resolve_inject
from anima.schema import Schema, SchemaError, ValidationError, z
from anima.server import Route, ServerRequest, ServerResponse, ServerService, WebSocket
from anima.timer import Scheduled, TimerService
from anima.tree import Entry, EntryChange, EntryGroup, EntryOptions, EntryTree, Group
from anima.utils import (
    Binary,
    Dict,
    Promisify,
    Time,
    filter_keys,
    hyphenate,
    is_non_nullable,
    is_nullable,
    is_plain_object,
    map_values,
    pick,
    value_map,
)
from anima.volatile import (
    Volatile,
    create_volatile,
    is_volatile,
    update_volatile,
    volatile_entries,
)
from anima.watcher import WatchEntry, WatcherService

# CLI symbols resolve lazily: importing anima.cli eagerly puts it in
# sys.modules before `python -m anima.cli` executes it, which triggers a
# RuntimeWarning about the double import.
_LAZY_CLI_EXPORTS = {
    "CliCommand": "anima.cli",
    "CliService": "anima.cli",
    "CliSyntaxError": "anima.cli",
}


def __getattr__(name: str) -> Any:
    """Lazily resolve CLI exports (PEP 562)."""
    if name in _LAZY_CLI_EXPORTS:
        module = importlib.import_module(_LAZY_CLI_EXPORTS[name])
        return getattr(module, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "Binary",
    "CliCommand",
    "CliEnv",
    "CliService",
    "CliSyntaxError",
    "Conflict",
    "ConsoleExporter",
    "Context",
    "Dict",
    "DisposableList",
    "Disposer",
    "Entry",
    "EntryChange",
    "EntryGroup",
    "EntryOptions",
    "EntryOwner",
    "EntryTree",
    "EventBus",
    "EventMode",
    "Exporter",
    "Fiber",
    "FiberState",
    "FlatEntry",
    "Group",
    "HmrService",
    "HttpConfig",
    "HttpError",
    "HttpRequest",
    "HttpResponse",
    "HttpService",
    "Include",
    "IncludeError",
    "Inject",
    "Journal",
    "JournalRecord",
    "JsExprError",
    "LoaderService",
    "Logger",
    "LoggerLevel",
    "LoggerService",
    "Message",
    "PatchIndex",
    "PkgResult",
    "PkgService",
    "Promisify",
    "ReflectService",
    "RegistryService",
    "Reload",
    "Route",
    "Scheduled",
    "Schema",
    "SchemaError",
    "ServerRequest",
    "ServerResponse",
    "ServerService",
    "Service",
    "StaleWriteError",
    "Time",
    "TimerService",
    "ValidationError",
    "Volatile",
    "WatchEntry",
    "WatcherService",
    "WebSocket",
    "apply_changes",
    "apply_entry_patches",
    "apply_journal",
    "create_volatile",
    "detach",
    "diff_entries",
    "diff_options",
    "ensure_ids",
    "ensure_insert_ids",
    "equal_except_volatile",
    "evaluate",
    "filter_keys",
    "flatten",
    "handle_error",
    "hyphenate",
    "inject",
    "interpolate",
    "interpolate_env",
    "is_bailed",
    "is_non_nullable",
    "is_nullable",
    "is_plain_object",
    "is_volatile",
    "load_profile",
    "map_values",
    "merge",
    "merge_records",
    "pick",
    "place",
    "reconcile",
    "record",
    "resolve_component_target",
    "resolve_inject",
    "route_journal",
    "save_profile",
    "update_volatile",
    "value_map",
    "volatile_entries",
    "z",
]
