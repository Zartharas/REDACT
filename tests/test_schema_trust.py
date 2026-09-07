"""
Tests Phase 3 of DETECTION_POLICY_DECOUPLING_SCOPING.md ("Engineering
upgrade 20", BUGS_AND_FIXES.md): schema_trust.py (config load/validate,
windows_event/syslog only), schema_trust_audit.py (signed sample-outcome
audit trail), detect.detect_all_schema_aware() (the actual skip-detection
mechanism, plus the mandatory byte-identical-with-empty-config regression
test), schema_trust_sampler.SchemaTrustSampler (the async drift-check
safety net), and service.py's GET /admin/schema_trust route.

Deliberately built around examples IP/EMAIL/PERSON-typed content that's
regex- or entropy-detectable where possible, so most tests here don't need
the analyzer-mocking workaround test_service_auth.py/test_policy_config.py/
test_policy_hot_reload.py all require for anything that touches
detect._get_analyzer(). Where a PERSON-typed example needs the real NER
model to independently re-detect it (the sampler's "consistent" case),
that's flagged explicitly in the test's own comment.
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

import anonymize             # noqa: E402
import detect                 # noqa: E402
import schema_trust           # noqa: E402
import schema_trust_audit     # noqa: E402
import schema_trust_sampler   # noqa: E402


@pytest.fixture(autouse=True)
def restore_schema_trust():
    """schema_trust.load_schema_trust() mutates schema_trust.SCHEMA_TRUST
    as a module-level side effect, same hazard test_policy_config.py's
    identical fixture guards against for anonymize.py's policy sets."""
    def _reset():
        schema_trust.SCHEMA_TRUST = {}
    _reset()
    yield
    _reset()


def _write_schema_trust(path: str, content: dict) -> None:
    with open(path, "w") as f:
        json.dump(content, f)


# --- Tier 1: schema_trust.py ---------------------------------------------

def test_missing_file_falls_back_to_empty_inert_config(tmp_path):
    nonexistent = os.path.join(str(tmp_path), "does_not_exist.json")
    result = schema_trust.load_schema_trust(path=nonexistent)
    assert result == {}
    assert schema_trust.SCHEMA_TRUST == {}


def test_valid_config_loads_windows_event_and_syslog(tmp_path):
    path = os.path.join(str(tmp_path), "schema_trust.json")
    _write_schema_trust(path, {
        "windows_event": {"TargetUserName": "person"},  # lowercase input
        "syslog": {"rhost": "IP"},
    })
    result = schema_trust.load_schema_trust(path=path)
    assert result == {
        "windows_event": {"TargetUserName": "PERSON"},  # normalized upper
        "syslog": {"rhost": "IP"},
    }


def test_cloudtrail_key_is_rejected(tmp_path):
    path = os.path.join(str(tmp_path), "schema_trust.json")
    _write_schema_trust(path, {"cloudtrail": {"sourceIPAddress": "IP"}})
    with pytest.raises(schema_trust.SchemaTrustConfigError, match="cloudtrail"):
        schema_trust.load_schema_trust(path=path)
    # Must not have partially applied.
    assert schema_trust.SCHEMA_TRUST == {}


def test_unknown_canonical_type_is_rejected(tmp_path):
    path = os.path.join(str(tmp_path), "schema_trust.json")
    _write_schema_trust(path, {"windows_event": {"TargetUserName": "NOT_A_REAL_TYPE"}})
    with pytest.raises(schema_trust.SchemaTrustConfigError, match="NOT_A_REAL_TYPE"):
        schema_trust.load_schema_trust(path=path)


def test_malformed_json_is_rejected(tmp_path):
    path = os.path.join(str(tmp_path), "schema_trust.json")
    with open(path, "w") as f:
        f.write("{not valid json")
    with pytest.raises(schema_trust.SchemaTrustConfigError):
        schema_trust.load_schema_trust(path=path)


def test_documentation_keys_are_ignored(tmp_path):
    path = os.path.join(str(tmp_path), "schema_trust.json")
    _write_schema_trust(path, {
        "_comment": "not a log_type",
        "windows_event": {"TargetUserName": "PERSON"},
    })
    result = schema_trust.load_schema_trust(path=path)
    assert result == {"windows_event": {"TargetUserName": "PERSON"}}


