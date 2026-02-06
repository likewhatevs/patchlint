# patchlint

Help with basic checking of kernel patches

Runs `checkpatch.pl`, builds `allmodconfig` and `allyesconfig` for both a
baseline and candidate (HEAD), compares warnings, and runs a defconfig boot
test — all five builds in parallel using git worktrees.

## Install

Requires [uv](https://docs.astral.sh/uv/getting-started/installation/). On Linux/macOS:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Then:

```bash
# Run directly without installing:
uvx --from git+https://github.com/likewhatevs/patchlint patchlint HEAD~1

# Or install globally:
uv tool install git+https://github.com/likewhatevs/patchlint
```

## Usage

```bash
# From inside a kernel git repo, compare HEAD against its parent:
patchlint HEAD~1

# Compare against a tag:
patchlint v6.18

# Explicit kernel tree:
patchlint HEAD~1 ~/repos/linux
```

This will:
1. Validate the environment (git repo, clean tree, `vng` on PATH)
2. Run `checkpatch.pl` on the patch series — bail out early if it fails
3. Create 5 git worktrees (2 baseline, 2 candidate, 1 boot)
4. Run all 5 builds in parallel:
   - Baseline `allmodconfig` + `allyesconfig` at BASELINE
   - Candidate `allmodconfig` + `allyesconfig` at HEAD
   - Boot test (defconfig + `vng -r` + `uname -a`) in its own worktree
5. Compare warnings and print a report to stdout

Example output:
```
This patch was tested by:
- Building with allmodconfig: no new warnings (compared to abc123def456)
- Building with allyesconfig: no new warnings (compared to abc123def456)
- Booting defconfig kernel via vng: OK
  uname -a: Linux (none) 6.18.0 #1 SMP x86_64 GNU/Linux
```

Exit codes: 0 = clean, 1 = new warnings or boot failure, 2 = build/checkpatch failure.

## Requirements

- [uv](https://docs.astral.sh/uv/getting-started/installation/) (see Install above)
- Python 3.11+
- `vng` (virtme-ng) on PATH
- A kernel git repository
- `ccache` recommended for faster rebuilds
