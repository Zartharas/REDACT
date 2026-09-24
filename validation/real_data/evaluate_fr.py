"""
Evaluation harness for the OpenPII French pilot sample
(validation/real_data/datasets/OpenPII_FR_raw.jsonl), scoring REDACT's
detection against real-format, synthetic-content gold PII spans.

Mirrors evaluate_meddocan.py's matching logic exactly (same overlap-based
TP/FP/FN rule: same type, p.start < g.end and g.start < p.end; same greedy
one-gold-per-prediction matching; same cross-layer dedup for overlapping
same-type hits) so results are directly comparable in METHODOLOGY to this
project's other real-data conditions. They are NOT directly comparable in
DIFFICULTY to MEDDOCAN's numbers -- see prepare_fr_dataset.py's "A REAL,
DISCLOSED DIFFERENCE FROM MEDDOCAN" section: this is synthetic, single-
sentence text, not real clinical narrative, and the staged sample (11
documents) is far smaller than MEDDOCAN's 520. Read both numbers with
their own caveats, not side by side as if they measured the same thing.

TYPE MAPPING: see validation/real_data/FR_TYPE_MAPPING.md for the full
reasoning. Only 3 of the ai4privacy labels observed in this sample have a
REDACT canonical-type equivalent (PERSON via GIVENNAME+SURNAME, EMAIL,
CREDIT_CARD); gold spans of any other label are not scored here.

DETECTION CONDITIONS: same five-condition structure as evaluate_meddocan.py
(THREE by default, two more needing --with-ner):
  1. "en-only"  : detect.scan_regex() alone (REDACT's existing, unmodified
                  English/US patterns -- SSN/EMAIL/CREDIT_CARD/IP/MRN).
                  Included because EMAIL and CREDIT_CARD in this sample are
                  BOTH scored by this layer alone per FR_TYPE_MAPPING.md --
                  a real question this condition answers is how much of
                  the mapped-type gold set en-only already covers without
                  any French-specific code at all.
  2. "fr-only"  : fr_detect.scan_fr() alone (the new French name-
                  dictionary layer -- PERSON only, see fr_detect.py's
                  docstring for why no new regex patterns were added).
  3. "combined" : en-only + fr-only unioned (with the same cross-layer
                  dedup evaluate() uses).
  4. "fr-ner" / "full" (--with-ner only): adds fr_ner.scan_fr_ner(), a
                  French-language Presidio NER model. NOT run by default
                  -- Presidio's French spaCy model download is
                  network-blocked in this project's normal sandbox (same
                  documented limitation as every other real-data condition
                  in this validation set), so fr_ner.py can only actually
                  run inside the Docker image (Dockerfile.fr_ner /
                  run_fr_ner.sh), which has real internet access to
                  download it. Without --with-ner, PERSON recall/precision
                  reflects the dictionary layer ONLY, not what a full
                  production pipeline (regex + dictionary + NER) would see
                  -- disclosed here and again in the printed results, not
                  glossed over.
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))
import detect
import fr_detect

DATA_PATH = os.path.join(os.path.dirname(__file__), "datasets", "OpenPII_FR_raw.jsonl")

# ai4privacy label -> REDACT canonical type, per FR_TYPE_MAPPING.md.
# Only these three map (GIVENNAME/SURNAME both fold into PERSON); every
# other label in a document's "ents" list is skipped when building gold
# spans (not a REDACT-scored category, or not yet observed in this sample
# -- see FR_TYPE_MAPPING.md's "labels present ... but NOT observed" note).
OPENPII_FR_TO_REDACT = {
    "GIVENNAME": "PERSON",
    "SURNAME": "PERSON",
    "EMAIL": "EMAIL",
    "CREDITCARDNUMBER": "CREDIT_CARD",
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
        rtype = OPENPII_FR_TO_REDACT.get(e["t"])
        if rtype is None:
            continue
        start, end = e["o"]
        spans.append({"type": rtype, "start": start, "end": end})
    return spans


def _dedup(preds):
    """Same cross-layer dedup as evaluate_meddocan.py's _dedup(): if a
    later hit is the same type and overlaps an already-kept hit, drop it
    as a harmless duplicate rather than double-counting or false-positive
    -ing on two layers correctly agreeing."""
    dedup = []
    for p in preds:
        if not any(p["type"] == d["type"] and p["start"] < d["end"] and d["start"] < p["end"]
                   for d in dedup):
            dedup.append(p)
    return dedup


def _predict(text, condition):
    if condition == "en-only":
        return detect.scan_regex(text)
    if condition == "fr-only":
        return fr_detect.scan_fr(text)
    if condition == "combined":
        return detect.scan_regex(text) + fr_detect.scan_fr(text)
    if condition == "fr-ner":
        import fr_ner
        return fr_ner.scan_fr_ner(text)
    if condition == "full":
        import fr_ner
        return detect.scan_regex(text) + fr_detect.scan_fr(text) + fr_ner.scan_fr_ner(text)
    raise ValueError(condition)


def evaluate(docs, condition):
    tp = fp = fn = 0
    by_type = {t: {"tp": 0, "fp": 0, "fn": 0} for t in ("PERSON", "EMAIL", "CREDIT_CARD")}

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
    print(f"  ** n={n_docs} is a small pilot sample -- read this as directional, "
          f"not a confident final number (see prepare_fr_dataset.py). **")
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
    """Root-causes PERSON-detection behavior across fr-only, fr-ner, and
    full, same three questions evaluate_meddocan.py's diagnose_person()
    answers for Spanish: (1) how much do the dictionary and NER layers'
    FALSE positives actually overlap vs stack additively; (2) of NER's OWN
    false positives, what fraction overlap a real gold non-PERSON span
    (this sample's mapped-out CITY spans are the closest analogue to
    MEDDOCAN's place/institution-type confusion); (3) concrete example FPs
    unique to NER, for a human to eyeball the failure mode directly. Given
    this sample's small size (11 documents, 27 gold spans total, only 11
    PERSON), treat the resulting percentages as illustrative of the
    METHOD, not as a statistically confident measurement -- MEDDOCAN's own
    97%-disjoint finding was measured over >2,000 PERSON gold spans, two
    orders of magnitude more evidence than this sample provides."""
    import fr_ner

    dict_only_fp = ner_only_fp = both_fp = 0
    ner_fp_place_overlap = 0
    ner_fp_total = 0
    ner_only_examples = []

    for doc in docs:
        text = doc["text"]
        gold_person = [(e["o"][0], e["o"][1]) for e in doc["ents"]
                        if e["t"] in ("GIVENNAME", "SURNAME")]
        # CITY is this sample's closest analogue to MEDDOCAN's
        # place/institution-type gold spans -- a capitalized proper noun
        # NER could plausibly confuse with a person name.
        gold_place = [(e["o"][0], e["o"][1]) for e in doc["ents"] if e["t"] == "CITY"]

        dict_preds = [p for p in fr_detect.scan_french_names(text)]
        ner_preds = [p for p in fr_ner.scan_fr_ner(text)]

        def is_fp(p):
            return not any(p["start"] < ge and gs < p["end"] for gs, ge in gold_person)

        dict_fp_spans = [(p["start"], p["end"]) for p in dict_preds if is_fp(p)]
        ner_fp_spans = [(p["start"], p["end"]) for p in ner_preds if is_fp(p)]

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
            if overlaps_any(span, gold_place):
                ner_fp_place_overlap += 1
            if not overlaps_any(span, dict_fp_spans):
                ner_only_fp += 1
                if len(ner_only_examples) < 20:
                    ner_only_examples.append(text[span[0]:span[1]])

    print("=== PERSON false-positive root-cause diagnostic ===")
    print("  ** Sample is small (11 documents) -- treat these as illustrative "
          "of the diagnostic METHOD, not a confident measurement. **")
    print(f"  dict-only FPs (not also flagged by NER):  {dict_only_fp}")
    print(f"  NER-only FPs (not also flagged by dict):  {ner_only_fp}")
    print(f"  FPs both layers agree on (same span):     {both_fp}")
    if ner_fp_total:
        print(f"  Of {ner_fp_total} NER PERSON FPs, {ner_fp_place_overlap} "
              f"({ner_fp_place_overlap/ner_fp_total:.1%}) overlap a real gold "
              f"CITY span (same failure-mode CLASS already root-caused for "
              f"Spanish/MEDDOCAN's place-name confusion, though French's "
              f"gold place-type coverage here is CITY only, not MEDDOCAN's "
              f"broader TERRITORIO/PAIS/HOSPITAL/INSTITUCION/CENTRO_SALUD set).")
    if ner_only_examples:
        print(f"  Sample NER-only FP text (first {len(ner_only_examples)}):")
        for ex in ner_only_examples:
            print(f"    {ex!r}")
    print()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--with-ner", action="store_true",
        help="Also run the fr-ner and full conditions (needs fr_ner.py's "
             "French spaCy model -- only actually available inside "
             "Dockerfile.fr_ner, see that file and run_fr_ner.sh). Fails "
             "loudly with a clear message if the model isn't installed, "
             "rather than silently skipping.",
    )
    parser.add_argument(
        "--diagnose", action="store_true",
        help="Also run diagnose_person(): root-causes WHY combining dict "
             "+ NER helps or hurts precision (FP overlap breakdown, NER's "
             "own place-name confusion rate, example NER-only false "
             "positives). Requires --with-ner (needs the French NER model "
             "too).",
    )
    args = parser.parse_args()

    docs = load_docs()
    print(f"Loaded {len(docs)} OpenPII French pilot documents "
          f"(all 'validation' split -- see prepare_fr_dataset.py for "
          f"provenance and the small-sample-size disclosure).")
    total_gold = sum(len(gold_spans(d)) for d in docs)
    print(f"{total_gold} gold spans across the 3 REDACT-mapped types "
          f"(PERSON, EMAIL, CREDIT_CARD).\n")

    conditions = ["en-only", "fr-only", "combined"]
    if args.with_ner:
        conditions += ["fr-ner", "full"]

    for condition in conditions:
        evaluate(docs, condition)

    if args.diagnose:
        if not args.with_ner:
            raise SystemExit("--diagnose requires --with-ner (it needs the French NER model).")
        diagnose_person(docs)


if __name__ == "__main__":
    main()
