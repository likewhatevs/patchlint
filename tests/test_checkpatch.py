"""Tests for checkpatch integration."""
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
from click.testing import CliRunner

from patchlint import run_checkpatch, main


class TestRunCheckpatch:
    def test_passes(self, tmp_path):
        """checkpatch returns 0 → no exception."""
        (tmp_path / "scripts").mkdir()
        (tmp_path / "scripts" / "checkpatch.pl").touch()

        git_proc = MagicMock()
        git_proc.stdout = MagicMock()
        git_proc.wait.return_value = 0

        cp_proc = MagicMock()
        cp_proc.stdout.read.return_value = "total: 0 errors, 0 warnings\n"
        cp_proc.returncode = 0
        cp_proc.wait.return_value = 0

        with patch("patchlint.subprocess.Popen", side_effect=[git_proc, cp_proc]):
            run_checkpatch(tmp_path, "HEAD~1")  # should not raise

    def test_fails(self, tmp_path):
        """checkpatch returns 1 → SystemExit(2) with output shown."""
        (tmp_path / "scripts").mkdir()
        (tmp_path / "scripts" / "checkpatch.pl").touch()

        git_proc = MagicMock()
        git_proc.stdout = MagicMock()
        git_proc.wait.return_value = 0

        cp_output = "ERROR: trailing whitespace\ntotal: 1 errors, 0 warnings\n"
        cp_proc = MagicMock()
        cp_proc.stdout.read.return_value = cp_output
        cp_proc.returncode = 1
        cp_proc.wait.return_value = 1

        with patch("patchlint.subprocess.Popen", side_effect=[git_proc, cp_proc]):
            with pytest.raises(SystemExit) as exc_info:
                run_checkpatch(tmp_path, "HEAD~1")
            assert exc_info.value.code == 2

    def test_script_missing(self, tmp_path):
        """scripts/checkpatch.pl not found → SystemExit(2)."""
        # No scripts/checkpatch.pl in tmp_path
        with pytest.raises(SystemExit) as exc_info:
            run_checkpatch(tmp_path, "HEAD~1")
        assert exc_info.value.code == 2


class TestCheckpatchCLIIntegration:
    """Test checkpatch integration within the check command via CliRunner."""

    @pytest.fixture
    def runner(self):
        return CliRunner()

    def test_checkpatch_fails_before_builds(self, runner, tmp_path):
        """When checkpatch fails, exit_code=2 and builds never start."""
        (tmp_path / "scripts").mkdir()
        (tmp_path / "scripts" / "checkpatch.pl").touch()

        git_proc = MagicMock()
        git_proc.stdout = MagicMock()
        git_proc.wait.return_value = 0

        cp_proc = MagicMock()
        cp_proc.stdout.read.return_value = "ERROR: bad style\n"
        cp_proc.returncode = 1
        cp_proc.wait.return_value = 1

        with patch("patchlint.shutil.which", return_value="/usr/bin/vng"), \
             patch("patchlint.check_git_repo"), \
             patch("patchlint.check_clean_tree"), \
             patch("patchlint.resolve_rev_short", return_value="abc123def456"), \
             patch("patchlint.run_capture", return_value="1\n"), \
             patch("patchlint.subprocess.Popen", side_effect=[git_proc, cp_proc]), \
             patch("patchlint.build_config") as mock_build, \
             patch("patchlint.boot_test") as mock_boot:
            result = runner.invoke(main, ["HEAD~1", str(tmp_path)])
            assert result.exit_code == 2
            mock_build.assert_not_called()
            mock_boot.assert_not_called()

    def test_checkpatch_passes_continues(self, runner, tmp_path):
        """When checkpatch passes, builds are attempted."""
        (tmp_path / "scripts").mkdir()
        (tmp_path / "scripts" / "checkpatch.pl").touch()

        git_proc = MagicMock()
        git_proc.stdout = MagicMock()
        git_proc.wait.return_value = 0

        cp_proc = MagicMock()
        cp_proc.stdout.read.return_value = "total: 0 errors, 0 warnings\n"
        cp_proc.returncode = 0
        cp_proc.wait.return_value = 0

        with patch("patchlint.shutil.which", return_value="/usr/bin/vng"), \
             patch("patchlint.check_git_repo"), \
             patch("patchlint.check_clean_tree"), \
             patch("patchlint.subprocess.Popen", side_effect=[git_proc, cp_proc]), \
             patch("patchlint.resolve_rev_short", return_value="abc123def456"), \
             patch("patchlint.run_capture", return_value="1\n"), \
             patch("patchlint.git_worktree") as mock_wt, \
             patch("patchlint.build_config", return_value=0) as mock_build, \
             patch("patchlint.boot_test", return_value=(0, "Linux test 6.x #1 SMP GNU/Linux")) as mock_boot:
            mock_wt.return_value.__enter__ = MagicMock(return_value=tmp_path)
            mock_wt.return_value.__exit__ = MagicMock(return_value=False)
            result = runner.invoke(main, ["HEAD~1", str(tmp_path)])
            assert mock_build.called or mock_boot.called
