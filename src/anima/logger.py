"""
Contextual logging service for Anima.
Provides structured, namespaced logging with reversible exporters and level filtering.
"""

from __future__ import annotations

import json
import logging
import math
import re
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, TextIO

from anima.core import Context, Service
from anima.events import Disposer


class LoggerLevel(IntEnum):
    ERROR = 0
    WARN = 1
    INFO = 2
    DEBUG = 3


@dataclass
class Message:
    sn: int
    ts: float
    name: str
    type: str
    level: int
    args: list[Any] = field(default_factory=list)


Formatter = Callable[[Any, "Exporter", Message], Any]

C16 = [6, 2, 3, 4, 5, 1]
C256 = [
    20,
    21,
    26,
    27,
    32,
    33,
    38,
    39,
    40,
    41,
    42,
    43,
    44,
    45,
    56,
    57,
    62,
    63,
    68,
    69,
    74,
    75,
    76,
    77,
    78,
    79,
    80,
    81,
    92,
    93,
    98,
    99,
    112,
    113,
    129,
    134,
    135,
    148,
    149,
    160,
    161,
    162,
    163,
    164,
    165,
    166,
    167,
    168,
    169,
    170,
    171,
    172,
    173,
    178,
    179,
    184,
    185,
    196,
    197,
    198,
    199,
    200,
    201,
    202,
    203,
    204,
    205,
    206,
    207,
    208,
    209,
    214,
    215,
    220,
    221,
]


def _hash_name_color(name: str, colors_mode: int | bool) -> int:
    if not colors_mode:
        return 0
    val = sum(ord(c) for c in name) + 13
    palette = C256 if (isinstance(colors_mode, int) and colors_mode >= 2) else C16
    return palette[abs(val) % len(palette)]


class Exporter:
    """Base exporter contract for consuming formatted log messages."""

    def __init__(
        self,
        *,
        colors: int | bool = 0,
        max_length: int = 10240,
        levels: dict[str, int] | None = None,
        formatters: dict[str, Formatter] | None = None,
    ) -> None:
        self.colors = colors
        self.max_length = max_length
        self.levels = levels or {}
        self.formatters = formatters or {}

    def export(self, message: Message) -> None:
        """Process and output a logged message."""
        raise NotImplementedError


class ConsoleExporter(Exporter):
    """Console stream exporter formatting output to stdout/stderr."""

    def __init__(
        self,
        stream: TextIO | None = None,
        *,
        colors: int | bool = 1,
        max_length: int = 10240,
        levels: dict[str, int] | None = None,
        formatters: dict[str, Formatter] | None = None,
    ) -> None:
        super().__init__(
            colors=colors,
            max_length=max_length,
            levels=levels,
            formatters=formatters,
        )
        self.stream: TextIO = stream if stream is not None else sys.stderr

    def export(self, message: Message) -> None:
        text = Logger.format(self, message)
        tag = message.type.upper()
        name_tag = f"[{message.name}]"
        if self.colors:
            color_code = _hash_name_color(message.name, self.colors)
            colored_name = f"\033[38;5;{color_code}m{name_tag}\033[0m"
            self.stream.write(f"{tag} {colored_name} {text}\n")
        else:
            self.stream.write(f"{tag} {name_tag} {text}\n")
        self.stream.flush()


def _default_formatter_s(val: Any, _exp: Exporter, _msg: Message) -> str:
    return str(val)


def _default_formatter_d(val: Any, _exp: Exporter, _msg: Message) -> str:
    try:
        return str(math.trunc(float(val)))
    except (ValueError, TypeError):
        return "0"


def _default_formatter_f(val: Any, _exp: Exporter, _msg: Message) -> str:
    try:
        return str(float(val))
    except (ValueError, TypeError):
        return "0.0"


def _default_formatter_o(val: Any, _exp: Exporter, _msg: Message) -> str:
    try:
        return json.dumps(val)
    except (TypeError, ValueError, OverflowError):
        return str(val)


def _default_formatter_c(_val: Any, _exp: Exporter, _msg: Message) -> str:
    return ""


