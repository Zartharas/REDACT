# Validation review: three new technical contributions

Raw numbers and reasoning. Sections 1-4 are the evidence; the Decisions section at the end resolves the four open questions from the prior pass.

## 1. Re-identification risk model (HMAC pseudonymization, Massey guessing entropy)

Built on real US population data (2010 Census surnames, SSA given names 2016-2025), computing an adversary's guessing-entropy curve over the (given name, surname) space under HMAC pseudonymization.

- Most-likely single pair carries 0.0054% of total probability mass.
- P(adversary succeeds within M guesses): M=1 -> 0.0054%, M=1,000 -> 1.47%, M=100,000 -> 16.57%, M=10,000,000 -> 61.63%.
- Key-rotation guidance (max safe epoch length λmax, given adversary query rate R and target risk ε): at ε=1% and R=1,000 queries/day, λmax ≈ 0.6 days; at ε=5%, λmax ≈ 7.7 days.

### New this pass: Monte Carlo cross-check of the closed-form implementation
The closed-form P_success(M) is computed via binary search + prefix sums over the real marginal distributions — a numerically involved implementation worth verifying independently, the same way the coverage model's large-N check verified its formula. Method: recover the probability threshold τ* the binary search converges to for a given M, then independently estimate the same mass by directly weighted-sampling (given, surname) pairs from the real marginals and checking what fraction clear τ*.

| M | closed-form | Monte Carlo (n=2M) | gap (pts) | within 95% CI? |
|---|---|---|---|---|
| 100 | 0.2784% | 0.2796% | +0.0012 | yes |
| 10,000 | 5.7463% | 5.7442% | -0.0021 | yes |
| 1,000,000 | 36.2269% | 36.1544% | -0.0726 | **no** (flagged) |

The M=1,000,000 case fell just outside its 95% CI on the first run. Investigated before trusting either number: (1) verified the recovered τ* reproduces the closed-form mass exactly bit-for-bit, ruling out a threshold-recovery bug; (2) reran the Monte Carlo check at M=1,000,000 with 3,000,000 samples across 4 independent seeds — gaps were -0.019, -0.010, -0.030, -0.045 points, all within their individual CIs, and the pooled mean gap (-0.026 pts) is within the pooled CI (±0.027 pts). **Conclusion: no systematic bias — the single flagged result was expected sampling noise under multiple comparisons (testing 3 M values without correction gives roughly a 1-in-7 chance at least one exceeds its 95% CI by chance).** The implementation is validated.

### New this pass: SSA given-name year-window sensitivity
Same structural question as the coverage model's year-window finding — the given-name distribution depends on which SSA birth-year window is used; the surname distribution (full 2010 Census) does not have this parameter.

| window | P_success(M=1,000) | P_success(M=100,000) | P_success(M=10,000,000) |
|---|---|---|---|
| last 5 years (2021-2025) | 1.4250% | 16.2730% | 61.2903% |
| last 10 years (2016-2025) — main result | 1.4682% | 16.5669% | 61.6250% |
| last 20 years (2006-2025) | 1.5287% | 16.8748% | 61.7693% |
| full range (1880-2025) | 2.4611% | 21.0954% | 68.1609% |

| window | M* at ε=5% | λmax @ R=1,000/day |
|---|---|---|
| last 5 years | 8,054 | 8.1 days |
| last 10 years — main result | 7,686 | 7.7 days |
| last 20 years | 7,279 | 7.3 days |
| full range | 3,760 | **3.8 days** |

The P_success(M) curve itself is only moderately sensitive to the window (mid-range M values shift ~10-30% relatively). But the **operationally actionable number — the key-rotation guidance — is sensitive by more than 2x** (7.7 days down to 3.8 days between the main-result window and the full-historical window). A practitioner who deploys against a population skewed toward older name-registration cohorts (e.g., a legacy HR/identity system spanning decades) and uses the paper's flat "~7.7 days" guidance would be over-confident by more than a factor of 2.

## 2. Statistical drift-detection framework (two-proportion z-test + CUSUM vs. the current naive 5-point threshold)

### Original Monte Carlo (parametric Bernoulli simulation, previously reported)
- Naive rule's false-positive rate is uncontrolled: 0%–76% depending on baseline rate and sample size (66–76% at n=20 across several baselines).
- Two-proportion z-test holds FPR near the nominal 5% throughout, by construction.
- At the paper's own two real (p0, p1, n) cases: sshd.user naive flags 57.1% vs z-test 14.8%; cloudtrail.reason both flag ~100% (a genuine large shift, both rules correctly fire).
- CUSUM: ARL0 (false-alarm spacing) ranges 954–3,574 steps across threshold h=2–10; detection delay after a true shift ranges 177–1,330 steps; h=4 is a reasonable operating point (ARL0≈2,331, delay≈560, 93.2% detection rate).

### New this pass: real-carrier-text injection stress test (not purely parametric)
Ran on two independent real datasets already in the repo (OpenSSH_2k.log, Linux_2k.log, Loghub), extracting the real `sshd.user` field values and injecting synthetic flattened usernames at controlled rates, then applying both the naive rule and the z-test across 1,000 repeated random splits per condition.

**Important methodology correction caught in this pass:** the first version of this script injected `fake.name()` ("John Smith", space-separated), matching how the paper's own `validate.py` Section 5 test injects into CloudTrail's free-text field. Verified directly that this scores **zero** hits under the regex + flattened-name layer (10/10 fake names → no detection), because that layer only matches concatenated, no-space tokens — catching space-separated names is the NER layer's job, and NER is network-blocked in this sandbox. Using `fake.name()` here would have silently made every "injected" record undetectable and invalidated the whole test without any visible error. Fixed by injecting a concatenated flattened-style username instead (`firstlast`), which is also the semantically correct shape for this field — real usernames in production logs are single tokens, not "First Last".

