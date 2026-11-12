"""Command-line router, parser, and execution substrate for Anima microkernel.

Anima-original surface: not part of the ported kernel.
"""

from __future__ import annotations

import asyncio
import importlib.metadata
import inspect
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from anima.cli_create import Scaffold
from anima.core import Context, Disposer, Service
from anima.loader import LoaderService

BRACKET_PATTERN = re.compile(r"<([^<>]+)>|\[([^\[\]]+)\]")
OPTION_SPEC_PATTERN = re.compile(r"^(-+)([^\s,<=]+)")


class CliSyntaxError(Exception):
    """Raised when command-line syntax or parsing fails."""

    def __init__(self, message: str, command: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.command = command


@dataclass
class CliParam:
    """Specification of a positional argument or option parameter."""

    name: str
    kind: str  # 'argument' | 'option'
    type_name: str
    parse_fn: Callable[[str], Any]
    variadic: bool = False
    required: bool = False
    greedy: bool = False


@dataclass
class CliOption:
    """Specification of a command option flag."""

    source: str
    description: str
    names: list[str]
    param: CliParam | None = None
    default: Any = None
    hidden: bool = False


@dataclass
class Argv:
    """Parsed command-line arguments and options container."""

    command: CliCommand
    args: list[Any]
    options: dict[str, Any]


class CliCommand:
    """A command node supporting options, aliases, and execution actions."""

    def __init__(
        self,
        ctx: Context,
        cli: CliService,
        name: str,
        source: str,
        description: str,
        *,
        hidden: bool = False,
    ) -> None:
        self.ctx = ctx
        self.cli = cli
        self.name = name
        self.source = source
        self.description = description
        self.hidden = hidden
        self.arguments: list[CliParam] = []
        self.options: list[CliOption] = []
        self.option_dict: dict[str, CliOption] = {}
        self.aliases: set[str] = {name}
        self.action_handler: Callable[..., Any] | None = None

        # Auto-register on cli service
        self.cli._register_command(self)

    def dispose(self) -> None:
        """Unregister this command from the CLI service."""
        self.cli._unregister_command(self)

    def alias(self, alias_name: str) -> CliCommand:
        """Register an alternate invocation name for this command."""
        self.aliases.add(alias_name)
        self.cli._register_alias(alias_name, self)
        return self

    def option(
        self,
        source: str,
        description: str = "",
        *,
        default: Any = None,
        type: Any = None,
        hidden: bool = False,
    ) -> CliCommand:
        """Add an option flag to this command."""
        parts = [p.strip() for p in source.split(",")]
        names: list[str] = []
        param_spec = ""

        for part in parts:
            match = OPTION_SPEC_PATTERN.match(part)
            if not match:
                continue
            flag_name = match.group(2)
            # Normalize kebab-case to snake_case
            names.append(flag_name.replace("-", "_"))
            remainder = part[match.end() :].strip()
            if remainder:
                param_spec = remainder

        opt_param: CliParam | None = None
        if param_spec:
            parsed_params = self.cli.parse_params(param_spec, "option")
            if parsed_params:
                opt_param = parsed_params[0]
        elif type is not None:
            parse_fn = self.cli.get_type_parser(type)
            opt_param = CliParam(
                name="val",
                kind="option",
                type_name=str(type),
                parse_fn=parse_fn,
                required=True,
            )

        option = CliOption(
            source=source,
            description=description,
            names=names,
            param=opt_param,
            default=default,
            hidden=hidden,
        )

        self.options.append(option)
        for name in names:
            self.option_dict[name] = option

        return self

    def action(self, handler: Callable[..., Any]) -> CliCommand:
        """Assign the execution handler for this command."""
        self.action_handler = handler
        return self

    def parse(self, tokens: list[str]) -> Argv:
        """Parse raw tokens into typed arguments and options."""
        parsed_opts: dict[str, Any] = {}
        for opt in self.options:
            if opt.default is not None:
                for n in opt.names:
                    parsed_opts[n] = opt.default
            elif opt.param is None:
                for n in opt.names:
                    parsed_opts[n] = False

        pos_tokens: list[str] = []
        i = 0
        while i < len(tokens):
            tok = tokens[i]
            if tok.startswith("--"):
                key = tok[2:]
                val: str | None = None
                if "=" in key:
                    key, val = key.split("=", 1)
                norm_key = key.replace("-", "_")
                target_opt = self.option_dict.get(norm_key)
                if not target_opt:
                    raise CliSyntaxError(f"unknown option: --{key}", self.name)

                if target_opt.param is not None:
                    if val is None:
                        i += 1
                        if i >= len(tokens) or tokens[i].startswith("-"):
                            raise CliSyntaxError(
                                f"option '--{key}' requires an argument", self.name
                            )
                        val = tokens[i]
                    parsed_val = target_opt.param.parse_fn(val)
                    for n in target_opt.names:
                        parsed_opts[n] = parsed_val
                else:
                    for n in target_opt.names:
                        parsed_opts[n] = True
            elif tok.startswith("-") and len(tok) > 1 and not tok[1].isdigit():
                flag = tok[1:]
                norm_flag = flag.replace("-", "_")
                target_opt = self.option_dict.get(norm_flag)
                if not target_opt:
                    raise CliSyntaxError(f"unknown option: -{flag}", self.name)
                if target_opt.param is not None:
                    i += 1
                    if i >= len(tokens) or tokens[i].startswith("-"):
                        raise CliSyntaxError(
                            f"option '-{flag}' requires an argument", self.name
                        )
                    parsed_val = target_opt.param.parse_fn(tokens[i])
                    for n in target_opt.names:
                        parsed_opts[n] = parsed_val
                else:
                    for n in target_opt.names:
                        parsed_opts[n] = True
            else:
                pos_tokens.append(tok)
            i += 1

        # Match positional arguments
        parsed_args: list[Any] = []
        arg_idx = 0
        token_idx = 0

        while arg_idx < len(self.arguments):
            param = self.arguments[arg_idx]
            if param.variadic:
                if token_idx >= len(pos_tokens) and param.required:
                    raise CliSyntaxError(
                        f"missing required argument <...{param.name}>", self.name
                    )
                var_vals: list[Any] = []
                while token_idx < len(pos_tokens):
                    var_vals.append(param.parse_fn(pos_tokens[token_idx]))
                    token_idx += 1
                parsed_args.extend(var_vals)
                break
            else:
                if token_idx < len(pos_tokens):
                    parsed_args.append(param.parse_fn(pos_tokens[token_idx]))
                    token_idx += 1
                elif param.required:
                    raise CliSyntaxError(
                        f"missing required argument <{param.name}>", self.name
                    )
            arg_idx += 1

        return Argv(command=self, args=parsed_args, options=parsed_opts)

    async def execute(self, argv: Argv) -> Any:
        """Execute the command action handler."""
        if self.action_handler is None:
            return None
        res = self.action_handler(argv, *argv.args)
        if inspect.isawaitable(res):
            return await res
        return res


class CliService(Service):
    """Subsystem for registering, parsing, and dispatching command-line interfaces."""

    def __init__(self, ctx: Context, config: dict[str, Any] | None = None) -> None:
        cfg = dict(config or {})
        super().__init__(ctx, cfg.get("name", "cli"))
        self.commands: list[CliCommand] = []
        self.aliases: dict[str, CliCommand] = {}
        self.types: dict[str, Callable[[str], Any]] = {}

        self._init_builtin_types()
        self._init_builtin_commands()

    def _init_builtin_types(self) -> None:
        self.define("string", lambda s: s)
        self.define("text", lambda s: s)
        self.define("boolean", lambda s: s.lower() in ("true", "1", "yes"))

        def _parse_int(s: str) -> int:
            try:
                return int(s)
            except ValueError:
                raise CliSyntaxError(f"invalid integer value: '{s}'") from None

        def _parse_posint(s: str) -> int:
            val = _parse_int(s)
            if val <= 0:
                raise CliSyntaxError(f"expected positive integer: '{s}'")
            return val

        def _parse_natural(s: str) -> int:
            val = _parse_int(s)
            if val < 0:
                raise CliSyntaxError(f"expected natural integer (>= 0): '{s}'")
            return val

        def _parse_num(s: str) -> float:
            try:
                return float(s)
            except ValueError:
                raise CliSyntaxError(f"invalid number: '{s}'") from None

        self.define("integer", _parse_int)
        self.define("posint", _parse_posint)
        self.define("natural", _parse_natural)
        self.define("number", _parse_num)

    def _init_builtin_commands(self) -> None:
        # Default help command
        self.command("help [command:string]", "Show help for commands").action(
            lambda argv, cmd=None: self.render_help(cmd)
        )

        # Default run command
        async def _run_action(argv: Argv, profile: str = "anima.yaml") -> Any:
            loader = (
                self.ctx.loader if self.ctx.has("loader") else LoaderService(self.ctx)
            )
            path = Path(profile)
            if path.exists():
                loader.load(path)
                await self.ctx.flush()
                return True
            return False

        self.command("run [profile:string]", "Load and run an Anima profile").action(
            _run_action
        )

        # Default create command (scaffold a new project)
        def _create_action(argv: Argv, name: str = "my-app") -> Any:
            Scaffold({"name": name}).start()
            return True

        self.command("create [name:string]", "Scaffold a new project").action(
            _create_action
        )

    def define(self, name: str, parse_fn: Callable[[str], Any]) -> Disposer:
        """Register a custom argument/option type parser."""
        self.types[name] = parse_fn

        def _dispose() -> None:
            self.types.pop(name, None)

        self.ctx.effect(lambda: _dispose)
        return _dispose

    def get_type_parser(self, type_spec: Any) -> Callable[[str], Any]:
        """Resolve a type specifier to a parse callable."""
        if callable(type_spec):
            return cast(Callable[[str], Any], type_spec)
        if isinstance(type_spec, str):
            if type_spec in self.types:
                return self.types[type_spec]
            raise ValueError(f"Unknown CLI type '{type_spec}'")
        return lambda s: s

    def parse_params(self, source: str, kind: str) -> list[CliParam]:
        """Extract positional or option parameters from syntax brackets."""
        params: list[CliParam] = []
        for match in BRACKET_PATTERN.finditer(source):
            raw = match.group(1) or match.group(2)
            required = match.group(1) is not None
            variadic = False
            if raw.startswith("..."):
                variadic = True
                raw = raw[3:]

            if ":" in raw:
                p_name, p_type = raw.split(":", 1)
            else:
                p_name, p_type = raw, "string"

            parse_fn = self.get_type_parser(p_type)
            params.append(
                CliParam(
                    name=p_name.strip(),
                    kind=kind,
                    type_name=p_type.strip(),
                    parse_fn=parse_fn,
                    variadic=variadic,
                    required=required,
                )
            )
        return params

    def command(
        self, spec: str, description: str = "", *, hidden: bool = False
    ) -> CliCommand:
        """Declare a new command route with argument and option specifications."""
        parts = spec.strip().split()
        cmd_path: list[str] = []
        arg_spec_parts: list[str] = []

        for p in parts:
            if p.startswith(("<", "[")):
                arg_spec_parts.append(p)
            else:
                cmd_path.append(p)

        cmd_name = ".".join(cmd_path)
        cmd = CliCommand(
            self.ctx,
            self,
            name=cmd_name,
            source=spec,
            description=description,
            hidden=hidden,
        )
        if arg_spec_parts:
            cmd.arguments = self.parse_params(" ".join(arg_spec_parts), "argument")

        return cmd

    def _register_command(self, cmd: CliCommand) -> None:
        self.commands.append(cmd)
        self.aliases[cmd.name] = cmd

    def _unregister_command(self, cmd: CliCommand) -> None:
        if cmd in self.commands:
            self.commands.remove(cmd)
        for alias in list(cmd.aliases):
            self.aliases.pop(alias, None)

    def _register_alias(self, alias: str, cmd: CliCommand) -> None:
        self.aliases[alias] = cmd

    def render_help(self, command_name: str | None = None) -> str:
        """Format human-readable CLI help text."""
        if command_name:
            norm_name = command_name.replace(" ", ".")
            cmd = self.aliases.get(norm_name)
            if not cmd:
                return f"Error: command '{command_name}' not found"

            lines = [
                f"Command: {cmd.source}",
                f"Description: {cmd.description or 'No description'}",
                "",
                "Usage:",
                f"  anima {cmd.name.replace('.', ' ')} [options]",
            ]
            if cmd.arguments:
                lines.append("\nArguments:")
                for arg in cmd.arguments:
                    req_str = "required" if arg.required else "optional"
                    lines.append(f"  {arg.name} ({arg.type_name}, {req_str})")
            if cmd.options:
                lines.append("\nOptions:")
                for opt in cmd.options:
                    lines.append(f"  {opt.source}    {opt.description}")
            return "\n".join(lines)

        lines = [
            "Anima Command-Line Interface",
            "",
            "Usage: anima <command> [options]",
            "",
            "Commands:",
        ]
        visible_cmds = [c for c in self.commands if not c.hidden and c.name]
        visible_cmds.sort(key=lambda c: c.name)
        for c in visible_cmds:
            display_name = c.name.replace(".", " ")
            lines.append(f"  {display_name:<24} {c.description}")

        lines.append(
            "\nRun 'anima help <command>' or 'anima <command> --help' for command details."
        )
        return "\n".join(lines)

    async def execute(self, tokens: list[str]) -> Any:
        """Route and execute a command from a list of string arguments."""
        if not tokens or tokens == ["--help"] or tokens == ["-h"]:
            return self.render_help()

        # Check for help flag on subcommand
        if "-h" in tokens or "--help" in tokens:
            cmd_tokens = [t for t in tokens if t not in ("-h", "--help")]
            cmd_name = " ".join(cmd_tokens)
            return self.render_help(cmd_name if cmd_name else None)

        # Resolve command path (greedy token match)
        resolved_cmd: CliCommand | None = None
        cmd_length = 0

        for length in range(len(tokens), 0, -1):
            candidate = ".".join(tokens[:length])
            if candidate in self.aliases:
                resolved_cmd = self.aliases[candidate]
                cmd_length = length
                break

        if resolved_cmd is None:
            raise CliSyntaxError(f"command '{tokens[0]}' not found")

        rem_tokens = tokens[cmd_length:]
        argv = resolved_cmd.parse(rem_tokens)
        return await resolved_cmd.execute(argv)

    async def execute_argv(self, argv: list[str] | None = None) -> Any:
        """Dispatch arguments directly from sys.argv."""
        tokens = sys.argv[1:] if argv is None else list(argv)
        return await self.execute(tokens)


async def main(args: list[str] | None = None) -> int:
    """CLI application entrypoint."""
    tokens = sys.argv[1:] if args is None else list(args)

    if "--version" in tokens or "-v" in tokens:
        try:
            ver = importlib.metadata.version("anima")
        except Exception:  # noqa: BLE001
            ver = "0.2.0"
        print(f"anima {ver}")
        return 0

    ctx = Context()
    cli = CliService(ctx)

    try:
        output = await cli.execute(tokens)
        if output:
            print(output)
        return 0
    except CliSyntaxError as err:
        print(f"Error: {err.message}", file=sys.stderr)
        return 1
    except Exception as err:  # noqa: BLE001
        print(f"Internal error: {err}", file=sys.stderr)
        return 1


def console_main() -> int:
    """Synchronous console-script entrypoint."""
    return asyncio.run(main())


if __name__ == "__main__":  # pragma: no cover
    sys.exit(asyncio.run(main()))
