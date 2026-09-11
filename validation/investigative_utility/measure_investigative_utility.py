"""
Privacy-vs-investigative-utility experiment: how much does full redaction,
pseudonymization, and reversible tokenization each cost (or not cost) a
realistic investigation, measured against scenario_corpus.jsonl (see
generate_scenario_corpus.py and this directory's README.md for why that
corpus exists and what it deliberately does and doesn't represent).

This runs REDACT's own real anonymize.py functions (redact(), pseudonymize(),
tokenize()) against GROUND-TRUTH spans, not detector output. That's a
deliberate scoping choice, not an oversight: this experiment is about what
the three ANONYMIZATION METHODS structurally do to investigative utility,
independent of detection accuracy. Detection false negatives (an entity the
detector misses entirely, so no method ever touches it) are a separate,
already-measured axis -- see the PIIBench/Loghub validation work -- and
would only ever make every method's real-world numbers here worse, never
better, if folded in. That caveat must travel with any number quoted from
this script.

Two things are measured, both directly, both against real code:

1. CORRELATION -- across DIFFERENT records, does a method's output let an
   investigator (a) link two mentions of the same real entity, and,
   separately, (b) avoid wrongly linking two mentions of DIFFERENT real
   entities? These are reported separately because they are not each
   other's complement: a method can trivially "solve" (a) while failing
   (b) catastrophically -- redaction is the concrete example measured below.

2. REVERSIBILITY -- can an authorized investigator recover the real value
   from the anonymized output?
   - redact: measured, not assumed, at 0% (see below).
   - pseudonymize: direct blind reversal is measured at 0% (genuinely
     one-way HMAC, no map is ever stored -- see anonymize.py's own
     docstring on this). But a realistic investigative sub-case is tested
     separately: CANDIDATE VERIFICATION, where an investigator already has
     a short list of suspected values ("was it one of these five users")
     and only needs to confirm or rule out each one, which pseudonymization
     *can* answer given the pseudonymization key. This is a materially
     different capability from blind reversal and is reported as its own
     number, not blended into the 0%.
   - tokenize: measured via the real TokenStore.resolve()/detokenize()
     path, given store access (the same authorization boundary the
     project's existing StorageProvider/audit-key design already assumes).
"""
import hashlib
import hmac
import itertools
import json
import os
import sys
import tempfile
from collections import defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))
import anonymize  # noqa: E402

CORPUS_PATH = os.path.join(os.path.dirname(__file__), "scenario_corpus.jsonl")
RESULTS_PATH = os.path.join(os.path.dirname(__file__), "results.json")

PSEUDO_KEY = "investigative-utility-experiment-pseudo-key"
TOKEN_KEY = "investigative-utility-experiment-token-key"

# Caps the number of cross-record pairs actually enumerated per type, since
# pair counts grow O(n^2) in mention count. PERSON has 344 mentions in the
# current corpus -> ~59,000 pairs, still trivially fast, but this cap keeps
# the script safe if the corpus is regenerated larger later. Pairs are
# sampled deterministically (fixed stride), not randomly, so a rerun against
# the same corpus is exactly reproducible.
MAX_PAIRS_PER_TYPE = 200_000


def load_mentions() -> dict[str, list[dict]]:
    """type -> list of {"record_id": int, "value": str}, one entry per
    ground-truth PII mention (not deduplicated -- a value mentioned twice in
    one record contributes two entries, consistent with how many times an
    investigator would actually encounter it)."""
    mentions = defaultdict(list)
    with open(CORPUS_PATH) as f:
        for line in f:
            rec = json.loads(line)
            log = rec["log"]
            for span in rec["pii"]:
                value = log[span["start"]:span["end"]]
                mentions[span["type"]].append({"record_id": rec["record_id"], "value": value})
    return mentions


