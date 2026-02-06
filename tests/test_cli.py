"""Click CLI integration tests using CliRunner."""
from pathlib import Path
from unittest.mock import patch, MagicMock
import subprocess

import pytest
from click.testing import CliRunner

from patchlint import main


@pytest.fixture
def runner():
    return CliRunner()


def test_help(runner):
    result = runner.invoke(main, ["--help"])
    assert result.exit_code == 0
    assert "patchlint" in result.output.lower() or "BASELINE" in result.output


def test_baseline_required(runner):
    result = runner.invoke(main, [])
    assert result.exit_code != 0


def test_no_vng(runner, tmp_path):
    with patch("patchlint.shutil.which", return_value=None):
        result = runner.invoke(main, ["HEAD~1", str(tmp_path)])
    assert result.exit_code != 0
    assert "vng" in result.output


def test_bad_baseline_rev(runner, tmp_path):
    """Bad baseline revision produces a friendly error, not a traceback."""
    exc = subprocess.CalledProcessError(
        128, "git", stderr="fatal: bad revision 'typo123'\n"
    )
    with patch("patchlint.shutil.which", return_value="/usr/bin/vng"), \
         patch("patchlint.check_git_repo"), \
         patch("patchlint.check_clean_tree"), \
         patch("patchlint.resolve_rev_short", side_effect=exc):
        result = runner.invoke(main, ["typo123", str(tmp_path)])
    assert result.exit_code == 2
    assert "unknown revision" in result.output
    assert "typo123" in result.output
