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

The synthetic corpus above is a close-to-best-case scenario for any detector, since its own generation templates are the patterns the regex layer was designed against. `validation/real_data/` answers the harder question directly: tested against five real, independently collected, unmodified log datasets (not written by anyone here), the same format-sensitivity gap shows up every time. **Precision is not consistently lower on real data than synthetic** — an earlier version of this claim was based on a measurement bug (`validation/real_data/inject_and_evaluate.py` was missing a dedup step, double-counting agreeing regex+NER detections as false positives; see Bug 10 in `BUGS_AND_FIXES.md`). Corrected: three of five real datasets now measure *higher* precision than the synthetic baseline, and the two that don't (Zookeeper, Thunderbird) are explained by the same internal-IP false-positive pattern documented in Finding 1 below, not a synthetic-to-real generalization gap. The flattened-username layer's recall gain also replicates on real log text. See `validation/real_data/README.md` for the corrected numbers, what's actually being tested, and why eleven of the sixteen candidate datasets were excluded rather than forced into producing a number.

## What was actually measured (single run, 10,000 synthetic entries, 3,002 containing PII)

Hardware: 1 vCPU, 4 GB RAM, no GPU. Numbers below are single-threaded Python throughput, **not** a benchmark of a production Logstash/OpenSearch deployment. That architecture is described in the chapter and, unlike an earlier version of this README claimed, has since been built and run end-to-end via Docker Compose (see "Docker Compose stack" below and `BUGS_AND_FIXES.md`) — but it has been run at demo scale (10,000 lines, a single node, a single shard per index), not load-tested at production volume, so these throughput numbers still shouldn't be read as a production benchmark.

**Post-Bug-9-fix note:** the corpus was regenerated after fixing `generate_logs.py`'s ground-truth labeling gap (Bug 9, see the Layer 4 section below) — same 10,000 entries, but 338 additional gold PERSON spans that were previously never labeled. All four rows below, including the two NER-dependent ones, are re-verified against the regenerated corpus (run locally by the user, since this repo's dev sandbox has no route to the spaCy model download).

| Condition | Micro-avg precision | Micro-avg recall | Micro-avg F1 | Throughput |
|---|---|---|---|---|
| Regex only | 0.574 | 0.542 | 0.558 | ~68,700 events/sec |
| Regex + NER, tiered (NER skipped when regex already found something in the line) | 0.577 | 0.594 | 0.585 | ~286 events/sec |
| Regex + NER, naive (NER runs on every line) | 0.588 | 0.706 | 0.642 | ~135 events/sec |
| Regex + NER (naive) + flattened-username layer | 0.633 | 0.854 | 0.727 | ~128 events/sec |

Recall on the "regex only" and NER rows dropped slightly versus the pre-Bug-9-fix numbers (e.g. naive NER recall 0.745 → 0.706) — this is the corrected ground truth working as intended, not a regression: 338 previously-unlabeled PERSON spans are now counted, and none of the pre-existing layers found them (that's exactly Layer 4's reason for existing; see below). The fourth row is new: it's the full default ensemble as `detect_all()` actually runs it today (`use_flattened=True` by default), and it's the clearest single number in this table — recall jumps from 0.706 to 0.854 by adding one dictionary-based layer, at a throughput cost within noise of the NER step it rides alongside (128 vs. 135 events/sec; NER, not the new layer, is the bottleneck).

Per-entity-type detail (naive condition, regex+NER only, no flattened layer):

| Type | TP | FP | FN | Precision | Recall | F1 |
|---|---|---|---|---|---|---|
| CREDIT_CARD | 237 | 0 | 0 | 1.000 | 1.000 | 1.000 |
| EMAIL | 488 | 0 | 0 | 1.000 | 1.000 | 1.000 |
| SSN | 250 | 0 | 0 | 1.000 | 1.000 | 1.000 |
| MRN | 242 | 0 | 0 | 1.000 | 1.000 | 1.000 (custom Presidio pattern recognizer) |
| IP | 2327 | 2626 | 0 | 0.470 | 1.000 | 0.639 |
| PERSON | 1074 | 612 | 1919 | 0.637 | 0.359 | 0.459 |

