"""
Signed audit trail for schema-trust SAMPLING RESULTS -- the mandatory
mitigation DETECTION_POLICY_DECOUPLING_SCOPING.md's Phase 3 devil's-
advocate section requires: every declared field is periodically re-checked
by real detection (off the request hot path, see schema_trust_sampler.py),
and every re-check's outcome -- consistent with the declared type, or
drifted -- is recorded here, independently of whether anyone is watching
the Prometheus counters at the moment drift happens.

Same HMAC-SHA256-authentication-tag pattern as audit.py/policy_audit.py,
reusing AUDIT_KEY for the same reason policy_audit.py does (no
narrow-audience guessable-original-value concern in a schema-trust event
the way FINGERPRINT_KEY's split from AUDIT_KEY protects against).

Deliberately does NOT record the sampled field's actual value or even a
fingerprint of it: the sampled text may be exactly the kind of PII this
whole project exists to protect (a real name, a real IP), so writing it --
or a value derived only from it -- into a SECOND log defeats the purpose
of sampling it in the first place. What's recorded is enough to
investigate (which field, which log_type, what type detection actually
found vs. what was declared) without ever re-exposing the value itself.
"""
import hashlib
import hmac
import json
import os
import time

try:
    import fcntl
    _HAVE_FCNTL = True
except ImportError:  # pragma: no cover -- see anonymize.py's identical guard
    _HAVE_FCNTL = False


def build_schema_trust_audit_event(
    outcome: str,
    log_type: str,
    field_name: str,
    declared_type: str,
    actual_types_found: list,
    audit_key: str,
    worker_pid: "int | None" = None,
) -> dict:
    """outcome, one of three (see schema_trust_sampler.py's own comment for
    the full reasoning):
      - "consistent": independent re-detection on the isolated field value
        found the declared_type among its results.
      - "no_signal": independent re-detection found NOTHING at all.
        Deliberately NOT treated as drift -- an isolated field value
        (without the surrounding line's context) is often genuinely
        harder for NER to confirm than the full line was, a known
        limitation this project's own PIIBench work already measured
        (context-starved NER has worse recall) -- so "found nothing" is
        ambiguous, not strong evidence the schema declaration is wrong.
        Still recorded (not silently dropped) so a field that NEVER once
        returns "consistent" is still visible to an operator, just not
        flagged with the same urgency as a real disagreement.
      - "drift_detected": independent re-detection found a DIFFERENT,
        specific type than what was declared -- the strong, actionable
        signal that the schema assumption for this field may be wrong.
    """
    event = {
        "event_type": "schema_trust_sample",
        "outcome": outcome,
        "log_type": log_type,
        "field_name": field_name,
        "declared_type": declared_type,
        "actual_types_found": sorted(set(actual_types_found)),
        "worker_pid": worker_pid if worker_pid is not None else os.getpid(),
        "timestamp": int(time.time()),
    }
    payload = json.dumps(event, sort_keys=True).encode()
    tag = hmac.new(audit_key.encode(), payload, hashlib.sha256).hexdigest()
    event["authentication_tag"] = tag
    return event


def verify_schema_trust_audit_event(event: dict, audit_key: str) -> bool:
    event = dict(event)
    tag = event.pop("authentication_tag", None)
    if tag is None:
        return False
    payload = json.dumps(event, sort_keys=True).encode()
    expected = hmac.new(audit_key.encode(), payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(tag, expected)


class _FlockContext:
    """Same minimal copy as policy_audit.py's own -- see that module's
    docstring for why each audit-log module keeps its own copy rather than
    sharing one lock-file path across unrelated critical sections."""

    def __init__(self, lock_path: str):
        self._lock_path = lock_path
        self._fh = None

    def __enter__(self):
        if not _HAVE_FCNTL:
            return self
        self._fh = open(self._lock_path, "a")
        fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self._fh is not None:
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
            self._fh.close()
            self._fh = None
        return False


def append_schema_trust_audit_event(event: dict, log_path: str) -> None:
    directory = os.path.dirname(log_path) or "."
    os.makedirs(directory, exist_ok=True)
    with _FlockContext(log_path + ".lock"):
        with open(log_path, "a") as f:
            f.write(json.dumps(event) + "\n")
