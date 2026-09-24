# PCCF phase 7: is per-group conformal calibration necessary? Baseline comparison (pre-registration)

Written 2026-09-24, **before any phase-7 number was computed.** No new
detection runs: it reuses the phase-5 caches, splits (seed 0) and the LR-UD
scorer exactly.

## Question
A reviewer will ask why PCCF needs conformal calibration per layer-agreement
group, rather than a simpler way of thresholding the same scorer.

## Rows
The 38 eligible rows of phase 5 and amendment 6:

- **18 phase-5 rows:** fr, ru, de, it, nl, pt, fi, tr, hi, te, ar_wiki, ja,
  id, id_hf, ko, zh, in, ko_kdpii.
- **20 wave-3 rows:** tr_mit, ko_klue, bg, pl, cs, lt, et, sv, sk, lv, hu,
  ro, el, da, sl, hr, sr, vi, ms, tl.

## Methods compared
All methods use the same LR-UD scorer, fitted on the fit split, and the
same candidates. Each keeps a candidate when its joint nonconformity
R = 1 − p satisfies R ≤ t.

| id | method | threshold | guarantee |
|---|---|---|---|
| B0 | union | none (keep everything) | trivial |
| B1 | F1-tuned threshold | one t, chosen on the calibration split to maximise candidate-level F1 | none |
| B2 | global split-conformal | one t, α = 0.05, pooled over all groups | marginal recall only |
| B3 | naive classifier | fixed t = 0.5 (p ≥ 0.5) | none |
| PCCF | Mondrian split-conformal | one t per presence-mask group, α = 0.05, fail-open when a group has < 19 true calibration candidates | per-group recall floor |

## Cells and floors
- **Cell:** a (row, group) pair with n_true_test ≥ 19.
- **Floor violation:** candidate recall in the cell < 0.95 − 2·sqrt(α(1−α)(1/n_cal_true + 1/n_test_true)),
  the phase-6 combined floor.
- **Marginal check (B2):** the same formula over all candidates of the row.

## Hypotheses (judged mechanically)
- **H30 (per-group validity needs the Mondrian split).** Both parts must hold:
  - PCCF violates in ≤ 10 % of cells.
  - B1's and B3's violation rates each exceed PCCF's by ≥ 10 percentage points.
- **H31 (a marginal guarantee is not enough).** Both parts must hold:
  - B2 meets its marginal check in ≥ 90 % of rows.
  - B2's per-group violation rate exceeds PCCF's by ≥ 5 percentage points.
- **H32 (descriptive: the price of the guarantee).** Report the mean precision
  and recall difference to union for each method, and the share of rows
  where B1 has both higher precision and lower recall than PCCF.

## Pre-committed interpretation
- **H30 and H31 supported:** per-group conformal calibration is necessary to
  hold recall where it matters. Uncertified thresholds and a single pooled
  guarantee both let recall collapse in some groups.
- **H31 not supported:** in these data, a marginal guarantee happens to
  protect groups too. Say so, and note that the per-group guarantee is still
  a design requirement for redaction, where any group could hold the
  sensitive names.
- **H30's first part fails:** that is a PCCF defect finding and must be
  reported as such.
