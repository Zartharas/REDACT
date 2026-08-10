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
# fields.py is no longer imported directly here -- detect.build_ner_candidate
# (aliased below as _build_ner_candidate) imports it lazily itself, now that
# the field-gating logic lives in detect.py rather than this file. See the
# comment above that alias for the full move history.


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


# _build_ner_candidate / _remap_hit used to be defined here directly.
# Moved to detect.py on 2026-08-09 (build_ner_candidate / remap_hit) so
# src/service.py and src/pipeline.py can call the field-gated detection
# path in production without importing this research/evaluation script --
# see detect.py's own comment above detect_all_field_gated() for the full
# reasoning, and detect.build_ner_candidate's docstring for this
# function's full version history (masking -> excision -> profiling-fix).
# Aliased under the original names here so the rest of this file (and the
# existing test suite, tests/test_field_level_gate.py) doesn't need to
# change on top of the move.
_build_ner_candidate = detect.build_ner_candidate
_remap_hit = detect.remap_hit


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
    condition after the excision rewrite closed most of it. Works with
    ANY use_ner configuration (field-gated, tiered, naive), not just
    field-gated -- the point is to let the SAME instrumentation run
    against different conditions for a true apples-to-apples comparison.
    Fills in whichever of these apply to the lines actually processed:
      - profile['candidate_build_seconds'], profile['ner_call_seconds'],
        profile['ner_calls_made'], profile['ner_calls_skipped']: the
        field-gated candidate path specifically (use_field_gate=True,
        line has a regex hit) -- ner_call_seconds here times scan_ner()
        on the EXCISED candidate text.
      - profile['regex_hit_ner_seconds'] / ['regex_hit_ner_calls']: time
        spent in scan_ner() on the FULL, unmodified text, for lines that
        DO have a regex hit -- populated when running naive/tiered
        (every regex-hit line goes through here), letting a naive run's
        cost on this exact subset of lines be compared directly against
        the field-gated candidate path's cost on the identical subset.
      - profile['no_regex_hit_ner_seconds'] / ['no_regex_hit_ner_calls']:
        time spent in scan_ner() on the full text for lines with NO
        regex hit at all -- populated by every configuration (including
        field-gated, since a line with nothing for field-gating to act
        on falls through to the same plain scan_ner(text) call naive
        would make), a built-in control: this number SHOULD come out
        close to identical across configurations, since none of them
        treat a no-regex-hit line any differently.
    An earlier version of this profiling only measured the field-gated
    candidate path, silently missing the field-gated condition's own
    no-regex-hit lines entirely (5,394 of 10,000 on this project's
    corpus, more than half the run) -- fixed the same day, before this
    gave a real answer to the question it was built to answer."""
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
                # Engineering upgrade, 2026-08-09: this branch is reached
                # by every line in the naive/tiered conditions, AND by
                # the field-gated condition's lines that have NO regex
                # hit at all (use_field_gate's own condition above is
                # `and regex_hits`, so an empty regex_hits list falls
                # through here). The first profiling pass measured only
                # the field-gated candidate path (profile['ner_call_seconds']
                # above) and completely missed this branch -- on the full
                # corpus, that meant 5,394 of 10,000 lines' NER cost was
                # invisible to the measurement, more than half the run.
                # Splitting by whether THIS line had a regex hit (not by
                # which strategy is active) is what makes an apples-to-
                # apples comparison possible: running this same profiling
                # against the naive condition gives its true cost on the
                # EXACT SAME subset of lines field-gated's candidate path
                # processes, instead of comparing against naive's
                # average across all 10,000 lines (a different, easier
                # population -- lines with no regex hit are typically
                # shorter/simpler).
                if profile is not None:
                    _t_ner_start = time.perf_counter()
                raw_hits = detect.scan_ner(text)
                if profile is not None:
                    _bucket = "regex_hit" if regex_hits else "no_regex_hit"
                    profile[f"{_bucket}_ner_seconds"] = (
                        profile.get(f"{_bucket}_ner_seconds", 0.0)
                        + (time.perf_counter() - _t_ner_start)
                    )
                    profile[f"{_bucket}_ner_calls"] = profile.get(f"{_bucket}_ner_calls", 0) + 1
                pred += raw_hits

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
    _no_hit_s = field_gate_profile.get("no_regex_hit_ner_seconds", 0.0)
    _no_hit_calls = field_gate_profile.get("no_regex_hit_ner_calls", 0)
    print(f"  field-gate profile: _build_ner_candidate={_build_s:.2f}s, "
          f"detect.scan_ner(candidate)={_ner_s:.2f}s over {_calls_made} calls, "
          f"skipped entirely={_calls_skipped}, "
          f"no-regex-hit lines (unaffected by gating)={_no_hit_s:.2f}s over {_no_hit_calls} calls")

    naive_profile: dict = {}
    per_type, elapsed = run_evaluation(entries, use_ner=True, use_entropy_gate=False,
                                        profile=naive_profile)
    summarize(per_type, len(entries), elapsed, "Regex + NER (naive: NER on every line)")
    _naive_hit_s = naive_profile.get("regex_hit_ner_seconds", 0.0)
    _naive_hit_calls = naive_profile.get("regex_hit_ner_calls", 0)
    _naive_no_hit_s = naive_profile.get("no_regex_hit_ner_seconds", 0.0)
    _naive_no_hit_calls = naive_profile.get("no_regex_hit_ner_calls", 0)
    print(f"  naive profile: detect.scan_ner(full text)={_naive_hit_s:.2f}s over "
          f"{_naive_hit_calls} regex-hit-line calls, "
          f"no-regex-hit lines={_naive_no_hit_s:.2f}s over {_naive_no_hit_calls} calls")
    # The controlled comparison this profiling exists to make: SAME
    # subset of lines (has a regex hit), field-gated's candidate cost
    # vs. naive's full-text cost. This is the number that actually
    # settles whether excision helps, isolated from the no-regex-hit
    # lines both conditions handle identically anyway.
    if _calls_made and _naive_hit_calls:
        print(f"  controlled comparison, regex-hit lines only "
              f"({_calls_made} lines): field-gated "
              f"{(_build_s + _ner_s) / _calls_made * 1000:.2f}ms/line "
              f"vs. naive {_naive_hit_s / _naive_hit_calls * 1000:.2f}ms/line")

    per_type, elapsed = run_evaluation(entries, use_ner=True, use_entropy_gate=False,
                                        use_flattened=True)
    summarize(per_type, len(entries), elapsed,
              "Regex + NER (naive) + flattened-username name dictionary")