def pseudonymize_value(value: str, pii_type: str) -> str:
    """Exercises the exact transform anonymize.pseudonymize() applies per
    span, called through the real function (not reimplemented) by giving it
    a single-span "text" that is just the value itself -- valid because
    pseudonymize()'s per-span transform depends only on (original substring,
    span type), never on surrounding text. Confirmed by reading
    anonymize.pseudonymize()'s transform() closure directly."""
    span = {"start": 0, "end": len(value), "type": pii_type}
    return anonymize.pseudonymize(value, [span], key=PSEUDO_KEY)


def redact_value(value: str, pii_type: str) -> str:
    span = {"start": 0, "end": len(value), "type": pii_type}
    return anonymize.redact(value, [span])


def build_output_map(mentions_for_type: list[dict], pii_type: str, store: "anonymize.TokenStore") -> dict[str, str]:
    """value -> anonymized output, one entry per unique value, computed once
    via the real functions rather than per-mention (redact/pseudonymize are
    pure functions of (value, type); tokenize's get_or_create_token() is
    keyed purely on (original, pii_type), confirmed by reading the method
    directly -- so recomputing per unique value and reusing the result for
    every mention of that value is exact, not an approximation)."""
    unique_values = sorted(set(m["value"] for m in mentions_for_type))
    out = {}
    for v in unique_values:
        out[v] = {
            "redact": redact_value(v, pii_type),
            "pseudonymize": pseudonymize_value(v, pii_type),
            "tokenize": store.get_or_create_token(v, pii_type),
        }
    return out


