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
  - syslog: NOT supported here. This dataset's syslog lines are closer to
    free text (an sshd message, a sudo command line) with no reliable field
    boundary a generic parser could locate. Treated as message-level only;
    drift.py explicitly skips syslog rather than pretending a field
    structure that isn't there.
"""
import json
import re

_KV_KEY_RE = re.compile(r"(\w+)=")


def extract_fields_windows_event(text: str) -> dict[str, str]:
    """Tolerant key=value extractor. A value runs from after '=' up to the
    start of the next recognized key=, or end of string -- this is what
    lets 'TargetUserName=Timothy Wong FailureReason=...' correctly assign
    'Timothy Wong' to TargetUserName instead of splitting on every space."""
    matches = list(_KV_KEY_RE.finditer(text))
    fields = {}
    for i, m in enumerate(matches):
        key = m.group(1)
        value_start = m.end()
        value_end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        value = text[value_start:value_end].strip()
        fields[key] = value
    return fields


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


def extract_fields(log_type: str, text: str) -> dict[str, str]:
    if log_type == "windows_event":
        return extract_fields_windows_event(text)
    if log_type == "cloudtrail":
        return extract_fields_cloudtrail(text)
    return {}  # syslog: unsupported by design, see module docstring
