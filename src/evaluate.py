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


def _build_ner_candidate(text: str, log_type: str, regex_hits: list[dict]):
    """Field-level counterpart to the whole-line entropy gate below.

    The whole-line gate (use_entropy_gate) skips NER for the entire line
    the moment ANY regex hit exists anywhere in it -- cheap and fast, but
    this is exactly the mechanism behind the documented PERSON recall
    collapse (0.113 tiered vs. 0.359 naive, measured live 2026-08-09
    against this project's current corpus -- see README.md's comparison
    table and Known Limitations section): an SSN regex hit in one field
    silently suppresses NER for a PERSON name sitting in a completely
    different field on the same line.

    VERSION HISTORY, kept rather than deleted, because the mistake this
    corrects is itself informative: the original implementation of this
    function (`_mask_regex_covered_fields`, replaced 2026-08-09 the same
    day its numbers came back) MASKED regex-covered field spans by
    replacing them with same-length '#' placeholders, keeping the
    candidate text's total length unchanged. Measured against the real
    spaCy model that day: the recall fix worked (PERSON recall 0.113 ->
    0.356, nearly matching naive's 0.359, with better precision besides),
    but the throughput claim ("preserves some of the original throughput
    advantage") did NOT hold up -- field-gated measured ~100 events/sec,
    SLOWER than naive's ~119. Root cause, diagnosed after the fact: same-
    length masking doesn't reduce what spaCy actually has to tokenize and
    tag -- the candidate string NER received was exactly as long as the
    original line, so its cost was exactly naive's cost, plus this
    function's own extract_fields()/masking overhead on top, for a line
    that (correctly, for recall) still needed a real NER call almost
    every time a PERSON was actually present.

    This version fixes that by EXCISING regex-covered field spans instead
    of masking them -- physically removing those characters and splicing
    the surrounding text back together, so the candidate passed to NER is
    actually SHORTER than the original line, not just internally altered.
    Since NER cost scales with input length/token count, this is the
    correct lever for a real throughput improvement, not a same-length
    swap that only changes content.

    RE-MEASURED against the real model, same day (2026-08-09), after
    this rewrite: the recall prediction held almost exactly -- PERSON
    recall 0.360 (vs. the masking version's 0.356), precision 0.658 (vs.
    0.657), confirming excision and masking hide the identical
    characters from NER, just structured differently. Throughput
    improved substantially but did NOT fully close the gap to naive:
    ~110 events/sec, cutting the shortfall from 16.1% slower than naive
    down to 4.3% slower -- real progress, not a net win. Likely
    explanation, not independently confirmed: spaCy/Presidio's per-call
    cost on these already-short log lines (roughly 50-150 characters)
    isn't purely proportional to character count -- some of it is fixed
    pipeline overhead a modest excision (often under 20 characters)
    barely moves, while this function's own bookkeeping
    (extract_fields()/text.find()/slicing) adds a small real cost of its
    own on every gated line. Honest conclusion: use this where the
    PERSON-recall gap matters more than a strict throughput win over
    naive -- it is NOT a faster replacement for the whole-line tiered
    strategy, which remains the only genuinely fast option among this
    module's five conditions (~264 events/sec), at tiered's own
    documented recall cost.

    A hit's [start, end) reported against
    the (shorter) candidate string is remapped back to the ORIGINAL
    text's coordinate space via `_remap_hit` below and the `segments`
    this function returns, since callers (and the gold-span comparison in
    run_evaluation) need offsets into the real line, not the candidate.

    Returns one of three shapes:
      - (candidate, segments): NER should run on `candidate` (shorter
        than `text`, or equal to it if nothing was excised); `segments`
        is a list of (candidate_start, original_start) pairs, sorted by
        candidate_start, used by `_remap_hit` to translate a hit's
        offsets back to `text`'s coordinate space. `segments` is `None`
        specifically when candidate == text unchanged (nothing excised,
        or fields.py didn't recognize this log_type/extracted nothing --
        the conservative, recall-safe fallback), since no remapping is
        needed in that case.
      - (None, None): every recognized field on this line was already
        regex-covered and nothing alphabetic remains outside those spans
        -- the one case where skipping the NER call entirely is safe.

    Known, disclosed limitation: field boundaries are located via
    text.find(value), since fields.py returns values, not offsets. This
    is correct except when a field's value string is not unique within
    the line (e.g. the same short value coincidentally appears earlier
    as a substring of something else) -- an edge case, not something
    this implementation claims to have ruled out, and one a real
    per-field extractor returning offsets directly would close properly.

    A second, new disclosed limitation from excising rather than masking:
    splicing two previously-non-adjacent chunks directly together could,
    in principle, create an accidental new adjacency NER might
    misinterpret (e.g. two excised spans separated by nothing at all).
    In practice this is low-risk for the field shapes this project's
    fields.py extracts: KV-style values are always bounded by a
    delimiter (`=`, `;`, `,`, a space) that stays in the candidate on
    both sides of an excision, and CloudTrail's flattened JSON string
    values are always bounded by quote/punctuation characters for the
    same reason -- but it is a real, structurally different risk than
    same-length masking had, and is disclosed here rather than assumed
    away.

    The "safe to skip NER entirely" decision below only looks at
    extracted FIELD VALUES, not the whole line -- field names (`SRC=`,
    `PWD=`, the syslog tag prefix, KV separators) are structural, not
    PII-bearing, and checking the whole line's alphabetic content for
    the skip decision would make it almost never trigger, since field
    names are alphabetic too and appear on nearly every structured line.
    """
    extracted = fields.extract_fields(log_type, text) if log_type else {}
    if not extracted:
        return text, None

    # Engineering upgrade, 2026-08-09 (chasing the remaining 4.3%
    # throughput gap the excision rewrite didn't close): this used to be
    # a bare `text.find(value)` per field, restarting the scan from
    # position 0 of the line EVERY time, for EVERY field -- O(len(text))
    # work repeated once per extracted field, on every gated line.
    # fields.py's own extractors emit values in left-to-right source
    # order (the KV extractors iterate `_KV_KEY_RE.finditer(text)`
    # matches in order; extract_fields_cloudtrail's walk() visits a JSON
    # object's keys and a list's elements in their natural,
    # already-left-to-right order), so a single forward-advancing search
    # cursor covers the same ground once across all fields instead of
    # once per field. `text.find(value, search_cursor)` falls back to a
    # from-the-start search only if the forward search comes up empty
    # (the value's true occurrence sits before the cursor -- the source-
    # order assumption above doesn't strictly hold for every message
    # shape), so correctness never regresses versus the old
    # always-from-0 behavior in the worst case.
    #
    # This is measured and characterized here as a SPEED optimization,
    # not a fix for the pre-existing "value isn't unique in the line"
    # ambiguity this function's own docstring already discloses above.
    # An earlier draft of this comment overclaimed the cursor also
    # resolves that ambiguity in general; checked against a constructed
    # duplicate-value example while writing this and found that's only
    # true when an EARLIER-processed field has already advanced the
    # cursor past a decoy occurrence -- if the very first field
    # processed is the one with an earlier decoy, cursor and no-cursor
    # search land on the identical (still ambiguous) result, since both
    # start from position 0. Worth stating precisely rather than
    # claiming a correctness win this change doesn't reliably deliver.
    search_cursor = 0
    excise_ranges = []
    any_alpha_value_unmasked = False
    for value in extracted.values():
        if not value:
            continue
        idx = text.find(value, search_cursor)
        if idx == -1:
            idx = text.find(value)
        if idx == -1:
            continue
        end = idx + len(value)
        search_cursor = end
        if any(h["start"] < end and idx < h["end"] for h in regex_hits):
            excise_ranges.append((idx, end))
        elif any(c.isalpha() for c in value):
            any_alpha_value_unmasked = True

    if not excise_ranges:
        return text, None
    if not any_alpha_value_unmasked:
        return None, None

    excise_ranges.sort()
    merged = []
    for s, e in excise_ranges:
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))

    candidate_parts = []
    segments = []  # (candidate_start, original_start) per kept chunk
    cursor = 0
    candidate_len = 0
    for s, e in merged:
        if cursor < s:
            chunk = text[cursor:s]
            segments.append((candidate_len, cursor))
            candidate_parts.append(chunk)
            candidate_len += len(chunk)
        cursor = e
    if cursor < len(text):
        chunk = text[cursor:]
        segments.append((candidate_len, cursor))
        candidate_parts.append(chunk)

    return "".join(candidate_parts), segments


