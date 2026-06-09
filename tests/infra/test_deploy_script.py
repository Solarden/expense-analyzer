"""Smoke tests for scripts/deploy.sh (Phase 18).

We cannot run a real docker deploy in CI, but we can guard against the script
being syntactically broken or its dry-run plan regressing — the parts that would
silently rot otherwise.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEPLOY = PROJECT_ROOT / "scripts" / "deploy.sh"


def test_deploy_script_exists_and_is_executable():
    assert DEPLOY.exists()
    assert DEPLOY.stat().st_mode & 0o111, "deploy.sh should be executable"


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")
def test_deploy_script_is_valid_bash():
    result = subprocess.run(["bash", "-n", str(DEPLOY)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")
def test_dry_run_prints_plan_without_touching_docker():
    result = subprocess.run(
        ["bash", str(DEPLOY), "--pull", "--keep", "7", "--dry-run"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    out = result.stdout
    assert "DRY RUN" in out
    assert "would pull:        true" in out
    assert "backups to keep:   7" in out


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")
def test_unknown_argument_fails():
    result = subprocess.run(
        ["bash", str(DEPLOY), "--bogus"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "unknown argument" in result.stderr
