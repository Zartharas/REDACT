"""
Tests src/queue_consumer.py's routing/ID-generation logic without needing
a live Redis, redact-service, or OpenSearch -- mocks call_redact_service
and index_document (both pure network I/O wrappers) and checks that
process_event() makes the same routing decisions
logstash/redact-pipeline.conf's output block does: success -> anonymized
index + one audit doc per audit_event, each with a fresh random UUID
never equal to the parent doc's ID or to each other; failure -> quarantine
index, original (un-anonymized) text preserved, never silently dropped.
"""
import os
import sys
import uuid
from unittest.mock import patch

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC_PATH = os.path.join(REPO_ROOT, "src")
if SRC_PATH not in sys.path:
    sys.path.insert(0, SRC_PATH)

import queue_consumer  # noqa: E402


def _is_valid_uuid4(s: str) -> bool:
    try:
        return str(uuid.UUID(s, version=4)) == s
    except (ValueError, AttributeError):
        return False


def test_success_routes_to_anonymized_index_and_fans_out_audit_events():
    fake_result = {
        "anonymized": "user [REDACTED] logged in",
        "span_count": 1,
        "audit_events": [
            {"field_type": "PERSON", "method": "redact", "policy_version": 1,
             "original_value_fingerprint": "abc123", "timestamp": 1234567890,
             "authentication_tag": "tag1"},
        ],
    }
    calls = []

    def fake_index_document(index, doc_id, body):
        calls.append((index, doc_id, body))

    with patch.object(queue_consumer, "call_redact_service", return_value=fake_result), \
         patch.object(queue_consumer, "index_document", side_effect=fake_index_document):
        queue_consumer.process_event({"message": "user jsmith logged in", "log_type": "syslog"})

    assert len(calls) == 2  # one anonymized doc, one audit doc
    anon_calls = [c for c in calls if c[0].startswith("security-logs-anonymized-")]
    audit_calls = [c for c in calls if c[0].startswith("redact-audit-trail-")]
    assert len(anon_calls) == 1
    assert len(audit_calls) == 1

    anon_index, anon_doc_id, anon_body = anon_calls[0]
    assert anon_body["message"] == "user [REDACTED] logged in"
    assert anon_body["log_type"] == "syslog"
    assert anon_body["pii_span_count"] == 1
    assert _is_valid_uuid4(anon_doc_id)

    audit_index, audit_doc_id, audit_body = audit_calls[0]
    assert audit_body["audit_event"] == fake_result["audit_events"][0]
    assert _is_valid_uuid4(audit_doc_id)
    # The specific bug this guards against (Bug 15, redact-pipeline.conf):
    # an audit doc's ID must NEVER be derived from the parent event's ID
    # or from the audit event's own content -- must be its own independent
    # random value, so distinct audit events never collide regardless of
    # shared content or timing.
    assert audit_doc_id != anon_doc_id


def test_multiple_audit_events_each_get_a_distinct_uuid():
    """Guards against Bug 15's exact failure mode: two audit events with
    IDENTICAL content (the realistic case -- a repeated field value like
    "TargetUserName=SYSTEM") landing in the same call must still get two
    independent document IDs, not overwrite each other."""
    identical_audit_event = {
        "field_type": "PERSON", "method": "redact", "policy_version": 1,
        "original_value_fingerprint": "same-fingerprint", "timestamp": 1234567890,
        "authentication_tag": "same-tag",
    }
    fake_result = {
        "anonymized": "a [REDACTED] and a [REDACTED]",
        "span_count": 2,
        "audit_events": [identical_audit_event, identical_audit_event],
    }
    calls = []
    with patch.object(queue_consumer, "call_redact_service", return_value=fake_result), \
         patch.object(queue_consumer, "index_document", side_effect=lambda i, d, b: calls.append((i, d, b))):
        queue_consumer.process_event({"message": "a jsmith and a jsmith", "log_type": "syslog"})

    audit_doc_ids = [c[1] for c in calls if c[0].startswith("redact-audit-trail-")]
    assert len(audit_doc_ids) == 2
    assert audit_doc_ids[0] != audit_doc_ids[1]  # the exact collision Bug 15 found


@pytest.mark.parametrize("exc_factory", [
    lambda: RuntimeError("service returned an error field"),
    lambda: ValueError("bad json from service"),
])
def test_service_failure_quarantines_original_text_not_silently_dropped(exc_factory):
    calls = []
    with patch.object(queue_consumer, "call_redact_service", side_effect=exc_factory()), \
         patch.object(queue_consumer, "index_document", side_effect=lambda i, d, b: calls.append((i, d, b))):
        queue_consumer.process_event({"message": "raw unprocessed line", "log_type": "syslog"})

    assert len(calls) == 1
    index, doc_id, body = calls[0]
    assert index.startswith("security-logs-quarantine-")
    assert body["message"] == "raw unprocessed line"  # original text preserved, not lost
    assert "quarantine_reason" in body
    assert _is_valid_uuid4(doc_id)


def test_event_with_no_message_field_is_a_silent_noop():
    """Matches Logstash's own behavior on an event with nothing to
    anonymize: no calls made at all, not an error."""
    with patch.object(queue_consumer, "call_redact_service") as mock_call, \
         patch.object(queue_consumer, "index_document") as mock_index:
        queue_consumer.process_event({"log_type": "syslog"})
    mock_call.assert_not_called()
    mock_index.assert_not_called()


def test_service_error_field_in_response_also_quarantines():
    """call_redact_service can succeed at the HTTP level but still return
    an {"error": ...} body (e.g. a 400 from a malformed request) --
    process_event must treat that the same as a connection failure, not
    as a successful zero-span response."""
    calls = []
    with patch.object(queue_consumer, "call_redact_service",
                       return_value={"error": "'log' must be a string"}), \
         patch.object(queue_consumer, "index_document", side_effect=lambda i, d, b: calls.append((i, d, b))):
        queue_consumer.process_event({"message": "some line", "log_type": "syslog"})

    assert len(calls) == 1
    assert calls[0][0].startswith("security-logs-quarantine-")
