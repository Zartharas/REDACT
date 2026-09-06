"""
Signed audit trail for POLICY CHANGES -- distinct from audit.py's
build_audit_event(), which records individual PII-redaction actions taken
under a given policy, not changes to the policy itself. Added for Phase 2
of DETECTION_POLICY_DECOUPLING_SCOPING.md ("Engineering upgrade 19",
BUGS_AND_FIXES.md): once policy can change at runtime (policy_watcher.py)
without a code review / deploy gate in between, "REDACT enforces policy X"
becomes an unverifiable claim unless every change -- attempted or
successful -- is recorded somewhere an auditor can read independently of
whatever the live in-memory policy happens to be right now.

Uses the same HMAC-SHA256-authentication-tag pattern as audit.py's
build_audit_event() (same reasoning: proves an event was produced by, and
unaltered since, someone holding the signing key -- not a public-key
signature, no independent third-party verification without that key) and
DELIBERATELY reuses audit.py's own AUDIT_KEY rather than minting a new one:
audit.py splits AUDIT_KEY from FINGERPRINT_KEY specifically because
FINGERPRINT_KEY protects a narrow-audience, guessable-original-value
concern (a keyed hash of a low-entropy value like an SSN) that a wide
audit-reading audience must NOT be able to brute-force. Nothing in a
policy-change event (type names, file hashes, timestamps, PIDs) carries
that same guessable-original-value risk, so there is no analogous
narrow-audience concern here to justify a fourth key -- reusing AUDIT_KEY
is a considered choice, not an oversight.
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


def build_policy_audit_event(
    outcome: str,
    resolved_path: str,
    old_policy_hash: "str | None",
    new_policy_hash: "str | None",
    triggered_by: str,
    audit_key: str,
    worker_pid: "int | None" = None,
    error: "str | None" = None,
) -> dict:
    """outcome: "reloaded" (policy actually changed and took effect) or
    "rejected" (file existed but failed anonymize.load_policy()'s
    validation -- old policy remains active, see policy_watcher.py's own
    comment on why this must never propagate as a crash for an
    already-running worker). policy_watcher.PolicyWatcher.check_now() has
    a third outcome, "unchanged", but deliberately does NOT call this
    function for it -- an audit-log line every poll interval confirming
    "still nothing happened" would grow without bound and carry no
    information beyond what the Prometheus gauges
    (redact_policy_last_check_timestamp_seconds) already provide far more
    cheaply; this audit log exists to record CHANGES (attempted or
    successful), not liveness.

    triggered_by: "poller" (this worker's own background PolicyWatcher
    thread, on its normal interval) or "admin_endpoint" (a caller hit
    POST /admin/policy/reload on THIS worker specifically -- see that
    route's docstring in service.py for why this only ever affects one
    worker, not a cluster-wide guarantee).

    Deliberately records HASHES of the policy content (sha256 of the
    resolved file's raw bytes), not the policy content itself a second
    time -- config/policy.json is not sensitive, so this isn't a
    confidentiality concern the way audit.py's fingerprint-not-original
    choice is; the hash is what lets an auditor confirm two audit events
    (from two different workers, or from a worker and its own later
    reload) refer to the identical file version without needing this
    module to duplicate anonymize.py's own file-diffing logic.
    """
    event = {
        "event_type": "policy_change",
        "outcome": outcome,
        "resolved_path": resolved_path,
        "old_policy_hash": old_policy_hash,
        "new_policy_hash": new_policy_hash,
        "triggered_by": triggered_by,
        "worker_pid": worker_pid if worker_pid is not None else os.getpid(),
        "error": error,
        "timestamp": int(time.time()),
    }
    payload = json.dumps(event, sort_keys=True).encode()
    tag = hmac.new(audit_key.encode(), payload, hashlib.sha256).hexdigest()
    event["authentication_tag"] = tag
    return event


def verify_policy_audit_event(event: dict, audit_key: str) -> bool:
    event = dict(event)
    tag = event.pop("authentication_tag", None)
    if tag is None:
        return False
    payload = json.dumps(event, sort_keys=True).encode()
    expected = hmac.new(audit_key.encode(), payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(tag, expected)


class _FlockContext:
    """Minimal copy of anonymize.py's own _FlockContext, not imported from
    there, to keep this module usable standalone (it has no other
    dependency on anonymize.py) and because the two lock files protect
    entirely unrelated critical sections (TokenStore's load-merge-save vs.
    this module's append-only log) -- sharing the class is fine, sharing
    the LOCK FILE PATH would not be. See anonymize.py's own _FlockContext
    for the full reasoning on blocking-exclusive-flock-on-a-sibling-file,
    the fallback when fcntl is unavailable, and why append mode is used."""

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


def append_policy_audit_event(event: dict, log_path: str) -> None:
    """Append one JSON line to the policy-change audit log, guarded by an
    exclusive advisory lock on a sibling `.lock` file (same pattern as
    anonymize.py's FileStorageProvider.lock_for_save()) -- NEEDED here for
    a reason Phase 1 never had: every gunicorn worker in a multi-worker
    deployment runs its own independent PolicyWatcher poller (see that
    module), so this file can receive genuinely concurrent appends from
    separate OS processes, not just separate threads in one process. A
    bare open(path, "a").write(...) is atomic against interleaving ONLY
    for writes at or under PIPE_BUF (4KB on Linux) -- true for any single
    line this function produces today, but relying on that margin silently
    is exactly the kind of unverified assumption this project's own
    FileStorageProvider comment already argues against; taking the lock
    costs one flock() per append (cheap, not on any request's hot path)
    and removes the assumption entirely rather than trusting it."""
    directory = os.path.dirname(log_path) or "."
    os.makedirs(directory, exist_ok=True)
    with _FlockContext(log_path + ".lock"):
        with open(log_path, "a") as f:
            f.write(json.dumps(event) + "\n")
