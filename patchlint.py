#!/usr/bin/env python3
"""patchlint — automate kernel patch warning testing and generate test blurbs."""
from __future__ import annotations

import os
import re
import signal
import shutil
import subprocess
import sys
import tempfile
import threading
from collections import deque
from concurrent.futures import ThreadPoolExecutor, Future
from contextlib import contextmanager, ExitStack
from pathlib import Path
from typing import IO, Iterable, Iterator

import click

# ---------------------------------------------------------------------------
# Graceful shutdown on Ctrl-C
# ---------------------------------------------------------------------------

_shutdown = threading.Event()
_child_procs: list[subprocess.Popen[str]] = []
_child_pids: list[int] = []  # raw pids from os.forkpty()
_child_procs_lock = threading.Lock()


def _sigint_handler(signum: int, frame: object) -> None:
    """Kill all child processes and signal threads to stop."""
    _shutdown.set()
    with _child_procs_lock:
        for proc in _child_procs:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except (ProcessLookupError, OSError):
                pass
        for pid in _child_pids:
            try:
                os.kill(pid, signal.SIGTERM)
            except (ProcessLookupError, OSError):
                pass
    click.secho("\n⚠️  Interrupted — cleaning up worktrees...", fg="yellow", err=True)
    raise KeyboardInterrupt

# ---------------------------------------------------------------------------
# Warning extraction & normalization
# ---------------------------------------------------------------------------

ANSI_RE = re.compile(r"\x1B\[[0-9;]*[A-Za-z]")

# Match gcc/clang format:  file.c:123:45: warning: msg [-Wflag]
# Match make format:       Makefile:15: warning: msg
COMPILER_WARNING_RE = re.compile(
    r"\S+:\d+(?::\d+)?:\s+warning:",
    re.IGNORECASE,
)

WARNING_LOC_COLON_RE = re.compile(r":[0-9]+(:[0-9]+)(\s*:\s*warning:)", re.IGNORECASE)
WARNING_LOC_LINE_RE = re.compile(r":[0-9]+(\s*:\s*warning:)", re.IGNORECASE)


def normalize_warning_line(line: str) -> str:
    """Normalize a warning line for comparison: strip ANSI, collapse whitespace, normalize locations."""
    s = ANSI_RE.sub("", line)
    s = re.sub(r"\s+", " ", s).rstrip()
    s = re.sub(r"(^|\s)\./", r"\1", s)
    s = WARNING_LOC_COLON_RE.sub(r":LINE:COL\2", s)
    s = WARNING_LOC_LINE_RE.sub(r":LINE\1", s)
    return s


def extract_normalized_warnings(lines: Iterable[str]) -> list[str]:
    """Extract and deduplicate normalized warning lines."""
    out: set[str] = set()
    for raw in lines:
        clean = ANSI_RE.sub("", raw)
        if COMPILER_WARNING_RE.search(clean):
            out.add(normalize_warning_line(raw))
    return sorted(out)


def compare_warnings(baseline: Path, candidate: Path) -> list[str]:
    """Compare two log files and return a sorted list of new warnings in candidate."""
    with baseline.open(errors="replace") as fh:
        base_warnings = extract_normalized_warnings(fh)
    with candidate.open(errors="replace") as fh:
        cand_warnings = extract_normalized_warnings(fh)
    return sorted(set(cand_warnings) - set(base_warnings))


# ---------------------------------------------------------------------------
# Subprocess helpers
# ---------------------------------------------------------------------------


def _spawn(cmd: list[str], *, cwd: Path | None = None) -> subprocess.Popen[str]:
    """Spawn a subprocess in its own process group and track it for cleanup."""
    if _shutdown.is_set():
        raise KeyboardInterrupt
    proc = subprocess.Popen(
        cmd,
        cwd=str(cwd) if cwd else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        start_new_session=True,
    )
    with _child_procs_lock:
        _child_procs.append(proc)
    return proc


def _reap(proc: subprocess.Popen[str]) -> int:
    """Wait for process and untrack it."""
    rc = proc.wait()
    with _child_procs_lock:
        try:
            _child_procs.remove(proc)
        except ValueError:
            pass
    return rc


def run_and_log(cmd: list[str], log_fh: IO[str], *, cwd: Path | None = None) -> int:
    """Run *cmd*, writing output only to *log_fh* (no stderr — safe for parallel use)."""
    proc = _spawn(cmd, cwd=cwd)
    assert proc.stdout is not None
    for line in iter(proc.stdout.readline, ''):
        log_fh.write(line)
    return _reap(proc)


