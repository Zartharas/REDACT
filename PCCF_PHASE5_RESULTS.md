# PCCF phase 5: all-languages sweep, results

> **Current status (2026-09-24, third run):** 18 eligible language rows, verdicts
> H22 SUPPORTED and H23 SUPPORTED (11/18). Provisional only because ko_legal
> (K-LegalDeID) has no data yet. See *Third run* at the end; earlier sections
> are kept as the record of each wave.

Pre-registration: `PCCF_PHASE5_PREREGISTRATION.md` (3 amendments).
Author's Docker run of 2026-09-24. **First wave only** (fr, ru, id, ko, zh,
in; ko_kdpii and ko_legal had no data). The run used the runner as it
stood before amendment 3, so the second-wave languages were not included.

## Results (redaction tier, α = 0.05, fail-open small groups)

| lang | test docs | union P / R | rule P (Δ) | LR-UD P / R (ΔP) | LR-UD floors |
|---|---|---|---|---|---|
| fr | 1,500 | 0.754 / 0.473 | 0.754 (+0.000) | 0.874 / 0.450 (+0.120) | ok |
| ru | 1,421 | 0.679 / 0.424 | 0.689 (+0.010) | 0.732 / 0.390 (+0.053) | **FAIL** ({ner} 0.86, n = 71) |
| ko (BCCard) | 1,750 | 0.579 / 0.505 | 0.579 (+0.000) | 0.717 / 0.477 (+0.138) | ok |
| zh | 2,000 | 0.856 / 0.922 | **0.921 (+0.065) at 0 recall cost** | 0.973 / 0.877 (+0.118) | ok |
| in (English/Hinglish) | 1,000 | 0.216 / 0.597 | 0.218 (+0.002) | 0.489 / 0.564 (+0.273) | ok |
| id | 221 usable | invalid run (see bugs) | | | |

**Provisional verdicts:** H22 SUPPORTED and H23 SUPPORTED. The eligible
languages were fr, ru, ko, zh and in; LR-UD was useful and valid in fr,
ko, zh and in.

**H24 (fallback) is sharpest on India.** The en_IN name dictionary
collides with ordinary words and places, because Faker's en_IN list
contains real surnames such as "Date", "Master", "Abha" and "Nagar". That
produces 564 dictionary-only candidates in test, of which 3 are real.

- **Fail-open** (the pre-registered choice) keeps that group: precision
  0.489, and the 3 real names are kept.
- **Borrowing the pooled threshold** drops the group: precision 0.945, but
  recall in that group falls to 0.00.

Both results are correct. This is the recall-vs-precision trade in its
purest form: fail-open stays the redaction default, and the alert tier
can borrow the pooled threshold. The same collision (a real name that is
also a common word or place) is what phase 1 found for Spanish place
names. It is a property of dictionary layers, so it was not "fixed" by
tuning the list.

## Bugs found by this run (fixed; they need the next run)
1. **Indonesian: 3,061 of 3,500 documents errored.**
   `pccf_ud.ud_features` called `float(None)`: the multilingual
   `xx_ent_wiki_sm` model has no sentence boundaries, so `is_sent_start`
   is None. Fixed with `bool(...)`, which leaves every model that has a
   parser unchanged. The same fix covers ar, tr, hi and te, which parse
   with xx.
2. **KDPII: 0 of 50,011 units parsed.** Its spans live under `PII_set`,
   with `begin`/`end`/`form`/`label`, and the key list did not include
   `PII_set`. It was found from `SCHEMA_SAMPLE_ko_kdpii.json`; the fixed
   parser was checked on the real sample rows. The runner re-downloads it
   automatically because the file is empty.

## Still to run
- Indonesian (bug fixed) and KDPII (parser fixed).
- The second wave: de, it, nl, pt, fi, ja, tr, hi, te, ar (amendment 3).
- K-LegalDeID, once the authors reply.


