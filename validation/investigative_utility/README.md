# Privacy vs. investigative utility: a small, honestly-scoped experiment

This measures a specific manuscript-revision question directly: how much
does full redaction, pseudonymization, and reversible tokenization each
cost (or not cost) a realistic investigation into REDACT's own anonymized
output? It reuses `anonymize.py`'s real, already-shipped `redact()`,
`pseudonymize()`, and `tokenize()`/`TokenStore` -- nothing new was built
into the anonymization layer itself for this. What's new is a small
supplementary corpus and a measurement script.

## Why a new corpus was needed

The main 10,000-line corpus (`data/synthetic_logs.jsonl`) can't answer this
question. Its generator (`src/generate_logs.py`) calls `make_slots()`
independently for every record, so almost every IP/SSN/CREDIT_CARD/MRN
value is unique -- confirmed directly: 2,327 IP mentions, 2,327 distinct
values, zero repeats; same story for SSN, CREDIT_CARD, MRN. Only PERSON
has any real recurrence (353 of 2,629 names repeat), and that's incidental
(Faker's name pool is small enough to coincidentally collide), not
intentional.

An investigation is fundamentally a question about recurrence -- "has this
IP/user/card shown up before, and can I get back to the real value." A
corpus where four of five entity types essentially never recur can't
honestly support measuring that, so `generate_scenario_corpus.py` builds a
small (400-record) supplementary corpus with a fixed pool of "actor" values
per type deliberately reused across many records (simulating a recurring
source IP, user, or card across an incident timeline), mixed with one-off
noise values. This corpus exists only to generate a measurable recurrence
pattern for this experiment -- it is not a second general validation
corpus and no other claim in this project is based on it.

## What was measured, and how

`measure_investigative_utility.py` runs against ground-truth spans, not
detector output -- this isolates what the three METHODS structurally do to
investigative utility from whether the detector finds the entity in the
first place. Detection false negatives are a separate, already-measured
axis (see `validation/piibench/README.md` and the Loghub-based validation
in the main `README.md`); folding them in would only make every method's
real-world number here worse, never better, so leaving them out is a
scope decision in the methods' favor, not against it, and is disclosed
here so it isn't mistaken for the full end-to-end picture.

**Correlation** (`results.json`'s `correlation` block, per type): across
different records, does a method's output let two mentions of the *same*
real value be linked (`correlation_recall`), and does it avoid wrongly
linking two mentions of *different* real values (`false_linkage_rate`)?
These are reported separately on purpose -- a method can trivially score
well on one while failing the other completely.

**Reversibility** (`results.json`'s `reversibility` block, per type): can
an authorized investigator recover the real value?
- `redact`: measured at 0%, confirmed directly (every value collapses to
  the same fixed placeholder; nothing about the original survives).
- `pseudonymize`: blind reversal is 0% by construction (one-way
  HMAC-SHA256, no map is ever stored) -- stated rather than "tested",
  since demonstrating a correct keyed hash's non-invertibility isn't a
  meaningful computation to run. What IS tested directly: **candidate-list
  verification** -- an investigator who already has a short list of
  suspected values (e.g. "was it one of these five accounts") and the
  pseudonymization key can confirm or rule out each candidate by
  recomputing the same HMAC and comparing. This is a materially different,
  much more realistic capability than blind reversal.
- `tokenize`: measured via the real `TokenStore.resolve()` path, given
  store access.

## Results (this corpus, this run)

Across all six entity types the pattern is consistent and not close:

| Method | correlation_recall | false_linkage_rate | direct reversal |
|---|---|---|---|
| redact | 1.0 | **1.0** | 0.0 |
| pseudonymize | 1.0 | 0.0 | 0.0 (candidate-verification: 1.0, given key + short list) |
| tokenize | 1.0 | 0.0 | 1.0 (given store access) |

The `correlation_recall = 1.0` for *every* method, including redact, is
not a point in redaction's favor -- it's an artifact of the metric's
definition (a constant placeholder trivially "links" same-value pairs
because it links *everything*). The number that actually distinguishes
the methods is `false_linkage_rate`: redaction sits at 1.0, meaning *any*
two different real entities of the same type become indistinguishable
after redaction -- not just "uncorrelated" but actively, wrongly
presented as the same entity to anything reading the output downstream.
Pseudonymization and tokenization both hold that at 0.0 across every type
tested, with zero real hash/token collisions observed in this corpus.

## What this does and doesn't show

- This is a structural demonstration on a small synthetic corpus (400
  records, 6-12 recurring actors per type), not a large-scale empirical
  study and not a claim about collision rates at production volume --
  HMAC-SHA256 and the token generation scheme's collision resistance are
  well-established cryptographic properties, not something a 400-record
  run meaningfully stress-tests.
- The candidate-list verification result (`1.0` correct identification
  across 30 trials, 5 candidates each) demonstrates the *mechanism*
  works exactly as the cryptography predicts; it says nothing about how
  often a real investigation would actually have a short, accurate
  candidate list to test in the first place -- that's an investigative-
  process question this experiment doesn't and can't answer.
- All three methods require some form of privileged access to be useful
  to an investigator at all (tokenize needs TokenStore access; the
  candidate-verification path needs the pseudonymization key). Neither
  is "free" recoverability; this experiment measures what's possible
  *given* that access, consistent with this project's existing
  audit-key/StorageProvider access-control assumptions elsewhere.
- Detection false negatives are explicitly out of scope here (see above)
  and must be stated alongside any number quoted from this script.

## Reproducing

```bash
python3 validation/investigative_utility/generate_scenario_corpus.py
python3 validation/investigative_utility/measure_investigative_utility.py
```

Both scripts are deterministic (fixed random seeds), so a rerun against
an unmodified `anonymize.py` reproduces `results.json` exactly.