def _remap_hit(hit: dict, segments: list[tuple[int, int]]) -> dict:
    """Translates a span reported against `_build_ner_candidate`'s
    (shorter) candidate text back into the ORIGINAL text's coordinate
    space, using `segments` (sorted (candidate_start, original_start)
    pairs). Every character kept in the candidate came from exactly one
    contiguous original chunk, so a hit's start position always falls
    inside exactly one segment; find it and apply that segment's
    constant offset to both start and end."""
    import bisect
    starts = [s[0] for s in segments]
    i = bisect.bisect_right(starts, hit["start"]) - 1
    candidate_start, original_start = segments[i]
    offset = original_start - candidate_start
    return {**hit, "start": hit["start"] + offset, "end": hit["end"] + offset}


def run_evaluation(entries: list[dict], use_ner: bool, use_entropy_gate: bool = False,
                    use_flattened: bool = False, use_field_gate: bool = False,
                    profile: dict | None = None):
    """use_entropy_gate: if True, only run NER on lines with no regex hit at all
    (the 'tiered' strategy described in the chapter). If False, run NER on
    every line regardless of regex results (the naive strategy).
    use_field_gate: field-level variant of use_entropy_gate (see
    _build_ner_candidate above) -- excises only the specific fields a
    regex hit actually covers before calling NER, instead of skipping
    NER for the whole line. Mutually exclusive with use_entropy_gate in
    intent (both are ways of deciding when to call NER); if both are
    somehow passed True, use_field_gate takes precedence per the branch
    order below.
    use_flattened: if True, also run the flattened-username name-dictionary
    layer (src/flattened_names.py), added specifically to address the
    documented 5.9% recall gap on concatenated name tokens.
    profile: optional dict to accumulate timing, added 2026-08-09 while
    chasing the remaining 4.3%-slower-than-naive gap on the field-gated
    condition after the excision rewrite closed most of it. Only
    meaningful with use_field_gate=True. If passed, this function fills
    in profile['candidate_build_seconds'] (total time inside
    _build_ner_candidate, across all gated lines),
    profile['ner_call_seconds'] (total time inside detect.scan_ner calls
    specifically for the field-gated path), profile['ner_calls_made'],
    and profile['ner_calls_skipped'] -- so the two competing hypotheses
    for where the remaining gap lives (fixed per-call spaCy/Presidio
    pipeline overhead that a modest excision barely reduces, vs. this
    function's own extract_fields()/text.find()/splicing bookkeeping
    cost) can be told apart with real measurement instead of continued
    reasoning about which one is more plausible."""
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
                if profile is not None:
                    _t_build_start = time.perf_counter()
                ner_text, segments = _build_ner_candidate(text, log_type, regex_hits)
                if profile is not None:
                    profile["candidate_build_seconds"] = (
                        profile.get("candidate_build_seconds", 0.0)
                        + (time.perf_counter() - _t_build_start)
                    )
                if ner_text is not None:
                    if profile is not None:
                        _t_ner_start = time.perf_counter()
                    raw_hits = detect.scan_ner(ner_text)
                    if profile is not None:
                        profile["ner_call_seconds"] = (
                            profile.get("ner_call_seconds", 0.0)
                            + (time.perf_counter() - _t_ner_start)
                        )
                        profile["ner_calls_made"] = profile.get("ner_calls_made", 0) + 1
                    if segments is not None:
                        raw_hits = [_remap_hit(h, segments) for h in raw_hits]
                    pred += raw_hits
                elif profile is not None:
                    profile["ner_calls_skipped"] = profile.get("ner_calls_skipped", 0) + 1
                # else: every regex-coverable field was excised and nothing
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

    field_gate_profile: dict = {}
    per_type, elapsed = run_evaluation(entries, use_ner=True, use_field_gate=True,
                                        profile=field_gate_profile)
    summarize(per_type, len(entries), elapsed,
              "Regex + NER (field-gated: NER skipped only for regex-covered fields, not whole lines)")
    _build_s = field_gate_profile.get("candidate_build_seconds", 0.0)
    _ner_s = field_gate_profile.get("ner_call_seconds", 0.0)
    _calls_made = field_gate_profile.get("ner_calls_made", 0)
    _calls_skipped = field_gate_profile.get("ner_calls_skipped", 0)
    print(f"  field-gate profile: _build_ner_candidate={_build_s:.2f}s, "
          f"detect.scan_ner={_ner_s:.2f}s, "
          f"NER calls made={_calls_made}, skipped entirely={_calls_skipped}")

    per_type, elapsed = run_evaluation(entries, use_ner=True, use_entropy_gate=False)
    summarize(per_type, len(entries), elapsed, "Regex + NER (naive: NER on every line)")

    per_type, elapsed = run_evaluation(entries, use_ner=True, use_entropy_gate=False,
                                        use_flattened=True)
    summarize(per_type, len(entries), elapsed,
              "Regex + NER (naive) + flattened-username name dictionary")