## Second-wave run (author's Docker run, 2026-09-24; all keys except ar, tr and ko_legal)
| lang | union P / R | rule ΔP | LR-UD P / R (ΔP) | LR-UD floors | note |
|---|---|---|---|---|---|
| de | 0.923 / 0.559 | +0.000 | 0.951 / 0.536 (+0.028) | ok | union already precise, little room left |
| it | 0.783 / 0.467 | +0.000 | 0.897 / 0.436 (+0.115) | **FAIL** ({ner} 0.93, n = 698, just under the 0.934 line) | |
| nl | 0.820 / 0.416 | +0.000 | 0.895 / 0.396 (+0.075) | ok | |
| pt | 0.504 / 0.416 | −0.007 | 0.619 / 0.393 (+0.115) | ok | fail-open keeps 455 false dictionary hits; the global fallback reaches 0.911 |
| fi | 0.656 / 0.360 | −0.004 | 0.855 / 0.346 (+0.199) | ok | |
| ja | 0.690 / 0.412 | −0.006 | 0.794 / 0.397 (+0.104) | ok | dictionary layer had 0 hits (repaired in amendment 4) |
| id | 0.197 / 0.493 | +0.000 | 0.206 / 0.484 (+0.009) | ok | xx NER floods the {ner} group (P 0.118); IndoBERT row added |
| ko_kdpii | 0.394 / 0.477 | +0.029 | 0.437 / 0.455 (+0.043) | ok | dialogue; gold includes PS_ID/PS_NICKNAME |
| hi, te | n/a | | | | all docs failed: gated IndicNER (fixed in amendment 4) |

Earlier languages are unchanged: fr, ru, ko, zh, in.

**Provisional verdicts (13 eligible languages):**

- **H22 SUPPORTED:** the rule scorer's floors hold in all 13.
- **H23 SUPPORTED:** LR-UD is useful and valid in 9 of 13 (fr, nl, pt,
  fi, ja, ko, zh, in, ko_kdpii). It failed on ru and it (floor), and on de
  and id (gain < +0.03).

**Emerging pattern:** the LR-UD floor failures (ru, it) sit in the large
{ner} group, and both are borderline misses of the sampling-tolerance line
rather than collapses. The fail-open vs global trade (pt, in, fi, nl, fr)
repeats wherever a dictionary layer collides with common words.

## Amendment-4 fixes (applied; all ran in the third run below)
- ar and tr: new open sources.
- hi and te: ungated NER plus a Latin-name dictionary.
- ja: dictionary repaired.
- id_hf: IndoBERT row added.


## Third run: all languages (author's Docker run, 2026-09-24; after amendments 4 and 5)

All 19 data-bearing rows cached with 0 errors (fr, ru, de, it, nl, pt, fi,
tr, hi, te, ar, ar_wiki, ja, id, id_hf, ko, zh, in, ko_kdpii). ko_legal: no
data file. Output: `validation/real_data/phase5/docker_run/`.

| lang | union P / R | rule ΔP (floors) | LR-UD ΔP / ΔR (floors) | LR-UD global-fallback ΔP (floors) | H23 status |
|---|---|---|---|---|---|
| fr | 0.754 / 0.473 | +0.000 (ok) | +0.120 / -0.024 (ok) | +0.145 (ok) | useful+valid |
| ru | 0.679 / 0.424 | +0.010 (ok) | +0.053 / -0.034 (**FAIL**) | +0.067 (**FAIL**) | floor fail |
| de | 0.923 / 0.559 | +0.000 (ok) | +0.028 / -0.023 (ok) | +0.047 (ok) | gain < 0.03 |
| it | 0.783 / 0.467 | +0.000 (ok) | +0.115 / -0.031 (**FAIL**) | +0.144 (**FAIL**) | floor fail |
| nl | 0.820 / 0.416 | +0.000 (ok) | +0.075 / -0.019 (ok) | +0.100 (ok) | useful+valid |
| pt | 0.504 / 0.416 | -0.007 (ok) | +0.115 / -0.022 (ok) | +0.407 (ok) | useful+valid |
| fi | 0.656 / 0.360 | -0.004 (ok) | +0.199 / -0.014 (ok) | +0.270 (ok) | useful+valid |
| tr | 0.602 / 0.908 | +0.070 (ok) | +0.157 / -0.009 (ok) | +0.157 (ok) | useful+valid |
| hi | 0.816 / 0.759 | +0.000 (ok) | +0.033 / -0.022 (**FAIL**) | +0.093 (**FAIL**) | floor fail |
| te | 0.803 / 0.654 | +0.000 (ok) | +0.024 / -0.017 (ok) | +0.048 (**FAIL**) | gain < 0.03 |
| ar | 0.000 / 0.000 | – | – | – | not eligible (no PERSON gold) |
| ar_wiki | 0.509 / 0.842 | +0.003 (ok) | +0.037 / -0.023 (ok) | +0.074 (ok) | useful+valid |
| ja | 0.619 / 0.448 | -0.002 (ok) | +0.083 / -0.019 (ok) | +0.083 (ok) | useful+valid |
| id | 0.197 / 0.493 | +0.000 (ok) | +0.009 / -0.009 (ok) | +0.009 (ok) | gain < 0.03 |
| id_hf | 0.689 / 0.590 | +0.000 (ok) | +0.058 / -0.031 (**FAIL**) | +0.058 (**FAIL**) | floor fail |
| ko | 0.579 / 0.505 | +0.000 (ok) | +0.138 / -0.028 (ok) | +0.138 (ok) | useful+valid |
| zh | 0.856 / 0.922 | +0.065 (ok) | +0.118 / -0.045 (ok) | +0.118 (ok) | useful+valid |
| in | 0.216 / 0.597 | +0.002 (ok) | +0.273 / -0.033 (ok) | +0.728 (ok) | useful+valid |
| ko_kdpii | 0.394 / 0.477 | +0.029 (ok) | +0.043 / -0.022 (ok) | +0.043 (ok) | useful+valid |

