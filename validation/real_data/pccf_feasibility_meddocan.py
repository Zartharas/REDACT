"""
PCCF feasibility check on MEDDOCAN (ALGORITHM_DESIGN.md Section 7).

Question: on data this project has already characterized, does
presence-conditioned conformal calibration (src/pccf.py) beat naive union
on PERSON precision? Answering that comes before writing anything for the
French/Russian data.

No new data. The input is the same 520 staged MEDDOCAN documents, the same
unmodified layers (es_detect.scan_spanish_names, es_ner.scan_es_ner), the
same gold mapping, and the same _dedup + overlap-matching rule as
evaluate_meddocan.py. That harness is imported, not modified. The only new
signal is the beam-marginal NER confidence from src/ner_confidence.py.
Without it, Presidio's constant 0.85 score makes the check degenerate; the
degenerate case is still run and reported as its own condition.

Layers for PERSON: K_effective = 2 (dictionary, NER). None of the regex
layers (detect.scan_regex, es_detect.scan_es_regex) emit PERSON, so the
non-empty presence patterns are {dict}, {ner}, and {dict, ner}.

Protocol:
  - PRIMARY: calibrate on MEDDOCAN train+validation (400 docs) and test on
    test (120 docs). This is MEDDOCAN's own split, fixed in advance.
  - ROBUSTNESS: 5-fold document-level CV over all 520 docs (seed 0).
  - UNCERTAINTY: paired bootstrap over test documents (2,000 resamples)
    for the two pre-specified primary configs (the design-faithful
    coverage rule at alpha=0.10, and precision mode at target 0.70).

Usage (run from anywhere; needs es_core_news_md installed locally, or use
the Dockerfile.meddocan_ner image):
  python validation/real_data/pccf_feasibility_meddocan.py --build-cache
  python validation/real_data/pccf_feasibility_meddocan.py
The cache stores offsets and scores only (no document text) in
output/pccf_meddocan_layer_cache.json.
"""
import argparse
import json
import os
import random
import statistics
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, HERE)

import evaluate_meddocan as em  # noqa: E402  (imported, not modified)
import pccf  # noqa: E402

CACHE = os.path.join(ROOT, "output", "pccf_meddocan_layer_cache.json")
RESULTS_TXT = os.path.join(HERE, "pccf_feasibility_results.txt")
RESULTS_JSON = os.path.join(HERE, "pccf_feasibility_results.json")
MODEL = "es_core_news_md"
PRESIDIO_CONSTANT = 0.85  # measured: every Presidio SpacyRecognizer hit


# --------------------------------------------------------------------------
# Cache: run the unmodified layers once
# --------------------------------------------------------------------------
def build_cache():
    import es_detect
    import es_ner
    import ner_confidence

    docs = em.load_docs()
    out = []
    t0 = time.perf_counter()
    for i, d in enumerate(docs):
        text = d["text"]
        dict_hits = es_detect.scan_spanish_names(text)
        ner_hits = ner_confidence.annotate(es_ner.scan_es_ner(text), text, MODEL, "PER")
        out.append({
            "id": d["id"], "split": d["split"],
            "dict": [[h["start"], h["end"]] for h in dict_hits],
            "ner": [[h["start"], h["end"], round(h["confidence"], 6)] for h in ner_hits],
        })
        if (i + 1) % 100 == 0:
            print(f"  cached {i+1}/{len(docs)} ({time.perf_counter()-t0:.0f}s)", flush=True)
    os.makedirs(os.path.dirname(CACHE), exist_ok=True)
    with open(CACHE, "w") as f:
        json.dump({"model": MODEL, "docs": out}, f)
    print(f"wrote {CACHE} ({len(out)} docs, {time.perf_counter()-t0:.0f}s)")


