"""
Root-cause diagnostic for a real regression found running
inject_and_evaluate.py's field-gated (header-stripped) condition against
real Loghub data, 2026-08-09:

    OpenSSH: RAW (0% field-gate engagement, identical to naive) P=0.974,
             FP=49 -> STRIPPED (53.2% engaged) P=0.778, FP=523. TP +1.
    Linux:   RAW (0% engagement, identical to naive) P=0.920, FP=122 ->
             STRIPPED (30.9% engaged) P=0.797, FP=357. TP +0.

Precision drop scales with how often field-gating actually engages;
recall gain is ~0 in both cases. That means: on real, structurally
diverse syslog text (as opposed to this project's own 3 fixed synthetic
templates), wherever field-gating actually excises something, it is
manufacturing new false positives, not finding new true positives --
the OPPOSITE of what the synthetic-corpus evaluation found (field-gated
matching or exceeding naive's precision there).

This directly contradicts the "field-gated is a strictly better choice
than naive" conclusion in README.md / detect.build_ner_candidate's
docstring, which was only ever validated against synthetic data. That
claim needs to be corrected once this is root-caused, not left standing.

This script isolates and prints exactly which predictions are NEW in the
field-gated (header-stripped) condition versus naive -- spans field-gated
predicted that naive did not -- alongside the EXACT candidate text
build_ner_candidate actually sent to NER for that line, and the original
line for comparison. The goal is to see directly whether this is:
  (a) excision creating an accidental new adjacency NER misreads (the
      risk build_ner_candidate's own docstring already discloses, e.g.
      two spliced fragments reading as a single new phrase), or
  (b) removing surrounding context (the excised IP/uid/etc.) making
      spaCy less confident in classifying nearby tokens correctly, or
  (c) something else not yet hypothesized.

Needs the real spaCy/Presidio model and the downloaded Loghub files
(same as inject_and_evaluate.py). Run from validation/real_data/:

    python diagnose_field_gate_false_positives.py --dataset Linux
    python diagnose_field_gate_false_positives.py --dataset OpenSSH
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'src'))
import detect  # noqa: E402
import fields  # noqa: E402

from inject_and_evaluate import (  # noqa: E402
    USER_FIELD_DATASETS, build_user_field_corpus, strip_syslog_header,
)


def overlaps(a, b) -> bool:
    return a['start'] < b['end'] and b['start'] < a['end']


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--dataset', choices=list(USER_FIELD_DATASETS), default='Linux')
    parser.add_argument('--max-examples', type=int, default=25)
    args = parser.parse_args()

    pattern = USER_FIELD_DATASETS[args.dataset]
    entries, injected, flat_n, spaced_n = build_user_field_corpus(args.dataset, pattern)
    print(f"{args.dataset}: {len(entries)} lines, {injected} PERSON injected\n")

    shown = 0
    total_new_fp = 0
    for e in entries:
        text = e['log']
        gold = e['pii']

        # naive: full original line
        naive_preds = detect.scan_regex(text) + detect.scan_ner(text)

        # field-gated, header-stripped (the condition that regressed)
        prefix_len, body = strip_syslog_header(text)
        extracted = fields.extract_fields('syslog', body) if prefix_len else {}
        regex_hits_body = detect.scan_regex(body)
        candidate_text, segments = (
            detect.build_ner_candidate(body, 'syslog', regex_hits_body)
            if regex_hits_body else (body, None)
        )
        fg_hits = detect.detect_all_field_gated(body, log_type='syslog', use_flattened=False)
        fg_preds = [{**h, 'start': h['start'] + prefix_len, 'end': h['end'] + prefix_len}
                    for h in fg_hits]
        fg_preds = [p for p in fg_preds if p['type'] != 'HIGH_ENTROPY']

        # Which field-gated predictions are genuinely NEW (no overlapping
        # same-type prediction in naive's own output for this line)?
        new_fp = []
        for p in fg_preds:
            if any(g['type'] == p['type'] and overlaps(g, p) for g in gold):
                continue  # matches gold -- a real TP, not what we're hunting for
            if any(n['type'] == p['type'] and overlaps(
                    {'start': n['start'], 'end': n['end']},
                    {'start': p['start'], 'end': p['end']}) for n in naive_preds):
                continue  # naive already predicted this too -- not NEW
            new_fp.append(p)

        if new_fp and shown < args.max_examples:
            shown += 1
            print(f"--- example {shown} ---")
            print(f"  original line:     {text!r}")
            if prefix_len:
                print(f"  stripped body:     {body!r}")
            print(f"  NER candidate:     {candidate_text!r}")
            print(f"  extracted fields:  {extracted}")
            for p in new_fp:
                span_text = text[p['start']:p['end']]
                print(f"  NEW FALSE POSITIVE: type={p['type']} text={span_text!r} "
                      f"method={p.get('method')} score={p.get('score')}")
            print()
        total_new_fp += len(new_fp)

    print(f"\nTotal NEW false positives (field-gated header-stripped vs. naive, "
          f"same lines): {total_new_fp}")
    print(f"Shown {shown} example(s) above (--max-examples {args.max_examples})")


if __name__ == '__main__':
    main()
