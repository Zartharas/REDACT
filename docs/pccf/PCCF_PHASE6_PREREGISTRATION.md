# PCCF phase 6: why did LR-UD miss its recall floor in 4 of 18 languages? (pre-registration)

Written 2026-09-24, **before any phase-6 number was computed.** Phase-5
verdicts (H22, H23) stand as recorded and are not re-judged here.

## Question
In phase 5, LR-UD missed the per-group recall floor in ru, it, hi and id_hf,
always in the {ner} group:

| lang | recall | floor |
|---|---|---|
| ru | 0.859 | 0.898 |
| it | 0.931 | 0.934 |
| hi | 0.932 | 0.937 |
| id_hf | 0.930 | 0.935 |

There are three candidate explanations:

1. **Split shift.** For 13 rows, fit and calibration come from the dataset's
   *train* split, while test comes from *validation*/*test*. That breaks
   exchangeability.
2. **Sampling chance.** The phase-5 tolerance `2·sqrt(α(1−α)/n_test_true)`
   ignores the randomness of the calibration set, which has about the same
   size. Across about 45 group checks, several misses are then expected even
   when coverage is exact.
3. **A method defect** (quantile, grouping or fit/cal leakage).

## Design (no new detection runs; reuses the phase-5 caches exactly)
- **Rows:**
  - The 18 eligible phase-5 rows.
  - The 13 shifted-split rows are fr, de, it, nl, pt, fi, ja, hi, te, id,
    id_hf, ko_kdpii and ar_wiki.
  - The 5 single-split rows are ru, tr, ko, zh and in. They are already
    exchangeable and act as the negative control; **ru is one of the misses
    and sits in this control set**.
- **Scorers:** LR-UD and the rule scorer (control). Settings are identical to
  phase 5: α = 0.05, fail-open, min_group_true 19.
- **E1 (pooled, exchangeable):** for seeds 1–50, shuffle all rows and split
  them 1/4 fit, 1/4 cal, 1/2 test, ignoring the dataset's own splits.
- **E2 (original protocol):** phase 5's `splits()` for seeds 1–50. Only the
  shuffle seed changes; test stays the non-train split where one exists.
- **Recorded per (seed, language, group):** n_true in cal, n_true in test,
  and test recall.
- **Eligible group:** mean n_true_test ≥ 19 across seeds.

## Hypotheses (judged mechanically)
- **H25 (validity under exchangeability).** In E1, the 50-seed mean LR-UD
  recall is ≥ 0.95 − 2·SE_mean in **every** eligible (language, group), where
  SE_mean is the seed SD / √50. The same check is also reported for the rule
  scorer.
- **H26 (split shift explains the shifted-row misses).** For it, hi and
  id_hf, the E2 mean {ner} recall is below the E1 mean by more than
  2·sqrt(SE_E1² + SE_E2²) in **at least 2 of 3**.
- **H27 (the phase-5 tolerance was too tight).** Across all E1 (seed,
  language, group) LR-UD checks, take the fraction that miss the phase-5
  criterion. It must be (a) above the nominal one-sided 2.3 %, and (b) within
  2 binomial SEs of, or below, the rate predicted by the combined-variance
  model. That model gives each check the miss probability
  Φ(−2·sqrt(1/n_test) / sqrt(1/n_cal + 1/n_test)), averaged over checks.

## Pre-committed interpretation
- **H25 NOT SUPPORTED:** a real method defect. Stop and inspect the
  quantile/grouping/leakage code before any write-up.
- **H25 SUPPORTED, H26 SUPPORTED:** the shifted-row misses are split shift.
  Calibrate in-distribution, which is the phase-3 lesson, now quantified.
- **H25 SUPPORTED, H26 NOT SUPPORTED:** the misses are consistent with
  chance, and H27 decides whether the tolerance was the problem.
- **ru is judged only through H25/H27.** It has no split shift, so a shift
  explanation cannot be claimed for it.
- **Forward-only rule:** if H27 is supported, **future** phases use the floor
  `0.95 − 2·sqrt(α(1−α)(1/n_cal_true + 1/n_test_true))`. Phase-5 results are
  **not** re-scored with it, except in a clearly labelled sensitivity table.
