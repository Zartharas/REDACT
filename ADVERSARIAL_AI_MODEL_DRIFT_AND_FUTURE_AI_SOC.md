# Adversarial AI, model drift, and future AI-assisted telemetry analysis

Manuscript revision support (minor suggestion #1: "expand discussion of
adversarial AI, model drift, and future AI-assisted telemetry analysis
challenges"). This completes the suggestion's broader literature/narrative
component — the concrete, measurable sub-piece (adversarial evasion
against REDACT's own detection layers) was already built and run live as
`validation/adversarial_evasion/` (see `BUGS_AND_FIXES.md` "Engineering
upgrade 23"). That work is the empirical anchor for Section 1 below, not
repeated in full here. Current as of research conducted 2026-09; several
cited papers are from within the last few months and should be treated as
recent, not settled, literature.

## 1. Adversarial AI against PII/log-privacy systems

REDACT's own evasion-testing experiment is the concrete evidence base for
this section, not a hypothetical: three of six PII types (EMAIL, IP,
MRN) evaded REDACT's detection ensemble completely using ordinary,
non-adversarial format variants (IP defanging, `[at]`/`[dot]` email
obfuscation), and PERSON — already the weakest-baseline type at 31.9%
miss on un-evaded names — degraded further under every character-level
technique tested, up to 99.2% evasion for a single digit inserted into a
flattened username (`validation/adversarial_evasion/README.md`). This
sits inside a broader, active research area: evasion attacks generally
(manipulating input to cause misclassification without touching the
model or training data) are a well-established adversarial ML category,
with open tooling like IBM's Adversarial Robustness Toolbox now
implementing 40+ such attacks and 30+ defenses across major ML
frameworks. REDACT's own evasion techniques are hand-designed against
specific, cited mechanisms in `src/detect.py` rather than drawn from a
generic attack library — a narrower, more targeted approach than ART's,
appropriate for a system with six known entity types and three known
detection layers rather than an arbitrary black-box model, but a smaller
slice of the possible attack surface than a systematic tool-driven search
would cover.

**A finding that raises REDACT's own results' stakes, not just parallels
them:** the same log fields REDACT's evasion testing targets — usernames
specifically — are independently identified in current SOC/LLM security
research as attacker-controlled fields carrying real risk beyond privacy
alone. Pandey and Bhujang's "Poisoning the Watchtower" (arXiv:2605.24421,
May 2026) studies "log-substrate prompt injection" against LLM-augmented
SOC analyst tools, and names user agents, URLs, payloads, DNS queries,
and **attempted usernames** as the exact fields that can carry
instructions to a model alongside evidence of an intrusion — the identical
field category (usernames/PERSON) REDACT's own Layer 4 flattened-name
detector already discloses as its single most important measured gap.
This means an unredacted or evasion-defeated username in a log line is
not only a privacy exposure under REDACT's own scope — if that same log
line is later fed to an LLM-based SOC copilot (Section 3 below), it is
simultaneously a potential prompt-injection vector, a security risk
distinct from and additional to the privacy one. REDACT does not claim to
address prompt injection (out of its own detection scope — it identifies
PII types, not adversarial instructions), but the overlap is a genuine,
citable connection between REDACT's own measured weakness and a
contemporaneous, independent finding in adjacent research.

## 2. Model drift — two distinct things REDACT does and doesn't measure

**What REDACT already has, real and shipped:** `src/drift.py` implements
field-level *taxonomy* drift detection — tracking what fraction of a
given field's values carry Critical-tier PII in a current window versus
a baseline window, and flagging a field whose rate moves by more than a
threshold (`compare()`/`compare_all()`, with an opt-in Prometheus/
Alertmanager wiring — see ROADMAP.md item 13). This catches a specific,
real failure mode: a field that never carried PII silently starting to
carry it after an unrelated code change. It is *data* drift monitoring at
the field level, not model drift in the classical ML sense — REDACT's
detection ensemble is never retrained or fine-tuned, so there is no
model-weights drift to monitor; `drift.py` monitors whether the *input
distribution* changes, using the existing fixed detector as the
measuring instrument. Current ML-monitoring literature draws exactly this
distinction (data drift vs. concept drift vs. model/parameter drift — see
the 136-paper multivocal literature review at arXiv:2509.14294), and
`drift.py`'s own naming (`CRITICAL_TYPES`/`SENSITIVE_TYPES`) already has a
documented internal naming collision against the anonymization taxonomy's
own same-named tiers (`TAXONOMY_AND_TRANSFORMATION_LOGIC.md` Section 4) —
worth citing together, since both are examples of this project's own
discipline of surfacing exactly this kind of ambiguity rather than
letting it sit unexamined.

**What REDACT has real, already-measured evidence of, but has never
framed as "drift":** REDACT's own detection layers show a strong,
already-quantified sensitivity to population/format shift — the
underlying signal classical model drift monitoring exists to catch, even
though REDACT's own model weights never change. Three existing,
previously-published results, reframed under this lens rather than
re-measured:

- **Population shift, most extreme case:** the flattened-name dictionary
  layer's recall collapses from 50.3% (matched against its own
  Faker-sourced population) to 1.4% against a disjoint non-US-locale name
  population, landing at 15.2% against real US SSA/Census-weighted name
  frequency (`validation/real_name_frequency/`, `flattened_names.py`'s
  own docstring). A detector whose accuracy depends this heavily on
  whether the input population matches its reference dictionary is, by
  construction, a detector highly exposed to distributional drift the
  moment a deployment's real user population differs from what it was
  tuned against — not a hypothetical risk, a measured one.
- **Domain shift:** PIIBench cross-domain evaluation measured REDACT's
  detection component at P=0.354/R=0.650 on general free text, well
  below its own synthetic-corpus numbers — explicitly scoped in
  `ROADMAP.md` item 14a as "not a validation of REDACT's log-pipeline
  claim," but exactly the kind of number a drift-style domain-shift check
  would want to track over time if REDACT's actual production log mix
  ever moved away from the log-shaped text it was built and tuned
  against.
- **Format shift within a known domain:** the real-vs-synthetic
  precision gap across five Loghub datasets (`README.md`'s real-data
  validation section) is itself evidence that format distribution,
  independent of population, materially moves detection accuracy.

None of this is currently wired into `drift.py`'s own monitoring —
`drift.py` watches whether a *field* starts carrying more or less PII
over time, not whether the *detector's own accuracy* on a fixed
population is degrading. Building that second kind of monitoring (a
held-out accuracy check re-run periodically against a reference set,
the standard model-drift-monitoring pattern in the literature above)
is a real, identifiable gap, not built here — matching this document's
own scope as narrative/synthesis, not new engineering.

## 3. Future AI-assisted telemetry analysis challenges

LLM-based SOC copilots are moving from prototype to production-adjacent
deployment during the same window this project has been active — real,
current examples include Microsoft Security Copilot (commercial,
enterprise-integrated) and academic systems like OpenSOC-AI
(arXiv:2604.26217, parameter-efficient LLM log analysis aimed at
democratizing SOC tooling for smaller organizations without commercial
Copilot budgets). The architecture pattern across both is structurally
similar: raw security events flow into a SIEM, and an LLM performs
contextual analysis, incident summarization, or triage suggestions
directly against that raw content.

This raises a concern squarely inside REDACT's own scope, not adjacent to
it: **an LLM-augmented SOC pipeline that ingests raw (non-anonymized) logs
inherits every privacy exposure this project exists to close, at a new
layer.** A username or IP address REDACT would otherwise tokenize or
pseudonymize doesn't just sit in a SIEM index if that same log line is
also passed to an LLM for summarization — it becomes part of a model's
context window, potentially logged again by the LLM provider, and (per
Section 1's finding) potentially carries adversarial content in the same
field. The EU AI Act's Article 19 (`INTERNATIONAL_PRIVACY_FRAMEWORKS.md`
Section 4) requiring automatically-generated, tamper-evident logs for
high-risk AI systems is directly relevant here from the opposite
direction: an LLM-augmented SOC tool serving EU users may itself become a
regulated high-risk AI system whose own logging is now legally mandated,
creating a second log stream (the copilot's own audit trail) that would
need the same PII-discipline REDACT already applies to the SOC's primary
telemetry — a compounding requirement, not a substitute for it.

**Devil's-advocate synthesis, tying Sections 1-3 together:** the
practical recommendation an LLM-augmented SOC pipeline should draw from
this isn't "add more PII detection somewhere" in the abstract — it's that
REDACT's own measured evasion results (Section 1) mean anonymizing logs
*before* they reach an LLM is not a solved problem just because REDACT
exists in the pipeline. A defanged IP or an `[at]`-obfuscated email that
evades REDACT's detection today would reach an LLM-based analyst
completely unredacted, at the exact moment that field might also be an
attacker's chosen prompt-injection vector (Section 1's citation).
"Tokenize PII before LLM calls" is already a stated best practice in
current SOC-copilot security guidance — but it is only as strong as the
detection layer performing that tokenization, and this project has now
measured, concretely, where that layer's coverage actually ends.

## What this document doesn't establish

Not a new experiment — Sections 2 and 3 synthesize REDACT's own
already-published results (population/domain/format-shift numbers,
Section 1's evasion results) under a framing (drift, adversarial risk to
downstream LLM pipelines) the original publications didn't use, rather
than measuring anything new. Not a claim that REDACT detects or defends
against prompt injection — that is out of REDACT's actual scope (PII
type detection, not adversarial-instruction detection), and the
connection drawn in Section 1 is a citation of independent research, not
a REDACT capability. Not a completed model-drift-monitoring feature — the
real gap identified in Section 2 (periodic accuracy re-checks against a
reference population, as distinct from `drift.py`'s existing field-level
data-drift monitoring) is named but not built, consistent with this
document's own scope as literature synthesis rather than new engineering.

