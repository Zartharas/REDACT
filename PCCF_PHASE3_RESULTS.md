# PCCF phase 3: results (T1–T6)

Hypotheses and designs were fixed first in `PCCF_PHASE3_PREREGISTRATION.md`
(one amendment, to T6's base image). Every verdict below is computed by
the harness itself. Raw output: `validation/real_data/pccf_phase3_*_results.{txt,json}`.

**Short answer:** the redaction tier (a per-pattern conformal recall floor
plus a context scorer) held up in all three settings where it could be
tested:

| setting | precision | recall |
|---|---|---|
| MEDDOCAN, structured fields (phase 2) | +0.33 | −0.04 |
| Spanish free text (T2) | +0.25 | 0.00 |
| security telemetry, leave-one-dataset-out (T3) | +0.20 | −0.01 |

In telemetry it also beats the production field-gated path. The alert tier
failed validity (T5) and should stay out of the paper as a positive claim.
French/Russian (T1/T4) cannot test PCCF at the current sample size.
Flattened-name recall is unchanged by any fusion rule, so format
sensitivity remains the headline finding.

## T1: French/Russian audit
First-ever FR/RU NER numbers for this project.

**FR (11 docs):**

- 11 gold names: 9 in free text, 2 as speaker tags.
- The dictionary layer finds none of them.
- Only the {ner} pattern occurs (7 candidates, 6 true).
- NER alone = union: P 0.857, R 0.545.

**RU (26 docs):**

- 33 gold names: 27 in free text, 6 after a label.
- Patterns: {dict+ner} 10 candidates (9 true), {ner} 7 candidates (5 true).
- union P 0.824, R 0.424
- NER alone P 0.833, R 0.455
- dictionary alone P 0.900, R 0.273

**D1 (mechanical):** in-language calibration is infeasible for every
pattern in both languages. The most true candidates any pattern has is 9;
19 is the minimum at α = 0.05. At the RU hit rate, one pattern needs about
50 documents; a real evaluation needs several hundred.

## T4: French/Russian redaction-tier transfer
**H10 is SUPPORTED, but vacuously.** The MEDDOCAN-calibrated rule-scorer
thresholds keep every FR/RU candidate: ΔP = ΔR = 0 in both languages. On
free text the cues rarely fire (within-pattern AUROC for NER: 0.50 FR, 0.45
RU), so there is nothing to threshold. This neither supports nor refutes
PCCF for FR/RU. Do not cite H10 as evidence of transfer.

## T2: Spanish free text
Setup: synthetic names and dictionary-colliding place names injected into
the real narrative of 471 MEDDOCAN documents. Train and validation use
template set A; test uses the disjoint template set B. Half the names are
outside the dictionary. The test narrative has 220 gold names.

| rule (test narrative) | P | R |
|---|---|---|
| union | 0.299 | 0.982 |
| intersection | 0.882 | **0.441** (misses all 119 out-of-dictionary names) |
| phase-1 beam score, α=0.05 | 0.299 | 0.982 |
| rule scorer, α=0.05 | 0.328 | 0.977 |
| LR fit on clean MEDDOCAN (transfer), α=0.05 | 0.472 | 0.945 |
| **LR refit on injected train, α=0.05** | **0.551** | **0.982** |

- LR-refit vs union, bootstrap: ΔP = [+0.224, +0.284], ΔR = [0.000, 0.000].
- Per-pattern test recall floors: {ner} 1.000, {dict+ner} 1.000.
- **H6 SUPPORTED, H7 SUPPORTED, H6b SUPPORTED:** the transferred LR gains
  less (+0.17) than the refit one (+0.25).

This is the free-text evidence phase 2 lacked. Caveats:

- The sentences come from templates (sets A and B differ, but share a
  style).
- The place negatives are synthetic.
- Two cues appear in both template sets: honorifics, and a place
  preposition before a place. Real clinical free text will be harder.
- The fit-free rule scorer gains only +0.03 here. On free text, the gain
  needs a fitted scorer.

## T3: Security telemetry (the paper's domain)
Setup: Loghub OpenSSH/Linux/Thunderbird, windows_event and cloudtrail, via
`inject_and_evaluate.py`'s own builders. Leave-one-dataset-out × seeds
42/43/44. 3,540 gold names, pooled.

| rule (pooled) | P | R |
|---|---|---|
| union (NER ∪ flattened dictionary) | 0.776 | 0.739 |
| production `detect_all_field_gated` (raw) | 0.786 | 0.739 |
| production field-gated (header-stripped sim.) | 0.803 | 0.739 |
| **KV rule scorer, α=0.05 (fit-free)** | **0.974** | **0.729** |
| KV LR, α=0.02 | 0.969 | 0.733 |
| intersection | 1.000 | 0.013 |

- KV rule vs union, bootstrap: ΔP = [+0.179, +0.215], ΔR = [−0.013, −0.007].
- Per-pattern recall floors: {ner} 0.981, {flat} 1.000, {flat+ner} 1.000.
- **H8 SUPPORTED.**
- Largest single effect: Thunderbird precision rises from 0.12 to 1.00.
  Union there is swamped by NER false positives on message text.
- **H9 (recall by name format):** spaced 0.980 vs flattened 0.504 for
  union, field-gated and PCCF alike. Fusion recovers nothing on flattened
  names, because no layer emits them.

**UPDATE (T7, `PCCF_PHASE3B_MESSAGE_TEXT_RESULTS.md`):** the caveat below was confirmed. The T3 model drops 99% of names placed in message text. With names possible anywhere, the valid gain is +3–4 points of precision, not +20.

**Caveat (read before citing T3):** the injection methodology only places
names in identity fields. The identity-key cue is therefore aligned with
the gold labels by construction. The KV rule drops NER hits that carry no
key, so a real name inside a free-text log message would be dropped, and
this benchmark cannot see that loss. Before any deployment claim, a
names-in-message-text injection test is needed.

## T5: Alert tier, with the construction fixed in advance
- **H11a NOT SUPPORTED:** 10 of 15 eligible units reach ≥ 0.85 precision.
  - Two MEDDOCAN folds miss narrowly: 0.845 and 0.846.
  - All three Thunderbird units fail badly: 0.15–0.21. Precision certified
    on the other log sources does not transfer to Thunderbird's message-text
    false positives.
  - With cloudtrail held out, the certificate keeps nothing at all.
- **H11b NOT SUPPORTED** (conditional on H11a). On MEDDOCAN alone it would
  have been useful: recall 0.815 vs intersection's 0.758 at similar
  precision.

Conclusion: a precision certificate does not survive a shift between log
sources. Report the alert tier as a negative result, or drop it.

## T6: Docker reproduction — DONE (2026-09-23, author's MacBook, Docker Desktop)
**Outcome:** a clean `python:3.10-slim` container rebuilt every cache from
scratch and reproduced phases 1–3 (T1–T5) **exactly**:

- all 6 human-readable result files are byte-identical
- 0 verdict, float or count differences
- the unit tests pass

The container's own comparison printed FAIL, but only because of 28
"missing" metadata keys: the committed phase-1 JSON predated the `search`
field that `pccf.py` later added to each model summary. No number or
verdict was involved. The phase-1 JSON was regenerated (its text output
is unchanged), and re-comparing against the container's results gives
**PASS**. T7 (`pccf_phase3b_message_text.py`) was written after this run
started; `run_pccf_all.sh` now includes it, so the next Docker run will
cover it too.

### Original T6 notes
- **Files:**
  - `validation/real_data/Dockerfile.pccf` (python:3.10-slim, pinned libraries and spaCy 3.8.0 models)
  - `run_pccf_docker.sh` (host)
  - `run_pccf_all.sh` (in the container)
  - `compare_pccf_results.py`
- **What it does:** the repo is mounted read-only. All caches are rebuilt
  from scratch, then the new results are compared to the committed ones.
  PASS means identical verdicts and every metric within ±0.005.
- **Self-check:** the comparator passes on identical inputs. Docker isn't
  available in the Cowork VM, so the container itself is untested here.
- **Found while writing it:** the existing `Dockerfile.ru_ner` uses
  `python:3.11-slim` with pymorphy2 0.9.1. That combination very likely
  fails at import (`inspect.getargspec` was removed in 3.11). Not modified,
  per the additive-only rule; flagged here.

## What goes in the paper (and what does not)
- **Claim:** presence-conditioned conformal recall floors with a context
  scorer raise precision substantially at small or zero recall cost. This
  holds on real structured clinical fields, synthetic free text, and real
  telemetry evaluated leave-one-dataset-out. In telemetry it beats the
  production field-gated path by about 19 points of precision.
- **Frame it under** the format-sensitivity finding: flattened recall stays
  at 0.50 whatever fusion rule is used.
- **Do not claim:**
  - alert-tier validity
  - FR/RU results
  - telemetry performance on names in free-text messages (untested)
- **Next data needed:**
  1. names injected into log *message* text, not only identity fields
  2. FR/RU at several hundred documents (for example the full OpenPII
     language splits)
  3. real, non-template clinical free text with person names
