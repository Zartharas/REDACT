# OpenPII (Indonesian) → REDACT canonical-type mapping — PROPOSED, PENDING REAL DATA

**Status: proposed from the source dataset's known label schema, NOT yet
verified against real staged Indonesian examples.** See
`prepare_id_dataset.py` for why: `ai4privacy/pii-masking-openpii-1.5m`
(the flagship dataset, confirmed to include `language: id`) could not be
reliably staged from this sandbox — Hugging Face's `datasets-server` row
API returned empty responses across ~10 attempts at different offsets and
splits. This document is built from the label schema observed directly in
OTHER languages' rows of the SAME dataset (Korean, Vietnamese, Japanese —
fetched successfully at `offset=0` before the API became unreliable) and
from `FR_TYPE_MAPPING.md`'s already-verified mapping for the sibling
French dataset, which shares the same `privacy_mask` label taxonomy. Every
row of every language in this dataset family uses the identical label set
— so the mapping below is a well-founded proposal, not a guess — but it
has not been checked against a single real Indonesian sentence, and that
distinction matters enough to state on every table row below rather than
bury in a footnote.

**Do not treat any count in this document as real** — there are none,
by design, until `datasets/OpenPII_ID_raw.jsonl` exists.

## Proposed mapping (pending verification once Indonesian rows are staged)

| ai4privacy label | REDACT type | Rationale | Verified against real Indonesian rows? |
|---|---|---|---|
| `GIVENNAME` + `SURNAME` | `PERSON` | Same reasoning as `FR_TYPE_MAPPING.md`'s PERSON mapping — space-separated given/surname spans, same schema. | **No — pending** |
| `EMAIL` | `EMAIL` | Language-agnostic format; REDACT's existing regex should already catch it, same conclusion reached for Spanish, French, and Russian. | **No — pending** |
| `CREDITCARDNUMBER` | `CREDIT_CARD` | Bare digit string; REDACT's existing regex format-matches, though whether the Luhn check passes on this dataset's specific synthetic values is unknown until real data is staged — see `MEDDOCAN_VALIDATION_REVIEW_SUMMARY.md` Sections 7 and 8 for why this is a real, dataset-specific empirical question, not something to assume either way. | **No — pending** |
| `SOCIALNUM` | `SSN` | This flagship dataset's schema includes `SOCIALNUM` (observed directly in a Vietnamese row fetched this session: `"số an sinh xã hội (9632359792)"`, tagged `B-SOCIALNUM`) — unlike the smaller French dataset, where `SOCIALNUM` exists in the taxonomy but did not appear in the specific 11-row sample staged (see `FR_TYPE_MAPPING.md`'s "labels present but NOT observed" section). If Indonesian rows carry `SOCIALNUM` examples once staged, this is the strongest SSN candidate — Indonesia's real-world NIK (Nomor Induk Kependudukan, 16-digit national ID) is the closest analogue, though `SOCIALNUM` vs `IDCARDNUM` is itself a mapping decision to re-check once real examples are in hand (see note below). | **No — pending** |

**No proposed MRN or IP mapping.** This dataset family's label schema
(same across every language observed so far: GIVENNAME, SURNAME, TITLE,
DATE, EMAIL, TELEPHONENUM, DRIVERLICENSENUM, CITY, STREET, BUILDINGNUM,
ZIPCODE, TAXNUM, SOCIALNUM, PASSPORTNUM, IDCARDNUM, CREDITCARDNUMBER,
AGE, SEX, GENDER) has no medical-record-context label (nothing like
MEDDOCAN's `ID_SUJETO_ASISTENCIA` or the Russian benchmark's `OMS`) and
no network-address label at all — unlike the Russian pass, which found
and closed a genuine `IP` gap. Rather than force a stretch mapping the
way this project explicitly avoided doing for MEDDOCAN's professional-
license number or the Russian passport/driver's-license types, MRN and
IP are left unmapped for Indonesian pending either (a) real staged data
revealing a label that fits better than expected, or (b) accepting that
this dataset family simply doesn't exercise those two REDACT types.

## Open question to resolve once real data is staged

`IDCARDNUM` (general national ID card number, same label French's
mapping excluded — see `FR_TYPE_MAPPING.md`'s reasoning for why it
doesn't cleanly fit MRN) vs `SOCIALNUM` (proposed above for SSN) may
overlap or compete for the same real-world Indonesian identifier (NIK)
depending on how ai4privacy's synthetic-data generator actually labels
Indonesian ID numbers — this cannot be resolved from schema alone and
needs real examples. Flagged here as the first thing to check once
`OpenPII_ID_raw.jsonl` exists, not deferred silently.
