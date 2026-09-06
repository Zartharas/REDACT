"""
Tests anonymize.load_policy() -- Phase 1 of
DETECTION_POLICY_DECOUPLING_SCOPING.md ("Engineering upgrade 18",
BUGS_AND_FIXES.md): externalizing PSEUDONYMIZE_TYPES/TOKENIZE_TYPES/
REDACT_TYPES from hardcoded module constants into an optional external
JSON file, with a fail-closed validator.

Covers, directly against anonymize.py (no Flask/spaCy involved -- this is
pure policy-loading logic, doesn't need service_app-style mocking):
  1. No file at the resolved path -> falls back to the built-in defaults
     (fail OPEN, not an error -- see load_policy()'s own docstring for why
     this case is handled differently from an invalid file).
  2. A valid custom file -> the three module-level sets are actually
     replaced with its contents.
  3. A canonical type left out of all three buckets -> PolicyConfigError
     (the specific fail-closed case DETECTION_POLICY_DECOUPLING_SCOPING.md's
     Phase 1 devil's-advocate section calls out by name).
  4. A type assigned to two buckets at once -> PolicyConfigError.
  5. Malformed JSON -> PolicyConfigError, not a raw JSONDecodeError leaking
     out (callers should only ever need to catch one exception type).
  6. A non-list value for one of the three keys -> PolicyConfigError.
  7. Extra/unknown top-level keys (e.g. "_comment") are ignored, not
     rejected -- config/policy.json itself relies on this.
  8. End-to-end: importing src/service.py with REDACT_POLICY_FILE pointing
     at an invalid file fails the import (fail-closed at process startup,
     the actual behavior this whole feature exists to guarantee) rather
     than starting with a silently incomplete policy.

Each test restores anonymize's three module-level sets afterward
(load_policy_defaults fixture) since load_policy() mutates module globals
as a side effect -- without that, test order could leak state between
tests, same class of hazard test_service_auth.py's re-import trick already
guards against for service.py's own module-level state.
"""
import importlib
import json
import os
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC_PATH = os.path.join(REPO_ROOT, "src")

if SRC_PATH not in sys.path:
    sys.path.insert(0, SRC_PATH)

import anonymize  # noqa: E402

_BUILTIN_PSEUDONYMIZE = {"IP", "PERSON"}
_BUILTIN_TOKENIZE = {"EMAIL", "MRN", "CREDIT_CARD", "SSN"}
_BUILTIN_REDACT: set = set()


@pytest.fixture(autouse=True)
def restore_builtin_policy():
    """Every test in this file either calls load_policy() (which mutates
    anonymize.PSEUDONYMIZE_TYPES/TOKENIZE_TYPES/REDACT_TYPES directly) or
    imports service.py (which calls load_policy() itself at module level).
    Reset to the shipped built-in defaults before AND after each test so
    no test's policy file leaks into the next one via these module
    globals -- pytest does not otherwise isolate module-level mutable
    state between test functions in the same file."""
    def _reset():
        anonymize.PSEUDONYMIZE_TYPES = set(_BUILTIN_PSEUDONYMIZE)
        anonymize.TOKENIZE_TYPES = set(_BUILTIN_TOKENIZE)
        anonymize.REDACT_TYPES = set(_BUILTIN_REDACT)
    _reset()
    yield
    _reset()


def _write_policy(tmp_path, content: dict) -> str:
    path = os.path.join(str(tmp_path), "policy.json")
    with open(path, "w") as f:
        json.dump(content, f)
    return path


def test_missing_file_falls_back_to_builtin_defaults(tmp_path):
    nonexistent = os.path.join(str(tmp_path), "does_not_exist.json")
    pseudo, tok, red = anonymize.load_policy(path=nonexistent)
    assert pseudo == _BUILTIN_PSEUDONYMIZE
    assert tok == _BUILTIN_TOKENIZE
    assert red == _BUILTIN_REDACT
    # And the module globals reflect the same fallback, since callers
    # (service.py, pipeline.py) read anonymize.PSEUDONYMIZE_TYPES etc.
    # directly rather than using load_policy()'s return value.
    assert anonymize.PSEUDONYMIZE_TYPES == _BUILTIN_PSEUDONYMIZE
    assert anonymize.TOKENIZE_TYPES == _BUILTIN_TOKENIZE
    assert anonymize.REDACT_TYPES == _BUILTIN_REDACT


def test_valid_custom_file_overrides_defaults(tmp_path):
    path = _write_policy(tmp_path, {
        "pseudonymize_types": ["PERSON"],
        "tokenize_types": ["IP", "EMAIL", "MRN", "CREDIT_CARD", "SSN"],
        "redact_types": [],
    })
    pseudo, tok, red = anonymize.load_policy(path=path)
    assert pseudo == {"PERSON"}
    assert tok == {"IP", "EMAIL", "MRN", "CREDIT_CARD", "SSN"}
    assert red == set()
    # Confirm this is a real override, not coincidentally equal to the
    # built-in default -- IP moved buckets relative to _BUILTIN_PSEUDONYMIZE.
    assert anonymize.PSEUDONYMIZE_TYPES == {"PERSON"}
    assert "IP" in anonymize.TOKENIZE_TYPES


def test_shipped_default_config_file_is_itself_valid():
    """config/policy.json (the file the Dockerfile COPYs into the image and
    docker-compose.yml bind-mounts) must load cleanly with today's
    CANONICAL_TYPES -- if a future edit to that file breaks it, this is
    the test that should fail, not a live gunicorn worker failing to
    start in someone's deployment."""
    pseudo, tok, red = anonymize.load_policy(path=anonymize.DEFAULT_POLICY_PATH)
    assert pseudo == _BUILTIN_PSEUDONYMIZE
    assert tok == _BUILTIN_TOKENIZE
    assert red == _BUILTIN_REDACT


