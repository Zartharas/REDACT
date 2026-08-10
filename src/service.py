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
import hmac
import time

sys.path.insert(0, os.path.dirname(__file__))
import detect      # noqa: E402
import anonymize   # noqa: E402
import audit        # noqa: E402

from flask import Flask, request, jsonify, Response  # noqa: E402
from prometheus_client import (  # noqa: E402
    Counter, Histogram, Gauge, generate_latest, CONTENT_TYPE_LATEST,
)

app = Flask(__name__)

# Engineering upgrade: this service had no metrics of any kind before this
# -- Bug 15 (the O(n)-per-request TokenStore.save() cost, BUGS_AND_FIXES.md)
# was only caught via manual `docker stats` and hand-timed `curl` calls
# during a scheduled load test, not by anything that would have surfaced
# it under normal, unmonitored operation. These four metrics target
# exactly that kind of gap: a way to see request latency, what's actually
# being detected, and TokenStore.save() behavior in production without
# needing to reproduce a load test to notice a regression.
#
# Deliberately module-level (not per-worker-namespaced): each gunicorn
# worker process gets its own independent copy of these metric objects
# (same reason each worker warms its own copy of the NER model -- see
# detect._get_analyzer() below), and a Prometheus scrape hitting one
# worker via a load balancer only sees that worker's own counters, not a
# cluster-wide total. This is the standard, disclosed limitation of the
# prometheus_client default registry under a forking multi-process
# server; prometheus_client's own multiprocess mode
# (PROMETHEUS_MULTIPROC_DIR) exists specifically to fix this by
# aggregating across workers on disk, and is a reasonable next step for
# a real production deployment, but is NOT wired up here -- adding it
# without a way to actually verify it in this environment (no live
# multi-worker gunicorn run possible here, see this project's standing
# disclosure pattern for anything Docker-dependent) would be asserting
# something unverified.
def _metric(cls, name, *args, **kwargs):
    """Idempotent metric registration.

    Returns the already-registered collector if `name` is already in
    prometheus_client's default registry, instead of letting a second
    registration under the same name raise
    prometheus_client.registry.DuplicateTimeseries. This module gets
    re-imported within the same process more than once in practice: this
    project's own test suite (tests/test_service_auth.py) deliberately
    forces a clean `sys.modules` re-import of service.py per test to pick
    up different env vars, and a real deployment's dev-mode Flask
    reloader or gunicorn's --reload flag would hit the exact same
    situation. Silently reusing the existing collector on a re-import is
    the correct behavior there -- the metric should keep accumulating
    across the "reload," not fail to come back up at all.
    """
    from prometheus_client import REGISTRY
    existing = REGISTRY._names_to_collectors.get(name)
    if existing is not None:
        return existing
    return cls(name, *args, **kwargs)


REQUEST_LATENCY = _metric(
    Histogram, "redact_anonymize_request_seconds",
    "Wall-clock time to handle one /anonymize request, from receiving the "
    "parsed JSON body to returning the response.",
)
DETECTIONS_TOTAL = _metric(
    Counter, "redact_detections_total",
    "PII/PHI spans detected and anonymized, labeled by canonical entity type "
    "(EMAIL, SSN, CREDIT_CARD, PERSON, IP, MRN). Counts spans after "
    "dedup_spans() and after HIGH_ENTROPY hits are filtered out, matching "
    "what actually gets anonymized and audited, not raw ensemble output.",
    ["type"],
)
TOKEN_STORE_SIZE = _metric(
    Gauge, "redact_token_store_size",
    "Number of forward-map entries in this worker's in-memory TokenStore "
    "view. Per-worker, not cluster-wide -- see the module comment above.",
)
STORE_SAVE_LATENCY = _metric(
    Histogram, "redact_store_save_seconds",
    "Wall-clock time spent inside TokenStore.save() per call, including "
    "calls the save-every-N debounce short-circuits (see "
    "REDACT_TOKEN_STORE_SAVE_EVERY below) -- expect a bimodal "
    "distribution: near-instant skipped calls, and real read/write calls.",
)
STORE_SAVE_TOTAL = _metric(
    Counter, "redact_store_save_total",
    "TokenStore.save() calls, labeled by whether the call actually "
    "persisted something ('persisted') or was skipped by the "
    "save-every-N debounce ('skipped').",
    ["outcome"],
)

