"""Tests for test blurb generation."""
from patchlint import generate_test_blurb


class TestGenerateTestBlurb:
    def test_all_clean(self):
        blurb = generate_test_blurb(
            parent_short="abc123def456",
            allmod_new=[],
            allyesconfig_new=[],
            boot_ok=True,
            uname_output="Linux (none) 6.12.0-rc1 #1 SMP x86_64 GNU/Linux",
        )
        assert "This patch was tested by:" in blurb
        assert "allmodconfig: no new warnings" in blurb
        assert "allyesconfig: no new warnings" in blurb
        assert "compared to abc123def456" in blurb
        assert "Booting defconfig kernel via vng: OK" in blurb
        assert "uname -a: Linux (none) 6.12.0-rc1" in blurb

    def test_new_warnings(self):
        blurb = generate_test_blurb(
            parent_short="abc123def456",
            allmod_new=["drivers/foo.c:LINE:COL: warning: unused [-Wunused]"],
            allyesconfig_new=[],
            boot_ok=True,
            uname_output="Linux 6.12.0",
        )
        assert "1 new warning(s)" in blurb
        assert "allmodconfig" in blurb
        assert "drivers/foo.c" in blurb
        assert "allyesconfig: no new warnings" in blurb

    def test_boot_failed(self):
        blurb = generate_test_blurb(
            parent_short="abc123def456",
            allmod_new=[],
            allyesconfig_new=[],
            boot_ok=False,
            uname_output="",
        )
        assert "FAILED" in blurb
        assert "uname" not in blurb.split("FAILED")[1]

    def test_no_uname_output(self):
        blurb = generate_test_blurb(
            parent_short="abc123def456",
            allmod_new=[],
            allyesconfig_new=[],
            boot_ok=True,
            uname_output="",
        )
        assert "OK" in blurb
        assert "uname" not in blurb

    def test_multiple_new_warnings(self):
        blurb = generate_test_blurb(
            parent_short="abc123def456",
            allmod_new=[],
            allyesconfig_new=[
                "a.c:LINE:COL: warning: one",
                "b.c:LINE:COL: warning: two",
            ],
            boot_ok=True,
            uname_output="Linux 6.12.0",
        )
        assert "2 new warning(s)" in blurb
        assert "    a.c" in blurb
        assert "    b.c" in blurb

    def test_multiple_commits(self):
        blurb = generate_test_blurb(
            parent_short="abc123def456",
            allmod_new=[],
            allyesconfig_new=[],
            boot_ok=True,
            uname_output="",
            commit_count=3,
        )
        assert "This patch and those between it and abc123def456 were tested by:" in blurb

    def test_single_commit(self):
        blurb = generate_test_blurb(
            parent_short="abc123def456",
            allmod_new=[],
            allyesconfig_new=[],
            boot_ok=True,
            uname_output="",
            commit_count=1,
        )
        assert "This patch was tested by:" in blurb

    def test_ends_with_newline(self):
        blurb = generate_test_blurb("x", [], [], True, "")
        assert blurb.endswith("\n")
