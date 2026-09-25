# PCCF: one-page results index (phases 1–11)

*Index as of 2026-09-24, branch `pccf-research`. Every verdict below is
mechanical and pre-registered unless it is marked* exploratory. *Phases
6–10A use the Docker reference results (`PCCF_REPRODUCIBILITY.md`). The
manuscript draft in `Academic Documentation/PCCF/` predates that switch; its
figures differ by ≤ 0.002.*

## What PCCF is
PCCF is a filter for multi-layer PII detection.

1. Candidates are grouped by which detection layers fired (the presence
   mask).
2. A scorer rates each candidate.
3. A Mondrian split-conformal threshold per group keeps each group's true
   names with probability ≥ 1 − α.
4. Groups with < 19 true calibration names fail open.

Code: `src/pccf.py`.

## Verdicts

| # | phase | claim (short) | verdict | key number | file |
|---|---|---|---|---|---|
| — | 1 | Presence patterns separate precision regimes (MEDDOCAN) | descriptive | {dict+ner} 0.94 vs {dict} 0.15 / {ner} 0.10 | PCCF_FEASIBILITY_RESULTS.md |
| H1 | 2 | Redaction tier: LR raises precision at ≥ 0.92 recall | ✅ | P 0.404 → 0.732, R 0.929 | PCCF_PHASE2_RESULTS.md |
| H2 | 2 | Alert tier beats the intersection on recall | ❌ | | PCCF_PHASE2_RESULTS.md |
| H3 | 2 | Gain survives without the form-label cue | ❌ | +0.038 only (template-driven) | PCCF_PHASE2_RESULTS.md |
| H4 | 2 | Guarantees hold in 5-fold CV | ✅ | 1/45 cells | PCCF_PHASE2_RESULTS.md |
| H5 | 2 | (expectation) stress collapses candidates | ❌ | | PCCF_PHASE2_RESULTS.md |
| H6/H7 | 3 | Spanish free text: +P and floors hold | ✅ | +0.252 P, 0 recall cost | PCCF_PHASE3_RESULTS.md |
| H8 | 3 | Log telemetry (fields): KV rule +P | ✅ | +0.197 P, −0.010 R | PCCF_PHASE3_RESULTS.md |
| H10 | 3 | MEDDOCAN thresholds transfer to FR/RU (small n) | ✅ | | PCCF_PHASE3_RESULTS.md |
| H11a/b | 3 | Pooled alert tier across sources | ❌ | 10/15 units | PCCF_PHASE3_RESULTS.md |
| H12–H14 | 3b | Names in log message text | ❌ | realistic gain about +0.04 | PCCF_PHASE3B_MESSAGE_TEXT_RESULTS.md |
| H16 | 4 | Identity-field policy fixes flattened names | ✅ | field recall 0.504 → 1.000 | PCCF_PHASE4_RESULTS.md |
| H17 | 4 | Real-name segmenter | ❌ | initial+surname too ambiguous | PCCF_PHASE4_RESULTS.md |
| H18/H19 | 4 | Per-source / online alert calibration | ✅ | 6/6 units | PCCF_PHASE4_RESULTS.md |
| H20/H21 | 4 | UD features / larger FR-RU under shift | ❌ | floors break under shift | PCCF_PHASE4_RESULTS.md |
| H22 | 5 | Rule-scorer floors hold in every language | ✅ | 18/18 rows | PCCF_PHASE5_RESULTS.md |
| H23 | 5 | LR-UD useful and valid in ≥ half of rows | ✅ | 11/18 | PCCF_PHASE5_RESULTS.md |
| H24 | 5 | (descriptive) fail-open vs global fallback | — | global erases small-group recall | PCCF_PHASE5_RESULTS.md |
| H25 | 6 | No method defect under exchangeable splits | ✅ | all cells | PCCF_PHASE6_RESULTS.md |
| H26 | 6 | The misses are train→validation shift | ✅ | it −0.023, hi −0.007, id_hf −0.008 | PCCF_PHASE6_RESULTS.md |
| H27 | 6 | The phase-5 tolerance was too tight | ✅ | 8.4 % chance misses, not 2.3 % | PCCF_PHASE6_RESULTS.md |
| H28/H29 | 5 (am. 6) | 20 more rows: floors hold; LR-UD useful | ✅ | 20/20; 16/20 | PCCF_PHASE5_RESULTS.md |
| H30/H31 | 7 / 7b | Per-group calibration beats simpler thresholds | ✅ | violations 2.4 % vs 13.1 / 34.5 / 34.5 % (48 rows) | PCCF_PHASE7(B)_RESULTS.md |
| H33/H34 | 5 (am. 7) | GLiNER NER improves weak languages; PCCF works on top | ✅ | 10/10; 9/10 | PCCF_PHASE5_RESULTS.md |
| H35 | 8 | Clustering small groups recovers fail-open precision | ❌ | clusters never reach 19 true | PCCF_PHASE8_RESULTS.md |
| H36 | 8 | Weighted conformal fixes shift | ❌ | | PCCF_PHASE8_RESULTS.md |
| H37 | 8 | Adaptive conformal (ACI) fixes shift | ✅ | 0/63 violations, ΔP ≈ 0 | PCCF_PHASE8_RESULTS.md |
| H38 | 9 | Certified noise-group dropping | ❌ (c) | sound 5/5, but fires in only 5/36 rows | PCCF_PHASE9_RESULTS.md |
| H39 | 9 | Labelling budget of about 100 true names per group | ✅ | floor 96.9 %; 34/38 rows ≥ 80 % of gain | PCCF_PHASE9_RESULTS.md |
| H40 | 10A | ACI with 10 % sampled audits on telemetry | ✅ (modest) | violations 3 → 2 (0 with full feedback) | PCCF_PHASE10A_RESULTS.md |
| — | 10B | Throughput (descriptive) | — | PCCF filter ≈ 2 vCPU-s per 1M lines; NER dominates | PCCF_PHASE10B_RESULTS.md |
| H41 | 11 | Rule floors hold on real documents | ✅ | 8/8 (TAB, BTC, WNUT-17, GermEval, FactRuEval) | PCCF_PHASE11_RESULTS.md |
| H42 | 11 | LR-UD useful and valid on real documents | ✅ | 6/8 (wnut_redact: shift miss, ACI fixes; factrueval: gain +0.012) | PCCF_PHASE11_RESULTS.md |
| H43 | 11 | TAB direct identifiers kept once proposed | ✅ | 0.978 / 0.980 of covered (end-to-end 0.944 / 0.955) | PCCF_PHASE11_RESULTS.md |

