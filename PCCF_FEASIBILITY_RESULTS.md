# PCCF feasibility check on MEDDOCAN: results

This runs the check that `ALGORITHM_DESIGN.md` Section 7 proposes. The
question: does presence-conditioned conformal calibration beat naive union
on PERSON precision, using data we have already characterized? That has to
be answered before anything is written against the French/Russian data.

**Short answer: not as designed.** The presence mask carries a very strong
signal. But the conformal rule in Section 6 is a recall guarantee, and the
detection layers give it almost nothing to work with inside a pattern. It
gains about one precision point for eight points of recall. What does beat
union by a lot is dropping the single-layer patterns. That decision is
exactly the one-line rule "keep dictionary+NER agreement only", and it
costs 19 points of recall.

## What was built (additive only)

No existing detector or harness file was modified. `git status` shows no
change to `detect.py`, `es_detect.py`, `es_ner.py` or `evaluate_meddocan.py`.

| file | role |
|---|---|
| `src/pccf.py` | PCCF core: candidates, presence masks, PASC-style max score, Mondrian calibration (optional nested augmentation), coverage mode and precision mode. Dependency-free. |
| `src/ner_confidence.py` | Per-span NER confidence from spaCy's own beam search. Needed because Presidio gives every hit the same score of 0.85 (measured). It does not change which spans NER emits. |
| `validation/real_data/pccf_feasibility_meddocan.py` | The harness. It imports `evaluate_meddocan.py`'s loader, gold mapping and `_dedup` rather than copying or editing them. |
| `validation/real_data/pccf_feasibility_results.{txt,json}` | Full output. |
| `tests/test_pccf.py` | 5 unit tests, all pass. |
| `output/pccf_meddocan_layer_cache.json` | Layer outputs as offsets and scores only, no document text. |

**Sanity check (passes exactly):** the fixed rules reproduce the recorded
numbers span for span on all 520 documents:

- union: TP 2013, FP 3117, FN 72
- dictionary alone: TP 1901, FP 2110, FN 184
- NER alone: TP 1693, FP 1112, FN 392

For PERSON only two layers fire (dictionary and NER), so K = 2 and there
are 3 presence patterns. No regex layer emits PERSON.

## Results

### Presence patterns (all 520 docs)

| pattern | candidates | real PII | precision | NER-confidence AUROC inside the pattern |
|---|---|---|---|---|
| dict only | 2,182 | 332 | **0.152** | none (the dictionary layer is binary) |
| ner only | 1,119 | 112 | **0.100** | 0.606 |
| dict + ner | 1,682 | 1,582 | **0.941** | 0.587 |

The premise holds: which layers fired is highly informative. The signal
inside a pattern, which is what the Section 6 threshold actually uses, is
close to useless.

### Primary protocol: calibrate on train+validation (400 docs), test on test (120 docs)

| rule | P | R | F1 |
|---|---|---|---|
| naive union | 0.404 | 0.969 | 0.570 |
| **PCCF coverage, α=0.10 (design as written), beam confidence** | 0.414 (+0.011) | 0.892 (−0.077) | 0.566 (−0.004) |
| PCCF coverage, any α, Presidio's own score | identical to union | | |
| **PCCF precision mode, target 0.70, non-nested** | 0.880 (+0.477) | 0.778 (−0.191) | 0.826 (+0.256) |
| intersection (agreement only), no calibration | 0.880 | 0.778 | 0.826 |
| NER alone | 0.607 | 0.836 | 0.703 |

Paired bootstrap over the 120 test documents (2,000 resamples) for the two
configurations fixed in advance:

- coverage mode: ΔP = [+0.002, +0.020], ΔR = [−0.100, −0.054], ΔF1 = [−0.016, +0.008]
- precision mode: ΔP = [+0.442, +0.512], ΔR = [−0.229, −0.154]

5-fold document-level CV agrees. Coverage mode at α=0.10: ΔP +0.009 ± 0.006,
ΔR −0.072 ± 0.020. Precision mode: ΔP +0.473 ± 0.042, ΔR −0.207 ± 0.009.

**Guarantees, checked on held-out data:**

