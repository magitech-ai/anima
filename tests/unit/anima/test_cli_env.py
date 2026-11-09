"""Unit tests for CLI Env cascading loader extension."""

import os
from pathlib import Path

import pytest

from anima.cli_env import CliEnv, expand_variables, load_env_cascade, parse_dotenv
from anima.core import Context


def test_parse_dotenv_basic():
    content = """
    # Comment line
    FOO=bar
    BAZ="hello world"
    QUX='single quoted'
    EXPORTED=true
    export EXPORTED2=value2
    EMPTY=
    SPACED = spaced_val 
    """
    parsed = parse_dotenv(content)
    assert parsed["FOO"] == "bar"
    assert parsed["BAZ"] == "hello world"
    assert parsed["QUX"] == "single quoted"
    assert parsed["EXPORTED"] == "true"
    assert parsed["EXPORTED2"] == "value2"
    assert parsed["EMPTY"] == ""
    assert parsed["SPACED"] == "spaced_val"


def test_expand_variables():
    env = {
        "HOST": "localhost",
        "PORT": "8080",
        "URL": "http://${HOST}:${PORT}/api",
        "FALLBACK": "${MISSING:-default_val}",
        "SIMPLE": "$HOST",
    }
    process_env = {"EXTRA": "world"}
    expanded = expand_variables(env, process_env)

    assert expanded["URL"] == "http://localhost:8080/api"
    assert expanded["FALLBACK"] == "default_val"
    assert expanded["SIMPLE"] == "localhost"


def test_load_env_cascade(tmp_path: Path):
    # 1. Base .env
    (tmp_path / ".env").write_text("A=1\nB=1\nC=1\nD=1\n", encoding="utf-8")
    # 2. .env.local
    (tmp_path / ".env.local").write_text("B=2\n", encoding="utf-8")
    # 3. .env.test
    (tmp_path / ".env.test").write_text("C=3\n", encoding="utf-8")
    # 4. .env.test.local
    (tmp_path / ".env.test.local").write_text("D=4\n", encoding="utf-8")

    merged = load_env_cascade(base_dir=tmp_path, mode="test")
    assert merged["A"] == "1"
    assert merged["B"] == "2"
    assert merged["C"] == "3"
    assert merged["D"] == "4"


def test_parse_dotenv_multiline_and_comments():
    content = (
        'MULTI_DOUBLE="hello\nmiddle\nworld"\n'
        "MULTI_SINGLE='foo\nmiddle\nbar'\n"
        "INLINE_COMMENT=val # inline comment\n"
        "NO_EQUALS_HERE\n"
        'UNCLOSED_D="line1\nline2\n'
    )
    parsed = parse_dotenv(content)
    assert parsed["MULTI_DOUBLE"] == "hello\nmiddle\nworld"
    assert parsed["MULTI_SINGLE"] == "foo\nmiddle\nbar"
    assert parsed["INLINE_COMMENT"] == "val"
    assert parsed["UNCLOSED_D"] == "line1\nline2"

    content_s = "UNCLOSED_S='line1\nline2\n"
    parsed_s = parse_dotenv(content_s)
    assert parsed_s["UNCLOSED_S"] == "line1\nline2"


def test_expand_variables_unresolved():
    env = {
        "A": "${NOT_FOUND}",
    }
    expanded = expand_variables(env, {})
    assert expanded["A"] == ""


@pytest.mark.asyncio
async def test_cli_env_plugin(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("EXISTING_KEY", "original_value")
    monkeypatch.delenv("NEW_KEY", raising=False)

    (tmp_path / ".env").write_text(
        "EXISTING_KEY=new_val\nNEW_KEY=created_val\nEXPANDED=${EXISTING_KEY}_suffix\n",
        encoding="utf-8",
    )

    ctx = Context()
    fiber = ctx.plugin(CliEnv, base_dir=tmp_path)
    await fiber.load()

    # 1. Check ctx.env
    assert hasattr(ctx, "env")
    assert ctx.env["NEW_KEY"] == "created_val"
    assert ctx.env["EXPANDED"] == "original_value_suffix"

    # 2. Check process.env (os.environ) - does not overwrite existing key!
    assert os.environ["EXISTING_KEY"] == "original_value"
    assert os.environ["NEW_KEY"] == "created_val"

    # 3. Disposer cleanup
    await fiber.dispose()
    assert "NEW_KEY" not in os.environ

    # 4. Top-level apply helper
    from anima.cli_env import apply as cli_env_apply

    ctx2 = Context()
    cli_env_apply(ctx2, base_dir=tmp_path)
    assert ctx2.env["EXISTING_KEY"] == "original_value"


def test_cli_env_node_env_mode(tmp_path: Path, monkeypatch):
    # Note: mode resolution honors NODE_ENV only (not ANIMA_ENV/APP_ENV).
    monkeypatch.setenv("NODE_ENV", "production")
    monkeypatch.delenv("ANIMA_ENV", raising=False)
    monkeypatch.delenv("APP_ENV", raising=False)
    (tmp_path / ".env").write_text("BASE=1\n", encoding="utf-8")
    (tmp_path / ".env.production").write_text("MODE=production\n", encoding="utf-8")

    merged = load_env_cascade(base_dir=tmp_path)
    assert merged["BASE"] == "1"
    assert merged["MODE"] == "production"
