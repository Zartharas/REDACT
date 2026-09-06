"""
Root-causes the 140 residual false positives left in the Zookeeper
real-IP condition after Engineering upgrade 15's Luhn filter (see
BUGS_AND_FIXES.md) cut it down from 1,557. Recall stayed 1.000 the whole
time -- this is purely about what ELSE is getting flagged on a dataset
whose ground truth is IP-only.

HYPOTHESIS, NOT yet confirmed against the actual data (that's what this
script is for): inject_and_evaluate.py's build_ip_only_corpus() naive
path runs `detect.scan_regex(text) + detect.scan_ner(text)` -- i.e. the
full regex AND Presidio NER ensemble, against ground truth that only
ever contains IP spans. Two live hypotheses:
  1. NER misfires: Zookeeper's log text is dense with camelCase Java
     class/method names (QuorumPeer, FastLeaderElection, SendWorker,
     NIOServerCnxnFactory, WorkerReceiver) that look nothing like natural
     language -- exactly the shape that trips up a general-purpose NER
     model into a false PERSON (or other entity) prediction.
  2. A second, rarer regex collision: a different incidental digit run
     (not the already-fixed 188978561024 thread ID) that happens to also
     pass the Luhn check, or matches SSN's \\d{3}-\\d{2}-\\d{4} shape, or
     EMAIL's `@`-anchored pattern against something with an `@` in it
     (Zookeeper log lines DO contain `ClassName@lineNumber` tokens, e.g.
     "QuorumCnxManager$Listener@493" -- EMAIL's regex requires a
     `.`-containing domain after the `@`, so this specific shape should
     NOT match, but worth confirming rather than assuming).

This script runs the real ensemble (spaCy/Presidio required, same as
production) against Zookeeper_2k.log, buckets every false positive by
(type, matched text), and prints the most common ones -- so a fix, if
one is warranted, targets the actual dominant cause rather than a
plausible-sounding guess.

Run from validation/real_data/ (same convention as
diagnose_cloudtrail_false_positives.py):
    python3 diagnose_zookeeper_false_positives.py
"""
import os
import re
import sys
from collections import Counter

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'src'))
import detect  # noqa: E402

ZOOKEEPER_PATH = os.path.join('datasets', 'Zookeeper_2k.log')
IP_RE = re.compile(r'\b(?:\d{1,3}\.){3}\d{1,3}\b')


def main():
    if not os.path.exists(ZOOKEEPER_PATH):
        print(f"{ZOOKEEPER_PATH} not found -- run download_loghub.sh first.")
        return

    with open(ZOOKEEPER_PATH, encoding='utf-8', errors='replace') as f:
        lines = [l.rstrip('\r\n') for l in f]

    fp_counter = Counter()
    fp_type_counter = Counter()
    total_fp = 0
    example_lines = {}

    for line in lines:
        gold = [(m.start(), m.end()) for m in IP_RE.finditer(line)]
        preds = detect.scan_regex(line) + detect.scan_ner(line)
        # Same de-dup logic as inject_and_evaluate.py's evaluate(): collapse
        # overlapping same-type hits from different layers before scoring.
        dedup = []
        for p in preds:
            if not any(p['type'] == d['type'] and p['start'] < d['end'] and d['start'] < p['end']
                       for d in dedup):
                dedup.append(p)
        matched_gold = set()
        for p in dedup:
            hit = False
            for i, (gs, ge) in enumerate(gold):
                if i in matched_gold:
                    continue
                if p['type'] == 'IP' and p['start'] < ge and gs < p['end']:
                    matched_gold.add(i)
                    hit = True
                    break
            if not hit:
                total_fp += 1
                text = line[p['start']:p['end']]
                fp_type_counter[p['type']] += 1
                fp_counter[(p['type'], text)] += 1
                if (p['type'], text) not in example_lines:
                    example_lines[(p['type'], text)] = line

    print(f"Total lines: {len(lines)}")
    print(f"Total false positives (matches inject_and_evaluate.py's methodology): {total_fp}")
    print()
    print("False positives by TYPE:")
    for ptype, count in fp_type_counter.most_common():
        print(f"  {ptype}: {count}")
    print()
    print("Top 20 most common (type, matched text) false-positive pairs:")
    for (ptype, text), count in fp_counter.most_common(20):
        print(f"  [{count:4d}x] {ptype}: {text!r}")
        if count <= 3:
            print(f"           e.g. line: {example_lines[(ptype, text)][:160]}")
    print()
    print("If one TYPE dominates, that's the layer to fix (regex pattern for that")
    print("type, or a targeted NER exclusion). If it's spread across many distinct")
    print("(type, text) pairs with low counts each, that's more consistent with a")
    print("diffuse NER weakness on this log format than a single fixable collision.")


if __name__ == '__main__':
    main()
