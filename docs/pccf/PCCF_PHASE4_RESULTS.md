# PCCF phase 4: fixing the observed limitations (F1–F5)

Pre-registered in `PCCF_PHASE4_PREREGISTRATION.md`. Harnesses and outputs
are in `validation/real_data/phase4/`, deliberately outside the Docker
comparison glob. Verdicts are mechanical. Rows tagged *exploratory* were
added after the verdicts were computed and are not judged.

## Scorecard

| fix | verdict | one-line result |
|---|---|---|
| F1 small-group fallback | built + unit-tested | a group with < 19 true calibration candidates now uses the pooled threshold, and the fallback is recorded in `summary()` |
| F2a identity-field policy | **H16 SUPPORTED** | flattened recall in identity fields 0.504 → **1.000** |
| F2b extended segmenter | **H17 NOT SUPPORTED** | recall up but precision down; the post-hoc variant without the ambiguous shape works (see below) |
| F3a per-source alert calibration | **H18 SUPPORTED** | 6 of 6 eligible units ≥ 0.85; small sources abstain |
| F3b online (ACI-style) alert threshold | **H19 SUPPORTED** | 6 of 6 eligible units ≥ 0.83 after burn-in |
| F4 language-agnostic UD features | **H20 NOT SUPPORTED** | recall floors break under wording/syntax shift; works in-distribution on MEDDOCAN (71% of the gain) |
| F5 larger FR/RU data | **H21 NOT SUPPORTED** (FR meets it, RU does not) | FR: LR-UD +0.145 P, floors hold. RU: LR-UD +0.067 P, but the {ner} floor breaks (0.859). The rule and hand-cue scorers hold on both. |

## F2: flattened names (T7 corpora, pooled over 5 datasets × 3 seeds)

| configuration | P | R | field/flat | message/flat |
|---|---|---|---|---|
| union (old flattened layer) | 0.813 | 0.754 | 0.504 | 0.541 |
| union + F2b (all three new shapes) | 0.532 | 0.954 | 0.920 | 0.926 |
| **F2a policy + union (old)** | 0.651 | 0.883 | **1.000** | 0.541 |
| *E4: union + F2b without initial+surname* | **0.829** | 0.837 | 0.673 | **0.704** |
| *E4: F2a policy + union + F2b without initial+surname* | 0.659 | 0.924 | **1.000** | **0.704** |

Hits from each new shape: given+digits 637 true / 0 false; given+surname
193 / 0; **initial+surname 876 / 4,805**. The false positives are
ordinary words with a letter in front: `rhost` (r + Host), `check`,
`flaws`, `using`. Without that shape (E4), message-text flattened recall
rises by **+0.163** and precision *rises* (+0.016). That would have met
H17's bar, but E4 was defined post hoc. The rule it suggests: accept
`initial+surname` only inside identity fields, where F2a already covers
it.

**Real-population check (H17b):** on the real frequency-weighted
SSA/Census `<given><surname>` sample, the extended layer reaches **60.9%
recall vs the old layer's 15.2%**. This is the independent,
non-circular evidence that the segmenter generalizes.

**F2a utility cost:** 2,154 identity-field values that are not gold
names got pseudonymized. Most are service or test accounts: `backup`,
`level6`, `test`, `news`, `cyrus`, and numeric account IDs. A further
3,261 allowlisted system values (`root`, `SYSTEM`, …) stay in the clear.
Under GDPR, an account identifier linkable to a person is personal data,
so pseudonymizing these is defensible. The real cost is forensic
readability, which a per-deployment allowlist tunes. One disclosed fix
was made after the first run: generic keys (`user=`) now take a single
token. H16 was supported both before and after.

## F3: alert tier (T3 corpora; KV-LR scorer)
- **Per-source calibration (F3a):**
  - OpenSSH P 0.94–0.98 and cloudtrail P 1.00, both at R about 0.70–0.75.
  - Linux, Thunderbird and windows_event keep **nothing**: 30% of their
    own lines holds too few true names to certify 0.85. This is valid but
    useless for small sources.
- **Online (F3b):** the threshold adapts from analyst verdicts on alerts.
  - OpenSSH P 0.91–0.96 and cloudtrail P 0.94, each with candidate recall
    ≥ 0.986.
  - Linux, Thunderbird and windows_event produce too few alerts to leave
    the 100-alert burn-in.
- **Diagnosis of the T5 failure:** with the learned structural scorer
  (KV-LR) instead of the coarse KV rule, even the *cross-source*
  certificate no longer failed on Thunderbird (P 1.00, but only 1–4
  kept). The T5 failure came from the rule scorer's coarse score levels
  sitting on the grid edge, compounded by the source shift.
- **Conclusion:** the alert tier is now valid everywhere it acts. It gets
  there by abstaining where evidence is thin, and it is useful only on
  high-volume sources. The online version fits SOC practice, where
  analyst verdicts already exist.

## F4: UD features instead of word lists
- **MEDDOCAN (real text, same distribution for train/calibrate/test):**
  LR-UD gains +0.232 precision (hand cues +0.328, so 71% recovered), with
  per-pattern floors holding (0.970, 0.931, 1.000).
