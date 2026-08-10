"""
Evaluation harness. Loads the synthetic dataset, runs the detection ensemble,
matches predictions against ground truth by overlap, and reports
precision/recall/F1 per entity type plus throughput.

Matching rule: a predicted span counts as a match for a ground-truth span if
they share the same canonical type and their character ranges overlap by at
least one character. Matching is greedy and one-to-one (each ground-truth
span can be matched by at most one prediction and vice versa), which is the
standard approach for span-level NER evaluation and avoids inflating recall
by letting one prediction "cover" multiple ground-truth spans.
"""
import sys
import json
import time
import argparse
from collections import defaultdict

sys.path.insert(0, "src")
import detect  # noqa: E402
import fields  # noqa: E402


def overlaps(a: dict, b: dict) -> bool:
    return a["start"] < b["end"] and b["start"] < a["end"]


def match_spans(gold: list[dict], pred: list[dict]) -> tuple[int, int, int]:
    """Returns (tp, fp, fn) for a single document's spans, matched per type."""
    tp = 0
    matched_gold = set()
    matched_pred = set()
    for gi, g in enumerate(gold):
        for pi, p in enumerate(pred):
            if pi in matched_pred:
                continue
            if p["type"] == g["type"] and overlaps(g, p):
                matched_gold.add(gi)
                matched_pred.add(pi)
                tp += 1
                break
    fn = len(gold) - len(matched_gold)
    fp = len(pred) - len(matched_pred)
    return tp, fp, fn


def load_dataset(path: str) -> list[dict]:
    with open(path) as f:
        return [json.loads(line) for line in f]


def _mask_regex_covered_fields(text: str, log_type: str, regex_hits: list[dict]):
    """Field-level counterpart to the whole-line entropy gate below.

    The whole-line gate (use_entropy_gate) skips NER for the entire line
    the moment ANY regex hit exists anywhere in it -- cheap and fast, but
    this is exactly the mechanism behind the documented PERSON recall
    collapse (0.113 tiered vs. 0.359 naive, measured live 2026-08-09
    against this project's current corpus -- see README.md's comparison
    table and Known Limitations section): an SSN regex hit in one field
    silently suppresses NER for a PERSON name sitting in a completely
    different field on the same line.

    This function narrows the gate to field granularity using fields.py's
    existing structured-field extraction (already built for drift.py, not
    written new for this). For each field the extractor recognizes,
    checks whether a regex hit falls inside that specific field's
    character span; only THOSE spans get masked out (replaced with '#',
    same length, so all other offsets in the line stay valid) before
    NER runs. A name sitting in an unmasked field, or in free text
    outside any recognized field, is still visible to NER.

    MEASURED RESULT (README.md's comparison table, run 2026-08-09 against
    the real spaCy model -- this module's own dev sandbox has no route to
    that model, so this is the first time these numbers existed): the
    recall fix works as designed -- PERSON recall 0.356, nearly closing
    the entire gap to naive's 0.359, with BETTER precision than naive
    besides (0.657 vs. 0.637). The throughput claim below did NOT hold up
    against real data: field-gated measured ~100 events/sec, SLOWER than
    naive's ~119, not faster. Root cause: a PERSON-bearing field is
    essentially never also regex-covered (a name doesn't live in a field
    that matches SSN/EMAIL/CREDIT_CARD/IP/MRN), so the full-skip branch
    below rarely fires, NER runs anyway on most lines, and this
    function's own extract_fields()/masking work becomes pure overhead on
    top of a NER call that was happening regardless. Left both the
    original design reasoning and this correction in place, rather than
    quietly rewriting history, since the discrepancy between predicted
    and measured behavior is itself useful information about when a
    "this should be faster" intuition needs verifying before being
    trusted.

    Returns:
      - the masked text, if NER should still run on it (there's
        unmasked content that could plausibly hold a name or other
        NER-only entity)
      - None, if every recognized field on this line was already
        regex-covered and nothing alphabetic remains outside the masked
        spans -- the one case where skipping the NER call entirely is
        actually safe, and the ONLY mechanism by which this function
        could recover any of the whole-line gate's throughput advantage.
        Measured to fire rarely enough in practice that it does not, on
        this corpus -- see MEASURED RESULT above.
      - the ORIGINAL text unchanged, if fields.py doesn't recognize this
        log_type's structure at all (unrecognized log_type, or a syslog
        message shape outside its documented coverage) or extracted no
        fields -- falling back to running NER on the full line is the
        conservative, recall-safe choice when field structure isn't
        known, not a silent skip.

    Known, disclosed limitation: field boundaries are located via
    text.find(value), since fields.py returns values, not offsets. This
    is correct except when a field's value string is not unique within
    the line (e.g. the same short value coincidentally appears earlier
    as a substring of something else) -- an edge case, not something
    this implementation claims to have ruled out, and one a real
    per-field extractor returning offsets directly would close properly.

    The "safe to skip NER entirely" decision below only looks at
    extracted FIELD VALUES, not the whole line -- field names (`SRC=`,
    `PWD=`, the syslog tag prefix, KV separators) are structural, not
    PII-bearing, and checking the whole line's alphabetic content for
    the skip decision would make it almost never trigger, since field
    names are alphabetic too and appear on nearly every structured line.
    """
    extracted = fields.extract_fields(log_type, text) if log_type else {}
    if not extracted:
        return text

    masked = list(text)
    any_field_masked = False
    any_alpha_value_unmasked = False
    for value in extracted.values():
        if not value:
            continue
        idx = text.find(value)
        if idx == -1:
            continue
        end = idx + len(value)
        if any(h["start"] < end and idx < h["end"] for h in regex_hits):
            any_field_masked = True
            for i in range(idx, end):
                masked[i] = "#"
        elif any(c.isalpha() for c in value):
            any_alpha_value_unmasked = True

    if not any_field_masked:
        return text
    if any_alpha_value_unmasked:
        return "".join(masked)
    return None