def test_shipped_default_config_loads_cleanly_and_is_empty():
    result = schema_trust.load_schema_trust(path=schema_trust.DEFAULT_SCHEMA_TRUST_PATH)
    assert result.get("windows_event", {}) == {}
    assert result.get("syslog", {}) == {}
    assert "cloudtrail" not in result


# --- Tier 2: schema_trust_audit.py ----------------------------------------

def test_schema_trust_audit_event_round_trips_and_verifies():
    event = schema_trust_audit.build_schema_trust_audit_event(
        outcome="drift_detected", log_type="windows_event", field_name="TargetUserName",
        declared_type="PERSON", actual_types_found=["EMAIL"], audit_key="test-key",
    )
    assert event["event_type"] == "schema_trust_sample"
    assert schema_trust_audit.verify_schema_trust_audit_event(event, "test-key") is True
    assert schema_trust_audit.verify_schema_trust_audit_event(event, "wrong-key") is False


def test_schema_trust_audit_event_tampering_fails_verification():
    event = schema_trust_audit.build_schema_trust_audit_event(
        outcome="consistent", log_type="syslog", field_name="rhost",
        declared_type="IP", actual_types_found=["IP"], audit_key="test-key",
    )
    event["outcome"] = "drift_detected"
    assert schema_trust_audit.verify_schema_trust_audit_event(event, "test-key") is False


def test_append_schema_trust_audit_event_produces_valid_jsonl(tmp_path):
    log_path = os.path.join(str(tmp_path), "schema_trust_audit_log.jsonl")
    for outcome in ("consistent", "no_signal", "drift_detected"):
        event = schema_trust_audit.build_schema_trust_audit_event(
            outcome=outcome, log_type="syslog", field_name="rhost",
            declared_type="IP", actual_types_found=[], audit_key="test-key",
        )
        schema_trust_audit.append_schema_trust_audit_event(event, log_path)
    with open(log_path) as f:
        lines = [json.loads(line) for line in f if line.strip()]
    assert [line["outcome"] for line in lines] == ["consistent", "no_signal", "drift_detected"]


# --- Tier 3: detect.detect_all_schema_aware() -----------------------------

_WINDOWS_EVENT_LINE = (
    "TargetUserName=Timothy Wong SourceIP=203.0.113.42 "
    "FailureReason=Unknown user name or bad password"
)
_SYSLOG_LINE = "sshd[1234]: Failed password for invalid user root from 198.51.100.7 port 4242 ssh2"


def test_detect_all_schema_aware_matches_field_gated_when_config_empty():
    """THE regression test the docstrings promise: with schema_trust.SCHEMA_TRUST
    empty (the default), detect_all_schema_aware() must be byte-identical to
    detect_all_field_gated() -- the actual proof Phase 3 changes nothing
    until an operator opts in, not just an assertion in a comment."""
    for text, log_type in [(_WINDOWS_EVENT_LINE, "windows_event"), (_SYSLOG_LINE, "syslog")]:
        expected = detect.detect_all_field_gated(text, log_type=log_type)
        actual = detect.detect_all_schema_aware(text, log_type=log_type)
        assert actual == expected


