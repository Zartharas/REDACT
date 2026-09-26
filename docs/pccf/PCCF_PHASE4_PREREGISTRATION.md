# PCCF phase 4: pre-registration (fixes for the observed limitations)

Written 2026-09-23, before any phase-4 run.

**Output location:** all outputs go to `validation/real_data/phase4/`,
deliberately outside the Docker comparison glob, so that the author's
in-flight T6 re-run is not disturbed. Each harness prints its hypotheses
and judges them mechanically.

## F1: small-group fallback (fixes: FR/RU and small log sources certify nothing)
`PCCF(min_group_true=19, fallback="global")`: a group with fewer than 19
true calibration candidates uses the global threshold, and the fallback
is recorded in `summary()`. Unit-tested here; evaluated on the large
FR/RU data once F5 has been fetched.

## F2: flattened names (fixes: 50% flattened recall)
**F2a, field policy.** Every value in an identity field (the kv key is an
identity key) is pseudonymized whether or not a detector fired, with an
allowlist for system accounts (`root`, `SYSTEM`, `LOCAL SERVICE`,
`NETWORK SERVICE`, `ANONYMOUS LOGON`, names ending in `$`, `-`,
`unknown`).

**F2b, extended segmenter** (`src/flattened_names_ext.py`, additive). It
adds the two username shapes the phase-3 diagnosis found missing:

- given name + 2–4 digits (`calvin52`)
- initial + surname (`tadams`)

Name lists come from **real SSA given names and Census 2010 surnames**,
independent of the Faker injection source: given names with ≥ 5,000
total SSA births; surnames in the top 20,000 by Census rank.

**Evaluation:** T7 corpora, template set B, seeds 42/43/44, all 5 datasets
pooled (no calibration needed).

**Hypotheses:**

- **H16:** F2a raises identity-field flattened recall from 0.504 to
  ≥ 0.95. Reported with the number of allowlisted and non-gold
  identity values it pseudonymizes (the utility cost).
- **H17:** F2b raises message-text flattened recall by ≥ +0.15 over the
  existing flattened layer, while pooled PERSON precision (union with
  F2b in place of the old flattened layer) drops by ≤ 0.02.
- **H17b (descriptive):** F2b recall on `validation/real_name_frequency`'s
  real-SSA/Census flattened sample, if that harness can be imported
  unmodified (baseline 15.2%).

## F3: alert tier (fixes: the precision certificate fails on an unseen log source)
**F3a, per-source calibration.** For each dataset and seed, calibrate on
the first 30% of that source's lines and test on the remaining 70%.
KV-LR scorer (fit on the other sources), global Bonferroni certificate,
target 0.85.

- **H18:** precision ≥ 0.85 in ≥ 90% of units that keep ≥ 20.

**F3b, online adaptive threshold** (ACI-style; Gibbs & Candès 2021).
Lines stream in order. Every kept candidate receives its label (a
simulated analyst verdict). The threshold starts from the cross-source
calibrated value and updates λ ← λ + η·((1−0.85) − err_t) with η = 0.01
on each kept candidate. Scores are on the p scale; λ is the minimum
p to keep.

- **H19:** on every held-out source with ≥ 50 kept candidates, precision
  over the stream after a 100-candidate burn-in is ≥ 0.83 (target minus
  0.02). ACI's guarantee is a long-run average, not finite-sample.
  Recall is reported alongside.

## F4: language-agnostic structural features (fixes: hand-written per-language cue lists)
`ud_features`: spaCy Universal-Dependencies context only:

- POS and dependency of the previous token
- POS and dependency of the span's head
- the span's own POS mix
- whether the line starts with a "Label:"-shaped prefix (not which label)
- sentence-initial position
- layer pattern and NER confidence

No word lists.

**Hypothesis H20 (T2 Spanish free-text protocol):** LR-UD coverage
α = 0.05 recovers ≥ 70% of LR-refit's precision gain over union on the
test narrative, with per-pattern recall ≥ 0.95 − tol. MEDDOCAN (phase-2
protocol) is reported descriptively.

## F5: larger FR/RU data (fixes: 11/26-document pilots)
Hugging Face is blocked by egress policy from both Claude workspaces.
`fetch_large_multilang.sh` runs a fetcher inside the author's
`redact-pccf` Docker image (which has internet) and writes to
`validation/real_data/datasets/large/`. Existing files are not touched.
The harness `pccf_phase4_multilang_large.py` runs once the data exists.

**Hypothesis H21:** with ≥ 19 true candidates in a group, in-language
calibration (fit and calibrate on OpenPII-FR train, test on validation;
RU split 50/50 by document) keeps per-group recall ≥ 0.95 − tol, and the
UD scorer gains ≥ +0.03 precision over union.
