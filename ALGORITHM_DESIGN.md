# Algorithm Design: Weighted Mondrian-PASC Fusion for Multi-Layer PII Detection

Research-only design document. **No code, no data staging, no evaluation
harness changes.** This is the extensive-search/thorough-examination pass
requested before committing to a method, following the same
research-before-code discipline as `LANGUAGE_EXTENSION.md` and
`PUBLICATION_STRATEGY.md`. Every citation below was fetched and read this
pass, not recalled from training — where a fetch failed, that is stated
rather than papered over.

## 1. The problem this is solving, restated precisely

Every evaluation harness built in this project (`evaluate_fr.py`,
`evaluate_ru.py`, `evaluate_id.py`, and the original MEDDOCAN one) combines
detection layers by naive set union: `combined = regex_hits + dictionary_hits
+ ner_hits`. The MEDDOCAN result already on record shows this is actively
harmful — PERSON precision *dropped* from 47.4% (dictionary alone) to 39.2%
(dictionary+NER unioned) because the two layers' false positives were 97%
disjoint while true positives overlapped. Union stacks errors instead of
canceling them. Nothing built since (French, Russian, Indonesian) changed
how layers combine — every language repeats the same union and inherits the
same flaw, undiagnosed because none of those evaluations specifically
measured combined-vs-standalone precision the way the MEDDOCAN pass did.

Two independent research efforts have now characterized *why* this
particular kind of combination is fixable rather than fundamental:

- **This project's own MEDDOCAN diagnosis**: dictionary false positives are
  94.3% place-name collisions (Santiago/Valencia are both names and
  places); NER false positives are dominated by disease-eponym confusion
  (Alport, McArdle) and clinical-header triggers (Antecedentes, Varón).
  Two distinguishable, largely non-overlapping failure signatures — not
  noise.