def run_capture(cmd: list[str], *, cwd: Path | None = None) -> str:
    return subprocess.check_output(
        cmd, cwd=str(cwd) if cwd else None, text=True, stderr=subprocess.PIPE
    )


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------


def check_git_repo(kernel_dir: Path) -> None:
    """Raise UsageError if *kernel_dir* is not inside a git repository."""
    try:
        subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            cwd=str(kernel_dir),
            check=True,
            capture_output=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        raise click.UsageError(f"{kernel_dir} is not a git repository") from exc


def check_clean_tree(kernel_dir: Path) -> None:
    """Raise UsageError if the working tree has uncommitted changes."""
    try:
        subprocess.run(
            ["git", "diff", "--quiet"],
            cwd=str(kernel_dir),
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "diff", "--cached", "--quiet"],
            cwd=str(kernel_dir),
            check=True,
            capture_output=True,
        )
    except subprocess.CalledProcessError as exc:
        raise click.UsageError(
            "Working tree is dirty — commit or stash changes first"
        ) from exc


def check_vng() -> None:
    """Raise UsageError if vng (virtme-ng) is not on PATH."""
    if not shutil.which("vng"):
        raise click.UsageError("vng (virtme-ng) not found on PATH")


def resolve_rev_short(kernel_dir: Path, rev: str) -> str:
    """Resolve *rev* to a short commit hash."""
    return run_capture(
        ["git", "rev-parse", "--short=12", rev], cwd=kernel_dir
    ).strip()


