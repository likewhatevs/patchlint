"""Tests for warning regex, normalization, and comparison."""
from pathlib import Path

from patchlint import (
    COMPILER_WARNING_RE,
    extract_normalized_warnings,
    normalize_warning_line,
    compare_warnings,
)


# --- COMPILER_WARNING_RE matching ---


class TestCompilerWarningRE:
    def test_gcc_format(self):
        assert COMPILER_WARNING_RE.search("drivers/net/foo.c:123:45: warning: unused variable")

    def test_clang_format(self):
        assert COMPILER_WARNING_RE.search("arch/x86/mm/init.c:42:10: warning: implicit conversion [-Wconversion]")

    def test_make_format(self):
        assert COMPILER_WARNING_RE.search("Makefile:15: warning: overriding recipe for target")

    def test_line_only_no_column(self):
        assert COMPILER_WARNING_RE.search("fs/ext4/super.c:100: warning: old-style declaration")

    def test_rejects_plain_text_with_warning_word(self):
        assert not COMPILER_WARNING_RE.search("This is a warning about the build process")

    def test_rejects_error_line(self):
        assert not COMPILER_WARNING_RE.search("drivers/foo.c:10:2: error: undeclared identifier")

    def test_rejects_comment_line(self):
        assert not COMPILER_WARNING_RE.search("# cmd: make allmodconfig")

    def test_rejects_empty_line(self):
        assert not COMPILER_WARNING_RE.search("")

    def test_rejects_cc_progress_line(self):
        assert not COMPILER_WARNING_RE.search("  CC      kernel/sched/fair.o")


# --- normalize_warning_line ---


class TestNormalizeWarningLine:
    def test_strips_ansi(self):
        raw = "\x1B[01;35mfoo.c:10:2: warning: bar\x1B[0m"
        result = normalize_warning_line(raw)
        assert "\x1B" not in result
        assert "foo.c" in result

    def test_collapses_whitespace(self):
        raw = "foo.c:10:2:   warning:    lots   of   space"
        result = normalize_warning_line(raw)
        assert "  " not in result

    def test_normalizes_line_col(self):
        result = normalize_warning_line("foo.c:123:45: warning: unused")
        assert ":LINE:COL:" in result
        assert ":123:" not in result

    def test_normalizes_line_only(self):
        result = normalize_warning_line("foo.c:99: warning: old-style")
        assert ":LINE:" in result
        assert ":99:" not in result

    def test_strips_dot_slash_prefix(self):
        result = normalize_warning_line("./drivers/net/foo.c:10:2: warning: x")
        assert result.startswith("drivers/")


# --- extract_normalized_warnings ---


class TestExtractNormalizedWarnings:
    def test_extracts_warnings_from_mixed_output(self):
        lines = [
            "  CC      kernel/fork.o",
            "drivers/foo.c:10:2: warning: unused variable 'x' [-Wunused-variable]",
            "  LD      vmlinux",
            "net/core/sock.c:890:3: warning: format issue [-Wformat=]",
        ]
        result = extract_normalized_warnings(lines)
        assert len(result) == 2

    def test_deduplicates_same_warning_different_line_numbers(self):
        lines = [
            "foo.c:10:2: warning: unused [-Wunused]",
            "foo.c:20:5: warning: unused [-Wunused]",
        ]
        result = extract_normalized_warnings(lines)
        assert len(result) == 1

    def test_ansi_colored_warning_extracted(self):
        """ANSI codes around 'warning:' should not prevent extraction."""
        # Simulates GCC colorized output: bold+magenta around "warning:"
        line = (
            "\x1B[01m\x1B[Kfoo.c:10:2:\x1B[m\x1B[K"
            " \x1B[01;35m\x1B[Kwarning:\x1B[m\x1B[K"
            " unused variable 'x' [-Wunused-variable]"
        )
        result = extract_normalized_warnings([line])
        assert len(result) == 1
        assert "foo.c" in result[0]
        assert "unused variable" in result[0]

    def test_empty_input(self):
        assert extract_normalized_warnings([]) == []

    def test_no_warnings(self):
        lines = ["  CC      kernel/fork.o", "  LD      vmlinux"]
        assert extract_normalized_warnings(lines) == []

    def test_fixture_allmodconfig(self):
        fixture = Path(__file__).parent / "fixtures" / "allmodconfig_warnings.log"
        warnings = extract_normalized_warnings(
            fixture.read_text(errors="replace").splitlines()
        )
        assert len(warnings) == 4

    def test_fixture_clean_build(self):
        fixture = Path(__file__).parent / "fixtures" / "clean_build.log"
        warnings = extract_normalized_warnings(
            fixture.read_text(errors="replace").splitlines()
        )
        assert warnings == []

    def test_fixture_kernel_error_gcc(self):
        fixture = Path(__file__).parent / "fixtures" / "kernel_error_gcc.log"
        warnings = extract_normalized_warnings(
            fixture.read_text(errors="replace").splitlines()
        )
        assert warnings == []


# --- compare_warnings ---


class TestCompareWarnings:
    def test_no_new_warnings(self, tmp_path):
        base = tmp_path / "base.log"
        cand = tmp_path / "cand.log"
        base.write_text("a.c:10:2: warning: foo\n")
        cand.write_text("a.c:99:44: warning: foo\n")
        assert compare_warnings(base, cand) == []

    def test_detects_new_warning(self, tmp_path):
        base = tmp_path / "base.log"
        cand = tmp_path / "cand.log"
        base.write_text("a.c:10:2: warning: foo\n")
        cand.write_text("a.c:10:2: warning: foo\nb.c:1:1: warning: bar\n")
        new = compare_warnings(base, cand)
        assert len(new) == 1
        assert "bar" in new[0]

    def test_removed_warning_not_flagged(self, tmp_path):
        base = tmp_path / "base.log"
        cand = tmp_path / "cand.log"
        base.write_text("a.c:10:2: warning: foo\nb.c:1:1: warning: bar\n")
        cand.write_text("a.c:10:2: warning: foo\n")
        assert compare_warnings(base, cand) == []
