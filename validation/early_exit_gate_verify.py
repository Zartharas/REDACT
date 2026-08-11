"""
Verification for detect.py's _could_contain_ner_entity() early-exit gate
(added 2026-08-11, BUGS_AND_FIXES.md "Engineering upgrade 3").

Cannot run this against the real spaCy/Presidio model in this environment
(no network access to fetch it, same standing constraint noted throughout
this project). What CAN be verified without the model: whether the gate
would ever skip a line that the canonical corpus's own GOLD PII SPANS say
contains a real PII entity of one of scan_ner's six canonical types
(PERSON, EMAIL, IP, SSN, CREDIT_CARD, MRN). If the gate is truly safe by
construction (see detect.py's own comment on this), this count must be
zero regardless of what the model would have found -- this script checks
that directly against ground truth rather than just trusting the
argument.

Also reports the real skip rate on this corpus: how many of the 10,000
lines this gate would actually cause scan_ner to return [] immediately
for, without calling the model at all. That number is a real, measured
input to deciding whether this is worth keeping; the wall-clock time it
would actually save is NOT measured here (needs a live model to time --
see detect.py's own comment on that gap).

Run: python3 validation/early_exit_gate_verify.py
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import detect  # noqa: E402

CANONICAL_NER_TYPES = {"PERSON", "EMAIL", "IP", "SSN", "CREDIT_CARD", "MRN"}


def main():
    corpus_path = Path(__file__).resolve().parent.parent / "data" / "synthetic_logs.jsonl"
    entries = []
    with open(corpus_path) as f:
        for raw_line in f:
            entries.append(json.loads(raw_line))

    print(f"Loaded {len(entries)} lines from {corpus_path.name}")

    total = len(entries)
    would_skip = 0
    false_skips = []  # lines the gate would skip that DO have a relevant gold span

    for i, entry in enumerate(entries):
        text = entry["log"]
        gold_spans = entry.get("pii", [])
        gate_says_skip = not detect._could_contain_ner_entity(text)

        if gate_says_skip:
            would_skip += 1
            relevant_gold = [g for g in gold_spans if g["type"] in CANONICAL_NER_TYPES]
            if relevant_gold:
                false_skips.append((i, text, relevant_gold))

    print(f"\nGate would skip scan_ner() entirely for {would_skip}/{total} lines "
          f"({would_skip / total:.1%}) on this corpus.")

    print(f"\nLines the gate would skip that have a real gold PII span of a "
          f"canonical NER type: {len(false_skips)}")
    if false_skips:
        print("UNSAFE -- gate would cause real recall loss. Details:")
        for i, text, gold in false_skips[:10]:
            print(f"  line {i}: gold={gold}")
            print(f"    text: {text!r}")
        print("\nSAFETY CHECK: FAIL")
    else:
        print("SAFETY CHECK: PASS -- gate never skips a line the ground truth "
              "says contains a relevant PII span, across the full corpus.")

    if would_skip == 0:
        print(
            "\nNOTE: the gate never actually fired on this corpus (0% skip "
            "rate) -- every line here has some digit run somewhere (a "
            "timestamp, a JSON field, an ID), which is enough to keep the "
            "gate's structural guarantee from applying. This is an honest, "
            "not-hidden finding: this specific synthetic corpus doesn't "
            "contain the 'pure heartbeat/health-check line' case the "
            "optimization actually targets. The check below demonstrates the "
            "mechanism DOES work on that case, separately from the "
            "safety-on-real-corpus check above."
        )
        print("\n--- Demonstration against realistic heartbeat/health-check-style lines ---")
        example_lines = [
            "heartbeat ok",
            "PING",
            "status: alive",
            "health check passed",
            "keepalive",
        ]
        for line in example_lines:
            skipped = not detect._could_contain_ner_entity(line)
            print(f"  {line!r}: {'SKIPPED (gate fires)' if skipped else 'not skipped'}")


if __name__ == "__main__":
    main()
