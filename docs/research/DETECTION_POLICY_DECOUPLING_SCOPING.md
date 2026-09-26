# Scoping: decoupling detection from redaction policy (Streamdal/AnonShield pattern)

Follow-up from the 2026-09 landscape scan. Two real systems were checked
directly (not assumed from memory) for how they separate "what gets
detected" from "what happens to it": Streamdal (commercial, rule-based log
pipelines) and AnonShield (arXiv:2606.15650, a CSIRT pseudonymization
framework, GPL-adjacent open research code). This is a scoping document
only, per this project's own standing convention for this kind of task
(see `wasm/EDGE_COLLECTOR_INTEGRATION_SCOPING.md` and
`monitoring/SOURCE_ATTRIBUTION_DESIGN.md`) -- no implementation attempted.

## REDACT's current state (confirmed by reading the actual code, not assumed)

`src/detect.py` returns typed spans (`{"type": "PERSON", "start": ..., "end": ...}`)
with no opinion about what happens to them. `src/anonymize.py`'s
`anonymize_by_policy()` is the policy layer -- but the policy itself is
three Python module-level set literals:

```python
PSEUDONYMIZE_TYPES = {"IP", "PERSON"}
TOKENIZE_TYPES = {"EMAIL", "MRN", "CREDIT_CARD", "SSN"}
REDACT_TYPES: set[str] = set()
```

Changing REDACT's policy today -- e.g., "stop pseudonymizing PERSON, redact
it instead" -- means editing this file and redeploying the container.
There is a second, narrower kind of decoupling already in place
(`detect_all_field_gated()` + `fields.py`), but it only decides whether NER
runs on a given field's text -- it never skips detection outright, and it
has no concept of an operator-editable policy at all.

## What Streamdal actually does

Rules/pipelines are managed from a separate console and distributed to
log-processing agents in real time; a rule can be hot-swapped (made more or
less strict, or replaced) without redeploying the instrumented application.
Detection logic (what a rule matches) and enforcement (replace/remove/
notify) are both expressed as data the pipeline consumes, not code the
application ships with. The architectural point worth taking: policy lives
outside the deployment unit, and changing it doesn't require a CI/CD cycle.

## What AnonShield actually does (read directly from the paper, arXiv:2606.15650)

AnonShield "decouples entity detection from replacement policy, enabling
fine-grained control via `--entities-to-preserve` and `fields_to_exclude`,
which support selective pseudonymization and field-level exclusion without
altering the recognition pipeline." It goes one step further than
Streamdal's rule model with a **schema-aware `anonymization_config`**
(`force_anonymize`, `fields_to_anonymize`, `fields_to_exclude`) that, for
known structured fields, skips the NER/RegEx detection pipeline *entirely*
rather than just filtering its output -- the paper reports this cuts
processing time on a 550MB JSON dataset from 985.7s to 18.88s (≈47x),
because deterministic field-level remapping bypasses inference altogether.

This is meaningfully different from REDACT's own field-gated NER: REDACT's
`build_ner_candidate()` skips NER only for the specific text a regex hit
already covers on a line, but `scan_regex()` itself still runs on every
field, every time. AnonShield's `force_anonymize`/`fields_to_exclude` skip
*all* detection for a declared field, trusting the schema instead.

## Proposed phased scope for REDACT

