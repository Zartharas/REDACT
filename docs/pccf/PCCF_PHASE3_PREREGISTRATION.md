# PCCF phase 3: pre-registration

Written 2026-09-23, **before** any phase-3 harness was run. Each harness
prints its hypotheses verbatim and judges them mechanically. If any
hypothesis is changed after this point, the change is dated and explained
here rather than silently edited.

The shared setup comes from phases 1–2: presence-pattern Mondrian
calibration (`src/pccf.py`), context-feature scorers (`src/pccf_context.py`),
and the redaction tier (per-pattern conformal recall floor). "tol" means
the sampling tolerance 2·sqrt(α(1−α)/n_true), as in phase 2.

## T1: French/Russian audit (descriptive, with one mechanical decision)
For OpenPII-FR (11 docs) and the RU PII benchmark (26 docs), report:

- gold PERSON counts, split into three slices: after a "Label:" prefix,
  speaker tag (name followed by ":"), and free text
- the presence-pattern table
- the first-ever layer-alone and union P/R for FR/RU NER

**Decision D1 (mechanical):** in-language conformal calibration is
declared infeasible for any pattern with fewer than 19 true candidates.
19 is the minimum n for a finite (1−α) quantile at α = 0.05.

## T2: Spanish free text (synthetic names injected into real MEDDOCAN narrative)
**Design:**

- Faker es_ES person names and dictionary-colliding place names are
  inserted as template sentences into the *narrative* region of real
  MEDDOCAN documents: 2 person and 2 place sentences per document.
- Template set A is used for train and validation; template set B,
  disjoint from A, for test.
- Scoring is restricted to narrative lines (no "Label:" prefix).
- Scorers:
  - LR-full, fit on clean MEDDOCAN train (the transfer case)
  - LR-refit, fit on injected train narrative (template A)
  - the rule scorer
- Thresholds are calibrated on injected validation narrative (template A).

**Hypotheses:**

- **H6:** LR-refit coverage α=0.05 reaches precision ≥ union + 0.05 on the
  test narrative, with the bootstrap 95% CI of ΔP excluding 0.
- **H7:** for LR-refit coverage α=0.05, per-pattern test candidate recall
  ≥ 0.95 − tol in every pattern with n_true ≥ 19.
- **H6b (expectation, not a claim):** LR-full transferred from
  header-dominated data gains less than LR-refit.

## T3: Security telemetry (the paper's domain)
**Design:**

- Datasets: the existing injection methodology (`inject_and_evaluate.py`,
  imported unmodified) on OpenSSH, Linux, Thunderbird, windows_event and
  cloudtrail, with injection seeds 42/43/44.
- Layers for PERSON: `detect.scan_ner` (English Presidio) and
  `detect.scan_flattened`, so K = 2.
- Context features: the key/field immediately preceding the value.
- Protocol: leave-one-dataset-out. For each held-out dataset, the other
  four are split 50/50 by line into fit and calibrate.

**Baselines:**

- union
- intersection
- each layer alone
- the production `detect_all_field_gated(use_flattened=True)`, both on raw
  lines and in the header-stripped simulation

**Hypotheses:**

- **H8:** the fit-free KV rule scorer, coverage α=0.05, pooled over
  held-out datasets and seeds, reaches precision ≥ union + 0.05 with a
  recall drop ≤ 0.03 vs union, and the bootstrap 95% CI of ΔP excludes 0.
- **H9 (descriptive):** recall split by name format (spaced vs flattened)
  for union and for PCCF. No claim.
- **H8c (descriptive):** PCCF vs the production field-gated path, P/R side
  by side. No claim.

## T4: French/Russian redaction tier (transfer only)
**Design:**

- The rule scorer with per-language cue lists (FR_CUES, RU_CUES), written
  from language knowledge before any FR/RU candidate was inspected.
  Thresholds are calibrated on MEDDOCAN validation (rule scorer, Spanish
  cues).