def _default_formatter_upper_c(val: Any, exp: Exporter, msg: Message) -> str:
    code = _hash_name_color(msg.name, exp.colors)
    if exp.colors:
        return f"\033[38;5;{code}m{val}\033[0m"
    return str(val)


# Terminal escape sequences are zero width: an ESC introducing a CSI
# (``\x1b[...``), an OSC (``\x1b]...``) or a two character escape must not be
# charged against ``max_length`` and must never be cut in half.
_ESCAPE_SEQUENCE_RE = re.compile(
    r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\|\Z)|\[[0-?]*[ -/]*\Z|[@-Z\\-_])"
)

DEFAULT_FORMATTERS: dict[str, Formatter] = {
    "s": _default_formatter_s,
    "d": _default_formatter_d,
    "i": _default_formatter_d,
    "f": _default_formatter_f,
    "o": _default_formatter_o,
    "O": _default_formatter_o,
    "c": _default_formatter_c,
    "C": _default_formatter_upper_c,
}


def _is_sgr(sequence: str) -> bool:
    """True for ``Select Graphic Rendition`` sequences, i.e. colors/attributes."""
    return sequence.startswith("\x1b[") and sequence.endswith("m")


def _is_sgr_reset(sequence: str) -> bool:
    params = sequence[2:-1]
    return all(part in ("", "0") for part in params.split(";"))


def _visible_length(line: str) -> int:
    return len(_ESCAPE_SEQUENCE_RE.sub("", line))


def _clip_to_visible_width(line: str, max_length: int) -> str:
    """Clip ``line`` to ``max_length`` visible characters.

    Escape sequences stay whole and cost nothing against the budget. If an SGR
    attribute is still open at the cut point, a reset is appended so a clipped
    line cannot bleed color into whatever gets printed after it.
    """
    if _visible_length(line) <= max_length:
        return line

    parts: list[str] = []
    visible = 0
    pos = 0
    open_sgr = False
    while pos < len(line) and visible < max_length:
        match = _ESCAPE_SEQUENCE_RE.match(line, pos)
        if match is not None:
            sequence = match.group(0)
            if _is_sgr(sequence):
                open_sgr = not _is_sgr_reset(sequence)
            parts.append(sequence)
            pos = match.end()
            continue
        parts.append(line[pos])
        visible += 1
        pos += 1

    clipped = "".join(parts) + "..."
    if open_sgr:
        clipped += "\x1b[0m"
    return clipped