- **`Zafar & Nowaczyk, "Mind the Gap: Robustness Risks in PII Detection
  Systems"` (arXiv 2609.03464, Sept 2026, Halmstad University — read in
  full this pass, not just the abstract)**: an independent, contemporaneous
  academic study evaluating SpaCy (encoder), Presidio (rule-based hybrid),
  and Qwen2.5-3B (LLM) on a purpose-built 7-category out-of-distribution
  stress benchmark (run-on text, conversational tone, informal register,
  code-switching, abbreviations, mixed-context PII, typos). Their finding,
  independently derived: **the three architectures fail in distinct,
  complementary ways on the same inputs.** Encoder models fail on unseen
  surface forms and entity-boundary absorption ("Kimberly Snyder DVM dob"
  tagged as one PERSON span). Rule-based systems fail on non-standard
  formats (spelled-out emails, phone extensions) and specific
  over-triggering (their date regex tags bare numbers like "6574" as
  DATE_OF_BIRTH, dropping precision to 0.492 while recall stays perfect —
  a *false-positive-dominated* failure mode, the mirror image of the
  encoder's false-negative-dominated one). The LLM fails via entity-type
  confusion (credit card numbers labeled SSN, recall 1.000→0.543) and
  generation instability (2.5% totally unparseable output).

Two unrelated studies, different languages/datasets/architectures,
converging on the same structural claim — complementary, characterizable,
non-random failure modes per architecture — is a real empirical signal, not
a coincidence to be waved at. **Section 5.2 of Zafar & Nowaczyk explicitly
proposes a hybrid pipeline with an "entity merger layer" to exploit this,
arguing the fused system's miss rate should be "bounded by the intersection
of their failure sets rather than the union."** They do not specify how the
merger layer resolves conflicts, give it any formal guarantee, or implement
it — it's a discussion-section proposal, not a built or evaluated
algorithm. That is the precise, stated gap this design fills.

Their own Limitations section (read in full) also states plainly: *"Our
benchmark is synthetic and operates within a single general domain...
Evaluation is limited to English... Future work should... expand to
multilingual settings."* This project already has the French, Russian, and
Indonesian detection layers their own stated future work would need.

## 2. Why naive union is the wrong tool, and what the right one is — the mathematical lineage

Naive union of detector outputs has no formal error-rate guarantee at all —
it's just "trust anything any layer says," which is exactly what produces
the disjoint-false-positive-stacking problem. The right class of tool is
**conformal prediction**: distribution-free, finite-sample coverage
guarantees, model-agnostic (works over regex, dictionary lookups, and
neural NER identically, without needing any of them to be a well-calibrated
probability model). Four pieces of that literature, each checked this pass
for what it actually contributes and what it doesn't:

**Tibshirani, Barber, Candès, Ramdas, "Conformal Prediction Under Covariate
Shift" (NeurIPS 2019).** The foundational fix for the fact that standard
conformal prediction's guarantee requires *exchangeability* between
calibration and test data — an assumption that breaks the moment
calibration data (clean, single-language, well-formatted) differs from
deployment data (messy, code-switched, multilingual telemetry), which is
exactly this project's actual operating condition. Their fix, **weighted
conformal prediction (WCP)**: reweight each calibration nonconformity score
by the likelihood ratio between the test and calibration covariate
distributions, generalizing exchangeability to "weighted exchangeability."
The guarantee holds as long as that likelihood ratio is known or can be
estimated. This is the correct, classical tool for the cross-lingual/OOD
shift problem — not a 2026 novelty, a 2019 one, cited here because it's
still the right mechanism and shouldn't be skipped in favor of something
newer-sounding.

**Mondrian (class-conditional) conformal prediction.** Calibrates
nonconformity quantiles *separately per class/partition* rather than one
global quantile, giving per-class coverage guarantees instead of one
blended average. A July 2026 empirical study (found this pass, not yet
read in full — flagged, not glossed over) reports standard CP collapsing to
52.94% coverage on a minority class under 1:345 imbalance, while
Mondrian/class-conditional calibration holds 90.59% coverage by computing
quantiles per class. This is the right mechanism for this project's
specific situation: the failure modes aren't one blob, they're
*named, distinguishable categories* (place-name collision, clinical-header
confusion, digit-sequence type-confusion, boundary-absorption) — exactly
what Mondrian partitioning is for.

**Kotte, "PASC: Pipeline-Aware Conformal Prediction with Joint Coverage
Guarantees for Multi-Stage NLP and LLM Pipelines" (arXiv 2605.18812, May
2026 — read in full this pass, not just the abstract).** Addresses multi-
stage pipelines (their running example: NER → entity disambiguation →
entity typing) where calibrating each stage independently loses joint
coverage, and Bonferroni correction (the safe fallback) is needlessly
conservative. PASC reduces the whole multi-stage problem to **a single
scalar conformal calibration on the joint maximum nonconformity score**
across stages, with a proven finite-sample guarantee that all K stages are
simultaneously covered with probability ≥ 1-α, "nearly tight up to a
1/(n+1) factor." Reported results: 96.4% empirical joint coverage vs. 93.4%
(Bonferroni) and 86.5% (independent-per-stage calibration) on a three-stage
CoNLL-2003 pipeline, at identical prediction-set size; under distribution
shift to WNUT-17/WikiNEuRal, PASC held >1-α coverage while independent CP
collapsed to 59%; scales to K=6 stages where independent CP drops to 53%
end-to-end coverage.

**A load-bearing caveat, stated plainly rather than glossed over: PASC's
setting is *sequential* stages (NER's output feeds NED, which feeds
typing).** This project's architecture is *parallel* — regex, dictionary,
and NER all run independently on the same input, not in a pipeline. PASC's
authors did not test a parallel-ensemble setting. The joint-maximum-
nonconformity-score reduction is mathematically agnostic to whether the K
scores come from sequential stages or parallel detectors — max is max
either way — but applying it to parallel detection layers is an
**adaptation this design proposes, not something PASC's own paper
validates.** That adaptation is the actual novel step here, not a
citation to be taken on faith.

## 3. The proposed algorithm: Weighted Mondrian-PASC Fusion

Three steps, each mapped to a piece of the lineage above:

**Step 1 — Mondrian partitioning by failure-mode class.** For each
detection layer (checksum-validated regex, dictionary/gazetteer, NER),
compute a nonconformity score per candidate span, but calibrate quantiles
*separately* within each named failure-mode partition rather than one
global threshold: {dictionary-hit-with-place-context-cue,
dictionary-hit-without-place-context-cue, NER-hit-matching-clinical-header-
pattern, NER-hit-not-matching, regex-hit-on-bare-numeric-context,
regex-hit-on-labeled-context, ...}. The partitions are the failure classes
this project and Zafar & Nowaczyk have already independently characterized
— this step formalizes existing diagnostic knowledge into calibration
structure, it doesn't invent the taxonomy from nothing.