def test_detect_all_schema_aware_emits_declared_span_and_skips_detection_there():
    schema_trust.SCHEMA_TRUST = {"windows_event": {"SourceIP": "IP"}}
    calls = []
    hits = detect.detect_all_schema_aware(
        _WINDOWS_EVENT_LINE, log_type="windows_event",
        on_schema_trust_span=lambda *args: calls.append(args),
    )

    schema_hits = [h for h in hits if h.get("source") == "schema_trust"]
    assert len(schema_hits) == 1
    span = schema_hits[0]
    assert span["type"] == "IP"
    assert span["field_name"] == "SourceIP"
    assert _WINDOWS_EVENT_LINE[span["start"]:span["end"]] == "203.0.113.42"

    # The callback must have fired with the located value.
    assert calls == [("windows_event", "SourceIP", "IP", "203.0.113.42")]

    # No OTHER hit should overlap the same span -- schema-trust must
    # replace, not duplicate, whatever regex would have found there too
    # (203.0.113.42 is itself IP-shaped and would normally be caught by
    # scan_regex on the whole line).
    for h in hits:
        if h is span:
            continue
        overlaps = h["start"] < span["end"] and span["start"] < h["end"]
        assert not overlaps, f"unexpected overlapping hit: {h}"

    # The rest of the line must still be detected normally -- a real name
    # (Timothy Wong) elsewhere on the line is untouched by the SourceIP
    # declaration and should still show up via the normal ensemble
    # (NER-dependent; only meaningful if a real analyzer is available, so
    # this assertion is intentionally loose -- just confirm nothing about
    # schema-trust suppressed detection outside the declared field).
    non_schema_hits = [h for h in hits if h.get("source") != "schema_trust"]
    assert all(h["start"] >= 0 for h in non_schema_hits)  # sanity: no negative/garbage offsets


def test_detect_all_schema_aware_syslog_field():
    # fields.py's extract_fields_syslog() prefixes every extracted field
    # with the syslog tag ("sshd.src_ip", not bare "src_ip" or "rhost") --
    # `return {f"{tag}.{k}": v for k, v in sm.groupdict().items() ...}` in
    # that function -- confirmed by reading it rather than assumed twice.
    schema_trust.SCHEMA_TRUST = {"syslog": {"sshd.src_ip": "IP"}}
    hits = detect.detect_all_schema_aware(_SYSLOG_LINE, log_type="syslog")
    schema_hits = [h for h in hits if h.get("source") == "schema_trust"]
    assert len(schema_hits) == 1
    assert schema_hits[0]["type"] == "IP"
    assert _SYSLOG_LINE[schema_hits[0]["start"]:schema_hits[0]["end"]] == "198.51.100.7"


def test_detect_all_schema_aware_falls_through_when_value_not_locatable(monkeypatch):
    """If fields.extract_fields() ever returned a value that isn't
    actually a verbatim substring of the text (shouldn't happen with
    today's extractors, but not assumed impossible), detect_all_schema_aware
    must refuse to guess a span rather than crash or emit a wrong one."""
    schema_trust.SCHEMA_TRUST = {"windows_event": {"TargetUserName": "PERSON"}}
    import fields
    monkeypatch.setattr(fields, "extract_fields",
                         lambda log_type, text: {"TargetUserName": "this string is not in the line"})
    hits = detect.detect_all_schema_aware(_WINDOWS_EVENT_LINE, log_type="windows_event")
    assert not any(h.get("source") == "schema_trust" for h in hits)


def test_detect_all_schema_aware_cloudtrail_is_always_a_no_op():
    # schema_trust.load_schema_trust() would already refuse a "cloudtrail"
    # key at load time (Tier 1 above); confirm the detection side is ALSO
    # a no-op for that log_type regardless (defense in depth, not relying
    # solely on the loader having been used correctly).
    schema_trust.SCHEMA_TRUST = {"cloudtrail": {"sourceIPAddress": "IP"}}
    text = '{"sourceIPAddress": "203.0.113.42", "eventName": "ConsoleLogin"}'
    expected = detect.detect_all_field_gated(text, log_type="cloudtrail")
    actual = detect.detect_all_schema_aware(text, log_type="cloudtrail")
    assert actual == expected


# --- Tier 4: schema_trust_sampler.SchemaTrustSampler ----------------------

def test_maybe_sample_respects_sample_rate(tmp_path):
    audit_log = os.path.join(str(tmp_path), "audit.jsonl")
    sampler_never = schema_trust_sampler.SchemaTrustSampler(
        audit_key="k", audit_log_path=audit_log, sample_rate=0.0,
    )
    assert sampler_never.maybe_sample("syslog", "rhost", "IP", "198.51.100.7") is False

    sampler_always = schema_trust_sampler.SchemaTrustSampler(
        audit_key="k", audit_log_path=audit_log, sample_rate=1.0,
    )
    assert sampler_always.maybe_sample("syslog", "rhost", "IP", "198.51.100.7") is True
    assert sampler_always.get_status()["samples_enqueued"] == 1