Per-entity-type detail (naive condition + flattened-username layer, the actual default ensemble):

| Type | TP | FP | FN | Precision | Recall | F1 |
|---|---|---|---|---|---|---|
| CREDIT_CARD | 237 | 0 | 0 | 1.000 | 1.000 | 1.000 |
| EMAIL | 488 | 0 | 0 | 1.000 | 1.000 | 1.000 |
| SSN | 250 | 0 | 0 | 1.000 | 1.000 | 1.000 |
| MRN | 242 | 0 | 0 | 1.000 | 1.000 | 1.000 (custom Presidio pattern recognizer) |
| IP | 2327 | 2626 | 0 | 0.470 | 1.000 | 0.639 |
| PERSON | 2038 | 612 | 955 | 0.769 | 0.681 | 0.722 |

### Three findings worth building the chapter around, because they're real

**1. IP precision is bad for a specific, traceable reason, not a vague one.** All 2,626 IP false positives trace back to a hardcoded internal address (`10.0.0.5` / `10.0.0.1`) appearing in non-PII log lines. The regex layer has no concept of "private range, not customer-facing"; it flags any dotted-quad. This is a direct, measured demonstration of why the taxonomy in the chapter treats internal infrastructure IPs and external/customer IPs as different sensitivity tiers, rather than a taxonomy detail that just sounds reasonable on paper.

**2. PERSON recall depends almost entirely on name formatting, and the gap is enormous.** Broken down by format:
   - Space-separated names ("Timothy Wong"): **98.8% recall** (975/987)
   - Flattened username-style tokens with no whitespace ("donaldgarcia"): **4.9% recall** (99/2,006)

   Re-verified 2026-08-07 against the post-Bug-9-fix corpus (`validation/breakdown_person_format.py`), run by the user directly since this repo's dev sandbox can't reach the spaCy model download. The spaced-name figure is unchanged; the flattened-name figure moved from the originally reported 5.9% (98/1,668) to 4.9% (99/2,006) on the corrected, larger denominator — the underlying gap (NER almost completely misses this format) is the same finding, the number is just now measured against accurate ground truth.

   This is the single most important measured result for the detection section. General-purpose NER models expect natural sentence structure. Logs routinely flatten a person's identity into a token that doesn't look like a name at all, and the detector misses it in nineteen cases out of twenty. Any claim in the chapter that "NER catches what regex misses" needs this qualifier attached, because it's true for one name format and false for the other.