**Step 2 — Weighted reweighting for cross-lingual/OOD shift.** Because
exchangeability breaks between a clean single-language calibration corpus
and messy multilingual/code-switched deployment text, reweight each
calibration point's nonconformity score by an estimated likelihood ratio
between calibration and deployment covariate distributions (Tibshirani et
al.'s WCP), using the OOD-category label itself (one of Zafar & Nowaczyk's
seven shift categories, extended with a language/script dimension) as the
covariate being corrected for, rather than trying to estimate a raw text-
level density ratio directly.

**Step 3 — Joint fusion across layers via PASC's reduction.** Combine the
Mondrian-and-weight-calibrated per-layer nonconformity scores using PASC's
joint-maximum-score reduction — one scalar conformal calibration step
across all layers simultaneously — replacing this project's existing
`combined = regex + dictionary + ner` union entirely. Output: a single,
non-Bonferroni-conservative, finite-sample coverage guarantee on the fused
detector, stratified by failure-mode class and robust to the specific kind
of distribution shift (cross-lingual, code-switched, informal-register)
this project's own use case (security telemetry) actually contains.

## 4. Devil's-advocate feasibility check — theoretical and practical

**Theoretical risk 1: the likelihood-ratio estimate in Step 2 is itself
estimated, not known.** WCP's guarantee is only as good as the ratio
estimate; a bad estimate degrades the guarantee's tightness (though not,
per Tibshirani et al., its basic validity in most formulations). This is
the main mathematical soft spot in the whole design, not a solved problem
— flagged here rather than assumed away.

**Theoretical risk 2: Mondrian partitioning fragments an already-small
calibration set.** Each failure-mode partition needs enough calibration
examples for its per-class quantile to be reliable. This project's current
pilot samples (11 French documents, 26 Russian documents) are nowhere near
enough once split across 5-10 failure-mode partitions per language — this
is the same small-N problem `PUBLICATION_STRATEGY.md` already flagged for
a different reason (statistical power for the tuning-artifact question),
and it recurs here for a second, independent reason. It reinforces rather
than duplicates that document's conclusion: this design is not executable
against the current 11/26-document pilots. It needs either ServiceNow's
REDACT corpus (453-621 records/language) or Zafar & Nowaczyk's own
benchmark (560 examples / 2,330 entities, 80 per OOD category, their code
released at `github.com/Adeelzafar/Mind-the-Gap-PII`) as calibration data.

