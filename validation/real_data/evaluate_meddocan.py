"""
Evaluation harness for the real MEDDOCAN corpus
(validation/real_data/datasets/MEDDOCAN_raw.jsonl), scoring REDACT's
detection against real, expert-annotated gold-standard PHI spans.

Mirrors inject_and_evaluate.py's evaluate() matching logic exactly (same
overlap-based TP/FP/FN rule: same type, p.start < g.end and g.start <
p.end; same greedy one-gold-per-prediction matching; same cross-layer
dedup for overlapping same-type hits) so results are directly comparable
in methodology to this project's other real-data conditions -- but this
harness needs NO injection step. Unlike OpenSSH/Linux/Thunderbird/
windows_event/cloudtrail (all of which start from real but PII-scrubbed
carrier text and have synthetic PII injected in), MEDDOCAN's PHI is
already real (enriched onto real SciELO case-report text by expert
annotators) and already has real gold-standard spans -- injection would
be pointless and would throw away the one thing that makes this dataset
worth adding.

TYPE MAPPING: see validation/real_data/MEDDOCAN_TYPE_MAPPING.md for the
full reasoning. Only 5 of MEDDOCAN's 22 annotated types have a REDACT
canonical-type equivalent; gold spans of any other type are not scored
here (scoring against a type REDACT never claims to detect would not
test anything real).

DETECTION CONDITIONS: this harness runs FOUR conditions per document by
default THREE (the fourth needs --with-ner), so the marginal contribution
of each layer is visible on its own, not just folded into one number:
  1. "en-only"   : detect.scan_regex() alone (REDACT's existing, unmodified
                   English/US patterns -- SSN/EMAIL/CREDIT_CARD/IP/MRN).
  2. "es-only"   : es_detect.scan_es() alone (the new NHC/NASS regex +
                   Spanish name dictionary).
  3. "combined"  : en-only + es-only unioned (with the same cross-layer
                   dedup evaluate() uses).
  4. "es-ner" / "full" (--with-ner only): adds es_ner.scan_es_ner(), a
                   Spanish-language Presidio NER model. NOT run by
                   default -- Presidio's Spanish spaCy model download is
                   network-blocked in THIS project's normal sandbox (same
                   documented limitation as every other real-data
                   condition in this validation set), so es_ner.py can
                   only actually run inside the Docker image
                   (Dockerfile.meddocan_ner / run_meddocan_ner.sh), which
                   has real internet access to download it. Without
                   --with-ner, PERSON recall/precision reflects the
                   dictionary layer ONLY, not what a full production
                   pipeline (regex + dictionary + NER) would see --
                   disclosed here and again in the printed results, not
                   glossed over.
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))
import detect
import es_detect

DATA_PATH = os.path.join(os.path.dirname(__file__), "datasets", "MEDDOCAN_raw.jsonl")

# MEDDOCAN entity type -> REDACT canonical type, per MEDDOCAN_TYPE_MAPPING.md.
# Only these five map; everything else in a document's "ents" list is
# skipped when building gold spans (not a REDACT-scored category).
MEDDOCAN_TO_REDACT = {
    "NOMBRE_SUJETO_ASISTENCIA": "PERSON",
    "NOMBRE_PERSONAL_SANITARIO": "PERSON",
    "CORREO_ELECTRONICO": "EMAIL",
    "ID_SUJETO_ASISTENCIA": "MRN",
    "ID_ASEGURAMIENTO": "SSN",
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
        rtype = MEDDOCAN_TO_REDACT.get(e["t"])
        if rtype is None:
            continue
        start, end = e["o"]
        spans.append({"type": rtype, "start": start, "end": end})
    return spans


def _dedup(preds):
    """Same cross-layer dedup as inject_and_evaluate.py's evaluate(): if a
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
    if condition == "es-only":
        return es_detect.scan_es(text)
    if condition == "combined":
        return detect.scan_regex(text) + es_detect.scan_es(text)
    if condition == "es-ner":
        import es_ner
        return es_ner.scan_es_ner(text)
    if condition == "full":
        import es_ner
        return detect.scan_regex(text) + es_detect.scan_es(text) + es_ner.scan_es_ner(text)
    raise ValueError(condition)


def evaluate(docs, condition, split_filter=None):
    tp = fp = fn = 0
    by_type = {t: {"tp": 0, "fp": 0, "fn": 0} for t in ("PERSON", "EMAIL", "MRN", "SSN")}

    t0 = time.perf_counter()
    n_docs = 0
    for doc in docs:
        if split_filter is not None and doc["split"] != split_filter:
            continue
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
    label = condition if split_filter is None else f"{condition} [{split_filter}]"
    print(f"=== {label} ({n_docs} documents) ===")
    print(f"  overall: P={prec:.3f} R={rec:.3f} (TP={tp} FP={fp} FN={fn})")
    for t, c in by_type.items():
        total_gold = c["tp"] + c["fn"]
        if total_gold == 0 and c["fp"] == 0:
            continue
        r = c["tp"] / total_gold if total_gold else 0
        p = c["tp"] / (c["tp"] + c["fp"]) if (c["tp"] + c["fp"]) else 0
        print(f"    {t:8s}: P={p:.3f} R={r:.3f} (TP={c['tp']} FP={c['fp']} FN={c['fn']}, "
              f"gold={total_gold})")
    rate = n_docs / elapsed if elapsed else float("inf")
    print(f"  timing: {n_docs} documents in {elapsed:.2f}s -> {rate:.1f} docs/sec")
    print()
    return {"precision": prec, "recall": rec, "tp": tp, "fp": fp, "fn": fn, "by_type": by_type}


