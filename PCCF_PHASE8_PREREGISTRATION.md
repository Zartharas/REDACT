# PCCF phase 8: small-group clustering and shift correction (+ phase 7b) (pre-registration)

Written 2026-09-24, **before any phase-8 or phase-7b number was computed.**
No new detection runs. It reuses the phase-5 caches, the seed-0 splits (the
original protocol) and the LR-UD scorer. It covers 48 eligible rows: the 38
from phase 7 plus the 10 GLiNER `_gl` rows. Floors use the phase-6 combined
rule: violation if recall < 0.95 − 2·sqrt(α(1−α)(1/n_cal_true + 1/n_test_true)),
with α = 0.05.

## Part A: clustered calibration for small groups (answers the phase-7 fail-open cost)
**Method (CLUST).** This is a two-cluster simplification of class-conditional
clustered conformal (Ding et al. 2023):

- Groups with ≥ 19 true calibration candidates keep their own Mondrian
  threshold.
- All groups below that are merged into one "small" cluster, which gets one
  conformal threshold.
- If the merged cluster still has < 19 true calibration candidates, it fails
  open.

The guarantee becomes per *cluster*: a small group alone is no longer
guaranteed. That is the stated trade.

- **H35.** Consider the rows where PCCF fails open on at least one group that
  has test candidates. Both parts must hold:
  - CLUST raises precision over PCCF by ≥ +0.03 in at least half of these
    rows.
  - CLUST's cluster-level floors hold in ≥ 90 % of eligible cells
    (clusters/groups with n_test_true ≥ 19).
- **Descriptive:** pooled test recall inside the originally-small groups,
  under PCCF, CLUST and the global fallback.

## Part B: correcting train→validation shift
Applied to the "shifted" rows, where calibration comes from the train split
and test from another split. That is every eligible row except ru, tr,
tr_mit, ko, zh and in. The four known misses are it, hi, id_hf and tl_gl.

- **WCP (weighted conformal; Tibshirani et al. 2019):**
  - A logistic domain classifier (calibration vs *unlabelled* test candidates;
    LR-UD features + mask one-hot) gives w = p/(1−p) × n_cal/n_test.
    Weights are clipped to [0.05, 20].
  - Each test candidate gets the weighted (1−α) quantile of its group's true
    calibration scores, with its own weight on +∞.
  - Groups below 19 true calibration candidates fail open, as in PCCF.
- **ACI (adaptive conformal inference; Gibbs & Candès 2021):**
  - Per group, α_g starts at 0.05 and test candidates are processed in
    document order.
  - The threshold is the unweighted calibration quantile at level 1 − α_g,t.
  - After each *true* candidate (analyst feedback assumed), update
    α ← α + γ(0.05 − err), with err = 1 if that true candidate was rejected.
    γ = 0.005, fixed a priori.
  - Small groups fail open.

**Hypotheses:**

- **H36.** Both parts must hold:
  - WCP meets the combined floor in ≥ 3 of the 4 known-miss cells (the
    {ner} groups of it, hi, id_hf and tl_gl).
  - WCP's total violations over all shifted-row cells are ≤ PCCF's.
- **H37.** Both parts must hold:
  - ACI meets the floor in ≥ 3 of the 4 known-miss cells.
  - ACI's mean precision change vs PCCF over shifted rows is ≥ −0.02.

## Part C: phase 7b
The phase-7 comparison (B0–B3 vs PCCF) is re-judged on all 48 rows with the
same H30/H31 criteria. The 38-row phase-7 verdicts stand as recorded; 7b is
reported next to them.

## Pre-committed interpretation
- **H35 supported:** clustering recovers the fail-open precision cost while
  keeping a (weaker, per-cluster) guarantee. Recommend it for groups that
  are small but not individually sensitive.
- **H35 not supported:** keep fail-open.
- **H36 or H37 supported:** that method is the recommended remedy for known
  shift. ACI additionally assumes online feedback.
- **Both H36 and H37 not supported:** the practical rule stays "calibrate on
  the deployment stream".