# --------------------------------------------------------------------------
# Per-document candidates, labels, scoring
# --------------------------------------------------------------------------
def layer_hits(entry, ner_score):
    """ner_score: 'beam' (real per-span confidence) or 'presidio'
    (the constant 0.85 the unmodified NER layer actually reports)."""
    dict_hits = [{"type": "PERSON", "start": s, "end": e, "method": "es_name_dict"}
                 for s, e in entry["dict"]]
    ner_hits = [{"type": "PERSON", "start": s, "end": e, "method": "es_ner",
                 "confidence": c if ner_score == "beam" else PRESIDIO_CONSTANT}
                for s, e, c in entry["ner"]]
    return {"dict": dict_hits, "ner": ner_hits}


def gold_person(doc):
    return [g for g in em.gold_spans(doc) if g["type"] == "PERSON"]


def label(cand, gold):
    return any(ls < g["end"] and g["start"] < le
               for _, h in cand.members for ls, le in [(h["start"], h["end"])] for g in gold)


def preds_from(cands, accepted_ids, hits):
    """Emit accepted candidates' member hits in the SAME order the
    'full' condition emits them (dictionary hits, then NER hits), so that
    _dedup keeps the same span when both layers agree."""
    keep = set()
    for c in cands:
        if id(c) in accepted_ids:
            for _, h in c.members:
                keep.add(id(h))
    return [h for h in hits["dict"] + hits["ner"] if id(h) in keep]


def score(pairs):
    """pairs: [(preds, gold)]. Same matching rule as em.evaluate()."""
    tp = fp = fn = 0
    for preds, gold in pairs:
        preds = em._dedup(preds)
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
                    break
            if not hit:
                fp += 1
        fn += len(gold) - len(matched)
    return counts_to_metrics(tp, fp, fn)


def counts_to_metrics(tp, fp, fn):
    P = tp / (tp + fp) if tp + fp else 0.0
    R = tp / (tp + fn) if tp + fn else 0.0
    f = lambda b: (1 + b * b) * P * R / (b * b * P + R) if (P + R) else 0.0  # noqa: E731
    return {"tp": tp, "fp": fp, "fn": fn, "P": P, "R": R, "F1": f(1), "F2": f(2)}


# --------------------------------------------------------------------------
# Decision rules (baselines + PCCF variants)
# --------------------------------------------------------------------------
LAYER_ALONE = {"dict-only (layer alone)": "dict", "ner-only (layer alone)": "ner"}


def rule_accept(rule):
    """Fixed, uncalibrated baselines, keyed on the presence mask only."""
    D, N, DN = frozenset({"dict"}), frozenset({"ner"}), frozenset({"dict", "ner"})
    table = {
        "union": {D, N, DN},
        "intersection (agreement only)": {DN},
    }[rule]
    return lambda c: c.mask in table


def configs():
    out = [("union", "presidio", None)]
    for r in ("dict-only (layer alone)", "ner-only (layer alone)", "intersection (agreement only)"):
        out.append((r, "presidio", None))
    for ns in ("presidio", "beam"):
        for nested in (False, True):
            for a in (0.05, 0.10, 0.20):
                out.append((f"PCCF coverage a={a:.2f} nested={nested} score={ns}", ns,
                            dict(mode="coverage", alpha=a, nested=nested)))
            for tpz in (0.50, 0.60, 0.70, 0.80):
                out.append((f"PCCF precision>={tpz:.2f} nested={nested} score={ns}", ns,
                            dict(mode="precision", target_precision=tpz, delta=0.05, nested=nested)))
    return out


PRIMARY = {
    "PCCF coverage a=0.10 nested=False score=beam",
    "PCCF precision>=0.70 nested=False score=beam",
}


def prepare(cache_docs, docs_by_id, ner_score):
    rows = []
    for entry in cache_docs:
        hits = layer_hits(entry, ner_score)
        cands = pccf.build_candidates(hits, "PERSON")
        gold = gold_person(docs_by_id[entry["id"]])
        rows.append({"id": entry["id"], "split": entry["split"], "hits": hits,
                     "cands": cands, "labels": [label(c, gold) for c in cands], "gold": gold})
    return rows


