# PCCF phase 5: all-languages sweep (pre-registration)

Written 2026-09-24 before any phase-5 data was fetched or run.

**Authorization and scope:** the author authorized this sweep explicitly
("add all the languages we are targeting"), which closes the
`LANGUAGE_EXTENSION.md` go/no-go for these candidates. Scope is the
shortlist plus the researched candidates with permissive licenses:

| lang | source (license) | PERSON layers used here |
|---|---|---|
| fr | ai4privacy open-pii-masking-500k (CC BY 4.0) | fr_detect dictionary + fr_ner (Presidio, fr_core_news_md) |
| ru | redmadrobot pii_benchmark (MIT) | ru_detect dictionary (lemmatized) + ru_ner (Presidio, ru_core_news_md) |
| id | ai4privacy pii-masking-openpii-1.5m (CC BY 4.0) | id_detect dictionary + spaCy xx_ent_wiki_sm (NOT IndoBERT; its Hugging Face download is blocked in Claude workspaces) |
| ko | BCCard/privacy-filter-openpii-masking (CC BY 4.0, derived from ai4privacy) | Faker ko_KR surname + given name at the start of a Hangul run + spaCy ko_core_news_md |
| zh | wan9yu/pii-bench-zh (Apache-2.0) | Faker zh_CN surname + given name within Han runs + spaCy zh_core_web_md |
| in | maskflow-ai/indiapii-bench (CC BY 4.0), **English/Hinglish, not Hindi** | Faker en_IN names over capitalized runs + spaCy en_core_web_lg |

**Excluded, deliberately:**

- Hindi-script HiNER: CC BY-SA would be a new share-alike obligation, and
  its annotation is general NER rather than PII.
- K-LegalDeID: CC BY-NC-SA.
- Japanese, Arabic, Cantonese: out of scope per `LANGUAGE_EXTENSION.md`.

The id/ko/zh/in layers in `src/lang_layers.py` are **uniform baseline
layers**, not tuned language modules. Their detection recall is a
property of those baselines and says nothing about PCCF.

**Protocol (all languages):**

- Redaction tier, coverage α = 0.05, grouped by presence pattern.
- `min_group_true=19` with **`fallback="keep"`** (fail-open, the phase-4
  recommendation). A `fallback="global"` row is reported for comparison.
- Scorers:
  - rule scorer: FR/RU cue lists; structural-only for the others
  - LR with hand cues: FR/RU only
  - LR-UD
- Splits: when a dataset ships train plus validation/test with ≥ 400
  train docs, train is split 50/50 into fit/calibrate and the rest is the
  test set. Otherwise documents are split (seed 0) 25% fit / 25%
  calibrate / 50% test.
- Gold PERSON: any label in {PERSON, PERSON_NAME, GIVENNAME, SURNAME,
  FIRST_NAME, LAST_NAME, MIDDLE_NAME, NAME, PER, PS}, case-insensitive.
  The label inventory is printed per language for checking.

**Hypotheses (judged mechanically; a language counts only if it has a
group with ≥ 19 true test candidates):**

- **H22 (validity):** with fail-open fallback, the rule scorer's
  per-group recall floor (≥ 0.95 − tol, for groups with n_true ≥ 19) holds
  in every eligible language.
- **H23 (utility):** LR-UD gains ≥ +0.03 precision over union with its
  floors holding in at least half of the eligible languages.
- **H24 (descriptive):** fail-open vs global fallback. Recall in the
  groups that fell back.

**Troubleshooting output (not hypotheses):** per language:

- rows fetched
- dropped rows and spans
- label inventory
- dictionary and NER hit counts
- presence-pattern table
- fallback flags
- any exception, captured without stopping the other languages

## Amendment (2026-09-24, before any phase-5 run on new data): add K-LegalDeID as "ko_legal"
The author decided to include K-LegalDeID (Korean court-judgment
de-identification, EACL 2026) for **non-commercial research use**, which
reverses the earlier exclusion.

**Conditions recorded here:**

