"""
Evaluates REDACT's detection ensemble (src/detect.py) against PIIBench --
arXiv:2604.15776 (Jha, 2026), a unified 48-entity-type, 3.35M-mention PII/NER
benchmark consolidating ten public datasets. Identified during this
project's landscape scan as a follow-up worth running: unlike n2c2 (see
validation/real_data/PHI_DATASET_ACCESS.md), PIIBench needs no DUA or
registration -- it is a public, Apache-2.0-licensed dataset, downloadable
directly from HuggingFace (Pritesh-2711/pii-bench) or via the benchmark
repo's own data pipeline (github.com/pritesh-2711/pii-bench).

REAL, CONFIRMED FORMAT (confirmed against an actual downloaded record, not
assumed -- same discipline as this project's other real-data work, e.g.
n2c2's PHI_DATASET_ACCESS.md declining to write a parser without a real
sample in hand):

  - Records are JSONL with `tokens` (per-token strings, several sources
    wordpiece-tokenized -- continuation pieces prefixed `##`, lowercased,
    accents stripped), `labels` (parallel BIO tags), `source`, and -- only
    when the pipeline was run with `--include-text` -- a `text` field
    holding the ORIGINAL, natural-language source string.

REVISION HISTORY -- kept, not deleted, because the mistake this corrects is
itself informative (same policy this project applies elsewhere, e.g.
build_ner_candidate's docstring in src/detect.py): the first version of
this script built the text REDACT scans as `" ".join(tokens)`, matching
run_benchmarking.py's own documented reconstruction for spans_to_bio(). A
live smoke test (200 records, 2026-09) found this reconstruction actively
corrupts REDACT's input: wordpiece-split tokens get spaces inserted between
every fragment (a real IPv4 gold span rendered as `'140 . 115 . 236 . 150'`
-- no IPv4 regex matches that), AND the token stream itself is lowercased
and accent-stripped relative to the real source text (`chere` vs the real
`chère`), corrupting NER input on top of the regex-breaking whitespace
problem. Neither of REDACT's own detectors was actually broken; the
reconstructed input they were being run against was.

CURRENT APPROACH: run REDACT against the record's real `text` field (falls
back to `" ".join(tokens)` only if `text` is absent -- true for a record
whose source dataset is genuinely token-only, e.g. conll2003/wikiann/
few_nerd/multinerd when PIIBench's own pipeline has no original string to
preserve; PIIBench's `--include-text` flag documents this exact fallback).
Gold spans, however, are only ever defined against the TOKEN stream (BIO
tags have no other coordinate system) -- so each gold entity's character
position in the real `text` is recovered by building a small regex from its
constituent tokens: a `##`-prefixed continuation token is glued directly
to the previous one (no separator, matching wordpiece's own semantics),
every other token boundary allows `\s*` (zero or more whitespace/
punctuation-adjacent characters), and the whole pattern is matched
case-insensitively, searched forward from the end of the previous
successfully-anchored span in the same record (keeps the match anchored to
the right occurrence when the same short token sequence recurs). A gold
span that cannot be re-anchored this way (e.g. its accent-stripped tokens
truly can't be found in the accented real text) is EXCLUDED from scoring,
not silently guessed at -- and the count of how many, per type, is
reported so this isn't hidden data loss.

METHODOLOGY DECISION, unchanged from before and still the single most
important thing to disclose wherever this script's output is cited (see
validation/piibench/README.md for the full devil's-advocate writeup):

  REDACT is scored ONLY on the five entity types it actually claims to
  detect (PERSON, EMAIL, SSN, CREDIT_CARD, IP -- see src/detect.py's
  REGEX_PATTERNS/_PRESIDIO_TO_CANONICAL). Every other PIIBench type (ORG,
  LOC, DATE_TIME, URL, IBAN, PASSPORT_NUMBER, ...) is excluded from scoring
  entirely -- neither a possible miss nor a possible false positive.
  Scoring REDACT against PIIBench's full 82-type taxonomy would produce a
  misleadingly low number for types REDACT was never built to detect.

  A second, unchanged caveat: PIIBench's constituent datasets are general
  free text, not log-shaped. This remains a cross-check of REDACT's
  detection COMPONENT out-of-domain, not a validation of REDACT's actual
  log-pipeline claim.

Usage (run on your own machine -- this project's sandbox has no working
shell this session, see BUGS_AND_FIXES.md's standing note):
    python validation/piibench/evaluate_piibench.py \
        --test-file data/test_5k.jsonl
"""
import argparse
import json
import re
import sys
import os
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))
import detect  # noqa: E402