**Theoretical risk 3: PASC's sequential-pipeline mechanism applied to
parallel detectors is an untested adaptation**, stated in Section 2 above
— the mathematics likely transfers (max-of-K-nonconformity-scores doesn't
care about the K scores' provenance) but this has not been shown correct
or validated by anyone, including PASC's own authors, for a parallel
ensemble specifically. This is the paper's actual novel theoretical claim
and needs its own proof sketch or empirical validation before being stated
as established — not before being *tried*.

**Practical feasibility: no LLM required.** Every component (nonconformity
scoring, Mondrian quantile calibration, WCP reweighting, PASC's joint
reduction) is closed-form or a simple quantile computation — no model
inference beyond what the existing regex/dictionary/NER layers already do.
This preserves the project's existing no-LLM/air-gapped design value
(raised in `PUBLICATION_STRATEGY.md`'s framing #1) rather than trading it
away for a fusion mechanism that needs its own model. PASC itself reports
running 1.7× faster than Bonferroni and needing only a single quantile
computation — cheap by construction.

**Practical feasibility: two ready-made evaluation paths, not one built
from scratch.** (a) Extend Zafar & Nowaczyk's own released 7-category OOD
benchmark design to French and Russian using this project's already-built
`fr_detect.py`/`fr_ner.py`/`ru_detect.py`/`ru_ner.py` — this directly fills
their own explicitly stated "expand to multilingual settings" gap, giving
a citable, differentiated extension rather than a from-scratch benchmark.
(b) Evaluate the fusion algorithm's formal coverage guarantee against
ServiceNow's REDACT corpus (already scoped in `PUBLICATION_STRATEGY.md`)
for the languages it shares with this project (French, Russian).

## 4.5 A closer precedent found after the above was written — this revises Section 3

While closing the "has anyone assembled this exact combination" gap flagged
in Section 5, a direct structural precedent turned up: **Moayedikia,
"Conformal Fusion Under Missing Modalities" (arXiv 2608.07183, Aug 2026,
Swinburne University of Technology)** — read in full through its Related
Work section (Sections 1-2; Sections 3-6 — architecture detail,
experiments, limitations, future work — not yet read, disclosed rather
than skipped over). This is close enough to change the recommended
construction, not just add a citation.

Their problem: multi-modal fusion architectures assume all modalities are
present at inference; in practice modalities go missing (sensor failure,
cost constraints), and existing missing-modality methods treat this purely
as an accuracy problem, never asking whether the model's *confidence*
degrades appropriately when it has strictly less information. Their fix,
**Modality-Conditioned Conformal Fusion (MCCF)**: per-modality evidential
heads producing Dirichlet distributions, combined via Dempster-Shafer
fusion (an absent modality contributes literally vacuous evidence,
structurally ignored — no imputation needed), calibrated by **a Mondrian
conformal module keyed on the binary modality-presence mask**, giving
`Pr(Y ∈ C(X) | S=s) ≥ 1-α` simultaneously for every non-empty modality
subset `s`. To their own stated knowledge, this is the first method to
give formal coverage guarantees under arbitrary modality-availability
patterns through architectural integration rather than post-hoc
recalibration — and their own literature review (which is unusually
thorough and worth trusting on this point) confirms no prior missing-
modality method measures or guarantees calibration under absence at all.

**The structural mapping to this project's problem is close to exact.**
"Which modality is present" ↔ "which detection layer fired on this
candidate span" (regex/checksum, dictionary, NER — each either produces a
hit here or doesn't, exactly like a sensor stream being present or absent).
Their Mondrian-on-presence-mask calibration is a cleaner mechanism than
what Section 3 originally proposed: instead of hand-curating named
failure-mode partitions (place-name collision, clinical-header confusion,
...) — which requires ongoing manual characterization and won't
automatically generalize to a new language — partition by the
*layer-firing pattern itself* (regex-only, dictionary-only, NER-only,
regex+dictionary, ..., all-three), a small (2³=8 for three layers),
automatically computable partition that needs no hand-curated taxonomy at
all. The failure-signature data from MEDDOCAN and Zafar & Nowaczyk becomes
evidence for *why* certain presence-patterns are more or less trustworthy,
not the partitioning key itself.

**One important, honest revision this forces: MCCF's full architecture
doesn't map cleanly onto this project's actual detectors, and adopting it
wholesale would be over-engineering.** MCCF's evidential heads and
Dempster-Shafer combination require *trainable, differentiable* per-source
outputs, integrated end-to-end via a ConfTr-style (Stutz et al. 2022)
differentiable set-size penalty during training. Regex and checksum layers
in this project are not neural, not differentiable, and not being trained
at all — there's no gradient to shape. Forcing them into an evidential-head
formulation to match MCCF exactly would be solving a harder problem than
this project has.

The paper's own related-work section identifies the better-fitting building
block: **CP-MDA-Nested (Zaffran et al., 2023)**, described there as "the
closest existing framework to the present work" and "the most direct
precursor to MCCF." CP-MDA-Nested is explicitly **model-agnostic and
requires no retraining** — it artificially masks calibration points so
their missingness patterns form supersets of the test point's available
modalities, and aggregates the resulting augmented sets to construct
prediction sets satisfying the same mask-conditional validity guarantee
`Pr(Y ∈ C(X) | S=s) ≥ 1-α`, **without requiring exact mask matching or
likelihood-ratio estimation** — sidestepping Tibshirani et al.'s WCP
likelihood-ratio-estimation burden entirely (which this paper's own
literature review independently flags as a real combinatorial problem when
the number of possible presence-subsets is large — for three detection
layers it's only 8, but the point that likelihood-ratio estimation is the
weaker link stands, and CP-MDA-Nested avoids needing it at all). MCCF
extends CP-MDA-Nested's frozen-black-box setting into a jointly-trained
evidential architecture; this project's setting (frozen, non-differentiable
regex/dictionary/NER layers) is exactly the frozen-black-box case
CP-MDA-Nested was built for, not the jointly-trained case MCCF was built
for.

**Revised recommendation: build on CP-MDA-Nested's masking mechanism +
Mondrian calibration keyed on layer-presence pattern, not MCCF's full
evidential architecture.** MCCF is the validating precedent that this
general shape of idea (a) works, (b) is recent (Aug 2026) and still
essentially unclaimed outside multi-modal sensor fusion, and (c) is
considered novel enough by its own authors to publish as "first of its
kind" in their domain — a reasonable bar for the PII-detection adaptation
to clear too, checked as not yet done for PII specifically (Section 5).
MCCF's Dempster-Shafer evidence combination is also worth flagging as a
risk if adopted directly: the paper's own related-work section notes
Dempster-Shafer fusion is "not calibrated in the frequentist sense" on its
own and can produce overconfident results when sources conflict — the
well-known **Zadeh's paradox** — which is exactly why MCCF wraps it in a
separate, formal Mondrian conformal layer rather than trusting the fusion
step's confidence directly. Any PII adaptation that uses evidence
combination (Dempster-Shafer or otherwise) across detector layers should
carry the same conformal wrapper for the same reason, not trust the fusion
step's own confidence output.

**Revised Section 3, in light of this:**

1. **Layer-presence partitioning (replaces the failure-mode-taxonomy
   partitioning originally proposed).** For each candidate span, compute
   the binary layer-presence mask (did regex/checksum fire? dictionary?
   NER?) — 8 possible non-empty-or-empty patterns for three layers.
2. **CP-MDA-Nested-style masked calibration, not Tibshirani-style
   likelihood-ratio reweighting.** Augment the calibration set by masking
   layers on calibration examples to simulate every presence pattern a
   test span could show, avoiding the need to estimate a likelihood ratio
   between calibration and deployment distributions at all — directly
   answering Theoretical risk 1 from the original Section 4, not just
   flagging it.
3. **Mondrian calibration keyed on the presence mask**, giving per-pattern
   coverage guarantees `Pr(span is real PII | presence-pattern=s) ≥ 1-α`
   for every layer-combination pattern simultaneously — replacing this
   project's existing naive union entirely, the same way MCCF replaces
   naive modality concatenation.
4. **PASC's joint-maximum-score reduction (retained from the original
   design) as the mechanism for combining the per-pattern-calibrated
   nonconformity scores into one final decision** — this piece of the
   original design still applies cleanly on top of the revised Steps 1-3,
   since PASC's reduction doesn't care what produced the per-stage scores,
   only that they exist and need joint calibration.

