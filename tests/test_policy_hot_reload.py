"""
Tests Phase 2 of DETECTION_POLICY_DECOUPLING_SCOPING.md ("Engineering
upgrade 19", BUGS_AND_FIXES.md): policy_watcher.PolicyWatcher (background
poller + manual check_now()), policy_audit.py (the signed policy-change
audit trail), and service.py's two new /admin/policy* routes.

Structured in three tiers, cheapest/most-isolated first:
  1. policy_audit.py alone -- event building/verification, append safety.
  2. policy_watcher.PolicyWatcher alone -- no Flask, no service.py import,
     mirrors test_policy_config.py's approach of testing anonymize.py
     directly rather than only through the HTTP layer.
  3. service.py's /admin/policy and /admin/policy/reload routes end to
     end -- same mock-the-analyzer-then-reimport pattern
     test_service_auth.py and test_policy_config.py already use, since
     service.py's module-level detect._get_analyzer() warmup call would
     otherwise need a real spaCy model this sandbox has no route to.
"""
import importlib
import json
import os
import sys
import time

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC_PATH = os.path.join(REPO_ROOT, "src")

if SRC_PATH not in sys.path:
    sys.path.insert(0, SRC_PATH)

import anonymize        # noqa: E402
import policy_audit     # noqa: E402
import policy_watcher   # noqa: E402

_BUILTIN_PSEUDONYMIZE = {"IP", "PERSON"}
_BUILTIN_TOKENIZE = {"EMAIL", "MRN", "CREDIT_CARD", "SSN"}
_BUILTIN_REDACT: set = set()


@pytest.fixture(autouse=True)
def restore_builtin_policy():
    """Same rationale as test_policy_config.py's identical fixture:
    load_policy() (called indirectly by every PolicyWatcher.check_now()
    call in this file) mutates anonymize's module-level sets as a side
    effect. Reset before and after every test in this file too."""
    def _reset():
        anonymize.PSEUDONYMIZE_TYPES = set(_BUILTIN_PSEUDONYMIZE)
        anonymize.TOKENIZE_TYPES = set(_BUILTIN_TOKENIZE)
        anonymize.REDACT_TYPES = set(_BUILTIN_REDACT)
    _reset()
    yield
    _reset()


def _write_policy(path: str, content: dict) -> None:
    with open(path, "w") as f:
        json.dump(content, f)


_VALID_A = {
    "pseudonymize_types": ["IP", "PERSON"],
    "tokenize_types": ["EMAIL", "MRN", "CREDIT_CARD", "SSN"],
    "redact_types": [],
}
_VALID_B = {
    "pseudonymize_types": ["PERSON"],
    "tokenize_types": ["IP", "EMAIL", "MRN", "CREDIT_CARD", "SSN"],
    "redact_types": [],
}
_INVALID_MISSING_SSN = {
    "pseudonymize_types": ["IP", "PERSON"],
    "tokenize_types": ["EMAIL", "MRN", "CREDIT_CARD"],
    "redact_types": [],
}


# --- Tier 1: policy_audit.py -------------------------------------------

def test_policy_audit_event_round_trips_and_verifies():
    event = policy_audit.build_policy_audit_event(
        outcome="reloaded", resolved_path="/tmp/policy.json",
        old_policy_hash="aaa", new_policy_hash="bbb",
        triggered_by="poller", audit_key="test-key",
    )
    assert event["event_type"] == "policy_change"
    assert event["outcome"] == "reloaded"
    assert policy_audit.verify_policy_audit_event(event, "test-key") is True
    assert policy_audit.verify_policy_audit_event(event, "wrong-key") is False


def test_policy_audit_event_tampering_fails_verification():
    event = policy_audit.build_policy_audit_event(
        outcome="rejected", resolved_path="/tmp/policy.json",
        old_policy_hash="aaa", new_policy_hash="ccc",
        triggered_by="admin_endpoint", audit_key="test-key",
        error="canonical type(s) missing",
    )
    event["outcome"] = "reloaded"  # tamper after signing
    assert policy_audit.verify_policy_audit_event(event, "test-key") is False


