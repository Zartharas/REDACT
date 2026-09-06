# Validating REDACT against PIIBench

PIIBench (arXiv:2604.15776, Jha, April 2026) is a unified benchmark
consolidating ten public PII/NER datasets into 2,369,883 annotated
sequences and 3.35 million entity mentions across a canonical taxonomy the
paper describes as 48 entity types (the maintained benchmark repo's own
result metadata calls it a "corrected 82-entity taxonomy" in at least one
place -- a live discrepancy between the paper and the repo, not resolved
here). All eight systems the paper benchmarks score span-level F1 below
0.14; the best (Microsoft Presidio, F1=0.1385) gets zero recall on most
entity types.

Unlike n2c2 (see `validation/real_data/PHI_DATASET_ACCESS.md`), this
dataset needs **no data use agreement or registration**. It's Apache-2.0
licensed code with data hosted directly on HuggingFace
(`Pritesh-2711/pii-bench`), or reproducible from source via the benchmark's
own repo: `github.com/pritesh-2711/pii-bench`.

## Getting the data

```bash
git clone https://github.com/pritesh-2711/pii-bench
cd pii-bench
pip install -r requirements.txt
python run_data_pipeline.py --include-text   # downloads + normalizes all 10
                                              # sources, preserving real
                                              # source text (see below)
python create_evaluation_subset.py           # writes the fixed, paper-
                                              # comparable data/test_5k.jsonl
                                              # (5,000 records, source-
                                              # stratified, seed 42)
```

`--include-text` matters and is not optional in practice: without it,
records only carry `tokens`/`labels`/`source`, with no real source string
at all -- forcing REDACT to run against a lossy `" ".join(tokens)`
reconstruction that actively corrupts input (see "What this actually
measures" below for what that did to a live run). Two other real bugs were
found and fixed getting the pipeline running at all, both a `datasets`
library v4+ compatibility break in the upstream repo, not this project's
own code: `load_dataset('wikiann', 'en')` and
`load_dataset('conll2003', ...)` both use bare, unnamespaced repo IDs that
`huggingface_hub` >= 1.16 rejects outright (`HfUriError`) -- fixed by
pointing both at their current namespaced locations,
`unimelb-nlp/wikiann` and `eriktks/conll2003` respectively, in the cloned
repo's own `src/download_datasets.py`. A third, unrelated failure
(`OSError: Too many open files`) while downloading `Babelscape/multinerd`
was a macOS file-descriptor ulimit, fixed with `ulimit -n 10240` before
rerunning, not a code issue at all.

`data/test_5k.jsonl` is the file `validation/piibench/evaluate_piibench.py`
expects. Using the fixed subset (not the full multi-million-row test.jsonl)
keeps this a one-machine, no-paid-infra run, consistent with this
project's own constraints, and keeps results directly comparable to the
paper's own published baseline numbers on the same subset.

## What this actually measures -- read before citing a number from it

**REDACT is scored only on the five entity types it actually claims to
detect**: PERSON, EMAIL, SSN, CREDIT_CARD, and IP (mapped from PIIBench's
`IP_ADDRESS` label). Every other PIIBench type -- ORG, LOC, DATE_TIME, URL,
IBAN, PASSPORT_NUMBER, DRIVER_LICENSE_NUMBER, TAX_ID, CRYPTO_ADDRESS,
NATIONAL_ID, ACCOUNT_NUMBER, PHONE_NUMBER, and more -- is excluded from
scoring entirely: not counted as a miss, not counted as an opportunity for
a false positive.

This is a deliberate scope restriction, and it must be stated explicitly
wherever this script's output gets cited (book chapter, paper
resubmission). The reason: REDACT was never built to detect a passport
number or an IBAN. Scoring it against PIIBench's full taxonomy would
produce a number close to zero, which would be *true* but *misleading* --
a reviewer who checks would rightly flag "REDACT scores near-zero on
PIIBench" as an unfair comparison if the scope restriction weren't
disclosed in the same breath.

