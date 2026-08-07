"""
Extracts (field_name, value) pairs from structured or semi-structured log
lines, so drift detection (drift.py) can track PII hit rate per field rather
than per whole log line.

Scope, stated honestly: this only works where a log format has an
identifiable field structure.
  - cloudtrail: real JSON, flattened to dotted paths.
  - windows_event: key=value pairs, extracted with a regex that tolerates
    values containing spaces (real Windows Event exports frequently have
    multi-word values like a person's name in TargetUserName).
  - syslog: PARTIALLY supported (added 2026-08-07, see
    extract_fields_syslog's own docstring for exactly what is and isn't
    covered). Earlier versions of this module treated syslog as
    unsupported outright, since most syslog message bodies are free text
    with no generic field boundary a parser could locate -- that's still
    true for message shapes this extractor doesn't recognize, and those
    still fall through to zero extracted fields (message-level blindness
    for that line), not a fabricated field structure that isn't really
    there.
"""
import json
import re

_KV_KEY_RE = re.compile(r"(\w+)=")


def _extract_kv_pairs(text: str) -> dict[str, str]:
    """Tolerant key=value extractor, shared by the windows_event and
    syslog (KV-shaped messages) extractors below. A value runs from after
    '=' up to the start of the next recognized key=, or end of string --
    this is what lets 'TargetUserName=Timothy Wong FailureReason=...'
    correctly assign 'Timothy Wong' to TargetUserName instead of splitting
    on every space."""
    matches = list(_KV_KEY_RE.finditer(text))
    fields = {}
    for i, m in enumerate(matches):
        key = m.group(1)
        value_start = m.end()
        value_end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        value = text[value_start:value_end].strip()
        fields[key] = value
    return fields


def extract_fields_windows_event(text: str) -> dict[str, str]:
    return _extract_kv_pairs(text)


def extract_fields_cloudtrail(text: str) -> dict[str, str]:
    """Flattens nested JSON into dotted field paths. Only leaf string values
    are returned -- numbers, booleans, and nulls carry no PII risk in this
    framework's taxonomy and are excluded so they don't dilute hit-rate
    denominators with fields that could never be flagged."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return {}

    fields: dict[str, str] = {}

    def walk(obj, prefix: str):
        if isinstance(obj, dict):
            for k, v in obj.items():
                walk(v, f"{prefix}.{k}" if prefix else k)
        elif isinstance(obj, str):
            fields[prefix] = obj
        # ints/floats/bools/None/lists of non-strings: intentionally skipped

    walk(data, "")
    return fields


# Tag prefix common to syslog lines this project's generator and the
# Loghub OpenSSH/Linux datasets both use: "process[pid]: rest" or
# "process: rest" (no pid). Captured separately from the message body so
# the two known message shapes below (KV-style, and the sshd
# preposition-style auth line) can each parse just the body, not have to
# also account for the tag prefix in their own patterns.
_SYSLOG_TAG_RE = re.compile(r"^(?P<tag>\w+)(?:\[(?P<pid>\d+)\])?:\s*(?P<rest>.*)$")

# sshd auth-message shapes. Order matters: "Failed password for invalid
# user X" must be tried before the plain "Failed password for X" pattern,
# since the plain pattern would otherwise greedily (and wrongly) capture
# "invalid" as part of the username.
_SYSLOG_SSHD_PATTERNS = [
    re.compile(r"^Failed password for invalid user (?P<user>\S+) from (?P<src_ip>\S+) port (?P<port>\d+)"),
    re.compile(r"^Failed password for (?P<user>\S+) from (?P<src_ip>\S+) port (?P<port>\d+)"),
    re.compile(r"^Accepted password for (?P<user>\S+) from (?P<src_ip>\S+) port (?P<port>\d+)"),
    re.compile(r"^Invalid user (?P<user>\S+) from (?P<src_ip>\S+)"),
    re.compile(r"^User (?P<user>\S+) from (?P<src_ip>\S+) not allowed"),
]


def extract_fields_syslog(text: str) -> dict[str, str]:
    """Partial syslog field extraction, added 2026-08-07 (ROADMAP item 8).

    Scope, stated as honestly as the rest of this module: syslog message
    bodies are free text in general, and there is no generic parser that
    reliably locates field boundaries across arbitrary syslog content --
    that limitation from this module's original docstring is still true.
    What changed is that a meaningful fraction of real syslog traffic
    (this project's own three syslog templates in generate_logs.py, and
    common real sshd auth-log lines in datasets like Loghub's OpenSSH) DOES
    have a recognizable, stable shape, and it's worth extracting fields
    from exactly those shapes rather than treating all syslog as opaque.

    Two shapes are recognized, checked in this order after stripping the
    leading "tag[pid]: " or "tag: " prefix:

    1. KV-style bodies (this project's `sudo` and `kernel` templates, e.g.
       "PWD=/home/donaldgarcia ; USER=root ; COMMAND=..." or
       "IN=eth0 SRC=1.2.3.4 DST=10.0.0.1 PROTO=TCP"): reuses the same
       tolerant key=value extractor windows_event already uses, since the
       underlying shape is identical once the syslog tag prefix is
       stripped off.
    2. sshd authentication messages ("Failed password for X from Y port
       Z", "Accepted password for...", "Invalid user X from Y", etc.):
       these have no `=` characters at all, so they'd never match the
       KV extractor, but they DO have a stable preposition-based
       structure ("for X from Y") that a small set of hand-written
       patterns can reliably parse into named fields (user, src_ip, port).

    Message shapes that match neither -- most notably the free-text tail
    of a sudo COMMAND value, or any auth-message wording not in the list
    above -- fall through to zero extracted fields for that line, same as
    before this function existed. This is a deliberate, honest partial
    fix: it closes the gap for the message shapes actually present in
    this project's own corpus and in common real sshd logs, not a claim
    that syslog is now fully covered the way windows_event/cloudtrail are.
    A production deployment logging different syslog message shapes
    (different daemons, different auth mechanisms) would need its own
    additional patterns here, following the same approach.
    """
    m = _SYSLOG_TAG_RE.match(text)
    if not m:
        return {}
    tag, rest = m.group("tag"), m.group("rest")

    if "=" in rest:
        kv = _extract_kv_pairs(rest)
        if kv:
            # Unlike windows_event's space-separated key=value pairs,
            # syslog KV-style messages (this project's `sudo` template is
            # the concrete example) commonly use ';' as the pair
            # separator ("PWD=/home/donaldgarcia ; USER=root ; ..."),
            # which _extract_kv_pairs's next-key lookahead doesn't know to
            # stop at -- it isn't a key=value token itself, so the trailing
            # " ;" ends up tacked onto the previous value. Strip it here
            # rather than complicate the shared KV extractor for a
            # syslog-specific separator convention.
            return {f"{tag}.{k}": v.rstrip(" ;") for k, v in kv.items()}

    for pattern in _SYSLOG_SSHD_PATTERNS:
        sm = pattern.match(rest)
        if sm:
            return {f"{tag}.{k}": v for k, v in sm.groupdict().items() if v is not None}

    return {}


def extract_fields(log_type: str, text: str) -> dict[str, str]:
    if log_type == "windows_event":
        return extract_fields_windows_event(text)
    if log_type == "cloudtrail":
        return extract_fields_cloudtrail(text)
    if log_type == "syslog":
        return extract_fields_syslog(text)
    return {}  # unrecognized log_type
