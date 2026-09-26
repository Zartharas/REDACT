# Russian PII Benchmark → REDACT canonical-type mapping

REDACT's canonical detection vocabulary (`src/detect.py`) is six types:
`EMAIL`, `SSN`, `CREDIT_CARD`, `PERSON`, `IP`, `MRN`. `redmadrobot-rnd/
pii_benchmark`'s 21-label taxonomy is grouped into four families (person,
location, contacts, Russian identity-document numbers). This document
decides, for each label observed in the 26-document pilot sample staged
in `datasets/OpenRU_PII_raw.jsonl`, whether it maps to a REDACT canonical
type. Same format as `MEDDOCAN_TYPE_MAPPING.md`/`FR_TYPE_MAPPING.md`.

**Sample size note**: counts below are out of 72 gold spans across 26
documents — a curated pilot sample selected for entity-type coverage
(see `prepare_ru_dataset.py`), not a random draw from the full
2,841-row dataset. Read as directional.

## Mapped (in scope for the evaluation harness)

| pii_benchmark label | REDACT type | Rationale | Count (of 72 staged) |
|---|---|---|---|
| `FIRST_NAME` + `LAST_NAME` + `MIDDLE_NAME` | `PERSON` | Given name, surname, and patronymic (отчество) — a genuinely new structural element neither Spanish nor French had. Russian names are conventionally three-part (e.g. "Елисей Леонтьевич" = given name + patronymic, no surname given in that sentence), and the patronymic alone is sometimes the only name element present in a sentence (`ru-pii-17`: "Вениаминович" with no first/last name nearby at all) — REDACT's PERSON type absorbs all three, same as MEDDOCAN folded two source types (patient/clinician name) into one. | 33 (11 + 11 + 11) |
| `EMAIL` | `EMAIL` | Email address. Format is language-agnostic — REDACT's existing `EMAIL` regex already catches this with zero modification, same conclusion reached for Spanish and French. | 2 |
| `CREDIT_CARD` | `CREDIT_CARD` | Credit/bank card number, 16-digit groups in various separator styles. REDACT's existing `CREDIT_CARD` regex (`\d{12,19}` + Luhn check) already matches the FORMAT with zero modification — whether the Luhn check itself passes on this dataset's specific values is an empirical question the evaluation harness answers, not assumed here (see `MEDDOCAN_VALIDATION_REVIEW_SUMMARY.md` Section 7's French CREDIT_CARD/Luhn finding for why this is worth checking rather than assuming). | 4 |
| `SNILS` | `SSN` | Individual insurance/pension account number (Страховой номер индивидуального лицевого счёта) — a mandatory, government-issued individual identifier, the direct real-world analogue to US SSN, same reasoning MEDDOCAN's NASS field used for Spanish. Canonical real-world format is 11 digits in a 3-3-3-2 grouping; observed staged examples are a bare 11-digit run ("11223344556") and a dot-separated 3-3-3-2 grouping ("123.456.789.01") — both label-anchored on "снилс"/"СНИЛС" nearby, since an unanchored 11-digit run alone is not distinctive enough to safely match (same design principle as `es_detect.py`'s NASS_ES pattern). | 2 |
| `OMS` | `MRN` | Mandatory medical insurance policy number (Обязательное медицинское страхование) — this is the one Russian ID type with an explicit medical-context framing, making it the closest real-world analogue to REDACT's MRN type, closer in fact than MEDDOCAN's own NHC mapping needed to reach (NHC is a hospital-internal patient ID; OMS is literally a *medical insurance* policy number by name). Format observed varies widely across staged examples — 16 digits with `=`, `|`, space, or no separator at all ("1234=5678=90123456", "1234|5678|90123456", "5678 9012 3456 7890", "9876543210987654") — label-anchored on "ОМС"/"полис" nearby, both because the bare-16-digit form is otherwise indistinguishable from `CREDIT_CARD` in this dataset and because an unanchored match would risk colliding with REDACT's own CREDIT_CARD regex. | 5 |
| `IP_ADDRESS` | `IP` | Network address. **A real, disclosed gap found by staging this specific dataset**: REDACT's existing `detect.py` IP regex (`\b(?:\d{1,3}\.){3}\d{1,3}\b`) is IPv4-only. This Russian sample's IP_ADDRESS spans include full IPv6 addresses ("f54a:d0a2:e874:66d7:ad66:a1be:9367:6a02", "844e:4b9f:37c8:994e:b889:7441:bf56:d85c") alongside IPv4 ("10.177.24.204") — REDACT's existing pattern would silently miss every IPv6 gold span in this sample. This is not a Russian-language-specific problem (IPv6 syntax has no locale variation) — see `src/ru_detect.py`'s docstring for why an IPv6 pattern was added there rather than left unscored, and why it is disclosed as a general-format gap this dataset happened to surface, not something intrinsic to Russian text. | 3 |

