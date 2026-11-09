"""Unit tests for create-anima scaffold utility."""

import json
import os
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from anima.cli_create import (
    Scaffold,
    confirm,
    is_enoent,
    scaffold,
    supports,
)


class TestSupports:
    def test_supports_true(self):
        with patch("anima.cli_create.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            assert supports("git --version") is True

    def test_supports_false(self):
        with patch("anima.cli_create.subprocess.run") as mock_run:
            mock_run.side_effect = FileNotFoundError()
            assert supports("nonexistent --version") is False


class TestIsEnoent:
    def test_is_enoent_true(self):
        err = MagicMock()
        err.code = "ENOENT"
        assert is_enoent(err) is True

    def test_is_enoent_false(self):
        err = MagicMock()
        err.code = "EACCES"
        assert is_enoent(err) is False


class TestConfirm:
    def test_confirm_default_yes(self):
        assert confirm("Proceed?", initial=True, prompt=lambda _: "") is True

    def test_confirm_default_no(self):
        assert confirm("Proceed?", initial=False, prompt=lambda _: "") is False

    def test_confirm_explicit_yes(self):
        assert confirm("Proceed?", initial=False, prompt=lambda _: "y") is True

    def test_confirm_explicit_no(self):
        assert confirm("Proceed?", initial=True, prompt=lambda _: "n") is False


class TestScaffoldGetName:
    def test_get_name_from_options(self):
        s = Scaffold({"name": "myapp"})
        assert s.get_name() == "myapp"

    def test_get_name_from_prompt(self):
        s = Scaffold({})
        assert s.get_name(prompt=lambda _: "myproject") == "myproject"

    def test_get_name_empty_prompt_uses_default(self):
        s = Scaffold({})
        assert s.get_name(prompt=lambda _: "  ") == "app-app"


class TestScaffoldPrepare:
    def test_prepare_creates_directory(self, tmp_path: Path):
        s = Scaffold({})
        s.project = "newproj"
        with patch("anima.cli_create.Path.cwd", return_value=tmp_path):
            s.prepare()
        assert (tmp_path / "newproj").is_dir()
        assert s.root_dir == str(tmp_path / "newproj")

    def test_prepare_existing_empty_directory(self, tmp_path: Path):
        s = Scaffold({})
        s.project = "emptyproj"
        (tmp_path / "emptyproj").mkdir()
        with patch("anima.cli_create.Path.cwd", return_value=tmp_path):
            s.prepare()
        assert s.root_dir == str(tmp_path / "emptyproj")

    def test_prepare_non_empty_aborts_without_force(self, tmp_path: Path):
        s = Scaffold({})
        s.project = "fullproj"
        (tmp_path / "fullproj").mkdir()
        (tmp_path / "fullproj" / "file.txt").write_text("x")
        with (
            patch("anima.cli_create.Path.cwd", return_value=tmp_path),
            patch("anima.cli_create.confirm", return_value=False),
            pytest.raises(SystemExit),
        ):
            s.prepare()


class TestScaffoldInitGit:
    def test_init_git_skipped_when_no_git_flag(self, tmp_path: Path):
        s = Scaffold({})
        s.root_dir = str(tmp_path)
        with patch("anima.cli_create.supports", return_value=False):
            s.init_git()  # should not raise

    def test_init_git_runs_git_init(self, tmp_path: Path):
        s = Scaffold({"git": True})
        s.root_dir = str(tmp_path)
        with (
            patch("anima.cli_create.supports", return_value=True),
            patch("anima.cli_create.subprocess.run") as mock_run,
        ):
            s.init_git()
            mock_run.assert_called_once_with(
                ["git", "init"],
                check=False,
                capture_output=True,
                cwd=str(tmp_path),
            )


class TestScaffoldInstall:
    def test_install_skipped_when_yes(self, tmp_path: Path):
        s = Scaffold({"yes": True})
        s.root_dir = str(tmp_path)
        with patch("anima.cli_create.subprocess.run") as mock_run:
            s.install()
            mock_run.assert_not_called()

    def test_install_runs_commands(self, tmp_path: Path):
        s = Scaffold({})
        s.root_dir = str(tmp_path)
        with (
            patch.object(
                Scaffold, "_which_agent", return_value={"name": "npm", "version": ""}
            ),
            patch("anima.cli_create.confirm", return_value=True),
            patch("anima.cli_create.subprocess.run") as mock_run,
        ):
            s.install()
            assert mock_run.call_count == 2


class TestScaffoldInternalHelpers:
    def test_stat_existing(self, tmp_path: Path):
        s = Scaffold({})
        f = tmp_path / "test.txt"
        f.write_text("hello")
        result = s._stat(f)
        assert result is not None

    def test_stat_missing(self, tmp_path: Path):
        s = Scaffold({})
        result = s._stat(tmp_path / "nonexistent")
        assert result is None

    def test_rm(self, tmp_path: Path):
        s = Scaffold({})
        d = tmp_path / "todelete"
        d.mkdir()
        (d / "file.txt").write_text("x")
        s._rm(d)
        assert not d.exists()

    def test_can_parse(self):
        s = Scaffold({})
        assert s._can_parse("https://registry.npmjs.org/foo") is True
        assert s._can_parse("not-a-url") is False

    def test_read_json_valid(self):
        s = Scaffold({})
        resp = MagicMock()
        resp.read.return_value = b'{"key": "value"}'
        assert s._read_json(resp) == {"key": "value"}

    def test_read_json_invalid(self):
        s = Scaffold({})
        resp = MagicMock()
        resp.read.return_value = b"not json"
        assert s._read_json(resp) is None

    def test_get_registry_success(self):
        s = Scaffold({})
        with patch("anima.cli_create.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0, stdout="https://registry.npmjs.org/\n"
            )
            assert s._get_registry() == "https://registry.npmjs.org/"
        s = Scaffold({})
        with patch("anima.cli_create.subprocess.run", side_effect=FileNotFoundError()):
            assert s._get_registry() is None

    def test_fetch_or_exit_invalid_url(self):
        s = Scaffold({})
        with pytest.raises(SystemExit):
            s._fetch_or_exit("not-a-url")

    def test_fetch_latest_yarn(self):
        s = Scaffold({"registry": "https://registry.npmjs.org"})
        with patch("anima.cli_create.urllib.request.urlopen") as mock_urlopen:
            mock_resp = MagicMock()
            mock_resp.read.return_value = json.dumps(
                {"dist-tags": {"latest": "1.22.0"}}
            ).encode()
            mock_urlopen.return_value = mock_resp
            assert s._fetch_latest_yarn() == "1.22.0"

    def test_fetch_latest_yarn_failure(self):
        s = Scaffold({"registry": "https://registry.npmjs.org"})
        with patch("anima.cli_create.urllib.request.urlopen", side_effect=Exception()):
            assert s._fetch_latest_yarn() is None

    def test_is_timeout(self):
        s = Scaffold({})
        err = MagicMock()
        err.name = "TimeoutError"
        assert s._is_timeout(err) is True

        err2 = MagicMock()
        err2.name = "OtherError"
        err2.cause = MagicMock()
        err2.cause.name = "HeadersTimeoutError"
        assert s._is_timeout(err2) is True

        err3 = MagicMock()
        err3.name = "OtherError"
        err3.cause = None
        assert s._is_timeout(err3) is False

    def test_which_agent(self):
        s = Scaffold({})
        with patch("anima.cli_create.supports", return_value=True):
            agent = s._which_agent()
            assert agent == {"name": "npm", "version": ""}

    def test_which_agent_none(self):
        s = Scaffold({})
        with patch("anima.cli_create.supports", return_value=False):
            assert s._which_agent() is None

    def test_load_pkg(self):
        s = Scaffold({})
        pkg = s._load_pkg()
        assert pkg["name"] == "anima"
        assert pkg["version"] == "0.1.0"

    def test_write_package_json(self, tmp_path: Path):
        s = Scaffold({})
        s.project = "my-project"
        s.root_dir = str(tmp_path)
        (tmp_path / "package.json").write_text(
            json.dumps({"name": "old", "version": "1.0.0"})
        )
        s._write_package_json("my-template")
        data = json.loads((tmp_path / "package.json").read_text())
        assert data["name"] == "my-project"
        assert data["version"] == "1.0.0"

    def test_write_package_json_prod(self, tmp_path: Path):
        s = Scaffold({"prod": True})
        s.project = "my-project"
        s.root_dir = str(tmp_path)
        (tmp_path / "package.json").write_text(
            json.dumps(
                {
                    "name": "old",
                    "version": "1.0.0",
                    "workspaces": ["packages/*"],
                    "devDependencies": {"typescript": "^5.0"},
                }
            )
        )
        s._write_package_json("my-template")
        data = json.loads((tmp_path / "package.json").read_text())
        assert data["name"] == "my-project"
        assert "workspaces" not in data
        assert "devDependencies" not in data


class TestScaffoldScaffold:
    def test_scaffold_detects_registry(self, tmp_path: Path):
        s = Scaffold({})
        s.project = "testproj"
        s.root_dir = str(tmp_path / "testproj")
        (tmp_path / "testproj").mkdir()
        with (
            patch.object(s, "_get_registry", return_value="https://registry.npmjs.org"),
            patch.object(s, "_fetch_or_exit") as mock_fetch,
        ):
            mock_resp = MagicMock()
            mock_resp.read.return_value = json.dumps(
                {
                    "dist-tags": {"latest": "1.0.0"},
                    "versions": {
                        "1.0.0": {
                            "dist": {
                                "tarball": "https://registry.npmjs.org/foo/-/foo-1.0.0.tgz"
                            }
                        }
                    },
                }
            ).encode()
            mock_fetch.return_value = mock_resp
            with (
                patch.object(s, "_extract_tarball", return_value=None),
                patch.object(s, "_stage_yarn_bin"),
                patch.object(s, "_write_package_json"),
            ):
                s.scaffold()
                assert mock_fetch.call_count >= 1


class TestConfirmEdgeCases:
    def test_confirm_mixed_case(self):
        assert confirm("OK?", initial=False, prompt=lambda _: "YES") is True
        assert confirm("OK?", initial=True, prompt=lambda _: "No") is False


class TestScaffoldPrepareEdgeCases:
    def test_prepare_not_a_directory(self, tmp_path: Path):
        s = Scaffold({})
        s.project = "afile"
        (tmp_path / "afile").write_text("not a dir")
        with (
            patch("anima.cli_create.Path.cwd", return_value=tmp_path),
            patch("anima.cli_create.confirm", return_value=True),
        ):
            s.prepare()
            assert (tmp_path / "afile").is_dir()

    def test_prepare_force_removes_non_empty(self, tmp_path: Path):
        s = Scaffold({"forced": True})
        s.project = "fullproj"
        (tmp_path / "fullproj").mkdir()
        (tmp_path / "fullproj" / "file.txt").write_text("x")
        with patch("anima.cli_create.Path.cwd", return_value=tmp_path):
            s.prepare()
            assert not (tmp_path / "fullproj" / "file.txt").exists()
            assert (tmp_path / "fullproj").is_dir()


class TestScaffoldScaffoldEdgeCases:
    def test_scaffold_registry_failure(self, tmp_path: Path):
        s = Scaffold({})
        s.project = "testproj"
        s.root_dir = str(tmp_path / "testproj")
        (tmp_path / "testproj").mkdir()
        with (
            patch.object(s, "_get_registry", return_value=None),
            pytest.raises(SystemExit),
        ):
            s.scaffold()

    def test_scaffold_invalid_registry(self, tmp_path: Path):
        s = Scaffold({})
        s.project = "testproj"
        s.root_dir = str(tmp_path / "testproj")
        (tmp_path / "testproj").mkdir()
        with (
            patch.object(s, "_get_registry", return_value="ftp://bad.example.com"),
            pytest.raises(SystemExit),
        ):
            s.scaffold()

    def test_scaffold_invalid_remote(self, tmp_path: Path):
        s = Scaffold({})
        s.project = "testproj"
        s.root_dir = str(tmp_path / "testproj")
        (tmp_path / "testproj").mkdir()
        with (
            patch.object(s, "_get_registry", return_value="https://registry.npmjs.org"),
            patch.object(s, "_fetch_or_exit") as mock_fetch,
        ):
            mock_resp = MagicMock()
            mock_resp.read.return_value = b"not json"
            mock_fetch.return_value = mock_resp
            with (
                patch.object(s, "_read_json", return_value=None),
                pytest.raises(SystemExit),
            ):
                s.scaffold()

    def test_scaffold_no_tarball(self, tmp_path: Path):
        s = Scaffold({})
        s.project = "testproj"
        s.root_dir = str(tmp_path / "testproj")
        (tmp_path / "testproj").mkdir()
        with (
            patch.object(s, "_get_registry", return_value="https://registry.npmjs.org"),
            patch.object(s, "_fetch_or_exit") as mock_fetch,
        ):
            mock_resp = MagicMock()
            mock_resp.read.return_value = json.dumps(
                {"dist-tags": {"latest": "1.0.0"}, "versions": {"1.0.0": {"dist": {}}}}
            ).encode()
            mock_fetch.return_value = mock_resp
            with pytest.raises(SystemExit):
                s.scaffold()

    def test_scaffold_stage_yarn_error(self, tmp_path: Path):
        s = Scaffold({})
        s.project = "testproj"
        s.root_dir = str(tmp_path / "testproj")
        (tmp_path / "testproj").mkdir()
        with (
            patch.object(s, "_get_registry", return_value="https://registry.npmjs.org"),
            patch.object(s, "_fetch_or_exit") as mock_fetch,
        ):
            mock_resp = MagicMock()
            mock_resp.read.return_value = json.dumps(
                {
                    "dist-tags": {"latest": "1.0.0"},
                    "versions": {
                        "1.0.0": {
                            "dist": {
                                "tarball": "https://registry.npmjs.org/foo/-/foo-1.0.0.tgz"
                            }
                        }
                    },
                }
            ).encode()
            mock_fetch.return_value = mock_resp
            with (
                patch.object(s, "_extract_tarball", return_value=None),
                patch.object(s, "_stage_yarn_bin", side_effect=RuntimeError("boom")),
                pytest.raises(SystemExit),
            ):
                s.scaffold()


class TestGetRegistryEdgeCases:
    def test_get_registry_subprocess_failure(self):
        s = Scaffold({})
        with patch("anima.cli_create.subprocess.run") as mock_run:
            mock_run.side_effect = FileNotFoundError()
            assert s._get_registry() is None


class TestFetchOrExitEdgeCases:
    def test_fetch_or_exit_invalid_url(self):
        s = Scaffold({})
        with pytest.raises(SystemExit):
            s._fetch_or_exit("not-a-url")

    def test_fetch_or_exit_exception(self):
        s = Scaffold({})
        with (
            patch(
                "anima.cli_create.urllib.request.urlopen", side_effect=OSError("boom")
            ),
            patch("anima.cli_create.sys.exit") as mock_exit,
        ):
            s._fetch_or_exit("https://example.com/foo")
            mock_exit.assert_called_once_with(1)


class TestExtractTarballEdgeCases:
    def test_extract_tarball_read_error(self):
        s = Scaffold({})
        s.root_dir = "/tmp/test"
        resp = MagicMock()
        resp.read.side_effect = OSError("read failed")
        result = s._extract_tarball(resp)
        assert "unable to download" in result

    def test_extract_tarball_extraction_error(self):
        s = Scaffold({})
        s.root_dir = "/tmp/test"
        resp = MagicMock()
        resp.read.return_value = b"not a tarball"
        result = s._extract_tarball(resp)
        assert "unable to download" in result


class TestStageYarnBinEdgeCases:
    def test_stage_yarn_bin_package_manager(self, tmp_path: Path):
        s = Scaffold({})
        s.root_dir = str(tmp_path)
        (tmp_path / "package.json").write_text(
            json.dumps({"packageManager": "yarn@1.22.0"})
        )
        s._stage_yarn_bin()  # should return early

    def test_stage_yarn_bin_non_yarn_agent(self, tmp_path: Path):
        s = Scaffold({})
        s.root_dir = str(tmp_path)
        (tmp_path / "package.json").write_text(json.dumps({}))
        with patch.object(
            Scaffold, "_which_agent", return_value={"name": "npm", "version": ""}
        ):
            s._stage_yarn_bin()  # should return early (not yarn)

    def test_stage_yarn_bin_no_yarnrc(self, tmp_path: Path):
        s = Scaffold({})
        s.root_dir = str(tmp_path)
        (tmp_path / "package.json").write_text(json.dumps({}))
        with patch.object(
            Scaffold, "_which_agent", return_value={"name": "yarn", "version": "1.22.0"}
        ):
            s._stage_yarn_bin()  # should return early (no .yarnrc.yml)

    def test_stage_yarn_bin_rc_not_dict(self, tmp_path: Path):
        s = Scaffold({})
        s.root_dir = str(tmp_path)
        (tmp_path / "package.json").write_text(json.dumps({}))
        (tmp_path / ".yarnrc.yml").write_text("- item1\n- item2\n")
        with patch.object(
            Scaffold, "_which_agent", return_value={"name": "yarn", "version": "1.22.0"}
        ):
            s._stage_yarn_bin()  # should return early (rc is a list, not dict)


class TestWritePackageJsonEdgeCases:
    def test_write_package_json_prod(self, tmp_path: Path):
        s = Scaffold({"prod": True})
        s.project = "my-project"
        s.root_dir = str(tmp_path)
        (tmp_path / "package.json").write_text(
            json.dumps(
                {
                    "name": "old",
                    "version": "1.0.0",
                    "workspaces": ["packages/*"],
                    "devDependencies": {"typescript": "^5.0"},
                }
            )
        )
        s._write_package_json("my-template")
        data = json.loads((tmp_path / "package.json").read_text())
        assert data["name"] == "my-project"
        assert "workspaces" not in data
        assert "devDependencies" not in data

    def test_write_package_json_file_not_found(self, tmp_path: Path):
        s = Scaffold({})
        s.project = "my-project"
        s.root_dir = str(tmp_path)
        # No package.json exists - should not raise
        s._write_package_json("my-template")


class TestInstallEdgeCases:
    def test_install_confirm_yes_with_agent(self, tmp_path: Path):
        s = Scaffold({})
        s.root_dir = str(tmp_path)
        with (
            patch.object(
                Scaffold, "_which_agent", return_value={"name": "npm", "version": ""}
            ),
            patch("anima.cli_create.confirm", return_value=True),
            patch("anima.cli_create.subprocess.run") as mock_run,
        ):
            s.install()
            assert mock_run.call_count == 2

    def test_install_confirm_no(self, tmp_path: Path):
        s = Scaffold({})
        s.root_dir = str(tmp_path)
        with (
            patch.object(
                Scaffold, "_which_agent", return_value={"name": "npm", "version": ""}
            ),
            patch("anima.cli_create.confirm", return_value=False),
            patch("anima.cli_create.subprocess.run") as mock_run,
        ):
            s.install()
            mock_run.assert_not_called()

    def test_install_no_agent(self, tmp_path: Path):
        s = Scaffold({})
        s.root_dir = str(tmp_path)
        with (
            patch.object(Scaffold, "_which_agent", return_value=None),
            patch("anima.cli_create.confirm", return_value=True),
            patch("anima.cli_create.subprocess.run") as mock_run,
        ):
            s.install()
            mock_run.assert_not_called()


class TestStartMethod:
    def test_start_full_flow(self, tmp_path: Path):
        s = Scaffold({})
        s.project = "myapp"
        s.root_dir = str(tmp_path / "myapp")
        (tmp_path / "myapp").mkdir()
        with (
            patch.object(s, "get_name", return_value="myapp"),
            patch.object(s, "prepare"),
            patch.object(s, "scaffold"),
            patch.object(s, "init_git"),
            patch.object(s, "install"),
        ):
            s.start()
            assert s.project == "myapp"


class TestGetRegistryTimeout:
    def test_get_registry_timeout(self):
        s = Scaffold({})
        with patch("anima.cli_create.subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired("npm", 10)
            assert s._get_registry() is None


class TestStageYarnBinMore:
    def test_stage_yarn_bin_pinned_exists(self, tmp_path: Path):
        s = Scaffold({})
        s.root_dir = str(tmp_path)
        (tmp_path / "package.json").write_text(json.dumps({}))
        (tmp_path / ".yarnrc.yml").write_text(
            "yarnPath: .yarn/releases/yarn-1.22.0.cjs\n"
        )
        releases_dir = tmp_path / ".yarn" / "releases"
        releases_dir.mkdir(parents=True)
        (releases_dir / "yarn-1.22.0.cjs").write_text("#!/usr/bin/env node\n")
        with patch.object(
            Scaffold, "_which_agent", return_value={"name": "yarn", "version": "1.x"}
        ):
            s._stage_yarn_bin()  # should return early (yarn binary exists)

    def test_stage_yarn_bin_invalid_yarnpath(self, tmp_path: Path):
        s = Scaffold({})
        s.root_dir = str(tmp_path)
        (tmp_path / "package.json").write_text(json.dumps({}))
        (tmp_path / ".yarnrc.yml").write_text("yarnPath: invalid/path\n")
        with patch.object(
            Scaffold, "_which_agent", return_value={"name": "yarn", "version": "1.x"}
        ):
            s._stage_yarn_bin()  # should return early (yarnPath doesn't match pattern)


class TestScaffoldNoTarball:
    def test_scaffold_no_tarball_exits(self, tmp_path: Path):
        s = Scaffold({})
        s.project = "testproj"
        s.root_dir = str(tmp_path / "testproj")
        (tmp_path / "testproj").mkdir()
        with (
            patch.object(s, "_get_registry", return_value="https://registry.npmjs.org"),
            patch.object(s, "_fetch_or_exit") as mock_fetch,
        ):
            mock_resp = MagicMock()
            mock_resp.read.return_value = json.dumps(
                {"dist-tags": {"latest": "1.0.0"}, "versions": {"1.0.0": {"dist": {}}}}
            ).encode()
            mock_fetch.return_value = mock_resp
            with pytest.raises(SystemExit):
                s.scaffold()


class TestScaffoldExtractFailure:
    def test_scaffold_extract_failure_exits(self, tmp_path: Path):
        s = Scaffold({})
        s.project = "testproj"
        s.root_dir = str(tmp_path / "testproj")
        (tmp_path / "testproj").mkdir()
        with (
            patch.object(s, "_get_registry", return_value="https://registry.npmjs.org"),
            patch.object(s, "_fetch_or_exit") as mock_fetch,
        ):
            mock_resp = MagicMock()
            mock_resp.read.return_value = json.dumps(
                {
                    "dist-tags": {"latest": "1.0.0"},
                    "versions": {
                        "1.0.0": {
                            "dist": {
                                "tarball": "https://registry.npmjs.org/foo/-/foo-1.0.0.tgz"
                            }
                        }
                    },
                }
            ).encode()
            mock_fetch.return_value = mock_resp
            with (
                patch.object(s, "_extract_tarball", return_value="extraction failed"),
                pytest.raises(SystemExit),
            ):
                s.scaffold()

    def test_install_same_dir(self, tmp_path: Path):
        s = Scaffold({})
        s.root_dir = os.getcwd()
        with (
            patch.object(
                Scaffold, "_which_agent", return_value={"name": "npm", "version": ""}
            ),
            patch("anima.cli_create.confirm", return_value=False),
        ):
            s.install()  # root_dir == cwd, so no "cd" message


class TestGetRegistryOSError:
    def test_get_registry_os_error(self):
        s = Scaffold({})
        with patch("anima.cli_create.subprocess.run") as mock_run:
            mock_run.side_effect = OSError("boom")
            assert s._get_registry() is None


class TestFetchOrExitTimeout:
    def test_fetch_or_exit_timeout(self):
        s = Scaffold({})
        with (
            patch(
                "anima.cli_create.urllib.request.urlopen",
                side_effect=subprocess.TimeoutExpired("url", 10),
            ),
            pytest.raises(SystemExit),
        ):
            s._fetch_or_exit("https://example.com/foo")


class TestExtractTarballExtractionError:
    def test_extract_tarball_extraction_error(self):
        s = Scaffold({})
        s.root_dir = "/tmp/test"
        resp = MagicMock()
        resp.read.return_value = b"not a tarball"
        result = s._extract_tarball(resp)
        assert "unable to download" in result


class TestStageYarnBinEarlyReturns:
    def test_stage_yarn_bin_no_package_json(self, tmp_path: Path):
        s = Scaffold({})
        s.root_dir = str(tmp_path)
        # No package.json - should return early
        s._stage_yarn_bin()

    def test_stage_yarn_bin_agent_none(self, tmp_path: Path):
        s = Scaffold({})
        s.root_dir = str(tmp_path)
        (tmp_path / "package.json").write_text(json.dumps({}))
        with patch.object(Scaffold, "_which_agent", return_value=None):
            s._stage_yarn_bin()  # should return early (no agent)


class TestScaffoldFunction:
    def test_scaffold_function(self):
        """Test the module-level scaffold() function."""
        result = scaffold({"name": "testapp"})
        assert isinstance(result, Scaffold)
        assert result.options == {"name": "testapp"}


class TestGetRegistryBranches:
    def test_get_registry_os_error(self):
        s = Scaffold({})
        with patch("anima.cli_create.subprocess.run") as mock_run:
            mock_run.side_effect = OSError("boom")
            assert s._get_registry() is None


class TestFetchOrExitSuccess:
    def test_fetch_or_exit_success(self):
        s = Scaffold({})
        mock_resp = MagicMock()
        with patch("anima.cli_create.urllib.request.urlopen", return_value=mock_resp):
            result = s._fetch_or_exit("https://example.com/foo")
            assert result is mock_resp


class TestExtractTarballSuccess:
    def test_extract_tarball_success(self, tmp_path: Path):
        s = Scaffold({})
        s.root_dir = str(tmp_path)
        import tarfile

        # Create a valid empty tar file
        tar_path = tmp_path / "archive.tar"
        with tarfile.open(tar_path, "w"):
            pass
        resp = MagicMock()
        resp.read.return_value = tar_path.read_bytes()
        result = s._extract_tarball(resp)
        assert result is None


class TestStageYarnBinReturnNone:
    def test_stage_yarn_bin_returns_none(self, tmp_path: Path):
        s = Scaffold({})
        s.root_dir = str(tmp_path)
        (tmp_path / "package.json").write_text(json.dumps({}))
        with patch.object(
            Scaffold, "_which_agent", return_value={"name": "npm", "version": ""}
        ):
            result = s._stage_yarn_bin()
            assert result is None


class TestStageYarnBinBranchCoverage:
    def test_stage_yarn_bin_agent_not_yarn1(self, tmp_path: Path):
        """agent.version doesn't start with '1.' → early return."""
        s = Scaffold({})
        s.root_dir = str(tmp_path)
        (tmp_path / "package.json").write_text(json.dumps({}))
        (tmp_path / ".yarnrc.yml").write_text("key: value\n")
        with patch.object(
            Scaffold, "_which_agent", return_value={"name": "yarn", "version": "2.x"}
        ):
            s._stage_yarn_bin()

    def test_stage_yarn_bin_no_latest_version(self, tmp_path: Path):
        """_fetch_latest_yarn returns empty → RuntimeError."""
        s = Scaffold({})
        s.root_dir = str(tmp_path)
        (tmp_path / "package.json").write_text(json.dumps({}))
        (tmp_path / ".yarnrc.yml").write_text("key: value\n")
        with (
            patch.object(
                Scaffold,
                "_which_agent",
                return_value={"name": "yarn", "version": "1.x"},
            ),
            patch.object(Scaffold, "_fetch_latest_yarn", return_value=""),
            pytest.raises(RuntimeError),
        ):
            s._stage_yarn_bin()


class TestStageYarnBinDownload:
    def test_stage_yarn_bin_download_yarn(self, tmp_path: Path):
        s = Scaffold({})
        s.root_dir = str(tmp_path)
        (tmp_path / "package.json").write_text(json.dumps({}))
        (tmp_path / ".yarnrc.yml").write_text("key: value\n")
        cache_dir = tmp_path / ".cache" / "create-anima" / ".yarn/releases"
        with (
            patch.object(
                Scaffold,
                "_which_agent",
                return_value={"name": "yarn", "version": "1.x"},
            ),
            patch.object(Scaffold, "_fetch_latest_yarn", return_value="1.22.0"),
            patch.object(Scaffold, "_cache_dir", return_value=cache_dir),
        ):
            mock_resp = MagicMock()
            mock_resp.read.return_value = b"fake tarball data"
            with (
                patch(
                    "anima.cli_create.urllib.request.urlopen", return_value=mock_resp
                ),
                patch("anima.cli_create.tarfile.open"),
                patch("anima.cli_create.shutil.copy2"),
            ):
                s._stage_yarn_bin()


class TestGetRegistryBranch:
    def test_get_registry_nonzero_returncode(self):
        s = Scaffold({})
        with patch("anima.cli_create.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="")
            assert s._get_registry() is None


class TestStageYarnBinBranches:
    def test_stage_yarn_bin_yarnpath_not_string(self, tmp_path: Path):
        s = Scaffold({})
        s.root_dir = str(tmp_path)
        (tmp_path / "package.json").write_text(json.dumps({}))
        (tmp_path / ".yarnrc.yml").write_text("yarnPath: 123\n")
        with patch.object(
            Scaffold, "_which_agent", return_value={"name": "yarn", "version": "1.x"}
        ):
            s._stage_yarn_bin()  # yarnPath is int, not str

    def test_stage_yarn_bin_yarnpath_no_match(self, tmp_path: Path):
        s = Scaffold({})
        s.root_dir = str(tmp_path)
        (tmp_path / "package.json").write_text(json.dumps({}))
        (tmp_path / ".yarnrc.yml").write_text("yarnPath: something-else\n")
        with patch.object(
            Scaffold, "_which_agent", return_value={"name": "yarn", "version": "1.x"}
        ):
            s._stage_yarn_bin()  # yarnPath doesn't match pattern


class TestCacheDir:
    def test_cache_dir(self):
        s = Scaffold({})
        result = s._cache_dir()
        assert result == Path.home() / ".cache" / "create-anima" / ".yarn/releases"


class TestStageYarnBinAgentVersion:
    def test_stage_yarn_bin_agent_version_not_1x(self, tmp_path: Path):
        s = Scaffold({})
        s.root_dir = str(tmp_path)
        (tmp_path / "package.json").write_text(json.dumps({}))
        (tmp_path / ".yarnrc.yml").write_text("key: value\n")
        with (
            patch.object(
                Scaffold,
                "_which_agent",
                return_value={"name": "yarn", "version": "2.x"},
            ),
            patch.object(Scaffold, "_cache_dir", return_value=tmp_path / ".cache"),
        ):
            s._stage_yarn_bin()  # agent.version "2.x" doesn't start with "1." → early return
