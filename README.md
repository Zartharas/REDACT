# REDACT

PII/PHI detection and anonymization for heterogeneous security telemetry. A three-layer detection ensemble (regex, Microsoft Presidio NER, Shannon entropy) combined with a taxonomy-driven anonymization router (redaction, keyed pseudonymization, reversible tokenization) and a field-level drift detector for catching a field that silently starts carrying personal data it never carried before.

Every number in this README comes from an actual executed run, not a projection, run `validate.py` yourself and check. No real data anywhere in this repository; the entire corpus is synthetically generated with a fixed seed.

This is the reference implementation accompanying two related but independently written pieces of work: a practitioner-oriented book chapter (IntechOpen, *Data Privacy in Practice*) and an empirical research paper. See each publication for its own scope; this repository is the shared technical foundation, not a copy of either.

## Does this actually work? Run this and see.

```bash
python src/generate_logs.py --n 10000 --out data/synthetic_logs.jsonl --dirty-ratio 0.3
python validate.py
```

`validate.py` is a single consolidated script answering exactly that question: 18 checks across every major claim in this README, run fresh, with no historical reliance on "it worked when I tested it before." Every check is a real assertion against real output, not a print statement dressed up to look like one. A failing check makes the script exit non-zero and print exactly what failed.

What it checks, in order: ground-truth offsets in the generated corpus are correct, detection recall and precision clear defensible floors (and the rigid-format entity types hit 100% recall, which they should), anonymization preserves correlation and round-trips correctly through tokenization, anonymized CloudTrail output stays valid JSON (a regression check for the overlapping-span bug described below), audit signatures verify and correctly reject tampering, and the taxonomy drift detector catches a freshly injected drift scenario while producing zero false positives on stable data. Eighteen checks, eighteen passes, on a fresh run against the code as it currently exists.

## Setup

```bash
python3 -m venv venv
source venv/bin/activate    # on Windows: venv\Scripts\activate
pip install -r requirements.txt
python -m spacy download en_core_web_lg
```

## Reproduce the results

```bash
python src/generate_logs.py --n 10000 --out data/synthetic_logs.jsonl --dirty-ratio 0.3
python src/evaluate.py --data data/synthetic_logs.jsonl
python src/analyze_entropy.py data/synthetic_logs.jsonl
```

Everything is seeded (`Faker.seed(42)`, `random.seed(42)`), so a fresh run reproduces the same dataset and the same numbers.

## Does it hold up on real data, not just synthetic?

```bash
cd validation/real_data
bash download_loghub.sh
python inject_and_evaluate.py
```

The synthetic corpus above is a close-to-best-case scenario for any detector, since its own generation templates are the patterns the regex layer was designed against. `validation/real_data/` answers the harder question directly: tested against five real, independently collected, unmodified log datasets (not written by anyone here), the same format-sensitivity gap shows up every time, and precision is consistently lower than the synthetic numbers alone would suggest. See `validation/real_data/README.md` for what's actually being tested and why eleven of the sixteen candidate datasets were excluded rather than forced into producing a number.

## What was actually measured (single run, 10,000 synthetic entries, 3,002 containing PII)

Hardware: 1 vCPU, 4 GB RAM, no GPU. Numbers below are single-threaded Python throughput, **not** a benchmark of a production Logstash/OpenSearch deployment. That architecture is described in the chapter but was not built or load-tested in this proof of concept.

| Condition | Micro-avg precision | Micro-avg recall | Micro-avg F1 | Throughput |
|---|---|---|---|---|
| Regex only | 0.574 | 0.572 | 0.573 | ~49,000 events/sec |
| Regex + NER, tiered (NER skipped when regex already found something in the line) | 0.577 | 0.626 | 0.600 | ~234 events/sec |
| Regex + NER, naive (NER runs on every line) | 0.588 | 0.745 | 0.657 | ~113 events/sec |

Per-entity-type detail (naive condition, full ensemble):