- **T2 (template set A to calibrate, B to test):** LR-UD reaches P 0.988
  but recall falls to 0.741. The floors break ({dict+ner} 0.66, {ner}
  0.84). POS/dependency patterns encode template *syntax*, so they
  overfit set A just as T7's word features overfit wording.
- **The general lesson** from T7 and F4: the more expressive the scorer,
  the more a wording or syntax shift between calibration and deployment
  breaks the recall guarantee. The fix is not a cleverer feature set but
  calibration drawn from the deployment distribution (per-source or online
  recalibration, as F3 does for precision). A recall-side online
  version needs audit labels on *dropped* candidates, for example from a
  small random audit sample. That is the natural next experiment.

## F5: larger FR/RU data (your action)

```
bash validation/real_data/phase4/fetch_large_multilang.sh      # uses the redact-pccf image; writes datasets/large/ only
python validation/real_data/phase4/pccf_phase4_multilang_large.py --build-cache   # repeat until "complete"
python validation/real_data/phase4/pccf_phase4_multilang_large.py
```

The harness was smoke-tested on the pilot files, with the fallback
engaging as designed. Its output from real data will settle H21.

## F5 results: French (3,500 OpenPII-FR docs fetched on the author's Mac)
Split: fit on 1,000 train docs, calibrate on 1,000 train docs, test on
1,500 validation docs. 3,601 gold person spans across the whole file.

| rule (test, 1,500 docs) | P | R |
|---|---|---|
| union | 0.754 | 0.473 |
| rule scorer (FR cues) | 0.754 | 0.473 |
| LR, hand cues | 0.815 | 0.459 |
| **LR-UD (no word lists)** | **0.899** | 0.448 |

- **Floors:** hold in every group with ≥ 19 true candidates.
- **Fallback:** the {dict} and {dict+ner} groups (9 and 28 true names in
  test; fewer in calibration) used the pooled threshold, which is F1
  working as designed.
- **H21 is met for French.** The harness still prints NOT SUPPORTED because
  H21 was registered as covering both languages and Russian is missing.
- **Why UD works here but failed in T2:** calibration and test come from
  the same generator, the in-distribution case where expressive
  features are safe. This is consistent with the T7/F4 lesson.
- **Low recall comes from detection, not fusion.** The FR dictionary layer
  barely fires, and NER misses short, lowercase or unusual names. The
  per-span matching rule (GIVENNAME and SURNAME are separate gold spans,
  but one merged prediction counts once) also depresses recall.

## F5 results: Russian (2,841 pii_benchmark docs, 1,264 gold person spans; 2 texts rebuilt from tokens; 0 span mismatches)
Split: documents 25% fit / 25% calibrate / 50% test (seed 0). Test has
1,421 docs.

| rule (test) | P | R | {ner} recall floor (n_true = 71) |
|---|---|---|---|
| union | 0.679 | 0.424 | n/a |
| rule scorer (RU cues) | 0.691 | 0.420 | 0.986 |
| LR, hand cues | 0.699 | 0.405 | 0.958 |
| LR-UD | **0.747** | 0.390 | **0.859 (FAIL; tolerance line 0.898)** |

**H21 as registered is NOT SUPPORTED.** French meets both parts. Russian
gains precision (+0.067), but LR-UD's floor for the {ner} group fails.
That group had only about 35 true names in calibration, and at that size
the realized coverage of a split-conformal quantile varies a lot from
draw to draw: the guarantee holds in expectation, not per draw. The
simpler scorers kept their floors on both languages but gained only
+0.01–0.06 precision.

**A finding about F1 itself.** Groups that fell back to the pooled
threshold can lose badly on recall. FR {dict} has only 9 true names in
test, and its recall was 0.33 (LR hand cues) and 0.56 (LR-UD). The pooled
threshold is tuned to the majority group, so a minority group inherits a
cut that doesn't suit it. **Recommendation for the redaction tier:** a
group with too little calibration data should fail open (keep everything)
rather than borrow the pooled threshold. Borrowing is acceptable for the
alert tier, where the error that matters is a false alert.

**Russian fetch history:** the first fetch kept 0 of 2,841 rows, because no token was
found in its row's `text` column, so the column layout differs from what
the pilot assumed. The fetcher now:

- rebuilds the text by joining tokens when the search fails (disclosed and
  counted)
- writes `RU_SCHEMA_SAMPLE.json` so the fix can be verified
- has `--only ru` so French is not fetched again

A second bug then surfaced: `tokens` and `ner_tags` are JSON-encoded
*strings*, so the second fetch iterated over characters. The fetcher
now decodes them. The third fetch is clean: 2,841 rows, 2 texts rebuilt
from tokens, 0 span mismatches. None of the bad files was ever used in
a result.

## T6 re-run (with T7 included)
The author's second clean-room Docker run: **PASS**. All 7 result files
(phases 1–3 plus T7) match exactly, with 0 differences of any kind.

## Housekeeping
- `pccf.py` gained a `fallback` field in `summary()`. The committed
  phase-1 JSON, the only one that stores model summaries, has been
  regenerated with it. Its text output is byte-identical.
- Unit tests: 9 of 9 pass.