**3. The entropy fallback, as commonly described in the literature, does not pull its weight on structured log fields.** A threshold sweep (min length 12–20 chars, entropy threshold 3.3–4.2) found no operating point that gets meaningful unique recall without a high false-alarm rate: at threshold 3.3, it flags 34.8% of clean (non-PII) lines, and even at that permissive setting it only catches 142 of 6,199 gold PII spans (2.3%) that regex and NER both missed, mostly redundant re-detection of things already caught. The false alarms are dominated by fixed-vocabulary structured tokens like `EventID=4672`, which have moderate character diversity relative to length but carry no PII at all.

   Important caveat, stated plainly rather than buried: this synthetic dataset doesn't include the category entropy detection is actually built for: API keys, session tokens, opaque hashes. The weak result here is partly a property of what this dataset tests, not necessarily a verdict on entropy detection generally. The chapter should say this explicitly rather than present a flattering number that the data doesn't support, or damn the technique based on a dataset that wasn't built to exercise its actual use case.

   **Post-Bug-9-fix note:** `src/analyze_entropy.py` was rerun against the regenerated corpus at the default threshold (min length 12, entropy 3.3) and the false-alarm rate lands close to the original (33.9% of clean lines flagged vs. the 34.8% reported above), consistent with Bug 9 not affecting entropy scoring (the fix only added gold spans, it didn't change which lines are clean). The exact "142/6,199 unique recall" figure above used a stricter methodology (novel catches only, excluding redundant re-detection of spans regex/NER already caught) that this rerun didn't reproduce — that specific number is flagged as needing a rerun with the original script/parameters, not silently restated. Item 11 in `ROADMAP.md` (a dedicated API-key/token/hash test corpus) remains the more useful next step regardless.

## Layer 4: closing part of the flattened-username gap (`src/flattened_names.py`)

Finding 2 above (4.9% recall on flattened names like `donaldgarcia`) is the single most consequential result in this project, so it got a dedicated fourth detection layer rather than being left as a documented limitation: dictionary-based compound segmentation, trying every split point in a token to see if it cleanly divides into `<first name><last name>`. This treats the problem as compound-word segmentation, not sentence-level NER, which is the actual shape of the problem NER structurally cannot solve here.

**Measured standalone (this layer alone, not yet combined with NER in an end-to-end run — see caveat below), against the same 10,000-entry corpus, regenerated after the Bug 9 fix below:**

| Metric | Before (NER alone, pre-Bug-9-fix corpus) | After (this layer alone, post-Bug-9-fix corpus) |
|---|---|---|
| Flattened-format PERSON recall | 4.9% (99/2,006) | **50.3% (1,010/2,006)** |
| False-trigger rate on space-separated names | n/a | 0.3% (3/987) |
| Precision | n/a | **100% exactly, 0 false positives, no caveat needed** |

Getting to that precision number required finding and fixing a real false-positive source first: tokens matching a name pattern immediately followed by `@` (email local-parts, since `fake.email()` is itself name-derived) were being double-flagged as PERSON on top of the correct EMAIL span. Fixed with a one-line exclusion once found empirically, not guessed in advance.

**Bug 9 is now fixed** (`generate_logs.py`'s `render()` previously located only the *first* occurrence of a repeated slot value via `text.find()`; the syslog `sudo` template uses `{PERSON_name_flat}` twice, so the second, equally-real occurrence never got a gold span). `render()` now uses `re.finditer()` to emit one gold span per occurrence, the canonical 10,000-entry corpus has been regenerated (same 10,000 entries, gold PII span count 6,199 → 6,537, exactly +338 — one new span per affected `sudo` entry, confirmed by direct count), and the flattened-layer numbers above are re-measured against the corrected corpus: recall holds at the same 50.3% on a larger, correct denominator (1,010/2,006 vs. the earlier 839/1,668), and the 171 apparent false positives that motivated finding this bug are now **confirmed to be exactly 0** — they were entirely a ground-truth labeling artifact, not a real detector weakness, as the original writeup suspected but hadn't yet proven.

**Now measured combined with the full ensemble, not just standalone:** `evaluate.py`'s fourth condition (regex + NER + this layer) was run end-to-end against the regenerated corpus (see the table above): PERSON recall goes from 0.359 (regex+NER alone) to 0.681 with this layer added, at unchanged precision on every other type and a throughput cost within noise of the NER step itself (128 vs. 135 events/sec). `validate.py`'s full 18-check suite also passed clean (18/18) against the regenerated corpus with this layer present as part of the default ensemble.

**Known limitation, stated in the code and repeated here:** the name dictionary is Faker's own `first_names`/`last_names` list — the same generator that built this corpus. That makes the 50.3% number optimistic in a way that won't transfer 1:1 to a real production user population with names outside that list. **Validated against the real Loghub datasets (2026-08-07):** on real, unmodified log text (not just this project's own synthetic templates), the same layer recovers similar recall gains — OpenSSH 0.0%→45.5%, Linux 3.4%→50.0% — at effectively unchanged precision. This confirms the gain isn't an artifact of the synthetic corpus's own templates, but it does **not** yet resolve the dictionary-matches-itself concern, since the injected names in that test are still Faker-generated. See `validation/real_data/README.md` for the full numbers and what a stronger test (a non-Faker name-frequency source) would still need to look like.

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

**A concrete example of the PERSON/flattened-username gap surviving the full pipeline, not just showing up in an aggregate recall number:** one CloudTrail entry's `targetUser` field contains the flattened name `donaldgarcia`. The detector missed it (consistent with the 4.9% recall on this format reported above), so it passed through the anonymizer completely unredacted, while the SSN in the same log line was correctly tokenized. This is the practical consequence of the earlier finding, not a separate issue: a real name sat unprotected in the final output of a pipeline that successfully protected the SSN three fields away.

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

**What's real vs. what's still unverified in `logstash/redact-pipeline.conf`:** the service it calls has been run and tested directly, with output confirmed. The Logstash config itself has since been run against a live Logstash 8.15.0 instance (a Docker-capable environment became available after the section above was originally written), repeatedly, end to end, against all 10,000 synthetic lines at once. That testing found and fixed seven real bugs — including two that silently destroyed or misrouted the majority of events while returning `200 OK` on every request, with no crash or error to signal anything was wrong. Full details, root causes, and fixes are in `BUGS_AND_FIXES.md`. Notably, the original design used the `clone` and `split` filters for the audit-trail fan-out; `clone` turned out not to deep-copy the `[tags]` array reliably under this pipeline's `ecs_compatibility => v8` setting, and was replaced with a hand-rolled `ruby` filter using `event.clone` instead (see Bug 5 in that file). Confirm both filters' current documented behavior against your own installed Logstash version before relying on any of this in production; this was tested against one specific version, on one machine, not across the plugin's version history.

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

**Honest verification status, same pattern as the Logstash config it wraps:** this has since been built and run, repeatedly, in a Docker-capable environment (a superseded version of this paragraph said otherwise, when none was available yet). Every configuration choice was still checked against current OpenSearch documentation rather than asserted from memory before being tested, specifically: `DISABLE_SECURITY_PLUGIN=true` for the single-node test setup (OpenSearch's own quickstart guide), and installing `logstash-output-opensearch` via a custom Dockerfile on top of the standard Logstash image (OpenSearch's documented Logstash integration approach, rather than the older `opensearchproject/logstash-oss-with-opensearch-output-plugin` image, which is pinned to Logstash 7.16.2 and doesn't track current releases). Live testing surfaced real problems that documentation review alone did not: an OpenSearch startup race that caused duplicate writes, a Flask dev-server concurrency limit that caused false quarantines, and the document-ID and audit-routing bugs described in `BUGS_AND_FIXES.md`. All were found and fixed the same way the bugs elsewhere in this README were: by running the thing and checking the actual output against what was expected, not by re-reading the config more carefully.

`opensearch-dashboards` is intentionally left out, not forgotten. OpenSearch's own documentation states that a security-disabled Dashboards image isn't something you pull; it's something you build locally after modifying `opensearch_dashboards.yml` and removing the security plugin (`docker build --tag=opensearch-dashboards-no-security .`). Bundling that into this file as if it were a one-line service addition would have been the same mistake as the earlier Logstash `on_error` parameter I wasn't sure existed: asserting a specific, checkable thing without having verified it.

Of the three services, `redact-service` (the `Dockerfile` at the repo root) was always the lowest-risk piece: it's a standard Python/Flask container with no version-sensitive plugin behavior, and the code it runs had already been tested extensively outside Docker throughout this README before Docker was involved at all. OpenSearch and Logstash's specific configurations have since been verified by actually running the full stack together — see "Docker Compose stack" above and `BUGS_AND_FIXES.md` for what that testing found.

## Known limitations of this proof of concept

- Drift detection (`drift.py`) only covers CloudTrail and Windows Event fields; syslog has no field-level coverage at all in this prototype, by design (see `fields.py`'s docstring). A production deployment logging significant PII through syslog-formatted sources would need a source-specific field extractor added before drift detection covers it.
- The token store (`TokenStore` in `anonymize.py`) defaults to a flat JSON file (`FileStorageProvider`). That is enough to demonstrate and test the tokenize/detokenize round trip honestly, but it is not an acceptable production secrets store on its own: no access control, no encryption at rest, no key rotation, and (before 2026-08-07) no way to swap in a real backend without rewriting `TokenStore` itself. **This has since changed:** `TokenStore` now delegates persistence to a `StorageProvider` interface (`FileStorageProvider` for local dev/testing, still the default everywhere in this repo; `RedisStorageProvider` for a shared production backend). `RedisStorageProvider` is written against the documented `redis-py` API but has **not yet been run against a live Redis instance** in this project's own testing — see its docstring in `src/anonymize.py` for exactly what still needs verifying (the tokenize/detokenize round trip and the same concurrent-access test that originally found the TokenStore race condition, Bug 6) before it's trusted in production. A HashiCorp Vault provider is not yet implemented; the interface is designed to make adding one a matter of implementing `load()`/`save()`, not touching `TokenStore`'s business logic. It also had a real concurrency bug, since fixed: dict read-modify-write operations with no synchronization produced `RuntimeError: dictionary changed size during iteration` under concurrent access from Logstash's multiple pipeline workers; now guarded with `threading.Lock()` (confirmed under a full Docker Compose run, see Bug 6 in `BUGS_AND_FIXES.md`) — though that lock only protects one process's in-memory dicts, not concurrent writes across multiple `redact-service` replicas sharing one Redis backend, which is a different guarantee `RedisStorageProvider`'s own atomicity (not this lock) would need to provide.
- Detection runs on whole log lines rather than per structured field, so the "tiered" NER strategy is gated at the document level, not the field level. The chapter's Section 8 already predicted this exact failure mode: the tiered condition's PERSON recall (0.127) is far worse than the naive condition's (0.404) specifically because skipping NER on any line where regex found something (like an SSN) also skips it for a PERSON entity sitting in the same line. A field-level implementation would not have this problem to the same degree.
- The Logstash and OpenSearch components, previously listed here as unexecuted for lack of a Docker-capable environment, have since been built and run end-to-end (10,000 synthetic lines, single-node OpenSearch, all three output indices reconciling exactly: 9,984 anonymized + 16 quarantined = 10,000, with a separate audit-trail index holding one signed record per detected PII span). That testing found and fixed seven real bugs, several of which failed silently — no crash, no error, `200 OK` on every request — while destroying or misrouting the majority of events. Full root-cause writeups are in `BUGS_AND_FIXES.md`; treat that file as the current source of truth on this stack's known-fixed and known-remaining issues, not this bullet list. What's still a genuine limitation after that testing: it ran on one Docker Desktop machine, at demo scale, against a single-node/single-shard OpenSearch instance — none of that has been load-tested or run against a multi-node, production-scale deployment. **The Flask-dev-server limitation itself is now fixed, not just documented as a known gap:** `Dockerfile`'s `CMD` runs `redact-service` behind gunicorn (`--workers $(nproc)`, matched to available CPU cores per the standard sizing guidance for CPU-bound work) instead of `app.run(...)`. This has been smoke-tested (a stub Flask app booted correctly under the exact same `--chdir src ... service:app` invocation, workers matching `nproc`, `/health` responding) but **not yet re-run through the full Docker Compose stack** the way the rest of this section's numbers were — the confirming Bug 6 run above predates this change. Worth a follow-up rerun to confirm gunicorn's multi-worker model plays correctly with the per-worker model-warmup fix (each worker independently loads its own copy of the spaCy model at startup, see `src/service.py` and `Dockerfile`'s own comments for why `--preload` was deliberately not used) before treating this as fully verified the way Bug 6 itself now is.
- Single-threaded, 1-vCPU throughput numbers are not representative of a horizontally scaled production deployment.