`ar` (sitr-arabic-pii) has no person-name label, so it is reported but not
eligible. `ar_wiki` is the WikiANN-ar stand-in from amendment 5 (short
Wikipedia fragments with silver labels), not PII-style text.

**Mechanical verdicts (18 eligible rows):**

- **H22 SUPPORTED.** The rule scorer's per-group floors hold in all 18.
- **H23 SUPPORTED.** LR-UD is useful (≥ +0.03 P) and valid in **11 of 18**:
  fr, nl, pt, fi, tr, ar_wiki, ja, ko, zh, in, ko_kdpii. It fails the
  floor in 4 (ru, it, hi, id_hf) and has a gain below +0.03 in 3 (de, te, id).
- The verdicts stay PROVISIONAL until ko_legal runs. One more row cannot flip
  H22 (the rule scorer has not failed a floor anywhere), and H23 needs 9 of 19,
  so it cannot flip either.

**Every LR-UD floor failure is in the {ner}-only group:**

- ru {ner}: recall 0.859 against a floor of 0.898 (n_true 71). This is the one clear miss.
- it {ner}: 0.931 against 0.934 (n_true 698).
- hi {ner}: 0.932 against 0.937 (n_true 1,146).
- id_hf {ner}: 0.930 against 0.935 (n_true 896).

Three of the four miss by ≤ 0.005. The pattern is the phase-3 lesson again:
the fit/calibration split and the test split differ enough (ai4privacy
train vs validation splits) that a lexical/structural scorer's quantile
drifts slightly. Recommended action: report it as it is. Do not tune α or
the tolerance after the fact.

**New findings in this run:**

1. **Turkish is the strongest new result.** Union recall is 0.908, and LR-UD
   adds +0.157 P for a −0.009 R cost. Floors hold in all three groups, and
   each group has ≥ 147 true names, so no group fell back.
2. **The NER layer's quality matters more than the scorer.** For id → id_hf,
   swapping the xx NER for IndoBERT moves the union from P 0.197 / R 0.493 to
   0.689 / 0.590 before any calibration. PCCF cannot rescue a layer with
   P 0.118 (id: +0.009).
3. **H24, fail-open vs global fallback:** the global fallback raises precision
   everywhere (pt +0.407, in +0.728), but it removes the small groups'
   recall: te {dict} 0.11, and pt/nl/fi/in {dict} 0.00. te's global
   variant fails its floor. Fail-open stays the redaction default, and the
   global variant is only acceptable for an alert tier.
4. **Collisions between dictionary names and common words** (pt 457 {dict} candidates with 2
   true; in 564 with 3) are the main source of precision loss that LR-UD
   recovers. This is the same finding as the second wave.
5. **Engineering (amendment 5):** the HF pipeline was rebuilt for every
   document (no protobuf; lru_cache does not cache exceptions), caches were
   lost between Docker runs, and two concurrent runs overwrote each other's
   caches. All are fixed and guarded. The trailing `ENV}: command not found`
   in the log came from editing `run_all_languages.sh` while bash was still
   reading it. It has no effect on the results, which had already been copied out.

**Follow-up (phase 6, pre-registered):** the four LR-UD floor misses above were
diagnosed in `PCCF_PHASE6_RESULTS.md`. it, hi and id_hf are train→validation
split shift; ru is a chance tail draw; no method defect was found. The phase-5
verdicts are unchanged.


## Wave 3 + tr_mit + ko_klue (amendment 6; author's Docker run, 2026-09-24)

All 20 new rows cached with 0 errors. The 20 original rows reproduced
unchanged (H22 and H23 are still SUPPORTED, identical table). The new rows
are judged separately, with the phase-6 combined-variance floor ("old" = the
phase-5 floor, shown for comparison only).

