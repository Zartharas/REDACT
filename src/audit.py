"""
Signed audit events for every anonymization action taken. Matches Section 7
of the chapter. Each event records what was done, under what policy version,
and a hash (not the original value itself) so the audit log doesn't become a
second copy of the sensitive data it's documenting.
"""
import hmac
import hashlib
import json
import time


def build_audit_event(field_type: str, method: str, policy_version: str,
                       original_value: str, audit_key: str, fingerprint_key: str) -> dict:
    # The fingerprint uses a separate, keyed HMAC rather than an unkeyed hash.
    # An unkeyed hash of a low-entropy original (an SSN, an IP, a username)
    # is a guess-and-verify oracle for anyone who reads the audit log, no
    # signing key required. Keying the fingerprint closes that specific hole;
    # it does not by itself make the fingerprint safe to disclose outside a
    # controlled audience, since the fingerprint key must then be protected
    # with the same care as the pseudonymization key.
    original_fingerprint = hmac.new(fingerprint_key.encode(), original_value.encode(),
                                     hashlib.sha256).hexdigest()
    event = {
        "field_type": field_type,
        "method": method,
        "policy_version": policy_version,
        "original_value_fingerprint": original_fingerprint,
        "timestamp": int(time.time()),
    }
    payload = json.dumps(event, sort_keys=True).encode()
    # HMAC-SHA256 authentication tag, not a digital signature: it proves the
    # event was produced by, and has not been altered since, someone holding
    # audit_key. Unlike a public-key signature it does not provide
    # independent third-party verification without access to that shared key.
    tag = hmac.new(audit_key.encode(), payload, hashlib.sha256).hexdigest()
    event["authentication_tag"] = tag
    return event


def verify_audit_event(event: dict, audit_key: str) -> bool:
    event = dict(event)
    tag = event.pop("authentication_tag", None)
    if tag is None:
        return False
    payload = json.dumps(event, sort_keys=True).encode()
    expected = hmac.new(audit_key.encode(), payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(tag, expected)