def run_config(cfg, calib_rows, test_rows):
    name, _, params = cfg
    model = None
    if name in LAYER_ALONE:
        # The layer's own raw output, exactly as evaluate_meddocan.py's
        # es-only / es-ner conditions emit PERSON (no candidate grouping).
        layer = LAYER_ALONE[name]
        acc = lambda c: layer in c.mask  # noqa: E731  (for the pattern stats only)
    elif params is None:
        acc = rule_accept(name)
    else:
        model = pccf.PCCF(**params)
        model.fit([c for r in calib_rows for c in r["cands"]],
                  [y for r in calib_rows for y in r["labels"]])
        acc = model.accept
    per_doc = []
    for r in test_rows:
        accepted = {id(c) for c in r["cands"] if acc(c)}
        preds = (list(r["hits"][LAYER_ALONE[name]]) if name in LAYER_ALONE
                 else preds_from(r["cands"], accepted, r["hits"]))
        per_doc.append((preds, r["gold"], r, accepted))
    m = score([(p, g) for p, g, _, _ in per_doc])
    # Per-pattern empirical recall among true candidates (the quantity the
    # coverage mode claims to control at >= 1-alpha).
    pat = {}
    for _, _, r, accepted in per_doc:
        for c, y in zip(r["cands"], r["labels"]):
            k = "+".join(sorted(c.mask))
            d = pat.setdefault(k, {"n": 0, "true": 0, "kept": 0, "kept_true": 0})
            d["n"] += 1
            d["true"] += y
            if id(c) in accepted:
                d["kept"] += 1
                d["kept_true"] += y
    return m, pat, per_doc, model


# --------------------------------------------------------------------------
# Diagnostics
# --------------------------------------------------------------------------
def auroc(pos, neg):
    if not pos or not neg:
        return None
    allv = sorted([(v, 1) for v in pos] + [(v, 0) for v in neg])
    # rank-sum with average ranks for ties
    ranks, i = {}, 0
    while i < len(allv):
        j = i
        while j < len(allv) and allv[j][0] == allv[i][0]:
            j += 1
        for k in range(i, j):
            ranks[k] = (i + j + 1) / 2
        i = j
    rp = sum(ranks[k] for k, (_, y) in enumerate(allv) if y)
    return (rp - len(pos) * (len(pos) + 1) / 2) / (len(pos) * len(neg))


def pattern_table(rows):
    out = {}
    for r in rows:
        for c, y in zip(r["cands"], r["labels"]):
            k = "+".join(sorted(c.mask))
            d = out.setdefault(k, {"n": 0, "true": 0, "ner_conf_true": [], "ner_conf_false": []})
            d["n"] += 1
            d["true"] += y
            if "ner" in c.scores:
                (d["ner_conf_true"] if y else d["ner_conf_false"]).append(1 - c.scores["ner"])
    res = {}
    for k, d in sorted(out.items()):
        res[k] = {
            "n": d["n"], "true": d["true"], "precision": d["true"] / d["n"],
            "ner_conf_mean_true": statistics.mean(d["ner_conf_true"]) if d["ner_conf_true"] else None,
            "ner_conf_mean_false": statistics.mean(d["ner_conf_false"]) if d["ner_conf_false"] else None,
            # AUROC of NER confidence for separating true from false INSIDE
            # the pattern. This is the only signal coverage mode can use.
            "ner_conf_auroc_within_pattern": auroc(d["ner_conf_true"], d["ner_conf_false"]),
        }
    return res