# Real-carrier gold types this project has no canonical detector for
# (location/institution/street types) -- used only to root-cause PERSON
# false positives (do they collide with a real place/institution name?),
# same diagnostic already run by hand for the dictionary-only layer in
# Section 6 (94.3% of dict PERSON FPs overlapped one of these).
_PLACE_LIKE_MEDDOCAN_TYPES = {
    "TERRITORIO", "PAIS", "CALLE", "HOSPITAL", "INSTITUCION", "CENTRO_SALUD",
}


def diagnose_person(docs):
    """Root-causes the PERSON-detection behavior seen across es-only,
    es-ner, and full, instead of leaving the aggregate P/R numbers to
    speak for themselves. Answers three questions live results from a
    single evaluate() call each cannot: (1) how much do the dictionary and
    NER layers' FALSE positives actually overlap vs stack additively; (2)
    of NER's OWN false positives, what fraction are the same place/
    institution-name confusion already root-caused for the dictionary
    layer, vs some other failure mode; (3) concrete example FPs unique to
    NER, for a human to eyeball the failure mode directly rather than
    trust only percentages."""
    import es_ner

    dict_only_fp = ner_only_fp = both_fp = 0
    ner_fp_place_overlap = 0
    ner_fp_total = 0
    ner_only_examples = []

    for doc in docs:
        text = doc["text"]
        gold_person = [(e["o"][0], e["o"][1]) for e in doc["ents"]
                        if e["t"] in ("NOMBRE_SUJETO_ASISTENCIA", "NOMBRE_PERSONAL_SANITARIO")]
        gold_place = [(e["o"][0], e["o"][1]) for e in doc["ents"]
                      if e["t"] in _PLACE_LIKE_MEDDOCAN_TYPES]

        dict_preds = [p for p in es_detect.scan_spanish_names(text)]
        ner_preds = [p for p in es_ner.scan_es_ner(text)]

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
    print(f"  dict-only FPs (not also flagged by NER):  {dict_only_fp}")
    print(f"  NER-only FPs (not also flagged by dict):  {ner_only_fp}")
    print(f"  FPs both layers agree on (same span):     {both_fp}")
    if ner_fp_total:
        print(f"  Of {ner_fp_total} NER PERSON FPs, {ner_fp_place_overlap} "
              f"({ner_fp_place_overlap/ner_fp_total:.1%}) overlap a real gold "
              f"place/institution/street span (same failure mode already "
              f"root-caused for the dictionary layer).")
    if ner_only_examples:
        print(f"  Sample NER-only FP text (first {len(ner_only_examples)}):")
        for ex in ner_only_examples:
            print(f"    {ex!r}")
    print()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--with-ner", action="store_true",
        help="Also run the es-ner and full conditions (needs es_ner.py's "
             "Spanish spaCy model -- only actually available inside "
             "Dockerfile.meddocan_ner, see that file and "
             "run_meddocan_ner.sh). Fails loudly with a clear message if "
             "the model isn't installed, rather than silently skipping.",
    )
    parser.add_argument(
        "--diagnose", action="store_true",
        help="Also run diagnose_person(): root-causes WHY combining dict "
             "+ NER hurts precision (FP overlap breakdown, NER's own "
             "place-name confusion rate, example NER-only false "
             "positives). Requires --with-ner (needs the Spanish NER "
             "model too).",
    )
    args = parser.parse_args()

    docs = load_docs()
    print(f"Loaded {len(docs)} real MEDDOCAN documents "
          f"({sum(1 for d in docs if d['split']=='train')} train / "
          f"{sum(1 for d in docs if d['split']=='validation')} validation / "
          f"{sum(1 for d in docs if d['split']=='test')} test).")
    total_gold = sum(len(gold_spans(d)) for d in docs)
    print(f"{total_gold} gold spans across the 5 REDACT-mapped types "
          f"(PERSON, EMAIL, MRN, SSN).\n")

    conditions = ["en-only", "es-only", "combined"]
    if args.with_ner:
        conditions += ["es-ner", "full"]

    for condition in conditions:
        evaluate(docs, condition)

    if args.diagnose:
        if not args.with_ner:
            raise SystemExit("--diagnose requires --with-ner (it needs the Spanish NER model).")
        diagnose_person(docs)


if __name__ == "__main__":
    main()
