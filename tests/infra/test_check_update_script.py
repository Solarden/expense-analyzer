"""Smoke tests for scripts/check_update.sh (Phase 18).

Only the paths that exit BEFORE `git fetch` are exercised (--help, bad args), so
the suite never reaches out to a remote. The version/publish logic is covered in
tests/ha/test_update_notify.py.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = PROJECT_ROOT / "scripts" / "check_update.sh"


def test_script_exists_and_is_executable():
    assert SCRIPT.exists()
    assert SCRIPT.stat().st_mode & 0o111, "check_update.sh should be executable"


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")
def test_is_valid_bash():
    result = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")
def test_help_exits_before_fetching():
    result = subprocess.run(
        ["bash", str(SCRIPT), "--help"], cwd=PROJECT_ROOT, capture_output=True, text=True
    )
    assert result.returncode == 0
    assert "notify" in result.stdout.lower()


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")
def test_unknown_argument_fails():
    result = subprocess.run(
        ["bash", str(SCRIPT), "--bogus"], cwd=PROJECT_ROOT, capture_output=True, text=True
    )
    assert result.returncode != 0
    assert "unknown argument" in result.stderr


def _dry_run(args, env_extra=None):
    import os

    env = {**os.environ, **(env_extra or {})}
    return subprocess.run(
        ["bash", str(SCRIPT), *args, "--dry-run"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        env=env,
    )


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")
def test_dry_run_resolves_a_remote_without_fetching():
    # Dry run reports the remote and exits before any git fetch (so it's offline).
    result = _dry_run([])
    assert result.returncode == 0
    assert "would fetch tags from '" in result.stdout


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")
def test_remote_flag_overrides():
    # A fork can point the check at its own repo (remote name or URL).
    result = _dry_run(["--remote", "upstream"])
    assert "'upstream'" in result.stdout


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")
def test_ea_update_remote_env_is_honored():
    result = _dry_run([], env_extra={"EA_UPDATE_REMOTE": "myfork"})
    assert "'myfork'" in result.stdout


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")
def test_flag_beats_env():
    result = _dry_run(["--remote", "fromflag"], env_extra={"EA_UPDATE_REMOTE": "fromenv"})
    assert "'fromflag'" in result.stdout
