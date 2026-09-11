# Adversarial evasion-testing experiment

Manuscript-revision follow-up (task #24 of the "all three, in that order"
plan agreed for the remaining minor suggestions: detection tradeoffs
synthesis -> taxonomy reference -> this). Craft inputs designed to evade
REDACT's real detection layers, measure the evasion rate per technique,
and disclose the results honestly -- including any technique that evades
completely.

## Scope, stated up front

This tests REDACT's own shipped detection mechanisms (`src/detect.py`),
using techniques that target a SPECIFIC, documented mechanism in that
file -- not a generic fuzzer, and not a red-team exercise against a
production deployment. No live system, cloud account, or third party is
touched. Every technique is annotated in `generate_evasion_variants.py`
with which exact regex/NER/entropy/dictionary mechanism it targets and
why that mechanism is expected to be vulnerable to it, so a reader can
verify the claim against the actual source line rather than take it on
faith.

## Method

1. `generate_evasion_variants.py` loads real ground-truth PII spans from
   `data/synthetic_logs.jsonl` (the same corpus `DETECTION_PERFORMANCE_
   TRADEOFFS.md`'s headline numbers were measured against -- not a
   separate, cherry-picked corpus). For each span, it substitutes the
   real matched value with a transformed variant, keeping the rest of the
   real log line -- field names, punctuation, adjacent tokens -- exactly
   as REDACT's own corpus generator produced it. 18,322 evasion-variant
   records were generated from the corpus's 19,310 total PII spans
   (988 skipped where a technique didn't structurally apply, e.g. a
   credit-card number not cleanly divisible into 4-digit groups).

2. `measure_evasion.py` runs `src/detect.py`'s real
   `detect_all_field_gated()` -- the exact function `src/pipeline.py` and
   `src/service.py` call in production, not a research-only variant --
   against every variant, and separately against the ORIGINAL (un-evaded)
   value in the same sentence context, as a baseline control. A technique
   "succeeds" for a given record if no hit of the correct type overlaps
   the substituted span. Reporting evasion rate alongside the baseline
   miss rate (not just evasion rate alone) matters: a technique that
   evades 40% of the time against a baseline that already misses 35% of
   the time is a very different finding from the same 40% against a
   baseline that normally catches everything.

## Techniques tested (13 total, across all 6 canonical types)

| Type | Technique | Targets |
|---|---|---|
| SSN | no_dashes, spaced, dotted | `REGEX_PATTERNS["SSN"]`'s literal `-` requirement |
| CREDIT_CARD | spaced_groups, dashed_groups | `REGEX_PATTERNS["CREDIT_CARD"]`'s contiguous-digit-run requirement |
| EMAIL | at_dot_obfuscation, zero_width_split | Regex/NER literal `@`/`.` adjacency |
| IP | defanged (`[.]`), octal_padded | Regex's literal `.` separator; zero-padding as a near-miss control |
| MRN | lowercase_no_dash, spelled_out | `REGEX_PATTERNS["MRN"]`'s exact `MRN-` prefix/case |
| PERSON | leetspeak, unicode_homoglyph, all_caps, flattened_digit_insertion | NER's tokenization/capitalization features; Layer 4's exact-split dictionary segmentation |

Every technique corresponds to a real, documented convention or attack
pattern -- IP defanging is standard SOC/threat-intel writing practice
(not contrived), `[at]`/`[dot]` email obfuscation and Cyrillic homoglyph
substitution are both long-documented real-world techniques, and
username digit-suffixing (`johnsmith2`) is an ordinary collision-avoidance
convention, not an adversarial invention. This matters for how the
results should be read: some of these evasions will happen by accident
in real production logs, not only under adversarial intent.

## Running it (needs spaCy/Presidio -- not available in this session's
sandbox, same standing limitation as every other live-NER task in this
project; see `BUGS_AND_FIXES.md`)

```bash
python3 validation/adversarial_evasion/generate_evasion_variants.py
python3 validation/adversarial_evasion/measure_evasion.py
```

The first step has already been run and committed
(`evasion_corpus.jsonl`, 18,322 records, deterministic via a fixed random
seed so re-running it reproduces the identical corpus). The second step
needs the real spaCy/Presidio model and has NOT yet been run against it
-- `results.json` does not exist yet. This README will be updated with
real numbers once it has, following this project's standing rule against
reporting evasion rates that weren't actually measured.

## What this does and doesn't show

Shows: whether REDACT's CURRENT shipped mechanisms catch each specific,
documented evasion technique, against real log-line context.

Does not show: an exhaustive adversarial search (only 13 hand-designed
techniques, not a fuzzing or optimization-based search for a
worst-case evasion); resistance to an adversary who has read this exact
file and adapts a new technique in response (a purely reactive posture,
not adversarial robustness in the security sense); or anything about
Presidio's own internal model robustness beyond what these specific
inputs probe, since Presidio's own NER model is unchanged, third-party
code.
