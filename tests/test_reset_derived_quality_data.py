"""Test reset_derived_quality_data.py dry-run safety."""
import subprocess, sys, os

SCRIPT = os.path.join(os.path.dirname(__file__), "..", "scripts", "reset_derived_quality_data.py")
PROJECT = os.path.join(os.path.dirname(__file__), "..")


def test_dry_run_no_changes():
    """--dry-run should exit 0 and not change the database."""
    result = subprocess.run(
        [sys.executable, SCRIPT, "--dry-run"],
        capture_output=True, text=True, timeout=30, cwd=PROJECT,
    )
    assert result.returncode == 0, result.stderr
    assert "DRY RUN" in result.stdout


def test_missing_arg_fails():
    """Calling without --dry-run or --apply should fail."""
    result = subprocess.run(
        [sys.executable, SCRIPT],
        capture_output=True, text=True, timeout=30, cwd=PROJECT,
    )
    assert result.returncode != 0


def test_dry_run_preserves_counts():
    """Media and feedback counts should appear in dry-run output."""
    result = subprocess.run(
        [sys.executable, SCRIPT, "--dry-run"],
        capture_output=True, text=True, timeout=30, cwd=PROJECT,
    )
    assert result.returncode == 0
    assert "Preserved" in result.stdout
    assert "media:" in result.stdout
    assert "feedback:" in result.stdout
