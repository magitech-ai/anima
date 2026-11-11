import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from anima.cli import CliCommand, CliService, CliSyntaxError, main
from anima.core import Context


@pytest.mark.asyncio
async def test_cli_service_registration():
    ctx = Context()
    cli = CliService(ctx)
    assert ctx.has("cli")
    assert ctx.cli == cli


@pytest.mark.asyncio
async def test_command_options_and_execution():
    ctx = Context()
    cli = CliService(ctx)

    called = []

    cmd = (
        cli.command("greet <name:string> [greeting:string]", "Greet someone")
        .option("-c, --count <num:integer>", "Number of times to greet", default=1)
        .option("-u, --uppercase", "Convert greeting to uppercase")
        .action(
            lambda argv, name, greeting="Hello": called.append(
                (name, greeting, argv.options)
            )
        )
    )
    assert isinstance(cmd, CliCommand)
    assert cmd.description == "Greet someone"

    # Execute with positional and options
    await cli.execute(["greet", "Alice", "-c", "3", "--uppercase"])
    assert len(called) == 1
    name, greeting, opts = called[0]
    assert name == "Alice"
    assert greeting == "Hello"
    assert opts["count"] == 3
    assert opts["uppercase"] is True

    # Execute with positional greeting override
    await cli.execute(["greet", "Bob", "Hi", "--count=2"])
    assert len(called) == 2
    name, greeting, opts = called[1]
    assert name == "Bob"
    assert greeting == "Hi"
    assert opts["count"] == 2


@pytest.mark.asyncio
async def test_command_variadic_args_and_types():
    ctx = Context()
    cli = CliService(ctx)

    results = []
    cli.command("sum <...nums:integer>", "Sum integer numbers").action(
        lambda argv, *nums: results.append(sum(nums))
    )

    await cli.execute(["sum", "1", "2", "3", "4"])
    assert results == [10]

    # Type failure
    with pytest.raises(CliSyntaxError) as exc_info:
        await cli.execute(["sum", "1", "not_a_number"])
    assert "integer" in str(exc_info.value)


@pytest.mark.asyncio
async def test_subcommand_dispatch():
    ctx = Context()
    cli = CliService(ctx)

    out = []
    cli.command("user create <username:string>", "Create user").action(
        lambda argv, username: out.append(f"created {username}")
    )
    cli.command("user delete <username:string>", "Delete user").action(
        lambda argv, username: out.append(f"deleted {username}")
    )

    await cli.execute(["user", "create", "john"])
    assert out == ["created john"]

    await cli.execute(["user", "delete", "john"])
    assert out == ["created john", "deleted john"]


@pytest.mark.asyncio
async def test_command_disposal_and_aliases():
    ctx = Context()
    cli = CliService(ctx)

    cmd = cli.command("echo <msg:string>", "Echo message")
    cmd.alias("repeat")
    runs = []
    cmd.action(lambda argv, msg: runs.append(msg))

    await cli.execute(["repeat", "test-msg"])
    assert runs == ["test-msg"]

    # Dispose command
    cmd.dispose()
    with pytest.raises(CliSyntaxError):
        await cli.execute(["echo", "hello"])

    with pytest.raises(CliSyntaxError):
        await cli.execute(["repeat", "hello"])


@pytest.mark.asyncio
async def test_help_generation_and_flags():
    ctx = Context()
    cli = CliService(ctx)

    cli.command("build <target:string>", "Build a project artifact").option(
        "-r, --release", "Build in release mode"
    )

    # Bare help returns general command listing
    res = await cli.execute(["--help"])
    assert "build" in res
    assert "Build a project artifact" in res

    # Command help
    res_cmd = await cli.execute(["help", "build"])
    assert "build" in res_cmd
    assert "target" in res_cmd
    assert "--release" in res_cmd

    # Help with -h
    res_h = await cli.execute(["build", "-h"])
    assert "build" in res_h
    assert "target" in res_h


@pytest.mark.asyncio
async def test_error_formatting_and_missing_arguments():
    ctx = Context()
    cli = CliService(ctx)

    cli.command("deploy <env:string>", "Deploy environment")

    # Missing required argument
    with pytest.raises(CliSyntaxError) as exc:
        await cli.execute(["deploy"])
    assert "missing required argument" in str(exc.value)

    # Unknown command
    with pytest.raises(CliSyntaxError) as exc2:
        await cli.execute(["nonexistent"])
    assert "not found" in str(exc2.value)


