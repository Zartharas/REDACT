"""
Shared pytest fixtures for the tests/ suite.

Design choice, stated up front: these tests do NOT reimplement the logic
already living in validation/*.py and src/*.py. Every existing validation
script in this repo was built and audited as a standalone, human-readable
tool (see BUGS_AND_FIXES.md's own "why" for keeping one implementation
instead of a second reimplementation -- the overlapping-span bug happened
once already from exactly that mistake). Reimplementing their assertions
inside pytest would recreate that same risk: two copies of the same check
that can silently drift apart.

Instead, these tests run each script as a real subprocess (the same way a
human runs `python validation/whatever_test.py`) and assert on its exit
code and/or printed output. This is slightly less "pytest-native" than
importing and calling functions directly, but it means CI is asserting on
the exact same code path a person running the script by hand would see --
no separate pytest-only code path that could pass while the real script
fails, or vice versa.
"""
import os
import subprocess
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture
def repo_root():
    return REPO_ROOT


def run_script(relative_path: str, timeout: int = 120, env: dict | None = None) -> subprocess.CompletedProcess:
    """Runs a validation script exactly as a human would from the repo
    root, and returns the completed process (exit code + captured
    stdout/stderr) for the caller to assert against."""
    full_env = dict(os.environ)
    if env:
        full_env.update(env)
    return subprocess.run(
        [sys.executable, relative_path],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=full_env,
    )