| row | union P / R | rule ΔP (floors) | LR-UD ΔP / ΔR (floors) | global-fallback ΔP | H29 status |
|---|---|---|---|---|---|
| tr_mit | 0.460 / 0.927 | +0.013 (ok) | +0.161 / -0.019 (ok; old ok) | +0.161 | useful+valid |
| ko_klue | 0.896 / 0.825 | +0.002 (ok) | +0.005 / -0.035 (ok; old ok) | +0.005 | gain < 0.03 |
| bg | 0.476 / 0.457 | +0.000 (ok) | +0.131 / -0.028 (ok; old ok) | +0.162 | useful+valid |
| pl | 0.649 / 0.474 | +0.000 (ok) | +0.210 / -0.019 (ok; old ok) | +0.219 | useful+valid |
| cs | 0.388 / 0.444 | +0.000 (ok) | +0.107 / -0.020 (ok; old ok) | +0.111 | useful+valid |
| lt | 0.656 / 0.418 | +0.163 (ok) | +0.177 / -0.025 (ok; old ok) | +0.177 | useful+valid |
| et | 0.418 / 0.421 | +0.000 (ok) | +0.113 / -0.026 (ok; old ok) | +0.123 | useful+valid |
| sv | 0.683 / 0.352 | +0.066 (ok) | +0.132 / -0.014 (ok; old ok) | +0.207 | useful+valid |
| sk | 0.414 / 0.422 | +0.000 (ok) | +0.089 / -0.017 (ok; old ok) | +0.112 | useful+valid |
| lv | 0.369 / 0.428 | +0.000 (ok) | +0.103 / -0.016 (ok; old ok) | +0.103 | useful+valid |
| hu | 0.356 / 0.436 | +0.000 (ok) | +0.093 / -0.016 (ok; old ok) | +0.125 | useful+valid |
| ro | 0.437 / 0.360 | +0.000 (ok) | +0.235 / -0.016 (ok; old ok) | +0.449 | useful+valid |
| el | 0.627 / 0.297 | -0.003 (ok) | +0.103 / -0.019 (ok; old miss) | +0.301 | useful+valid |
| da | 0.699 / 0.385 | +0.063 (ok) | +0.147 / -0.022 (ok; old ok) | +0.233 | useful+valid |
| sl | 0.699 / 0.440 | +0.008 (ok) | +0.216 / -0.022 (ok; old ok) | +0.226 | useful+valid |
| hr | 0.608 / 0.482 | +0.000 (ok) | +0.233 / -0.028 (ok; old ok) | +0.263 | useful+valid |
| sr | 0.319 / 0.400 | +0.000 (ok) | +0.076 / -0.023 (ok; old ok) | +0.076 | useful+valid |
| vi | 0.197 / 0.361 | +0.000 (ok) | +0.024 / -0.010 (ok; old ok) | +0.024 | gain < 0.03 |
| ms | 0.197 / 0.433 | +0.000 (ok) | +0.022 / -0.013 (ok; old ok) | +0.022 | gain < 0.03 |
| tl | 0.438 / 0.558 | +0.000 (ok) | +0.000 / -0.013 (ok; old ok) | +0.000 | gain < 0.03 |

**Mechanical verdicts (20 eligible rows):**

- **H28 SUPPORTED.** The rule scorer's combined floors hold in all 20.
- **H29 SUPPORTED.** LR-UD is useful and valid in **16 of 20**. It gains
  less than +0.03 in ko_klue, vi, ms and tl. No row misses the combined
  floor.

**Findings:**

1. **Largest gains yet:** ro +0.235, hr +0.233, sl +0.216, pl +0.210,
   lt +0.177. Each costs ≤ 0.028 recall. Most wave-3 rows have one eligible
   group ({ner}), so the gain is LR-UD reordering NER hits by their
   syntactic context.
2. **The forward floor matters.** el LR-UD and lt rule miss the old phase-5
   floor but pass the combined one, which is the phase-6 prediction working
   as intended. Both are reported, not re-scored.
3. **The multilingual NER lesson repeats.** vi and ms use xx_ent_wiki_sm,
   whose {ner} group has P 0.159 and 0.162. Their union P is 0.197 and
   LR-UD cannot lift it past +0.03, the same pattern as `id`. tl gains
   nothing: its dictionary (es_ES + en_US names) dominates, and the {ner}
   group has P 0.223.