def test_missing_canonical_type_fails_closed(tmp_path):
    # SSN is left out of all three buckets entirely.
    path = _write_policy(tmp_path, {
        "pseudonymize_types": ["IP", "PERSON"],
        "tokenize_types": ["EMAIL", "MRN", "CREDIT_CARD"],
        "redact_types": [],
    })
    with pytest.raises(anonymize.PolicyConfigError, match="SSN"):
        anonymize.load_policy(path=path)
    # Failing must not have silently mutated the live policy -- a caller
    # that catches this exception and (wrongly) presses on should not find
    # a half-applied policy sitting in the module globals.
    assert anonymize.PSEUDONYMIZE_TYPES == _BUILTIN_PSEUDONYMIZE


def test_type_in_two_buckets_fails_closed(tmp_path):
    path = _write_policy(tmp_path, {
        "pseudonymize_types": ["IP", "PERSON", "EMAIL"],
        "tokenize_types": ["EMAIL", "MRN", "CREDIT_CARD", "SSN"],
        "redact_types": [],
    })
    with pytest.raises(anonymize.PolicyConfigError, match="EMAIL"):
        anonymize.load_policy(path=path)


def test_malformed_json_fails_closed_not_raw_jsondecodeerror(tmp_path):
    path = os.path.join(str(tmp_path), "policy.json")
    with open(path, "w") as f:
        f.write("{not valid json")
    with pytest.raises(anonymize.PolicyConfigError):
        anonymize.load_policy(path=path)


def test_non_list_value_fails_closed(tmp_path):
    path = _write_policy(tmp_path, {
        "pseudonymize_types": "IP,PERSON",  # string, not a list -- a
                                             # plausible operator typo
        "tokenize_types": ["EMAIL", "MRN", "CREDIT_CARD", "SSN"],
        "redact_types": [],
    })
    with pytest.raises(anonymize.PolicyConfigError):
        anonymize.load_policy(path=path)


def test_extra_top_level_keys_are_ignored(tmp_path):
    path = _write_policy(tmp_path, {
        "_comment": "this is documentation, not policy",
        "pseudonymize_types": ["IP", "PERSON"],
        "tokenize_types": ["EMAIL", "MRN", "CREDIT_CARD", "SSN"],
        "redact_types": [],
    })
    pseudo, tok, red = anonymize.load_policy(path=path)
    assert pseudo == _BUILTIN_PSEUDONYMIZE
    assert tok == _BUILTIN_TOKENIZE


def test_service_startup_fails_closed_on_invalid_policy_file(tmp_path, monkeypatch):
    """End-to-end check of the actual guarantee this feature exists for:
    src/service.py must fail to import (not start serving) when
    REDACT_POLICY_FILE points at an invalid policy file. Mirrors
    test_service_auth.py's mock-the-analyzer-then-reimport pattern, since
    service.py's module-level detect._get_analyzer() warmup call would
    otherwise try to load the real spaCy model, which this sandbox has no
    route to (see that test's own docstring)."""
    bad_path = _write_policy(tmp_path, {
        "pseudonymize_types": ["IP", "PERSON"],
        "tokenize_types": ["EMAIL", "MRN", "CREDIT_CARD"],  # SSN missing
        "redact_types": [],
    })
    monkeypatch.setenv("REDACT_POLICY_FILE", bad_path)
    monkeypatch.setenv("REDACT_SERVICE_API_KEY", "test-api-key-12345")
    monkeypatch.setenv(
        "REDACT_TOKEN_STORE_PATH",
        os.path.join(str(tmp_path), "token_store.json"),
    )

    for mod_name in ("service", "detect"):
        sys.modules.pop(mod_name, None)

    import detect  # noqa: E402
    monkeypatch.setattr(detect, "_get_analyzer", lambda: None)

    with pytest.raises(anonymize.PolicyConfigError, match="SSN"):
        importlib.import_module("service")

    # Clean up so this failed, half-imported module doesn't confuse a
    # later test in the same pytest session.
    sys.modules.pop("service", None)


def test_service_startup_succeeds_with_valid_custom_policy_file(tmp_path, monkeypatch):
    """The positive counterpart to the test above: a VALID custom policy
    file should let service.py import successfully and actually take
    effect (not just fail to error)."""
    good_path = _write_policy(tmp_path, {
        "pseudonymize_types": ["PERSON"],
        "tokenize_types": ["IP", "EMAIL", "MRN", "CREDIT_CARD", "SSN"],
        "redact_types": [],
    })
    monkeypatch.setenv("REDACT_POLICY_FILE", good_path)
    monkeypatch.setenv("REDACT_SERVICE_API_KEY", "test-api-key-12345")
    monkeypatch.setenv(
        "REDACT_TOKEN_STORE_PATH",
        os.path.join(str(tmp_path), "token_store.json"),
    )

    for mod_name in ("service", "detect"):
        sys.modules.pop(mod_name, None)

    import detect  # noqa: E402
    monkeypatch.setattr(detect, "_get_analyzer", lambda: None)

    service = importlib.import_module("service")
    try:
        assert anonymize.PSEUDONYMIZE_TYPES == {"PERSON"}
        assert "IP" in anonymize.TOKENIZE_TYPES
    finally:
        sys.modules.pop("service", None)