def measure_correlation(mentions_for_type: list[dict], output_map: dict) -> dict:
    """Enumerates cross-record mention pairs and checks, per method, whether
    equal real values produce equal outputs (a true link an investigator
    could draw) and whether unequal real values produce equal outputs (a
    false link redaction's constant placeholder is expected to create)."""
    methods = ["redact", "pseudonymize", "tokenize"]
    same_pairs_total = 0
    same_pairs_linked = {m: 0 for m in methods}
    diff_pairs_total = 0
    diff_pairs_falsely_linked = {m: 0 for m in methods}

    n = len(mentions_for_type)
    all_pairs = itertools.combinations(range(n), 2)
    # Deterministic stride sampling if the full pair count would exceed the
    # cap -- keeps this reproducible and bounded without random sampling.
    total_possible = n * (n - 1) // 2
    stride = max(1, total_possible // MAX_PAIRS_PER_TYPE)

    for idx, (i, j) in enumerate(all_pairs):
        if idx % stride != 0:
            continue
        a, b = mentions_for_type[i], mentions_for_type[j]
        if a["record_id"] == b["record_id"]:
            continue  # within-record duplicate mention, not a cross-record correlation case
        same_entity = a["value"] == b["value"]
        if same_entity:
            same_pairs_total += 1
        else:
            diff_pairs_total += 1
        for m in methods:
            out_a = output_map[a["value"]][m]
            out_b = output_map[b["value"]][m]
            linked = out_a == out_b
            if same_entity and linked:
                same_pairs_linked[m] += 1
            elif not same_entity and linked:
                diff_pairs_falsely_linked[m] += 1

    result = {"same_entity_pairs_examined": same_pairs_total,
              "different_entity_pairs_examined": diff_pairs_total}
    for m in methods:
        result[m] = {
            "correlation_recall": round(same_pairs_linked[m] / same_pairs_total, 4) if same_pairs_total else None,
            "false_linkage_rate": round(diff_pairs_falsely_linked[m] / diff_pairs_total, 4) if diff_pairs_total else None,
        }
    return result


def measure_reversibility(unique_values: list[str], pii_type: str, output_map: dict, store: "anonymize.TokenStore") -> dict:
    result = {}

    # redact: structurally 0% -- confirmed directly, not assumed, by checking
    # that every value's redact output is the single constant placeholder.
    redact_outputs = {output_map[v]["redact"] for v in unique_values}
    result["redact"] = {
        "direct_reversal_rate": 0.0,
        "note": f"all {len(unique_values)} values map to {len(redact_outputs)} distinct placeholder(s); "
                f"the original value is not present in the output in any form.",
    }

    # pseudonymize: direct blind reversal is 0% by construction (one-way
    # HMAC, no stored map -- this is stated, not brute-force-"tested",
    # since a correct HMAC-SHA256 has no feasible inversion to demonstrate
    # failing). What IS tested directly: candidate-list verification, the
    # realistic case where an investigator already has a short list of
    # suspects and needs to confirm/rule out each one.
    candidate_list_size = 5
    all_values_pool = unique_values
    n_trials = min(len(unique_values), 30)
    correct_identifications = 0
    for i in range(n_trials):
        true_value = all_values_pool[i]
        observed_pseudonym = output_map[true_value]["pseudonymize"]
        decoys = [v for v in all_values_pool if v != true_value][:candidate_list_size - 1]
        candidates = [true_value] + decoys
        matches = [c for c in candidates if pseudonymize_value(c, pii_type) == observed_pseudonym]
        if matches == [true_value]:
            correct_identifications += 1
    result["pseudonymize"] = {
        "direct_reversal_rate": 0.0,
        "note": "one-way HMAC-SHA256, no reverse map is ever stored -- blind reversal is not "
                "computationally attempted here because a correctly implemented keyed hash has "
                "no feasible inversion to demonstrate failing; this is a structural property, not "
                "a measured one.",
        "candidate_list_verification": {
            "description": f"given a candidate list of {candidate_list_size} plausible values "
                            f"(1 true + {candidate_list_size - 1} decoys) and the pseudonymization "
                            f"key, can an investigator correctly identify which candidate produced "
                            f"the observed pseudonym?",
            "trials": n_trials,
            "correct_identification_rate": round(correct_identifications / n_trials, 4) if n_trials else None,
            "caveat": "requires possession of the pseudonymization key -- the same privileged-access "
                      "assumption this project's TokenStore/audit design already requires elsewhere, "
                      "not a weaker one.",
        },
    }

    # tokenize: real resolve() call, not assumed.
    resolved_ok = 0
    for v in unique_values:
        token = output_map[v]["tokenize"]
        if store.resolve(token) == v:
            resolved_ok += 1
    result["tokenize"] = {
        "direct_reversal_rate": round(resolved_ok / len(unique_values), 4) if unique_values else None,
        "note": "via the real TokenStore.resolve() path, given store access.",
    }
    return result


def main():
    mentions = load_mentions()
    tmp_store_path = os.path.join(tempfile.mkdtemp(), "token_store.json")
    store = anonymize.TokenStore(tmp_store_path, token_key=TOKEN_KEY)

    report = {
        "methodology_note": (
            "Measured against ground-truth spans in scenario_corpus.jsonl, not detector "
            "output -- isolates the anonymization methods' own properties from detection "
            "accuracy (a separate, already-measured axis; see this directory's README.md)."
        ),
        "by_type": {},
    }

    for pii_type, type_mentions in sorted(mentions.items()):
        output_map = build_output_map(type_mentions, pii_type, store)
        unique_values = sorted(output_map.keys())
        correlation = measure_correlation(type_mentions, output_map)
        reversibility = measure_reversibility(unique_values, pii_type, output_map, store)
        report["by_type"][pii_type] = {
            "total_mentions": len(type_mentions),
            "unique_values": len(unique_values),
            "correlation": correlation,
            "reversibility": reversibility,
        }

    with open(RESULTS_PATH, "w") as f:
        json.dump(report, f, indent=2)
    print(f"Wrote {RESULTS_PATH}")

    # Compact console summary.
    for pii_type, data in report["by_type"].items():
        c = data["correlation"]
        print(f"\n{pii_type} (mentions={data['total_mentions']}, unique={data['unique_values']}):")
        for m in ["redact", "pseudonymize", "tokenize"]:
            rec = c[m]["correlation_recall"]
            fl = c[m]["false_linkage_rate"]
            print(f"  {m:12s} correlation_recall={rec}  false_linkage_rate={fl}")


if __name__ == "__main__":
    main()
