"""create-anima: Scaffold an Anima application.

Python port of the create-anima: interactive project scaffolding that
downloads a template tarball from the npm registry, extracts it, optionally
stages a yarn binary, and initializes a Git repository.
"""

from __future__ import annotations

import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml


def supports(command: str) -> bool:
    """Return True when `command` is executable on PATH."""
    try:
        subprocess.run(
            command,
            shell=True,
            capture_output=True,
            check=True,
        )
        return True
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return False


def is_enoent(error: Any) -> bool:
    """Return True when `error` is an ENOENT (missing file) error."""
    return getattr(error, "code", None) == "ENOENT"


def confirm(
    message: str,
    initial: bool = True,
    prompt: Callable[[str], str] = input,
) -> bool:
    """Ask a yes/no question via `prompt`, defaulting to `initial`."""
    suffix = "Y/n" if initial else "y/N"
    answer = prompt(f"{message} [{suffix}] ").strip().lower()
    if not answer:
        return initial
    return answer[0] == "y"


class Scaffold:
    """Interactive project scaffolding."""

    def __init__(self, options: dict[str, Any] | None = None) -> None:
        self.options = options or {}
        self.registry: str | None = None
        self.project: str = ""
        self.root_dir: str = ""

    def get_name(self, prompt: Callable[[str], str] = input) -> str:
        """Resolve the project name from options or an interactive prompt."""
        if self.options.get("name"):
            return str(self.options["name"])
        name = prompt("Project name: ").strip()
        default_name = f"{self.options.get('name', 'app')}-app"
        return default_name if not name else name

    def prepare(self) -> None:
        """Create the target directory, removing it first if forced."""
        root_dir = Path.cwd() / self.project
        if not root_dir.exists():
            root_dir.mkdir(parents=True, exist_ok=True)
            self.root_dir = str(root_dir)
            return
        if root_dir.is_dir():
            files = list(root_dir.iterdir())
            if not files:
                self.root_dir = str(root_dir)
                return
            message = f'  Target directory "{self.project}" is not empty.'
        else:
            message = f'  Target "{self.project}" is not a directory.'

        if not self.options.get("forced") and not self.options.get("yes"):
            print(message)
            yes = confirm("Remove existing files and continue?")
            if not yes:
                sys.exit(0)

        self._rm(root_dir)
        root_dir.mkdir()
        self.root_dir = str(root_dir)

    def scaffold(self) -> None:
        """Download the template tarball, extract it, and set up the project."""
        self.registry = self._get_registry()
        if not self.registry:
            print("  error: unable to detect npm registry")
            sys.exit(1)
        if not self.registry.startswith(("http://", "https://")):
            print(f"  error: invalid npm registry {self.registry}")
            sys.exit(1)
        assert self.registry is not None
        self.registry = self.registry.rstrip("/")

        print(f"  Registry server: {self.registry}")
        template = self.options.get("template") or "@anima/boilerplate"
        print(f"  Scaffolding project in {self.project} ...")

        meta_url = f"{self.registry}/{template}"
        resp = self._fetch_or_exit(meta_url)
        remote = self._read_json(resp)
        if not remote:
            print(f"  error: invalid registry response from {meta_url}")
            sys.exit(1)

        version = remote.get("dist-tags", {}).get(self.options.get("ref") or "latest")
        tarball = (
            remote.get("versions", {}).get(version, {}).get("dist", {}).get("tarball")
        )
        if not tarball:
            print(f"  error: no tarball for {self.options.get('ref') or 'latest'}")
            sys.exit(1)

        resp2 = self._fetch_or_exit(tarball)
        failure = self._extract_tarball(resp2)
        if failure:
            print(f"  error: {failure}")
            sys.exit(1)

        try:
            self._stage_yarn_bin()
        except Exception as error:  # noqa: BLE001
            msg = "timed out" if self._is_timeout(error) else str(error)
            print(f"  error: unable to set up yarn: {msg}")
            sys.exit(1)

        self._write_package_json(template)
        print("  Done.\n")

    def init_git(self) -> None:
        """Initialize a Git repository if requested and git is available."""
        if not self.options.get("git") or not supports("git --version"):
            return
        subprocess.run(
            ["git", "init"], check=False, capture_output=True, cwd=self.root_dir
        )
        print("  Done.\n")

    def install(self) -> None:
        """Install dependencies if not skipped with -y."""
        if self.options.get("yes"):
            return
        agent = self._which_agent()
        if agent is None:
            return
        yes = confirm("Install and start it now?")
        if yes:
            agent_str = agent["name"]
            subprocess.run([agent_str, "install"], check=True, cwd=self.root_dir)
            subprocess.run([agent_str, "run", "start"], check=True, cwd=self.root_dir)
        else:
            print()
            if self.root_dir != os.getcwd():
                try:
                    relative = os.path.relpath(self.root_dir, os.getcwd())
                except ValueError:
                    # Cross-drive paths (e.g. C: vs D: on Windows) have no
                    # relative form; fall back to the absolute path.
                    relative = self.root_dir
                print(f"  cd {relative}")
            agent_str = agent["name"]
            print(f"  {agent_str} install")
            print(f"  {agent_str} run start")
            print()

    def start(self, prompt: Callable[[str], str] = input) -> None:
        """Run the full scaffolding flow."""
        pkg = self._load_pkg()
        print()
        print(f"  create {pkg['name']}  v{pkg['version']}")
        print()
        name = self.get_name(prompt)
        self.project = Path(name).name
        self.root_dir = str(Path.cwd() / self.project)
        self.prepare()
        self.scaffold()
        self.init_git()
        self.install()

    # -- internal helpers -----------------------------------------------------

    def _stat(self, path: Path) -> Any:
        try:
            return path.stat()
        except FileNotFoundError:
            return None

    def _rm(self, path: Path) -> None:
        if path.is_file():
            path.unlink()
        else:
            shutil.rmtree(path, ignore_errors=True)

    def _get_registry(self) -> str | None:
        """Detect the npm registry URL."""
        try:
            result = subprocess.run(
                ["npm", "config", "get", "registry"],
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode == 0:
                return result.stdout.strip()
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            pass
        return None

    def _fetch_or_exit(self, url: str) -> Any:
        if not self._can_parse(url):
            print(f"  error: invalid url {url}")
            sys.exit(1)
        timeout = self.options.get("timeout")
        try:
            resp = urllib.request.urlopen(url, timeout=timeout)
            return resp
        except Exception as error:  # noqa: BLE001
            msg = (
                "timed out waiting for"
                if self._is_timeout(error)
                else "unable to reach"
            )
            print(f"  error: {msg} {url}")
            sys.exit(1)

    def _can_parse(self, url: str) -> bool:
        from urllib.parse import urlparse

        return bool(urlparse(url).scheme)

    def _read_json(self, resp: Any) -> Any:
        try:
            data = resp.read()
            return json.loads(data)
        except Exception:  # noqa: BLE001
            return None

    def _extract_tarball(self, resp: Any) -> str | None:
        try:
            data = resp.read()
        except Exception as error:  # noqa: BLE001
            msg = (
                "timed out downloading"
                if self._is_timeout(error)
                else "unable to download"
            )
            return f"{msg} tarball"
        try:
            with tempfile.TemporaryDirectory() as tmp:
                tar_path = Path(tmp) / "archive.tar"
                tar_path.write_bytes(data)
                with tarfile.open(tar_path) as tar:
                    tar.extractall(path=Path(self.root_dir))
        except Exception as error:  # noqa: BLE001
            msg = (
                "timed out downloading"
                if self._is_timeout(error)
                else "unable to download"
            )
            return f"{msg} tarball"
        return None

    def _cache_dir(self) -> Path:
        return Path.home() / ".cache" / "create-anima" / ".yarn/releases"

    def _stage_yarn_bin(self) -> None:
        dir_path = Path(self.root_dir)
        pkg_path = dir_path / "package.json"
        pkg: dict[str, Any] = {}
        try:
            pkg = json.loads(pkg_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return

        if pkg.get("packageManager"):
            return
        agent = self._which_agent()
        if agent is None or agent.get("name") != "yarn":
            return

        rc_path = dir_path / ".yarnrc.yml"
        rc: dict[str, Any] = {}
        try:
            rc = yaml.safe_load(rc_path.read_text(encoding="utf-8")) or {}
        except (FileNotFoundError, yaml.YAMLError):
            return

        if not isinstance(rc, dict):
            return

        pinned: str | None = None
        yarn_path = rc.get("yarnPath")
        if isinstance(yarn_path, str):
            m = re.match(r"^\.yarn/releases/yarn-([^/]+)\.cjs$", yarn_path)
            if m:
                pinned = m.group(1)

        version: str = ""
        write_rc = False

        if yarn_path:
            if not pinned:
                return
            target_file = dir_path / yarn_path
            if target_file.exists():
                return
            version = pinned or ""
        else:
            if not agent.get("version", "").startswith("1."):
                return
            version = self._fetch_latest_yarn() or ""
            if not version:
                raise RuntimeError("no latest version from @yarnpkg/cli-dist")
            rc["yarnPath"] = f".yarn/releases/yarn-{version}.cjs"
            write_rc = True

        cache_dir = self._cache_dir()
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_file = cache_dir / f"yarn-{version}.cjs"

        if not cache_file.exists():
            temp_dir = Path(tempfile.mkdtemp(prefix="create-anima-yarn-"))
            try:
                url = f"{self.registry}/@yarnpkg/cli-dist/-/cli-dist-{version}.tgz"
                resp = urllib.request.urlopen(url, timeout=60000)
                data = resp.read()
                with tarfile.open(fileobj=io.BytesIO(data)) as tar:
                    for member in tar.getmembers():
                        if member.name.endswith("package/bin/yarn.js"):
                            extracted = tar.extractfile(member)
                            if extracted:
                                cache_file.write_bytes(extracted.read())
                            break
            finally:
                shutil.rmtree(temp_dir, ignore_errors=True)

        target_dir = dir_path / ".yarn" / "releases"
        target_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(cache_file, target_dir / f"yarn-{version}.cjs")

        if write_rc:
            rc_path.write_text(yaml.safe_dump(rc), encoding="utf-8")

    def _fetch_latest_yarn(self) -> str | None:
        url = f"{self.registry}/@yarnpkg/cli-dist"
        try:
            resp = urllib.request.urlopen(url, timeout=10)
            meta = json.loads(resp.read())
            return str(meta.get("dist-tags", {}).get("latest") or "")
        except Exception:  # noqa: BLE001
            return None

    def _is_timeout(self, error: Any) -> bool:
        cause = getattr(getattr(error, "cause", None), "name", None)
        return (
            getattr(error, "name", None) == "TimeoutError"
            or cause in ("HeadersTimeoutError", "BodyTimeoutError")
            or isinstance(error, (TimeoutError, urllib.error.URLError))
        )

    def _which_agent(self) -> dict[str, Any] | None:
        for name in ("npm", "pnpm", "yarn"):
            if supports(f"{name} --version"):
                return {"name": name, "version": ""}
        return None

    def _load_pkg(self) -> dict[str, Any]:
        return {"name": "anima", "version": "0.1.0"}

    def _write_package_json(self, template: str) -> None:
        """Write/update package.json with the project name."""
        path = Path(self.root_dir) / "package.json"
        try:
            meta = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return
        meta["name"] = self.project
        if self.options.get("prod"):
            meta.pop("workspaces", None)
            meta.pop("devDependencies", None)
        path.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")


def scaffold(options: dict[str, Any] | None = None) -> Scaffold:
    """Entry point: create and run a Scaffold."""
    return Scaffold(options or {})
