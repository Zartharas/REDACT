"""
Redis-backed regression tests. Skipped automatically if no Redis is
reachable at REDIS_URL (default redis://localhost:6379/0) -- CI supplies
one as a service container (see .github/workflows/ci.yml); locally, run:

    docker run -d --rm -p 6379:6379 --name redact-test-redis redis:7
    pytest tests/test_redis_validation.py
    docker stop redact-test-redis
"""
import os
import socket
from urllib.parse import urlparse

import pytest

# See test_fast_validation.py's own comment on this import for why it's
# `conftest`, not `tests.conftest` -- the latter only worked under
# `python -m pytest`, not a bare `pytest` invocation.
from conftest import run_script

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")


def _redis_reachable() -> bool:
    parsed = urlparse(REDIS_URL)
    host, port = parsed.hostname or "localhost", parsed.port or 6379
    try:
        with socket.create_connection((host, port), timeout=2):
            return True
    except OSError:
        return False


pytestmark = pytest.mark.needs_redis
requires_redis = pytest.mark.skipif(
    not _redis_reachable(), reason=f"no Redis reachable at {REDIS_URL}"
)


@requires_redis
def test_redis_storage_provider_basic():
    result = run_script("validation/redis_storage_provider_test.py", timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ALL CHECKS PASSED" in result.stdout


@requires_redis
def test_multiprocess_redis_zero_loss():
    """Bug 14/15 Redis-path regression guard: 8 processes x 50 tokens,
    0 of 400 reverse-map entries should ever be lost."""
    result = run_script("validation/multiprocess_redis_test.py", timeout=90)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "RESULT: PASS" in result.stdout
    assert "Reverse-map entries lost (unrecoverable via resolve()): 0" in result.stdout
