# Detection performance tradeoffs: false positives, false negatives, and processing overhead

Manuscript revision support (minor suggestion: "present detection performance
tradeoffs, including false positives, false negatives, and processing
overhead"). This document does not introduce new engineering — every number
below already exists, verified, somewhere in `README.md` or
`BUGS_AND_FIXES.md`. What's new here is pulling them into one place, organized
around the tradeoffs question specifically, with each number's exact source
and scope caveat attached so nothing gets cited out of context. Written for
whoever drafts the manuscript section to pull from directly, not as manuscript
prose itself.

## 1. The core precision/recall/throughput tradeoff, five detection strategies

All rows below: 10,000-entry synthetic corpus, 6,537 gold PII spans, single
1-vCPU/4GB machine, no GPU (`README.md`, "What was actually measured").

| Strategy | Precision | Recall | F1 | Throughput |
|---|---|---|---|---|
| Vanilla Presidio (no REDACT layers) | 0.571 | 0.658 | 0.611 | ~111 events/sec |
| Regex only | 0.574 | 0.542 | 0.558 | ~65,100 events/sec |
| Regex + NER, tiered (skip NER if regex hit anything on the line) | 0.577 | 0.594 | 0.585 | ~261 events/sec |
| Regex + NER, field-gated (excise only regex-covered fields, not whole lines) | 0.592 | 0.707 | 0.644 | ~111 events/sec |
| Regex + NER, naive (NER on every line) | 0.588 | 0.706 | 0.642 | ~112 events/sec |
| Regex + NER (naive) + flattened-username dictionary layer — **the actual shipped default** | 0.633 | 0.854 | 0.727 | ~109 events/sec |

**The tradeoff in one sentence:** regex alone is ~600x faster than anything
involving NER but leaves every unstructured PERSON entity undetected;
NER recovers most of that recall for a ~99.8% throughput cost; the tiered
strategy is the fastest NER-involving option (~261 events/sec) but only
because it silently skips NER for any line where regex already matched
something else, which is precisely the failure mode that makes its recall the
worst of the four NER-involving rows.

**The tiered strategy's field-gated fix has its own smaller, real tradeoff.**
Field-gating (excising only the specific regex-covered field, not the whole
line) closes almost all of tiered's recall gap versus naive (0.707 vs. 0.706)
at statistically the same throughput — but only after two real, measured
false starts: a same-length-masking version that fixed recall without fixing
throughput (still ~100 events/sec, paying masking overhead without the length
reduction that actually drives NER's cost), and a first excision version that
looked like a real 1.8% throughput win until a controlled, apples-to-apples
profiling run showed that "win" was smaller than the run-to-run noise floor
on the test machine (4.6%) — i.e., not a confirmed win at all, genuinely
indistinguishable from naive's speed. Both false starts are kept in
`README.md` rather than silently corrected, since the corrections are
themselves part of the honest performance picture.

## 2. Precision/recall by entity type — the split that actually matters

Naive condition (regex+NER, no flattened layer), per type:

| Type | TP | FP | FN | Precision | Recall |
|---|---|---|---|---|---|
| CREDIT_CARD | 237 | 0 | 0 | 1.000 | 1.000 |
| EMAIL | 488 | 0 | 0 | 1.000 | 1.000 |
| SSN | 250 | 0 | 0 | 1.000 | 1.000 |
| MRN | 242 | 0 | 0 | 1.000 | 1.000 |
| IP | 2327 | 2626 | 0 | 0.470 | 1.000 |
| PERSON | 1074 | 612 | 1919 | 0.637 | 0.359 |

Regex-covered types (CREDIT_CARD/EMAIL/SSN/MRN) are perfect on this corpus —
expected, since a well-formed regex either matches a well-formed synthetic
value or it doesn't; real-world text is messier (see Section 4). The other
two types carry this project's two most important, most traceable findings:

**IP precision (0.470) has one specific, fully root-caused cause, not a
diffuse one.** All 2,626 false positives trace to a single hardcoded internal
address (`10.0.0.5`/`10.0.0.1`) appearing in non-PII log lines — the regex
layer has no concept of "internal infrastructure range" vs. "external,
customer-facing." This is the direct, measured justification for treating
internal and external IPs as different sensitivity tiers in the taxonomy,
not just a plausible-sounding design choice.

**PERSON recall (0.359 naive-only, or 0.681 with the flattened layer — see
below) hides an enormous format-dependent split, the single most important
measured finding in this project:**

| Format | Recall |
|---|---|
| Space-separated ("Timothy Wong") | **98.8%** (975/987) |
| Flattened, no whitespace ("donaldgarcia") | **4.9%** (99/2,006) — NER alone, before Layer 4 |

Any claim that "NER catches what regex misses" needs the format qualifier
attached. General-purpose NER expects sentence structure; a log line's
flattened username token doesn't have any, and NER misses it 19 times out
of 20.

## 3. Closing part of the gap: the flattened-username dictionary layer

Built specifically in response to Section 2's finding — dictionary-based
compound segmentation (does a token split cleanly into `<first><last>`?)
rather than another attempt at sentence-level NER, since the actual problem
is compound-word segmentation, not sentence understanding.

| Metric | Before (NER alone) | After (Layer 4 alone) |
|---|---|---|
| Flattened-format PERSON recall | 4.9% (99/2,006) | **50.3%** (1,010/2,006) |
| False-trigger rate on space-separated names | n/a | 0.3% (3/987) |
| Precision | n/a | **100%**, 0 false positives |

Combined with the full ensemble (`evaluate.py`'s fourth condition): overall
PERSON recall 0.359 → **0.681**, precision unchanged on every other type,
throughput cost within noise of the NER step itself it rides alongside.

**Known limitation, disclosed rather than hidden:** the name dictionary is
Faker's own `first_names`/`last_names` list — the same generator that built
this project's synthetic corpus, so the 50.3%/0.681 numbers are optimistic
relative to a real production username population. Three follow-up
validations quantify exactly how optimistic, not just flag it as a caveat:

- **Real Loghub log text** (OpenSSH, Linux — not this project's own
  templates): recall gains of similar magnitude replicate (OpenSSH
  0.0%→45.5%, Linux 3.4%→50.0%) at unchanged precision — confirms the gain
  isn't a synthetic-template artifact.
- **A disjoint name population** (Faker's German/French/Spanish/Italian
  providers against the same en_US dictionary): recall collapses to **1.4%**
  (28/2,000) — direct confirmation that dictionary coverage, not the
  segmentation algorithm, is the binding constraint.
- **A real US population, sampled by real frequency** (SSA given-name +
  Census surname data, Zipfian-weighted, not Faker-generated): **15.2%**
  recall (305/2,000) — between the two numbers above, as expected, and the
  number that should anchor any production-readiness claim, not the
  synthetic corpus's 50.3%. Two compounding, independently measured causes:
  low raw dictionary coverage (Faker's ~700 first names cover only 1.3% of
  distinct real given names) and role rigidity (a real, measured modern
  naming trend — surname-shaped first names like "Foster" or "Kennedy" — means
  a name can be dictionary-covered and still fail the segmenter's expected
  first/last role).

## 4. Real data tells a materially different, and mixed, story than synthetic

Field-gating's synthetic-corpus result ("closes nearly all of tiered's recall
gap, at parity throughput") does **not** hold as a general claim once tested
against real, structurally diverse Loghub text — a finding that directly
falsified an earlier "field-gated is strictly better than naive" conclusion
that had stood, uncorrected, until this test:

| Dataset | Condition | Precision | FP | Recall |
|---|---|---|---|---|
| OpenSSH (n=2,000) | naive | 0.974 | 49 | — |
| OpenSSH | field-gated (before fix) | **0.778** | **523** | +1 TP only |
| Linux (n=2,000) | naive | 0.920 | 122 | — |
| Linux | field-gated (before fix) | **0.797** | **357** | +0 TP |

**Root cause, found via direct inspection of the candidate text sent to NER,
not guessed:** field-gating's excision logic removed a matched value but left
its `key=` prefix dangling (e.g. `rhost=` with nothing meaningful following
it) — real spaCy consistently misclassified that fragment as PERSON at
~0.85 confidence. Fixed by excising the `key=` prefix along with the value.
**Re-verified after the fix, same day:** OpenSSH precision 0.778→**0.987**
(FP 523→24 — now *below* naive's own 49), Linux 0.797→**0.974** (FP
357→37 — below naive's own 122), recall unchanged or +1. The fix didn't just
close the regression, it left field-gating with a genuine, real-data-verified
precision edge over naive.

**A second real-data false-positive source, different mechanism, found the
same way:** on 2,000 real CloudTrail events, both naive and field-gated
precision collapsed to ~0.31 (FP≈4,600) — far worse than any syslog
condition. Root cause: AWS account IDs are always exactly 12 digits,
colliding with the `CREDIT_CARD` regex (`\d{12,19}`) both directly and via
the same ID embedded in the `arn` field — 81.6% of false positives traced to
this one collision. Fixed with a narrow, context-aware exclusion (not a
blanket regex change, which would have regressed genuine 12-digit synthetic
credit-card recall). Re-verified live: precision 0.310→0.750 (naive),
0.313→0.754 (field-gated); recall unchanged at 0.846 in both. **~690 residual
false positives on CloudTrail are disclosed as a real, still-open gap**, not
implied to be resolved.

**Takeaway for the manuscript:** every one of this project's real-data
validation rounds found at least one false-positive mechanism the synthetic
corpus's own templates never surfaced. This is itself evidence worth stating
directly — synthetic-corpus performance numbers systematically understate
real-world false-positive risk, not just by some vague margin but by
specific, traceable, and in both cases here, fixable mechanisms.

## 5. The entropy layer: a documented case of a technique tested against the wrong target, then the right one

At default settings on the main synthetic corpus, entropy detection performs
badly (34.8% false-alarm rate on clean lines, only 2.3% unique recall over
regex+NER) — but this corpus doesn't contain what entropy detection is
actually built for (API keys, tokens, hashes), so that result measures a
mismatch, not the technique. Tested against a dedicated corpus of genuinely
secret-shaped tokens: **F1 0.811** (precision 0.811, recall 0.812, 9.7%
false-alarm rate). A follow-up fix (excluding UUID-shaped substrings, which
structurally have lower true entropy than real secrets despite looking
random) brought this to **precision 1.000, F1 0.896, 0% false-alarm rate**,
recall unchanged, with one disclosed tradeoff: a small number of real
services issue UUID-shaped API keys, which this exclusion would now also
suppress.

**Manuscript-relevant point:** this is a clean, citable example of the
broader methodological lesson in Section 4 — a detection technique's
measured performance depends entirely on whether the test corpus actually
exercises its intended use case, and REDACT's own numbers on this exact
technique moved from "doesn't work" to "works well" purely by fixing that
mismatch, not by changing the technique.

## 6. Cross-domain validation: PIIBench (out-of-domain check, not a production accuracy claim)

REDACT's detector (scoped to the 5 types it actually claims — PERSON, EMAIL,
SSN, CREDIT_CARD, IP) scored against PIIBench's 5,000-record paper-comparison
subset (general-prose NER benchmark data, not log-shaped text):

| Type | Precision | Recall |
|---|---|---|
| Overall | 0.354 | 0.650 |
| IP | 0.613 | 0.803 |
| EMAIL | 0.858 | 0.904 |
| SSN | 0.584 | 0.754 |
| PERSON | 0.264 | 0.589 |
| CREDIT_CARD | 0.342 | **0.166** |

**CREDIT_CARD's low recall is root-caused, not left as a mystery:** 70% of
misses fail REDACT's own deliberate Luhn-checksum filter (a design choice
that rejects non-Luhn-valid synthetic numbers by intent), the remainder are
card brand-name strings outside REDACT's scope entirely.

**Mandatory framing, not optional:** this cross-check measures the detection
component against general prose the implementation was never tuned toward
— not the log-record shapes REDACT actually targets, and not a valid
head-to-head against PIIBench's own published Presidio/spaCy baselines
(scored across PIIBench's full 82-type taxonomy, not REDACT's 5). It answers
"how does this hold up out-of-domain," not "how accurate is REDACT in
production." Both this out-of-domain number (0.650 overall recall) and the
Loghub-based real-log-shape numbers in Section 4 should be cited together if
either is cited alone, since they answer different questions and a reader
who sees only one could reasonably draw the wrong conclusion about which
domain it describes.

## 7. Processing overhead: what actually costs time, at what scale

**Per-layer cost, single machine, no GPU:** regex ~65,100 events/sec; entropy
adds negligible cost riding alongside regex; NER (any strategy) is the real
bottleneck at ~110-260 events/sec depending on gating strategy; the
flattened-username dictionary layer's own cost is within measurement noise
of the NER step it rides alongside (~117 vs. ~119 events/sec) — Aho-Corasick
over a fixed dictionary is fast enough that NER, not this layer, remains the
throughput ceiling.

**TokenStore's reversibility cost was a real, measured, and fixed
bottleneck, not a theoretical one.** A 1,000,000-line load test found
`TokenStore.save()`'s original read-merge-write-the-whole-store design
cost 2.927s per call once the store reached 93,279 entries (12.4MB) —
throughput collapsed from ~250 lines/sec to ~3/sec well before the run
finished. Fixed with an incremental write-ahead-log design
(`StorageProvider.save_incremental()`); re-measured: the O(n)-per-call
growth is simply gone, growth factor ~1.0x even at `save_every_n_calls=1`
(save after every single token, the most conservative/expensive setting).
**This is the concrete engineering cost of Ask 1's finding that tokenization
is the only one of the three anonymization methods offering real
investigative reversibility** — that reversibility isn't free, and this fix
is what keeps its cost bounded rather than growing without limit as a
deployment's token store accumulates entries.

**Full end-to-end pipeline throughput at increasing scale** (export → Logstash
→ redact-service → OpenSearch, real Docker Compose stack, not a component
microbenchmark):

| Corpus size | Throughput | Wall clock | Result |
|---|---|---|---|
| 100,000 lines | — | — | RECONCILIATION: PASS |
| 1,000,000 lines | 439.4 lines/sec | — | RECONCILIATION: PASS |
| 5,000,000 lines | 218.3 lines/sec | 22,900s (~6.4 hours) | RECONCILIATION: PASS |

Throughput roughly halves from 1M to 5M lines on the same hardware — a real,
disclosed scaling cost, not glossed over. Reconciliation passing at every
scale (anonymized + quarantine counts exactly matching lines exported) is
the correctness claim; the lines/sec figures are the honest single-machine
cost of achieving it, explicitly **not** a production multi-node throughput
benchmark (see `README.md`'s own repeated disclosure on this point).

## 8. One-paragraph synthesis, if only one is needed

REDACT's detection ensemble trades a large, quantified throughput cost
(regex's ~65,000 events/sec collapses to ~110-260 events/sec once NER is
involved) for recall gains concentrated almost entirely in one place: PERSON
entities, and specifically the format split between space-separated names
(98.8% recall via NER alone) and flattened username-style tokens (4.9% via
NER alone, 68.1% once a purpose-built dictionary layer is added — itself
validated down to a realistic 15.2% against a real, frequency-weighted US
name population rather than the optimistic 50.3% synthetic-corpus figure).
False positives are not diffuse noise but trace to specific, root-caused,
and in every case found here, fixable mechanisms — a hardcoded internal IP
range, a dangling `key=` fragment left by an early field-gating
implementation, and a 12-digit AWS-account-ID collision with the
credit-card regex — each found only once real (not synthetic) data was
tested against the production detection path, a pattern strong enough to
state as a general finding: synthetic benchmarks systematically understate
real-world false-positive risk in ways synthetic ground truth cannot by
construction reveal.

## What this document does not cover

- **Adversarial robustness** — whether an adversary can deliberately craft
  input to evade detection is a different question from ordinary false-
  negative rate on unmodified real/synthetic text, and isn't measured
  anywhere cited above. See the separate adversarial-evasion work item.
- **Production multi-node/multi-region scale** — every throughput number
  above is single-machine; REDACT's largest verified run (5M lines,
  Section 7) is real but still several orders of magnitude below a
  genuine high-volume enterprise SOC's daily log ingestion, stated
  plainly rather than extrapolated past what was actually run.
- **Model/version drift over time** — see the separate drift-check
  writeup (Ask 3 of this session); only informal signal exists, no
  dedicated study.
