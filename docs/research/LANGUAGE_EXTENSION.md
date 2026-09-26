# Language Extension: Research Notes & Engineering Starting Point

## Shortlist status

| language | status | final numbers (dictionary-only; NER pending Docker run) |
|---|---|---|
| **French** | **Done (dictionary-only pass) — see `MEDDOCAN_VALIDATION_REVIEW_SUMMARY.md` Section 7** | combined: P=1.000 R=0.143 (n=11 pilot sample, 14 gold spans across PERSON/EMAIL/CREDIT_CARD) — PERSON 0/11 recall (root-caused, see below), EMAIL 1/1, CREDIT_CARD 1/2 |
| **Russian** | **Done (dictionary-only pass) — see `MEDDOCAN_VALIDATION_REVIEW_SUMMARY.md` Section 8** | combined: P=0.905 R=0.388 (n=26 pilot sample, 49 gold spans across all 6 REDACT types) — PERSON 9/33 recall (lemmatization solved inflection; dictionary-coverage + capitalization-regex gaps root-caused), SSN(SNILS) 2/2, MRN(OMS) 3/5, IP 3/3 (with new IPv6 support), EMAIL 2/2, CREDIT_CARD 0/4 (Luhn-checksum finding replicated) |
| **Indonesian** | **Pipeline built, evaluation blocked on data access — see `MEDDOCAN_VALIDATION_REVIEW_SUMMARY.md` Section 9** | No staged data yet — `ai4privacy/pii-masking-openpii-1.5m`'s row API returned empty across ~10 attempts (confirmed reproducible, not a one-off). Detection layer, IndoBERT-NER wiring, Docker harness, and evaluation harness are all built and will run immediately once `OpenPII_ID_raw.jsonl` is staged (exact unblock commands in `prepare_id_dataset.py`) |
| Korean (candidate, research-only) | **Researched, not built — awaiting go-ahead** | `BCCard/privacy-filter-openpii-masking` (HF, CC-BY-4.0) found: reliable row API (10/10 rows fetched), char-offset-native, 57,851 rows, PII-shaped taxonomy (PERSON/DATE/ADDRESS/PHONE/EMAIL/ZIPCODE/CARD_NUMBER/SSN/GENERIC_ID). No code written. |
| Mandarin (candidate, research-only) | **Researched, not built — awaiting go-ahead** | `wan9yu/pii-bench-zh` (HF, Apache-2.0) found: 8,000 synthetic rows/23,206 entities, char-offset-native with pre-verified integrity, checksum-realistic IDs (MOD-11-2/Luhn), 8 entity types. No code written. |
| Indian language (candidate, research-only) | **Researched, not built — awaiting go-ahead** | Two candidates, serving different purposes — see "Indian language(s), researched (this pass)" below for the full trade-off. No code written. |

Reference document for extending REDACT's Spanish-language adaptation
pattern (built for MEDDOCAN validation, see `validation/real_data/`) to
additional languages. Written after two research passes surveying nine
languages' NLP tooling, benchmark availability, and licensing. This is the
starting point for a fresh engineering session — no prior chat context is
assumed. It records what's already verified, the recommended next target,
and exactly which existing files implement the pattern to copy.

## TL;DR

Extend to **French, then Russian, then Indonesian**, in that order — one at
a time, each mirroring the Spanish pattern file-for-file
(`src/es_detect.py`, `src/es_ner.py`, `validation/real_data/Dockerfile.meddocan_ner`,
`validation/real_data/run_meddocan_ner.sh`, `validation/real_data/evaluate_meddocan.py`,
`validation/real_data/MEDDOCAN_TYPE_MAPPING.md`,
`validation/real_data/prepare_meddocan_dataset.py`). French has the most
mature tooling and the most promising (though not yet license-verified)
benchmark candidates of any language surveyed; Russian and Indonesian are
the next-best-ready tier after it. Full ranking, rationale, and per-language
gotchas below — see "Scope note" for why this list stops at three.

## The pattern to mirror (already built and working, Spanish)

Every file below exists, has been run against real data, and is the
template for a new language's version:

| file | role |
|---|---|
| `src/es_detect.py` | Regex + dictionary detection layer. Opt-in, separate module — never modifies `src/detect.py`. Contains label-anchored regexes for the target language's ID formats (MRN/SSN-equivalents) and a name dictionary (Faker's locale-specific person provider) matched over capitalized-word runs. |
| `src/es_ner.py` | A second, independent Presidio `AnalyzerEngine` explicitly configured for the target language's spaCy model (`NlpEngineProvider` + `supported_languages`). Kept separate from `detect.py`'s own English-model analyzer so nothing already measured is touched. |
| `validation/real_data/Dockerfile.meddocan_ner` | Standalone Docker image that downloads the spaCy model (this project's sandbox blocks huggingface.co/github.com/spaCy's model releases directly) — repo source is mounted read-only at runtime, not baked into the image, so code edits don't require a rebuild. |
| `validation/real_data/run_meddocan_ner.sh` | Builds and runs the above, tees results to a file, resolves paths so it can be run from any working directory. |
| `validation/real_data/evaluate_meddocan.py` | Evaluation harness: loads staged data, runs 5 conditions (native-language patterns alone / dictionary alone / NER alone / combined / full), overlap-based span scoring mirroring `inject_and_evaluate.py`'s methodology, plus a `--diagnose` flag that root-causes false positives (which layer, what fraction overlap a different real entity type). |
| `validation/real_data/MEDDOCAN_TYPE_MAPPING.md` | Documents which of the source benchmark's entity types map onto REDACT's canonical vocabulary (PERSON/EMAIL/MRN/SSN/etc.) and which are explicitly excluded, with stated rationale per exclusion — not silently dropped. |
| `validation/real_data/prepare_meddocan_dataset.py` | Dataset staging/provenance script — documents exactly how the data was obtained and what coverage was achieved, discloses any partial-coverage limitation honestly. |

## Per-language findings (both research passes, condensed)