Results (real baseline hit rate on both datasets: 0/1016 and 0/372, i.e. genuinely 0% before any injection, consistent with finding #4 below):

| injection rate (both sides) | naive FPR | z-test FPR |
|---|---|---|
| 0% | 0.0% | 0.0% |
| 10% | 17.0–18.0% | 4.6–5.3% |
| 30% | 37.8–38.7% | 4.1–5.2% |
| 50% | 42.4–42.7% | 5.6–6.3% |

| current-side rate (baseline fixed at 0%) | naive power | z-test power |
|---|---|---|
| 5% | 47.0–49.6% | 88.4–90.4% |
| 10% | 97.8–98.5% | 99.8–100.0% |
| 20%+ | 100% | 100% |

This independently reproduces the earlier parametric finding — naive rule's FPR is uncontrolled and climbs with base rate, z-test holds near 5% — using two additional real datasets and real carrier text rather than only simulated proportions. Consistent across both datasets to within ~1 point.

## 3. Predictive coverage model (flattened-name dictionary layer)

### Original result (previously reported)
Closed-form independence-assumption formula: predicted recall 14.65% vs. measured real-population recall 15.25% (305/2,000, Census+SSA weighted draws) — 0.60 percentage point absolute error.

### New this pass: large-N validation (isolating model bias from sampling noise)
Direct weighted-sampling simulation (no independence-formula shortcut) at n=1,000,000: simulated P(role-compatible)=14.7215% vs. closed-form 14.6938%, a gap of +0.028 points — well inside the 95% CI (±0.069 points) at that sample size. **Conclusion: the original 0.60-point gap was almost entirely small-sample noise in the 2,000-draw real test, not systematic bias in the independence assumption.** The closed-form model is validated.

### New this pass: SSA year-window sensitivity (previously undocumented finding)
The model's prediction is highly sensitive to which SSA birth-year window is used to build the given-name frequency distribution:

| SSA window | P(given ∈ FIRST_NAMES) | P(role-compatible) |
|---|---|---|
| last 5 years (2021–2025) | 28.41% | 13.41% |
| last 10 years (2016–2025) — used in main result | 31.28% | 14.69% |
| last 20 years (2006–2025) | 38.44% | 17.87% |
| full range (1880–2025) | 67.42% | 30.87% |

This is a genuinely new finding, not previously in the paper. It means the reported 15.25%/14.65% figures are specific to a "recent names" population assumption (last 10 years), and the true figure could be more than double that (up to ~31%) if the deployment population skews toward older recorded names (e.g., legacy HR/identity systems with decades of birth-year given names) rather than recently-registered individuals. This needs an explicit decision: whether to report a single point estimate, a sensitivity range, or add population-assumption caveats to the model's scope.

## 4. Real production-log baseline check (new this pass, cross-dataset)

Extended the existing CloudTrail-only baseline check (0/2000 fields showed nonzero critical hits) to two more independent real datasets: OpenSSH_2k.log and Linux_2k.log (Loghub), using the same RFC3164 header-stripping the paper's own `inject_and_evaluate.py` already uses.

- All 9 OpenSSH fields and all 8 Linux fields with ≥40 real observations: 0.00% critical-hit rate.
- 60 repeated random 50/50 splits across both datasets: 0/60 incorrectly flagged drift — but this is a trivial result (0% vs 0% can never cross any threshold), not a meaningful stress test.
- **Conclusion, now confirmed on 3 independent real datasets rather than 1:** unmodified production infrastructure logs carry ~0% Critical-tier PII by construction. This validates (rather than undermines) the paper's own existing design choice to use synthetic injection for drift-detection evaluation — raw real logs alone cannot exercise false-positive behavior at all. Worth stating explicitly in the paper as a cross-validated methodological justification, not just an assumption.

## 5. Broadened real-dataset coverage for the drift-detection stress test (new this pass)

You asked whether the drift-detection real-data test was limited, and whether we could get more datasets. It was: the earlier pass only used 2 of the 7 real datasets already validated elsewhere in this project (paper Section 5.7), and used a custom injection routine instead of the project's own already-reviewed one. Fixed both.

**Reused the project's own established corpus builders** (`inject_and_evaluate.py`, the harness behind the paper's existing Section 5.7 real-data validation) instead of my earlier ad hoc version — this extends real-dataset coverage for the drift framework from 2 to 5 independent real sources: OpenSSH, Linux, Thunderbird (via `build_user_field_corpus`), real Windows Security-Auditing samples (`build_windows_event_corpus`, 33 genuine captured events from a lab domain environment, source: github.com/d4rk-d4nph3/Windows-Event-Samples), and real flaws.cloud CloudTrail events (`build_cloudtrail_corpus`). OpenStack/Zookeeper are correctly excluded — their real ground truth is IP-only, and `drift.py`'s own CRITICAL_TYPES set excludes IP, so they can't inform this specific test (this mirrors the paper's own existing exclusion logic, not a new gap).

**Disclosed limitation carried over:** the project's own injection methodology mixes flat usernames and full "First Last" names 50/50. Without NER (network-blocked in this sandbox), only the flat half is detectable by the regex+flattened layer used here — so every hit rate and power number below is a genuine underestimate of what the full production pipeline (regex + flattened + NER) would see. Flagged explicitly rather than silently reported as if it were the full picture.

Results (naive vs. z-test FPR/power, n=132 per split, 1,000 trials):

| dataset | baseline hit rate | z-test FPR (0% vs 0%) | z-test power @ 50% injection |
|---|---|---|---|
| OpenSSH | 0.00% | 0.0% | 36.1% |
| Linux | 0.10% | 0.0% | 9.0% |
| Thunderbird | 0.00% | 0.0% | 0.1% |
| windows_event | 0.00% | 0.0% | 96.9% |
| cloudtrail | 1.25% | 2.9% | 76.7% |

Power varies a lot by dataset — expected, since it's driven by how often each dataset's injection field actually lands somewhere the flat-username-only detector can see (Thunderbird's user-field injection point is comparatively rare in that dataset's structure, windows_event's is comparatively common). This variability is itself informative: it's a real illustration of why drift sensitivity is deployment-specific, not a fixed property of the algorithm.

**Genuinely new finding, found only by broadening to whole-line real-JSON scanning:** raw, unmodified CloudTrail shows a nonzero 1.25% baseline critical-hit rate (25/2000), not the 0% found in the earlier per-field-extraction check. Traced it: the CREDIT_CARD regex's Luhn-check safeguard (added earlier in this project specifically to fix a false-positive collision with AWS account IDs and Zookeeper thread IDs — see `detect.py`'s own documented history) reduces but does not eliminate false positives on other incidental long digit-strings. CloudTrail's real `startTime`/`endTime` fields are 13-digit millisecond epoch timestamps; 25 of 292 such values in this dataset (8.6%) happen to be Luhn-valid, close to the ~10% baseline rate expected for the Luhn check applied to effectively arbitrary digit strings of matching length. **This is not a defeat of the Luhn mitigation** — it's the expected residual false-positive rate the mitigation was always going to leave, now quantified on real data rather than left as a theoretical possibility. Worth stating explicitly in the paper: Luhn checking reduces the false-positive surface from "any 12-19 digit number" to "roughly 1 in 10 of them," not to zero, and any numeric field with enough independent long-digit values (timestamps, sequence numbers, session IDs) will produce some residual false positives at scale.

### On getting more datasets beyond what's already here

- **What's already real and already used:** 7 independent real datasets across this project (OpenSSH, Linux, Thunderbird, OpenStack, Zookeeper, windows_event, cloudtrail) — all now exercised by the drift-detection stress test where applicable, in addition to their existing role validating detection recall/precision in Section 5.7.
- **The one genuine, well-documented real gap:** PHI/MRN detection has zero real ground truth anywhere in this project. The fix (n2c2 2014 de-identification corpus, the actual gold-standard benchmark for this) is already scoped in this repo's own `PHI_DATASET_ACCESS.md` — but it requires a Data Use Agreement through Harvard DBMI tied to your personal/institutional identity. That's account creation and submission of personal identifying information to a third-party portal, which isn't something I can do on your behalf. If you want this closed, you'd need to register and request access yourself (the doc has the exact portal links and a heads-up that the dataset was reported "temporarily unavailable" as recently as mid-2026) — I can pick the pipeline work back up the moment you have a real sample file in hand.
- **Deliberately not pursued:** the other 11 Loghub datasets (Apache, HDFS, BGL, Hadoop, etc.) were excluded by this project's own authors because none plausibly carries a person's name, email, SSN, or MRN in production — downloading them would pad the dataset count without testing anything real. Consistent with the paper's own stated discipline against exactly that.
- **A real option I haven't pursued yet:** the coverage and re-identification models are validated against real data (2010 Census, SSA), but that data is US-centric only — Faker's `en_US` name dictionaries, matched against US frequency data. If you want, I can look for free, no-registration international name-frequency sources (e.g., UK ONS baby names, other national statistics offices) to test whether the framework's assumptions hold outside a US-only population — this would be new ground, not yet touched anywhere in the paper. Let me know if you want this pursued; it depends on whether this sandbox's network allowlist reaches those sources, which I haven't checked yet.

## 6. Real PHI/MRN validation via MEDDOCAN (new this pass — closes the one real gap named in Section 5)

Section 5 flagged PHI/MRN detection as the project's one real dataset gap: zero real ground truth, n2c2 (the intended fix) blocked behind a Harvard DBMI Data Use Agreement whose portal was confirmed live (browser check) to be showing "Temporarily Unavailable" platform-wide, independent of account status. Rather than leave this closed off, searched for a DUA-free alternative and found **MEDDOCAN**: 1,000 real Spanish clinical case reports (SciELO-sourced, PHI enriched by expert annotators, 98% inter-annotator agreement, CC BY 4.0 — no DUA, no registration). Verified two other candidates (PhysioNet's original de-identification corpus, CARMEN-I) still require DUA+credentialing despite initially looking promising, ruling them out the same way n2c2 was ruled in-but-blocked.

