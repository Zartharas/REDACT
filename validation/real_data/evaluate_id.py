"""
Evaluation harness for the (currently blocked -- see prepare_id_dataset.py)
Indonesian PII pilot sample (validation/real_data/datasets/
OpenPII_ID_raw.jsonl). Built and ready to run the moment that file exists
-- same 5-condition structure as evaluate_fr.py/evaluate_ru.py/
evaluate_meddocan.py, so results will be directly comparable in
methodology once real data is staged.

TYPE MAPPING: see validation/real_data/ID_TYPE_MAPPING.md -- marked
PROPOSED, PENDING VERIFICATION throughout, since no real Indonesian
example has been checked against it yet.

DETECTION CONDITIONS:
  1. "en-only"  : detect.scan_regex() alone.
  2. "id-only"  : id_detect.scan_id() alone (the Indonesian name
                  dictionary -- no regex layer yet, see id_detect.py's
                  docstring for why).
  3. "combined" : en-only + id-only unioned.
  4. "id-ner" / "full" (--with-ner only): adds id_ner.scan_id_ner(), a
                  HuggingFace transformers-based Indonesian NER model
                  (architecturally different from the spaCy-based NER
                  layers in the other three languages -- see
                  id_ner.py's docstring for why). Network-blocked in
                  this project's normal sandbox, only available inside
                  Dockerfile.id_ner / run_id_ner.sh.
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))
import detect
import id_detect

DATA_PATH = os.path.join(os.path.dirname(__file__), "datasets", "OpenPII_ID_raw.jsonl")

# ai4privacy label -> REDACT canonical type, per ID_TYPE_MAPPING.md.
# PROPOSED, PENDING VERIFICATION against real Indonesian data -- see that
# document's status header before trusting any result this produces.
OPENPII_ID_TO_REDACT = {
    "GIVENNAME": "PERSON",
    "SURNAME": "PERSON",
    "EMAIL": "EMAIL",
    "CREDITCARDNUMBER": "CREDIT_CARD",
    "SOCIALNUM": "SSN",
}


def load_docs(path=DATA_PATH):
    if not os.path.exists(path):
        raise SystemExit(
            f"{path} not found. This is a KNOWN, DISCLOSED blocker -- see "
            f"prepare_id_dataset.py's 'THE ACTUAL BLOCKER' and 'HOW TO "
            f"UNBLOCK THIS' sections for exactly why this sandbox couldn't "
            f"stage Indonesian rows from ai4privacy/pii-masking-"
            f"openpii-1.5m, and the exact commands to stage this file "
            f"yourself with real internet access. This harness will run "
            f"immediately once that file exists -- nothing else needs to "
            f"change."
        )
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
        rtype = OPENPII_ID_TO_REDACT.get(e["t"])
        if rtype is None:
            continue
        start, end = e["o"]
        spans.append({"type": rtype, "start": start, "end": end})
    return spans


def _dedup(preds):
    dedup = []
    for p in preds:
        if not any(p["type"] == d["type"] and p["start"] < d["end"] and d["start"] < p["end"]
                   for d in dedup):
            dedup.append(p)
    return dedup


def _predict(text, condition):
    if condition == "en-only":
        return detect.scan_regex(text)
    if condition == "id-only":
        return id_detect.scan_id(text)
    if condition == "combined":
        return detect.scan_regex(text) + id_detect.scan_id(text)
    if condition == "id-ner":
        import id_ner
        return id_ner.scan_id_ner(text)
    if condition == "full":
        import id_ner
        return detect.scan_regex(text) + id_detect.scan_id(text) + id_ner.scan_id_ner(text)
    raise ValueError(condition)


def evaluate(docs, condition):
    tp = fp = fn = 0
    by_type = {t: {"tp": 0, "fp": 0, "fn": 0} for t in ("PERSON", "EMAIL", "CREDIT_CARD", "SSN")}

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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--with-ner", action="store_true",
                         help="Also run id-ner and full (needs Dockerfile.id_ner).")
    parser.add_argument("--diagnose", action="store_true",
                         help="Root-cause PERSON FP/FN behavior. Requires --with-ner.")
    args = parser.parse_args()

    docs = load_docs()
    print(f"Loaded {len(docs)} Indonesian PII documents.")
    total_gold = sum(len(gold_spans(d)) for d in docs)
    print(f"{total_gold} gold spans across the REDACT-mapped types "
          f"(PERSON, EMAIL, CREDIT_CARD, SSN -- see ID_TYPE_MAPPING.md, "
          f"proposed mapping pending verification).\n")

    conditions = ["en-only", "id-only", "combined"]
    if args.with_ner:
        conditions += ["id-ner", "full"]

    for condition in conditions:
        evaluate(docs, condition)

    if args.diagnose:
        if not args.with_ner:
            raise SystemExit("--diagnose requires --with-ner.")
        print("diagnose_person() not yet implemented for Indonesian -- add "
              "once real data reveals what's worth root-causing (mirror "
              "evaluate_ru.py's/evaluate_fr.py's diagnose_person(), which "
              "were both written after seeing real results, not before).")


if __name__ == "__main__":
    main()
