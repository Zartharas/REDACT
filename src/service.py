"""
Thin HTTP wrapper around the tested detect.py / anonymize.py / audit.py
modules, so Logstash's `http` filter can call the real, tested Python logic
instead of a second, separately-maintained reimplementation in Ruby.

This is the deliberate alternative to writing HMAC and token-store logic
twice in two languages. Two implementations of the same crypto/storage logic
is exactly how the overlapping-span bug happened once already in this
project (see README) -- keeping one implementation and having the pipeline
call it, rather than mirroring it, removes that entire class of drift.

Run with:
    python src/service.py
Then POST to http://localhost:8080/anonymize with body:
    {"log": "<raw log line>"}
"""
import sys
import os
import json

sys.path.insert(0, os.path.dirname(__file__))
import detect      # noqa: E402
import anonymize   # noqa: E402
import audit        # noqa: E402

from flask import Flask, request, jsonify  # noqa: E402

app = Flask(__name__)

POLICY_VERSION = "redact-v0.1"
PSEUDO_KEY = os.environ.get("REDACT_PSEUDO_KEY", "demo-pseudonymization-key-do-not-use-in-prod")
AUDIT_KEY = os.environ.get("REDACT_AUDIT_KEY", "demo-audit-signing-key-do-not-use-in-prod")
TOKEN_STORE_PATH = os.environ.get("REDACT_TOKEN_STORE_PATH", "output/token_store.json")

os.makedirs(os.path.dirname(TOKEN_STORE_PATH) or ".", exist_ok=True)
_store = anonymize.TokenStore(TOKEN_STORE_PATH)


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


@app.route("/anonymize", methods=["POST"])
def anonymize_endpoint():
    body = request.get_json(force=True, silent=True) or {}
    if "log" not in body:
        return jsonify({"error": "request body must include a 'log' field"}), 400
    text = body["log"]
    if not isinstance(text, str):
        return jsonify({"error": "'log' must be a string"}), 400

    spans = detect.detect_all(text, use_ner=True)
    typed_spans = [s for s in spans if s["type"] != "HIGH_ENTROPY"]
    typed_spans = anonymize.dedup_spans(typed_spans)

    anonymized_text = anonymize.anonymize_by_policy(
        text, typed_spans, key=PSEUDO_KEY, store=_store
    )

    audit_events = []
    for span in typed_spans:
        original_value = text[span["start"]:span["end"]]
        if span["type"] in anonymize.PSEUDONYMIZE_TYPES:
            method = "pseudonymize"
        elif span["type"] in anonymize.TOKENIZE_TYPES:
            method = "tokenize"
        else:
            method = "redact"
        audit_events.append(audit.build_audit_event(
            field_type=span["type"], method=method,
            policy_version=POLICY_VERSION,
            original_value=original_value, audit_key=AUDIT_KEY,
        ))

    _store.save()

    return jsonify({
        "anonymized": anonymized_text,
        "span_count": len(typed_spans),
        "audit_events": audit_events,
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
