"""Tests for git/worktree operations with mocked subprocess."""
from pathlib import Path
from unittest.mock import patch, MagicMock
import subprocess

import click
import pytest

from patchlint import (
    check_git_repo,
    check_clean_tree,
    check_vng,
    resolve_rev_short,
    git_worktree,
)


class TestCheckGitRepo:
    def test_valid_repo(self, tmp_path):
        with patch("patchlint.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            check_git_repo(tmp_path)  # should not raise

    def test_not_a_repo(self, tmp_path):
        with patch("patchlint.subprocess.run", side_effect=subprocess.CalledProcessError(128, "git")):
            with pytest.raises(click.UsageError, match="not a git repository"):
                check_git_repo(tmp_path)


class TestCheckCleanTree:
    def test_clean_tree(self, tmp_path):
        with patch("patchlint.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            check_clean_tree(tmp_path)  # should not raise

    def test_dirty_unstaged(self, tmp_path):
        with patch("patchlint.subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.CalledProcessError(1, "git diff")
            with pytest.raises(click.UsageError, match="dirty"):
                check_clean_tree(tmp_path)

    def test_dirty_staged(self, tmp_path):
        def side_effect(*args, **kwargs):
            cmd = args[0]
            if "--cached" in cmd:
                raise subprocess.CalledProcessError(1, "git diff --cached")
            return MagicMock(returncode=0)

        with patch("patchlint.subprocess.run", side_effect=side_effect):
            with pytest.raises(click.UsageError, match="dirty"):
                check_clean_tree(tmp_path)


class TestCheckVng:
    def test_vng_found(self):
        with patch("patchlint.shutil.which", return_value="/usr/bin/vng"):
            check_vng()  # should not raise

    def test_vng_not_found(self):
        with patch("patchlint.shutil.which", return_value=None):
            with pytest.raises(click.UsageError, match="vng"):
                check_vng()


class TestResolveRevShort:
    def test_resolves_rev(self, tmp_path):
        with patch("patchlint.run_capture", return_value="abc123def456\n"):
            result = resolve_rev_short(tmp_path, "HEAD~1")
        assert result == "abc123def456"


class TestGitWorktree:
    def test_creates_and_removes_worktree(self, tmp_path):
        with patch("patchlint.subprocess.run") as mock_run, \
             patch("patchlint.tempfile.mkdtemp", return_value=str(tmp_path / "wt")):
            mock_run.return_value = MagicMock(returncode=0)
            (tmp_path / "wt").mkdir()

            with git_worktree(tmp_path, "HEAD~1") as wt:
                assert wt == tmp_path / "wt"

            assert mock_run.call_count == 2
            add_call = mock_run.call_args_list[0]
            remove_call = mock_run.call_args_list[1]
            assert "add" in add_call.args[0]
            assert "--detach" in add_call.args[0]
            assert "remove" in remove_call.args[0]

    def test_removes_worktree_on_exception(self, tmp_path):
        with patch("patchlint.subprocess.run") as mock_run, \
             patch("patchlint.tempfile.mkdtemp", return_value=str(tmp_path / "wt")):
            mock_run.return_value = MagicMock(returncode=0)
            (tmp_path / "wt").mkdir()

            with pytest.raises(RuntimeError):
                with git_worktree(tmp_path, "HEAD~1") as wt:
                    raise RuntimeError("boom")

            remove_call = mock_run.call_args_list[-1]
            assert "remove" in remove_call.args[0]

    def test_worktree_creation_failure(self, tmp_path):
        """Failed worktree add raises ClickException with git's error message."""
        exc = subprocess.CalledProcessError(
            128, "git", stderr="fatal: 'badrev' is not a commit\n"
        )
        with patch("patchlint.subprocess.run", side_effect=exc), \
             patch("patchlint.tempfile.mkdtemp", return_value=str(tmp_path / "wt")), \
             patch("patchlint.shutil.rmtree") as mock_rmtree:
            (tmp_path / "wt").mkdir()
            with pytest.raises(click.ClickException, match="failed to create worktree"):
                with git_worktree(tmp_path, "badrev") as wt:
                    pass  # pragma: no cover
            mock_rmtree.assert_called_once_with(tmp_path / "wt", ignore_errors=True)
