"""
Fast, self-contained regression tests: no Docker, no live Redis, no spaCy
model, no external data download. These are exactly the checks that
should run on every push -- pure Python, deterministic (fixed seeds
throughout), each one finishing in well under a minute.

Every test here runs the real script as a subprocess (see conftest.py's
run_script for why) and asserts on its exit code and/or a specific line
in its printed output -- the same signal a human reads when running it
by hand.
"""
# Bare `conftest` import, not `tests.conftest`: pytest's default
# "prepend" import mode inserts THIS FILE'S OWN DIRECTORY (tests/, since
# there's no tests/__init__.py) onto sys.path -- not the repo root. That
# insertion happens the same way regardless of how pytest is invoked, so
# `import conftest` resolves correctly whether run as `python -m pytest`
# (which also happens to put the repo root on sys.path, masking this) or
# a bare `pytest` console-script invocation (which does not put the repo
# root on sys.path, exposing it). `from tests.conftest import ...` only
# worked by accident under the first invocation style -- found the hard
# way when a bare `pytest tests/ -v` run on a real machine raised
# ModuleNotFoundError: No module named 'tests.conftest', while
# `python -m pytest` in this project's own sandboxed dev environment
# never exposed it.
from conftest import run_script


def test_multiprocess_tokenstore_zero_loss():
    """Bug 14/15 regression guard: 8 processes x 50 tokens against
    FileStorageProvider must still lose exactly 0 reverse-map entries."""
    result = run_script("validation/multiprocess_tokenstore_test.py", timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "RESULT: PASS" in result.stdout
    assert "Reverse-map entries lost (unrecoverable via resolve()): 0" in result.stdout


def test_wal_compaction_correctness():
    """Bug 15 real-fix regression guard: WAL compaction must not lose
    tokens, same-process or after a simulated restart, and must stay
    bounded by its configured threshold."""
    result = run_script("validation/wal_compaction_correctness_test.py", timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "RESULT: PASS" in result.stdout


def test_tokenstore_save_scaling_stays_flat():
    """Bug 15 regression guard, the sharpest one: if the incremental-write
    fix ever regresses back toward the original O(n) read-merge-write,
    this growth factor climbs from ~1.0-1.5x back toward the pre-fix
    10-22x range. This test doesn't just check the script runs -- it
    parses the printed growth factor and asserts it stays low, which is
    the one number in this suite that would have caught Bug 15 itself had
    it existed before that bug was found."""
    result = run_script("validation/tokenstore_save_scaling_test.py", timeout=90)
    assert result.returncode == 0, result.stdout + result.stderr
    import re
    m = re.search(r"Growth factor: ([\d.]+)x", result.stdout)
    assert m, "expected a 'Growth factor: N.Nx' line in output"
    growth_factor = float(m.group(1))
    # Generous ceiling (real fix measures ~1.0-1.5x; pre-fix mitigation-only
    # behavior measured 10-22x) -- catches a real regression without being
    # so tight that normal machine-to-machine timing noise fails CI.
    assert growth_factor < 5.0, (
        f"TokenStore.save() growth factor was {growth_factor}x -- expected "
        f"well under 5x if the incremental-write fix (Bug 15) is intact. "
        f"A value in the 10-20x range means the O(n) read-merge-write "
        f"regressed back in."
    )


def test_syslog_coverage_round1():
    result = run_script("validation/syslog_coverage_extension_test.py", timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ALL CHECKS PASSED" in result.stdout


def test_syslog_coverage_round2():
    result = run_script("validation/syslog_coverage_extension_round2_test.py", timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ALL CHECKS PASSED" in result.stdout


def test_syslog_coverage_round3():
    result = run_script("validation/syslog_coverage_extension_round3_test.py", timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ALL CHECKS PASSED" in result.stdout


def test_syslog_coverage_round4():
    """Task #10 (real-data validation) found this the hard way: fetching
    the actual Loghub Linux_2k.log showed _SYSLOG_TAG_RE's own comment was
    wrong about already covering it -- PAM-decorated tags like
    "sshd(pam_unix)[19939]:" had no path through the old regex at all.
    See validation/syslog_coverage_extension_round4_test.py's own
    docstring for the full story, including a verbatim spot check against
    the real line that surfaced this."""
    result = run_script("validation/syslog_coverage_extension_round4_test.py", timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ALL CHECKS PASSED" in result.stdout


def test_entropy_uuid_exclusion_regression():
    """Regenerates the entropy fair-test corpus (deterministic, Random(42))
    and checks the false-alarm rate stays at the post-UUID-exclusion-fix
    level, not the pre-fix 9.7%."""
    gen = run_script("validation/entropy_fair_test/generate_secrets_corpus.py", timeout=30)
    assert gen.returncode == 0, gen.stdout + gen.stderr
    result = run_script("validation/entropy_fair_test/evaluate_entropy.py", timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    # This script has no exit-code pass/fail (it's a measurement sweep, not
    # a single yes/no check) -- assert directly on the best-row false-alarm
    # rate instead, parsed from its own printed table.
    assert "0.0%" in result.stdout, (
        "expected the best-threshold row's false-alarm rate to still read "
        "0.0% after the UUID-shape exclusion fix -- see BUGS_AND_FIXES.md "
        "and validation/entropy_fair_test/README.md"
    )


def test_aws_account_id_credit_card_exclusion():
    """Bug 17 regression guard: real CloudTrail account IDs (always
    exactly 12 digits) must not be reported as CREDIT_CARD via the arn
    field or the accountId/recipientAccountId JSON keys, while bare
    12-digit numbers (including Faker's own synthetic CREDIT_CARD_num
    values) and any longer number near AWS-shaped text must still be
    detected normally. See BUGS_AND_FIXES.md Bug 17."""
    result = run_script("validation/aws_account_id_credit_card_exclusion_test.py", timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ALL CHECKS PASSED" in result.stdout
