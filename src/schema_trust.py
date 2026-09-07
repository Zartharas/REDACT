"""
Phase 3 of DETECTION_POLICY_DECOUPLING_SCOPING.md ("Engineering upgrade
20", BUGS_AND_FIXES.md): declares specific structured-log fields as
trusted to always be a given canonical type, so detect.py can skip
regex/NER/entropy detection entirely for that field's value rather than
merely excluding an already-regex-covered span from NER (what field-gating
has done since Phase 0). Scoped to `windows_event`/`syslog` ONLY -- see
this module's own SUPPORTED_LOG_TYPES and the scoping doc's Phase 3
section for why CloudTrail (JSON) cannot safely use this mechanism without
a larger detect.py restructure this pass didn't attempt: excising a JSON
field's value out of the raw text breaks json.loads() for the entire
remainder, silently disabling field-gating for every OTHER field on that
line too.

Same external-config pattern as anonymize.py's load_policy() (Phase 1),
deliberately, for consistency: an optional JSON file, fail-CLOSED on an
invalid file (a canonical type not in anonymize.CANONICAL_TYPES, or a
`cloudtrail` key present at all), fail-OPEN to an empty (fully inert)
default when no file exists. The default is empty rather than Phase 1's
"defaults already cover every type" approach for a load-bearing reason:
there is no safe non-empty default here -- ANY declared field is, by
construction, detection this project's own PIIBench/inject_and_evaluate.py
numbers were never measured against, so shipping even an illustrative
example as active would silently change production detection scope on
upgrade. Phase 3 must be strictly opt-in.
"""
import json
import os

import anonymize

SUPPORTED_LOG_TYPES = frozenset({"windows_event", "syslog"})

DEFAULT_SCHEMA_TRUST_PATH = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "config", "schema_trust.json")
)

# Module-level, mutated by load_schema_trust() below -- same pattern as
# anonymize.py's PSEUDONYMIZE_TYPES/TOKENIZE_TYPES/REDACT_TYPES. Empty by
# default: with nothing declared, detect.detect_all_schema_aware() must
# behave identically to detect.detect_all_field_gated() -- see that
# function's own docstring for the regression test this guarantee rests on.
SCHEMA_TRUST: dict[str, dict[str, str]] = {}


class SchemaTrustConfigError(Exception):
    """Raised by load_schema_trust() when a schema-trust file exists but is
    invalid -- malformed JSON, a `cloudtrail` (or any other unsupported)
    top-level key, or a declared type outside anonymize.CANONICAL_TYPES.
    Never caught inside this module: the point is to stop
    service.py/pipeline.py from starting with a schema-trust config that
    would either do nothing an operator thinks it's doing (a rejected
    log_type) or actively skip detection for a type detect.py can't
    actually produce (a typo'd type name)."""


def resolve_schema_trust_path(path: "str | None" = None) -> str:
    return path or os.environ.get("REDACT_SCHEMA_TRUST_FILE") or DEFAULT_SCHEMA_TRUST_PATH


def load_schema_trust(path: "str | None" = None) -> dict[str, dict[str, str]]:
    """Load {log_type: {field_name: canonical_type}} from an external JSON
    file, overriding this module's SCHEMA_TRUST global (empty by default).

    Missing file: FAIL OPEN to {} (fully inert -- detect_all_schema_aware()
    degrades to detect_all_field_gated() exactly, for every log_type).

    File exists but invalid: FAIL CLOSED, raises SchemaTrustConfigError.
    Specifically rejects (not silently ignores) any top-level key outside
    SUPPORTED_LOG_TYPES -- a `cloudtrail` key present in the file is an
    operator trying to configure something this module cannot safely do;
    refusing to start makes that visible immediately rather than letting
    them believe CloudTrail fields are being schema-trusted when nothing
    is actually happening for them.
    """
    global SCHEMA_TRUST

    resolved_path = resolve_schema_trust_path(path)

    if not os.path.exists(resolved_path):
        import sys
        print(
            f"schema_trust.load_schema_trust(): no file at {resolved_path!r} -- "
            f"schema-trust is fully inert (SCHEMA_TRUST={{}})",
            file=sys.stderr,
        )
        SCHEMA_TRUST = {}
        return dict(SCHEMA_TRUST)

    try:
        with open(resolved_path) as f:
            raw = json.load(f)
    except json.JSONDecodeError as e:
        raise SchemaTrustConfigError(f"schema-trust file {resolved_path!r} is not valid JSON: {e}") from e

    if not isinstance(raw, dict):
        raise SchemaTrustConfigError(
            f"schema-trust file {resolved_path!r} must contain a JSON object "
            f"at the top level, got {type(raw).__name__}"
        )

    new_config: dict[str, dict[str, str]] = {}
    for log_type, declared in raw.items():
        if log_type.startswith("_"):
            continue  # documentation keys, e.g. "_comment" -- see config/schema_trust.json
        if log_type not in SUPPORTED_LOG_TYPES:
            raise SchemaTrustConfigError(
                f"schema-trust file {resolved_path!r}: log_type {log_type!r} is not "
                f"supported -- only {sorted(SUPPORTED_LOG_TYPES)} can safely use "
                f"schema-trust (see DETECTION_POLICY_DECOUPLING_SCOPING.md's Phase 3 "
                f"section for why CloudTrail/JSON cannot)"
            )
        if not isinstance(declared, dict):
            raise SchemaTrustConfigError(
                f"schema-trust file {resolved_path!r}: {log_type!r} must map to an "
                f"object of field_name -> type, got {type(declared).__name__}"
            )
        field_map: dict[str, str] = {}
        for field_name, declared_type in declared.items():
            if not isinstance(declared_type, str):
                raise SchemaTrustConfigError(
                    f"schema-trust file {resolved_path!r}: {log_type}.{field_name} "
                    f"must be a string type name, got {declared_type!r}"
                )
            canonical = declared_type.upper()
            if canonical not in anonymize.CANONICAL_TYPES:
                raise SchemaTrustConfigError(
                    f"schema-trust file {resolved_path!r}: {log_type}.{field_name} "
                    f"declares type {declared_type!r}, not one of "
                    f"anonymize.CANONICAL_TYPES ({sorted(anonymize.CANONICAL_TYPES)})"
                )
            field_map[field_name] = canonical
        new_config[log_type] = field_map

    SCHEMA_TRUST = new_config
    return dict(SCHEMA_TRUST)