def test_append_policy_audit_event_produces_valid_jsonl(tmp_path):
    log_path = os.path.join(str(tmp_path), "policy_audit_log.jsonl")
    for i in range(3):
        event = policy_audit.build_policy_audit_event(
            outcome="reloaded", resolved_path="/tmp/policy.json",
            old_policy_hash=f"hash{i}", new_policy_hash=f"hash{i + 1}",
            triggered_by="poller", audit_key="test-key",
        )
        policy_audit.append_policy_audit_event(event, log_path)
    with open(log_path) as f:
        lines = [json.loads(line) for line in f if line.strip()]
    assert len(lines) == 3
    assert [line["old_policy_hash"] for line in lines] == ["hash0", "hash1", "hash2"]
    assert all(policy_audit.verify_policy_audit_event(line, "test-key") for line in lines)


# --- Tier 2: policy_watcher.PolicyWatcher -------------------------------

def _make_watcher(policy_path, audit_log_path, **kwargs):
    return policy_watcher.PolicyWatcher(
        resolved_path=policy_path, audit_key="test-key",
        audit_log_path=audit_log_path, poll_interval_s=1000, **kwargs,
    )


def test_check_now_unchanged_on_first_call_no_edit(tmp_path):
    policy_path = os.path.join(str(tmp_path), "policy.json")
    _write_policy(policy_path, _VALID_A)
    audit_log = os.path.join(str(tmp_path), "audit.jsonl")
    watcher = _make_watcher(policy_path, audit_log)

    result = watcher.check_now()
    assert result["outcome"] == "unchanged"
    assert not os.path.exists(audit_log)  # no audit event for "unchanged"


def test_check_now_applies_valid_change(tmp_path):
    policy_path = os.path.join(str(tmp_path), "policy.json")
    _write_policy(policy_path, _VALID_A)
    audit_log = os.path.join(str(tmp_path), "audit.jsonl")
    watcher = _make_watcher(policy_path, audit_log)

    _write_policy(policy_path, _VALID_B)
    result = watcher.check_now()

    assert result["outcome"] == "reloaded"
    assert anonymize.PSEUDONYMIZE_TYPES == {"PERSON"}
    assert "IP" in anonymize.TOKENIZE_TYPES

    with open(audit_log) as f:
        lines = [json.loads(line) for line in f if line.strip()]
    assert len(lines) == 1
    assert lines[0]["outcome"] == "reloaded"
    assert policy_audit.verify_policy_audit_event(lines[0], "test-key")


def test_check_now_rejects_invalid_change_and_keeps_old_policy(tmp_path):
    policy_path = os.path.join(str(tmp_path), "policy.json")
    _write_policy(policy_path, _VALID_A)
    audit_log = os.path.join(str(tmp_path), "audit.jsonl")
    watcher = _make_watcher(policy_path, audit_log)

    _write_policy(policy_path, _INVALID_MISSING_SSN)
    result = watcher.check_now()

    assert result["outcome"] == "rejected"
    assert "SSN" in result["error"]
    # The live policy must be untouched by a rejected reload.
    assert anonymize.PSEUDONYMIZE_TYPES == _BUILTIN_PSEUDONYMIZE
    assert anonymize.TOKENIZE_TYPES == _BUILTIN_TOKENIZE

    with open(audit_log) as f:
        lines = [json.loads(line) for line in f if line.strip()]
    assert len(lines) == 1
    assert lines[0]["outcome"] == "rejected"


def test_check_now_retries_broken_file_until_fixed(tmp_path):
    """The specific behavior the comment in check_now() promises: a still-
    broken file keeps registering as 'changed' on every check (compared
    against the ORIGINAL last-good hash, not the broken one), so a
    subsequent fix is detected rather than the watcher having given up."""
    policy_path = os.path.join(str(tmp_path), "policy.json")
    _write_policy(policy_path, _VALID_A)
    audit_log = os.path.join(str(tmp_path), "audit.jsonl")
    watcher = _make_watcher(policy_path, audit_log)

    _write_policy(policy_path, _INVALID_MISSING_SSN)
    assert watcher.check_now()["outcome"] == "rejected"
    # Still broken -- checking again should reject again, not silently
    # stop noticing.
    assert watcher.check_now()["outcome"] == "rejected"

    _write_policy(policy_path, _VALID_B)
    result = watcher.check_now()
    assert result["outcome"] == "reloaded"
    assert anonymize.PSEUDONYMIZE_TYPES == {"PERSON"}


