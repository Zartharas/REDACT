"""
CREDIT_CARD spaced_groups/dashed_groups evaded exactly 119/237 (50.2%) --
the only technique in this experiment that landed strictly between 0%
and 100%, which is worth root-causing rather than reporting as an
unexplained number. Working hypothesis (NOT yet confirmed): Presidio
ships its own built-in CREDIT_CARD recognizer, independent of REDACT's
own REGEX_PATTERNS["CREDIT_CARD"], and that recognizer's pattern may
tolerate space/dash-separated 4-digit groups for SOME card lengths (a
16-digit card splits evenly into four groups of 4; a 15-digit Amex-style
number does not, since Amex's real-world convention is 4-6-5, not 4-4-4-3
-- REDACT's synthetic corpus generator may not follow that convention,
which would itself be a separate, second finding). Breaks results down by
original card length to test that hypothesis directly against the real
model, rather than asserting it.

Run: python3 validation/adversarial_evasion/diagnose_credit_card_partial_evasion.py
"""
import json
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))
import detect  # noqa: E402

VARIANTS_PATH = os.path.join(os.path.dirname(__file__), "evasion_corpus.jsonl")


def main():
    recs = [json.loads(l) for l in open(VARIANTS_PATH)]
    cc = [r for r in recs if r["pii_type"] == "CREDIT_CARD"]

    for technique in ("spaced_groups", "dashed_groups"):
        by_len_total = Counter()
        by_len_evaded = Counter()
        examples_evaded = []
        examples_caught = []
        for r in cc:
            if r["technique"] != technique:
                continue
            hits = detect.detect_all_field_gated(r["log"], log_type=r["log_type"])
            evaded = not any(
                h["start"] < r["span"]["end"] and h["end"] > r["span"]["start"]
                and h["type"] == "CREDIT_CARD"
                for h in hits
            )
            L = len(r["original_value"])
            by_len_total[L] += 1
            if evaded:
                by_len_evaded[L] += 1
                if len(examples_evaded) < 2:
                    examples_evaded.append((L, r["variant_value"]))
            else:
                if len(examples_caught) < 2:
                    examples_caught.append((L, r["variant_value"]))

        print(f"\n=== {technique} ===")
        print(f"{'digit_len':<12}{'evaded':<10}{'total':<10}{'evasion_rate'}")
        for L in sorted(by_len_total):
            t, e = by_len_total[L], by_len_evaded[L]
            print(f"{L:<12}{e:<10}{t:<10}{e/t:.1%}")
        print("Examples evaded:", examples_evaded)
        print("Examples caught:", examples_caught)


if __name__ == "__main__":
    main()