@pytest.mark.asyncio
async def test_custom_type_definition():
    ctx = Context()
    cli = CliService(ctx)

    def parse_hex(val: str) -> int:
        return int(val, 16)

    disposer = cli.define("hex", parse_hex)

    recorded = []
    cli.command("dump <val:hex>").action(lambda argv, val: recorded.append(val))

    await cli.execute(["dump", "0xff"])
    assert recorded == [255]

    # Unregister type via disposer
    disposer()
    with pytest.raises(ValueError):
        cli.command("dump2 <val:hex>")


@pytest.mark.asyncio
async def test_run_command_profile_integration(tmp_path: Path):
    profile_path = tmp_path / "test_profile.yaml"
    profile_path.write_text("components:\n  - timer\n", encoding="utf-8")

    ctx = Context()
    cli = CliService(ctx)

    loaded = []
    ctx.on("internal/plugin", lambda fib: loaded.append(fib.name))

    # Test run command
    await cli.execute(["run", str(profile_path)])
    # loader was invoked
    assert ctx.has("loader")
    assert ctx.has("timer")


@pytest.mark.asyncio
async def test_main_cli_entrypoint(monkeypatch):
    # Test --version
    with patch("sys.stdout"):
        exit_code = await main(["--version"])
        assert exit_code == 0

    # Test unknown command returns 1
    with patch("sys.stderr"):
        exit_code = await main(["unknown_cmd_foo"])
        assert exit_code == 1


@pytest.mark.asyncio
async def test_cli_option_edge_cases_and_parsing_errors():
    ctx = Context()
    cli = CliService(ctx)

    cmd = (
        cli.command("test-cmd <...files:string>")
        .option("-o, --output <path:string>", "Output file")
        .option("-f, --force", "Force mode")
        .option("-t, --typed", "Typed opt", type="integer")
        .option("-c, --custom", "Custom fn", type=lambda s: s.upper())
        .option("-d, --default-type", "Default obj type", type=object())
    )

    # Unknown long option
    with pytest.raises(CliSyntaxError) as exc1:
        await cli.execute(["test-cmd", "--unknown-opt"])
    assert "unknown option: --unknown-opt" in str(exc1.value)

    # Unknown short option
    with pytest.raises(CliSyntaxError) as exc2:
        await cli.execute(["test-cmd", "-z"])
    assert "unknown option: -z" in str(exc2.value)

    # Missing arg for long option (at end)
    with pytest.raises(CliSyntaxError) as exc3:
        await cli.execute(["test-cmd", "--output"])
    assert "option '--output' requires an argument" in str(exc3.value)

    # Missing arg for long option (followed by another option)
    with pytest.raises(CliSyntaxError) as exc4:
        await cli.execute(["test-cmd", "--output", "-f"])
    assert "option '--output' requires an argument" in str(exc4.value)

    # Missing arg for short option (at end)
    with pytest.raises(CliSyntaxError) as exc5:
        await cli.execute(["test-cmd", "-o"])
    assert "option '-o' requires an argument" in str(exc5.value)

    # Missing arg for short option (followed by another option)
    with pytest.raises(CliSyntaxError) as exc6:
        await cli.execute(["test-cmd", "-o", "-f"])
    assert "option '-o' requires an argument" in str(exc6.value)

    # Missing required variadic argument
    with pytest.raises(CliSyntaxError) as exc7:
        await cli.execute(["test-cmd"])
    assert "missing required argument <...files>" in str(exc7.value)

    # Successful short flag and custom typed option
    executed = []
    cmd.action(lambda argv, *files: executed.append((argv.options, files)))
    await cli.execute(
        [
            "test-cmd",
            "-f",
            "-t",
            "42",
            "-c",
            "hello",
            "-d",
            "val",
            "file1.txt",
            "file2.txt",
        ]
    )
    assert len(executed) == 1
    opts, files = executed[0]
    assert opts["force"] is True
    assert opts["typed"] == 42
    assert opts["custom"] == "HELLO"
    assert opts["default_type"] == "val"
    assert files == ("file1.txt", "file2.txt")

    # Command with no action handler returns None
    cli.command("noop")
    res = await cli.execute(["noop"])
    assert res is None