| language | spaCy model | Faker locale | real benchmark candidate | script/tokenization complication | rank |
|---|---|---|---|---|---|
| **French** | `fr_core_news_md` — mature | `fr_FR`, populated | CAS (7,580 clinical cases), ESSAI (13,848 cases), QUAERO French Medical Corpus (~103K words, 10 entity types) — **license/access NOT YET VERIFIED** | None — Latin script, real whitespace, same structure as Spanish/English | **1 (recommended next)** |
| Russian | `ru_core_news_md/lg` — mature | `ru_RU`, populated | None found (RuMedNER/RuDReC are clinical-entity, not PII-de-id-labeled) | Cyrillic, case-sensitive, heavily inflected — dictionary lookup needs lemmatization, not exact-string matching (the Spanish `_CAP_RUN_RE` approach won't port cleanly) | 2 |
| Indonesian | No official spaCy model (community IndoBERT-NER fine-tunes exist) | `id_ID`, richest of the SE Asian locales (~470/280 first names) | None found | None — real whitespace word boundaries | 2 |
| Japanese | `ja_core_news_md` (SudachiPy-based segmentation) — mature | `ja_JP`, populated | NTCIR MedNLP / Real-MedNLP shared task (best F1 ~84.2) — closest non-Western PHI de-id precedent found | No whitespace; 3 mixed scripts (kanji/hiragana/katakana); honorific suffixes (さん/様) attach to names and need explicit stripping | 3 |
| Chinese (Mandarin) | `zh_core_web_md` (pkuseg segmentation) — mature | `zh_CN`/`zh_TW`, populated | CCKS shared tasks exist but tag clinical entities (symptoms/diagnoses), not PII/PHI spans — doesn't solve the benchmark gap | No whitespace | 3 |
| Korean | `ko_core_news_*` — now exists officially (KLUE-trained) but newer/thinner track record | `ko_KR`, populated | K-LegalDeID (legal domain), KDPII (dialogue) — no clinical PHI benchmark | Eojeol (phrase-level) spacing, not word-level — whitespace still isn't a usable boundary | 4 |
| Arabic | No official spaCy model (community fork only, unverified upkeep) | `ar_AA` — names are tribal/clan names, not modern surnames, a realism mismatch | Kocaman et al. 2023 (ArabicNLP) — BERT de-identifier on a *translated* i2b2 corpus, license/access unverified | **No letter case at all** — the Spanish approach's capitalization heuristic is structurally inapplicable, not just weaker | 4 |
| Cantonese | None — would inherit Mandarin tooling as an out-of-distribution dialect | No `zh_HK` locale found | None | Same as Chinese, plus distinct lexicon/grammar from Standard Written Chinese that no tooling targets | 5 (don't pursue standalone) |
| Thai | No spaCy model; requires PyThaiNLP/deepcut segmentation *before* NER is possible | `th_TH`, comparable size to `es_ES` | Generic NER only (Thai NER v2.0/LST20), no PII-specific benchmark | **No whitespace between words at all** — breaks the flattened-vs-spaced premise this project's other work (FlatPII) depends on, since there's no "spaced" baseline to compare against | 5 (don't pursue) |
| Vietnamese | No spaCy model; needs syllable-to-word regrouping (VnCoreNLP/underthesea) | `vi_VN` — only ~10 surnames total, unusably sparse | None | Spaces mark syllable boundaries, not word boundaries — naive tokenization fragments names | 5 (don't pursue) |

## Why French, specifically

Best tooling maturity of any language surveyed, a real Faker locale, and —
uniquely among the non-Spanish languages checked — actual existing clinical
NLP corpora (CAS, ESSAI, QUAERO) with documented de-identification research
already published against them (Grabar et al.). No new tokenization
approach is needed; the Spanish `_CAP_RUN_RE` capitalization-run dictionary
match and the `NlpEngineProvider`-based second-analyzer pattern both port
directly.

**RESOLVED (this pass): CAS/ESSAI/QUAERO are disqualified — but not for the
license reason this document originally flagged.** License was actually
resolvable (QUAERO is GFDL-licensed; CAS's paper is CC BY 4.0). The real,
more fundamental disqualifier, confirmed by fetching `DrBenchmark/QUAERO`'s
published NER label schema directly: **none of the three annotate PII/
de-identification spans at all.** Their label set (LIVB/PROC/ANAT/DEVI/
CHEM/GEOG/PHYS/PHEN/DISO/OBJC) is UMLS clinical-concept categories —
living beings, procedures, anatomy, devices, diseases — a different task
entirely from PERSON/EMAIL/ID-number de-identification. A benchmark can be
well-licensed and well-documented and still be unusable if it was never
annotated for the right kind of span. Worth recording as a finding in its
own right: the original "license NOT YET VERIFIED" framing undersold the
actual problem.

**Alternative found and used:** `ai4privacy/open-pii-masking-500k-
ai4privacy` (Hugging Face), CC BY 4.0, `gated: false` — a genuinely
multilingual PII-masking corpus (English, French, German, Italian,
Spanish, Hindi, Telugu, and more) with exact-offset `privacy_mask` spans
across a PERSON/EMAIL/phone/ID-number-shaped label taxonomy, the right
KIND of annotation MEDDOCAN also has. A real, disclosed difference from
MEDDOCAN: this dataset's carrier text is template/LLM-generated synthetic
text with synthetic PII values, not real human-authored documents — see
`validation/real_data/prepare_fr_dataset.py`'s full disclosure. Full
writeup: `MEDDOCAN_VALIDATION_REVIEW_SUMMARY.md` Section 7.

## A real finding worth re-deriving before building the French layer

Combining independent detection layers (dictionary + real NER) does **not**
straightforwardly improve precision. On MEDDOCAN, PERSON precision dropped
from 47.4% (dictionary alone) to 39.2% (dictionary + NER combined) because
the two layers' false positives were ~97% disjoint while their true
positives mostly overlapped — unioning them stacked errors instead of
averaging them. Root cause differed by layer too: the dictionary's FPs were
94.3% place-name collisions (Santiago/Valencia are both names and places);
NER's FPs were only 9.4% place-name collisions, dominated instead by
disease-eponym confusion (Alport, McArdle — real historical surnames now
used as syndrome names) and clinical-header false triggers (Antecedentes,
Varón). Expect a structurally similar but not identical dynamic for French
— plan the evaluation harness (mirroring `evaluate_meddocan.py`'s
`--diagnose` flag) to measure combined-vs-standalone layers and root-cause
false positives from the start, not as an afterthought once a
surprising number shows up.

## Russian, resolved (this pass)

LANGUAGE_EXTENSION.md's original research found no Russian PII/
de-identification benchmark at all (RuMedNER/RuDReC are clinical-concept
entity recognition, the same wrong-kind-of-annotation problem French's
CAS/ESSAI/QUAERO candidates had). A fresh literature search this pass
found `redmadrobot-rnd/pii_benchmark` (Hugging Face, MIT license,
`gated: false`) — 21 PII entity types including Russian-specific
identity-document numbers (SNILS, OMS, INN, passport, driver's license,
military ID, birth certificate), with a real-context/synthetic-value
provenance mix (per its own README: real production-log sentences with
PII values replaced by synthetic equivalents, plus synthetic document
templates and hand-filtered hard negatives).

The flagged inflection gotcha was confirmed real before building around
it: Russian surnames, patronymics, and place names inflect by grammatical
case (e.g. "Курганской области" is genitive, not the nominative
dictionary form), so the Spanish/French exact-string dictionary approach
would silently miss inflected occurrences. Fixed with `pymorphy2` (MIT,
its Russian dictionary installs via plain `pip install` in this sandbox
— no Docker needed, unlike the NER model), lemmatizing both the
candidate word and the dictionary before comparing. A second, genuinely
separate finding surfaced once inflection was solved: several staged
surnames simply aren't in Faker's `ru_RU` dictionary at all (a
coverage gap, not a lemmatization failure) — same class of result as
French's 0% name-dictionary overlap, now measured and root-caused rather
than assumed. Full write-up: `MEDDOCAN_VALIDATION_REVIEW_SUMMARY.md`
Section 8.

## Indonesian, blocked on data access (this pass)

Unlike French and Russian, Indonesian's blocker is genuinely different in
kind: a license-clear, correctly-annotated candidate WAS found
(`ai4privacy/pii-masking-openpii-1.5m`, the same dataset family used for
French, confirmed to include `language: id`), but Hugging Face's
`datasets-server` row API returned empty responses across roughly ten
attempts at different offsets and both splits for this specific
1.5M-row/4.6GB dataset — a confirmed, reproducible access-reliability
problem, not a license or annotation-type problem. Rather than fabricate
data or keep retrying an API already shown unreliable, the full pipeline
(type mapping proposed from the dataset's known shared schema, name
dictionary layer, a from-scratch HuggingFace-transformers NER wrapper
since Indonesian has no spaCy model — the flagged gotcha, addressed
before writing code — Docker harness, evaluation harness) was built and
is ready to run immediately once the one remaining step — staging real
Indonesian rows, which needs real (non-sandboxed) internet access — is
done. Exact unblock commands: `validation/real_data/
prepare_id_dataset.py`. Full write-up: `MEDDOCAN_VALIDATION_REVIEW_
SUMMARY.md` Section 9.

## Korean, Mandarin, and an Indian language — researched (second pass, this session)

The original shortlist (French/Russian/Indonesian) is complete. The user
then asked to check Korean, Mandarin, and an Indian language as candidates
to *add* coverage. This section is that research pass — license, benchmark,
tooling, per the same checklist discipline used for every prior language.
**No detection code, Docker files, or evaluation harnesses were written for
any of the three languages below.** This is deliberate: the project's own
checklist (see "Checklist for the next engineering session") requires
license/benchmark/tooling verification *before* code, and the user's
request was framed as "check" — treated as authorization to investigate,
not to build. Go/no-go on any of these is the user's call.

### Korean

**Original (first-pass) finding, still true:** no dedicated PHI/PII
de-identification benchmark exists in the clinical sense French/Spanish
have — `K-LegalDeID` (EACL 2026, Korean court-judgment de-identification)
is the closest legal-domain precedent, but it's **CC BY-NC-SA 4.0**,
non-commercial and share-alike. That's a real license-friction point this
document flags explicitly rather than building around quietly: every
language shipped so far (Spanish/MEDDOCAN, French, Russian) uses a
permissively-licensed (CC-BY-4.0 or MIT) source, and REDACT itself is
framed as open/reusable tooling. A non-commercial-restricted training/eval
source is a different category of dependency than anything in this project
to date, and should be a decision the user makes deliberately, not
inherited by default.

**New finding, this pass:** `BCCard/privacy-filter-openpii-masking`
(Hugging Face, `CC-BY-4.0`, `gated: false`, `private: false`) is a
materially better-fitting candidate than K-LegalDeID. It's a *relabeled and
supplemented* derivative of `ai4privacy/pii-masking-openpii-1.5m`'s Korean
rows — same parent family already used for French — but published as its
own smaller, reliably-hosted dataset (57,851 rows, mixed Korean/English),
so it sidesteps the exact `datasets-server` unreliability that blocked
Indonesian's own use of the 1.5M-row parent directly (confirmed via 10/10
successful row fetches this pass, vs. ~1/10 for the parent). Char-offset
`privacy_mask` annotations, PII-shaped taxonomy (PERSON, DATE, ADDRESS,
PHONE, EMAIL, ZIPCODE, CARD_NUMBER, SSN, GENERIC_ID) — the right *kind* of
annotation, not clinical-concept NER. Tokenization complication (eojeol/
phrase-level spacing, not word-level) is addressable: `konlpy`'s `Okt`
tagger pip-installs and imports successfully in this sandbox.

**Devil's-advocate check:** `BCCard/privacy-filter-openpii-masking` is
itself a *derivative* of `ai4privacy/pii-masking-openpii-1.5m` — CC-BY-4.0
requires attribution to BCCard, but worth confirming BCCard's own upstream
attribution to ai4privacy holds up before citing this as a clean-chain
source in any published writeup. Not verified this pass — a 10-minute
follow-up before this dataset is actually staged, not a blocker to noting
it as the recommended Korean candidate now.

### Mandarin

**Original (first-pass) finding, still true:** CCKS shared tasks exist but
tag clinical entities (symptoms/diagnoses), the same wrong-kind-of-
annotation problem French's CAS/ESSAI/QUAERO had — doesn't solve the
benchmark gap on its own.

**New finding, this pass:** `wan9yu/pii-bench-zh` (Hugging Face,
`Apache-2.0`, `gated: false`, `private: false`) is a purpose-built PII
benchmark, not a repurposed clinical-NER set. 8,000 samples / 23,206
entities across two register subsets (5,000 formal + 3,000 noisy chat),
100% synthetic with an explicit bilingual disclaimer, char-offset-native
with pre-verified `text[start:end] == entity.text` integrity (no
reconstruction needed, unlike Russian). 8 entity types (person, phone,
id_number, bank_card, address, email, passport, license_plate) with
**realistic checksums** — MOD-11-2 for id_number, Luhn for bank_card — a
genuinely stronger synthetic-data discipline than several sources already
used (recall: French/Russian's own CREDIT_CARD gold values weren't
consistently Luhn-valid, which is exactly why REDACT's Luhn-checksum
safeguard under-recalled on them).

**Structural complication flagged first-pass, confirmed still real this
pass:** Chinese script has no capitalization signal, so the
`_CAP_RUN_RE`-capitalized-run-plus-dictionary architecture every other
language's `{lang}_detect.py` uses **does not port**. `jieba.cut()` was
smoke-tested this pass on a mixed Chinese/English/email string and
correctly segmented a two-character name ("张伟") as one token — solves
the no-whitespace tokenization problem — but a `zh_detect.py` would still
need a materially different architecture (segment-then-dictionary-lookup,
not capitalization-run-then-dictionary-lookup) from every language shipped
so far. This is the single biggest reason Mandarin is not a drop-in
"fourth French" even with a strong benchmark now in hand — it's a genuine
engineering-effort outlier relative to French/Russian/Indonesian, not a
data problem.

**Devil's-advocate check:** the README states names are drawn from "50
common surnames × 50 common given names" — a 2,500-combination name space.
Worth empirically checking word-list diversity/collision rate before
trusting PERSON recall numbers from this benchmark as representative,
the same way French's near-0% Faker-dictionary-overlap and Russian's
partial-coverage gap were measured rather than assumed. Not checked this
pass.

### Indian language(s)

This one splits into two genuinely different candidates that serve
different goals — presented as a decision point, not resolved unilaterally.

**Option A — `maskflow-ai/indiapii-bench` (Hugging Face, `CC-BY-4.0`,
`gated: false`, `private: false`).** The strongest-fitting candidate found
in *either* research pass by several measures: single raw `.jsonl` file
(no `datasets-server` row-API reliability risk at all — the Indonesian
blocker and the parent Korean/Mandarin family's own flakiness structurally
can't recur, since the whole file was fetched directly), deterministic/
reproducible build (`seed 20260827`), 2,000 documents / 13,468 labelled
spans, char-offset-native with clean `text[start:end]` alignment (spot-
checked against 15 sample rows this pass, all consistent), and a
genuinely PII-specific — not general-NER — taxonomy covering India-specific
structured identifiers: AADHAAR (+ masked variant), PAN, GSTIN, IFSC,
UPI_VPA, ABHA (number + address), INDIAN_MOBILE, INDIAN_PASSPORT,
INDIAN_ADDRESS, PIN_CODE, BANK_ACCOUNT_IN, DRIVING_LICENCE, VEHICLE_REG,
VOTER_ID, PERSON_NAME. Aadhaar/GSTIN values carry mathematically valid
checksums (Verhoeff, GSTIN mod-36) — the same checksum-realism discipline
found in Mandarin's `wan9yu` set, stronger than what several already-
shipped languages' gold data had. It also ships **labelled hard negatives**
(PII-shaped non-PII: non-Verhoeff-valid 12-digit numbers, PAN-shaped
invoice numbers, VPA-shaped emails, timestamps) specifically so precision
is measurable against deliberate near-misses, not just recall against true
positives — a more rigorous eval design than any benchmark used in this
project so far.

**The catch, stated plainly:** this is not Hindi-*language* text. Per its
own README, "English and Hinglish only; no dedicated Devanagari-only
documents" — confirmed in the 15 sample rows read this pass: `lang` field
values were `"en"` or `"hi-en"` (code-mixed, Latin-script Hinglish, e.g.
"Sir maine payment bheja hai apke UPI..."), never pure Devanagari script.
Extending REDACT with this dataset would really mean **adding an
India-specific structured-ID regex layer to the existing English
pipeline** (parallel to how SSN/MRN regexes are already country-specific
patterns) — not a new foreign-script dictionary-plus-NER language pipeline
in the shape of French/Russian/Indonesian. That's arguably an *easier* and
lower-risk build (checksummable regexes, no lemmatization, no Faker-
dictionary-coverage gap, no segmentation rewrite) — but it is a different
kind of extension than "Hindi," and calling it "Indian language support"
without this caveat would overstate what it covers. Devil's-advocate
framing: a reviewer who reads "added Hindi support" and then finds no
Devanagari text anywhere in the eval set has a legitimate "limited
technical depth" complaint — the same failure mode this project's earlier
desk rejection is explicitly trying not to repeat. Any writeup using this
dataset must name it as "Indian structured-PII formats (English/Hinglish
text)," not "Hindi."

**Option B — `cfilt/HiNER-original` (Hugging Face, `CC-BY-SA-4.0`).**
Genuine Hindi/Devanagari-script text (76,025 train / 10,861 validation /
21,722 test examples, LREC 2022, expert-annotated) — the literal "Hindi
language" candidate. But two real problems, both flagged rather than
absorbed silently: (1) it's general NER (person/location/organization-
style tagging, the same wrong-kind-of-annotation gap French's CAS/ESSAI/
QUAERO and Mandarin's CCKS had), not PII/de-identification-labeled, so
type-mapping onto REDACT's PERSON/EMAIL/SSN vocabulary would need the same
kind of careful, partial, honestly-disclosed mapping Russian and French
both needed for their weaker label matches. (2) `CC BY-SA-4.0` is
**share-alike** — a new license category for this project. Every source
used so far (MEDDOCAN's CC BY 4.0, French/Korean's CC-BY-4.0, Russian's
MIT, Mandarin's Apache-2.0, IndiaPII-Bench's CC-BY-4.0) is a plain
attribution or permissive license with no downstream-licensing
obligation; share-alike would be the first source in this project that
constrains how REDACT itself (or work built from it) can be licensed
going forward. Worth a deliberate decision, not a default.

**Recommendation, stated as a recommendation, not a decision:** if the
goal is "detect India-specific structured PII formats," Option A
(IndiaPII-Bench) is ready to stage today with no open license question and
the strongest eval-rigor of anything considered across both research
passes — build it as an India-regex layer, named accurately. If the goal
is specifically "detect PII in Hindi-language prose," neither option fully
delivers: Option A has no Devanagari text at all, and Option B has
Devanagall text but the wrong annotation type and a license with a new
downstream obligation. That gap — no clean, permissively-licensed,
PII-labeled, Devanagari-script benchmark was found in either research
pass — is itself worth recording as a finding, the same way "no Russian
PHI benchmark exists" was recorded in the first pass rather than papered
over.

## Scope note

This is a validation-generalization side-track, not the project's current
primary research focus — a separate, in-progress paper (FlatPII, on
flattened-token PII detection in log/telemetry data specifically) is the
priority work and should not be delayed by this. Language extension is
worth doing for robustness, but treat it as bounded, **sequential** work
against a fixed shortlist, not an open-ended expansion to every language
surveyed above. Eligible shortlist, in priority order:

1. **French** — do this one first. Best tooling maturity of any language
   surveyed, real (if not yet license-verified) clinical corpus candidates.
2. **Russian** — mature spaCy model and Faker locale, but no PHI-specific
   benchmark exists; the dictionary layer will need lemmatization/fuzzy
   matching instead of the Spanish exact-string approach, since Russian is
   heavily inflected.
3. **Indonesian** — clean whitespace tokenization (no segmentation rewrite
   needed) and a decent Faker locale, but no official spaCy model (needs a
   HuggingFace IndoBERT-NER swap-in) and no benchmark found at all.

Tier-3 languages (Japanese, Chinese) and everything ranked 4-5 in the table
above are explicitly **not** in scope for this pass — they require a real
tokenization rewrite (no whitespace) or have no usable model/benchmark at
all, and pursuing them now would be the same scope-creep risk this
project's own history (the "limited technical depth" desk rejection) has
already run into once. Finish and write up French before starting Russian;
finish and write up Russian before starting Indonesian. Do not work on more
than one language at a time, and do not add a fourth language to this
shortlist without re-running the same tooling/benchmark/license research
this document is based on.

**Update, second research pass:** the original three-language shortlist is
now complete (French, Russian, Indonesian — see status table at top).
Korean, Mandarin, and an Indian-language/regex-layer option have since been
researched (license, benchmark, tooling — see the section above) at the
user's explicit request, but **none have been built**. This document does
not unilaterally extend the shortlist to six — that decision, including
which (if any) of the three new candidates to build and in what order, is
the user's to make. The same "no fourth language without re-running
research" discipline stated above applies symmetrically here: research is
done, code is not, and shouldn't start without explicit go-ahead.

## Checklist for the next engineering session

Repeat this sequence once per language, in the priority order above
(`{lang}` = `fr`/`ru`/`id`, `{XX}` = the corresponding Faker/spaCy locale
code):

1. Verify the target language's benchmark candidate's license/access terms
   BEFORE staging data or writing code — for French, that's CAS/ESSAI/QUAERO
   (unverified as of this writing); for Russian and Indonesian, no
   candidate has been identified yet, so this step starts with a literature
   search, the same kind that found MEDDOCAN as the Spanish DUA-free
   alternative to n2c2. If nothing DUA-free/license-clear turns up, document
   that finding and move to the next language on the shortlist rather than
   stalling on one language indefinitely.
2. Stage a real text sample once a viable source is confirmed, with the
   same honest partial-coverage disclosure discipline as
   `prepare_meddocan_dataset.py` if full coverage isn't achievable.
3. Map the source's entity types onto REDACT's canonical vocabulary
   (PERSON/EMAIL/MRN/SSN/etc.), documenting exclusions explicitly —
   mirror `MEDDOCAN_TYPE_MAPPING.md`'s format and rationale style.
4. Build `src/{lang}_detect.py` (regex + `{XX}` Faker dictionary layer),
   confirmed via `git status` to touch zero existing files. For Russian
   specifically: don't reuse the Spanish `_CAP_RUN_RE` exact-string
   dictionary match as-is — inflection means the same name appears in
   multiple case-declined forms, so this needs lemmatization or fuzzy
   matching from the start, not as a fix after low recall shows up.
5. Build `src/{lang}_ner.py` + `Dockerfile.{lang}_ner` + `run_{lang}_ner.sh`
   (mirroring the Spanish Docker workaround for the sandboxed model
   download). For Indonesian specifically: there's no official spaCy
   model, so this step means wiring in a HuggingFace IndoBERT-NER model
   instead of a `spacy.load()` call — expect the `NlpEngineProvider`
   config to look different from the Spanish/French/Russian versions.
6. Build `evaluate_{lang}.py` with the same 5-condition structure and
   `--diagnose` root-cause tooling as `evaluate_meddocan.py`.
7. Run it, document results including the disjoint-false-positive-set
   check from day one, not after a surprising number appears.
8. Update this document's shortlist status (mark the language done, note
   final precision/recall numbers) before starting the next language.