- CC BY-NC-SA 4.0 governs the data:
  - no redistribution (the converted file lives in the gitignored
    `datasets/restricted/`)
  - no training or tuning for anything that ships commercially (this
    includes the author's APEX/FAPV commercialization tracks)
  - attribution in any write-up
  - share-alike on any *derived dataset* published
- **No public download exists.** Neither the paper nor its ACL Anthology
  page links to data, so the files must be requested from the authors.
  `convert_k_legaldeid.py` handles both the char-offset span format and
  the KLUE-BERT token-BIO format the paper describes.
- It uses the same baseline Korean layers as "ko".
- Its labels are Korean legal PII: "name" counts as PERSON; address,
  number, account and so on are out of PERSON scope.
- H22 and H23 apply to it unchanged, once data is present.

## Amendment (2026-09-24, before any run): add KDPII as "ko_kdpii"
While searching for an open copy of K-LegalDeID, a search of Zenodo and
OpenAIRE found none: OpenAIRE returned 0 datasets and 0 research
products; Zenodo turned up only unrelated legal corpora. It did surface
**KDPII**, the Korean Dialogic Dataset for PII De-identification (Yonsei
University with TSCIENTIFIC; IEEE Access 12:135626–135641, 2024;
DOI 10.5281/zenodo.10968609).

- License: **CC BY 4.0**, open access.
- Files: train.json, valid.json, test.json.
- Content: open, PII-labelled, conversational Korean. It complements
  K-LegalDeID's court-judgment register; it does not replace it.
- It is added with the same baseline Korean layers and the same
  hypotheses.
- Its annotation format was not visible from Claude's workspace (Zenodo
  is blocked there). The fetcher is schema-tolerant and writes
  `SCHEMA_SAMPLE_ko_kdpii.json`.

## Amendment 3 (2026-09-24, before any run on these languages): second wave
The author asked for **every eligible target language**, whatever its
priority. Added:

| lang | data | dictionary layer | NER layer |
|---|---|---|---|
| de, it, nl, hi, te | open-pii-masking-500k (CC BY 4.0) | Faker de_DE / it_IT / nl_NL / hi_IN over capitalized runs (hi: Devanagari token runs); te: none (no Faker locale), so Telugu is **single-layer** | spaCy de/it/nl `_core_news_md`; hi, te: HF `ai4bharat/IndicNER` |
| id, ja, pt, ar, tr, fi | pii-masking-openpii-1.5m (CC BY 4.0), if present | id_detect; Faker ja_JP surname + given name; pt_BR/pt_PT, tr_TR, fi_FI capitalized runs; ar_AA ∪ ar_SA Arabic token runs with بن/ابن/آل connectors | xx (id); spaCy ja/pt/fi; HF `Davlan/xlm-roberta-base-ner-hrl` (ar), `savasy/bert-base-turkish-ner-cased` (tr) |

**Smoke tests (Cowork VM):**

- spaCy's multilingual `xx_ent_wiki_sm` found **no** person in an Arabic
  sentence and tagged Hindi words as MISC. That is why those languages use
  Hugging Face models, which can run only in the author's Docker image.
- The Hugging Face model ids and their licences must be confirmed from the
  first run. A missing model is recorded as a per-language error, not a
  crash.

**Whether each language exists in its dataset is not known in advance.**
One streaming pass per dataset records `languages_seen` in `MANIFEST.json`.
A language with 0 rows is reported as unavailable, not as a result.

**Expected stress cases, stated in advance so they cannot be
rationalised afterwards:**

- German capitalizes every noun, so the dictionary layer will
  over-trigger.
- Japanese uses three mixed scripts and a small Faker list, so dictionary
  recall will be low.
- Arabic has no letter case and name chains built with connectors.
- Telugu has a single layer, so PCCF reduces to per-pattern thresholding
  of NER confidence.

H22–H24 apply unchanged.

## Amendment 4 (2026-09-24, after the second-wave run; fixes only, no hypothesis changes)
The author's run surfaced four data/infrastructure problems, not results.
Each fix is listed. Hypotheses H22–H24 are unchanged.

