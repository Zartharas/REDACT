# Language Extension: Research Notes & Engineering Starting Point

Reference document for extending REDACT's Spanish-language adaptation
pattern (built for MEDDOCAN validation, see `validation/real_data/`) to
additional languages. Written after two research passes surveying nine
languages' NLP tooling, benchmark availability, and licensing. This is the
starting point for a fresh engineering session — no prior chat context is
assumed. It records what's already verified, the recommended next target,
and exactly which existing files implement the pattern to copy.

## TL;DR

Extend to **French** next, mirroring the Spanish pattern file-for-file
(`src/es_detect.py`, `src/es_ner.py`, `validation/real_data/Dockerfile.meddocan_ner`,
`validation/real_data/run_meddocan_ner.sh`, `validation/real_data/evaluate_meddocan.py`,
`validation/real_data/MEDDOCAN_TYPE_MAPPING.md`,
`validation/real_data/prepare_meddocan_dataset.py`). French has the most
mature tooling and the most promising (though not yet license-verified)
benchmark candidates of any language surveyed. Full ranking and rationale
below.

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

**The one thing NOT yet done, and the actual first engineering step:**
CAS/ESSAI/QUAERO's license and access terms have not been verified. Do not
build code around any of them until this is checked — this project's own
history is the cautionary example: n2c2 looked like the right dataset and
turned out to be blocked behind a Harvard DBMI Data Use Agreement whose
portal was down; MEDDOCAN was found afterward specifically because it's
CC BY 4.0 with no DUA. Check CAS/ESSAI/QUAERO's actual terms first, and if
all three require a DUA or registration, look for a DUA-free alternative
the same way MEDDOCAN was found for Spanish before writing any French
detection code.

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

## Scope note

This is a validation-generalization side-track, not the project's current
primary research focus — a separate, in-progress paper (FlatPII, on
flattened-token PII detection in log/telemetry data specifically) is the
priority work and should not be delayed by this. Language extension is
worth doing for robustness, but treat it as bounded, documented follow-up
work, not an open-ended expansion — French only, evaluated and written up
before considering a second additional language.

## Checklist for the next engineering session

1. Verify CAS/ESSAI/QUAERO license/access terms; find a DUA-free
   alternative if all three are blocked.
2. Stage a real French clinical (or general) text sample once a viable
   source is confirmed.
3. Map the source's entity types onto REDACT's canonical vocabulary
   (PERSON/EMAIL/MRN/SSN/etc.), documenting exclusions explicitly —
   mirror `MEDDOCAN_TYPE_MAPPING.md`'s format and rationale style.
4. Build `src/fr_detect.py` (regex + `fr_FR` Faker dictionary layer),
   confirmed via `git status` to touch zero existing files.
5. Build `src/fr_ner.py` + `Dockerfile.fr_ner` + `run_fr_ner.sh`
   (mirroring the Spanish Docker workaround for the sandboxed model
   download).
6. Build `evaluate_fr.py` with the same 5-condition structure and
   `--diagnose` root-cause tooling as `evaluate_meddocan.py`.
7. Run it, document results including the disjoint-false-positive-set
   check from day one, not after a surprising number appears.
