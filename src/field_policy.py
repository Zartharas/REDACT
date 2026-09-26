"""
Identity-field pseudonymization policy (PCCF phase 4, F2a). Additive.

In structured telemetry, a value in an identity field (user=,
TargetUserName=, userIdentity.userName, "for user X", ...) is identity
data whatever it looks like. Deciding whether to pseudonymize it by
asking a NAME detector is what produces the flattened-username misses.
This policy marks every identity-field value for pseudonymization,
except well-known system/service accounts (ALLOWLIST), whose
pseudonymization would cost forensic utility without protecting a
natural person.

Uses the same identity-key vocabulary as pccf_context.TELEMETRY_IDENTITY_KEYS.
"""
import re

ALLOWLIST = {"root", "system", "local service", "network service", "anonymous logon", "-", "unknown",
             "daemon", "nobody", "admin", "administrator", "guest"}

_KV = re.compile(r'"?\b(targetusername|subjectusername|accountname|username|user_name|samaccountname|'
                 r'principal|owner|login|logname|ruser|user)"?\s*[=:]', re.IGNORECASE)
_PHRASE = re.compile(r"\b(invalid user|for user|by user|user)\s+", re.IGNORECASE)


_SPACED_KEYS = {"targetusername", "subjectusername", "accountname", "samaccountname"}


def _value_end(text, i, key):
    if i < len(text) and text[i] == '"':
        j = text.find('"', i + 1)
        return i + 1, (j if j != -1 else len(text))
    if key.lower() in _SPACED_KEYS:
        # Windows-style KV values may contain spaces ("LOCAL SERVICE",
        # "John Smith"): stop at the next " Key=" or line/record end.
        m = re.search(r' [A-Za-z][\w.]*=|[",}\n]', text[i:])
    else:
        # generic keys (user=, ruser=, logname=): the value is one token
        m = re.search(r'[\s",;}]', text[i:])
    return i, (i + m.start() if m else len(text))


def identity_values(text: str) -> list[dict]:
    out = []
    for m in _KV.finditer(text):
        i = m.end()
        q = re.match(r'\s*"', text[i:])
        if q:  # JSON style: "userName": "value"
            i += q.end() - 1
        elif i < len(text) and text[i].isspace():
            continue  # empty value, e.g. "logname= uid=0"
        s, e = _value_end(text, i, m.group(1))
        out.append((s, e))
    for m in _PHRASE.finditer(text):
        s = m.end()
        mm = re.match(r"\S+", text[s:])
        if mm:
            out.append((s, s + mm.end()))
    hits, seen = [], set()
    for s, e in sorted(out):
        val = text[s:e].strip()
        if not val or (s, e) in seen:
            continue
        seen.add((s, e))
        allow = val.lower() in ALLOWLIST or val.endswith("$")
        hits.append({"type": "PERSON", "start": s, "end": e, "method": "field_policy",
                     "allowlisted": allow, "value": val})
    return hits


def scan_field_policy(text: str) -> list[dict]:
    return [h for h in identity_values(text) if not h["allowlisted"]]
