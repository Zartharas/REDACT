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
                       original_value: str, audit_key: str) -> dict:
    original_hash = hashlib.sha256(original_value.encode()).hexdigest()
    event = {
        "field_type": field_type,
        "method": method,
        "policy_version": policy_version,
        "original_value_hash": original_hash,
        "timestamp": int(time.time()),
    }
    payload = json.dumps(event, sort_keys=True).encode()
    signature = hmac.new(audit_key.encode(), payload, hashlib.sha256).hexdigest()
    event["signature"] = signature
    return event


def verify_audit_event(event: dict, audit_key: str) -> bool:
    event = dict(event)
    signature = event.pop("signature", None)
    if signature is None:
        return False
    payload = json.dumps(event, sort_keys=True).encode()
    expected = hmac.new(audit_key.encode(), payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature, expected)
