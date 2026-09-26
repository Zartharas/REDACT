# International privacy frameworks beyond GDPR and HIPAA

Manuscript revision support (minor suggestion: "include additional
discussion of international privacy frameworks beyond GDPR and HIPAA").
Legal/regulatory research, not an engineering deliverable — no code
changes accompany this document. **Not legal advice**; a licensed
attorney in each relevant jurisdiction should be consulted before any
compliance claim is made based on this document. Current as of research
conducted 2026-09, several of these frameworks are actively in flux (see
Section 4), and this is not exhaustive of the roughly 140 national data
protection laws now in force worldwide — six jurisdictions were chosen
for genuine architectural relevance to REDACT's own design, not
comprehensiveness.

## 1. Why this matters specifically for security telemetry, not privacy law in general

A single SOC ingesting logs from a global user base is processing
personal data that is simultaneously subject to several of these regimes
at once — an authentication failure log entry naming an EU resident, a
California consumer, and a China-based employee can appear in the same
10,000-line batch REDACT processes as one corpus. Two properties specific
to this project's actual design turn out to matter far more than a
general privacy-law survey would suggest: (1) whether a jurisdiction
treats **pseudonymized** data as still being "personal data" in full
legal scope (this determines whether REDACT's own IP/PERSON pseudonymize
tier, see `TAXONOMY_AND_TRANSFORMATION_LOGIC.md` Section 3, actually
exits a given regulation's scope or merely reduces the compliance burden
within it), and (2) whether cross-border transfer of that data — to a
centralized SIEM, a shared `TokenStore`, or a cloud log lake, all three
of which this project has now built and run live (`validation/siem_splunk/`,
`validation/cloud_loglake/`) — is itself independently restricted,
regardless of how well the data was anonymized before it moved.

## 2. Framework survey, six jurisdictions

| Jurisdiction | Law | Personal-data-scope test for pseudonymized data | Cross-border transfer mechanism | Status, 2026 |
|---|---|---|---|---|
| EU | GDPR (already covered elsewhere in this project — see below) | Remains personal data (EDPB Guidelines 01/2025) | Adequacy decisions, SCCs, BCRs | In force; see Section 4's DPF note for the transatlantic-specific complication |
| California, US | CCPA/CPRA | Two-tier, distinct from GDPR: **pseudonymous** data can still be re-identified by the business internally (kept separate, access-controlled) and is exempt from access/deletion/rectification rights but not fully out of scope; **de-identified** data (the business cannot reasonably re-identify it, plus a public non-re-identification commitment and a contractual flow-down to recipients) is the CCPA's actual "anonymized" tier and exits scope entirely | No formal transfer mechanism comparable to GDPR adequacy; contractual "service provider" terms govern most practical cases | In force |
| China | PIPL, plus the 2026 Personal Information De-identification Guide (draft) | Explicitly two technical paths, not one: "desensitisation" (simple) and "pseudonymisation" (complex) — both remain personal information in PIPL's scope; only true, irreversible anonymization exits scope, and de-identification generally (including pseudonymization) is explicitly treated as reversible and therefore still covered | Three-pathway system as of January 1, 2026: CAC Security Assessment, China Standard Contract, or Certification (new certification measures effective Jan 1, 2026) — a real, current data-localization-adjacent regime | In force, three-pathway system now complete |
| Brazil | LGPD | Same structural split as GDPR: anonymization exits scope (unless reversible in practice), pseudonymization remains personal data, explicitly described as "a valuable security measure" that does not change legal status | Adequacy-style and contractual mechanisms, broadly GDPR-influenced | In force; ANPD's 2025-2026 regulatory agenda explicitly lists "regulating anonymization and pseudonymization techniques" as a still-open rulemaking item — the exact technical bar is not yet finalized |
| India | DPDP Act 2023 + DPDP Rules (notified Nov 14, 2025) | Rules are new enough that de-identification/anonymization technical standards are not yet as litigated as GDPR's; breach notification is fast and firm regardless — Data Protection Board notified "without delay" plus a detailed report within 72 hours, every affected Data Principal individually notified | Framework still maturing; full day-to-day compliance obligations phase in over 18 months, targeting mid-May 2027 | Rules notified, phased implementation in progress |
| Singapore | PDPA | Anonymization (explicitly including pseudonymization as one of several listed techniques, alongside aggregation, masking, generalization) that succeeds in preventing identification of a particular individual exits PDPA scope entirely — a notably more permissive bar than GDPR/PIPL/LGPD's "pseudonymization always stays personal data" position, worth flagging as a genuine cross-jurisdictional divergence, not an oversight in this table | Comparable-protection standard (not identical-protection) via contractual clauses, binding corporate rules, or certification (APEC CBPR) | In force; PDPC's April 2026 guidance sharpened cross-border transfer expectations further |

**A genuine, material divergence this table surfaces, not an assumption
carried over from GDPR:** Singapore's PDPA treats successful
pseudonymization as capable of exiting personal-data scope entirely,
while the EU, Brazil, and China all treat it as remaining in scope
regardless of technical strength. This means REDACT's IP/PERSON
pseudonymize tier could support a materially different compliance
posture depending on which jurisdiction's law governs a given dataset —
a single global taxonomy config (`config/policy.json`) cannot make this
distinction on its own; the legal conclusion depends on jurisdiction, not
just on the transformation applied. This is exactly the kind of gap a
one-size-fits-all "Sensitive → Pseudonymize" tier obscures if the
manuscript doesn't flag it explicitly.

## 3. Mapping REDACT's actual transformations against each bar

Reusing `TAXONOMY_AND_TRANSFORMATION_LOGIC.md`'s three real, shipped
mechanisms (Section 3 there) rather than restating them abstractly:

**Redact** (irreversible placeholder replacement): exits personal-data
scope under every framework surveyed here, including Singapore's — no
framework treats an irreversibly destroyed value as still personal data,
since there is nothing left to re-identify. The cost, already measured
in `validation/investigative_utility/` (1.0 false-linkage rate), is paid
in investigative utility, not legal risk.

**Pseudonymize** (`hmac.new(key, ...)`, one-way, deterministic,
correlatable): stays in-scope as personal data under GDPR, PIPL, and
LGPD. Exits scope under Singapore's PDPA if the specific implementation
genuinely prevents re-identification of a particular individual — which
is the same empirical question `validation/investigative_utility/`
already measured for a different purpose (30/30 = 100% candidate-list
re-identification success given the key and a short candidate list). That
measured result is directly relevant here: it suggests REDACT's current
pseudonymization implementation, for a low-entropy input space like a
name or IP address, likely does NOT clear even Singapore's more
permissive bar in practice, since an investigator with the key and a
short candidate list can already re-identify it reliably — the same
finding that keeps it "personal data" under GDPR's EDPB Guidelines 01/2025
would very plausibly also keep it in-scope under Singapore's "genuinely
cannot identify" standard, once tested against it directly. This is
presented as a reasoned inference from an existing measured result, not a
new experiment run against Singapore's specific legal test — flagged as
such rather than asserted as a legal conclusion.

**Tokenize** (`TokenStore`, real reversible lookup, resolvable given
store access): stays in-scope as personal data everywhere, and rightly
so — full reversibility is the point of choosing this tier for Critical
data, and no framework surveyed treats genuinely reversible data as
anonymized. The relevant question these frameworks add is not whether
tokenized data is personal data (it obviously is) but WHERE the token
store itself may lawfully be located and accessed from, which Section 4
below addresses directly rather than assuming a single global answer.

## 4. Devil's-advocate: what this table doesn't resolve on its own

**Cross-border data flow, not just anonymization strength, is now an
independent, moving constraint on where REDACT's real infrastructure can
live — and the ground shifted mid-2026, not years ago.** The EU-US Data
Privacy Framework — the adequacy mechanism a project running `TokenStore`
or a Splunk/Azure Blob log lake (`validation/siem_splunk/`,
`validation/cloud_loglake/`, both built and run live this session) on
US-based infrastructure would lean on for EU-sourced log data — remains
valid law as of mid-2026 (the European Commission's adequacy decision
stands, upheld by the EU General Court in September 2025), but is under
live challenge: on June 29, 2026 the US Supreme Court's ruling in *Trump
v. Slaughter* restricted the President's removal power over FTC
Commissioners, and privacy advocate Max Schrems has argued directly to
the European Commission that this undermines the DPF's required
independent-oversight guarantee, calling for the adequacy decision's
revocation — with a separate CJEU appeal of the 2025 General Court ruling
still pending. **None of this currently blocks the Azure Blob/Splunk
integrations this project built and confirmed live** — data flows are
not suspended, and the adequacy decision remains in force unless formally
repealed or annulled — but a manuscript published now describing a
"real cloud SIEM integration" as compliance-safe for EU-sourced telemetry
should disclose that the legal basis it may be relying on is actively
contested, not settled, as of the same month this chapter's engineering
work was done. This is a genuine, current instability, not a
hypothetical caveat inserted for balance.

**China's three-pathway cross-border regime is a second, independent
constraint that would apply even to a perfectly anonymized dataset if the
underlying processing activity itself falls under PIPL's security
assessment threshold** — REDACT's own architecture doesn't currently
distinguish "this log batch contains China-sourced personal information
subject to the CAC's security-assessment pathway" from any other batch;
`config/policy.json`'s taxonomy is about entity TYPE (PERSON, IP, ...),
not about the data subject's jurisdiction or the deployment's cross-border
posture. Building that distinction was out of scope for this session (no
new engineering was undertaken for this document, matching the session's
explicit "don't build speculative features" constraint) but is a real gap
worth naming plainly: a taxonomy keyed only on entity type cannot, by
itself, answer a jurisdiction-scoped legal question.

**A structural point in REDACT's favor, not a caveat:** CCPA/CPRA's own
definition of qualifying pseudonymous data — kept separate from directly
identifying information, with technical and organizational controls
preventing recombination — describes almost exactly what `TokenStore`
already does by design (forward/reverse mapping held in a separate
`StorageProvider`, never inline with the anonymized output;
`TAXONOMY_AND_TRANSFORMATION_LOGIC.md` Section 3). This wasn't built in
response to CCPA specifically — `TokenStore`'s separation predates this
document — but it's a real, favorable alignment worth citing rather than
treating every framework in this table as purely a source of new
constraints.

## What this document doesn't establish

Not a compliance certification or legal opinion for any jurisdiction.
Not exhaustive — six frameworks were chosen for architectural relevance
to REDACT's actual pseudonymize/tokenize/redact design, not as a
complete survey of global privacy law (Japan's APPI, South Korea's
PIPA, South Africa's POPIA, Canada's PIPEDA/proposed successor, and
dozens of others are real and relevant to a genuinely global deployment
but weren't researched for this pass). Does not resolve the cross-border
transfer question for any specific deployment — that requires a real
data-flow map (which jurisdictions' data lands in which storage, under
which legal transfer mechanism) this project has not built. The EU
AI Act is deliberately excluded from Section 2's table: it governs AI
*systems* (risk management, logging, human oversight for high-risk
use cases — its Article 19 automatically-generated-logs requirement is
a design point genuinely adjacent to REDACT's own `audit.py`), not
personal-data processing generally, so it answers a different question
than this document's scope and would need its own dedicated treatment
rather than a shallow mention alongside these six data-protection laws.

## Sources

- [The GDPR's Anonymization versus CCPA/CPRA's De-identification — TermsFeed](https://www.termsfeed.com/blog/gdpr-anonymization-versus-ccpa-de-identification/)
- [Anonymized and Pseudonymized Data: Are They Subject to Data Subject Requests? — TermsFeed](https://www.termsfeed.com/blog/anonymized-pseudonymized-data-subject-requests/)
- [China Data Protection and Cybersecurity: Annual Review of 2025 and Outlook for 2026 — Bird & Bird](https://www.twobirds.com/en/insights/2026/china/china-data-protection-and-cybersecurity-annual-review-of-2025-and-outlook-for-2026)
- [China PIPL Compliance Guide 2026: Localization & CAC Rules — Vucense](https://vucense.com/tech-guides/security-101/china-pipl/)
- [What is data anonymization and de-identification in China? — Chinafy](https://www.chinafy.com/blog/what-is-data-anonymization-and-de-identification-in-china)
- [Brazil's New Data Protection Roadmap: ANPD's 2025-2026 Regulatory Agenda — EuroCloud Europe](https://eurocloud.org/news/article/brazils-new-data-protection-roadmap-a-closer-look-at-the-anpds-2025-2026-regulatory-agenda-and-it/)
- [Brazil's LGPD: Anonymization, Pseudonymization — Limina](https://www.getlimina.ai/en/blog/brazils-lgpd)
- [Digital Personal Data Protection Rules, 2025 — Wikipedia](https://en.wikipedia.org/wiki/Digital_Personal_Data_Protection_Rules,_2025)
- [DPDP 72-Hour Breach Notification Rule: India Guide — Consently](https://www.consently.in/blog/dpdp-72-hour-breach-notification-rule-india-guide)
- [Singapore Data Privacy Laws: Complete PDPA Compliance Guide (2026) — Recording Law](https://www.recordinglaw.com/world-laws/world-data-privacy-laws/singapore-data-privacy-laws/)
- [Fintech Singapore: PDPA Cross-border Data Transfers (2026) — Global Law Experts](https://globallawexperts.com/pdpa-crossborder-data-transfers-fintech-singapore-2026/)
- [EU AI Act Compliance 2026: What High-risk AI Systems Must Do Now — Salt Security](https://salt.security/eu-ai-act-compliance)
- [Article 19: Automatically Generated Logs — EU Artificial Intelligence Act](https://artificialintelligenceact.eu/article/19/)
- [Article 12: Record-Keeping — EU Artificial Intelligence Act](https://artificialintelligenceact.eu/article/12/)
- [EU-US Data Transfers in 2026: The DPF, Schrems III and What Comes Next — Whispli](https://www.whispli.com/blog/how-invalid-eu-us-privacy-shield-whistleblowing-program)
- [EU-U.S. Data Privacy Framework at risk following U.S. Supreme Court ruling — activeMind.legal](https://www.activemind.legal/guides/dpf-supreme-court/)
- [Schrems addresses emerging questions around EU-US Data Privacy Framework — IAPP](https://iapp.org/news/a/schrems-addresses-emerging-questions-around-eu-us-data-privacy-framework)