**Scope decision, made explicitly rather than defaulted into:** MEDDOCAN is Spanish-language, a real mismatch with this project's English/US-only detection patterns. Presented three options (lightweight stand-in check, full adaptation, hold for n2c2); **full adaptation was chosen** — build real Spanish-aware patterns and score them properly, not an approximate substitute.

**Coverage obtained:** 520 of 1,000 documents (52%) — 210/500 train, 190/250 validation, 120/250 test — fetched via Hugging Face's public datasets-server row API (no DUA needed for this path either) through a paginated `web_fetch`-to-local-file technique, since this sandbox's network allowlist blocks huggingface.co/github.com/zenodo.org directly. Collection was cut short by a sustained run of empty API responses across all three splits late in the session — disclosed as a real, unresolved API-reliability limitation, not silently worked around. Every document that IS staged carries its full real carrier text and complete real gold-standard entity annotation; this is an honestly partial *sample*, not a corrupted or synthetic stand-in. Staged at `validation/real_data/datasets/MEDDOCAN_raw.jsonl`; provenance and exact resume-collection instructions in `validation/real_data/prepare_meddocan_dataset.py`.

**Type mapping (full reasoning in `validation/real_data/MEDDOCAN_TYPE_MAPPING.md`):** of MEDDOCAN's 22 annotated PHI types, only 5 map onto REDACT's 6-type canonical vocabulary — `NOMBRE_SUJETO_ASISTENCIA`/`NOMBRE_PERSONAL_SANITARIO` → PERSON, `CORREO_ELECTRONICO` → EMAIL, `ID_SUJETO_ASISTENCIA` (NHC/patient ID) → MRN, `ID_ASEGURAMIENTO` (NASS/social-insurance number) → SSN. The other 16 types (address, dates, age, sex, institution names, phone/fax, profession, etc.) are real PHI under HIPAA Safe Harbor's 18-category list but are not claims REDACT makes — scoring against them would test nothing real, so they're excluded as a stated scope decision, not silently dropped. One gap worth naming in the paper's future-work: `NUMERO_TELEFONO`/`NUMERO_FAX` (phone/fax) plausibly should be a REDACT type given how often phone numbers appear in real enterprise telemetry; currently out of scope.

**New Spanish-aware detection layer (`src/es_detect.py`), added as a genuinely separate, opt-in module — not a modification of `detect.py`:** confirmed via `git status` that adding it touched zero existing files, and the existing test suite (`tests/test_fast_validation.py`, 10 tests) passed at the same 9/10 rate as before (the one failure is the pre-existing, already-documented spaCy-model network block, unrelated to this change) — the same "never touch an already-measured pattern" discipline `detect.py`'s own inline history already documents for the AWS-account-ID and Zookeeper collisions. Three additions: `NHC_ES` (patient-ID regex, label-anchored like the existing MRN pattern, tolerant of the "NHC:", "CIPA: nhc-", and bare-CIPA label variants actually observed), `NASS_ES` (3-digit-group social-insurance-number regex, deliberately NOT matching the 2-group variant missing its check digits — a stated precision/recall tradeoff, not an oversight), and a Spanish name dictionary (Faker `es_ES` provider, ~952 first names/~1,085 surnames — same "vetted public generator data" sourcing already used for the English name dictionary) over runs of capitalized words, since MEDDOCAN's names are space-separated and this sandbox's Presidio NER (the layer that normally handles space-separated names) is network-blocked, the same limitation already disclosed throughout Section 5.