@contextmanager
def git_worktree(kernel_dir: Path, rev: str) -> Iterator[Path]:
    """Create a detached worktree for *rev*, yield its path, remove on exit."""
    # Place worktrees on the same filesystem as the kernel tree to avoid
    # filling up tmpfs — allmodconfig/allyesconfig builds are very large.
    wt = Path(tempfile.mkdtemp(prefix="patchlint-", dir=str(kernel_dir.parent)))
    try:
        subprocess.run(
            ["git", "worktree", "add", "--detach", str(wt), rev],
            cwd=str(kernel_dir),
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        shutil.rmtree(wt, ignore_errors=True)
        msg = f"failed to create worktree for {rev}"
        if exc.stderr:
            msg += f"\n{exc.stderr.strip()}"
        raise click.ClickException(msg) from exc
    try:
        yield wt
    finally:
        rm_result = subprocess.run(
            ["git", "worktree", "remove", "--force", str(wt)],
            cwd=str(kernel_dir),
            capture_output=True,
            text=True,
        )
        if rm_result.returncode != 0:
            click.secho(
                f"⚠️  failed to remove worktree: {wt.name}",
                fg="yellow", err=True,
            )


# ---------------------------------------------------------------------------
# Checkpatch
# ---------------------------------------------------------------------------


def run_checkpatch(kernel_dir: Path, baseline: str) -> None:
    """Run checkpatch.pl on the commit range baseline..HEAD.

    Raises SystemExit(2) if checkpatch reports issues or the script is missing.
    """
    checkpatch = kernel_dir / "scripts" / "checkpatch.pl"
    if not checkpatch.exists():
        click.secho(
            "❌ scripts/checkpatch.pl not found in kernel tree",
            fg="red", err=True,
        )
        sys.exit(2)

    click.secho("📋 Running checkpatch...", fg="yellow", err=True)

    git_proc = subprocess.Popen(
        ["git", "format-patch", "--stdout", f"{baseline}..HEAD"],
        cwd=str(kernel_dir),
        stdout=subprocess.PIPE,
    )
    cp_proc = subprocess.Popen(
        [str(checkpatch), "-"],
        cwd=str(kernel_dir),
        stdin=git_proc.stdout,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    # Allow git_proc to receive SIGPIPE if checkpatch exits early.
    assert git_proc.stdout is not None
    git_proc.stdout.close()

    assert cp_proc.stdout is not None
    output = cp_proc.stdout.read()
    cp_proc.wait()
    git_proc.wait()

    if cp_proc.returncode != 0:
        click.secho(
            "❌ checkpatch failed — fix issues before continuing",
            fg="red", err=True,
        )
        click.echo(output, err=True, nl=False)
        sys.exit(2)

    click.secho("✅ checkpatch passed", fg="green", err=True)


# ---------------------------------------------------------------------------
# Build helpers
# ---------------------------------------------------------------------------

WARN_CONFIGS = ("allmodconfig", "allyesconfig")


def build_config(log_path: Path, kernel_dir: Path, config_name: str) -> int:
    """Build *config_name* in *kernel_dir*, writing full output to *log_path*.

    Always cleans first to ensure a known-good starting state.
    Uses ``make KCFLAGS=-Wno-error`` to ensure warnings are never promoted to
    errors, regardless of CONFIG_WERROR.
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    header = [
        "build-log mode",
        f"log={log_path}",
        f"kernel_dir={kernel_dir}",
        f"config={config_name}",
    ]
    commands = [
        ["vng", "--clean"],
        ["make", config_name],
        ["./scripts/config", "-d", "WERROR"],
        ["make", "olddefconfig"],
        ["vng", "--build", "--skip-config", "KCFLAGS=-Wno-error"],
    ]
    with log_path.open("w", encoding="utf-8") as log_fh:
        for line in header:
            log_fh.write(f"# {line}\n")
        for cmd in commands:
            log_fh.write(f"# cmd: {' '.join(cmd)}\n")
            rc = run_and_log(cmd, log_fh, cwd=kernel_dir)
            if rc != 0:
                return rc
    return 0


BOOT_TIMEOUT = 60  # seconds


def _run_vng_boot(kernel_dir: Path) -> tuple[int, str]:
    """Boot kernel via vng and return (exit_code, raw_output).

    Uses os.forkpty() because vng requires a controlling terminal with
    valid PTY file descriptors at /proc/self/fd/{0,1,2} — plain pipes
    and pty.openpty()+Popen both fail its isatty/O_RDWR checks.

    Passes ``panic=-1`` so a kernel panic causes immediate reboot (VM exit)
    instead of hanging forever.  A Python-level timeout acts as a safety net.
    """
    if _shutdown.is_set():
        return 1, ""
    kdir = kernel_dir.resolve()
    boot_cmd = [
        "vng", "-r", str(kdir),
        "--append", "panic=-1",
        "-e", "uname -a",
    ]

    pid, fd = os.forkpty()
    if pid == 0:
        # Child — exec vng with a proper controlling terminal.
        # Must catch all exceptions: if execlp/chdir fails, the child
        # holds a copy of the parent's interpreter and must not be
        # allowed to unwind into parent cleanup code.
        try:
            os.chdir(str(kdir))
            os.execlp(boot_cmd[0], *boot_cmd)
        except BaseException:
            pass
        os._exit(127)

    # Parent — track pid for Ctrl-C cleanup.
    with _child_procs_lock:
        _child_pids.append(pid)

    # Watchdog timer: kill child if it exceeds BOOT_TIMEOUT.
    timed_out = threading.Event()

    def _kill_on_timeout() -> None:
        timed_out.set()
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    timer = threading.Timer(BOOT_TIMEOUT, _kill_on_timeout)
    timer.start()

    output_chunks: list[bytes] = []
    try:
        while True:
            try:
                data = os.read(fd, 4096)
                if not data:
                    break
                output_chunks.append(data)
            except OSError:
                break
    finally:
        timer.cancel()
        os.close(fd)

    _, status = os.waitpid(pid, 0)
    rc = os.WEXITSTATUS(status) if os.WIFEXITED(status) else 1

    with _child_procs_lock:
        try:
            _child_pids.remove(pid)
        except ValueError:
            pass

    if timed_out.is_set():
        return 1, b"".join(output_chunks).decode(errors="replace") + "\n[boot timed out]\n"

    return rc, b"".join(output_chunks).decode(errors="replace")


def boot_test(log_path: Path, kernel_dir: Path) -> tuple[int, str]:
    """Build defconfig and boot via vng, returning (exit_code, uname_output).

    Always cleans first to ensure a known-good starting state.
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    header = [
        "boot-check mode",
        f"log={log_path}",
        f"kernel_dir={kernel_dir}",
    ]
    # Let vng handle its own config via virtme-configkernel, which adds
    # the 9p/virtio options required for virtme's root filesystem.
    # Manual "make defconfig" + "--skip-config" produces an unbootable kernel.
    build_commands = [
        ["vng", "--clean"],
        ["vng", "--build", "KCFLAGS=-Wno-error"],
    ]
    with log_path.open("w", encoding="utf-8") as log_fh:
        for line in header:
            log_fh.write(f"# {line}\n")
        for cmd in build_commands:
            log_fh.write(f"# cmd: {' '.join(cmd)}\n")
            rc = run_and_log(cmd, log_fh, cwd=kernel_dir)
            if rc != 0:
                return rc, ""

    # Boot and capture uname output.
    boot_rc, boot_output = _run_vng_boot(kernel_dir)
    # Strip ANSI escape sequences and extract clean uname output
    clean = ANSI_RE.sub("", boot_output).strip()
    # Find the uname -a line: "Linux <host> <ver> ... GNU/Linux"
    uname_output = ""
    for line in reversed(clean.splitlines()):
        line = line.strip()
        if re.match(r"Linux\s+\S+\s+\S+\s+#\d+\s+.*\s+GNU/Linux$", line):
            uname_output = line
            break
    with log_path.open("a", encoding="utf-8") as log_fh:
        log_fh.write(f"# cmd: vng -r {kernel_dir} -e 'uname -a'\n")
        log_fh.write(boot_output)
    # If vng exited 0 but we couldn't find a valid uname line, that's a failure.
    if boot_rc == 0 and not uname_output:
        boot_rc = 1
    return boot_rc, uname_output


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


def generate_test_blurb(
    parent_short: str,
    allmod_new: list[str],
    allyesconfig_new: list[str],
    boot_ok: bool,
    uname_output: str,
    commit_count: int = 1,
) -> str:
    """Generate a copy-pasteable test section for a commit message."""
    if commit_count > 1:
        header = f"This patch and those between it and {parent_short} were tested by:"
    else:
        header = "This patch was tested by:"
    lines: list[str] = [header]

    for cfg, new_warnings in [
        ("allmodconfig", allmod_new),
        ("allyesconfig", allyesconfig_new),
    ]:
        if new_warnings:
            lines.append(
                f"- Building with {cfg}: {len(new_warnings)} new warning(s) "
                f"(compared to {parent_short})"
            )
            for w in new_warnings:
                lines.append(f"    {w}")
        else:
            lines.append(
                f"- Building with {cfg}: no new warnings "
                f"(compared to {parent_short})"
            )

    if boot_ok:
        lines.append("- Booting defconfig kernel via vng: OK")
        if uname_output:
            lines.append(f"  uname -a: {uname_output}")
    else:
        lines.append("- Booting defconfig kernel via vng: FAILED")

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Error display helpers
# ---------------------------------------------------------------------------


def _show_log_tail(log_path: Path, n: int = 5) -> None:
    """Print log path and last *n* non-empty lines to stderr."""
    if not log_path.exists():
        return
    click.echo(f"   log: {log_path}", err=True)
    with log_path.open(errors="replace") as fh:
        tail = deque((l.rstrip() for l in fh if l.strip()), maxlen=n)
    for l in tail:
        click.echo(f"   {l}", err=True)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


@click.command(options_metavar="")
@click.argument("baseline")
@click.argument(
    "kernel_dir",
    default=".",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
)
def main(baseline: str, kernel_dir: Path) -> None:
    """Run checkpatch, build baseline & candidate, compare warnings, and boot test.

    BASELINE is the git revision to compare against (e.g. HEAD~1, a commit
    hash, or a tag).  KERNEL_DIR is the kernel source tree (default: current
    directory).
    """
    kernel_dir = kernel_dir.resolve()
    check_vng()
    check_git_repo(kernel_dir)
    check_clean_tree(kernel_dir)

    # Validate revisions before expensive operations
    try:
        parent_short = resolve_rev_short(kernel_dir, baseline)
    except subprocess.CalledProcessError as exc:
        click.secho(f"❌ unknown revision: {baseline}", fg="red", err=True)
        if exc.stderr:
            click.echo(exc.stderr.strip(), err=True)
        sys.exit(2)
    try:
        head_rev = resolve_rev_short(kernel_dir, "HEAD")
    except subprocess.CalledProcessError as exc:
        click.secho("❌ failed to resolve HEAD", fg="red", err=True)
        if exc.stderr:
            click.echo(exc.stderr.strip(), err=True)
        sys.exit(2)

    commit_count = int(run_capture(
        ["git", "rev-list", "--count", f"{baseline}..HEAD"], cwd=kernel_dir
    ).strip())

    # Run checkpatch before expensive builds
    run_checkpatch(kernel_dir, baseline)

    _shutdown.clear()
    prev_handler = signal.signal(signal.SIGINT, _sigint_handler)

    try:
        click.secho(f"🔍 Baseline: {parent_short}  Candidate: {head_rev}", fg="cyan", err=True)

        tmp = Path(tempfile.mkdtemp(prefix="patchlint-logs-"))

        # 5 worktrees: 2 baseline, 2 candidate @ HEAD, 1 boot @ HEAD.
        # Each gets its own directory so all 5 builds run simultaneously
        # without touching the main working tree.
        with ExitStack() as stack:
            click.secho("🌲 Creating worktrees...", fg="yellow", err=True)
            wt_baseline: dict[str, Path] = {}
            wt_candidate: dict[str, Path] = {}
            for cfg in WARN_CONFIGS:
                wt_baseline[cfg] = stack.enter_context(
                    git_worktree(kernel_dir, baseline)
                )
                wt_candidate[cfg] = stack.enter_context(
                    git_worktree(kernel_dir, "HEAD")
                )
            wt_boot = stack.enter_context(
                git_worktree(kernel_dir, "HEAD")
            )
            click.secho("✅ Worktrees ready", fg="green", err=True)

            # Launch all 5 builds in parallel
            click.secho("🔨 Launching 5 parallel builds...", fg="yellow", err=True)
            baseline_logs: dict[str, Path] = {}
            candidate_logs: dict[str, Path] = {}
            futures: dict[str, Future[int | tuple[int, str]]] = {}

            with ThreadPoolExecutor(max_workers=5) as pool:
                for cfg in WARN_CONFIGS:
                    b_log = tmp / "baseline" / f"{cfg}.log"
                    baseline_logs[cfg] = b_log
                    futures[f"baseline-{cfg}"] = pool.submit(
                        build_config, b_log, wt_baseline[cfg], cfg,
                    )

                    c_log = tmp / "candidate" / f"{cfg}.log"
                    candidate_logs[cfg] = c_log
                    futures[f"candidate-{cfg}"] = pool.submit(
                        build_config, c_log, wt_candidate[cfg], cfg,
                    )

                boot_log = tmp / "candidate" / "defconfig-boot.log"
                futures["boot"] = pool.submit(boot_test, boot_log, wt_boot)

            # Collect results
            build_failed = False
            for label, fut in futures.items():
                if label == "boot":
                    continue
                rc = fut.result()
                if rc != 0:
                    click.secho(f"❌ [{label}] build failed", fg="red", err=True)
                    log_path = None
                    if label.startswith("baseline-"):
                        log_path = baseline_logs.get(label.removeprefix("baseline-"))
                    elif label.startswith("candidate-"):
                        log_path = candidate_logs.get(label.removeprefix("candidate-"))
                    if log_path:
                        _show_log_tail(log_path)
                    build_failed = True
                else:
                    click.secho(f"✅ [{label}] done", fg="green", err=True)

            if build_failed:
                click.echo(f"   logs: {tmp}", err=True)
                sys.exit(2)

            # Compare warnings
            results: dict[str, list[str]] = {}
            for cfg in WARN_CONFIGS:
                results[cfg] = compare_warnings(baseline_logs[cfg], candidate_logs[cfg])

            # Collect boot result
            boot_rc, uname_output = futures["boot"].result()
            boot_ok = boot_rc == 0
            if boot_ok:
                click.secho("✅ [boot] OK", fg="green", err=True)
            else:
                click.secho("❌ [boot] FAILED", fg="red", err=True)
                _show_log_tail(boot_log)

        # Print blurb to stdout
        blurb = generate_test_blurb(
            parent_short,
            results["allmodconfig"],
            results["allyesconfig"],
            boot_ok,
            uname_output,
            commit_count,
        )
        click.echo(blurb)

        has_failures = any(results[cfg] for cfg in results) or not boot_ok
        if has_failures:
            click.echo(f"   logs: {tmp}", err=True)
        else:
            shutil.rmtree(tmp, ignore_errors=True)
        sys.exit(1 if has_failures else 0)
    finally:
        signal.signal(signal.SIGINT, prev_handler)


if __name__ == "__main__":
    main()