- Coverage mode's per-pattern recall floor held at every α. For example, at
  α=0.10 the test recall of true candidates was 0.904, 0.964 and 1.000 per
  pattern.
- Non-nested precision mode's certified precision held: the kept pattern
  scored 0.962 on test.
- Nested augmentation broke the precision certificate. The {ner} pool's
  calibration precision rose from 0.099 to 0.604 once masked {dict+ner}
  candidates were folded in. The pattern was then certified at ≥ 0.60 and
  scored **0.122** on test.

## What this says about the design (devil's advocate)

1. **The Section 6.2 rule controls the wrong error.** A conformal quantile
   over true-PII scores guarantees Pr(kept | real PII, pattern) ≥ 1−α, which
   is a recall floor. It cannot target precision. The guarantee in Section
   6.3, "correctly classified", mixes the two and should be restated.
2. **Section 6.1's "native confidence" premise is false for these layers.**
   The dictionary is binary. Presidio reports a constant 0.85. Even spaCy's
   own beam marginals separate true from false within a pattern only weakly
   (AUROC about 0.6). The PASC max reduction does nothing when one layer's
   score is always 0.
3. **Nested / masked augmentation (CP-MDA-Nested, Fan et al.) does not fit
   this setting, and should be removed from Section 4.5.** Those methods
   assume missing features. A detector that does not fire is not missing
   data: it is a negative vote, and it carries information (missing not at
   random). Masking {dict+ner} down to {ner} imports names that the
   dictionary confirmed into a pool whose real precision is 10%. That is
   how the certificate failed on test.
4. **The win is the partition, not the conformal machinery.** Precision
   mode reproduces the intersection rule span for span. Conformal
   calibration adds an auditable, per-language, finite-sample certificate
   for that choice. That is worth something for a GDPR Art. 32 / NIST
   800-53 evidence trail, but it is not a better detector.
5. **The recall cost is a compliance cost.** Intersection misses 22% of
   real names (444 of 2,085 gold names sit only in single-layer patterns).
   In a redaction pipeline, recall is the regulatory number. The union
   stays the redaction default. A precision-certified tier fits only
   alerting or triage, where false positives cost analyst time.
6. **This may not transfer to REDACT's own domain.** The 0.94 agreement
   precision comes from clinical prose, where both layers see
   space-separated, capitalized names. This project's core format-
   sensitivity finding is that NER largely misses flattened identifiers.
   In telemetry, the {dict+ner} pattern would be starved, and intersection
   would drop most names. Measure per identifier format before claiming
   anything.
7. **Scope caveats:**
   - 52% of MEDDOCAN (520 docs).
   - The dictionary layer is a documented stand-in.
   - Beam decoding sometimes disagrees with the greedy spans Presidio
     emits. The matching rule is in `ner_confidence.py`.
   - The Clopper-Pearson bound treats spans as independent, but spans
     inside one document are correlated.
   - Several configurations were run. Only the two fixed in advance were
     bootstrapped.

## Recommended revision before any French/Russian work

1. Keep the presence mask as the Mondrian partition key. That part is
   confirmed.
2. Take the within-pattern nonconformity from failure-mode context
   features. These are the ones Section 4.5 demoted:
   - for dictionary-only hits: a place-context cue, such as a preceding
     "en"/"de" or a "Localidad:"/"Provincia:" label
   - for NER-only hits: header or eponym cues, such as a line-initial token
     followed by ":", or disease-eponym context

   The recoverable recall is in those two single-layer pools (332 + 112
   real names).
3. Replace the single rule with two certified tiers. The redaction tier
   uses conformal risk control on per-pattern FNR. The alert tier uses
   Learn-then-Test on per-pattern precision. Report both, never one.
4. Drop nested augmentation.
5. The French (11 docs) and Russian (26 docs) pilots cannot calibrate even 3
   patterns to useful bounds. This confirms Section 4, risk 2. Calibration
   needs the larger corpora already named.

## Reproduce

```
python validation/real_data/pccf_feasibility_meddocan.py --build-cache   # ~105 s, needs es_core_news_md
python validation/real_data/pccf_feasibility_meddocan.py                 # ~60 s
python -m pytest -q tests/test_pccf.py
```