@pytest.mark.asyncio
async def test_cli_types_and_validation():
    ctx = Context()
    cli = CliService(ctx)

    rec = []
    cli.command(
        "types <pi:posint> <nat:natural> <num:number> <b:boolean> <t:text>"
    ).action(lambda argv, *args: rec.append(args))

    await cli.execute(["types", "5", "0", "3.14", "yes", "some long text"])
    assert rec == [(5, 0, 3.14, True, "some long text")]

    # posint validation failure (0 or negative)
    with pytest.raises(CliSyntaxError) as ep1:
        await cli.execute(["types", "0", "0", "1", "true", "x"])
    assert "positive integer" in str(ep1.value)

    with pytest.raises(CliSyntaxError) as ep2:
        await cli.execute(["types", "-5", "0", "1", "true", "x"])
    assert "positive integer" in str(ep2.value)

    # natural validation failure (< 0)
    with pytest.raises(CliSyntaxError) as en1:
        await cli.execute(["types", "1", "-1", "1", "true", "x"])
    assert "natural integer" in str(en1.value)

    # number validation failure
    with pytest.raises(CliSyntaxError) as enum1:
        await cli.execute(["types", "1", "0", "not_float", "true", "x"])
    assert "invalid number" in str(enum1.value)

    # Run command when profile file does not exist
    run_res = await cli.execute(["run", "nonexistent_profile.yaml"])
    assert run_res is False


@pytest.mark.asyncio
async def test_help_render_hidden_and_empty_desc():
    ctx = Context()
    cli = CliService(ctx)

    # Command with no description and hidden command
    cli.command("hidden-cmd", "secret", hidden=True)
    cli.command("nodesc", "")

    general_help = cli.render_help()
    assert "hidden-cmd" not in general_help
    assert "nodesc" in general_help

    # Specific command help for nodesc
    nodesc_help = cli.render_help("nodesc")
    assert "No description" in nodesc_help

    # Subcommand help via -h with args
    sub_help = await cli.execute(["help", "nonexistent_target"])
    assert "Error: command 'nonexistent_target' not found" in sub_help


@pytest.mark.asyncio
async def test_main_cli_execution_branches(monkeypatch):
    ctx = Context()
    cli = CliService(ctx)

    # Test execute_argv default sys.argv
    monkeypatch.setattr("sys.argv", ["anima", "--help"])
    output = await cli.execute_argv()
    assert "Anima Command-Line Interface" in output

    # Test main version exception fallback
    with (
        patch("importlib.metadata.version", side_effect=Exception("no pkg")),
        patch("sys.stdout"),
    ):
        exit_code = await main(["-v"])
        assert exit_code == 0

    # Test main success with command returning a string
    def mock_cmd(argv):
        return "result output"

    with patch.object(CliService, "_init_builtin_commands"):
        pass

    # Test main command producing stdout output
    with patch("sys.stdout"):
        # 'help' returns string
        exit_code = await main(["help"])
        assert exit_code == 0

    # Test main with unexpected exception
    with (
        patch.object(CliService, "execute", side_effect=RuntimeError("internal crash")),
        patch("sys.stderr"),
    ):
        exit_code = await main(["foo"])
        assert exit_code == 1

    # Test main with command returning None (no output printed)
    with (
        patch.object(CliService, "execute", return_value=None),
        patch("sys.stdout") as mock_out,
    ):
        exit_code = await main(["help"])
        assert exit_code == 0
        mock_out.write.assert_not_called()


@pytest.mark.asyncio
async def test_cli_additional_syntax_branches():
    ctx = Context()
    cli = CliService(ctx)

    # 1. Option with invalid segment and no param spec
    cmd = cli.command("syntax <bare_arg>")
    cmd.option("-x, invalid_part, -y")
    # Option with param spec without brackets (e.g. malformed or unbracketed)
    cmd.option("-n no_brackets")
    cmd.option("-o, --output <dest:string>")
    cmd.action(lambda argv, bare_arg: bare_arg)

    # 2. Long option with space-separated argument
    res = await cli.execute(["syntax", "--output", "file.log", "hello_world"])
    assert res == "hello_world"

    # 3. Double dispose
    cmd.dispose()
    cmd.dispose()


def test_console_main_runs_help_synchronously(monkeypatch, capsys):
    """console_main is a sync entrypoint: help exits 0 without an event loop."""
    from anima.cli import console_main

    monkeypatch.setattr(sys, "argv", ["anima", "help"])
    assert console_main() == 0
    assert "Anima Command-Line Interface" in capsys.readouterr().out


def test_console_main_returns_one_for_unknown_command(monkeypatch, capsys):
    """console_main propagates the async main exit code."""
    from anima.cli import console_main

    monkeypatch.setattr(sys, "argv", ["anima", "unknown_cmd_foo"])
    assert console_main() == 1
    assert "Error:" in capsys.readouterr().err


def test_module_execution_has_no_runtime_warning():
    """python -m anima.cli runs without the double-import RuntimeWarning."""
    result = subprocess.run(
        [sys.executable, "-m", "anima.cli", "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert "RuntimeWarning" not in result.stderr
