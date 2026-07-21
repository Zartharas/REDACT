import json
import sys

sys.path.insert(0, "src")
import detect  # noqa: E402


def overlaps(a, b):
    return a["start"] < b["end"] and b["start"] < a["end"]


def main(path: str):
    with open(path) as f:
        entries = [json.loads(line) for line in f]

    total_entropy_flags = 0
    flags_overlapping_gold = 0
    flags_on_clean_lines = 0
    clean_lines_with_a_flag = 0
    total_clean_lines = 0
    example_false_alarms = []

    for e in entries:
        text = e["log"]
        gold = e["pii"]
        entropy_hits = detect.scan_entropy(text)
        total_entropy_flags += len(entropy_hits)

        if not gold:
            total_clean_lines += 1
            if entropy_hits:
                clean_lines_with_a_flag += 1
            flags_on_clean_lines += len(entropy_hits)
            for h in entropy_hits[:1]:
                if len(example_false_alarms) < 8:
                    example_false_alarms.append(text[h["start"]:h["end"]])
            continue

        for h in entropy_hits:
            if any(overlaps(h, g) for g in gold):
                flags_overlapping_gold += 1

    print(f"Total entries: {len(entries)}  (clean: {total_clean_lines})")
    print(f"Total entropy flags raised: {total_entropy_flags}")
    print(f"Entropy flags overlapping an actual gold PII span: {flags_overlapping_gold}")
    print(f"Entropy flags on entries with NO PII at all (pure false alarms): {flags_on_clean_lines}")
    print(f"Clean entries with at least one false alarm: {clean_lines_with_a_flag}/{total_clean_lines} "
          f"({clean_lines_with_a_flag/total_clean_lines:.1%})")
    print(f"\nExample tokens the entropy layer flagged on entries with no PII present:")
    for ex in example_false_alarms:
        print(f"  {ex!r}  (entropy={detect.shannon_entropy(ex):.2f})")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "data/synthetic_logs.jsonl")
