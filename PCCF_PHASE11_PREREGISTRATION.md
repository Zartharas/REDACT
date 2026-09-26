# PCCF phase 11: real-document validation (pre-registration)

Frozen 2026-09-24, **before any phase-11 data was downloaded.** The author
approved the dataset choices:

- TAB is accepted, even though the reuse terms of the underlying HUDOC text
  are unverified.
- Enron is included as a *descriptive* side analysis.
- English is run with *both* layer sets.

The proposal it supersedes is `PCCF_PHASE11_PROPOSAL.md`.

**Amendment 1 (2026-09-26).** Added `ko_legal_precedents`: a descriptive-only
Korean legal-domain row, added to close the "waiting on the K-LegalDeID
authors" dependency without depending on anyone's permission (see
`Academic Documentation/Outreach/K-LegalDeID_follow_up.md`, marked
superseded/optional, not a blocker). Also formally retires `tr` (savasy)
from anything citable: its Hugging Face model card carries no licence, and
the Turkish licence-request draft is superseded because `tr_mit`
(`akdeniz27/bert-base-turkish-cased-ner`) was confirmed `license: mit` via
the Hugging Face API on this date. Neither change touches any already-
reported verdict (H22/H23/H41/H42 counts are unchanged); `ko_legal_precedents`
is descriptive by construction (see its row below) and `tr` was never a
phase-11 row.

## Rows
| row | data (licence) | layers | splits → protocol |
|---|---|---|---|
| en_tab_redact / en_tab_spacy | TAB, ECHR cases (repo MIT) | see "English layers" | train → fit/cal; dev + test → test (shifted protocol) |
| en_btc_redact / en_btc_spacy | Broad Twitter Corpus (CC BY 4.0) | English | train ≤ 2,000 → fit/cal; validation + test ≤ 1,500 → test |
| en_wnut_redact / en_wnut_spacy | WNUT-17 (CC BY 4.0) | English | as BTC |
| de_germeval | GermEval 2014 (CC BY 4.0) | phase-5 `de` layers | as BTC |
| ru_factrueval | FactRuEval-2016 (MIT) | phase-3/5 `ru` layers | 255 docs < 400 train → pooled random quarters (exchangeable) |
| ar_anercorp | ANERcorp, CAMeL splits (CC BY-SA 4.0) | phase-5 `ar` layers (XLM-R + ar dictionary) | train ≤ 2,000 sentences; test ≤ 1,500 |
| en_enron_redact / en_enron_spacy | Enron-Meetings + Enron-Random, CMU/Minkov (**no licence stated**) | English | pooled random quarters. **Descriptive only; excluded from H41–H43** |
| ko_legal_precedents | Korean court precedents, joonhok-exo-ai (openrail, no permission needed) | phase-5 `ko` layers | all `train` (streamed sample, capped). **Descriptive only; no gold PERSON spans (already court-anonymised) -- excluded from H41–H43, not run through H43/ACI/split-diagnostic** |

**English layers.** Both layer sets use spaCy beam confidence on the NER
layer.

- **`_redact`:** the production REDACT detector. The NER layer is
  `detect.scan_ner` (Presidio + spaCy `en_core_web_lg`); the dictionary
  layer is `detect.scan_flattened` (the production flattened-name layer).
- **`_spacy`:** the NER layer is spaCy `en_core_web_lg` PERSON entities
  directly. The dictionary layer is SSA given names (≥ 5,000 births,
  2016–2025) and Census top-20k surnames, matched over capitalised word runs.

**Gold.**

- Person spans are the dataset's person labels: TAB `PERSON`; BTC `PER`;
  WNUT `person`; GermEval `PER` (outer level only; PERderiv/PERpart
  excluded); FactRuEval spans referenced by `Person` objects; ANERcorp
  `PERS`; Enron person-name labels.
- **TAB (multi-annotator dev/test):** a span is gold PERSON if **any**
  annotator marked it. This is recall-oriented, which is appropriate for
  redaction. Its identifier type is DIRECT if any annotator marked an
  overlapping PERSON mention DIRECT; otherwise QUASI; otherwise NO_MASK.
- **Texts:** token datasets are rebuilt by joining tokens with single
  spaces, so spans are exact by construction.

**Missing data.** ANERcorp sits behind a licence-acceptance form, so the
author downloads it manually. If it is absent, the row reports "no data"
and is simply excluded; that is not a failure.

## Method (unchanged from phase 5)
- Rule scorer and LR-UD.
- α = 0.05, min_group_true = 19, fail-open.
- The phase-6 combined floor.
- Eligible row: at least one group with n_true_test ≥ 19.
- Reference results are computed in the Docker image.

## Hypotheses (judged mechanically; Enron excluded)
- **H41 (validity on real documents).** The rule scorer's combined floors
  hold in every eligible phase-11 row.
- **H42 (utility on real documents).** LR-UD gains ≥ +0.03 precision over
  union, with its combined floors holding, in at least half of the eligible
  rows.
- **H43 (direct identifiers on TAB).** On both TAB rows, consider the gold
  PERSON spans of identifier type DIRECT that some candidate covers (n_D).
  The fraction kept by LR-UD PCCF must be ≥ 0.95 − 2·sqrt(0.05·0.95/n_D).

## Descriptive (not judged)
- End-to-end recall by TAB identifier type.
- `_redact` vs `_spacy` layer comparison per dataset.
- The pooled-split (E1) diagnostic of phase 6 for the shifted rows.
- Enron precision and recall and floors.
- An ACI arm (phase-8 method) on the shifted rows (TAB/BTC/WNUT/GermEval/FactRuEval/
  ANERcorp/Enron only; `ko_legal_precedents` has no gold, so ACI and the
  split-diagnostic are not applicable and are not run for it).
- `ko_legal_precedents` candidate counts (dict_hits / ner_hits) as a sanity
  check on real Korean legal text; its printed P/R is vacuously 0.000 (there
  is no gold to match against) and must not be read as a measurement.
