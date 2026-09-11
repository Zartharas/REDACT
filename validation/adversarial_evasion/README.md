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

## Running it

```bash
python3 validation/adversarial_evasion/generate_evasion_variants.py
python3 validation/adversarial_evasion/measure_evasion.py
```

Both steps have now been run. Corpus generation needs no model (pure
string transforms); measurement needs the real spaCy/Presidio model, not
available in this session's sandbox (same standing limitation as every
other live-NER task in this project — see `BUGS_AND_FIXES.md`), so the
user ran `measure_evasion.py` locally on 2026-09-11 and pasted the
output back. `results.json` (committed) is that real run's output,
verbatim.

## Real results (run 2026-09-11, against the real spaCy/Presidio model)

18,322 evasion-variant records, all 15 (type, technique) pairs:

| Type | Technique | Baseline miss | Variant miss (evasion rate) |
|---|---|---|---|
| SSN | no_dashes | 0/250 (0.0%) | 250/250 (**100.0%**) |
| SSN | spaced | 0/250 (0.0%) | 0/250 (0.0%) |
| SSN | dotted | 0/250 (0.0%) | 0/250 (0.0%) |
| CREDIT_CARD | spaced_groups | 0/237 (0.0%) | 119/237 (50.2%) |
| CREDIT_CARD | dashed_groups | 0/237 (0.0%) | 119/237 (50.2%) |
| EMAIL | at_dot_obfuscation | 0/488 (0.0%) | 488/488 (**100.0%**) |
| EMAIL | zero_width_split | 0/488 (0.0%) | 488/488 (**100.0%**) |
| IP | defanged | 0/2327 (0.0%) | 2327/2327 (**100.0%**) |
| IP | octal_padded | 0/2327 (0.0%) | 0/2327 (0.0%) |
| MRN | lowercase_no_dash | 0/242 (0.0%) | 242/242 (**100.0%**) |
| MRN | spelled_out | 0/242 (0.0%) | 242/242 (**100.0%**) |
| PERSON | all_caps | 955/2993 (31.9%) | 1011/2993 (33.8%) |
| PERSON | leetspeak | 955/2993 (31.9%) | 2801/2993 (93.6%) |
| PERSON | unicode_homoglyph | 955/2993 (31.9%) | 2047/2993 (68.4%) |
| PERSON | flattened_digit_insertion | 942/2005 (47.0%) | 1989/2005 (99.2%) |

### Reading these numbers honestly

**Every regex-layer-only technique that uses a real, ordinary format
variant (not an exotic invented one) achieves complete or near-complete
evasion.** SSN with no separator, defanged IPs (`[.]`), `[at]`/`[dot]`
email obfuscation, a zero-width space split, and lowercase/no-dash MRNs
all reach 100% -- `REGEX_PATTERNS` in `src/detect.py` matches literal
characters and formats exactly, with no semantic understanding that these
variants mean the same thing. This is not a contrived weakness: IP
defanging specifically is standard, legitimate SOC/threat-intel writing
practice (see the technique table above), meaning some of this 100% is a
real, non-adversarial false-negative risk in ordinary security telemetry
today, not only a risk under active evasion intent.

**A second, independent layer sometimes closes the gap REDACT's own
regex leaves open, and that matters more than the headline evasion
number for two of the six types.** SSN `spaced` and `dotted`, and IP
`octal_padded`, all measured **0% evasion** -- REDACT's own
`REGEX_PATTERNS["SSN"]` requires literal dashes and would miss the
spaced/dotted variants exactly like the no-dashes case, yet the full
ensemble caught them anyway. The most likely explanation (consistent with
how `detect_all_field_gated()` is composed -- see `src/detect.py`) is
Presidio's own built-in `US_SSN` recognizer, run as part of the same NER
pass, independently tolerating those formats -- this is REDACT's layered
design working as intended, a second mechanism catching what the first
missed, not a contradiction. This wasn't independently confirmed by
inspecting Presidio's own recognizer source in this write-up (out of
scope for this session), so it's reported as the best-supported
explanation given the code's structure, not a verified internal fact.

**CREDIT_CARD group-splitting's 50.2% blended rate hid a real, confirmed
split by card length**, run live via `diagnose_credit_card_partial_evasion.py`:

| Digit length | Evaded | Total | Evasion rate |
|---|---|---|---|
| 12 | 26 | 26 | 100.0% |
| 13 | 21 | 21 | 100.0% |
| 14 | 23 | 23 | 100.0% |
| 15 | 16 | 35 | 45.7% |
| 16 | 12 | 111 | **10.8%** |
| 19 | 21 | 21 | 100.0% |

This confirms the working hypothesis rather than leaving it asserted:
Presidio's own built-in CREDIT_CARD recognizer clearly has specific
tolerance for the standard 16-digit, four-groups-of-four format (the
dominant Visa/Mastercard shape -- 111 of the corpus's 237 CREDIT_CARD
values, and only 10.8% evasion there) but essentially none for every
other length REDACT's synthetic corpus produces. 15-digit (Amex-shaped)
numbers land in between (45.7%) rather than either extreme -- consistent
with some grouping variants coincidentally still passing whatever pattern
Presidio applies, but not investigated further here (out of scope: this
would require reading Presidio's own recognizer source, a third-party
dependency, not REDACT's code). The practical takeaway for the manuscript
is specific, not vague: REDACT's real resistance to this evasion
technique is concentrated almost entirely in the most common card format
and is close to absent for every other length it claims to detect.

**PERSON findings are the most consequential for the manuscript, because
this layer already has the weakest baseline (31.9% miss rate on ordinary,
un-evaded full names) and every technique tested makes it materially
worse except one.** `all_caps` barely moves the needle (+1.9 points) --
capitalization pattern alone is a much weaker signal for this model than
the character-level techniques, a specific, useful finding, not an
assumption. `unicode_homoglyph` (Cyrillic lookalikes) more than doubles
the miss rate to 68.4%; `leetspeak` (digit substitution) very nearly
triples it to 93.6%. `flattened_digit_insertion`, targeting Layer 4's
dictionary segmentation specifically (not NER), is the single most
effective technique measured at 99.2% -- a single inserted digit between
two real name halves defeats the exact-split assumption `flattened_names.py`
already discloses as a design limitation; this experiment shows how
cheap it is to trigger that limitation deliberately, on top of the
already-documented coverage gap against non-Faker name populations.

## Devil's-advocate read: what a regulator or an attacker would take from this

Three of the six types (EMAIL, IP-via-defanging, MRN) evade **completely**
using formats that occur in ordinary, non-adversarial security writing --
not just under active evasion intent. That distinction matters for how
this should be framed: a SOC analyst who pastes a defanged IOC into a
ticket, or a support ticket containing a `user [at] domain [dot] com`
address typed by a human trying to avoid a spam filter, would currently
pass through REDACT's pipeline with those values untouched, with no
adversarial intent involved at all. Under GDPR Article 32 ("Security of
Processing"), a documented, systematic gap of this kind -- known,
reproducible, and not requiring sophistication to trigger -- is a
materially different finding from an occasional edge-case miss, and is
the kind of gap an auditor comparing this framework's stated claims
against its measured behavior would be expected to flag. The honest
mitigation path is not proposed or built here (out of this session's
explicit scope -- "don't build speculative features unrelated to the
asks"), but the manuscript should not describe REDACT's regex layer as
covering these formats without this finding attached.

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