def test_on_result_callback_invoked_with_each_outcome(tmp_path):
    policy_path = os.path.join(str(tmp_path), "policy.json")
    _write_policy(policy_path, _VALID_A)
    audit_log = os.path.join(str(tmp_path), "audit.jsonl")
    seen = []
    watcher = _make_watcher(policy_path, audit_log, on_result=seen.append)

    watcher.check_now()  # unchanged
    _write_policy(policy_path, _VALID_B)
    watcher.check_now()  # reloaded

    assert seen == ["unchanged", "reloaded"]


def test_on_result_callback_exception_does_not_propagate(tmp_path):
    policy_path = os.path.join(str(tmp_path), "policy.json")
    _write_policy(policy_path, _VALID_A)
    audit_log = os.path.join(str(tmp_path), "audit.jsonl")

    def _broken_callback(outcome):
        raise RuntimeError("boom")

    watcher = _make_watcher(policy_path, audit_log, on_result=_broken_callback)
    result = watcher.check_now()  # must not raise despite the callback
    assert result["outcome"] == "unchanged"


def test_poller_thread_picks_up_edit_and_survives_a_bad_one(tmp_path):
    """End-to-end of the actual background thread (not just check_now()
    called directly), confirming both real guarantees policy_watcher.py's
    module docstring promises: (1) an edit is picked up within roughly one
    poll interval with no manual trigger, and (2) a bad edit in between
    does not kill the polling thread -- a subsequent good edit still gets
    picked up afterward."""
    policy_path = os.path.join(str(tmp_path), "policy.json")
    _write_policy(policy_path, _VALID_A)
    audit_log = os.path.join(str(tmp_path), "audit.jsonl")
    watcher = policy_watcher.PolicyWatcher(
        resolved_path=policy_path, audit_key="test-key",
        audit_log_path=audit_log, poll_interval_s=0.05,
    )
    try:
        watcher.start()
        time.sleep(0.15)
        assert watcher.get_status()["last_check_ts"] is not None

        _write_policy(policy_path, _INVALID_MISSING_SSN)
        time.sleep(0.2)
        status = watcher.get_status()
        assert status["last_outcome"] == "rejected"
        assert anonymize.PSEUDONYMIZE_TYPES == _BUILTIN_PSEUDONYMIZE  # untouched

        _write_policy(policy_path, _VALID_B)
        time.sleep(0.2)
        # NOT asserting status["last_outcome"] == "reloaded" here: at
        # poll_interval_s=0.05 with a 0.2s wait (~4 ticks), the reload can
        # genuinely happen on an early tick and then get immediately
        # followed by one or more "unchanged" ticks within the same
        # window (the file hasn't changed again, but last_outcome is a
        # single shared field the next tick legitimately overwrites) --
        # that's correct, expected behavior for a continuously-polling
        # thread, not a bug, and asserting a specific transient value
        # against it would be flaky by construction. What's actually
        # durable and worth asserting: the live policy converged (below)
        # AND the audit log recorded a "reloaded" event happened at some
        # point in this window (append-only -- an earlier tick's entry is
        # never overwritten the way last_outcome can be).
        assert anonymize.PSEUDONYMIZE_TYPES == {"PERSON"}
        with open(audit_log) as f:
            events = [json.loads(line) for line in f if line.strip()]
        assert any(e["outcome"] == "reloaded" for e in events)
    finally:
        watcher.stop()


# --- Tier 3: service.py's /admin/policy* routes -------------------------

