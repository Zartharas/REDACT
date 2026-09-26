# PCCF phase 2 on MEDDOCAN: context-feature nonconformity and two-tier decisions

This follows up `PCCF_FEASIBILITY_RESULTS.md` (phase 1), which found that
the presence mask is informative but that nothing inside a pattern
separates real names from false hits. Phase 2 tests the revision that
report recommended:

- score each candidate with context features built from the failure modes
  already root-caused
- use conformal recall floors for a redaction tier
- use certified precision for an alert tier

Full output: `validation/real_data/pccf_phase2_results.{txt,json}`.

**Short answer:**

- **Redaction tier: works.** On held-out data it gains 33 points of
  precision for 4 points of recall, with recall floors certified per
  pattern and holding across folds.
- **Alert tier: does not yet beat the plain agreement rule.**
- **Caveat:** most of the gain comes from MEDDOCAN's "Label:" field
  structure, not from free-text disambiguation. MEDDOCAN cannot test free
  text: 2,081 of its 2,085 gold names sit on labelled lines.

## What was added (additive only; no existing detector or harness touched)

| file | role |
|---|---|
| `src/pccf_context.py` | Context features plus two scorers: a numpy L2 logistic regression and a fit-free rule scorer. Spanish cue lists were fixed from the train split only. |
| `src/pccf.py` | New option `partition="global"` (default unchanged). |
| `validation/real_data/pccf_phase2_meddocan.py` | Pre-registered hypotheses, a 3-way split, CV, bootstrap, stress test, and mechanical verdicts. |
| `tests/test_pccf.py` | 6 tests, all pass. |
| `output/pccf_meddocan_stress_cache.json` | Perturbed-test layer outputs (offsets and scores only). |

The protocol splits MEDDOCAN three ways: train (210 docs) fits the scorer,
validation (190 docs) sets the thresholds, and test (120 docs) is scored.
The hypotheses were fixed before the first test run. H4 was amended once
before any test run (sampling-noise tolerance); the amendment is recorded
in the harness.

## Within-pattern signal (calibration split, AUROC)

| scorer | dict only | ner only | dict + ner |
|---|---|---|---|
| phase-1 beam confidence | none | 0.61 | 0.59 |
| LR, all features | **0.85** | **0.98** | 0.71 |
| LR, without field labels | 0.69 | 0.89 | 0.63 |
| rule (no fitting) | 0.81 | 0.83 | 0.57 |

The largest LR weights are all readable (positive means more name-like):

| feature | weight |
|---|---|
| name label (`Nombre:`, `Apellidos:`, `Médico:`) | +3.4 |
| place preposition before the span | −2.1 |
| place label (`Localidad/ Provincia:`, `País:`) | −1.8 |
| both layers fired | +1.7 |
| honorific (`Dr.`, `Dra.`) before the span | +1.35 |

## Test split results (120 docs)

| rule | P | R | F1 | F2 |
|---|---|---|---|---|
| naive union | 0.404 | 0.969 | 0.570 | 0.757 |
| intersection (agreement only) | 0.880 | 0.778 | 0.826 | 0.797 |
| phase-1 PCCF, coverage α=0.05 | 0.395 | 0.923 | 0.553 | 0.729 |
| **rule scorer, coverage (any α)** | **0.521** | **0.969** | 0.678 | 0.827 |
| **LR-full, coverage α=0.05** | **0.732** | **0.929** | **0.819** | **0.882** |
| LR-full, coverage α=0.02 | 0.520 | 0.954 | 0.673 | 0.818 |
| LR-noFieldLabel, coverage α=0.05 | 0.442 | 0.938 | 0.601 | 0.766 |
| LR-full, per-pattern precision ≥ 0.85 | 0.882 | 0.778 | 0.827 | 0.797 |
| LR-full, tuned-F1 threshold (no certificate) | 0.888 | 0.836 | 0.861 | 0.846 |

Paired bootstrap for LR-full coverage α=0.05 vs union:
ΔP = [+0.300, +0.358], ΔR = [−0.058, −0.023].

5-fold CV:

- LR-full α=0.05: ΔP = +0.314 ± 0.018, ΔR = −0.030 ± 0.015
- rule scorer: ΔP = +0.115 ± 0.005, ΔR = −0.000 ± 0.001

Guarantees across CV: coverage failed in 1 of 45 (fold, pattern) cells and
precision in 0 of 16.

## Verdicts (computed mechanically by the harness)