| Type | TP | FP | FN | Precision | Recall | F1 |
|---|---|---|---|---|---|---|
| CREDIT_CARD | 237 | 0 | 0 | 1.000 | 1.000 | 1.000 |
| EMAIL | 488 | 0 | 0 | 1.000 | 1.000 | 1.000 |
| SSN | 250 | 0 | 0 | 1.000 | 1.000 | 1.000 |
| MRN | 242 | 0 | 0 | 1.000 | 1.000 | 1.000 (custom Presidio pattern recognizer) |
| IP | 2327 | 2626 | 0 | 0.470 | 1.000 | 0.639 |
| PERSON | 1073 | 613 | 1582 | 0.636 | 0.404 | 0.494 |

### Three findings worth building the chapter around, because they're real

**1. IP precision is bad for a specific, traceable reason, not a vague one.** All 2,626 IP false positives trace back to a hardcoded internal address (`10.0.0.5` / `10.0.0.1`) appearing in non-PII log lines. The regex layer has no concept of "private range, not customer-facing"; it flags any dotted-quad. This is a direct, measured demonstration of why the taxonomy in the chapter treats internal infrastructure IPs and external/customer IPs as different sensitivity tiers, rather than a taxonomy detail that just sounds reasonable on paper.

**2. PERSON recall depends almost entirely on name formatting, and the gap is enormous.** Broken down by format:
   - Space-separated names ("Timothy Wong"): **98.8% recall** (975/987)
   - Flattened username-style tokens with no whitespace ("donaldgarcia"): **5.9% recall** (98/1668)

   This is the single most important measured result for the detection section. General-purpose NER models expect natural sentence structure. Logs routinely flatten a person's identity into a token that doesn't look like a name at all, and the detector misses it in nineteen cases out of twenty. Any claim in the chapter that "NER catches what regex misses" needs this qualifier attached, because it's true for one name format and false for the other.

**3. The entropy fallback, as commonly described in the literature, does not pull its weight on structured log fields.** A threshold sweep (min length 12–20 chars, entropy threshold 3.3–4.2) found no operating point that gets meaningful unique recall without a high false-alarm rate: at threshold 3.3, it flags 34.8% of clean (non-PII) lines, and even at that permissive setting it only catches 142 of 6,199 gold PII spans (2.3%) that regex and NER both missed, mostly redundant re-detection of things already caught. The false alarms are dominated by fixed-vocabulary structured tokens like `EventID=4672`, which have moderate character diversity relative to length but carry no PII at all.

   Important caveat, stated plainly rather than buried: this synthetic dataset doesn't include the category entropy detection is actually built for: API keys, session tokens, opaque hashes. The weak result here is partly a property of what this dataset tests, not necessarily a verdict on entropy detection generally. The chapter should say this explicitly rather than present a flattering number that the data doesn't support, or damn the technique based on a dataset that wasn't built to exercise its actual use case.

## Anonymization and audit trail (added after the initial detection-only pass)

```bash
python src/pipeline.py --in data/synthetic_logs.jsonl --out output/anonymized.jsonl \
    --audit-out output/audit_log.jsonl --limit 500
```

Runs detection, routes each finding through the decision matrix (`anonymize.py`: PERSON/IP → pseudonymize, EMAIL/SSN/CREDIT_CARD/MRN → tokenize), and writes a signed audit event per action.

**A real bug turned up during testing, worth documenting rather than quietly fixing and moving on.** The first version of `anonymize.py` applied transforms directly to whatever spans the caller passed in. Regex and Presidio frequently detect the *same* PII instance independently (both have recognizers for EMAIL, SSN, CREDIT_CARD, IP), producing two overlapping spans for one real-world value. Applying a right-to-left string replacement against both spans corrupted the output: the second replacement operated on stale offsets from a string the first replacement had already changed, producing malformed tokens and, in some cases, would have broken JSON structure in CloudTrail lines. Fixed by adding `dedup_spans()` as a safeguard inside `anonymize.py` itself, not just as something callers are expected to do first, since the bug was caused by exactly that assumption failing.

