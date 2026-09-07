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
    {"log": "<raw log line>", "log_type": "windows_event"}
"log_type" is optional ("windows_event" / "syslog" / "cloudtrail", matching
fields.py's coverage) -- if present, detection uses the field-gated NER
strategy's structured-field information; if absent, detection still runs
correctly, just without that extra gating context for this request (see
detect.detect_all_field_gated's docstring).
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

# Phase 1 of DETECTION_POLICY_DECOUPLING_SCOPING.md (2026-09), "Engineering
# upgrade 18" in BUGS_AND_FIXES.md: load the pseudonymize/tokenize/redact
# policy from config/policy.json (overridable via REDACT_POLICY_FILE) here,
# at module import time -- same placement rule as detect._get_analyzer()'s
# warmup call below (must run unconditionally at import, NOT inside
# `if __name__ == "__main__":`, since gunicorn imports this module directly
# and never executes that block; see that call's own comment). A malformed
# or incomplete policy file raises anonymize.PolicyConfigError here, which
# propagates up and fails this worker's startup -- the deliberate fail-closed
# behavior the scoping doc's Phase 1 devil's-advocate section requires,
# rather than silently serving with an incomplete policy. A missing file
# (nothing configured yet) is not an error -- see load_policy()'s own
# docstring -- and falls back to anonymize.py's built-in defaults, so this
# call is a safe no-op for any deployment that hasn't opted into this yet.
anonymize.load_policy()

import policy_watcher  # noqa: E402
import policy_audit    # noqa: E402

# Phase 3 of DETECTION_POLICY_DECOUPLING_SCOPING.md (2026-09), "Engineering
# upgrade 20" in BUGS_AND_FIXES.md: load the schema-trust declarations
# (windows_event/syslog only -- see schema_trust.py's own module docstring
# for why CloudTrail/JSON can't safely use this) from config/schema_trust.json.
# Same fail-closed-on-invalid/fail-open-on-missing split as Phase 1's
# anonymize.load_policy() above, and the same "must run at unconditional
# module-import time" placement rule.
import schema_trust           # noqa: E402
import schema_trust_sampler   # noqa: E402
import schema_trust_audit     # noqa: E402

schema_trust.load_schema_trust()

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
# Phase 2, "Engineering upgrade 19": without these, a policy_watcher poller
# thread that silently stopped updating (see that module's own comment on
# why its poll loop can't be allowed to die silently either) would be
# invisible from the outside -- these are what let an operator's own
# alerting notice "this worker's last successful policy check was N minutes
# ago" rather than assuming the poller is fine because nothing crashed.
POLICY_LAST_RELOAD_TIMESTAMP = _metric(
    Gauge, "redact_policy_last_reload_timestamp_seconds",
    "Unix timestamp of this worker's last successful policy reload "
    "(outcome='reloaded'). Does NOT update on 'unchanged' checks -- a "
    "flat value here across a long window is expected and healthy if the "
    "policy file simply hasn't changed, not itself a sign of a stuck "
    "poller; cross-check against redact_policy_last_check_timestamp_seconds "
    "below to distinguish the two.",
)
POLICY_LAST_CHECK_TIMESTAMP = _metric(
    Gauge, "redact_policy_last_check_timestamp_seconds",
    "Unix timestamp of this worker's last policy check attempt, "
    "regardless of outcome. This SHOULD advance roughly every "
    "REDACT_POLICY_POLL_INTERVAL_S seconds continuously -- if it stops "
    "advancing, the poller thread has died (see policy_watcher.py's own "
    "comment on why its poll loop is guarded against that, and alert on "
    "this metric as the independent, outside confirmation that guard "
    "actually worked).",
)
POLICY_RELOAD_TOTAL = _metric(
    Counter, "redact_policy_reload_total",
    "Policy check outcomes on this worker, labeled 'reloaded' / "
    "'unchanged' / 'rejected'. A nonzero and growing 'rejected' count "
    "means someone keeps editing config/policy.json into an invalid "
    "state -- see policy_audit_log.jsonl for the specific validation "
    "error each time.",
    ["outcome"],
)
# Phase 3, "Engineering upgrade 20": the actual, monitored answer to "what
# if a schema-trust declaration turns out to be wrong" -- a nonzero and
# growing 'drift_detected' count means independent re-detection is finding
# a DIFFERENT type than what config/schema_trust.json declares for some
# field, the concrete signal DETECTION_POLICY_DECOUPLING_SCOPING.md's
# Phase 3 devil's-advocate section required before shipping any
# skip-detection mechanism.
SCHEMA_TRUST_SAMPLE_TOTAL = _metric(
    Counter, "redact_schema_trust_sample_total",
    "Async schema-trust re-check outcomes on this worker, labeled "
    "'consistent' / 'no_signal' / 'drift_detected'. See "
    "schema_trust_audit_log.jsonl for which specific field and what "
    "type was actually found on each 'drift_detected' event.",
    ["outcome"],
)
SCHEMA_TRUST_QUEUE_DEPTH = _metric(
    Gauge, "redact_schema_trust_sample_queue_depth",
    "Current depth of this worker's schema-trust sampling queue. A "
    "queue that's consistently near its cap (1000 by default) means "
    "samples are being dropped (see redact_schema_trust_samples_dropped_total) "
    "-- the background sampler can't keep up with the enqueue rate.",
)
SCHEMA_TRUST_SAMPLES_DROPPED_TOTAL = _metric(
    Gauge, "redact_schema_trust_samples_dropped_total",
    "Cumulative samples dropped by this worker because the sampling "
    "queue was full at enqueue time (see SchemaTrustSampler.maybe_sample()). "
    "A Gauge, not a Counter, because the underlying value "
    "(SchemaTrustSampler._samples_dropped) is read from a snapshot at "
    "scrape time rather than incremented at the exact moment each drop "
    "happens -- see the /metrics route for where this is set.",
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

# Phase 2 of DETECTION_POLICY_DECOUPLING_SCOPING.md (2026-09), "Engineering
# upgrade 19" in BUGS_AND_FIXES.md: a SEPARATE key from SERVICE_API_KEY, per
# that doc's own Phase 2 devil's-advocate requirement ("its own
# authentication, separate from anything else REDACT exposes today"). A
# caller with only the ordinary /anonymize key should not also be able to
# read or change this deployment's PII-handling policy -- the blast radius
# of these two keys leaking is not the same (one processes a log line, the
# other decides what protection every future log line gets), so they should
# not be the same secret.
POLICY_ADMIN_KEY = os.environ.get(
    "REDACT_POLICY_ADMIN_KEY", "demo-policy-admin-key-do-not-use-in-prod"
)
POLICY_AUDIT_LOG_PATH = os.environ.get(
    "REDACT_POLICY_AUDIT_LOG_PATH", "output/policy_audit_log.jsonl"
)
# Default 10s: fast enough that an operator watching /admin/policy after an
# edit sees convergence within a few polls, slow enough not to make
# _file_hash()'s per-poll file read (cheap, but not free at high frequency
# across many replicas x workers) a meaningful cost. Not validated against
# any specific production SLA -- a starting point, same disclosure this
# project already applies to TOKEN_STORE_SAVE_EVERY's default above.
POLICY_POLL_INTERVAL_S = float(os.environ.get("REDACT_POLICY_POLL_INTERVAL_S", "10"))

_policy_watcher = policy_watcher.PolicyWatcher(
    resolved_path=anonymize.resolve_policy_path(),
    audit_key=AUDIT_KEY,
    audit_log_path=POLICY_AUDIT_LOG_PATH,
    poll_interval_s=POLICY_POLL_INTERVAL_S,
    on_result=lambda outcome: POLICY_RELOAD_TOTAL.labels(outcome=outcome).inc(),
)
# Started unconditionally at module level, same placement rule as
# anonymize.load_policy() and detect._get_analyzer() below -- gunicorn
# imports this module directly and never runs `if __name__ ==
# "__main__":`, so anything that needs to be true for every real worker
# must be triggered here, not there.
_policy_watcher.start()

# Phase 3, "Engineering upgrade 20": the mandatory async drift-sampling
# safety net for schema-trust-skipped fields (see schema_trust_sampler.py's
# own module docstring for why this MUST be async/queued rather than a
# synchronous double-check on the request path -- doing it synchronously
# would defeat the entire point of skipping detection in the first place).
# Deliberately reuses AUDIT_KEY, same reasoning as policy_audit.py's own
# choice not to mint a new key for policy-change events.
SCHEMA_TRUST_AUDIT_LOG_PATH = os.environ.get(
    "REDACT_SCHEMA_TRUST_AUDIT_LOG_PATH", "output/schema_trust_audit_log.jsonl"
)
# 1% default: frequent enough to catch sustained drift within a reasonable
# number of requests for any field seeing real traffic, infrequent enough
# that the (off-hot-path, but still real) background re-detection cost
# stays a small fraction of total request volume. Not validated against
# any specific production SLA -- same disclosure this project already
# applies to every other interval/rate default (TOKEN_STORE_SAVE_EVERY,
# REDACT_POLICY_POLL_INTERVAL_S above).
SCHEMA_TRUST_SAMPLE_RATE = float(os.environ.get("REDACT_SCHEMA_TRUST_SAMPLE_RATE", "0.01"))

_schema_trust_sampler = schema_trust_sampler.SchemaTrustSampler(
    audit_key=AUDIT_KEY,
    audit_log_path=SCHEMA_TRUST_AUDIT_LOG_PATH,
    sample_rate=SCHEMA_TRUST_SAMPLE_RATE,
    on_result=lambda outcome: SCHEMA_TRUST_SAMPLE_TOTAL.labels(outcome=outcome).inc(),
)
_schema_trust_sampler.start()


@app.before_request
def _require_api_key():
    # /health stays open (no key needed) so container healthchecks and
    # uptime probes don't need to know a secret just to ask "are you up."
    # /admin/* routes are EXEMPT from this check, not because they're
    # unauthenticated -- see _require_policy_admin_key below, which gates
    # them on a completely separate key (REDACT_POLICY_ADMIN_KEY). This is
    # the actual "its own authentication, separate from anything else
    # REDACT exposes today" requirement from
    # DETECTION_POLICY_DECOUPLING_SCOPING.md's Phase 2 section: a caller
    # holding only SERVICE_API_KEY (meant for /anonymize traffic, e.g.
    # Logstash) gets no access to /admin/policy at all, and a caller
    # holding only POLICY_ADMIN_KEY gets no access to /anonymize -- two
    # non-overlapping authorization domains, not one check with two valid
    # keys.
    if request.path == "/health" or request.path.startswith("/admin/"):
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


def _require_policy_admin_key():
    """Called explicitly at the top of each /admin/policy* route (NOT a
    second @app.before_request hook) -- Flask runs all before_request
    hooks for every matching route regardless of path, so a second
    unconditional hook here would need its own path-based exemption logic
    duplicating _require_api_key's own, for no real benefit over just
    calling this directly from the two routes that need it."""
    provided = request.headers.get("X-Redact-Policy-Admin-Key", "")
    if not hmac.compare_digest(provided, POLICY_ADMIN_KEY):
        return jsonify({"error": "missing or invalid X-Redact-Policy-Admin-Key header"}), 401
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
    # Set lazily at scrape time from the watcher's own status, same pattern
    # as TOKEN_STORE_SIZE above -- these are Gauges (a snapshot of current
    # state), unlike POLICY_RELOAD_TOTAL (a Counter, incremented once per
    # real event via the on_result callback at construction time above),
    # so recomputing them here on every scrape is correct, not redundant.
    status = _policy_watcher.get_status()
    if status["last_reload_ts"] is not None:
        POLICY_LAST_RELOAD_TIMESTAMP.set(status["last_reload_ts"])
    if status["last_check_ts"] is not None:
        POLICY_LAST_CHECK_TIMESTAMP.set(status["last_check_ts"])
    sampler_status = _schema_trust_sampler.get_status()
    SCHEMA_TRUST_QUEUE_DEPTH.set(sampler_status["queue_depth"])
    SCHEMA_TRUST_SAMPLES_DROPPED_TOTAL.set(sampler_status["samples_dropped"])
    return Response(generate_latest(), mimetype=CONTENT_TYPE_LATEST)


@app.route("/admin/policy", methods=["GET"])
def admin_policy_status():
    """Read-only status for THIS worker process -- current policy sets,
    the resolved file path, and this worker's own last-check/last-reload
    timestamps. Behind a load balancer with multiple gunicorn workers
    and/or replicas, repeated calls can and will land on different
    workers and show slightly different last_check_ts values (each
    worker's poller runs on its own independent schedule, not
    synchronized to any other worker's) -- that's expected, not a bug;
    see policy_watcher.py's own module docstring for why per-worker
    polling is Phase 2's actual propagation mechanism."""
    auth_error = _require_policy_admin_key()
    if auth_error is not None:
        return auth_error
    return jsonify(_policy_watcher.get_status())


@app.route("/admin/policy/reload", methods=["POST"])
def admin_policy_reload():
    """Manually trigger an immediate policy check on THIS worker,
    short-circuiting the wait for its next scheduled poll tick. Explicitly
    scoped in the response body, not just this docstring, since this is
    the single easiest thing about Phase 2 to misunderstand operationally:
    calling this once does NOT guarantee every worker/replica in a
    deployment has picked up a change -- it only forces the ONE worker
    process that happens to handle this specific HTTP request (gunicorn
    routes each request to exactly one worker) to check right now. Every
    other worker still converges independently via its own background
    poller, within REDACT_POLICY_POLL_INTERVAL_S seconds. A caller wanting
    to confirm cluster-wide convergence should poll GET /admin/policy
    against each replica (or via a load balancer, repeatedly, to sample
    different workers) rather than trust a single 200 from this route."""
    auth_error = _require_policy_admin_key()
    if auth_error is not None:
        return auth_error
    result = _policy_watcher.check_now(triggered_by="admin_endpoint")
    result["scope"] = (
        "this worker process only (pid={}); other gunicorn workers and/or "
        "replicas converge independently via their own background poller "
        "within REDACT_POLICY_POLL_INTERVAL_S seconds -- see GET "
        "/admin/policy on each to confirm cluster-wide convergence"
    ).format(os.getpid())
    status_code = 200 if result["outcome"] != "rejected" else 422
    return jsonify(result), status_code


@app.route("/admin/schema_trust", methods=["GET"])
def admin_schema_trust_status():
    """Read-only status for THIS worker's schema-trust configuration and
    sampler -- reuses the same POLICY_ADMIN_KEY auth domain as
    /admin/policy* (both are "operator visibility into detection/policy
    configuration," not two things needing separate keys). Shows the
    currently loaded declarations (schema_trust.SCHEMA_TRUST, empty unless
    an operator has opted in) and the async sampler's own counters --
    samples_processed and the per-outcome breakdown are the actual,
    concrete answer to "is the mandatory drift-sampling safety net
    running," not just a claim that it exists."""
    auth_error = _require_policy_admin_key()
    if auth_error is not None:
        return auth_error
    return jsonify({
        "worker_pid": os.getpid(),
        "schema_trust": schema_trust.SCHEMA_TRUST,
        "sampler": _schema_trust_sampler.get_status(),
    })


@app.route("/anonymize", methods=["POST"])
def anonymize_endpoint():
    request_start = time.time()

    body = request.get_json(force=True, silent=True) or {}
    if "log" not in body:
        return jsonify({"error": "request body must include a 'log' field"}), 400
    text = body["log"]
    if not isinstance(text, str):
        return jsonify({"error": "'log' must be a string"}), 400
    # Optional, added 2026-08-09 alongside the switch to field-gated NER
    # below: "log_type" ("windows_event" / "syslog" / "cloudtrail") lets
    # detect_all_field_gated() use fields.py's structured extraction to
    # gate NER at the field level instead of the whole line. Deliberately
    # optional and permissive (falls back to None, not a 400) -- a caller
    # that doesn't send it (e.g. logstash/redact-pipeline.conf before its
    # own log_type-forwarding change ships) still gets correct detection,
    # just without the field-gating benefit for that request; see
    # detect.detect_all_field_gated's own docstring for why that fallback
    # is safe rather than a silent correctness regression.
    log_type = body.get("log_type")
    if log_type is not None and not isinstance(log_type, str):
        return jsonify({"error": "'log_type' must be a string if present"}), 400

    # Engineering upgrade, 2026-08-09: switched from detect.detect_all()
    # (naive -- NER on every line) to detect.detect_all_field_gated().
    # Three measured iterations against the real model (see
    # detect.build_ner_candidate's docstring and README.md's comparison
    # table) found field-gated matches or exceeds naive's recall and
    # precision, at throughput that is at worst statistically
    # indistinguishable from naive's given the measured noise floor --
    # never worse on any measured axis, and meaningfully better on PERSON
    # recall than the whole-line "tiered" strategy this project also
    # evaluated. See tests/README.md's field-gate section for the honest,
    # not-yet-fully-resolved throughput comparison this claim rests on.
    # Phase 3, "Engineering upgrade 20": schema-aware detection, not plain
    # field-gated detection -- with an empty (default) config/schema_trust.json
    # this is byte-identical to detect_all_field_gated()'s own output (see
    # detect_all_schema_aware's own docstring for the regression test this
    # rests on), so switching this call is a safe no-op until an operator
    # opts in. on_schema_trust_span feeds SchemaTrustSampler.maybe_sample()
    # without detect.py needing to import or know about the sampler at all.
    spans = detect.detect_all_schema_aware(
        text, log_type=log_type,
        on_schema_trust_span=_schema_trust_sampler.maybe_sample,
    )
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