**Phase 1 (low risk, mirrors AnonShield's `--entities-to-preserve`/`fields_to_exclude`):**
Externalize `PSEUDONYMIZE_TYPES`/`TOKENIZE_TYPES`/`REDACT_TYPES` from Python
module constants into a config file (YAML/JSON) loaded at process startup
by `service.py`/`pipeline.py`. An operator edits a file and restarts the
service to change policy -- no code change, no rebuild. This is the
smallest possible step that captures the actual decoupling AnonShield's
paper describes, and it's buildable and testable entirely within this
project's existing environment and free-tools constraint.

**Phase 2 (hot-reload, Streamdal-style):** Watch the policy file for
changes (or expose an authenticated admin endpoint accepting a new policy
document) and reload without a container restart. Real operational value
for a SOC that needs to react to a new requirement or incident faster than
a CI/CD cycle allows. Needs its own access control and an audit trail of
who changed what policy when -- see the devil's-advocate section below
before treating this as a small step.

**Phase 3 (AnonShield's schema-aware detection bypass, not just NER-skip):**
Extend `fields.py`'s per-`log_type` field recognition into a genuine
"always this type, skip detection entirely" declaration for specific,
well-known fields (e.g., CloudTrail's `sourceIPAddress` JSON key), the way
AnonShield's `force_anonymize` does -- REDACT's existing field-gating only
ever skips NER, never `scan_regex()`, so this would be new capability, not
an extension of what already exists. This is also the riskiest phase; see
below.

## Devil's advocate: what could go wrong at each phase, and how to mitigate it

**Phase 1 -- policy-file integrity.** An externally-loaded policy file is a
new artifact someone has to protect. A misconfigured or tampered file that
silently maps `PERSON` to nothing (or to an unrecognized type, which
`anonymize_by_policy()`'s `transform()` already handles by leaving the
value untouched rather than guessing) turns off PII protection with no
visible error -- exactly the kind of silent failure a GDPR Article 32
audit would flag as a control that isn't actually enforced. Mitigation: a
startup-time validator that rejects a policy file mapping any of REDACT's
canonical detected types (PERSON, EMAIL, IP, SSN, CREDIT_CARD, MRN) to
neither `PSEUDONYMIZE_TYPES`, `TOKENIZE_TYPES`, nor `REDACT_TYPES`
explicitly -- fail closed (refuse to start) rather than silently defaulting
to "leave untouched" for a type the file forgot to mention.

**Phase 2 -- hot-reload attack surface and staleness.** Whoever can write
the policy file or call the reload endpoint can disable protection live,
with no deploy artifact and no code review gate in between. This needs (a)
its own authentication, separate from anything else REDACT exposes today
(there is no admin-auth layer in `service.py` currently), and (b) an
append-only audit log of every policy change, who made it, and when --
without that, "REDACT enforces policy X" becomes an unverifiable claim the
moment policy can change silently at runtime. Also needs an explicit answer
to "what happens if the reload fails halfway" -- REDACT should keep serving
under the last-known-good policy rather than crash or fall back to an
undefined state.

**Phase 3 -- trusting the schema is itself a risk, not a free win.**
Skipping detection entirely for a declared field is stronger, and riskier,
than skipping only NER. If the schema assumption is ever wrong -- a
CloudTrail field that's supposed to always be a plain account ID but
occasionally carries a free-text error string with an embedded email
address, say, because of an upstream API change -- a field marked
`force_anonymize`/exclude-from-detection produces a false negative REDACT's
current, always-runs-detection-on-everything approach would still have
caught. AnonShield's own paper does not claim this is risk-free; it's an
operator-declared trust boundary, and the paper's own false-negative
analysis (Table VIII, `Version/Strategy` breakdown) shows accuracy already
degrades even with full detection running, from NER context starvation and
schema-less free text -- a fully-skipped field has no equivalent recovery
path at all. **Concrete mitigation available inside REDACT already:**
`drift.py` exists specifically to catch a field's statistical shape
changing over time. Extending it to periodically re-run full detection on
a small random sample of otherwise-skipped fields (off the hot path, async)
would catch schema drift in an excluded field before it becomes a silent,
sustained leak -- turning an unmonitored trust assumption into a monitored
one. This should be a hard requirement for shipping Phase 3, not an
optional nice-to-have.

**Cross-cutting -- the pseudonymize/tokenize/redact distinction is a legal
one, not just an engineering one.** README.md already documents (citing
the Article 29 Working Party 2014 opinion and EDPB Guidelines 01/2025) that
pseudonymized data remains personal data in full GDPR scope, while properly
irreversible redaction may not. A policy layer that becomes easier to edit
makes it easier to change this classification without necessarily reading
that documentation first -- e.g., an operator moving `CREDIT_CARD` from
`TOKENIZE_TYPES` to `REDACT_TYPES` needs to understand that this is a
one-way door for every future occurrence (past tokenized values stay
resolvable via the existing `TokenStore`; new ones won't exist to resolve
at all). Any policy-editing surface built in Phase 1/2 should surface this
distinction inline -- a warning at edit time, not just prose in a README a
different person may never open.

## Recommendation

Build Phase 1 only for now. It captures the concrete decoupling both
Streamdal and AnonShield demonstrate, needs no new infrastructure, and is
fully buildable and testable in this project's existing environment.
Phases 2 and 3 both introduce real, unresolved risk (an auth/audit gap and
a schema-trust gap, respectively) that this scoping pass is deliberately
not resolving by guessing -- consistent with this project's standing
discipline against building things it has no way to verify here.

## Update, 2026-09: Phases 1 and 2 built (see BUGS_AND_FIXES.md "Engineering
upgrade 18" and "19"). Phase 3 design, below, before implementing it.

Checked what actually exists in `fields.py` before designing against it,
rather than assuming the sketch above ("extend fields.py's per-log_type
field recognition") pointed at something already there. It doesn't:
`extract_fields_cloudtrail()` is a fully generic recursive JSON flattener
with **no enumerated field list at all** -- `sourceIPAddress` (this doc's
own example above) appears only as whatever dotted key happens to exist
in a given JSON event, not as a name fields.py already recognizes. Building
Phase 3 means adding a genuinely new declaration surface, not wiring up
something that's already there. Also confirmed: today, `scan_regex()` runs
unconditionally on every field's text; field-gating only ever skips NER for
regex-covered spans (`build_ner_candidate()`), never regex itself -- so
"skip detection entirely for a declared field" is new behavior end to end,
not an extension of the existing NER-skip.

**Concrete design, following Phase 1/2's own established pattern for
consistency (external file, `load_*()` + a `*ConfigError`, fail-closed on
an invalid file, fail-open/inert on a missing one):**

- `config/schema_trust.json`: `{log_type: {field_name: canonical_type}}`,
  e.g. `{"cloudtrail": {"sourceIPAddress": "IP"}}`. **Ships EMPTY
  (`{}` for every log_type) by default.** This is the one point where
  Phase 3 deliberately diverges from Phase 1's own default: Phase 1's
  built-in defaults happen to already cover every canonical type (so a
  missing file changes nothing), but Phase 3 has no safe non-empty
  default -- any declared field is, by construction, detection this
  project's own PIIBench/inject_and_evaluate.py numbers were never
  measured against. Shipping even the illustrative `sourceIPAddress`
  example as an active default would silently change production detection
  scope the moment someone deploys this, contradicting every accuracy
  number this project has published. Phase 3 must be strictly opt-in.
- `src/schema_trust.py`: `load_schema_trust()`/`SchemaTrustConfigError`,
  same resolution order and fail-closed-on-invalid/fail-open-on-missing
  split as `anonymize.load_policy()`. Validates every declared type
  against `anonymize.CANONICAL_TYPES` and every log_type key against the
  three `fields.py` recognizes.
- `detect.py` gains `detect_all_schema_aware()`, wrapping
  `detect_all_field_gated()`: for each field `fields.extract_fields()`
  finds that has a schema-trust declaration for this log_type, locate that
  field's value substring in the raw text (a plain `str.find()`, since
  `fields.py`'s extractors don't preserve offsets today -- a real,
  disclosed limitation, not silently assumed away: if the value can't be
  located verbatim in the text, e.g. because it was itself already
  transformed by an earlier extraction step, this function refuses to
  trust it and falls through to full detection for that field instead of
  guessing at a span), emit a span of the declared type directly for it
  with NO regex/NER/entropy call against that substring, excise it from
  what the rest of the pipeline sees, and run the normal
  `detect_all_field_gated()` on the remainder. **With an empty (default)
  schema_trust config, this function's output must be byte-identical to
  `detect_all_field_gated()`'s own -- a real regression test, not just an
  assertion, is the actual proof Phase 3 changes nothing until an operator
  opts in.**
- **The mandatory mitigation, built as a hard requirement, not an
  optional nice-to-have (per this doc's own original Phase 3 devil's-
  advocate section below):** `src/schema_trust_sampler.py`, a background
  daemon thread (same shape as Phase 2's `PolicyWatcher` -- per-worker,
  daemon=True, broad try/except around the loop body) that drains a
  small thread-safe queue of (declared_type, field_text) pairs sampled at
  a configurable rate (`REDACT_SCHEMA_TRUST_SAMPLE_RATE`, default 1%) at
  the moment a schema-trust span is about to be emitted, and re-runs FULL
  detection against just that field's isolated text, OFF the request hot
  path. A sampled result that disagrees with the declared type (finds a
  different type, or finds nothing when the declared type implies it
  should) is logged to its own signed audit stream (mirroring
  `policy_audit.py`) and a Prometheus counter, as the actual, monitored
  answer to "what happens when the schema assumption turns out to be
  wrong" -- this doc's own Phase 3 devil's-advocate section already named
  this as the concrete mitigation `drift.py` could provide; built here as
  a live async sampler rather than `drift.py`'s existing batch/weekly-cron
  path (`airflow_tasks.py`'s `check_taxonomy_drift`), since a leak from a
  wrongly-trusted field should not have to wait for the next weekly
  Airflow run to surface.
**Further finding, made while working through the excision mechanism concretely (not caught until actually tracing `fields.py`'s CloudTrail path step by step): excising a declared field's VALUE out of raw CloudTrail text and then re-running `fields.extract_fields_cloudtrail()` on what's left breaks JSON well-formedness for the WHOLE remainder** (`json.loads()` fails outright on a string with a hole spliced into one of its values), silently disabling field-gating for every OTHER field on that line too -- a real regression, not hypothetical, found by tracing the actual code path rather than assumed safe by analogy to `build_ner_candidate()`'s own (KV-format-only) excision, which never re-parses anything as JSON. A second finding, from the same trace: REDACT's existing field-gated NER already excludes regex-matched spans (which includes IP) from the NER call today -- so for an IP-typed field specifically, most of the throughput benefit Phase 3 is meant to provide is already captured by code this project shipped months ago, independent of Phase 3 entirely.

**Final decision, made explicitly rather than building past this finding: Phase 3 is scoped to `windows_event`/`syslog` only.** These are KV-shaped text, not JSON -- excising a value and re-running the KV regex extractor on the remainder is exactly the mechanism `build_ner_candidate()` already relies on and has already been validated safe for (see that function's own docstring on why KV values are always delimiter-bounded on both sides). `config/schema_trust.json` therefore only accepts `windows_event`/`syslog` as top-level keys; a `cloudtrail` key present in the file is REJECTED (fail-closed, not silently ignored) so an operator can't believe they've configured CloudTrail schema-trust when it would silently do nothing. **This means the original doc's own CloudTrail `sourceIPAddress` illustration is explicitly NOT what got built** -- disclosed here rather than left to look like the implementation matches the sketch above.

**Honest reframing of what this delivers:** a genuine skip-detection mechanism for `windows_event`/`syslog` fields (regex/NER/entropy never run against a declared field's value at all, using the same excise-and-remap machinery `build_ner_candidate()`/`remap_hit()` already provide), with the mandatory async drift-sampling safety net described above as the actual, monitored answer to "what if the schema assumption is wrong." **What this does NOT deliver: AnonShield's own reported ~47x number**, which came from skipping detection on large-volume STRUCTURED (JSON-shaped) data specifically -- the one format this pass found isn't safely excisable with REDACT's current single-pass whole-line detection architecture. Achieving that would need restructuring `detect.py` to scan structured fields independently rather than as part of one whole-line regex/NER pass -- a materially larger, riskier change, explicitly flagged as a real future item (a "Phase 4"), not attempted here.

- **What can and cannot be verified in this sandbox:** the mechanism
  (schema declares a `windows_event`/`syslog` field, detection is skipped
  for it via the same excise-and-remap path `build_ner_candidate()` already
  uses, the sampler independently re-checks a fraction of skipped events
  and flags disagreement) is fully testable here with synthetic data, using
  worked examples typed as PERSON/IP/EMAIL over regex- and
  entropy-detectable text -- no spaCy/Presidio model needed for the
  regex-typed cases, so most of this phase's tests don't need the
  analyzer-mocking workaround test_service_auth.py/test_policy_config.py/
  test_policy_hot_reload.py all require. What is NOT verified here, and
  should not be claimed: any real-world throughput number, for either the
  windows_event/syslog case actually built (no live high-volume corpus to
  measure it against) or AnonShield's own reported ~47x (which this pass
  found isn't safely achievable at all with REDACT's current architecture
  for the JSON/CloudTrail case that number was measured against -- see
  the finding above).