**Results, three conditions per document (full numbers via `validation/real_data/evaluate_meddocan.py`):**

| condition | overall P | overall R | PERSON P/R | EMAIL P/R | MRN P/R | SSN P/R |
|---|---|---|---|---|---|---|
| en-only (existing patterns, unmodified) | 0.988 | 0.138 | 0.000 / 0.000 | 0.994 / 0.992 | 0.000 / 0.000 | 0.000 / 0.000 |
| es-only (new Spanish layer alone) | 0.576 | 0.791 | 0.474 / 0.912 | 0.000 / 0.000 | 0.996 / 0.923 | 0.995 / 0.946 |
| combined (both layers, deduped) | 0.614 | 0.929 | 0.474 / 0.912 | 0.994 / 0.992 | 0.996 / 0.923 | 0.995 / 0.946 |

3,626 gold spans scored, across 520 real documents. Every number here is genuinely earned — EMAIL's near-1.0/1.0 in en-only confirms the existing regex needed zero modification (format is language-agnostic, exactly as predicted); en-only's flat 0.000 on PERSON/MRN/SSN is the expected, honest baseline showing the English/US patterns structurally cannot match Spanish PHI formats at all (not a bug, the absence of any attempt); the Spanish layer closes essentially all of that gap for MRN/SSN (92–95% recall, >99% precision — a clean regex-format win) and most of it for PERSON (91% recall) at a real precision cost (47%).

**Devil's-advocate finding, root-caused and quantified, not just flagged:** PERSON precision (47%) is the one weak number here, and it deserved investigation before being reported as a bare figure. Traced it directly: of 2,017 PERSON false positives across the full 520-document corpus, **1,903 (94.3%) overlap a real gold span of a different, out-of-scope MEDDOCAN type** — overwhelmingly `TERRITORIO` (place names) and related location/institution types. Root cause: Spanish place names and Spanish personal names share a real, unavoidable lexical overlap that a plain dictionary lookup cannot resolve — "Santiago," "Valencia," "Córdoba," "Segovia," and "Rosario" are simultaneously common Spanish first names/surnames AND common Spanish place names in this exact dataset. This is not a bug in the implementation (the dictionary lookup is working exactly as designed) — it's the well-known general weakness of dictionary-only NER-substitutes: no local disambiguation between "used as a name" vs. "used as a place" without sentence-level context, which is precisely the kind of context Presidio's real NER model would supply and this network-blocked-workaround cannot.