1. **Arabic and Turkish are not in either ai4privacy set.** The manifest's
   `languages_seen` lists 30 languages for the 1.5M set, and neither ar nor
   tr is among them. New sources:
   - Arabic: `mabahboh/sitr-arabic-pii`. Apache-2.0, ungated, 17.9k rows,
     character offsets, Gulf customer-service and document text in Arabic
     mixed with English.
   - Turkish: `newmindai/nm-kvkk-pii-6K`. Apache-2.0, ungated, fully
     synthetic, KVKK taxonomy (`full_name`, ...); the character-offset
     "spans" configuration is used.
2. **Hindi and Telugu: `ai4bharat/IndicNER` is a gated repository.** All
   3,500 documents in each language failed with 401. Two further findings:
   - **100% of the ai4privacy hi/te person names are Latin-script**
     (3,545 of 3,545 for Hindi), embedded in Devanagari/Telugu sentences.
     These rows therefore test *foreign-script names inside Indic text*,
     not native-script Indian names, and must be described that way.
   - NER is now `Davlan/xlm-roberta-base-ner-hrl` (AFL-3.0, ungated).
   - The dictionary layer gains a multi-locale Latin name list (17 Faker
     locales). Telugu becomes two-layer.
3. **Japanese: the dictionary layer produced 0 hits.** The gold names are
   mostly standalone surnames (三浦, 青木), which the surname+given
   combination rule cannot match. The rule now matches any Faker ja_JP name
   of ≥ 2 characters, and 1-character names only before さん/様/氏/君. This is
   a repair of a non-functional layer, disclosed as such.
4. **Indonesian: the multilingual xx NER is very noisy** (13,830 hits;
   P = 0.118 in the {ner} group). The pre-registered `id` row is kept
   unchanged. An additional `id_hf` row uses the project's own IndoBERT model
   (`cahya/bert-base-indonesian-NER`, the model `id_ner.py` specifies).

**Licence flag:** the Turkish NER model's card (savasy) shows no licence.
It is used for research evaluation only and must be confirmed before
publication.

## Amendment 5 (2026-09-24, during the third run, before any ar/hi/te/ar_wiki result was seen; fixes only, no hypothesis changes)

1. **Hugging Face pipeline rebuilt for every document (ar/hi/te).** The
   Docker image had no `protobuf`, so transformers could not read
   XLM-R's `sentencepiece.bpe.model` and the pipeline build failed.
   `functools.lru_cache` does not cache a call that raises, so the build was
   retried on every document (≈450 "Loading weights" bars in `run.log`).
   Fixes: `protobuf` added to `Dockerfile.pccf_multilang`; `_hf()` in
   `src/lang_layers.py` now caches failures as well as successes (one build
   attempt per model per process); HF logging and progress bars are silenced;
   the cache builder aborts a language after its first 25 documents all
   error, and prints the error.
2. **Caches were lost between Docker runs.** The container works on a
   throw-away copy of the repo, so de/it/nl/pt/fi/ja/… were recomputed on every
   run. Caches now live on the host in `phase5/docker_run/cache`
   (`PCCF_PHASE5_CACHE`) and are checkpointed every 250 documents.
3. **Arabic has no PERSON gold.** The `mabahboh/sitr-arabic-pii` label
   inventory has no person-name label (phones, national IDs, iqama, email,
   …), so `ar` can only be reported as "0 gold PERSON, not eligible". A
   new row, `ar_wiki`, uses WikiANN-ar (`unimelb-nlp/wikiann`, config `ar`;
   2,000 train / 1,000 validation / 1,000 test; Wikipedia-derived silver
   labels, text CC BY-SA) with the same Arabic layers. **Disclosures:** it is
   short Wikipedia fragments rather than PII-style text; its labels are
   silver; and the HF NER model may have seen similar Wikipedia data during
   training. It counts towards H22/H23 eligibility like any other row but
   is flagged as a proxy in the results table.

## Amendment 6 (2026-09-24, before any wave-3, tr_mit or ko_klue data was fetched): wave 3 + licence and Korean substitutes

**New rows (20):**