def bootstrap(per_doc_a, per_doc_b, n_boot=2000, seed=0):
    rng = random.Random(seed)
    idx = list(range(len(per_doc_a)))
    dP, dR, dF = [], [], []
    for _ in range(n_boot):
        s = [rng.choice(idx) for _ in idx]
        ma = score([(per_doc_a[i][0], per_doc_a[i][1]) for i in s])
        mb = score([(per_doc_b[i][0], per_doc_b[i][1]) for i in s])
        dP.append(mb["P"] - ma["P"])
        dR.append(mb["R"] - ma["R"])
        dF.append(mb["F1"] - ma["F1"])
    ci = lambda v: (sorted(v)[int(0.025 * len(v))], sorted(v)[int(0.975 * len(v)) - 1])  # noqa: E731
    return {"dP_95ci": ci(dP), "dR_95ci": ci(dR), "dF1_95ci": ci(dF)}


# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--build-cache", action="store_true")
    args = ap.parse_args()
    if args.build_cache:
        build_cache()
        return

    with open(CACHE) as f:
        cache = json.load(f)
    docs = em.load_docs()
    docs_by_id = {d["id"]: d for d in docs}
    lines, results = [], {}
    say = lambda s="": (print(s), lines.append(s))  # noqa: E731

    rows = {ns: prepare(cache["docs"], docs_by_id, ns) for ns in ("presidio", "beam")}

    # ---- A. sanity: reproduce evaluate_meddocan.py's recorded numbers ----
    say("=== A. Sanity: fixed rules on all 520 docs must reproduce evaluate_meddocan.py ===")
    say("    (recorded: full PERSON TP=2013 FP=3117 FN=72; es-only 1901/2110/184; es-ner 1693/1112/392)")
    sanity = {}
    for rule in ("union", "dict-only (layer alone)", "ner-only (layer alone)"):
        m, _, _, _ = run_config((rule, "presidio", None), [], rows["presidio"])
        sanity[rule] = m
        say(f"  {rule:32s} TP={m['tp']} FP={m['fp']} FN={m['fn']}  P={m['P']:.3f} R={m['R']:.3f}")
    results["sanity"] = sanity
    say()

    # ---- B. presence-pattern diagnostic ----
    say("=== B. Presence-pattern diagnostic (all 520 docs, beam NER confidence) ===")
    pt = pattern_table(rows["beam"])
    results["pattern_table"] = pt
    for k, d in pt.items():
        au = d["ner_conf_auroc_within_pattern"]
        say(f"  {k:9s} n={d['n']:5d} true={d['true']:5d} precision={d['precision']:.3f}"
            + (f"  NER conf true/false={d['ner_conf_mean_true']:.3f}/{d['ner_conf_mean_false']:.3f}"
               f"  within-pattern AUROC={au:.3f}" if au is not None else ""))
    say()

    # ---- C. primary split ----
    say("=== C. PRIMARY: calibrate on train+validation (400 docs), test on test (120 docs) ===")
    calib = {ns: [r for r in rows[ns] if r["split"] != "test"] for ns in rows}
    test = {ns: [r for r in rows[ns] if r["split"] == "test"] for ns in rows}
    primary, per_doc_store = {}, {}
    base = None
    for cfg in configs():
        m, pat, per_doc, model = run_config(cfg, calib[cfg[1]], test[cfg[1]])
        if cfg[0] == "union":
            base = m
        primary[cfg[0]] = {"metrics": m, "test_patterns": pat,
                           "model": model.summary() if model else None}
        per_doc_store[cfg[0]] = per_doc
        say(f"  {cfg[0]:52s} P={m['P']:.3f} ({m['P']-base['P']:+.3f})  R={m['R']:.3f} "
            f"({m['R']-base['R']:+.3f})  F1={m['F1']:.3f} ({m['F1']-base['F1']:+.3f})  F2={m['F2']:.3f}")
    results["primary"] = primary
    say()

    say("  Per-pattern thresholds and test-set recall among true candidates (coverage claims >= 1-alpha):")
    for name in primary:
        mdl = primary[name]["model"]
        if not mdl or not name.startswith("PCCF coverage") or "nested=False" not in name:
            continue
        cells = []
        for k, d in primary[name]["test_patterns"].items():
            thr = mdl["patterns"].get(k, {}).get("threshold")
            rec = d["kept_true"] / d["true"] if d["true"] else float("nan")
            cells.append(f"{k}: q={thr if thr is None or thr == float('inf') else round(thr, 3)} "
                         f"recall={rec:.3f}")
        say(f"    {name:48s} " + " | ".join(cells))
    say()
    say("  Precision-mode pattern decisions (threshold -inf = pattern dropped; test precision of kept):")
    for name in primary:
        mdl = primary[name]["model"]
        if not mdl or not name.startswith("PCCF precision") or "score=beam" not in name:
            continue
        cells = []
        for k, d in primary[name]["test_patterns"].items():
            thr = mdl["patterns"].get(k, {}).get("threshold")
            pk = d["kept_true"] / d["kept"] if d["kept"] else float("nan")
            cp = mdl["patterns"].get(k, {}).get("calib_precision_all")
            cells.append(f"{k}: calibP={cp:.3f} q={thr if thr in (None, float('inf'), -float('inf')) else round(thr, 3)} "
                         f"kept={d['kept']}/{d['n']} testP={pk:.3f}")
        say(f"    {name:48s} " + " | ".join(cells))
    say()

    say("  Paired bootstrap over the 120 test docs (2,000 resamples), pre-specified configs vs union:")
    boots = {}
    for name in sorted(PRIMARY):
        b = bootstrap(per_doc_store["union"], per_doc_store[name])
        boots[name] = b
        say(f"    {name:48s} dP 95%CI=[{b['dP_95ci'][0]:+.3f},{b['dP_95ci'][1]:+.3f}]  "
            f"dR=[{b['dR_95ci'][0]:+.3f},{b['dR_95ci'][1]:+.3f}]  "
            f"dF1=[{b['dF1_95ci'][0]:+.3f},{b['dF1_95ci'][1]:+.3f}]")
    results["bootstrap"] = boots
    say()

    # ---- D. 5-fold CV ----
    say("=== D. ROBUSTNESS: 5-fold document-level CV over all 520 docs (seed 0), deltas vs union ===")
    rng = random.Random(0)
    ids = [r["id"] for r in rows["beam"]]
    rng.shuffle(ids)
    folds = [set(ids[i::5]) for i in range(5)]
    cv = {}
    for cfg in configs():
        if cfg[0] == "union":
            continue
        dP, dR, dF = [], [], []
        for fset in folds:
            cal = [r for r in rows[cfg[1]] if r["id"] not in fset]
            tst = [r for r in rows[cfg[1]] if r["id"] in fset]
            mu, _, _, _ = run_config(("union", "presidio", None), [], tst)
            m, _, _, _ = run_config(cfg, cal, tst)
            dP.append(m["P"] - mu["P"])
            dR.append(m["R"] - mu["R"])
            dF.append(m["F1"] - mu["F1"])
        cv[cfg[0]] = {"dP": dP, "dR": dR, "dF1": dF}
        say(f"  {cfg[0]:52s} dP={statistics.mean(dP):+.3f}±{statistics.stdev(dP):.3f}  "
            f"dR={statistics.mean(dR):+.3f}±{statistics.stdev(dR):.3f}  "
            f"dF1={statistics.mean(dF):+.3f}±{statistics.stdev(dF):.3f}")
    results["cv"] = cv

    with open(RESULTS_TXT, "w") as f:
        f.write("\n".join(lines) + "\n")
    with open(RESULTS_JSON, "w") as f:
        json.dump(results, f, indent=1, default=lambda o: None if o in (float("inf"), -float("inf")) else str(o))
    print(f"\nwrote {RESULTS_TXT} and {RESULTS_JSON}")


if __name__ == "__main__":
    main()
