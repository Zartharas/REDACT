# MEDDOCAN → REDACT canonical-type mapping

REDACT's canonical detection vocabulary (`src/detect.py`) is six types:
`EMAIL`, `SSN`, `CREDIT_CARD`, `PERSON`, `IP`, `MRN`. MEDDOCAN annotates 22
defined entity types (21 of which actually occur in the 520 documents
staged so far). This document decides, for each MEDDOCAN type, whether it
maps to a REDACT canonical type, and states the reasoning — including for
the types that do NOT map, so the exclusion is a decision rather than an
oversight.

## Mapped (in scope for the evaluation harness)

| MEDDOCAN type | REDACT type | Rationale | Count (of 12,048 staged) |
|---|---|---|---|
| `NOMBRE_SUJETO_ASISTENCIA` | `PERSON` | Patient full name. Same semantic category as REDACT's PERSON; format is "First Last" or "First Middle Last" Spanish names, space-separated — REDACT's flattened-name layer (concatenated-token matching) will NOT catch these as-is; NER is the layer that would, and NER is unavailable in this sandbox (network-blocked spaCy/Presidio model download, documented elsewhere in this validation set). Scored, but expect near-zero recall from the sandbox-limited harness — disclosed, not hidden. | 1,046 |
| `NOMBRE_PERSONAL_SANITARIO` | `PERSON` | Clinician/healthcare-personnel full name. Same PERSON category and same space-separated-name caveat as above. | 1,039 |
| `CORREO_ELECTRONICO` | `EMAIL` | Email address. Format is language-agnostic (`user@domain.tld`); REDACT's existing `EMAIL` regex should already catch essentially all of these with zero modification needed. | 504 |
| `ID_SUJETO_ASISTENCIA` | `MRN` | Patient ID / NHC ("Número de Historia Clínica"). This is the direct real-world analogue to REDACT's MRN gap (previously regex-only against a synthetic `MRN-\d{7}` pattern with zero real ground truth). Real format is inconsistent — plain digits ("368503"), digits with an embedded space ("9764132 3"), or prefixed ("nhc-272226", "CIPA: nhc-150679") — REDACT's current strict `MRN-\d{7}` pattern will not match any of these; a new Spanish-aware pattern is required (Task 5). | 608 |
| `ID_ASEGURAMIENTO` | `SSN` | NASS (Spanish Social Security affiliation number), the closest real-world analogue to US SSN — a government-issued individual identifier used for social-insurance purposes. Real format varies: two-digit + 8-digit + 2-digit blocks with spaces ("26 63514095 08"), hyphenated ("55-55012378-99"), or occasionally missing the check block entirely ("26 63514095"). Needs its own Spanish-aware pattern, not a reuse of the 9-digit US SSN regex. | 429 |

**Mapped subtotal: 3,626 of 12,048 spans (30%) across 5 MEDDOCAN types.**

## Explicitly out of scope (no REDACT canonical type — and why)

REDACT's vocabulary is deliberately narrow (six types tied to concrete
enterprise-telemetry PII/PHI risks: identity, contact, financial,
government-ID, network, healthcare-record). MEDDOCAN's clinical-narrative
annotation scheme covers a broader span of what counts as PHI under
HIPAA's Safe Harbor list (18 identifier categories) — most of which
REDACT was never scoped to detect in security telemetry, because these
categories rarely appear in logs (Windows Event, Syslog, CloudTrail) at
all. Each is listed so the exclusion is visible and intentional:

| MEDDOCAN type | Why out of scope | Count |
|---|---|---|
| `TERRITORIO` | Geographic subdivision (city, province, postal code). REDACT has no LOCATION type — out of scope for security-telemetry PII, which rarely contains addresses. | 2,079 |
| `FECHAS` | Dates (birth date, admission date, etc.). REDACT has no DATE type — dates alone are not the re-identifying risk category REDACT targets, and are already ubiquitous, non-sensitive-by-default in log timestamps. | 1,386 |
| `EDAD_SUJETO_ASISTENCIA` | Patient age. No REDACT AGE type; HIPAA Safe Harbor itself only restricts ages ≥90, a narrower rule than blanket redaction. | 1,054 |
| `SEXO_SUJETO_ASISTENCIA` | Sex/gender marker. No REDACT type; not an identifier on its own. | 948 |
| `CALLE` | Street address. No REDACT LOCATION/ADDRESS type. | 907 |
| `PAIS` | Country. No REDACT LOCATION type. | 763 |
| `ID_TITULACION_PERSONAL_SANITARIO` | Clinician's professional-license/collegiate number ("NºCol"). This identifies a *healthcare worker*, not a patient — outside REDACT's patient-PHI threat model, and not a category REDACT's MRN/SSN patterns were designed for. | 481 |
| `HOSPITAL` | Hospital/institution name. No REDACT ORG type. | 294 |
| `FAMILIARES_SUJETO_ASISTENCIA` | Mentions of family members ("madre", "hermano gemelo"). Relational, not a direct identifier value — no REDACT type covers this. | 224 |
| `INSTITUCION` | Non-hospital institution name (universities, etc.). No REDACT ORG type. | 126 |
| `ID_CONTACTO_ASISTENCIAL` | Episode/contact ID (internal hospital case-tracking number). Institution-internal, not an individual identifier in REDACT's sense — closer to a case/ticket number than an MRN. | 83 |
| `NUMERO_TELEFONO` | Phone number. REDACT has no PHONE type — a real gap worth flagging for future scope (see Recommendation below), but not retrofitted onto an existing type. | 43 |
| `OTROS_SUJETO_ASISTENCIA` | Catch-all/other patient-identifying phrase. Too heterogeneous to map to any single REDACT type. | 10 |
| `NUMERO_FAX` | Fax number. Same reasoning as phone. | 10 |
| `PROFESION` | Patient's stated profession. Not an identifier value. | 10 |
| `CENTRO_SALUD` | Health center name. Same as HOSPITAL/INSTITUCION. | 4 |

**Out-of-scope subtotal: 8,422 of 12,048 spans (70%) across 16 MEDDOCAN types.**

## Net result

Of MEDDOCAN's 22 defined types, **5 map onto REDACT's existing 6-type
canonical vocabulary** (PERSON ×2 source types, EMAIL, MRN, SSN — IP and
CREDIT_CARD have no MEDDOCAN equivalent since clinical narratives don't
carry network addresses or payment-card numbers). The evaluation harness
(Task 6) will score REDACT's detection against only these 5 mapped types;
the other 16 are real PHI by HIPAA's standard but are not claims REDACT
makes, so scoring against them would not test anything REDACT actually
does — the exclusion is a scope decision, not a result being hidden.

**Recommendation for the paper's discussion/future-work section:**
`NUMERO_TELEFONO`/`NUMERO_FAX` (phone/fax) is the one MEDDOCAN category
that plausibly *should* be a REDACT type given how often phone numbers
appear in real enterprise telemetry (support tickets, contact fields) —
worth naming explicitly as a scoping gap rather than leaving it as a
silent omission.
