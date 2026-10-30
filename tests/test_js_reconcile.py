"""Run the standalone reconciliation unit tests with Node's built-in runner."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_lyric_reconciliation_unit_tests():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is not installed; skipping reconciliation unit tests.")
    result = subprocess.run(
        [node, "--test", "tests/js/reconcile.test.js"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