1. **Wave 3: 18 ai4privacy openpii-1.5m languages.** bg, pl, cs, lt, et, sv, sk,
   lv, hu, ro, el, da, sl, hr, sr, vi, ms, tl. Caps: 2,000 train + 1,500
   validation, the same as wave 2.
   - **NER:** spaCy 3.8 `*_core_news_md` where it exists (pl `persName`, sv
     `PRS`, lt/ro/el `PERSON`, da/sl/hr `PER`). Otherwise the multilingual
     `xx_ent_wiki_sm`, which is the `id` baseline pattern and expected to be
     noisy (id: {ner} P = 0.118): bg, cs, et, sk, lv, hu, sr, vi, ms, tl.
   - **Dictionary:** Faker person lists (all first/middle/last lists) over
     script-agnostic capitalised-word runs. Faker has no sr, ms or tl
     provider, so those borrow hr_HR, id_ID and es_ES + en_US; this is
     disclosed as an approximation. Some lists are small (vi 24 tokens, pl 155).
2. **tr_mit.** The same Turkish data as `tr`, with the MIT-licensed
   `akdeniz27/bert-base-turkish-cased-ner` instead of the unlicensed savasy
   model. If it performs comparably, it becomes the publishable Turkish row.
3. **ko_klue.** KLUE-NER (`klue/klue`, config `ner`, CC BY-SA 4.0): 2,000
   train + 1,500 validation sentences, character tokens, PS = person. It is an
   openly licensed Korean stand-in while K-LegalDeID is pending. It is
   news/wiki text, not PII-style text, and is disclosed as such.

**Hypotheses for these 20 rows:** they are judged as a separate family.
H22/H23 remain computed over the original rows only.

- **H28 (validity):** the rule scorer's per-group floors hold in every eligible
  new row, using the phase-6 forward floor
  `0.95 − 2·sqrt(α(1−α)(1/n_cal_true + 1/n_test_true))`.
- **H29 (utility):** LR-UD gains ≥ +0.03 precision over union, with its floors
  (same combined floor) holding in at least half of the eligible new rows.
- Eligibility, α = 0.05, fail-open fallback and min_group_true = 19 are
  unchanged. The old floor is also printed for comparison but is not judged.
- **Code note:** the new floor needs per-group calibration counts, so
  `evaluate()` now also records `cal_true` and `floors_hold_cv`. This is
  additive. Re-evaluating the existing caches on the host reproduces every
  phase-5 verdict. pt LR-UD moves by 0.0003, which is a numpy/BLAS platform
  difference that is also seen without the edit.

## Amendment 7 (2026-09-24, before any GLiNER output was computed): stronger NER for the ten xx rows

**Motivation.** Where the NER layer was spaCy's multilingual `xx_ent_wiki_sm`,
PCCF could not help (vi, ms {ner} P ≈ 0.16; id 0.118). A licence and
coverage search (see the session notes) found no single supervised,
openly licensed NER model that covers all ten languages.

**New rows (10):** bg_gl, cs_gl, et_gl, sk_gl, lv_gl, hu_gl, sr_gl, vi_gl,
ms_gl, tl_gl.

- **Data and dictionary:** the same data, splits and dictionary layer as the
  base row.
- **NER layer:** `urchade/gliner_multi-v2.1` (Apache-2.0; v0/v1 are NC and
  are not used). It runs zero-shot with the label `person`.
- **Threshold:** 0.3, fixed a priori as a high-recall operating point, since
  PCCF filters for precision. It is not tuned on any split.
- **Confidence:** GLiNER's span score is the per-span confidence.

**Hypotheses (separate family; phase-6 combined floor):**

- **H33 (layer quality).** Union F1 of the `_gl` row exceeds union F1 of its
  xx base row in ≥ 7 of 10 languages.
- **H34 (PCCF on the better layer).** The rule scorer's floors hold in all
  eligible `_gl` rows, AND LR-UD is useful (≥ +0.03 P) and valid in at least
  half of them.

**Disclosures:**

- GLiNER's per-language coverage is not documented, and it is used zero-shot.
- `gliner==0.2.29` needs transformers < 5.17. The image may therefore pin a
  different transformers version than the earlier HF rows used. Those rows'
  caches persist and are not recomputed.
- Per-language supervised models were considered (sk, cs, hu, et, sr, lv).
  They were not used in this amendment, to keep one comparable model across
  the ten rows.
