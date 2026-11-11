"""
Anima: The Composable Micro-Kernel by Magitech.
"""

__version__ = "0.2.0"

import importlib
from typing import Any

from anima.core import Context, Fiber, FiberState, Service, inject
from anima.events import DisposableList, Disposer, EventBus, EventMode, is_bailed
from anima.group import Group, GroupEntry
from anima.hmr import HmrService
from anima.http import HttpConfig, HttpError, HttpRequest, HttpResponse, HttpService
from anima.include import (
    IncludeError,
    IncludeService,
    Journal,
    JournalRecord,
    apply_patch,
    diff_entries,
)
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
from anima.schema import Schema, SchemaError, z
from anima.server import Route, ServerRequest, ServerResponse, ServerService, WebSocket
from anima.timer import Scheduled, TimerService
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
    "CliCommand",
    "CliService",
    "CliSyntaxError",
    "ConsoleExporter",
    "Context",
    "DisposableList",
    "Disposer",
    "EventBus",
    "EventMode",
    "Exporter",
    "Fiber",
    "FiberState",
    "Group",
    "GroupEntry",
    "HmrService",
    "HttpConfig",
    "HttpError",
    "HttpRequest",
    "HttpResponse",
    "HttpService",
    "IncludeError",
    "IncludeService",
    "Inject",
    "Journal",
    "JournalRecord",
    "LoaderService",
    "Logger",
    "LoggerLevel",
    "LoggerService",
    "Message",
    "PkgResult",
    "PkgService",
    "ReflectService",
    "RegistryService",
    "Route",
    "Scheduled",
    "Schema",
    "SchemaError",
    "ServerRequest",
    "ServerResponse",
    "ServerService",
    "Service",
    "TimerService",
    "WatchEntry",
    "WatcherService",
    "WebSocket",
    "apply_patch",
    "diff_entries",
    "inject",
    "interpolate_env",
    "is_bailed",
    "load_profile",
    "resolve_component_target",
    "resolve_inject",
    "save_profile",
    "z",
]