**Verified, not just asserted:**
- Pseudonymization is correctly correlation-preserving: the same original IP produces the identical token everywhere it appears (checked across 471 distinct IPs, zero mismatches).
- Anonymized CloudTrail output is valid JSON in 100% of a spot-checked sample (167 entries); the dedup fix above was necessary for this to hold.
- Tokenize → detokenize round-trips exactly to the original string.
- Audit event signatures verify correctly on genuine events, correctly reject a tampered field, and correctly reject the wrong signing key.
- Anonymization itself is not the bottleneck: ~532,000 events/sec against ground-truth spans, versus ~113 events/sec for the NER detection step that feeds it. The cost in this pipeline lives entirely in detection, not in the anonymization actions themselves.

**A concrete example of the PERSON/flattened-username gap surviving the full pipeline, not just showing up in an aggregate recall number:** one CloudTrail entry's `targetUser` field contains the flattened name `donaldgarcia`. The detector missed it (consistent with the 5.9% recall on this format reported above), so it passed through the anonymizer completely unredacted, while the SSN in the same log line was correctly tokenized. This is the practical consequence of the earlier finding, not a separate issue: a real name sat unprotected in the final output of a pipeline that successfully protected the SSN three fields away.

## Correction: pseudonymization is not reversible (found and fixed after the initial write-up)

The first version of this README and the chapter text both described keyed HMAC pseudonymization as "reversible with the key." That's wrong: HMAC-SHA256 is a one-way function; there is no operation that recovers the original value from the token, with or without the key. What the key actually provides is determinism (same input → same token, so correlation across events survives) and the ability to verify a *candidate* value against a token, which is not the same thing as recovering an unknown one.

This matters beyond code correctness: for low-entropy fields like IP addresses (≈4.3 billion possible IPv4 values) and person names (an enumerable population), an attacker holding the key doesn't need to invert the hash; brute-forcing candidates against a fast hash is entirely tractable. The Article 29 Working Party's 2014 opinion on anonymization techniques identifies exactly this as a weakness of keyed-hash pseudonymization for guessable inputs. So pseudonymized IP/PERSON fields stay classified as personal data under GDPR not because a formal reversal mechanism exists, but because of this residual re-identification risk, which is a different, more specific reason than what was originally written.

`anonymize.py` and the chapter have both been corrected. Where an investigation genuinely needs the original value back, the pipeline routes to `tokenize()` instead, which uses an explicit stored mapping rather than relying on any property of a hash function.

## Service layer and Logstash integration

```bash
python src/service.py    # starts the HTTP wrapper on :8080
```

`src/service.py` wraps the tested `detect.py` / `anonymize.py` / `audit.py` modules behind a small Flask API (`POST /anonymize`, `GET /health`), so `logstash/redact-pipeline.conf` can call the *actual tested Python logic* via Logstash's `http` filter instead of a second, hand-maintained reimplementation in Ruby. This was a deliberate architecture change: an earlier draft reimplemented the HMAC/tokenization logic directly in Logstash Ruby filters, and that duplication is exactly how the overlapping-span bug above happened once already; two implementations of the same logic in two languages is two chances for them to drift apart.

Verified by actually running the service and sending it requests (not just confirming it starts):
- `/health` returns `{"status": "ok"}`.
- `POST /anonymize` against the CloudTrail line that originally exposed the overlapping-span bug returns correctly deduplicated, correctly tokenized/pseudonymized output, with tokens matching the ones produced by calling `anonymize.py` directly, confirming the service is a thin, faithful wrapper and not a third implementation with its own drift risk.
- A malformed request (missing `log` key) initially returned a silent `200` with an empty result instead of an error, because `dict.get("log", "")` masked the missing key. Fixed to return `400` with an explicit error message, and re-verified.

**What's real vs. what's still unverified in `logstash/redact-pipeline.conf`:** the service it calls has been run and tested directly, with output confirmed. The Logstash config itself has not been run against a live Logstash instance: no Docker/Java runtime was available in this development environment. It's written against the `http`, `clone`, and `split` filters' documented behavior, with explicit caveats left in the file's comments at the two points (the http filter's failure-tag name, and the clone filter's field-naming behavior) where plugin-version differences could matter. Confirm both against your installed Logstash version before relying on them in production.