| hypothesis | verdict | detail |
|---|---|---|
| H1: redaction tier beats union on precision (≥ +0.05) at recall ≥ 0.92 | **SUPPORTED** | +0.328 P at R 0.929 |
| H2: alert tier beats intersection's recall at precision ≥ 0.85 | **NOT SUPPORTED** | Per-pattern certification never kept a single-layer candidate; the small single-layer pools can't reach a 0.85 lower bound. |
| H3: gain survives without field-label features (≥ 50%) | **NOT SUPPORTED** | Only 12% survives (+0.038 of +0.328). |
| H4: guarantees hold in ≥ 90% of CV cells | **SUPPORTED** | 1 of 61 cells violated. |
| H5: format shift collapses end-to-end recall (expectation) | **NOT SUPPORTED as stated** | Lowercased names: union R 0.508 (NER still finds half). Flattened names (`gabriel.navarro`): union R 0.098, which is the expected collapse. |

**E1, exploratory (added after H2 failed; not pre-registered).** Replacing
the per-pattern precision certificate with one global certificate gives:

- target ≥ 0.80: P 0.836, R 0.898
- target ≥ 0.85: P 0.893, R 0.724, which is worse recall than intersection

A different grid tried off-harness gave P 0.886, R 0.838 at the same
target. The alert tier therefore depends on the testing procedure. Treat
it as unresolved rather than tune it further; more tuning would be a
garden of forking paths.

## What this means (devil's advocate)

1. **Presence-conditioned calibration now has a real, certified redaction
   result.** It gives +33 points of precision with a per-pattern recall
   floor that held out of sample. Phase 1 could not produce this, because
   the layers had no within-pattern signal.
2. **The lever is field context, not free-text understanding.** With field
   labels removed, only 12% of the gain remains. On MEDDOCAN, PERSON is
   effectively a structured-field task (2,081 of 2,085 names are on
   labelled lines). This is good news for REDACT's actual domain:
   key=value telemetry exposes field names natively, which is the same
   idea as `detect.py`'s field-gated paths. It is **not** evidence that
   PCCF disambiguates names in free text, and the paper must not imply it
   is.
3. **The fit-free rule scorer is the strongest practical result.** It adds
   12 points of precision at **zero** measured recall cost (CV:
   −0.000 ± 0.001). It is fully auditable and needs no training data. For
   a redaction pipeline under GDPR Art. 32 / HIPAA §164.312, it is the
   safest default.
4. **The LR tier has a real recall cost.** It still drops 3–4% of real
   names relative to union. Deploying it for redaction means a documented
   residual-risk acceptance. The certificate states that risk per pattern;
   it does not remove it. α=0.02 halves the recall loss and keeps +12
   points of precision.
5. **The guarantees are candidate-level, not end-to-end.** Names that no
   layer emits (72 corpus-wide, and most names under the flattened
   stress test) are outside any fusion guarantee. The stress test confirms
   that format sensitivity is a detector-level problem that fusion cannot
   fix. The project's headline finding stands and should frame the PCCF
   claim, not be displaced by it.
6. **Integrity notes:**
   - Cues came from train inspection only.
   - The H1 bar (+0.05) was set after phase-1 numbers were known, so it is
     modest by construction; the observed +0.33 clears it by a wide
     margin.
   - H4 was amended before any test run.
   - E1 is post-hoc and labelled as such.
   - MEDDOCAN is 52% of the corpus, Spanish, and uses a stand-in
     dictionary layer.

## Recommendation for French/Russian

- Port only the **redaction tier** (conformal recall floor per presence
  pattern, with a context-feature scorer), and start with the **rule
  scorer**. It needs a per-language cue list (field labels, honorifics,
  place prepositions) but no training data. That matters because the
  11/26-document pilots are far too small to fit an LR.
- Before claiming free-text value, measure how much of the FR/RU gold sits
  on labelled lines. If those benchmarks are mostly free-text sentences,
  they are the complementary test MEDDOCAN cannot provide.
- Keep the alert tier out of the paper until a certificate construction is
  fixed in advance and validated on a second corpus.

## Reproduce

```
python validation/real_data/pccf_feasibility_meddocan.py --build-cache     # phase-1 cache, ~105 s
python validation/real_data/pccf_phase2_meddocan.py --build-stress-cache   # ~60 s
python validation/real_data/pccf_phase2_meddocan.py                        # ~150 s
python -m pytest -q tests/test_pccf.py
```