## Sources

- [Detection and prevention of evasion attacks on machine learning models — ScienceDirect](https://www.sciencedirect.com/science/article/abs/pii/S0957417424029117)
- [Adversarial Robustness: Hardening AI Models Against Attacks (2026) — AI Safety Directory](https://aisecurityandsafety.org/en/guides/ai-adversarial-robustness/)
- [Poisoning the Watchtower: Prompt Injection Attacks Against LLM-Augmented Security Operations Through Adversarial Log Content — arXiv:2605.24421](https://arxiv.org/abs/2605.24421)
- [OpenSOC-AI: Democratizing Security Operations with Parameter Efficient LLM Log Analysis — arXiv:2604.26217](https://arxiv.org/abs/2604.26217)
- [Monitoring Machine Learning Systems: A Multivocal Literature Review — arXiv:2509.14294](https://arxiv.org/pdf/2509.14294)
- [Model Drift vs Data Drift in 2026: Detection & Mitigation Guide — FutureAGI](https://futureagi.com/blog/model-vs-data-drift-how-to-identify-and-handle-it/)
- [Model Drift vs. Concept Drift: Detection & Mitigation for 2026 — Lumenova AI](https://www.lumenova.ai/blog/model-drift-concept-drift-introduction/)
- [Building an AI Powered Security Operations Center (SOC) — Medium](https://medium.com/@bervice/building-an-ai-powered-security-operations-center-soc-4e85e4dc92c8)
- [Microsoft Copilot Security: 2026 Guide to Risks, Oversharing & Safe Enterprise Rollout — Strac](https://www.strac.io/blog/microsoft-copilot-security)
