"""
Package management service interfacing asynchronously with the uv package manager.

Anima-original surface: not part of the ported kernel.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from anima.core import Context, Service

logger = logging.getLogger("anima.pkg")


@dataclass(frozen=True)
class PkgResult:
    """Outcome of a package manager CLI execution."""

    success: bool
    exit_code: int
    stdout: str
    stderr: str
    command: list[str]


class PkgService(Service):
    """Asynchronous package manager service leveraging uv."""

    def __init__(self, ctx: Context, config: dict[str, Any] | None = None) -> None:
        cfg = dict(config or {})
        uv_path = cfg.get("uv_path")
        super().__init__(ctx, "pkg")
        self.uv_path = (
            str(uv_path) if uv_path is not None else shutil.which("uv") or "uv"
        )

    async def _run_command(
        self,
        args: list[str],
        *,
        cwd: Path | str | None = None,
        timeout: float | None = None,
    ) -> PkgResult:
        """Execute uv subcommand asynchronously and capture structured output."""
        cmd = [self.uv_path, *args]
        work_dir = str(cwd) if cwd is not None else None

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=work_dir,
            )
            try:
                if timeout is not None:
                    stdout_bytes, stderr_bytes = await asyncio.wait_for(
                        proc.communicate(), timeout=timeout
                    )
                else:
                    stdout_bytes, stderr_bytes = await proc.communicate()
            except TimeoutError:
                proc.kill()
                await proc.wait()
                return PkgResult(
                    success=False,
                    exit_code=-1,
                    stdout="",
                    stderr=f"Command timed out after {timeout} seconds",
                    command=cmd,
                )

            stdout_str = stdout_bytes.decode("utf-8", errors="replace")
            stderr_str = stderr_bytes.decode("utf-8", errors="replace")
            exit_code = proc.returncode if proc.returncode is not None else 0

            return PkgResult(
                success=(exit_code == 0),
                exit_code=exit_code,
                stdout=stdout_str,
                stderr=stderr_str,
                command=cmd,
            )

        except FileNotFoundError:
            return PkgResult(
                success=False,
                exit_code=-1,
                stdout="",
                stderr=f"Package manager executable not found: {self.uv_path}",
                command=cmd,
            )
        except Exception as exc:
            logger.exception("Unexpected error executing package command: %s", cmd)
            return PkgResult(
                success=False,
                exit_code=-1,
                stdout="",
                stderr=str(exc),
                command=cmd,
            )

    async def add(
        self,
        packages: list[str] | str,
        *,
        dev: bool = False,
        cwd: Path | str | None = None,
        timeout: float | None = None,
    ) -> PkgResult:
        """Install one or more packages using uv add."""
        pkg_list = [packages] if isinstance(packages, str) else list(packages)
        args = ["add"]
        if dev:
            args.append("--dev")
        args.extend(pkg_list)

        res = await self._run_command(args, cwd=cwd, timeout=timeout)
        try:
            await self.ctx.parallel("pkg/add", pkg_list, res)
        except Exception:
            logger.exception("Error broadcasting pkg/add event")
        return res

    async def remove(
        self,
        packages: list[str] | str,
        *,
        cwd: Path | str | None = None,
        timeout: float | None = None,
    ) -> PkgResult:
        """Remove one or more packages using uv remove."""
        pkg_list = [packages] if isinstance(packages, str) else list(packages)
        args = ["remove", *pkg_list]

        res = await self._run_command(args, cwd=cwd, timeout=timeout)
        try:
            await self.ctx.parallel("pkg/remove", pkg_list, res)
        except Exception:
            logger.exception("Error broadcasting pkg/remove event")
        return res

    async def sync(
        self,
        *,
        dev: bool = True,
        cwd: Path | str | None = None,
        timeout: float | None = None,
    ) -> PkgResult:
        """Synchronize project environment using uv sync."""
        args = ["sync"]
        if not dev:
            args.append("--no-dev")

        res = await self._run_command(args, cwd=cwd, timeout=timeout)
        try:
            await self.ctx.parallel("pkg/sync", res)
        except Exception:
            logger.exception("Error broadcasting pkg/sync event")
        return res

    async def lock(
        self,
        *,
        check: bool = False,
        cwd: Path | str | None = None,
        timeout: float | None = None,
    ) -> PkgResult:
        """Generate or verify lockfile using uv lock."""
        args = ["lock"]
        if check:
            args.append("--check")

        res = await self._run_command(args, cwd=cwd, timeout=timeout)
        try:
            await self.ctx.parallel("pkg/lock", res)
        except Exception:
            logger.exception("Error broadcasting pkg/lock event")
        return res

    async def list_installed(
        self,
        *,
        cwd: Path | str | None = None,
        timeout: float | None = None,
    ) -> dict[str, str]:
        """List currently installed packages and versions via uv pip list."""
        res = await self._run_command(
            ["pip", "list", "--format=json"],
            cwd=cwd,
            timeout=timeout,
        )
        if not res.success:
            return {}

        try:
            items = json.loads(res.stdout)
            if isinstance(items, list):
                result: dict[str, str] = {}
                for item in items:
                    if isinstance(item, dict) and "name" in item and "version" in item:
                        result[str(item["name"])] = str(item["version"])
                return result
        except Exception:
            logger.exception("Failed to parse package list json output")

        return {}
