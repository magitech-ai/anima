import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from anima.core import Context
from anima.pkg import PkgResult, PkgService


@pytest.mark.asyncio
async def test_pkg_service_init_and_component_mount():
    ctx = Context()
    ctx.use(PkgService, uv_path="/custom/bin/uv")
    await ctx.flush()

    pkg: PkgService = ctx.pkg
    assert pkg.uv_path == "/custom/bin/uv"

    # Default init without arguments uses shutil.which or fallback
    pkg_default = PkgService(ctx)
    assert pkg_default.uv_path is not None


@pytest.mark.asyncio
async def test_pkg_run_command_success_and_failure():
    ctx = Context()
    pkg = PkgService(ctx, {"uv_path": "uv"})

    mock_proc = MagicMock()
    mock_proc.returncode = 0
    mock_proc.communicate = AsyncMock(return_value=(b"output text", b""))

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        res = await pkg._run_command(["version"])
        assert res.success is True
        assert res.exit_code == 0
        assert res.stdout == "output text"
        assert res.command == ["uv", "version"]

    mock_proc_fail = MagicMock()
    mock_proc_fail.returncode = 1
    mock_proc_fail.communicate = AsyncMock(return_value=(b"", b"error occurred"))

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc_fail):
        res_fail = await pkg._run_command(["invalid"])
        assert res_fail.success is False
        assert res_fail.exit_code == 1
        assert res_fail.stderr == "error occurred"


@pytest.mark.asyncio
async def test_pkg_add_and_remove():
    ctx = Context()
    pkg = PkgService(ctx)

    events: list[tuple[str, Any]] = []
    ctx.on("pkg/add", lambda pkgs, res: events.append(("add", pkgs)))
    ctx.on("pkg/remove", lambda pkgs, res: events.append(("remove", pkgs)))

    with patch.object(pkg, "_run_command") as mock_run:
        mock_run.return_value = PkgResult(True, 0, "ok", "", ["uv"])

        # Add single string package
        res1 = await pkg.add("requests")
        assert res1.success is True
        mock_run.assert_called_with(["add", "requests"], cwd=None, timeout=None)
        assert ("add", ["requests"]) in events

        # Add list with dev=True
        events.clear()
        res2 = await pkg.add(["pytest", "ruff"], dev=True)
        assert res2.success is True
        mock_run.assert_called_with(
            ["add", "--dev", "pytest", "ruff"], cwd=None, timeout=None
        )
        assert ("add", ["pytest", "ruff"]) in events

        # Remove single string package
        events.clear()
        res3 = await pkg.remove("requests")
        assert res3.success is True
        mock_run.assert_called_with(["remove", "requests"], cwd=None, timeout=None)
        assert ("remove", ["requests"]) in events

        # Remove list
        events.clear()
        res4 = await pkg.remove(["pytest", "ruff"])
        assert res4.success is True
        mock_run.assert_called_with(
            ["remove", "pytest", "ruff"], cwd=None, timeout=None
        )
        assert ("remove", ["pytest", "ruff"]) in events


@pytest.mark.asyncio
async def test_pkg_sync_and_lock():
    ctx = Context()
    pkg = PkgService(ctx)

    events: list[str] = []
    ctx.on("pkg/sync", lambda res: events.append("sync"))
    ctx.on("pkg/lock", lambda res: events.append("lock"))

    with patch.object(pkg, "_run_command") as mock_run:
        mock_run.return_value = PkgResult(True, 0, "ok", "", ["uv"])

        # Sync dev=True
        await pkg.sync(dev=True)
        mock_run.assert_called_with(["sync"], cwd=None, timeout=None)
        assert "sync" in events

        # Sync dev=False
        await pkg.sync(dev=False)
        mock_run.assert_called_with(["sync", "--no-dev"], cwd=None, timeout=None)

        # Lock check=False
        await pkg.lock(check=False)
        mock_run.assert_called_with(["lock"], cwd=None, timeout=None)
        assert "lock" in events

        # Lock check=True
        await pkg.lock(check=True)
        mock_run.assert_called_with(["lock", "--check"], cwd=None, timeout=None)


@pytest.mark.asyncio
async def test_pkg_list_installed():
    ctx = Context()
    pkg = PkgService(ctx)

    with patch.object(pkg, "_run_command") as mock_run:
        # Success JSON
        pkg_data = [
            {"name": "pytest", "version": "8.0.0"},
            {"name": "ruff", "version": "0.3.0"},
            {"incomplete": "item"},
            "not_a_dict",
        ]
        mock_run.return_value = PkgResult(True, 0, json.dumps(pkg_data), "", ["uv"])
        installed = await pkg.list_installed()
        assert installed == {"pytest": "8.0.0", "ruff": "0.3.0"}

        # Command failed
        mock_run.return_value = PkgResult(False, 1, "", "error", ["uv"])
        assert await pkg.list_installed() == {}

        # Malformed JSON
        mock_run.return_value = PkgResult(True, 0, "not json {[[", "", ["uv"])
        assert await pkg.list_installed() == {}

        # Non-list JSON
        mock_run.return_value = PkgResult(True, 0, json.dumps({"a": "b"}), "", ["uv"])
        assert await pkg.list_installed() == {}


@pytest.mark.asyncio
async def test_pkg_error_handling_and_timeout():
    ctx = Context()
    pkg = PkgService(ctx, {"uv_path": "non_existent_binary"})

    # FileNotFoundError
    with patch("asyncio.create_subprocess_exec", side_effect=FileNotFoundError):
        res_not_found = await pkg._run_command(["sync"])
        assert res_not_found.success is False
        assert "not found" in res_not_found.stderr

    # Generic unexpected exception
    with patch(
        "asyncio.create_subprocess_exec", side_effect=PermissionError("no access")
    ):
        res_err = await pkg._run_command(["sync"])
        assert res_err.success is False
        assert "no access" in res_err.stderr

    # Timeout handling
    mock_proc = MagicMock()
    mock_proc.communicate = AsyncMock(side_effect=asyncio.TimeoutError)
    mock_proc.kill = MagicMock()
    mock_proc.wait = AsyncMock()

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        res_timeout = await pkg._run_command(["sync"], timeout=0.01)
        assert res_timeout.success is False
        assert "timed out after 0.01" in res_timeout.stderr
        mock_proc.kill.assert_called_once()

    # Event broadcast exception in add, remove, sync, lock
    with (
        patch.object(ctx, "parallel", side_effect=RuntimeError("bus error")),
        patch.object(
            pkg, "_run_command", return_value=PkgResult(True, 0, "ok", "", ["uv"])
        ),
    ):
        res_add = await pkg.add("pkg")
        assert res_add.success is True
        res_rem = await pkg.remove("pkg")
        assert res_rem.success is True
        res_sync = await pkg.sync()
        assert res_sync.success is True
        res_lock = await pkg.lock()
        assert res_lock.success is True
