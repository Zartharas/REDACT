"""
One-off script to re-verify the README's headline "spaced vs. flattened
PERSON recall" split against the post-Bug-9-fix corpus, using the exact
same regex+NER "naive" detection condition evaluate.py's main() already
measures (no flattened-username layer included here — that layer's own
numbers are already verified separately in the Layer 4 section).

Run from the repo root: python validation/breakdown_person_format.py
"""
import sys
import json

sys.path.insert(0, "src")
import detect  # noqa: E402


def overlaps(a, b):
    return a["start"] < b["end"] and b["start"] < a["end"]


def main():
    entries = [json.loads(l) for l in open("data/synthetic_logs.jsonl")]

    flat_gold = spaced_gold = 0
    flat_tp = spaced_tp = 0

    for e in entries:
        text = e["log"]
        pred = detect.scan_regex(text) + detect.scan_ner(text)
        gold_person = [g for g in e["pii"] if g["type"] == "PERSON"]
        for g in gold_person:
            val = text[g["start"]:g["end"]]
            is_flat = " " not in val
            if is_flat:
                flat_gold += 1
            else:
                spaced_gold += 1
            matched = any(p["type"] == "PERSON" and overlaps(p, g) for p in pred)
            if matched:
                if is_flat:
                    flat_tp += 1
                else:
                    spaced_tp += 1

    print(f"Spaced-format PERSON recall (regex+NER, naive, no flattened layer): "
          f"{spaced_tp}/{spaced_gold} ({spaced_tp/spaced_gold:.1%})")
    print(f"Flattened-format PERSON recall (regex+NER, naive, no flattened layer): "
          f"{flat_tp}/{flat_gold} ({flat_tp/flat_gold:.1%})")


if __name__ == "__main__":
    main()
