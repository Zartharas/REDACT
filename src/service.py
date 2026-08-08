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
# Deliberately a separate key from AUDIT_KEY: the audit-signing key authenticates
# the event as a whole, while this key protects the low-entropy fingerprint inside
# it. Using one key for both would mean anyone who can verify an event's
# authenticity (which is meant to be a relatively wide audience, an auditor,
# a regulator) can also brute-force the fingerprint, which is meant to be
# usable by a much narrower audience.
FINGERPRINT_KEY = os.environ.get("REDACT_FINGERPRINT_KEY", "demo-fingerprint-key-do-not-use-in-prod")
TOKEN_KEY = os.environ.get("REDACT_TOKEN_KEY", "demo-token-key-do-not-use-in-prod")
TOKEN_STORE_PATH = os.environ.get("REDACT_TOKEN_STORE_PATH", "output/token_store.json")
# Bug 15 (BUGS_AND_FIXES.md), found via the 1,000,000-line load test: every
# call to TokenStore.save() -- one per request, see below -- used to
# rewrite the ENTIRE persisted store, so cost per request grew with total
# store size (measured: 2.927s for a single request once the store
# reached 93,279 entries). That's since been fixed at the root
# (StorageProvider.save_incremental() in anonymize.py: only newly-minted
# entries are persisted per call now, not the whole store -- RedisStorageProvider
# via a per-key HSET, FileStorageProvider via an append-only WAL with
# periodic compaction) -- confirmed in-sandbox to no longer show O(n)
# growth even with this debounce fully disabled (save_every_n_calls=1),
# see validation/tokenstore_save_scaling_test.py's rewritten result.
#
# This debounce parameter still exists and is still worth using in
# production: it further reduces how often TokenStore.save() takes its
# internal lock and touches the storage backend at all, on top of the
# incremental-write fix, at the cost of the same bounded crash-recovery
# tradeoff as before -- if this worker process crashes between real
# writes, up to REDACT_TOKEN_STORE_SAVE_EVERY - 1 requests' worth of
# reverse-map entries exist only in this worker's memory and are lost
# (the forward-map side is harmless to lose, since HMAC token generation
# is deterministic and any process recomputes the identical token for the
# same input -- see get_or_create_token's own comment in anonymize.py --
# but a lost reverse-map entry means detokenize() can no longer recover
# that one original value). Set to 1 to write on every single call
# (safest, and no longer meaningfully slower now that writes are
# incremental) if that tradeoff isn't acceptable for a given deployment;
# 25 is a starting point, not a value validated against any specific
# production SLA.
TOKEN_STORE_SAVE_EVERY = int(os.environ.get("REDACT_TOKEN_STORE_SAVE_EVERY", "25"))

os.makedirs(os.path.dirname(TOKEN_STORE_PATH) or ".", exist_ok=True)
_store = anonymize.TokenStore(TOKEN_STORE_PATH, token_key=TOKEN_KEY,
                               save_every_n_calls=TOKEN_STORE_SAVE_EVERY)


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
            fingerprint_key=FINGERPRINT_KEY,
        ))

    _store.save()

    return jsonify({
        "anonymized": anonymized_text,
        "span_count": len(typed_spans),
        "audit_events": audit_events,
    })


# Warm the NER model before accepting traffic. Root-caused 2026-08-07:
# detect._get_analyzer() is @lru_cache(maxsize=1)'d, so the expensive
# spaCy/Presidio model load only happens on the *first* real /anonymize
# call, not at process startup. Docker Compose's healthcheck only hits
# /health, which never touches the analyzer -- so redact-service reports
# "healthy" and Logstash starts sending its configured 8 concurrent
# requests (pipeline.workers => 8) before the model is loaded. During
# that multi-second, GIL-holding load, every request queues; enough of
# them exceed the http filter's timeout to get tagged
# _httprequestfailure and quarantined. Confirmed live: a fresh
# `docker compose up --build` produced a burst of "Read timed out"
# errors in Logstash's log in the first ~30-60s, then zero for the
# remainder of the run; final reconciliation still landed at exactly
# 9,968 anonymized + 32 quarantined = 10,000 (quarantine doing its job,
# nothing lost), but a production deployment shouldn't rely on
# Logstash's timeout/quarantine fallback to paper over a predictable
# cold-start window. This is the previously-undiagnosed "residual
# startup-only cluster of timeouts" flagged as unexplained in Bug 3
# (BUGS_AND_FIXES.md) -- now explained and fixed here, not there,
# since the actual fix belongs at the service-startup level.
#
# Deliberately at MODULE level, not inside `if __name__ == "__main__":`.
# gunicorn (see the production CMD in Dockerfile) imports this module
# directly -- it never executes the `__main__` block -- so if this call
# stayed inside that guard, gunicorn workers would silently regain the
# exact cold-start race this fix exists to close. Under gunicorn's default
# (non-preload) worker model, each worker process imports this module
# independently after forking, so each worker warms its own copy of the
# model in parallel with its siblings at startup; this is more memory
# (model size x worker count) than gunicorn's --preload flag would use
# (one shared copy-on-write load in the master before forking), but avoids
# the fork-after-model-load edge cases some native-extension-heavy
# libraries (spaCy included) can hit with --preload, and hasn't been
# tested against --preload specifically, so plain worker-level loading is
# the safer default until that's verified.
detect._get_analyzer()

if __name__ == "__main__":
    # threaded=True added after live testing showed Logstash's http filter
    # (pipeline.workers => 8, see logstash/redact-pipeline.conf) timing out
    # against this server ("Read timed out" in Logstash's log) under
    # concurrent load. Flask's dev server defaults to handling one request
    # at a time; with 8 concurrent POST /anonymize calls queued behind it,
    # some exceeded the http filter's request timeout and got tagged
    # _httprequestfailure -- which routes them to sensitive_quarantine
    # instead of being anonymized normally. Not a duplicate-write bug like
    # the OpenSearch document_id issue elsewhere in this project; this one
    # produces real documents in the wrong index.
    #
    # CAVEAT, stated plainly for the chapter's own sake: threaded=True only
    # buys overlapping I/O, not true parallelism -- the NER call inside
    # detect.detect_all() is CPU-bound and still serializes on the GIL. This
    # is sufficient to unblock this demo's throughput (10k lines, dev
    # laptop) but is NOT a production fix. The chapter's Performance
    # Optimization section should recommend a multi-process WSGI server
    # (e.g. gunicorn with worker count matched to CPU cores) instead, and
    # this comment should be treated as a known limitation, not a solved
    # problem, if cited as such.
    app.run(host="0.0.0.0", port=8080, threaded=True)