*Exploratory, not judged:* the phase-2 global alert certificate (E1); the
T7 E2/E3 rows; the H17b real-name population; the tl_gl split diagnostic
(0.951 pooled vs 0.929 original).

## Practitioner rules (supported by the rows above)
1. **Condition on layer agreement.** It separates precision regimes more
   sharply than any single layer's confidence does.
2. **Use a per-group guarantee, not a pooled one.** A pooled guarantee holds
   on average while individual groups collapse (7/7b).
3. **Calibrate on the deployment stream.** If that is impossible, run ACI on
   analyst feedback (6, 8, 10A).
4. **Label about 100 true names per active group** (400+ for a tight worst
   case) (9).
5. **Fail open on small groups by default.** Drop a group only when a
   precision certificate proves it is noise, and log the decision (8, 9).
6. **Fix a weak NER layer first,** because PCCF cannot create signal (5,
   amendment 7). Transformer NER costs 17–50× spaCy per thread (10B).
7. **Handle flattened identifiers by field policy,** not free-text
   segmentation (4).

## Main limitations
- **Scope of the guarantee:** per candidate and marginal. It does not cover
  names no layer proposes, and it breaks under shift.
- **Data realism:** most multilingual rows are synthetic (ai4privacy). Real
  documents: MEDDOCAN, KDPII dialogue, WikiANN/KLUE proxies, and (phase 11)
  TAB court judgments, BTC and WNUT-17 tweets, GermEval and FactRuEval news.
  Enron email and ANERcorp are not yet run.
- **Licensing:** the Turkish savasy model has no licence. The MIT
  replacement (`tr_mit`) is weaker.
- **Numeric sensitivity:** cross-platform floating point moves single cells
  by about 0.01 (up to 0.06 for re-tuned thresholds).
