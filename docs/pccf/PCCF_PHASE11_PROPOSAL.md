# PCCF phase 11 (PROPOSAL, not yet a pre-registration): real-document validation

**Status:** draft for the author's approval of the datasets. Nothing has
been downloaded or run. The pre-registration will be frozen *before* any
data is fetched.

## Why
Most multilingual rows are synthetic (ai4privacy). The real-document
evidence so far is:

- MEDDOCAN (clinical)
- KDPII (dialogue)
- the WikiANN and KLUE proxies

A reviewer will ask whether the guarantees and gains hold on real documents.

## Candidate data
These were verified by a literature and licence search on 2026-09-24.

| role | dataset | licence | why |
|---|---|---|---|
| **primary** | TAB: Text Anonymization Benchmark (ECHR court cases; Pilán et al. 2022) | repo MIT; reuse terms of the underlying HUDOC text *unverified*, cases public | Real documents with gold PERSON (incl. aliases, usernames, initials) and DIRECT/QUASI identifier labels, made for anonymisation. Multi-annotator test split (127 docs) |
| secondary | Broad Twitter Corpus (GateNLP) | CC BY 4.0 | Real noisy user text; 5,271 gold PER; official splits |
| stress | WNUT-17 | CC BY 4.0 | Rare and unseen names, where the dictionary layer should fail |
| multilingual | GermEval 2014 (de), ANERcorp (ar), FactRuEval-2016 (ru) | CC BY 4.0 / CC BY-SA 4.0 / MIT | Real gold PER in three more languages. ANERcorp gives Arabic a *gold* row, replacing the WikiANN proxy |
| excluded | Enron-CMU email subsets | no licence stated | Closest domain, but no licence. Descriptive only, if at all |
| excluded | CoNLL-2002/2003, OntoNotes, CLUENER, SUC, BSNLP | restricted or unknown licence | |

## Planned design (to be frozen)
- **Layers:**
  - English: spaCy `en_core_web_lg` via the existing detector, plus the
    SSA/Census name dictionary.
  - de/ar/ru: the existing phase-5 layers.
- **Scorers, α and floors:** the rule scorer and LR-UD, α = 0.05, the
  combined floor, fail-open, and ACI as a secondary arm.
- **Splits:** each dataset's official splits (train → fit/cal, test → test),
  which is the realistic, shifted setting. Also pooled splits, to separate
  shift from method.
- **Draft hypotheses:**
  - H41: rule-scorer floors hold on every real row.
  - H42: LR-UD is useful (≥ +0.03 P) and valid on ≥ half of the rows.
  - H43: on TAB, PCCF recall of DIRECT identifiers is ≥ its recall of
    QUASI identifiers, and ≥ 0.95 − floor.
  - Descriptive: inter-annotator agreement on TAB as a ceiling.

## Author decisions needed
1. Approve the datasets. In particular, is TAB acceptable given that the
   HUDOC reuse terms are unverified?
2. Include Enron descriptively, or exclude it?
3. Decide the English-layer choice: the existing REDACT detector, or spaCy
   + dictionary only.
