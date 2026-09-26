# PCCF phase 9: certified noise-group dropping and the labelling budget (pre-registration)

Written 2026-09-24, **before any phase-9 number was computed.**

- **Data and scoring:** the phase-5 caches, the LR-UD scorer and α = 0.05.
  It covers the 48 eligible rows.
- **Floor:** the phase-6 combined floor.

## Part A: NOISE-DROP (follows from phase 8)
Take each group that PCCF would fail open (< 19 true calibration candidates),
with n calibration candidates of which k are true.

- **Bound:** compute the one-sided Clopper–Pearson upper bound u on the
  group's precision at confidence 1 − δ/m. Here δ = 0.05 and m is the
  number of such groups in the row (Bonferroni).
- **Decision:** if u < 0.05, **drop the whole group** and log the decision,
  meaning the group is certified as ≥ 95 % noise. Otherwise keep it
  (fail open).
- **Minimum size:** groups with n < 30 calibration candidates are always
  kept. With so few candidates, no certificate can be informative.
- Every other group keeps its Mondrian threshold, unchanged.

**H38.** All three parts must hold:

- (a) **Drops are sound:** across all drop decisions, the dropped group's
  *test* precision is ≤ 0.10 in ≥ 90 % of decisions.
- (b) **Recall cost is small:** mean recall change vs PCCF over affected rows
  is ≥ −0.01.
- (c) **The gain is real:** NOISE-DROP raises precision by ≥ +0.03 over PCCF
  in ≥ 25 % of the rows where PCCF fails open on at least one group.

## Part B: the labelling budget
This part measures how many labelled true names a group needs.

- **Splits:** exchangeable pooled splits (fit 1/4, calibration 1/4, test
  1/2), seeds 1–10. This keeps the budget effect separate from shift.
- **Subsampling:** the calibration *documents* are subsampled to fractions
  f ∈ {0.05, 0.1, 0.2, 0.4, 1.0}. The scorer is fixed per seed.
- **Recorded per (row, seed, f, group):** n_cal_true, test recall and
  floor status.
- **Recorded per (row, seed, f):** the row's precision gain over union.

**H39.** Both parts must hold:

- **Floor:** for cells with 100 ≤ n_cal_true, the combined floor holds in
  ≥ 90 % of (cell, seed, f) draws.
- **Gain:** at the smallest fraction where the row's largest group has
  n_cal_true ≥ 100, the median over seeds of the row's precision gain is
  ≥ 80 % of its gain at f = 1.0, in ≥ 80 % of rows. Rows are excluded when
  that group never reaches 100, or when the full gain is < 0.03.

**Descriptive:** floor-hold rate and precision gain by n_cal_true bin
(19–49, 50–99, 100–199, 200–399, 400+). This becomes the practitioner's
"how many names to label" table.

## Interpretation
- **H38 supported:** recommend NOISE-DROP as an opt-in, auditable
  precision mode. It only ever drops groups with a logged certificate.
- **H39:** gives the labelling budget directly.