def run_evaluation(entries: list[dict], use_ner: bool, use_entropy_gate: bool = False,
                    use_flattened: bool = False, use_field_gate: bool = False):
    """use_entropy_gate: if True, only run NER on lines with no regex hit at all
    (the 'tiered' strategy described in the chapter). If False, run NER on
    every line regardless of regex results (the naive strategy).
    use_field_gate: field-level variant of use_entropy_gate (see
    _mask_regex_covered_fields above) -- only masks the specific fields a
    regex hit actually covers, instead of skipping NER for the whole
    line. Mutually exclusive with use_entropy_gate in intent (both are
    ways of deciding when to call NER); if both are somehow passed True,
    use_field_gate takes precedence per the branch order below.
    use_flattened: if True, also run the flattened-username name-dictionary
    layer (src/flattened_names.py), added specifically to address the
    documented 5.9% recall gap on concatenated name tokens."""
    per_type = defaultdict(lambda: {"tp": 0, "fp": 0, "fn": 0})
    t0 = time.time()
    for entry in entries:
        text = entry["log"]
        gold = entry["pii"]
        log_type = entry.get("log_type")

        regex_hits = detect.scan_regex(text)
        pred = list(regex_hits)

        if use_ner:
            if use_field_gate and regex_hits:
                ner_text = _mask_regex_covered_fields(text, log_type, regex_hits)
                if ner_text is not None:
                    pred += detect.scan_ner(ner_text)
                # else: every regex-coverable field was masked and nothing
                # alphabetic remained -- safe to skip the NER call entirely.
            elif use_entropy_gate and regex_hits:
                pass  # whole-line tiered strategy: skip NER if regex already found something
            else:
                pred += detect.scan_ner(text)

        if use_flattened:
            pred += detect.scan_flattened(text)

        # de-duplicate overlapping same-type hits from different layers
        # (keep first occurrence; layers agreeing is not a defect)
        dedup = []
        for h in pred:
            if not any(h["type"] == d["type"] and overlaps(h, d) for d in dedup):
                dedup.append(h)

        by_type_gold = defaultdict(list)
        by_type_pred = defaultdict(list)
        for g in gold:
            by_type_gold[g["type"]].append(g)
        for p in dedup:
            by_type_pred[p["type"]].append(p)

        all_types = set(by_type_gold) | set(by_type_pred)
        for t in all_types:
            tp, fp, fn = match_spans(by_type_gold[t], by_type_pred[t])
            per_type[t]["tp"] += tp
            per_type[t]["fp"] += fp
            per_type[t]["fn"] += fn

    elapsed = time.time() - t0
    return per_type, elapsed


def summarize(per_type: dict, n_entries: int, elapsed: float, label: str):
    print(f"\n=== {label} ===")
    print(f"{'Type':<14}{'TP':>6}{'FP':>6}{'FN':>6}{'Precision':>11}{'Recall':>9}{'F1':>7}")
    total_tp = total_fp = total_fn = 0
    for t, c in sorted(per_type.items()):
        tp, fp, fn = c["tp"], c["fp"], c["fn"]
        total_tp += tp
        total_fp += fp
        total_fn += fn
        precision = tp / (tp + fp) if (tp + fp) else 0.0
        recall = tp / (tp + fn) if (tp + fn) else 0.0
        f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
        print(f"{t:<14}{tp:>6}{fp:>6}{fn:>6}{precision:>11.3f}{recall:>9.3f}{f1:>7.3f}")
    micro_p = total_tp / (total_tp + total_fp) if (total_tp + total_fp) else 0.0
    micro_r = total_tp / (total_tp + total_fn) if (total_tp + total_fn) else 0.0
    micro_f1 = 2 * micro_p * micro_r / (micro_p + micro_r) if (micro_p + micro_r) else 0.0
    print(f"{'MICRO-AVG':<14}{total_tp:>6}{total_fp:>6}{total_fn:>6}{micro_p:>11.3f}{micro_r:>9.3f}{micro_f1:>7.3f}")
    print(f"Processed {n_entries} entries in {elapsed:.2f}s -> {n_entries/elapsed:.1f} events/sec")
    return {"micro_precision": micro_p, "micro_recall": micro_r, "micro_f1": micro_f1,
            "events_per_sec": n_entries / elapsed}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", type=str, default="data/synthetic_logs.jsonl")
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()

    entries = load_dataset(args.data)
    if args.limit:
        entries = entries[:args.limit]

    print(f"Loaded {len(entries)} entries ({sum(1 for e in entries if e['pii'])} with PII)")

    per_type, elapsed = run_evaluation(entries, use_ner=False)
    summarize(per_type, len(entries), elapsed, "Regex only")

    per_type, elapsed = run_evaluation(entries, use_ner=True, use_entropy_gate=True)
    summarize(per_type, len(entries), elapsed, "Regex + NER (tiered: NER only when regex found nothing)")

    per_type, elapsed = run_evaluation(entries, use_ner=True, use_field_gate=True)
    summarize(per_type, len(entries), elapsed,
              "Regex + NER (field-gated: NER skipped only for regex-covered fields, not whole lines)")

    per_type, elapsed = run_evaluation(entries, use_ner=True, use_entropy_gate=False)
    summarize(per_type, len(entries), elapsed, "Regex + NER (naive: NER on every line)")

    per_type, elapsed = run_evaluation(entries, use_ner=True, use_entropy_gate=False,
                                        use_flattened=True)
    summarize(per_type, len(entries), elapsed,
              "Regex + NER (naive) + flattened-username name dictionary")