POLICY_VERSION = "redact-v0.1"
# Engineering upgrade, added after the original build-and-verify pass:
# /anonymize had no authentication at all -- anything on the network that
# could reach this port could call it. Low real risk in the single-machine
# demo topology this project has verified so far (see BUGS_AND_FIXES.md),
# but exactly the kind of gap that's cheap to close now and expensive to
# retrofit once there's more than one caller. A shared-secret header,
# checked with hmac.compare_digest (constant-time, so response timing
# doesn't leak how many leading characters of the key were correct) --
# not a full auth system (OAuth, mTLS), since this service is meant to sit
# behind Logstash on a private network, not be internet-facing; a bearer
# token that Logstash's http filter sends on every request is proportional
# to that threat model. /health is deliberately exempt (see the check
# inside the route below) so Docker Compose's healthcheck, which only
# calls /health and doesn't know about this key, keeps working unchanged.
SERVICE_API_KEY = os.environ.get("REDACT_SERVICE_API_KEY", "demo-service-api-key-do-not-use-in-prod")
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


@app.before_request
def _require_api_key():
    # /health stays open (no key needed) so container healthchecks and
    # uptime probes don't need to know a secret just to ask "are you up."
    # Every other route requires a matching X-Redact-Api-Key header.
    if request.path == "/health":
        return None
    provided = request.headers.get("X-Redact-Api-Key", "")
    # compare_digest needs equal-length inputs to be meaningfully constant-
    # time; comparing against the real key (not a fixed-length dummy) is
    # still safer than == here since == short-circuits on the first
    # mismatched byte, which is the actual timing side-channel this guards
    # against.
    if not hmac.compare_digest(provided, SERVICE_API_KEY):
        return jsonify({"error": "missing or invalid X-Redact-Api-Key header"}), 401
    return None


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


@app.route("/metrics", methods=["GET"])
def metrics():
    # Kept BEHIND the X-Redact-Api-Key check above (unlike /health) --
    # request-count and detection-count metrics don't contain PII values
    # themselves, but they do reveal traffic volume and roughly what kind
    # of data this deployment is processing, which is worth the same
    # access control as /anonymize rather than left open by default. A
    # Prometheus scrape_config against this service needs to supply the
    # header (authorization: {type: Bearer, credentials_file: ...} or
    # bearer_token_file, per Prometheus's own scrape_config docs) --
    # untested against a live Prometheus instance in this environment,
    # same disclosure as everything else here that needs a real running
    # stack to fully verify.
    TOKEN_STORE_SIZE.set(len(_store._forward))
    return Response(generate_latest(), mimetype=CONTENT_TYPE_LATEST)


@app.route("/anonymize", methods=["POST"])
def anonymize_endpoint():
    request_start = time.time()

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
        DETECTIONS_TOTAL.labels(type=span["type"]).inc()

    save_start = time.time()
    persisted = _store.save()
    STORE_SAVE_LATENCY.observe(time.time() - save_start)
    STORE_SAVE_TOTAL.labels(outcome="persisted" if persisted else "skipped").inc()

    REQUEST_LATENCY.observe(time.time() - request_start)

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
    # Caveat for the chapter: threaded=True only buys overlapping I/O, not
    # true parallelism -- the NER call inside
    # detect.detect_all() is CPU-bound and still serializes on the GIL. This
    # is sufficient to unblock this demo's throughput (10k lines, dev
    # laptop) but is NOT a production fix. The chapter's Performance
    # Optimization section should recommend a multi-process WSGI server
    # (e.g. gunicorn with worker count matched to CPU cores) instead, and
    # this comment should be treated as a known limitation, not a solved
    # problem, if cited as such.
    app.run(host="0.0.0.0", port=8080, threaded=True)
