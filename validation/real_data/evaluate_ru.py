"""
Evaluation harness for the Russian PII pilot sample
(validation/real_data/datasets/OpenRU_PII_raw.jsonl), scoring REDACT's
detection against gold spans reconstructed from a real-context/
synthetic-value benchmark. See prepare_ru_dataset.py's "WHAT THIS DATASET
IS" section for the provenance mix (real production-log/query carrier
text, synthetic PII values, synthetic document templates, hand-filtered
hard negatives) -- a different mix from both MEDDOCAN (real carrier + real
PII) and OpenPII French (fully synthetic templates).

Mirrors evaluate_meddocan.py's/evaluate_fr.py's matching logic exactly
(same overlap-based TP/FP/FN rule, same greedy one-gold-per-prediction
matching, same cross-layer dedup) so results are directly comparable in
METHODOLOGY. Sample size (26 documents, curated for entity-type coverage
rather than randomly drawn) means results here are directional, not a
confident final read -- see prepare_ru_dataset.py's "COVERAGE" section.

TYPE MAPPING: see validation/real_data/RU_TYPE_MAPPING.md. 6 of the 21
pii_benchmark labels observed in this sample map onto REDACT's full
6-type canonical vocabulary (PERSON via 3 source labels, EMAIL,
CREDIT_CARD, SSN via SNILS, MRN via OMS, IP via IP_ADDRESS/IPv6) -- the
first language pass to exercise every REDACT type at once.

DETECTION CONDITIONS: same five-condition structure as evaluate_fr.py/
evaluate_meddocan.py:
  1. "en-only"  : detect.scan_regex() alone (REDACT's existing English/US
                  patterns). Included because EMAIL/CREDIT_CARD/IP(v4)
                  are all scored by this layer alone per RU_TYPE_MAPPING.md
                  -- a real question is how much of the mapped gold set
                  en-only covers with zero Russian-specific code, and
                  (new this language) how much it MISSES on IPv6 despite
                  the type nominally being "IP" -- a concrete, measurable
                  instance of the general IPv4-only gap RU_TYPE_MAPPING.md
                  names.
  2. "ru-only"  : ru_detect.scan_ru() alone (SNILS/OMS/IPv6 regex + the
                  pymorphy2-lemmatized name dictionary).
  3. "combined" : en-only + ru-only unioned (with the same cross-layer
                  dedup evaluate() uses).
  4. "ru-ner" / "full" (--with-ner only): adds ru_ner.scan_ru_ner(), a
                  Russian-language Presidio NER model. NOT run by default
                  -- network-blocked in this project's normal sandbox, only
                  available inside Dockerfile.ru_ner / run_ru_ner.sh.
                  Without --with-ner, PERSON recall/precision reflects the
                  lemmatized-dictionary layer ONLY.
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))
import detect
import ru_detect

DATA_PATH = os.path.join(os.path.dirname(__file__), "datasets", "OpenRU_PII_raw.jsonl")

# pii_benchmark label -> REDACT canonical type, per RU_TYPE_MAPPING.md.
RU_PII_TO_REDACT = {
    "FIRST_NAME": "PERSON",
    "LAST_NAME": "PERSON",
    "MIDDLE_NAME": "PERSON",
    "EMAIL": "EMAIL",
    "CREDIT_CARD": "CREDIT_CARD",
    "SNILS": "SSN",
    "OMS": "MRN",
    "IP_ADDRESS": "IP",
}


def load_docs(path=DATA_PATH):
    docs = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                docs.append(json.loads(line))
    return docs


def gold_spans(doc):
    spans = []
    for e in doc["ents"]:
        rtype = RU_PII_TO_REDACT.get(e["t"])
        if rtype is None:
            continue
        start, end = e["o"]
        spans.append({"type": rtype, "start": start, "end": end})
    return spans


def _dedup(preds):
    """Same cross-layer dedup as evaluate_meddocan.py's/evaluate_fr.py's
    _dedup(): if a later hit is the same type and overlaps an
    already-kept hit, drop it as a harmless duplicate."""
    dedup = []
    for p in preds:
        if not any(p["type"] == d["type"] and p["start"] < d["end"] and d["start"] < p["end"]
                   for d in dedup):
            dedup.append(p)
    return dedup


def _predict(text, condition):
    if condition == "en-only":
        return detect.scan_regex(text)
    if condition == "ru-only":
        return ru_detect.scan_ru(text)
    if condition == "combined":
        return detect.scan_regex(text) + ru_detect.scan_ru(text)
    if condition == "ru-ner":
        import ru_ner
        return ru_ner.scan_ru_ner(text)
    if condition == "full":
        import ru_ner
        return detect.scan_regex(text) + ru_detect.scan_ru(text) + ru_ner.scan_ru_ner(text)
    raise ValueError(condition)


def evaluate(docs, condition):
    tp = fp = fn = 0
    by_type = {t: {"tp": 0, "fp": 0, "fn": 0} for t in
               ("PERSON", "EMAIL", "CREDIT_CARD", "SSN", "MRN", "IP")}

    t0 = time.perf_counter()
    n_docs = 0
    for doc in docs:
        n_docs += 1
        text = doc["text"]
        gold = gold_spans(doc)
        preds = _dedup(_predict(text, condition))

        matched = set()
        for p in preds:
            hit = False
            for i, g in enumerate(gold):
                if i in matched:
                    continue
                if g["type"] == p["type"] and p["start"] < g["end"] and g["start"] < p["end"]:
                    matched.add(i)
                    hit = True
                    tp += 1
                    by_type[p["type"]]["tp"] += 1
                    break
            if not hit:
                fp += 1
                if p["type"] in by_type:
                    by_type[p["type"]]["fp"] += 1
        for i, g in enumerate(gold):
            if i not in matched:
                fn += 1
                by_type[g["type"]]["fn"] += 1

    elapsed = time.perf_counter() - t0

    prec = tp / (tp + fp) if (tp + fp) else 0
    rec = tp / (tp + fn) if (tp + fn) else 0
    print(f"=== {condition} ({n_docs} documents) ===")
    print(f"  overall: P={prec:.3f} R={rec:.3f} (TP={tp} FP={fp} FN={fn})")
    print(f"  ** n={n_docs} is a small, curated pilot sample -- read this as "
          f"directional, not a confident final number (see prepare_ru_dataset.py). **")
    for t, c in by_type.items():
        total_gold = c["tp"] + c["fn"]
        if total_gold == 0 and c["fp"] == 0:
            continue
        r = c["tp"] / total_gold if total_gold else 0
        p = c["tp"] / (c["tp"] + c["fp"]) if (c["tp"] + c["fp"]) else 0
        print(f"    {t:12s}: P={p:.3f} R={r:.3f} (TP={c['tp']} FP={c['fp']} FN={c['fn']}, "
              f"gold={total_gold})")
    rate = n_docs / elapsed if elapsed else float("inf")
    print(f"  timing: {n_docs} documents in {elapsed:.2f}s -> {rate:.1f} docs/sec")
    print()
    return {"precision": prec, "recall": rec, "tp": tp, "fp": fp, "fn": fn, "by_type": by_type}


def diagnose_person(docs):
    """Root-causes PERSON-detection behavior across ru-only, ru-ner, and
    full -- same three questions the Spanish/French diagnose_person()
    functions answer, PLUS a fourth specific to Russian: of the
    lemmatized-dictionary layer's OWN false negatives (gold names it
    missed entirely), how many are a genuine dictionary-coverage gap
    (the lemma legitimately isn't in Faker's ru_RU list) vs. a
    lemmatization failure (the lemma should have matched but didn't) --
    see ru_detect.py's docstring for why these are two different
    problems that should not be conflated into one bare recall number."""
    import ru_ner

    dict_only_fp = ner_only_fp = both_fp = 0
    ner_fp_total = 0
    ner_only_examples = []
    dict_missed_names = []

    for doc in docs:
        text = doc["text"]
        gold_person = [(e["o"][0], e["o"][1], e["s"]) for e in doc["ents"]
                        if e["t"] in ("FIRST_NAME", "LAST_NAME", "MIDDLE_NAME")]

        dict_preds = [p for p in ru_detect.scan_russian_names(text)]
        ner_preds = [p for p in ru_ner.scan_ru_ner(text)]

        def is_fp(p, gold_list):
            return not any(p["start"] < ge and gs < p["end"] for gs, ge, _ in gold_list)

        dict_fp_spans = [(p["start"], p["end"]) for p in dict_preds if is_fp(p, gold_person)]
        ner_fp_spans = [(p["start"], p["end"]) for p in ner_preds if is_fp(p, gold_person)]

        def overlaps_any(span, other_spans):
            s, e = span
            return any(s < oe and os_ < e for os_, oe in other_spans)

        for span in dict_fp_spans:
            if overlaps_any(span, ner_fp_spans):
                both_fp += 1
            else:
                dict_only_fp += 1
        for span in ner_fp_spans:
            ner_fp_total += 1
            if not overlaps_any(span, dict_fp_spans):
                ner_only_fp += 1
                if len(ner_only_examples) < 20:
                    ner_only_examples.append(text[span[0]:span[1]])

        # Dictionary-coverage vs. lemmatization-failure split for FNs.
        dict_hit_spans = [(p["start"], p["end"]) for p in dict_preds]
        for gs, ge, surface in gold_person:
            if any(gs < pe and ps < ge for ps, pe in dict_hit_spans):
                continue  # matched, not a miss
            if len(dict_missed_names) < 30:
                dict_missed_names.append(surface)

    print("=== PERSON false-positive/false-negative root-cause diagnostic ===")
    print("  ** Sample is small (26 documents) -- treat these as illustrative "
          "of the diagnostic METHOD, not a confident measurement. **")
    print(f"  dict-only FPs (not also flagged by NER):  {dict_only_fp}")
    print(f"  NER-only FPs (not also flagged by dict):  {ner_only_fp}")
    print(f"  FPs both layers agree on (same span):     {both_fp}")
    if ner_fp_total:
        print(f"  {ner_fp_total} total NER PERSON FPs observed.")
    if ner_only_examples:
        print(f"  Sample NER-only FP text (first {len(ner_only_examples)}):")
        for ex in ner_only_examples:
            print(f"    {ex!r}")
    if dict_missed_names:
        print(f"  Dictionary layer's missed gold names (first {len(dict_missed_names)}) -- "
              f"check each by hand against Faker's ru_RU lists to separate a real "
              f"dictionary-coverage gap from a lemmatization failure:")
        for n in dict_missed_names:
            print(f"    {n!r}")
    print()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--with-ner", action="store_true",
        help="Also run the ru-ner and full conditions (needs ru_ner.py's "
             "Russian spaCy model -- only actually available inside "
             "Dockerfile.ru_ner, see that file and run_ru_ner.sh).",
    )
    parser.add_argument(
        "--diagnose", action="store_true",
        help="Also run diagnose_person(): root-causes PERSON FP/FN behavior, "
             "including separating a genuine dictionary-coverage gap from a "
             "lemmatization failure for missed names. Requires --with-ner.",
    )
    args = parser.parse_args()

    docs = load_docs()
    print(f"Loaded {len(docs)} Russian PII pilot documents "
          f"(curated 'test'-split sample -- see prepare_ru_dataset.py for "
          f"provenance and the small-sample-size disclosure).")
    total_gold = sum(len(gold_spans(d)) for d in docs)
    print(f"{total_gold} gold spans across the 6 REDACT-mapped types "
          f"(PERSON, EMAIL, CREDIT_CARD, SSN, MRN, IP).\n")

    conditions = ["en-only", "ru-only", "combined"]
    if args.with_ner:
        conditions += ["ru-ner", "full"]

    for condition in conditions:
        evaluate(docs, condition)

    if args.diagnose:
        if not args.with_ner:
            raise SystemExit("--diagnose requires --with-ner (it needs the Russian NER model).")
        diagnose_person(docs)


if __name__ == "__main__":
    main()