**Legal/monetary/regulatory framing (per the project's stated review mandate):** this quantifies a concrete GDPR Article 32 / HIPAA Security Rule risk if a dictionary-only fallback were ever deployed as a PERSON detector's sole layer in production: a 47% precision PERSON detector generates roughly one false redaction/alert for every real hit, which in an automated pipeline either (a) silently over-redacts non-PII place names — a usability/data-utility cost, not a compliance risk — or (b) if used for anything requiring high-precision human review, roughly doubles analyst triage load. **Mitigation, stated as an auditable requirement, not a suggestion:** dictionary-based name detection should never be the sole PERSON layer in a production deployment — it is presented and scored here explicitly as a documented, effort-bounded stand-in for the NER layer this sandbox cannot run, not as a recommended production architecture. The paper should state this limitation exactly this plainly rather than let a reader infer a stronger production claim than the evidence supports.

**Honest coverage caveat for the paper:** these results are measured on 52% of the MEDDOCAN corpus (520/1,000 documents), not the full benchmark, for the disclosed API-reliability reason above. The sample spans all three of MEDDOCAN's official splits (train/validation/test) and 21 of its 22 defined entity types, so it is not a narrow or cherry-picked slice — but the paper should state "520 real documents" or "52% of MEDDOCAN," not imply full-corpus coverage.

## Decisions

Resolved as follows, per your direction to work through these myself.

**1. Coverage model — point estimate vs. sensitivity range: report both, primary + range.**
Keep 14.65%/15.25% as the primary reported figure (consistent with the existing 10-year-window convention elsewhere in the paper), but state the 13.4%–30.9% sensitivity range explicitly as a population-assumption caveat rather than omit it. Devil's-advocate case for this: the re-identification model just independently reproduced the *same* sensitivity pattern on a structurally different quantity (key-rotation guidance, 7.7→3.8 days). Two independent models both showing population-window sensitivity is a real, reproducible property of SSA-based frequency modeling, not a fluke in one script. A reviewer who spot-checks this (trivial to do — the SSA data is public) and finds an undisclosed 2x swing would read as either sloppy or cherry-picked; disclosing it proactively converts a vulnerability into evidence of rigor, which is the direct antidote to "technical depth is very limited."

**2. Drift framework — include both the parametric Monte Carlo and the real-carrier-text injection results.**
They corroborate each other (z-test FPR ~5% vs. naive rule's uncontrolled 17–43%, consistent within ~1 point across two independent real datasets and the earlier parametric sweep). Reporting both is itself a second independent validation methodology, not just a repeated number — this is exactly the kind of triangulation an editor citing "limited technical depth" would want to see.

**3. Disclose the fake.name()/flattened-username methodology correction — yes.**
Frame it positively: injection compatibility with each detection layer was verified before use; an initial design (free-text names) was found incompatible with the regex/flattened layer specifically because that layer only matches concatenated tokens by design, and was corrected to a layer-appropriate injection shape. This is consistent with the paper's existing house style of disclosing methodological substitutions (e.g., the NER-sandbox-blocked disclosure already in the manuscript), and it preempts a reviewer independently finding the same gap, which would land far worse undisclosed.

**4. Re-identification model — now complete (this pass).** Monte Carlo cross-check confirms the implementation has no systematic bias (one flagged result traced to expected multiple-comparisons noise, not a defect). New finding: key-rotation guidance is population-window sensitive by >2x, same population-assumption caveat as decision #1, and should be written up with the same framing for internal consistency — i.e., not "λmax ≈ 7.7 days" as a bare constant, but tied explicitly to an assumed population age profile, with the sensitivity table as supporting evidence. This also suggests a small addition to the paper's contribution: framing the year-window choice as a **decision parameter** tied to deployment context (recent-signup systems vs. decades-spanning legacy identity systems) turns a limitation into practitioner guidance, which fits the practitioner-facing framing of the paper generally.

All four data-gathering items are now closed. Nothing has been written into manuscript prose — next step (on hold pending your go-ahead) is turning this into the actual write-up.

## 7. French language extension via OpenPII (new this pass — first of the LANGUAGE_EXTENSION.md shortlist)

`LANGUAGE_EXTENSION.md` (a prior research pass surveying nine languages)
recommended French first, flagging three clinical-NLP corpora — CAS,
ESSAI, QUAERO — as candidates needing license verification. Checked all
three directly: license turned out to be resolvable (QUAERO is
GFDL-licensed, CAS's paper CC BY 4.0), but a more fundamental problem
surfaced by fetching `DrBenchmark/QUAERO`'s actual published NER schema —
**none of the three annotate PII/de-identification spans at all.** Their
label set (LIVB/PROC/ANAT/DEVI/CHEM/GEOG/PHYS/PHEN/DISO/OBJC) is UMLS
clinical-concept annotation (living beings, procedures, diseases), not
PERSON/EMAIL/ID-number spans — a different task. A benchmark can be
well-licensed and still be structurally unusable if it annotated the
wrong kind of thing. Same "verify before building" discipline this
project already applied to n2c2/MEDDOCAN, applied here and catching a
different, non-license failure mode.

**Alternative found:** `ai4privacy/open-pii-masking-500k-ai4privacy`
(Hugging Face), CC BY 4.0, `gated: false`, `private: false` (verified
directly against the HF dataset API). Multilingual PII-masking corpus
with exact-offset `privacy_mask` spans across a PERSON/EMAIL/phone/
ID-number-shaped taxonomy — the right KIND of annotation. **A real,
disclosed difference from MEDDOCAN, stated plainly rather than glossed
over:** this dataset's carrier text is template/LLM-generated synthetic
text ("Ajljin : 'Je suis très excité de commencer ce projet...'"), not
real human-authored documents. Results below measure REDACT's French
layer against realistic-format, synthetic-content PII in short
single-sentence contexts — a meaningfully easier and different setting
than MEDDOCAN's full real clinical narratives, not directly comparable to
Section 6's numbers.

**Coverage — a genuinely small pilot sample, disclosed as such, not
inflated:** 11 French-language documents, hand-staged and offset-verified
(every gold span's characters checked against its own text — zero
mismatches), from the dataset's `validation` split (row_idx 0–114 of
~116,000 rows in that split alone). Comparable in scale to, or smaller
than, this project's own windows_event condition (n=33), already
precedented here as small-but-directional rather than a confident final
read — treat these numbers the same way. Staged at
`validation/real_data/datasets/OpenPII_FR_raw.jsonl`; provenance and
exact resume-collection instructions in
`validation/real_data/prepare_fr_dataset.py`.

**Type mapping (full reasoning in `validation/real_data/
FR_TYPE_MAPPING.md`):** of the ai4privacy labels observed in this sample,
3 map onto REDACT's canonical vocabulary — `GIVENNAME`+`SURNAME` → PERSON,
`EMAIL` → EMAIL, `CREDITCARDNUMBER` → CREDIT_CARD. Unlike Spanish/MEDDOCAN,
**no new regex patterns were needed for SSN or MRN** — no French-specific
ID-number format (SOCIALNUM/IDCARDNUM) appeared anywhere in this pilot
sample, so `src/fr_detect.py` adds exactly one new layer: a French name
dictionary (Faker `fr_FR`, 219 first names / 400 surnames) over
capitalized-word runs, mirroring `es_detect.py`'s Spanish approach. This
is itself a disclosed scope limitation, not a claim that French has no
SSN/MRN-equivalent format — just that this 11-document sample hasn't
surfaced one yet to build a real pattern against.

**Results, three dictionary-only conditions (full numbers via
`validation/real_data/evaluate_fr.py`; NER conditions need the Docker
handoff — see below):**

| condition | overall P | overall R | PERSON P/R | EMAIL P/R | CREDIT_CARD P/R |
|---|---|---|---|---|---|
| en-only (existing patterns, unmodified) | 1.000 | 0.143 | 0.000 / 0.000 | 1.000 / 1.000 | 1.000 / 0.500 |
| fr-only (new French dictionary layer alone) | 0.000 | 0.000 | 0.000 / 0.000 | 0.000 / 0.000 | 0.000 / 0.000 |
| combined (both layers, deduped) | 1.000 | 0.143 | 0.000 / 0.000 | 1.000 / 1.000 | 1.000 / 0.500 |

14 gold spans scored, across 11 documents (11 PERSON, 1 EMAIL, 2 CREDIT_CARD).

**Two real findings, root-caused rather than left as bare numbers:**

1. **The French name dictionary layer scored 0/11 recall — not a bug,
   a genuine dictionary-coverage mismatch.** Checked every one of the 11
   gold PERSON names directly against `fr_detect.py`'s dictionary: not one
   word (first or last) of any gold name — "Ajljin," "Naphtali," "Dafni
   Votime," "Yue Elmostafa," "Tüba" — appears in Faker's `fr_FR` list
   (219 first names / 400 surnames, a curated "authentic French names"
   list). Root cause: ai4privacy's synthetic given-names are drawn from a
   deliberately broad, globalized name-generation pool (consistent with a
   dataset covering many languages/regions with the same generator
   family), not from a distribution resembling France's actual name
   frequency — a different situation from MEDDOCAN, where real Spanish
   clinical-document patient names genuinely did overlap Faker's `es_ES`
   list at a real, measurable 47.4%/91.2% precision/recall. **This means
   the 0% figure is a property of this specific synthetic dataset's name
   generation, not evidence the dictionary-lookup approach itself is
   broken** — but it is exactly the kind of gap that needs a larger
   sample and/or the NER layer (which doesn't depend on a fixed
   dictionary) to resolve, and should not be read as "French PERSON
   detection doesn't work."
2. **CREDIT_CARD's 1/2 recall traces to REDACT's existing Luhn-checksum
   safeguard, not a French-specific bug.** `detect.py`'s CREDIT_CARD regex
   already includes a Luhn-validity check (added earlier in this project
   specifically to cut AWS-account-ID false positives — see `detect.py`'s
   own documented history, and Section 5's CloudTrail timestamp finding
   for the same mechanism's other side effect). Confirmed directly:
   ai4privacy's two staged `CREDITCARDNUMBER` values are
   `3132629134190324844` (Luhn-valid, detected) and `4543256230675317`
   (Luhn-INVALID, silently missed) — ai4privacy generates these as
   plausible-length random digit strings, not real Luhn-valid card
   numbers. **This is the same class of residual false-negative/positive
   tradeoff Section 5's Luhn finding already named for CloudTrail
   timestamps, now observed from the other direction:** the safeguard
   that reduces false positives on incidental digit strings will also
   silently miss any genuinely PII-labeled digit string that happens not
   to satisfy Luhn — worth stating in the paper as a general property of
   the Luhn mitigation, not something specific to this dataset or
   language.

**NER conditions (`fr-ner`, `full`) not yet run** — same
network-blocked-spaCy-model-download limitation as every other real-data
condition in this project. `src/fr_ner.py`, `Dockerfile.fr_ner`, and
`run_fr_ner.sh` are built and ready (mirroring `es_ner.py`/
`Dockerfile.meddocan_ner`/`run_meddocan_ner.sh` file-for-file); running
`bash validation/real_data/run_fr_ner.sh` locally (needs Docker) will
produce real French NER numbers and `--diagnose` root-cause output on
demand, same as MEDDOCAN's `es-ner` results.

**Devil's-advocate framing:** the honest headline here is not "French
detection works" — dictionary-only PERSON recall is 0% on this specific
sample, a real and disclosed result, not hidden behind the still-clean
EMAIL/CREDIT_CARD numbers. The right read is narrower and still useful:
(a) the file-for-file Spanish→French port process itself works cleanly —
zero existing files touched, evaluation harness runs, type-mapping
discipline held; (b) EMAIL and CREDIT_CARD confirm REDACT's existing
language-agnostic regex needs no French-specific code, exactly as
FR_TYPE_MAPPING.md predicted; (c) PERSON's 0% is a genuine, traceable
dictionary-coverage gap specific to this synthetic dataset's name
generation, not a structural failure of the approach — and it is exactly
the kind of result the NER layer and a larger staged sample exist to
resolve, both concretely actioned above rather than left open-ended.

**LANGUAGE_EXTENSION.md's shortlist status updated** to mark French done
(dictionary-only pass) before Russian work begins, per the project's own
sequential-work discipline.

## 8. Russian language extension via a redmadrobot PII benchmark (new this pass — second of the LANGUAGE_EXTENSION.md shortlist)

LANGUAGE_EXTENSION.md's original research pass found no Russian PII/
de-identification benchmark at all — the closest candidates,
RuMedNER/RuDReC, annotate drug-reaction clinical entities, the same
wrong-kind-of-annotation problem Section 7 found for French's CAS/ESSAI/
QUAERO. A fresh literature search (same discipline that originally found
MEDDOCAN) surfaced `redmadrobot-rnd/pii_benchmark` (Hugging Face): MIT
license, `gated: false`, `private: false` (verified directly against the
HF API). 21 PII entity types across four families — person names
(including patronymics), address hierarchy, contacts, and Russian
identity-document numbers (SNILS, OMS, INN, passport, driver's license,
military ID, birth certificate).

**A third, distinct provenance pattern, disclosed rather than glossed
over:** per the dataset's own README, its test set "combines real,
manually annotated examples from production logs — where all real
personal data has been replaced with synthetic equivalents — along with
synthetic document-style texts and hand-filtered hard negatives." This
is neither MEDDOCAN's real-carrier/real-PII pairing nor OpenPII French's
fully-synthetic-template pairing — it's real carrier context with
synthetic PII values substituted in, the same pattern this project's own
`inject_and_evaluate.py` methodology already uses. Full reasoning:
`validation/real_data/prepare_ru_dataset.py`.

**A real mechanical wrinkle this dataset had that neither MEDDOCAN nor
OpenPII French did: no character offsets.** The dataset ships token-level
BIO tags, not `[start, end]` spans. Character spans in the staged file
were reconstructed — each token located via sequential left-to-right
substring search from the previous token's end, adjacent same-type B-/I-
tags merged into one span, every resulting span re-verified against the
source text. 26 of 26 staged rows validated cleanly (zero offset
mismatches) — full method in `prepare_ru_dataset.py`.

**Coverage:** 26 documents, curated (not randomly sampled) from the
dataset's 2,841-row `test` split for coverage of REDACT-mappable entity
types, staged at `validation/real_data/datasets/OpenRU_PII_raw.jsonl`.
Smaller than MEDDOCAN, comparable to OpenPII French — disclosed as
directional, not a confident final read, same discipline as Section 7.

**Type mapping (full reasoning in `validation/real_data/
RU_TYPE_MAPPING.md`):** 6 of the 21 observed labels map onto REDACT's
**full six-type canonical vocabulary at once** — the first language pass
to do so. `FIRST_NAME`+`LAST_NAME`+`MIDDLE_NAME` → PERSON (patronymics
are a structurally new element neither Spanish nor French had), `EMAIL`
→ EMAIL, `CREDIT_CARD` → CREDIT_CARD, `SNILS` (individual insurance
account number) → SSN — the direct Russian analogue to US SSN, same role
MEDDOCAN's NASS field played for Spanish — `OMS` (mandatory medical
insurance policy number) → MRN, arguably a cleaner medical-context fit
than MEDDOCAN's own NHC mapping needed (OMS is literally named "medical
insurance"), and `IP_ADDRESS` → IP. `INN` (taxpayer ID) was deliberately
left unmapped rather than double-loaded onto SSN — recorded as a
disclosed, bounded next step, not a silent gap.

**A real, disclosed general-format gap this dataset surfaced: REDACT's
IP pattern is IPv4-only.** This Russian sample's real IP_ADDRESS gold
spans include full IPv6 addresses, which `detect.py`'s existing
`\b(?:\d{1,3}\.){3}\d{1,3}\b` pattern cannot match at all — not a
Russian-language issue (IPv6 syntax has no locale variation), but a
scope gap only surfaced because this pass staged non-US, non-English
real-context data. Added as a new IPv6 regex inside `ru_detect.py`
(explicitly not by touching `detect.py`'s own IP pattern), and disclosed
plainly as general-purpose, not Russian-specific.

**The flagged inflection gotcha, addressed before building, not after a
low number appeared:** LANGUAGE_EXTENSION.md explicitly warned Russian's
heavy inflection would break the Spanish/French exact-string dictionary
approach. Confirmed directly: `"Курганской области"` is genitive case,
not the nominative dictionary form. Fixed with `pymorphy2` (MIT-licensed;
`pip install pymorphy2 pymorphy2-dicts-ru` succeeds directly in this
sandbox — a genuine capability difference from the NER model download,
which remains Docker-only), lemmatizing BOTH the candidate word and the
Faker `ru_RU` dictionary through the same function before comparing —
comparing a raw dictionary string against a bare pymorphy2 lemma would
have silently failed even for an already-nominative word (confirmed:
"Леонтьевич" normalizes to the spelling variant "леонтиевич", which
would not match the unlemmatized dictionary string at all).

**Results, three dictionary-only conditions (full numbers via
`validation/real_data/evaluate_ru.py`; NER conditions need the Docker
handoff, same as Spanish/French):**

| condition | overall P | overall R | PERSON P/R | EMAIL P/R | CREDIT_CARD P/R | SSN P/R | MRN P/R | IP P/R |
|---|---|---|---|---|---|---|---|---|
| en-only (existing patterns, unmodified) | 0.750 | 0.061 | 0.000/0.000 | 1.000/1.000 | 0.000/0.000 | 0.000/0.000 | 0.000/0.000 | 0.500/0.333 |
| ru-only (new Russian layer alone) | 0.941 | 0.327 | 0.900/0.273 | 0.000/0.000 | 0.000/0.000 | 1.000/1.000 | 1.000/0.600 | 1.000/0.667 |
| combined (both layers, deduped) | 0.905 | 0.388 | 0.900/0.273 | 1.000/1.000 | 0.000/0.000 | 1.000/1.000 | 1.000/0.600 | 0.750/1.000 |

49 gold spans scored, across 26 documents.

**Three real findings, root-caused rather than left as bare numbers:**

1. **CREDIT_CARD scored 0/4 recall — the Luhn-checksum finding from
   Section 7 replicates, more starkly, on a second independent dataset.**
   Checked directly: all four staged Russian CREDIT_CARD values
   (`5536-9137-5000-4321`, `4817-7654-1111-9876`, `5428 7710 8956 3450`,
   `4276 1111 2222 3333`) fail `detect.py`'s Luhn check. This is now a
   twice-replicated pattern across two unrelated synthetic-PII datasets
   in two languages: benchmark-generated "plausible" card numbers are
   frequently NOT Luhn-valid, so REDACT's Luhn safeguard — which
   correctly suppresses false positives on incidental digit strings —
   will systematically miss synthetic-benchmark card numbers specifically
   because they're synthetic. Worth stating in the paper as a general,
   now cross-validated property of the mitigation, not a per-dataset
   curiosity.
2. **PERSON recall (27.3%, dictionary-only) splits cleanly into two
   separate, independently-actionable failure modes, checked by hand
   against every missed name rather than reported as one number.** Of 12
   missed names: 4 (`вениаминович`, `гЕРМаНОвНа`, `сВЯтослАВОВИЧ`,
   `архипов`) DO have a matching dictionary lemma but were missed because
   the capitalized-word-run regex (inherited unchanged from the Spanish/
   French pattern) requires the run to START with an uppercase letter —
   these four appear in the dataset's own hand-filtered hard-negative
   examples specifically as garbled-case or lowercase-leading tokens
   (e.g. `"архипов, Reginald сВЯтослАВОВИЧ"` — a surname in lowercase at
   sentence start). This is a real, fixable regex-design limitation, not
   a dictionary problem — a future pass should anchor the run on ANY
   letter-casing, not just capitalized, and verify via lemma lookup
   alone. The remaining 8 (`Стрельцов`, `Зубарева`, `Добрыня`,
   `Wakefield`, `Leonard`, `Назаровна`, `терехов`, `Reginald`) are a
   genuine dictionary-coverage gap — Faker's `ru_RU` list (250/250
   surnames, 321/80 first names) simply doesn't contain them, confirmed
   by checking each lemma directly against the dictionary set. Same class
   of finding as Section 7's French 0%-overlap result, but here isolated
   from the (correctly solved) inflection problem rather than conflated
   with it — `evaluate_ru.py --diagnose` reports this same
   dictionary-vs-regex split automatically for any future run.
3. **MRN (OMS) and SSN (SNILS) precision is clean (100%) but MRN recall
   caps at 60% by design, not by accident.** Both regexes are
   deliberately label-anchored (require "снилс"/"ОМС"/"полис" nearby)
   because an unanchored 16-digit run is indistinguishable from
   CREDIT_CARD, and an unanchored 11-digit run is not distinctive enough
   to safely match — same design principle `es_detect.py`'s NASS_ES
   pattern already established. The two OMS misses trace to real
   label-mismatch cases in the staged text: one has a typo'd label
   ("поолис" instead of "полис", `ru-pii-34`) and one has no nearby label
   at all in the sentence itself (`ru-pii-38`, "Я пользуюсь номером
   9876543210987654" — the OMS framing is established earlier in a
   longer real production-log message than the isolated sentence
   captures). Both are honest, expected consequences of the
   label-anchoring design decision, not detection bugs — the same
   precision/recall tradeoff NASS_ES's own 2-group-variant exclusion
   already documented for Spanish.

**NER conditions (`ru-ner`, `full`) not yet run** — same
network-blocked-spaCy-model-download limitation as every other real-data
condition in this project. `src/ru_ner.py`, `Dockerfile.ru_ner`, and
`run_ru_ner.sh` are built and ready (mirroring the Spanish/French Docker
pattern file-for-file); running `bash validation/real_data/run_ru_ner.sh`
locally will produce real Russian NER numbers and answer the one open
question this pass could not: does spaCy's `ru_core_news_md` handle
inflected surnames (and the case-garbled hard negatives) better than the
lemmatized dictionary layer — the actual comparison NER exists to make
here, not assumed either way.

**Devil's-advocate framing:** the honest headline is that Russian's
inflection problem was correctly anticipated and solved (lemmatization
works, confirmed directly on the "Леонтьевич" spelling-variant case) —
but solving inflection did not close the door on PERSON recall, because a
SEPARATE dictionary-coverage gap and a separate regex-casing limitation
were both waiting underneath it. Reporting only the aggregate 27.3%
number would have obscured that two different, differently-fixable
problems are stacked in it. This is presented as a lesson for the
Indonesian pass too: solving the one flagged gotcha (here, inflection;
there, no spaCy model) does not guarantee a clean result — plan for a
`--diagnose`-style breakdown from the start rather than treating the
flagged risk as the only risk.

**LANGUAGE_EXTENSION.md's shortlist status updated** to mark Russian done
(dictionary-only pass) before Indonesian work begins.

## 9. Indonesian language extension — pipeline built, evaluation blocked on data access (third of the LANGUAGE_EXTENSION.md shortlist)

**This section reports a genuine blocker honestly rather than presenting
results that don't exist.** Indonesian is the one language in this
three-language pass with no real evaluation numbers yet — not because
the engineering wasn't done, but because the one dataset that fits could
not be reliably staged from this sandbox. Read on for exactly what was
tried, what was built anyway, and the precise, actionable unblock step.

**License/benchmark research (done first, per checklist discipline):** a
fresh literature search confirmed LANGUAGE_EXTENSION.md's original
finding still holds — no dedicated Indonesian PII/de-identification
corpus exists. IndoNLU/IndoLEM (MIT-licensed, general PERSON/
ORGANISATION/PLACE NER), IndQNER (Quran-translation entities, 18 classes,
religious domain), IndoLER (legal-document entities), and NERSkill.Id
(skill entities) are all real and license-clear, but every one of them
has the same wrong-kind-of-annotation problem Sections 7 and 8 already
found for French's CAS/ESSAI/QUAERO and Russian's RuMedNER/RuDReC — they
label the wrong KIND of span, not just the wrong domain.

**A candidate WAS found: `ai4privacy/pii-masking-openpii-1.5m`** — the
same dataset family used for French (Section 7), specifically its larger
flagship rather than the `open-pii-masking-500k-ai4privacy` sibling
French used (which does not cover Indonesian — confirmed via its
language-code list). CC BY 4.0, `gated: false`, and `language: id`
confirmed present via the HF dataset API and by directly observing rows
of other languages (Korean, Vietnamese, Japanese) sharing the exact same
`privacy_mask` label schema this project already trusts from French.

**The actual blocker, reproduced directly rather than assumed:** Hugging
Face's `datasets-server` row API — the identical API and `web_fetch`
paging technique that successfully staged MEDDOCAN and OpenPII French —
returned empty response bodies for this specific dataset across every
attempt but one: `split=validation` failed at every offset tried;
`split=train` succeeded exactly once, at `offset=0, length=5` (5 real
rows returned, languages ko/vi/ja/ja/ja, no Indonesian in that
particular page); the identical query re-run ~60 seconds later returned
empty; offsets 5, 10, 1000, and 50000 at various lengths all returned
empty, tried multiple times each. This matches a data-access-reliability
limitation this project's own history had already flagged for this exact
large dataset (as distinct from its smaller, reliably-accessible
sibling) — now directly reconfirmed with roughly ten fresh attempts
rather than assumed to still hold from an earlier note.

**Decision made explicitly, not defaulted into:** per the project's own
checklist ("if nothing license-clear turns up for a language, document
that finding and move to the next language rather than stalling"), the
SPIRIT of that rule is honored here even though the literal trigger
(no license-clear candidate) didn't occur — the honest move when a
real, correctly-annotated, license-clear candidate exists but isn't
PRACTICALLY stageable is the same as when none exists at all: don't
fabricate data, don't burn further budget on an API already shown
unreliable, disclose the blocker precisely, and build everything that
CAN be built so the moment it's unblocked, real numbers follow
immediately rather than requiring another full engineering pass.

**What WAS built, ready to run the instant data is staged:**
- `validation/real_data/ID_TYPE_MAPPING.md` — a mapping proposed from the
  dataset family's known shared label schema (verified identical across
  every language's rows observed so far), explicitly marked pending
  verification on every row rather than presented as settled: PERSON
  (GIVENNAME+SURNAME), EMAIL, CREDIT_CARD, and a proposed SSN mapping via
  SOCIALNUM (a label directly observed in this session, in a Vietnamese
  row sharing the same schema: "số an sinh xã hội (9632359792)"). No MRN
  or IP mapping proposed — this dataset family's schema has no
  medical-context or network-address label at all, a real, disclosed
  structural absence rather than a stretch mapping.
- `src/id_detect.py` — Faker `id_ID` name dictionary layer (716 first
  names / 175 last names, the richest Faker locale of any language built
  in this project) over capitalized-word runs. No lemmatization needed
  (Bahasa Indonesia is not case-inflected, unlike Russian) and no new
  regex patterns yet (writing one against an ID-number format nobody has
  actually observed in real Indonesian text would be guessing, not
  engineering — disclosed explicitly in the module's own docstring).
- `src/id_ner.py` — **the flagged architectural gotcha, addressed from
  the start, not discovered as a surprise:** Indonesian has no official
  spaCy model, so Presidio's `NlpEngineProvider` (the pattern every other
  language's `_ner.py` uses) has nothing to point at. Built a direct
  HuggingFace `transformers` pipeline wrapper instead —
  `cahya/bert-base-indonesian-NER`, a community-maintained BERT model
  fine-tuned for Indonesian NER — bypassing Presidio's spaCy-centric
  architecture entirely rather than forcing a transformers model through
  Presidio's more involved TransformersNlpEngine integration. Same
  `{"type", "start", "end", "method"}` hit-dict shape as every other
  scan_*() function in this project, so it drops into the same evaluation
  harness pattern without special-casing.
- `Dockerfile.id_ner` / `run_id_ner.sh` — same Docker-handoff pattern as
  the other three languages, adapted for `transformers`+`torch` instead
  of `spacy`. `run_id_ner.sh` checks for the staged data file and fails
  with a clear, actionable message pointing at `prepare_id_dataset.py` if
  it's missing, rather than failing deep inside a Docker build.
- `validation/real_data/evaluate_id.py` — same 5-condition structure as
  the other three languages' harnesses. `load_docs()` fails loudly with
  the exact blocker explanation and unblock commands if
  `OpenPII_ID_raw.jsonl` doesn't exist yet, rather than crashing
  obscurely or silently returning zero documents.

**Exact unblock path, for whoever has real internet access next:**
`prepare_id_dataset.py`'s docstring has the precise `pip install
datasets` + `load_dataset(...)` + filter-for-`language=='id'` commands,
plus the exact JSONL shape to convert the results into (identical schema
to French's staged file, since it's the same source dataset family).
Once `validation/real_data/datasets/OpenPII_ID_raw.jsonl` exists,
`evaluate_id.py` runs with no other code changes needed, and
`run_id_ner.sh` unblocks the NER condition the same way `run_fr_ner.sh`/
`run_ru_ner.sh` already do for French/Russian.

**LANGUAGE_EXTENSION.md's shortlist status updated** to reflect
Indonesian's actual state: pipeline complete, evaluation pending data
access — not silently marked "done" with numbers that don't exist, and
not silently skipped without explanation either.
