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
import json
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


# --------------------------------------------------------------------------
# Kafka consumer path (KAFKA_BROKERS, added 2026-08-11, ROADMAP item 13).
# No live Kafka broker in this sandbox -- these mock the `kafka` library's
# KafkaConsumer class itself (patched at its source, `kafka.KafkaConsumer`,
# since queue_consumer.py imports it lazily inside _run_kafka_consumer)
# rather than needing a real broker. What's checked: the actual redelivery
# guarantee this path exists for -- commit() is called after a successful
# process_event() and NOT called after a failed one, which is the entire
# mechanism that makes "Kafka instead of Redis" a real improvement, not
# just a different transport with the same at-most-once failure mode.

class _FakeKafkaMessage:
    def __init__(self, value: str):
        self.value = value


class _StopTestLoop(Exception):
    """Raised by the fake consumer's __iter__ on its second call, so the
    test can end queue_consumer's deliberately-infinite `while True` loop
    without actually running forever -- distinct from StopIteration (which
    _run_kafka_consumer already handles as a legitimate idle-timeout
    signal) and from kafka.errors.KafkaError (handled as a retry signal),
    so this test-only exception propagates straight out uncaught, exactly
    where the test wants to stop and assert."""


class _FakeKafkaConsumer:
    """Yields `messages` on its first iteration, then raises _StopTestLoop
    on any subsequent iteration attempt -- see _StopTestLoop's own
    docstring for why."""

    def __init__(self, *args, **kwargs):
        self.messages = kwargs.pop("_test_messages", [])
        self.commit_call_count = 0
        self._iterations = 0

    def __iter__(self):
        self._iterations += 1
        if self._iterations > 1:
            raise _StopTestLoop()
        return iter(self.messages)

    def commit(self):
        self.commit_call_count += 1


def test_main_dispatches_to_kafka_when_brokers_configured():
    """KAFKA_BROKERS set -> main() must call the Kafka path, not touch the
    Redis/Sentinel path at all."""
    called = {"kafka": False}

    def fake_kafka_consumer():
        called["kafka"] = True

    with patch.object(queue_consumer, "KAFKA_BROKERS", ["broker1:9092"]), \
         patch.object(queue_consumer, "_run_kafka_consumer", side_effect=fake_kafka_consumer):
        queue_consumer.main()

    assert called["kafka"] is True


def test_kafka_consumer_commits_after_successful_processing():
    fake_consumer_instance = _FakeKafkaConsumer(
        _test_messages=[_FakeKafkaMessage(json.dumps(
            {"message": "user jsmith logged in", "log_type": "syslog"}
        ))]
    )

    def fake_kafka_consumer_factory(*args, **kwargs):
        return fake_consumer_instance

    with patch.object(queue_consumer, "KAFKA_BROKERS", ["broker1:9092"]), \
         patch("kafka.KafkaConsumer", side_effect=fake_kafka_consumer_factory), \
         patch.object(queue_consumer, "process_event", return_value=None) as mock_process:
        with pytest.raises(_StopTestLoop):
            queue_consumer._run_kafka_consumer()

    mock_process.assert_called_once()
    assert fake_consumer_instance.commit_call_count == 1


def test_kafka_consumer_does_not_commit_after_failed_processing():
    """The actual redelivery-guarantee mechanism, confirmed directly: a
    message whose process_event() call raises must NOT be committed, so
    Kafka redelivers it to the next consumer in the group rather than
    this path silently losing it the way the Redis BLPOP path's docstring
    discloses it can."""
    fake_consumer_instance = _FakeKafkaConsumer(
        _test_messages=[_FakeKafkaMessage(json.dumps(
            {"message": "user jsmith logged in", "log_type": "syslog"}
        ))]
    )

    def fake_kafka_consumer_factory(*args, **kwargs):
        return fake_consumer_instance

    with patch.object(queue_consumer, "KAFKA_BROKERS", ["broker1:9092"]), \
         patch("kafka.KafkaConsumer", side_effect=fake_kafka_consumer_factory), \
         patch.object(queue_consumer, "process_event",
                       side_effect=ConnectionError("OpenSearch unreachable")):
        with pytest.raises(_StopTestLoop):
            queue_consumer._run_kafka_consumer()

    assert fake_consumer_instance.commit_call_count == 0


def test_kafka_consumer_commits_unparseable_messages_to_avoid_permanent_block():
    """A malformed message that will never successfully parse must still
    be committed -- otherwise it would permanently block that partition's
    redelivery on every restart, a worse outcome than dropping the one
    unparseable message (matches the Redis path's own
    dropped-unparseable-item behavior in the main while-loop above)."""
    fake_consumer_instance = _FakeKafkaConsumer(
        _test_messages=[_FakeKafkaMessage("not valid json{{{")]
    )

    def fake_kafka_consumer_factory(*args, **kwargs):
        return fake_consumer_instance

    with patch.object(queue_consumer, "KAFKA_BROKERS", ["broker1:9092"]), \
         patch("kafka.KafkaConsumer", side_effect=fake_kafka_consumer_factory), \
         patch.object(queue_consumer, "process_event") as mock_process:
        with pytest.raises(_StopTestLoop):
            queue_consumer._run_kafka_consumer()

    mock_process.assert_not_called()
    assert fake_consumer_instance.commit_call_count == 1
