"""
Consumer for the queue-decoupled ingestion path (ROADMAP item 12,
2026-08-10). Pulls raw events off a Redis list (pushed by
logstash/redact-pipeline-queued.conf's `redis` output block), calls
redact-service exactly the way logstash/redact-pipeline.conf's `http`
filter does, and writes the result to OpenSearch -- replicating that
file's document-ID generation and index-routing logic by hand, in
Python, rather than reinventing it from scratch. That logic exists the
way it does because of real, hard-won bugs (see redact-pipeline.conf's
own comments and Bugs 4/12/15 in BUGS_AND_FIXES.md): content-derived
document IDs collide under real traffic (repeated field values, same-
second timestamps), a whole-line skip on service error would silently
index un-anonymized text, and audit events need their OWN random ID,
independent of the parent event's ID. This file is deliberately written
to match that logic field-for-field rather than treat it as "basically
the same idea" and improvise -- see each function's docstring for the
specific bug in redact-pipeline.conf's history that shaped it.

Run multiple copies concurrently (docker-compose.yml's queue-consumer
service, `docker compose --profile queued up --scale queue-consumer=N`)
to load-balance the queue -- BLPOP against the same Redis key is atomic
per Redis's own semantics (a given list element is delivered to exactly
one popping client), so this needs no additional coordination.

DISCLOSED, REAL LIMITATION (see docker-compose.yml's logstash-queued
comment for the full reasoning): this reads from a plain Redis LIST via
BLPOP, not a Redis Stream with consumer groups, because the OFFICIAL
logstash-output-redis plugin only supports RPUSH/PUBLISH, not XADD.
BLPOP removes an item the instant it's popped -- if THIS PROCESS crashes
after popping but before finishing (the redact-service call, or the
OpenSearch write), that one event is lost, not redelivered. There is no
XPENDING/XCLAIM recovery path here the way there would be with a real
consumer group. MIGRATION PATH, if this ever needs closing: swap
logstash-pipeline-queued.conf's `redis` output block for the
logstash-output-redis-streams plugin (redis-field-engineering, installable
via `bin/logstash-plugin install logstash-output-redis-streams`, uses
XADD -- found via live documentation research 2026-08-10, NOT verified
running in this project, low visible adoption, treat as unverified) and
rewrite this script's poll loop around Python's `redis` client's
`xreadgroup`/`xack`/`xpending`/`xclaim` methods instead of `blpop`. Not
done now because building the "closed for good" queue path on an
unverified third-party plugin would repeat the exact mistake this
project's own Bug 16 (a one-character Logstash config error that
silently crashed the whole pipeline for hours) exists to warn against.

UNVERIFIED against a live Redis/OpenSearch/redact-service stack in this
sandbox (no Docker daemon here) -- needs the user's machine, same
disclosure as every other Docker Compose component in this project until
it's actually been run.
"""
import sys
import os
import json
import time
import uuid
import urllib.request
import urllib.error

sys.path.insert(0, os.path.dirname(__file__))

REDIS_HOST = os.environ.get("REDIS_HOST", "redis")
REDIS_PORT = int(os.environ.get("REDIS_PORT", "6379"))
QUEUE_KEY = os.environ.get("REDACT_QUEUE_KEY", "redact:raw-events")
REDACT_SERVICE_URL = os.environ.get(
    "REDACT_SERVICE_URL", "http://redact-lb:8080/anonymize"
)
REDACT_SERVICE_API_KEY = os.environ.get("REDACT_SERVICE_API_KEY", "")
OPENSEARCH_HOST = os.environ.get("OPENSEARCH_HOST", "http://opensearch:9200")
BLPOP_TIMEOUT_SECONDS = int(os.environ.get("REDACT_CONSUMER_BLPOP_TIMEOUT", "5"))