- In-language calibration is used only where D1 allows it.

**Hypothesis H10 (transfer validity):** per-pattern candidate recall on
FR/RU ≥ 0.95 − tol for every pattern with n_true ≥ 5. Reported with the
small-n caveat. A failure is evidence that thresholds do not transfer
across languages.

## T5: Alert tier, with the construction fixed here
**Construction:**

- partition="global"
- precision certificate
- search="bonferroni" over the absolute grid R ∈ {0.01, 0.02, …, 0.50}
  (that is, p ≥ 0.50 … 0.99)
- the loosest passing threshold is used
- target 0.85, δ = 0.05

**Evaluation units:**

- MEDDOCAN 5-fold CV (LR-full, same folds and seed as phase 2)
- T3 held-out datasets × seeds (KV rule scorer)
- the T2 test set (LR-refit)

**Hypotheses:**

- **H11a (validity):** test precision ≥ 0.85 in ≥ 90% of evaluation units
  that keep ≥ 20 candidates.
- **H11b (utility):** on MEDDOCAN CV, mean recall exceeds the intersection
  rule's mean recall while H11a holds.

## T6: Docker reproduction (run by the author on macOS)
`Dockerfile.pccf` + `run_pccf_docker.sh` rebuild every phase 1–3 result in
a clean `python:3.11-slim` container with pinned library and model
versions, then diff them against the committed result files.

**Success:** identical verdicts, and every reported metric within ±0.005.
Exact equality is expected but not required: spaCy and BLAS builds can
differ.

## Amendments
- **2026-09-23, T6 only, before any Docker run:** the base image is
  `python:3.10-slim`, not `python:3.11-slim`.
  - Reason: `ru_detect.py` depends on pymorphy2 0.9.1, which calls
    `inspect.getargspec`, removed in Python 3.11.
  - The committed results were produced on Python 3.10.12.
  - The success criterion is unchanged.

## T7 (added 2026-09-23, before any T7 run): names in log MESSAGE text
**Motivation:** the T3 caveat. Names were injected only into identity
fields, so the field-key cue was aligned with the gold labels by
construction, and the KV rule drops NER hits with no key.

**Design:** same 5 datasets, seeds, leave-one-dataset-out protocol and
identity-field injection as T3, plus message-text injection on top:

- Each line independently gets, with probability 0.15, an appended
  sentence containing a name. Syslog/windows_event lines get it at the end
  of the line; cloudtrail gets it as an added `"errorMessage"` string.
- Names are 50% spaced (Faker `name()`) and 50% flattened (Faker
  `user_name()`).
- With probability 0.10 (exclusive of the above), a line instead gets a
  distractor sentence naming a software product. There is no name in it,
  so it tests precision.
- Template and product lists: set A for the four fit/calibrate datasets,
  set B (disjoint) for the held-out dataset.

**Method fix under test:**

- (a) the calibration group becomes presence mask × key status
  {identity-key, other-key, no-key}, so unkeyed candidates get their own
  recall floor
- (b) a hybrid scorer: the KV rule for keyed candidates, and a text LR
  (generic shape features + NER confidence + learned previous-word
  indicators) for unkeyed candidates

Every coverage config uses α = 0.05.

**Hypotheses:**

- **H12 (the problem is real):** the T3 configuration (KV rule, mask-only
  groups) recalls message-text names at < 0.80 × union's message-text
  recall.
- **H13 (fix, validity):** for the hybrid scorer with mask×key groups,
  pooled per-group candidate recall ≥ 0.95 − tol for every group with
  n_true ≥ 19.
- **H14 (fix, utility):** for the hybrid scorer with mask×key groups,
  pooled message-text recall ≥ union's message-text recall − 0.03, AND
  overall precision ≥ union + 0.05 with the bootstrap 95% CI of ΔP
  excluding 0.
- **H15 (descriptive):** the hybrid vs the production
  `detect_all_field_gated`, by gold location (field vs message) and name
  format.
