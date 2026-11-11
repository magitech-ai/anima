"""Service constructors take (ctx, config) under the F16 invocation shape.

Every Service subclass must mount via ctx.use with the config dict
arriving as a single positional argument — a second constructor
parameter of any other meaning silently corrupts the service (the
config dict lands in it). These tests mount each service through the
component path and assert the parameter values actually land.
"""

from pathlib import Path

import pytest

from anima.cli import CliService
from anima.core import Context
from anima.http import HttpService
from anima.include import IncludeService
from anima.loader import LoaderService
from anima.logger import LoggerService
from anima.server import ServerService


@pytest.mark.asyncio
async def test_cli_service_mounts_via_component_with_default_name():
    ctx = Context()
    await ctx.use(CliService)
    await ctx.flush()

    svc = ctx.get("cli")
    assert isinstance(svc, CliService)
    assert svc.name == "cli"
    assert svc.commands


@pytest.mark.asyncio
async def test_cli_service_mounts_via_component_with_config_name():
    ctx = Context()
    await ctx.use(CliService, {"name": "custom_cli"})
    await ctx.flush()

    svc = ctx.get("custom_cli")
    assert isinstance(svc, CliService)
    assert svc.name == "custom_cli"


@pytest.mark.asyncio
async def test_http_service_mounts_via_component():
    ctx = Context()
    await ctx.use(HttpService)
    await ctx.flush()

    svc = ctx.get("http")
    assert isinstance(svc, HttpService)
    assert svc.name == "http"


@pytest.mark.asyncio
async def test_server_service_mounts_via_component():
    ctx = Context()
    await ctx.use(ServerService)
    await ctx.flush()

    svc = ctx.get("server")
    assert isinstance(svc, ServerService)
    assert svc.name == "server"


@pytest.mark.asyncio
async def test_include_service_mounts_via_component():
    ctx = Context()
    await ctx.use(IncludeService)
    await ctx.flush()

    svc = ctx.get("include")
    assert isinstance(svc, IncludeService)
    assert svc.name == "include"


@pytest.mark.asyncio
async def test_logger_service_mounts_via_component_with_default_buffer():
    ctx = Context()
    await ctx.use(LoggerService)
    await ctx.flush()

    svc = ctx.get("logger")
    assert isinstance(svc, LoggerService)
    assert svc.buffer_size == 1000
    svc.info("hello")
    assert len(svc.buffer) == 1
    assert svc.buffer[0].args == ["hello"]


@pytest.mark.asyncio
async def test_logger_service_mounts_via_component_with_config_buffer_size():
    ctx = Context()
    await ctx.use(LoggerService, {"buffer_size": 3})
    await ctx.flush()

    svc = ctx.get("logger")
    assert isinstance(svc, LoggerService)
    assert svc.buffer_size == 3
    for i in range(5):
        svc.info(f"message {i}")
    assert len(svc.buffer) == 3
    assert svc.buffer[0].args == ["message 2"]


@pytest.mark.asyncio
async def test_loader_service_mounts_via_component_with_config_filename(
    tmp_path: Path,
):
    profile = tmp_path / "profile.yaml"
    profile.write_text("name: test\n", encoding="utf-8")

    ctx = Context()
    await ctx.use(LoaderService, {"filename": str(profile)})
    await ctx.flush()

    svc = ctx.get("loader")
    assert isinstance(svc, LoaderService)
    assert svc.filename == profile.resolve()


@pytest.mark.asyncio
async def test_loader_service_mounts_via_component_without_filename():
    ctx = Context()
    await ctx.use(LoaderService)
    await ctx.flush()

    svc = ctx.get("loader")
    assert isinstance(svc, LoaderService)
    assert svc.filename is None