def call_redact_service(text: str, log_type: str | None) -> dict:
    """Mirrors redact-pipeline.conf's http filter block exactly: same URL
    env var convention, same header, same JSON body shape (the
    "log"/"log_type" two-key body -- see that file's own Bug 16 comment
    for why this specific shape matters and how the comma mistake
    happened building it). Raises on any failure (non-200, connection
    error, timeout) -- callers route that to quarantine, same as
    redact-pipeline.conf's `_httprequestfailure` tag check."""
    payload = json.dumps({"log": text, "log_type": log_type}).encode("utf-8")
    req = urllib.request.Request(
        REDACT_SERVICE_URL,
        data=payload,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Redact-Api-Key": REDACT_SERVICE_API_KEY,
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def index_document(index: str, doc_id: str, body: dict) -> None:
    """Single-document PUT against OpenSearch's own REST API
    (PUT /<index>/_doc/<id>), the direct equivalent of what Logstash's
    opensearch output plugin does per event under the hood. Using a
    caller-supplied doc_id (never letting OpenSearch assign one) for the
    exact reason redact-pipeline.conf's own uuid filter exists: it makes
    a retried write idempotent (a retry with the SAME id overwrites
    rather than duplicates) without ever risking a content-derived
    collision (see Bugs 4/12's history -- this project already learned
    that lesson once and isn't repeating it here)."""
    url = f"{OPENSEARCH_HOST}/{index}/_doc/{doc_id}"
    payload = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url, data=payload, method="PUT",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        resp.read()


def process_event(raw_event: dict) -> None:
    """One popped queue item -> one redact-service call -> N OpenSearch
    writes (one anonymized-or-quarantine doc, plus one per audit event).
    Field/index names deliberately match redact-pipeline.conf's output
    block so the queued path's data lands in the same indices the
    synchronous path already uses -- reconcile.py and the rest of
    validation/load_test/ work against either path without modification.

    KNOWN SCHEMA DIFFERENCE, disclosed rather than silently assumed away:
    the anonymized/quarantine documents written here are NOT byte-
    identical in shape to what the synchronous Logstash pipeline writes
    -- Logstash adds its own metadata fields (@version, host, an array-
    shaped [log][file][path], etc.) that this script does not attempt to
    reproduce, since redact-pipeline-queued.conf doesn't forward them
    through the queue in the first place (see that file's own comment on
    why). A dashboard or downstream consumer built against the
    synchronous path's exact document shape may need updating to handle
    documents from this path too."""
    text = raw_event.get("message")
    log_type = raw_event.get("log_type")
    if not text:
        return  # nothing to anonymize; matches Logstash silently having
                 # nothing to do on an event with no [message] field

    now = time.gmtime()
    date_suffix = time.strftime("%Y.%m.%d", now)
    month_suffix = time.strftime("%Y.%m", now)

    try:
        result = call_redact_service(text, log_type)
        if "error" in result:
            raise RuntimeError(result["error"])
    except (urllib.error.URLError, urllib.error.HTTPError, RuntimeError,
            TimeoutError, ValueError) as exc:
        # Fail closed, same principle as redact-pipeline.conf's
        # _httprequestfailure branch: unavailable/errored detection means
        # quarantine, never a silent pass-through of un-anonymized text.
        doc_id = str(uuid.uuid4())
        index_document(
            f"security-logs-quarantine-{date_suffix}", doc_id,
            {
                "message": text,
                "log_type": log_type,
                "quarantine_reason": f"redact_service_unreachable_or_error: {exc}",
            },
        )
        return

    doc_id = str(uuid.uuid4())
    index_document(
        f"security-logs-anonymized-{date_suffix}", doc_id,
        {
            "message": result["anonymized"],
            "log_type": log_type,
            "pii_span_count": result.get("span_count", 0),
        },
    )

    # Audit fan-out: one document per audit event, each with its OWN
    # random UUID -- NOT derived from the audit event's own content
    # (authentication_tag) and NOT shared with the parent doc_id above.
    # This is exactly Bug 15's fix in redact-pipeline.conf (see that
    # file's fifth-bug comment): a content-derived audit doc_id collides
    # under real traffic when two distinct audit events share identical
    # field content within the same wall-clock second -- a random UUID
    # per audit event, generated fresh here, doesn't have that failure
    # mode regardless of throughput.
    for audit_event in result.get("audit_events", []):
        audit_doc_id = str(uuid.uuid4())
        index_document(
            f"redact-audit-trail-{month_suffix}", audit_doc_id,
            {"audit_event": audit_event, "log_type": log_type},
        )


def main():
    import redis  # noqa: E402 -- lazy import, same pattern as
                   # anonymize.py's RedisStorageProvider: only this
                   # entrypoint needs the redis package, not every caller
                   # of src/detect.py or src/anonymize.py.

    client = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)
    print(f"queue_consumer: polling '{QUEUE_KEY}' on {REDIS_HOST}:{REDIS_PORT}", flush=True)

    while True:
        item = client.blpop(QUEUE_KEY, timeout=BLPOP_TIMEOUT_SECONDS)
        if item is None:
            continue  # timeout, no event -- loop and block again
        _, raw = item
        try:
            event = json.loads(raw)
        except json.JSONDecodeError as exc:
            print(f"queue_consumer: dropped unparseable queue item: {exc}", flush=True)
            continue
        try:
            process_event(event)
        except Exception as exc:  # noqa: BLE001 -- deliberately broad:
            # one malformed/unexpected event must not kill the consumer
            # loop and stop processing everything behind it in the queue.
            print(f"queue_consumer: error processing event, dropped: {exc}", flush=True)


if __name__ == "__main__":
    main()