def test_maybe_sample_drops_when_queue_full(tmp_path):
    audit_log = os.path.join(str(tmp_path), "audit.jsonl")
    sampler = schema_trust_sampler.SchemaTrustSampler(
        audit_key="k", audit_log_path=audit_log, sample_rate=1.0, maxsize=1,
    )
    assert sampler.maybe_sample("syslog", "rhost", "IP", "1.2.3.4") is True
    assert sampler.maybe_sample("syslog", "rhost", "IP", "5.6.7.8") is False  # queue full
    status = sampler.get_status()
    assert status["samples_enqueued"] == 1
    assert status["samples_dropped"] == 1


def test_sampler_thread_processes_consistent_case(tmp_path):
    """IP is regex-detected -- no spaCy/Presidio model needed, so a
    declared IP field whose value really is IP-shaped should come back
    'consistent' from independent re-detection."""
    audit_log = os.path.join(str(tmp_path), "audit.jsonl")
    seen = []
    sampler = schema_trust_sampler.SchemaTrustSampler(
        audit_key="test-key", audit_log_path=audit_log, sample_rate=1.0,
        on_result=seen.append,
    )
    try:
        sampler.start()
        sampler.maybe_sample("syslog", "rhost", "IP", "198.51.100.7")
        deadline = time.time() + 5
        while sampler.get_status()["samples_processed"] < 1 and time.time() < deadline:
            time.sleep(0.02)
        status = sampler.get_status()
        assert status["samples_processed"] == 1
        assert status["outcome_counts"]["consistent"] == 1
        assert seen == ["consistent"]
        with open(audit_log) as f:
            lines = [json.loads(line) for line in f if line.strip()]
        assert lines[0]["outcome"] == "consistent"
        assert "IP" in lines[0]["actual_types_found"]
    finally:
        sampler.stop()


def test_sampler_thread_processes_no_signal_case(tmp_path):
    """A declared type that plain regex/entropy detection has no way to
    confirm on an isolated, context-free value -- should come back
    'no_signal', not 'drift_detected' (see schema_trust_audit.py's own
    docstring for why these are kept distinct)."""
    audit_log = os.path.join(str(tmp_path), "audit.jsonl")
    sampler = schema_trust_sampler.SchemaTrustSampler(
        audit_key="test-key", audit_log_path=audit_log, sample_rate=1.0,
    )
    try:
        sampler.start()
        # A bare short lowercase word -- not IP/EMAIL/SSN/CREDIT_CARD-shaped,
        # not high-entropy, and PERSON NER on a zero-context single common
        # word essentially never fires. If this ever proves flaky, it's a
        # sign the underlying model's behavior changed, worth knowing
        # either way rather than silently retrying past it.
        sampler.maybe_sample("syslog", "service", "PERSON", "cron")
        deadline = time.time() + 5
        while sampler.get_status()["samples_processed"] < 1 and time.time() < deadline:
            time.sleep(0.02)
        status = sampler.get_status()
        assert status["samples_processed"] == 1
        assert status["outcome_counts"]["no_signal"] == 1
    finally:
        sampler.stop()


def test_sampler_thread_processes_drift_detected_case(tmp_path):
    """Declared PERSON, but the actual value is EMAIL-shaped -- independent
    regex detection finds a DIFFERENT specific type, the strong signal
    that should surface as drift_detected."""
    audit_log = os.path.join(str(tmp_path), "audit.jsonl")
    sampler = schema_trust_sampler.SchemaTrustSampler(
        audit_key="test-key", audit_log_path=audit_log, sample_rate=1.0,
    )
    try:
        sampler.start()
        sampler.maybe_sample("windows_event", "TargetUserName", "PERSON", "not-a-name@example.com")
        deadline = time.time() + 5
        while sampler.get_status()["samples_processed"] < 1 and time.time() < deadline:
            time.sleep(0.02)
        status = sampler.get_status()
        assert status["samples_processed"] == 1
        assert status["outcome_counts"]["drift_detected"] == 1
        with open(audit_log) as f:
            lines = [json.loads(line) for line in f if line.strip()]
        assert lines[0]["outcome"] == "drift_detected"
        assert "EMAIL" in lines[0]["actual_types_found"]
    finally:
        sampler.stop()


