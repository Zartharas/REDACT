# OpenPII (French) → REDACT canonical-type mapping

REDACT's canonical detection vocabulary (`src/detect.py`) is six types:
`EMAIL`, `SSN`, `CREDIT_CARD`, `PERSON`, `IP`, `MRN`. `ai4privacy/open-pii-
masking-500k-ai4privacy`'s `privacy_mask` field annotates its own broader,
flatter PII taxonomy. This document decides, for each label observed in the
11-document French pilot sample staged in `datasets/OpenPII_FR_raw.jsonl`,
whether it maps to a REDACT canonical type, and states the reasoning —
including for labels that do NOT map, so the exclusion is a decision, not
an oversight. Same format as `MEDDOCAN_TYPE_MAPPING.md`, mirrored
deliberately for direct comparison.

**A note on sample size before the tables**: counts below are out of 27
gold spans across 11 documents — a small pilot sample (see
`prepare_fr_dataset.py`'s "COVERAGE" section), not a systematic corpus.
Percentages here are directional, not a confident final read.

## Mapped (in scope for the evaluation harness)

| ai4privacy label | REDACT type | Rationale | Count (of 27 staged) |
|---|---|---|---|
| `GIVENNAME` + `SURNAME` | `PERSON` | Given name and surname spans, almost always adjacent ("Naphtali" + "Mártin de Tavernier Bronchales", "Evica" + "Shalaku Tepedino"). Same semantic category as REDACT's PERSON. Format is space-separated French given/surnames — same shape as MEDDOCAN's Spanish names, and the same gap applies: REDACT's `flattened_names.py` dictionary layer (concatenated-token matching) will NOT catch these as-is; a name-dictionary-over-capitalized-word-runs layer (mirroring `es_detect.py`'s `scan_spanish_names()`) is needed, built as `fr_detect.py`'s `scan_french_names()`. | 11 (8 GIVENNAME + 3 SURNAME) |
| `EMAIL` | `EMAIL` | Email address. Format is language-agnostic (`user@domain.tld`) — REDACT's existing `EMAIL` regex in `detect.py` already catches this format with zero modification needed, same conclusion MEDDOCAN's mapping reached for `CORREO_ELECTRONICO`. | 1 |
| `CREDITCARDNUMBER` | `CREDIT_CARD` | Credit card number. Bare digit string (13-19 digits observed: "3132629134190324844", "4543256230675317") — REDACT's existing `CREDIT_CARD` regex (`\d{12,19}`) already matches this format with zero modification needed. | 2 |

**Mapped subtotal: 14 of 27 spans (52%) across 3 ai4privacy labels (PERSON via 2 source labels, EMAIL, CREDIT_CARD).**

## Explicitly out of scope (no REDACT canonical type — and why)

| ai4privacy label | Why out of scope | Count |
|---|---|---|
| `TELEPHONENUM` | Phone number. REDACT has no PHONE type — the same scoping gap MEDDOCAN's mapping already flagged for `NUMERO_TELEFONO`/`NUMERO_FAX` (see that document's "Recommendation" section). Worth naming again here since it recurs: two different real datasets in two different languages both surface phone numbers as a common PII category REDACT doesn't yet claim. | 2 |
| `CITY` | Geographic location. No REDACT LOCATION type — same reasoning as MEDDOCAN's `TERRITORIO`/`PAIS` exclusion. | 2 |
| `DATE` | Dates. No REDACT DATE type — dates alone are not the re-identifying risk category REDACT targets. Same reasoning as MEDDOCAN's `FECHAS`. | 2 |
| `AGE` | Age. No REDACT AGE type; same reasoning as MEDDOCAN's `EDAD_SUJETO_ASISTENCIA` (HIPAA Safe Harbor itself only restricts ages ≥90, a narrower rule than blanket redaction). | 2 |
| `ZIPCODE` | Postal code. No REDACT LOCATION/ADDRESS type — same family as MEDDOCAN's `CALLE`/`TERRITORIO` exclusions. | 2 |
| `TIME` | Clock time (distinct from `DATE` in this dataset's schema). Not an identifier on its own. No REDACT type. | 2 |
| `SEX` | Sex/gender marker ("O" for the French non-binary/other marker observed in this sample). No REDACT type; not an identifier on its own — same reasoning as MEDDOCAN's `SEXO_SUJETO_ASISTENCIA`. | 1 |

**Out-of-scope subtotal: 13 of 27 spans (48%) across 7 ai4privacy labels.**

## Labels present in ai4privacy's broader taxonomy but NOT observed in this pilot sample

The full ai4privacy label set (seen across the dataset's other languages
while paging for French rows, not all within the French subset itself)
also includes `SOCIALNUM`, `IDCARDNUM`, `STREET`, `BUILDINGNUM`, `TITLE`,
`DRIVERLICENSENUM`, `PASSPORTNUM`, `GENDER`, and a template artifact,
`LANGUAGEPLACEHOLDER` (not a real PII type — a leftover masking-template
token, excluded outright, same treatment `PLACEHOLDER`-style artifacts
would get in any dataset). None of these appeared in this specific
11-document French sample, so none are mapped or excluded with real counts
here — that would be asserting a decision about data not actually seen.

Two are worth flagging explicitly rather than silently deferring:
- `SOCIALNUM` is the closest candidate for a French-analogue `SSN` mapping
  (the way MEDDOCAN's NASS field mapped to SSN) — but it did not appear in
  this sample, so no French-specific SSN-format regex is built in this
  pass. If a larger sample surfaces real French `SOCIALNUM` examples
  (France's INSEE/NIR number has a well-known 13+2-digit structure), this
  mapping and a corresponding regex should be added then, not guessed at
  now from zero real examples.
- `IDCARDNUM` did not appear in this sample either. Unlike MEDDOCAN's
  `ID_SUJETO_ASISTENCIA` (a hospital patient-record number with a direct
  MRN interpretation), ai4privacy's `IDCARDNUM` is a general national-ID-
  card number with no medical-record framing — mapping it to REDACT's MRN
  type would be a stretch of MRN's own semantic meaning, not a natural
  fit, so it would likely be excluded even if observed. Recorded here so
  the absence is visible as "not yet evaluated," not "found and rejected."

## Net result

Of the ai4privacy labels actually observed in this French pilot sample,
**3 map onto REDACT's existing 6-type canonical vocabulary** (PERSON via 2
source labels, EMAIL, CREDIT_CARD — IP has no ai4privacy equivalent, and
SSN/MRN have no *observed* French-specific analogue in this small sample;
see the section above). The evaluation harness (`evaluate_fr.py`) scores
REDACT's French detection against only these 3 mapped types; the other 7
observed labels are real PII by ai4privacy's own annotation standard but
are not claims REDACT makes, so scoring against them would not test
anything REDACT actually does.

**Recommendation for the paper's discussion/future-work section:** same
phone-number gap MEDDOCAN's mapping already named, now confirmed
recurring in a second, unrelated dataset and language — worth stating as
a real, repeated scoping gap rather than a one-off. Separately: this
pilot sample is too small to confirm or rule out a French `SOCIALNUM` ->
`SSN` mapping; growing the staged sample (see `prepare_fr_dataset.py`)
is the concrete next step before that decision can be made on real
evidence rather than the taxonomy's label name alone.