# PIIBench canonical label -> REDACT canonical label. Only entity types
# REDACT's own detect.py actually claims to detect are listed here --
# everything else in PIIBench's taxonomy is deliberately left unmapped (see
# module docstring's "METHODOLOGY DECISION").
#
# NAME/FIRST_NAME/LAST_NAME and CREDIT_CARD_NUMBER/CREDIT_DEBIT_CARD are
# included as siblings of PERSON/CREDIT_CARD, not separate types of their
# own -- confirmed via validation/piibench/diagnose_sibling_labels.py
# (2026-09, live 200-record run) that a real chunk of what looked like
# false positives under a stricter one-label-per-type mapping were actually
# correct detections scored wrong purely because PIIBench's own taxonomy
# splits "this text names a specific individual" / "this text is a payment
# card number" across multiple sibling labels REDACT has no reason to
# distinguish (16/160 PERSON FPs, 2/6 CREDIT_CARD FPs on that run). This is
# a taxonomy-alignment fix, not scope creep: it does not add any PIIBench
# type REDACT wasn't already conceptually claiming to detect.
#
# KNOWN, DISCLOSED LIMITATION from this merge: FIRST_NAME and LAST_NAME are
# sometimes tagged as two separate ADJACENT BIO spans rather than one
# combined PERSON span. extract_gold_entities() below still emits them as
# two distinct gold entities (a "B-" tag always starts a new entity, even
# when the mapped type is unchanged from the previous one) -- so a single
# correct REDACT detection spanning "John Smith" can satisfy only one of
# the two sibling gold spans under the overlap-matching in evaluate()
# below, leaving the other counted as a false negative. This under-credits
# recall slightly in that specific case; it does not inflate precision or
# manufacture false true-positives. Not fixed here (would need span-merging
# across adjacent same-mapped-type BIO entities) -- this evaluation is
# scoped as a cross-check, not a publication-grade scoring harness.
PIIBENCH_TO_REDACT = {
    "PERSON": "PERSON",
    "NAME": "PERSON",
    "FIRST_NAME": "PERSON",
    "LAST_NAME": "PERSON",
    "EMAIL": "EMAIL",
    "SSN": "SSN",
    "CREDIT_CARD": "CREDIT_CARD",
    "CREDIT_CARD_NUMBER": "CREDIT_CARD",
    "CREDIT_DEBIT_CARD": "CREDIT_CARD",
    "IP_ADDRESS": "IP",
}


def extract_gold_entities(tokens: list[str], labels: list[str]) -> list[tuple[str, int, int]]:
    """Returns (redact_type, tok_start, tok_end) inclusive token-index
    ranges for each gold entity whose PIIBench type maps onto one of
    REDACT's own detected types. Pure BIO-tag walking, no text/offsets
    involved -- offset recovery happens separately, in locate_span_in_text,
    against whatever text REDACT is actually scanning."""
    entities = []
    cur_type = None
    cur_start = None
    for i, label in enumerate(labels):
        if label == "O":
            if cur_type is not None:
                entities.append((cur_type, cur_start, i - 1))
                cur_type = None
            continue
        tag, _, etype = label.partition("-")
        mapped = PIIBENCH_TO_REDACT.get(etype)
        if tag == "B" or mapped != cur_type:
            if cur_type is not None:
                entities.append((cur_type, cur_start, i - 1))
            cur_type = mapped
            cur_start = i
        # else: "I-" continuing the same mapped type -- extend, do nothing yet.
    if cur_type is not None:
        entities.append((cur_type, cur_start, len(labels) - 1))
    return [(t, s, e) for (t, s, e) in entities if t is not None]