## Taxonomy drift detection (Section 3.3)

```bash
python src/drift.py --baseline data/baseline.jsonl --current data/current.jsonl --threshold 0.05
```

Implements the weekly check Section 3.3 describes: track what fraction of each field's values contain Critical-tier PII, compare a current window against a baseline, and flag any field whose rate moved by more than the threshold. Scope is honest about its limits: this only works on log types with an identifiable field structure. `fields.py` extracts real fields from CloudTrail JSON (flattened to dotted paths) and from Windows Event's `key=value` format (with a regex tolerant of multi-word values like a person's name inside `TargetUserName=`). Syslog in this dataset is closer to free text with no reliable field boundary a generic parser could locate, so it's explicitly excluded rather than forced into a field model that doesn't fit it.

**Tested two ways, not one.** First, a no-false-positive check: the stable 10,000-entry corpus was split in half and one half compared against the other as if they were baseline and current. Result: zero fields flagged across all 26 distinct fields; a stable population correctly produces no noise. Second, a real simulated drift: in a copy of the "current" half, the `reason` field inside CloudTrail's `GetPatientRecord` events, which in the baseline is always the fixed string `"billing reconciliation"`, was changed to include a patient contact name in 77 of 132 occurrences (a partial rollout, not every call), representing exactly the scenario the chapter describes: an unrelated code change makes a field start carrying PII it never carried before, with nothing in the field's name signaling it. The drift check flagged exactly that field and no other: `cloudtrail.requestParameters.reason`, baseline rate 0.0% (n=110), current rate 56.1% (n=132), delta +56.1%. The 56.1% detected rate against a 58.3% actual injection rate is consistent with, not a new discrepancy from, the PERSON recall gap already measured in Section 4: a handful of the injected names weren't in space-separated format the NER layer catches reliably.

This is the concrete, tested version of what was previously a design claim in the chapter's discussion of taxonomy drift: a silent failure becomes a detected one, converted from something asserted to something demonstrated against an actual injected failure.

## Airflow DAG: actually run, not just written (Section 7)

```bash
pip install apache-airflow
export AIRFLOW_HOME=/path/to/airflow_home
mkdir -p $AIRFLOW_HOME/dags && cp dags/redact_weekly_validation.py src -r $AIRFLOW_HOME/dags/
airflow db migrate
airflow tasks test redact_weekly_validation sample_medium_confidence_hits
airflow tasks test redact_weekly_validation check_taxonomy_drift
airflow tasks test redact_weekly_validation rotate_pseudonymization_key
```

Unlike the Logstash config, this was actually executed, not just written against documented behavior. Apache Airflow 3.3.0 installed cleanly and every task ran through the real `airflow tasks test` runner against real data, each reaching `Task instance in success state`:

- `check_taxonomy_drift` returned the correct real result on the stable baseline/current split (`fields_flagged: 0`), matching what running `drift.py` directly produces.
- `sample_medium_confidence_hits` correctly pulled a random, sized sample (12 of 249 entries with findings, at a 5% rate) from the real pipeline output, including one entry showing the `donaldgarcia`-style flattened-name gap still visible in the sample, exactly the kind of case a human reviewer pulling this sample would need to catch.
- `rotate_pseudonymization_key` correctly generated a new key, retired the old one to a timestamped file, and a second rotation confirmed the key actually changes between runs rather than silently returning the same value.

One real API correction found in the process: the chapter's original DAG skeleton used Airflow 2.x-style imports (`from airflow import DAG`, `from airflow.operators.python import PythonOperator`). Airflow 3.3.0 still accepts these through a backward-compatibility shim, but importing them emits a `DeprecatedImportWarning`, confirmed by actually triggering both import forms and observing the warning on the old one. The DAG shipped here (`dags/redact_weekly_validation.py`) uses the current API (`airflow.sdk.DAG`, `airflow.providers.standard.operators.python.PythonOperator`) instead, and the task logic itself lives in `src/airflow_tasks.py` as plain, independently testable functions. The DAG file wires them together, it doesn't reimplement them, for the same reason `service.py` wraps rather than duplicates the detection logic.

## Docker Compose stack

```bash
python src/export_raw_logs.py                 # splits the JSONL corpus into per-source raw files
echo "REDACT_PSEUDO_KEY=$(openssl rand -hex 32)" > .env
echo "REDACT_AUDIT_KEY=$(openssl rand -hex 32)" >> .env
docker compose up --build
```

Ties together OpenSearch, `redact-service`, and Logstash. Deliberately does **not** include Airflow: the DAG runs as a separate weekly batch job against this stack's output (see `requirements-airflow.txt`), not as a live pipeline component, and bundling a full Airflow deployment into this compose file would conflate two different operational concerns.

**Honest verification status, same pattern as the Logstash config it wraps:** no Docker runtime was available in this development environment, so nothing here has been built or run. Every configuration choice was checked against current OpenSearch documentation rather than asserted from memory, specifically: `DISABLE_SECURITY_PLUGIN=true` for the single-node test setup (OpenSearch's own quickstart guide), and installing `logstash-output-opensearch` via a custom Dockerfile on top of the standard Logstash image (OpenSearch's documented Logstash integration approach, rather than the older `opensearchproject/logstash-oss-with-opensearch-output-plugin` image, which is pinned to Logstash 7.16.2 and doesn't track current releases).

`opensearch-dashboards` is intentionally left out, not forgotten. OpenSearch's own documentation states that a security-disabled Dashboards image isn't something you pull; it's something you build locally after modifying `opensearch_dashboards.yml` and removing the security plugin (`docker build --tag=opensearch-dashboards-no-security .`). Bundling that into this file as if it were a one-line service addition would have been the same mistake as the earlier Logstash `on_error` parameter I wasn't sure existed: asserting a specific, checkable thing without having verified it.

Of the three services, `redact-service` (the `Dockerfile` at the repo root) is the lowest-risk piece: it's a standard Python/Flask container with no version-sensitive plugin behavior, and the code it runs has already been tested extensively outside Docker throughout this README. OpenSearch and Logstash's specific configurations remain unverified in the way everything under "believed correct but unexecuted" in the limitations section below is unverified.

## Known limitations of this proof of concept

- Drift detection (`drift.py`) only covers CloudTrail and Windows Event fields; syslog has no field-level coverage at all in this prototype, by design (see `fields.py`'s docstring). A production deployment logging significant PII through syslog-formatted sources would need a source-specific field extractor added before drift detection covers it.
- The token store (`TokenStore` in `anonymize.py`) is a flat JSON file. That is enough to demonstrate and test the tokenize/detokenize round trip honestly, but it is not an acceptable production secrets store: no access control, no encryption at rest, no key rotation. A production deployment needs HashiCorp Vault or equivalent, as the chapter's architecture section describes.
- Detection runs on whole log lines rather than per structured field, so the "tiered" NER strategy is gated at the document level, not the field level. The chapter's Section 8 already predicted this exact failure mode: the tiered condition's PERSON recall (0.127) is far worse than the naive condition's (0.404) specifically because skipping NER on any line where regex found something (like an SSN) also skips it for a PERSON entity sitting in the same line. A field-level implementation would not have this problem to the same degree.
- No Logstash or OpenSearch component was actually run: no Docker/Java runtime was available in this development environment. The pipeline configs shown elsewhere in the chapter for those two are believed correct but unexecuted; they should be labeled as such until a Docker-capable environment is used to validate them. Airflow is the exception: it installed cleanly as a pure Python package and every DAG task was run for real (see above), which the other two infrastructure components could not be, given the tooling available here.
- Single-threaded, 1-vCPU throughput numbers are not representative of a horizontally scaled production deployment.