def test_sampler_thread_survives_a_processing_exception(tmp_path, monkeypatch):
    audit_log = os.path.join(str(tmp_path), "audit.jsonl")
    sampler = schema_trust_sampler.SchemaTrustSampler(
        audit_key="test-key", audit_log_path=audit_log, sample_rate=1.0,
    )
    real_detect_all_field_gated = detect.detect_all_field_gated
    call_count = {"n": 0}

    def _flaky(text, log_type=None, use_flattened=True):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("simulated failure")
        return real_detect_all_field_gated(text, log_type=log_type, use_flattened=use_flattened)

    monkeypatch.setattr(detect, "detect_all_field_gated", _flaky)
    try:
        sampler.start()
        sampler.maybe_sample("syslog", "rhost", "IP", "1.2.3.4")  # triggers the simulated failure
        sampler.maybe_sample("syslog", "rhost", "IP", "5.6.7.8")  # should still be processed
        deadline = time.time() + 5
        while sampler.get_status()["samples_processed"] < 1 and time.time() < deadline:
            time.sleep(0.02)
        assert sampler.get_status()["samples_processed"] == 1  # the second one succeeded
    finally:
        sampler.stop()


# --- Tier 5: service.py's GET /admin/schema_trust -------------------------

def test_admin_schema_trust_status_requires_policy_admin_key(tmp_path, monkeypatch):
    policy_path = os.path.join(str(tmp_path), "policy.json")
    with open(policy_path, "w") as f:
        json.dump({
            "pseudonymize_types": ["IP", "PERSON"],
            "tokenize_types": ["EMAIL", "MRN", "CREDIT_CARD", "SSN"],
            "redact_types": [],
        }, f)
    schema_trust_path = os.path.join(str(tmp_path), "schema_trust.json")
    _write_schema_trust(schema_trust_path, {"syslog": {"rhost": "IP"}})

    monkeypatch.setenv("REDACT_POLICY_FILE", policy_path)
    monkeypatch.setenv("REDACT_SCHEMA_TRUST_FILE", schema_trust_path)
    monkeypatch.setenv("REDACT_POLICY_ADMIN_KEY", "test-admin-key")
    monkeypatch.setenv("REDACT_POLICY_AUDIT_LOG_PATH", os.path.join(str(tmp_path), "p_audit.jsonl"))
    monkeypatch.setenv("REDACT_SCHEMA_TRUST_AUDIT_LOG_PATH", os.path.join(str(tmp_path), "st_audit.jsonl"))
    monkeypatch.setenv("REDACT_POLICY_POLL_INTERVAL_S", "1000")
    monkeypatch.setenv("REDACT_SCHEMA_TRUST_SAMPLE_RATE", "0")  # no background sampling noise
    monkeypatch.setenv("REDACT_SERVICE_API_KEY", "test-api-key-12345")
    monkeypatch.setenv("REDACT_TOKEN_STORE_PATH", os.path.join(str(tmp_path), "token_store.json"))

    for mod_name in ("service", "detect", "policy_watcher", "policy_audit",
                      "schema_trust", "schema_trust_sampler", "schema_trust_audit"):
        sys.modules.pop(mod_name, None)

    import detect as detect_mod  # noqa: E402
    monkeypatch.setattr(detect_mod, "_get_analyzer", lambda: None)

    service = importlib.import_module("service")
    try:
        client = service.app.test_client()
        resp = client.get("/admin/schema_trust")
        assert resp.status_code == 401

        resp = client.get("/admin/schema_trust", headers={"X-Redact-Policy-Admin-Key": "test-admin-key"})
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["schema_trust"] == {"syslog": {"rhost": "IP"}}
        assert "sampler" in body
        assert body["sampler"]["sample_rate"] == 0.0
    finally:
        sys.modules.pop("service", None)