class Logger:
    """Namespace logger instance writing through LoggerService."""

    def __init__(
        self,
        name: str,
        service: LoggerService,
        level: int | None = None,
    ) -> None:
        self.name = name
        self.service = service
        self.level = level

    @staticmethod
    def format(exporter: Exporter, message: Message) -> str:
        args = list(message.args)
        if not args:
            return ""

        first = args[0]
        if isinstance(first, Exception):
            args[0] = str(first)
            fmt = "%s"
            args_to_format = args
        elif isinstance(first, str):
            fmt = args.pop(0)
            args_to_format = args
        else:
            fmt = "%o"
            args_to_format = args

        def _sub(m: re.Match[str]) -> str:
            token = m.group(1)
            if token == "%":
                return "%"
            formatter = exporter.formatters.get(token) or DEFAULT_FORMATTERS.get(token)
            if formatter and args_to_format:
                val = args_to_format.pop(0)
                return str(formatter(val, exporter, message))
            return m.group(0)

        formatted = re.sub(r"%([a-zA-Z%])", _sub, fmt)

        # Append remaining args
        if args_to_format:
            tail = []
            for arg in args_to_format:
                if isinstance(arg, (dict, list, tuple)):
                    tail.append(_default_formatter_o(arg, exporter, message))
                else:
                    tail.append(str(arg))
            formatted = f"{formatted} {' '.join(tail)}".strip()

        # Enforce max length per line, counting visible width only
        lines = formatted.split("\n")
        clipped = [_clip_to_visible_width(line, exporter.max_length) for line in lines]
        return "\n".join(clipped)

    def _log(
        self,
        type_name: str,
        level: int,
        *args: Any,
        _seen: frozenset[int] = frozenset(),
    ) -> None:
        if len(args) == 1 and isinstance(args[0], Exception):
            err = args[0]
            # ``__cause__`` is writable, so a chain can loop back on itself
            # (``raise a from b`` with ``b.__cause__ = a``). Recursing on it used
            # to raise RecursionError inside the log call, turning a crash in the
            # thing being reported into a flood of tracebacks. Following the
            # chain with a visited set logs the node that closes the loop.
            if err.__cause__ is not None and id(err) not in _seen:
                self._log(type_name, level, err.__cause__, _seen=_seen | {id(err)})
                return
            if hasattr(err, "exceptions"):
                sub_errors = list(err.exceptions)  # type: ignore[attr-defined]
                if sub_errors:
                    for sub_err in sub_errors:
                        self._log(type_name, level, sub_err, _seen=_seen | {id(err)})
                    return
                # Empty aggregate (or a duck-typed one): fall through and log
                # the aggregate itself instead of swallowing it

        sn = self.service._next_sn()
        ts = time.time()
        msg = Message(
            sn=sn,
            ts=ts,
            name=self.name,
            type=type_name,
            level=level,
            args=list(args),
        )

        # Store in buffer
        self.service._push_buffer(msg)

        # Export to exporters
        for exporter in list(self.service.exporters.values()):
            # Resolve the threshold with explicit ``None`` checks: ERROR is 0,
            # so an ``or`` chain treats the strictest level anyone can ask for
            # as "unset" and falls through to INFO, exporting exactly the
            # messages that level was meant to drop.
            target_level = exporter.levels.get(self.name)
            if target_level is None:
                target_level = exporter.levels.get("default")
            if target_level is None:
                target_level = self.level
            if target_level is None:
                target_level = self.service.default_level
            if level <= target_level:
                try:
                    exporter.export(msg)
                except Exception:
                    # One broken exporter must not starve the others nor
                    # propagate into the call site that logged.
                    logging.getLogger("anima.logger").exception(
                        "Error in exporter %r", exporter
                    )

    def error(self, *args: Any) -> None:
        self._log("error", LoggerLevel.ERROR, *args)

    def warn(self, *args: Any) -> None:
        self._log("warn", LoggerLevel.WARN, *args)

    warning = warn

    def info(self, *args: Any) -> None:
        self._log("info", LoggerLevel.INFO, *args)

    def debug(self, *args: Any) -> None:
        self._log("debug", LoggerLevel.DEBUG, *args)


class LoggerService(Service):
    """Central logging micro-kernel service."""

    def __init__(self, ctx: Context, config: dict[str, Any] | None = None) -> None:
        cfg = dict(config or {})
        super().__init__(ctx, "logger")
        self.buffer_size = cfg.get("buffer_size", 1000)
        self.buffer: list[Message] = []
        self.default_level: int = LoggerLevel.INFO
        self._sn_counter = 0
        self._exporter_id = 0
        self.exporters: dict[int, Exporter] = {}

    def _next_sn(self) -> int:
        self._sn_counter += 1
        return self._sn_counter

    def _push_buffer(self, msg: Message) -> None:
        self.buffer.append(msg)
        overflow = len(self.buffer) - self.buffer_size
        if overflow > 0:
            del self.buffer[:overflow]

    def exporter(self, exporter: Exporter) -> Disposer:
        """Register a message exporter with automatic reversible effect disposal."""
        self._exporter_id += 1
        eid = self._exporter_id
        self.exporters[eid] = exporter

        def _remove() -> None:
            self.exporters.pop(eid, None)

        return self.ctx.effect(lambda: _remove)

    def __call__(self, name: str | None = None, level: int | None = None) -> Logger:
        """Obtain a namespaced Logger handle."""
        intercept = self.resolve_intercept()
        final_name = name or intercept.get("name") or "root"
        final_level = level if level is not None else intercept.get("level")
        return Logger(final_name, self, final_level)

    def error(self, *args: Any) -> None:
        self("root").error(*args)

    def warn(self, *args: Any) -> None:
        self("root").warn(*args)

    warning = warn

    def info(self, *args: Any) -> None:
        self("root").info(*args)

    def debug(self, *args: Any) -> None:
        self("root").debug(*args)