This is a smaller, more buildable claim than the original Section 3, not a
larger one — and that's a feature of the revision, not a weakness: fewer
untested cross-domain adaptations stacked on each other, a closer-fitting
precedent to build from, and an honest acknowledgment that this project's
non-differentiable detector layers rule out the fancier trainable-evidential
version of the idea.

## 5. What was not verified this pass — stated honestly

- `Domain-Shift-Aware Conformal Prediction for Large Language Models`
  (arXiv 2510.05566) returned an empty/non-machine-readable PDF on fetch,
  same failure mode as two earlier fetches this session (ServiceNow's
  REDACT PDF, RECAP's PDF). Not read. Tibshirani et al.'s WCP is used
  instead as the foundational, confirmed-readable source for the
  covariate-shift fix — though Section 4.5's revision makes this less
  load-bearing than originally: CP-MDA-Nested sidesteps the
  likelihood-ratio-estimation problem WCP requires, rather than solving it,
  so the unread 2510.05566 paper's specific mechanism is no longer
  something the design depends on either way.
- The July 2026 Mondrian/class-imbalance empirical study was found and its
  abstract-level claims (52.94% vs. 90.59% coverage under 1:345 imbalance)
  are reported above from search-result summary text, not from a full read
  of the paper itself. Should be read in full before the specific numbers
  are cited in any manuscript.
- **The triple-intersection search (Mondrian CP + pipeline/fusion
  calibration + PII detection) was run this pass** and did surface the
  closest precedent found in this whole research pass — `Conformal Fusion
  Under Missing Modalities` (Section 4.5) — confirming the general shape of
  idea is current and unclaimed in multi-modal sensor fusion, but the
  search results themselves stated plainly that nothing combines it with
  PII detection specifically. That stands as the strongest available
  signal that the PII-specific adaptation is open, short of exhaustively
  reading every paper that cites Mondrian CP.
- **`Conformal Fusion Under Missing Modalities` itself was only read
  through Section 2 (Related Work)** — Section 3 (the actual MCCF
  architecture and equations), Section 4 (experiments), and Section 5
  (limitations/future work — which could easily state something like "does
  not extend to non-differentiable detectors" or "future work: apply to
  NLP/text fusion," either confirming or complicating the revised
  recommendation above) were not read. That is the single most important
  remaining gap before treating Section 4.5's revision as settled, more
  important than the smaller unread items above it.
- **Zaffran et al. (2023), CP-MDA-Nested — now confirmed as a real, correct
  citation at the abstract level** (Zaffran, Dieuleveut, Josse, Romano,
  ICML 2023, `arXiv:2306.02732`, "Conformal Prediction with Missing
  Values," code released at `github.com/mzaffran/
  ConformalPredictionMissingValues`). The abstract confirms the mechanism
  as described secondhand by the MCCF paper: standard conformal prediction
  under-covers conditionally on missing-value patterns despite valid
  marginal coverage, and their "missing data augmentation" framework fixes
  this "despite their exponential number" of possible patterns — matching
  the "8 possible layer-presence patterns for 3 detectors" framing above.
  The full method section (their generalized conformalized quantile
  regression construction) was still not read — only the abstract — so the
  exact augmentation mechanism should be verified against the actual paper
  before being reproduced in code.

- **A newer, more current result than either MCCF or CP-MDA-Nested, found
  in this final check: Fan, Park, Vo, Brunel, "Weighted Conformal
  Prediction Provides Adaptive and Valid Mask-Conditional Coverage for
  General Missing Data Mechanisms" (`arXiv:2512.14221`, submitted 16 Dec
  2025 — CC-BY-4.0, confirmed via the license badge on the abstract page).**
  This directly answers the "current technologies" half of the user's
  request: it's a follow-on to the Zaffran line specifically, proposing a
  "preimpute-mask-then-correct" framework — multiple imputation of the
  calibration set followed by a reweighted conformal correction — that
  provides both marginal coverage and mask-conditional validity, and
  reports **significantly narrower prediction sets than standard
  mask-conditional-validity methods** (i.e., tighter, more useful
  guarantees than the CP-MDA-Nested-style masking approach MCCF built on)
  while keeping the same formal guarantees. If this holds up on a full
  read, it is a better starting point than CP-MDA-Nested for the
  layer-presence-conditioned calibration step in Section 4.5 — newer,
  reportedly tighter, and CC-BY-4.0 licensed (cleaner than MCCF's own
  arXiv-perpetual-license terms for any derived publication). Only the
  abstract was read; the two derived algorithms it proposes were not
  examined. This is now the single highest-priority paper to read in full
  before finalizing Section 4.5, ahead of Zaffran (2023) itself, precisely
  because it appears to supersede it.

## 6. Final proposed algorithm: PCCF (Presence-Conditioned Conformal Fusion)

Everything above was research. This section is the concrete design that
falls out of it — specific enough to prototype, still not built.

### 6.1 Setup

For a language with K detection layers (this project: K=3 — checksum-aware
regex, Faker/gazetteer dictionary, NER), every candidate span `x` a layer
flags gets a **presence mask** `S(x) ∈ {0,1}^K`, `S_k(x)=1` iff layer k
fired on `x`. There are `2^K - 1 = 7` non-empty patterns for K=3 (plus the
empty pattern, which never reaches this algorithm since nothing flagged
`x`).

Each layer, when it fires, already produces a usable raw signal without
any new infrastructure: regex/checksum layers emit a binary match plus
checksum-valid flag (Luhn, IBAN mod-97, Verhoeff — already implemented for
several ID types in this project); the dictionary layer emits a
match-confidence (exact vs. lemma-matched vs. fuzzy, per the Russian
lemmatization work); NER emits a model confidence score natively. Convert
each to a per-layer nonconformity score `r_k(x) = 1 - confidence_k(x)`
when `S_k(x)=1`.

### 6.2 Calibration (offline, once per language)

1. On a held-out, labeled calibration corpus (ServiceNow's REDACT French/
   Russian subsets, or Zafar & Nowaczyk's OOD benchmark extended
   multilingually — not this project's own 11/26-document pilots, per the
   data-sparsity point in Section 4.4) compute `S(x)` and the joint
   nonconformity score `R(x) = max_{k : S_k(x)=1} r_k(x)` for every
   calibration span — the PASC-style joint-maximum reduction (Section 2),
   applied across parallel layers rather than sequential pipeline stages.
2. For each presence pattern `s`, using the nested/superset augmentation
   principle from the Zaffran (2023) / Fan-Park-Vo-Brunel (2025) line
   (Section 4.5, Section 5) — calibration points whose own pattern is a
   *superset* of `s` count toward `s`'s calibration set too, so rare
   patterns aren't starved of data — compute the empirical `(1-α)`-quantile
   `q_s` of `R(x)` over that augmented set. This is a sort plus an index
   lookup: `O(n log n)` once, `n` = calibration set size for that pattern
   group.
3. Store the 7 quantiles `{q_s}` per language. That's the entire
   calibration artifact — 7 floating-point numbers.

### 6.3 Inference (replaces the existing `combined = regex + dict + ner`
union in every `evaluate_*.py` harness)

For a new candidate span `x`: compute `S(x)` (already known — it's which
layers fired), compute `R(x)` (already computed from each layer's existing
output), look up `q_{S(x)}` (array index, `O(1)`), and flag `x` as PII iff
`R(x) ≤ q_{S(x)}`. That's the whole decision rule. No new model runs, no
retraining, no LLM call.

**Guarantee, stated at the precision this research actually supports, not
overstated:** if the calibration data and the failure-signature
partitioning are representative of deployment conditions, this construction
targets `Pr(x correctly classified | S(x)=s) ≥ 1-α` simultaneously for
every presence pattern `s` — the Mondrian/mask-conditional property from
Section 4.5, achieved without Bonferroni's conservatism via PASC's
joint-max reduction. This is a target inherited from the cited
constructions, not something this project has proven correct for the
parallel-detector setting itself (Section 4.5's honest caveat: the
sequential-to-parallel adaptation of PASC's reduction is asserted, not
proven, in this document).

### 6.4 Why this is efficient, concretely

- **Calibration cost:** one sort per presence pattern, done offline, once
  per language, on existing labeled data. Not repeated per request.
- **Inference cost:** one array lookup plus one comparison, added on top of
  work the existing regex/dictionary/NER layers already do. No additional
  model inference of any kind — the efficiency profile is identical to the
  current naive-union pipeline plus a rounding error.
- **No LLM in the loop anywhere**, preserving the air-gapped/zero-
  inference-cost value proposition raised in `PUBLICATION_STRATEGY.md`
  (framing #1) — this is a genuine point of difference from RECAP, GPT-4.1,
  and Claude Sonnet 4.6 as detectors, all of which require a paid API call
  or a locally-hosted LLM per span.
- **Storage cost:** 7 numbers per language. Trivial to version, audit, or
  regenerate.
- **PASC's own reported numbers** (Section 2) — 1.7× faster than
  Bonferroni, a single quantile computation, scaling cleanly to K=6 where
  independent calibration collapses — are the closest available evidence
  for what to expect here, carried over from the sequential-pipeline
  setting rather than measured in this project's parallel-detector setting.

### 6.5 Why this is novel, stated at the precision the research supports

Not "conformal prediction is novel" — it isn't, Vovk's framework is
decades old, and this document says so plainly in Section 2. The specific,
checked claim: **no paper found across this entire research pass (Sections
1-5, including the dedicated triple-intersection search) applies
presence/mask-conditional conformal calibration to multi-layer PII/PHI
detection.** The closest precedent (MCCF, Aug 2026) is in multi-modal
sensor fusion and states its own "first of its kind" claim for that domain;
the calibration mechanism it's built on (Zaffran 2023, refined Dec 2025) is
in general missing-data statistics, not text or entity detection at all.
Assembling these three specifically for the regex/dictionary/NER PII
detection setting — informed by two independently-derived, converging
failure-mode characterizations (this project's MEDDOCAN diagnosis and
Zafar & Nowaczyk's architecture-specific OOD failure taxonomy) as the
empirical basis for why presence-pattern partitioning should work here —
is the actual novel step, and it's a combination step, not an invention of
any of its parts. That's a more modest and more defensible claim than
"we invented a new calibration method," and it's the honest one.

## 7. What this document is not

Not a build plan. No detection code, calibration scripts, or evaluation
harness changes exist as a result of this document. Per this project's
established discipline, the next step if this direction is approved would
be a feasibility prototype — computing nonconformity scores and Mondrian
partitions on the failure-mode data already characterized in the MEDDOCAN
pass (existing data, no new staging needed) as a first, cheap check of
whether Step 1 alone measurably improves precision before building Steps
2 and 3 on top of it.
