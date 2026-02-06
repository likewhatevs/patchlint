"""Tests for build sequences with mocked subprocess."""
from pathlib import Path
from unittest.mock import patch, MagicMock
import io

from patchlint import build_config, boot_test, run_and_log


class TestRunAndLog:
    def test_writes_to_log_only(self, tmp_path):
        log_fh = io.StringIO()
        with patch("patchlint.subprocess.Popen") as mock_popen:
            mock_proc = MagicMock()
            mock_stdout = io.StringIO("output\n")
            mock_proc.stdout = mock_stdout
            mock_proc.wait.return_value = 0
            mock_popen.return_value = mock_proc

            rc = run_and_log(["echo", "hi"], log_fh)

        assert rc == 0
        assert "output\n" in log_fh.getvalue()


class TestBuildConfig:
    def test_runs_full_command_sequence(self, tmp_path):
        log_path = tmp_path / "build.log"

        with patch("patchlint.run_and_log", return_value=0) as mock_log:
            rc = build_config(log_path, Path("/src/linux"), "allmodconfig")

        assert rc == 0
        assert mock_log.call_count == 5
        cmds = [c.args[0] for c in mock_log.call_args_list]
        assert cmds[0] == ["vng", "--clean"]
        assert cmds[1] == ["make", "allmodconfig"]
        assert cmds[2] == ["./scripts/config", "-d", "WERROR"]
        assert cmds[3] == ["make", "olddefconfig"]
        assert cmds[4] == ["vng", "--build", "--skip-config", "KCFLAGS=-Wno-error"]

    def test_always_cleans(self, tmp_path):
        log_path = tmp_path / "build.log"

        with patch("patchlint.run_and_log", return_value=0) as mock_log:
            build_config(log_path, Path("/src/linux"), "allmodconfig")

        first_cmd = mock_log.call_args_list[0].args[0]
        assert first_cmd == ["vng", "--clean"]

    def test_stops_on_first_failure(self, tmp_path):
        log_path = tmp_path / "build.log"

        with patch("patchlint.run_and_log", side_effect=[0, 1]) as mock_log:
            rc = build_config(log_path, Path("/src/linux"), "allmodconfig")

        assert rc == 1
        assert mock_log.call_count == 2

    def test_creates_log_file(self, tmp_path):
        log_path = tmp_path / "subdir" / "build.log"

        with patch("patchlint.run_and_log", return_value=0):
            build_config(log_path, Path("/src/linux"), "defconfig")

        assert log_path.exists()
        content = log_path.read_text()
        assert "build-log mode" in content
        assert "config=defconfig" in content

    def test_kcflags_wno_error_in_build_cmd(self, tmp_path):
        log_path = tmp_path / "build.log"

        with patch("patchlint.run_and_log", return_value=0) as mock_log:
            build_config(log_path, Path("/src/linux"), "allmodconfig")

        last_cmd = mock_log.call_args_list[-1].args[0]
        assert "KCFLAGS=-Wno-error" in last_cmd


class TestBootTest:
    def test_successful_boot(self, tmp_path):
        log_path = tmp_path / "boot.log"

        with patch("patchlint.run_and_log", return_value=0), \
             patch("patchlint._run_vng_boot",
                   return_value=(0, "Linux (none) 6.12.0-rc1 #1 SMP x86_64 GNU/Linux\n")):
            rc, uname = boot_test(log_path, Path("/src/linux"))

        assert rc == 0
        assert "6.12.0-rc1" in uname

    def test_runs_build_sequence(self, tmp_path):
        log_path = tmp_path / "boot.log"
        uname = "Linux virtme-ng 6.18.0-virtme #1 SMP PREEMPT_DYNAMIC 0 x86_64 GNU/Linux\n"

        with patch("patchlint.run_and_log", return_value=0) as mock_log, \
             patch("patchlint._run_vng_boot", return_value=(0, uname)):
            boot_test(log_path, Path("/src/linux"))

        assert mock_log.call_count == 2
        cmds = [c.args[0] for c in mock_log.call_args_list]
        assert cmds[0] == ["vng", "--clean"]
        assert cmds[1] == ["vng", "--build", "KCFLAGS=-Wno-error"]

    def test_build_failure_skips_boot(self, tmp_path):
        log_path = tmp_path / "boot.log"

        with patch("patchlint.run_and_log", side_effect=[0, 1]):
            rc, uname = boot_test(log_path, Path("/src/linux"))

        assert rc == 1
        assert uname == ""

    def test_boot_failure(self, tmp_path):
        log_path = tmp_path / "boot.log"

        with patch("patchlint.run_and_log", return_value=0), \
             patch("patchlint._run_vng_boot", return_value=(1, "")):
            rc, uname = boot_test(log_path, Path("/src/linux"))

        assert rc == 1

    def test_boot_logs_output(self, tmp_path):
        log_path = tmp_path / "boot.log"

        with patch("patchlint.run_and_log", return_value=0), \
             patch("patchlint._run_vng_boot",
                   return_value=(0, "Linux (none) 6.12.0-rc1 #1 SMP x86_64 GNU/Linux\n")):
            boot_test(log_path, Path("/src/linux"))

        content = log_path.read_text()
        assert "vng -r" in content
        assert "6.12.0-rc1" in content

    def test_invalid_uname_fails(self, tmp_path):
        """vng exits 0 but output doesn't contain valid uname — should fail."""
        log_path = tmp_path / "boot.log"

        with patch("patchlint.run_and_log", return_value=0), \
             patch("patchlint._run_vng_boot",
                   return_value=(0, "some garbage output\n")):
            rc, uname = boot_test(log_path, Path("/src/linux"))

        assert rc == 1
        assert uname == ""