4. **Faker dictionaries mostly miss ai4privacy names.** For bg, hu, el and
   ro, the dict and dict+ner groups hold 0 true names in test. For
   pl, cs, lt, lv and sl, they hold ≤ 8. In these languages the
   dictionary layer adds almost nothing, and the result is effectively
   single-layer PCCF.
5. **ko_klue.** The union is already P 0.896 / R 0.825 on KLUE's news text,
   so there is little to recover (+0.005). The row does show that the
   Korean pipeline behaves on openly licensed data.
6. **tr_mit vs tr (licensing).** On the same data and test split:

   | model | LR-UD P | LR-UD R | F1 |
   |---|---|---|---|
   | MIT model | 0.621 | 0.909 | ≈ 0.74 |
   | unlicensed savasy | 0.759 | 0.900 | ≈ 0.82 |

   The MIT model finds more names (union R 0.927 vs 0.908) but also far
   more false candidates ({ner} hits 14,259 vs 8,806). Amendment 6 did not
   define "comparable" mechanically, so this is a judgement: **the MIT row is
   weaker on precision.** Recommendation: publish tr_mit as the licensed
   Turkish row, and report tr (savasy) only as a research comparison unless
   a licence is obtained.
7. **H24 again.** The global fallback's precision (ro +0.449, el +0.301)
   comes from dropping small-group recall. Fail-open stays the redaction
   default.


## Wave 4: GLiNER NER for the ten xx rows (amendment 7; author's Docker run, 2026-09-24)

All ten `_gl` rows were cached with 0 errors, at about 4.4 docs/s on CPU (vi
2.8 docs/s). All earlier rows reproduced unchanged: H22, H23, H28 and H29 are
still SUPPORTED.

| lang | union P / R, xx NER | union P / R, GLiNER | LR-UD P on GLiNER (ΔP / ΔR) | combined floor |
|---|---|---|---|---|
| bg | 0.476 / 0.457 | **0.710 / 0.607** | 0.863 (+0.153 / −0.031) | ok |
| cs | 0.388 / 0.444 | **0.767 / 0.622** | 0.927 (+0.160 / −0.037) | ok (old floor: miss) |
| et | 0.418 / 0.421 | **0.730 / 0.585** | 0.904 (+0.174 / −0.029) | ok |
| sk | 0.414 / 0.422 | **0.746 / 0.594** | 0.867 (+0.121 / −0.023) | ok |
| lv | 0.369 / 0.428 | **0.743 / 0.614** | 0.929 (+0.186 / −0.032) | ok |
| hu | 0.356 / 0.436 | **0.696 / 0.562** | 0.818 (+0.122 / −0.032) | ok |
| sr | 0.319 / 0.400 | **0.775 / 0.616** | 0.913 (+0.138 / −0.036) | ok |
| vi | 0.197 / 0.361 | **0.643 / 0.568** | 0.738 (+0.095 / −0.022) | ok |
| ms | 0.197 / 0.433 | **0.664 / 0.596** | 0.867 (+0.203 / −0.028) | ok |
| tl | 0.438 / 0.558 | **0.616 / 0.622** | 0.717 (+0.101 / −0.034) | **miss** ({ner} 0.91, n = 550) |

**Mechanical verdicts:**

- **H33 SUPPORTED (10/10).** Union F1 improves in every language. Precision
  and recall both rise, e.g. vi 0.197/0.361 → 0.643/0.568 and sr 0.319 →
  0.775 precision.
- **H34 SUPPORTED.** The rule scorer's combined floors hold in all 10.
  LR-UD is useful and valid in 9 of 10. vi and ms, which PCCF could not help
  with the xx model (+0.024 / +0.022), now gain +0.095 and +0.203.

**Findings:**

1. **This confirms the phase-5 lesson directly.** PCCF needs a usable NER
   layer. With one, every one of these languages reaches LR-UD precision of
   0.74–0.93, against 0.22–0.61 before.
2. **The tl_gl floor miss is split shift.** An *exploratory* re-run of the
   phase-6 diagnostic, not pre-registered, gave {ner} recall 0.951 ± 0.002
   under pooled splits and 0.929 ± 0.002 under the original train→validation
   protocol. That is the same signature as it, hi and id_hf.
3. **Rule scorer on et_gl and sk_gl.** It costs about 0.035 recall for no
   precision gain. It stays within its floor, but here the rule scorer's
   cues do not suit GLiNER's candidates.
4. **Cost.** GLiNER runs at about 4 docs/s on CPU, about 10× slower than
   spaCy md. That matters for telemetry throughput.