**Mapped subtotal: 49 of 72 spans (68%) across 8 pii_benchmark labels (PERSON via 3 source labels, EMAIL, CREDIT_CARD, SSN, MRN, IP).**

## Explicitly out of scope (no REDACT canonical type — and why)

| pii_benchmark label | Why out of scope | Count |
|---|---|---|
| `PHONE` | Phone number. REDACT has no PHONE type — the same recurring scoping gap already flagged for both MEDDOCAN's `NUMERO_TELEFONO` and OpenPII French's `TELEPHONENUM`. Now confirmed a third time, in a third unrelated dataset and language — this is no longer a one-off, it is a structural gap worth prioritizing in any future scope expansion. | 5 |
| `DRIVER_LICENSE` | Driver's license number. No REDACT type for non-medical, non-financial government-issued credentials beyond what SSN/MRN already cover — mapping it to either would stretch their semantic meaning past what MEDDOCAN's or this document's own SNILS/OMS mappings did. | 4 |
| `PASSPORT` | Passport number. Same reasoning as `DRIVER_LICENSE` — a general government ID, not a medical or social-insurance identifier; MEDDOCAN's own `ID_TITULACION_PERSONAL_SANITARIO` (a professional-license number) was excluded for an analogous reason. | 4 |
| `BIRTH_CERTIFICATE` | Birth certificate series/number. Same "general government document ID, no fitting REDACT type" reasoning. | 3 |
| `CITY` | Geographic location. No REDACT LOCATION type — same reasoning as MEDDOCAN's `TERRITORIO` and OpenPII French's `CITY`. | 2 |
| `DISTRICT` | Geographic subdivision. Same reasoning as `CITY`. | 1 |
| `INN` | Taxpayer identification number (Идентификационный номер налогоплательщика). A real candidate for a future SSN-adjacent mapping — it is an individual (or business) tax ID, semantically close to SNILS — but excluded here to avoid overloading REDACT's single SSN type with two different Russian ID formats in the same evaluation pass. Recorded as a disclosed alternative not pursued, not a silent omission — see "Net result" below. | 1 |
| `STREET` | Street address component. Same reasoning as `CITY`. | 1 |
| `HOUSE` | Building/house number component of an address. Same reasoning as `CITY`. | 1 |
| `MILITARY_ID` | Military service ID (военный билет). Same "general government document ID, no fitting REDACT type" reasoning as `DRIVER_LICENSE`/`PASSPORT`. | 1 |

**Out-of-scope subtotal: 23 of 72 spans (32%) across 10 pii_benchmark labels.**

## Net result

Of the pii_benchmark labels observed in this Russian pilot sample, **6 map
onto REDACT's existing 6-type canonical vocabulary** — every REDACT type
now has a mapped source in at least one of the three languages built so
far (PERSON, EMAIL, CREDIT_CARD, SSN via SNILS, MRN via OMS, IP via
IP_ADDRESS/IPv6). This is the first language pass where MRN has a
non-medical-record-office analogue this clean (OMS's literal "medical
insurance" framing) and the first where IP required new work at all
(IPv6 support, a general gap not specific to Russian).

**Recommendation for the paper's discussion/future-work section:** the
PHONE gap has now recurred in all three languages extended so far
(MEDDOCAN, OpenPII French, this dataset) — worth stating as a confirmed,
repeated, cross-language scoping gap rather than a language-specific
curiosity. Separately: `INN` is a real, disclosed candidate for a second
SSN-adjacent Russian ID type, deliberately not pursued in this pass to
avoid overloading a single REDACT type with two formats at once — a
concrete, bounded next step if Russian coverage is revisited rather than
an open-ended one.