def build_span_regex(tokens: list[str], tok_start: int, tok_end: int) -> "re.Pattern | None":
    """Builds a regex that finds this token range's real-text occurrence,
    tolerating the exact two corruptions the token stream introduces
    relative to the real source string: (1) wordpiece continuation pieces
    (`##foo`) glue directly onto the previous token with no gap, everything
    else allows `\\s*` between tokens so both "13-41" (real, no space) and
    "13 - 41" (if a source ever does have real spaces there) match the same
    pattern; (2) case is ignored, covering the lowercasing every
    wordpiece-tokenized source in this benchmark applies. Does NOT restore
    stripped accents/diacritics -- a token whose only real-text form
    contains an accent the tokenizer removed (e.g. "chere" for the real
    "chère") will not match here, which is exactly why unmatched spans are
    excluded and counted rather than silently forced to match somewhere
    wrong."""
    parts = []
    for k in range(tok_start, tok_end + 1):
        tok = tokens[k]
        if tok.startswith("##") and parts:
            parts.append(re.escape(tok[2:]))
        else:
            if parts:
                parts.append(r"\s*")
            parts.append(re.escape(tok[2:] if tok.startswith("##") else tok))
    pattern = "".join(parts)
    if not pattern:
        return None
    return re.compile(pattern, re.IGNORECASE)


def locate_gold_spans(tokens: list[str], labels: list[str], text: str) -> tuple[list[dict], dict]:
    """Returns (gold_spans, unanchored_counts). gold_spans only contains
    entities successfully re-anchored to a real character position in
    `text`; unanchored_counts tallies, per REDACT type, how many gold
    entities could NOT be re-anchored (see build_span_regex's docstring for
    why that happens) -- reported, never silently dropped."""
    entities = extract_gold_entities(tokens, labels)
    spans = []
    unanchored = {}
    cursor = 0
    for etype, tok_start, tok_end in entities:
        pattern = build_span_regex(tokens, tok_start, tok_end)
        m = pattern.search(text, cursor) if pattern else None
        if m is None and pattern is not None:
            m = pattern.search(text)  # retry from the start of the record
        if m is None:
            unanchored[etype] = unanchored.get(etype, 0) + 1
            continue
        spans.append({"type": etype, "start": m.start(), "end": m.end()})
        cursor = m.end()
    return spans, unanchored


def load_records(path: str, max_records: "int | None") -> list[dict]:
    records = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    if not records:
        raise ValueError(f"'{path}' is empty.")
    if max_records:
        records = records[:max_records]
    return records