**A second, equally important scoping decision, found the hard way via a
live smoke test:** the text REDACT is scanned against is the record's real
`text` field (present only when `--include-text` was used), not
`" ".join(tokens)`. A first version of this evaluation used the token-join
reconstruction -- matching how PIIBench's own `run_benchmarking.py` scores
its published baselines -- and a live 200-record run showed this actively
corrupts REDACT's input for several entity types: a real IPv4 gold span
came back as `'140 . 115 . 236 . 150'` (wordpiece tokenization splits each
period into its own token; joining with spaces destroys the shape no IP
regex would then match), and several sources' token streams are lowercased
and accent-stripped relative to the real text (French `chere` for the real
`chère`), corrupting NER input too. IP recall measured **0%** on that first
run purely because of this -- not a REDACT detection gap. Gold spans are
now re-anchored into the real `text` field via a small per-token regex
(see `evaluate_piibench.py`'s `build_span_regex()`); any gold entity that
can't be re-anchored (e.g. its accent-stripped tokens truly aren't findable
in the accented real text) is excluded from scoring and reported as a
count, not silently dropped. **A consequence worth knowing:** since
PIIBench's own published Presidio/spaCy baselines (F1=0.1385 best-in-class)
were scored against the token-join reconstruction, not the real text, part
of why every system PIIBench benchmarks scores so low may be this same
reconstruction artifact rather than pure out-of-domain difficulty -- this
project has not independently verified that, but it's a real,
falsifiable implication of what the live smoke test found, worth raising
rather than assuming away if this evaluation is cited against the paper's
own published numbers.

## Devil's advocate: what this result does NOT prove, and how to say so

1. **Domain mismatch.** PIIBench's ten constituent datasets are general
   free text -- financial documents, Wikipedia/news NER corpora
   (wikiann, few-nerd, conll2003, multinerd), synthetic PII sentences
   (ai4privacy, Isotonic, gretelai, nvidia Nemotron-PII). None of it is
   log-shaped. Running REDACT's detection layer against it is a legitimate
   cross-check of the underlying regex/NER **component** on out-of-domain
   text -- it is NOT a validation of REDACT's actual claim, which is about
   a **log-anonymization pipeline**. Report it as exactly that: "REDACT's
   detection component, evaluated out-of-domain against PIIBench," never
   "REDACT validated against PIIBench."

2. **Measurement-methodology noise already baked into PIIBench's own
   numbers.** The benchmark's own span-to-BIO realignment
   (`run_benchmarking.py`'s `spans_to_bio()`) uses a "first-span-wins"
   simplification and a several-character fallback search when a
   predicted span doesn't land cleanly on a token boundary. This script's
   own ground-truth direction (BIO -> character spans, via
   `bio_to_gold_spans()`) is exact, since it constructs the joined text
   itself rather than searching for it -- but any cross-comparison against
   PIIBench's own published Presidio/spaCy baseline numbers should account
   for this asymmetry rather than treating both sides as equally precise.

3. **Licensing -- do not quote raw PIIBench text in the chapter.** This
   project's own standing rule is synthetic/dummy data only, no real
   organizational or personal data, in anything published. PIIBench is
   licensed Apache-2.0 at the code level, but its ten constituent
   datasets carry their own separate licenses, and several of them
   (`wikiann`, `Babelscape/multinerd`, `DFKI-SLT/few-nerd`, `conll2003`)
   are built from real Wikipedia articles and real Reuters news text with
   real named entities -- not synthetic surrogates the way
   `ai4privacy`/`gretelai`/`nvidia Nemotron-PII` are. Reproducing a raw
   example sentence from PIIBench in the book chapter risks incidentally
   publishing a real person's real name lifted from a real news article,
   which is a different risk profile than this project's own Faker-based
   synthetic corpus. **Mitigation:** report only aggregate precision/
   recall/F1 numbers from this evaluation; if a worked example is needed
   for the chapter, construct it from REDACT's own Faker-generated
   synthetic corpus instead, exactly as every other worked example in this
   project already does.

4. **False-negative risk from IP_ADDRESS scope.** PIIBench's `IP_ADDRESS`
   label likely includes IPv6-shaped addresses; `src/detect.py`'s `IP`
   regex (`\b(?:\d{1,3}\.){3}\d{1,3}\b`) is IPv4-only. Any IPv6 gold spans
   will show up as false negatives under REDACT's `IP` type -- a genuine,
   pre-existing gap (not something this evaluation introduces), worth
   calling out explicitly in the per-type IP recall number rather than
   treating it as a surprise if IP recall looks low.

## Running it

```bash
python validation/piibench/evaluate_piibench.py \
    --test-file /path/to/pii-bench/data/test_5k.jsonl \
    --output validation/piibench/results.json
```

Add `--max-records 500` for a fast smoke-test before committing to the
full 5,000-record run.
