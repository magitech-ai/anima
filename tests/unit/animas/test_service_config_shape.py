"""Service constructors take (ctx, config) under the F16 invocation shape.

Every Service subclass must mount via ctx.use with the config dict
arriving as a single positional argument — a second constructor
parameter of any other meaning silently corrupts the service (the
config dict lands in it). These tests mount each service through the
component path and assert the parameter values actually land.
"""

from pathlib import Path

import pytest

from animas.core import Context
from animas.include import Include, IncludeError
from animas.loader import LoaderService
from animas.logger import LoggerService


@pytest.mark.asyncio
async def test_include_service_mounts_via_component(tmp_path: Path):
    # P1-7: Include is a file-backed tree, so mounting requires a path;
    # the old pathless mount no longer applies (fail-loud configuration).
    profile = tmp_path / "profile.yaml"
    profile.write_text("[]", encoding="utf-8")

    ctx = Context()
    await ctx.use(Include, {"path": str(profile)})
    await ctx.flush()

    svc = ctx.get("include")
    assert isinstance(svc, Include)
    assert svc.name == "include"


@pytest.mark.asyncio
async def test_include_service_mount_without_path_fails_loud():
    ctx = Context()
    fiber = ctx.use(Include)
    await ctx.flush()
    with pytest.raises(IncludeError, match="requires a 'path' string"):
        await fiber.await_settle()


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