def _import_service(tmp_path, monkeypatch, initial_policy: dict):
    policy_path = os.path.join(str(tmp_path), "policy.json")
    _write_policy(policy_path, initial_policy)
    monkeypatch.setenv("REDACT_POLICY_FILE", policy_path)
    monkeypatch.setenv("REDACT_POLICY_ADMIN_KEY", "test-admin-key")
    monkeypatch.setenv("REDACT_POLICY_AUDIT_LOG_PATH",
                        os.path.join(str(tmp_path), "policy_audit_log.jsonl"))
    monkeypatch.setenv("REDACT_POLICY_POLL_INTERVAL_S", "1000")  # no background interference
    monkeypatch.setenv("REDACT_SERVICE_API_KEY", "test-api-key-12345")
    monkeypatch.setenv("REDACT_TOKEN_STORE_PATH",
                        os.path.join(str(tmp_path), "token_store.json"))

    for mod_name in ("service", "detect", "policy_watcher", "policy_audit"):
        sys.modules.pop(mod_name, None)

    import detect  # noqa: E402
    monkeypatch.setattr(detect, "_get_analyzer", lambda: None)

    service = importlib.import_module("service")
    # _policy_watcher.start() (service.py, module level) launches a
    # background thread that performs ONE immediate check_now() call
    # before its first interval wait -- against the file as it exists
    # right now (initial_policy, unchanged), so that immediate check is
    # guaranteed to resolve to "unchanged" and settle before this function
    # returns. Without this pause, that immediate background check races
    # against whatever the test does next (e.g. writing a new policy and
    # hitting /admin/policy/reload): if the background thread's one-time
    # check happens to run AFTER the test's own edit but BEFORE the test's
    # own explicit reload call, the background check "wins" the reload
    # and the test's own call correctly (but confusingly) reports
    # "unchanged" instead of "reloaded" -- a real race this project hit
    # and diagnosed live (not assumed), not fixed by changing
    # policy_watcher.py's actual logic (which is correct -- the policy
    # DOES converge either way), only by giving this one-time startup
    # race a chance to finish before each test's own scenario begins.
    time.sleep(0.05)
    return service, policy_path


def test_admin_policy_status_requires_policy_admin_key(tmp_path, monkeypatch):
    service, _ = _import_service(tmp_path, monkeypatch, _VALID_A)
    try:
        client = service.app.test_client()
        resp = client.get("/admin/policy")
        assert resp.status_code == 401

        # The ordinary /anonymize key must NOT grant admin access either --
        # confirms these are genuinely separate authorization domains.
        resp = client.get("/admin/policy", headers={"X-Redact-Api-Key": "test-api-key-12345"})
        assert resp.status_code == 401

        resp = client.get("/admin/policy", headers={"X-Redact-Policy-Admin-Key": "test-admin-key"})
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["pseudonymize_types"] == sorted(_BUILTIN_PSEUDONYMIZE)
        assert body["resolved_path"].endswith("policy.json")
    finally:
        sys.modules.pop("service", None)


def test_admin_policy_key_does_not_grant_anonymize_access(tmp_path, monkeypatch):
    """The reverse direction of the domain-separation check above: holding
    only the policy admin key must not grant access to /anonymize."""
    service, _ = _import_service(tmp_path, monkeypatch, _VALID_A)
    try:
        client = service.app.test_client()
        resp = client.post(
            "/anonymize",
            json={"log": "hello"},
            headers={"X-Redact-Policy-Admin-Key": "test-admin-key"},
        )
        assert resp.status_code == 401
    finally:
        sys.modules.pop("service", None)


def test_admin_policy_reload_applies_valid_change(tmp_path, monkeypatch):
    service, policy_path = _import_service(tmp_path, monkeypatch, _VALID_A)
    try:
        client = service.app.test_client()
        _write_policy(policy_path, _VALID_B)
        resp = client.post("/admin/policy/reload",
                            headers={"X-Redact-Policy-Admin-Key": "test-admin-key"})
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["outcome"] == "reloaded"
        assert "this worker process only" in body["scope"]
        assert anonymize.PSEUDONYMIZE_TYPES == {"PERSON"}
    finally:
        sys.modules.pop("service", None)


def test_admin_policy_reload_rejects_invalid_change_with_422(tmp_path, monkeypatch):
    service, policy_path = _import_service(tmp_path, monkeypatch, _VALID_A)
    try:
        client = service.app.test_client()
        _write_policy(policy_path, _INVALID_MISSING_SSN)
        resp = client.post("/admin/policy/reload",
                            headers={"X-Redact-Policy-Admin-Key": "test-admin-key"})
        assert resp.status_code == 422
        body = resp.get_json()
        assert body["outcome"] == "rejected"
        assert "SSN" in body["error"]
        # Live policy must still be the original, unaffected by the
        # rejected reload attempt.
        assert anonymize.PSEUDONYMIZE_TYPES == _BUILTIN_PSEUDONYMIZE
    finally:
        sys.modules.pop("service", None)