def evaluate(records: list[dict]) -> dict:
    """Same overlap-based, same-type span matching convention as
    validation/real_data/inject_and_evaluate.py's evaluate() -- kept
    identical deliberately so numbers from this script are read the same
    way as every other real-data condition already in this project."""
    tp = fp = fn = 0
    per_type = {t: {"tp": 0, "fp": 0, "fn": 0} for t in set(PIIBENCH_TO_REDACT.values())}
    unanchored_total = {}
    records_missing_text = 0

    t0 = time.perf_counter()
    scored_records = 0
    for rec in records:
        text = rec.get("text")
        if not text:
            records_missing_text += 1
            text = " ".join(rec["tokens"])

        gold, unanchored = locate_gold_spans(rec["tokens"], rec["labels"], text)
        for etype, count in unanchored.items():
            unanchored_total[etype] = unanchored_total.get(etype, 0) + count

        preds = [h for h in detect.detect_all(text) if h["type"] in PIIBENCH_TO_REDACT.values()]
        # De-dup overlapping same-type hits from different detection layers,
        # matching inject_and_evaluate.py's own convention.
        dedup = []
        for p in preds:
            if not any(p["type"] == d["type"] and p["start"] < d["end"] and d["start"] < p["end"]
                       for d in dedup):
                dedup.append(p)
        preds = dedup

        matched = set()
        for p in preds:
            hit = False
            for i, g in enumerate(gold):
                if i in matched:
                    continue
                if g["type"] == p["type"] and p["start"] < g["end"] and g["start"] < p["end"]:
                    matched.add(i)
                    hit = True
                    tp += 1
                    per_type[p["type"]]["tp"] += 1
                    break
            if not hit:
                fp += 1
                per_type[p["type"]]["fp"] += 1
        for i, g in enumerate(gold):
            if i not in matched:
                fn += 1
                per_type[g["type"]]["fn"] += 1
        scored_records += 1

    elapsed = time.perf_counter() - t0
    prec = tp / (tp + fp) if (tp + fp) else 0.0
    rec_ = tp / (tp + fn) if (tp + fn) else 0.0

    return {
        "records_scored": scored_records,
        "records_missing_text_field": records_missing_text,
        "elapsed_seconds": round(elapsed, 1),
        "overall": {"precision": round(prec, 4), "recall": round(rec_, 4), "tp": tp, "fp": fp, "fn": fn},
        "per_type": {
            t: {
                **v,
                "precision": round(v["tp"] / (v["tp"] + v["fp"]), 4) if (v["tp"] + v["fp"]) else None,
                "recall": round(v["tp"] / (v["tp"] + v["fn"]), 4) if (v["tp"] + v["fn"]) else None,
                "unanchored_gold_excluded": unanchored_total.get(t, 0),
            }
            for t, v in per_type.items()
        },
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--test-file", default="data/test_5k.jsonl",
                         help="Path to PIIBench's test_5k.jsonl (default: data/test_5k.jsonl)")
    parser.add_argument("--max-records", type=int, default=None,
                         help="Cap evaluation at N records (smoke-test)")
    parser.add_argument("--output", default=None,
                         help="Optional path to write results as JSON")
    args = parser.parse_args()

    if not os.path.exists(args.test_file):
        print(f"ERROR: '{args.test_file}' not found.")
        print("Get it from the PIIBench repo (github.com/pritesh-2711/pii-bench):")
        print("  python run_data_pipeline.py --include-text       # then:")
        print("  python create_evaluation_subset.py")
        print("(--include-text matters: without it, records have no real")
        print(" source text, only a lossy whitespace-joined token fallback.)")
        sys.exit(1)

    records = load_records(args.test_file, args.max_records)
    print(f"Loaded {len(records):,} records from {args.test_file}")
    print(f"Scoring REDACT ONLY on: {sorted(set(PIIBENCH_TO_REDACT.values()))}")
    print("(all other PIIBench entity types excluded from scoring -- see this")
    print(" script's module docstring for why)\n")

    results = evaluate(records)

    if results["records_missing_text_field"]:
        print(f"NOTE: {results['records_missing_text_field']} of {results['records_scored']} "
              f"records had no 'text' field -- fell back to the lossy whitespace-joined\n"
              f"token reconstruction for those specific records. Re-run the PIIBench data\n"
              f"pipeline with --include-text if this number looks larger than expected.\n")

    print("=== REDACT vs. PIIBench (scoped to REDACT's claimed entity types) ===")
    o = results["overall"]
    print(f"  overall: P={o['precision']:.3f} R={o['recall']:.3f} "
          f"(TP={o['tp']} FP={o['fp']} FN={o['fn']}) in {results['elapsed_seconds']}s")
    for t, v in sorted(results["per_type"].items()):
        if v["tp"] + v["fp"] + v["fn"] + v["unanchored_gold_excluded"] == 0:
            continue
        p_str = f"{v['precision']:.3f}" if v["precision"] is not None else "n/a"
        r_str = f"{v['recall']:.3f}" if v["recall"] is not None else "n/a"
        print(f"  {t:<12} P={p_str} R={r_str} (TP={v['tp']} FP={v['fp']} FN={v['fn']}) "
              f"[{v['unanchored_gold_excluded']} gold excluded, unanchorable]")

    if args.output:
        with open(args.output, "w") as f:
            json.dump(results, f, indent=2)
        print(f"\nResults written to: {args.output}")


if __name__ == "__main__":
    main()
