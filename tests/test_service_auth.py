"""
Tests the X-Redact-Api-Key auth check added to src/service.py, without
needing a live Docker stack or the real spaCy/Presidio model.

src/service.py calls detect._get_analyzer() at MODULE level (deliberately
-- see that call's own comment on why it can't live inside `if __name__ ==
"__main__":`), which normally downloads/loads en_core_web_lg on first
import. This project's own dev sandbox has no route to that download (see
BUGS_AND_FIXES.md and README.md throughout for how NER-dependent numbers
in this project get verified: by a human, locally, not in CI). Rather
than skip testing the auth logic entirely for that reason, this test
monkeypatches detect._get_analyzer to a no-op BEFORE importing service,
so the module-level warmup call becomes a cheap no-op instead of a real
model load -- testing the auth check on its own merits, independent of
whether a spaCy model happens to be available in the environment running
this test.
"""
import importlib
import os
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC_PATH = os.path.join(REPO_ROOT, "src")


@pytest.fixture
def service_app(monkeypatch):
    """Imports (or re-imports) src/service.py with a mocked analyzer and a
    known test API key, returning a Flask test client."""
    monkeypatch.setenv("REDACT_SERVICE_API_KEY", "test-api-key-12345")
    # Also point the token store somewhere disposable so this test doesn't
    # touch a real output/token_store.json.
    import tempfile
    tmpdir = tempfile.mkdtemp(prefix="redact_service_auth_test_")
    monkeypatch.setenv("REDACT_TOKEN_STORE_PATH", os.path.join(tmpdir, "token_store.json"))

    if SRC_PATH not in sys.path:
        sys.path.insert(0, SRC_PATH)

    # Force a clean re-import of both modules so the env vars above are
    # actually re-read (service.py reads them at module level, once).
    for mod_name in ("service", "detect"):
        sys.modules.pop(mod_name, None)

    import detect  # noqa: E402
    monkeypatch.setattr(detect, "_get_analyzer", lambda: None)
    # detect_all_field_gated() itself would still try to use the (mocked-
    # away) analyzer if it decided to call scan_ner; the /anonymize
    # functional check below only needs the auth layer to work, not real
    # detection, so patch it to a harmless no-op returning no spans.
    # Patched here (not detect.detect_all, which service.py stopped
    # calling 2026-08-09 when it switched to the field-gated default --
    # see src/detect.py's detect_all_field_gated and src/service.py's
    # /anonymize endpoint) so this mock actually intercepts the call
    # service.py makes.
    monkeypatch.setattr(detect, "detect_all_field_gated",
                         lambda text, log_type=None, use_flattened=True: [])

    import service  # noqa: E402
    service.app.testing = True
    return service.app.test_client()


def test_health_requires_no_key(service_app):
    resp = service_app.get("/health")
    assert resp.status_code == 200
    assert resp.get_json() == {"status": "ok"}


def test_anonymize_rejects_missing_key(service_app):
    resp = service_app.post("/anonymize", json={"log": "hello"})
    assert resp.status_code == 401
    assert "X-Redact-Api-Key" in resp.get_json()["error"]


def test_anonymize_rejects_wrong_key(service_app):
    resp = service_app.post(
        "/anonymize", json={"log": "hello"},
        headers={"X-Redact-Api-Key": "definitely-not-the-right-key"},
    )
    assert resp.status_code == 401


def test_anonymize_accepts_correct_key(service_app):
    resp = service_app.post(
        "/anonymize", json={"log": "hello"},
        headers={"X-Redact-Api-Key": "test-api-key-12345"},
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["anonymized"] == "hello"  # detect_all_field_gated mocked to find nothing
    assert body["span_count"] == 0


def test_anonymize_accepts_optional_log_type(service_app):
    """Engineering upgrade, 2026-08-09: /anonymize now accepts an optional
    "log_type" field (windows_event/syslog/cloudtrail), forwarded into
    detect.detect_all_field_gated() so field-gating can use fields.py's
    structured extraction. Confirms a request that includes it still
    succeeds -- the mocked detect_all_field_gated above doesn't inspect
    log_type, so this is really testing that service.py accepts and
    forwards the field rather than rejecting or choking on it."""
    resp = service_app.post(
        "/anonymize", json={"log": "hello", "log_type": "windows_event"},
        headers={"X-Redact-Api-Key": "test-api-key-12345"},
    )
    assert resp.status_code == 200


def test_anonymize_rejects_non_string_log_type(service_app):
    resp = service_app.post(
        "/anonymize", json={"log": "hello", "log_type": 12345},
        headers={"X-Redact-Api-Key": "test-api-key-12345"},
    )
    assert resp.status_code == 400
    assert "log_type" in resp.get_json()["error"]


def test_anonymize_forwards_log_type_to_detection(service_app, monkeypatch):
    """Not just accepted -- actually forwarded. Replaces the fixture's
    detect_all_field_gated mock with a spy that records the log_type it
    was called with, confirming the value in the request body reaches
    detect.py rather than being silently dropped somewhere in between."""
    import service
    calls = []
    monkeypatch.setattr(
        service.detect, "detect_all_field_gated",
        lambda text, log_type=None, use_flattened=True: calls.append(log_type) or [],
    )

    resp = service_app.post(
        "/anonymize", json={"log": "hello", "log_type": "cloudtrail"},
        headers={"X-Redact-Api-Key": "test-api-key-12345"},
    )
    assert resp.status_code == 200
    assert calls == ["cloudtrail"]
