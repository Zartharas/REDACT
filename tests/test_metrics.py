"""
Tests the Prometheus metrics added to src/service.py (redact_detections_total,
redact_anonymize_request_seconds, redact_token_store_size,
redact_store_save_seconds/redact_store_save_total).

Same constraints and pattern as tests/test_service_auth.py: no live spaCy
model in this environment, so detect._get_analyzer()/detect.detect_all()
are monkeypatched before importing service, and service.py is force-
re-imported per test via sys.modules eviction. That re-import pattern is
exactly what motivated service.py's _metric() idempotent-registration
helper (see its own docstring) -- these tests are also the thing that
would fail loudly with prometheus_client.registry.DuplicateTimeseries if
that helper regressed.
"""
import os
import re
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC_PATH = os.path.join(REPO_ROOT, "src")


@pytest.fixture
def service_app(monkeypatch):
    monkeypatch.setenv("REDACT_SERVICE_API_KEY", "test-api-key-12345")
    import tempfile
    tmpdir = tempfile.mkdtemp(prefix="redact_metrics_test_")
    monkeypatch.setenv("REDACT_TOKEN_STORE_PATH", os.path.join(tmpdir, "token_store.json"))

    if SRC_PATH not in sys.path:
        sys.path.insert(0, SRC_PATH)

    for mod_name in ("service", "detect"):
        sys.modules.pop(mod_name, None)

    import detect  # noqa: E402
    monkeypatch.setattr(detect, "_get_analyzer", lambda: None)

    def fake_detect_all(text, use_ner=True):
        # One deterministic EMAIL hit whenever the text contains '@', so
        # tests can assert on a known, predictable detection count instead
        # of depending on the real NER model.
        if "@" in text:
            idx = text.index("@")
            return [{"type": "EMAIL", "start": max(0, idx - 4), "end": idx + 4,
                      "method": "regex"}]
        return []

    monkeypatch.setattr(detect, "detect_all", fake_detect_all)

    import service  # noqa: E402
    service.app.testing = True
    return service.app.test_client()


def _get_metric_value(metrics_text: str, metric_line_prefix: str) -> float | None:
    for line in metrics_text.splitlines():
        if line.startswith(metric_line_prefix):
            return float(line.rsplit(" ", 1)[-1])
    return None


def test_metrics_endpoint_requires_api_key(service_app):
    resp = service_app.get("/metrics")
    assert resp.status_code == 401


def test_metrics_endpoint_returns_prometheus_text(service_app):
    resp = service_app.get("/metrics", headers={"X-Redact-Api-Key": "test-api-key-12345"})
    assert resp.status_code == 200
    assert resp.mimetype == "text/plain"
    body = resp.get_data(as_text=True)
    # All four metric families should at least be present (registered),
    # even before any /anonymize traffic.
    for name in ("redact_anonymize_request_seconds", "redact_detections_total",
                 "redact_token_store_size", "redact_store_save_seconds",
                 "redact_store_save_total"):
        assert name in body, f"{name} missing from /metrics output"


def test_detection_counter_increments_per_span_type(service_app):
    headers = {"X-Redact-Api-Key": "test-api-key-12345"}
    resp = service_app.post("/anonymize", json={"log": "contact jane@example.com please"},
                             headers=headers)
    assert resp.status_code == 200
    assert resp.get_json()["span_count"] == 1

    metrics_resp = service_app.get("/metrics", headers=headers)
    body = metrics_resp.get_data(as_text=True)
    match = re.search(r'redact_detections_total\{type="EMAIL"\}\s+([\d.]+)', body)
    assert match is not None, "expected a redact_detections_total{type=\"EMAIL\"} sample"
    assert float(match.group(1)) == 1.0


def test_store_save_total_records_an_outcome(service_app):
    headers = {"X-Redact-Api-Key": "test-api-key-12345"}
    service_app.post("/anonymize", json={"log": "no pii here"}, headers=headers)

    metrics_resp = service_app.get("/metrics", headers=headers)
    body = metrics_resp.get_data(as_text=True)
    # TOKEN_STORE_SAVE_EVERY defaults to 25 in service.py, and this test
    # only sends one request, so the debounce should have skipped the
    # real write -- outcome="skipped" should have a sample >= 1.
    match = re.search(r'redact_store_save_total\{outcome="skipped"\}\s+([\d.]+)', body)
    assert match is not None
    assert float(match.group(1)) >= 1.0


def test_request_latency_histogram_records_a_sample(service_app):
    headers = {"X-Redact-Api-Key": "test-api-key-12345"}
    service_app.post("/anonymize", json={"log": "hello"}, headers=headers)

    metrics_resp = service_app.get("/metrics", headers=headers)
    body = metrics_resp.get_data(as_text=True)
    count = _get_metric_value(body, "redact_anonymize_request_seconds_count")
    assert count is not None and count >= 1.0
