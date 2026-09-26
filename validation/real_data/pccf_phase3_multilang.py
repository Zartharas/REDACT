"""
PCCF phase 3, T1 + T4 (PCCF_PHASE3_PREREGISTRATION.md): French/Russian audit
and redaction-tier transfer. Additive. It imports evaluate_fr.py,
evaluate_ru.py, fr_/ru_detect.py, fr_/ru_ner.py and the phase-1/2 harnesses,
and modifies none of them.

  python validation/real_data/pccf_phase3_multilang.py --build-cache   # needs fr/ru_core_news_md + pymorphy2
  python validation/real_data/pccf_phase3_multilang.py
"""
import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, HERE)

import evaluate_meddocan as em  # noqa: E402
import pccf  # noqa: E402
import pccf_context as ctx  # noqa: E402
import pccf_feasibility_meddocan as H  # noqa: E402
import pccf_phase2_meddocan as P2  # noqa: E402

CACHE = os.path.join(ROOT, "output", "pccf_phase3_multilang_cache.json")
OUT_TXT = os.path.join(HERE, "pccf_phase3_multilang_results.txt")
OUT_JSON = os.path.join(HERE, "pccf_phase3_multilang_results.json")

HYP = """T1 (descriptive) + D1: in-language calibration infeasible for any pattern with < 19 true candidates.
T4 H10: with MEDDOCAN-calibrated rule-scorer thresholds (coverage a=0.05), per-pattern candidate
    recall on FR/RU >= 0.95 - tol for every pattern with n_true >= 5."""

LANGS = {
    "fr": {"eval": "evaluate_fr", "det": ("fr_detect", "scan_french_names"),
           "ner": ("fr_ner", "scan_fr_ner"), "model": "fr_core_news_md", "cues": "FR_CUES"},
    "ru": {"eval": "evaluate_ru", "det": ("ru_detect", "scan_russian_names"),
           "ner": ("ru_ner", "scan_ru_ner"), "model": "ru_core_news_md", "cues": "RU_CUES"},
}


def _mod(name):
    return __import__(name)


def build_cache():
    import ner_confidence
    out = {}
    for lang, cfg in LANGS.items():
        ev = _mod(cfg["eval"])
        det = getattr(_mod(cfg["det"][0]), cfg["det"][1])
        ner = getattr(_mod(cfg["ner"][0]), cfg["ner"][1])
        entries = []
        for d in ev.load_docs():
            t = d["text"]
            nh = ner_confidence.annotate(ner(t), t, cfg["model"], "PER")
            entries.append({"id": d["id"], "split": d.get("split", ""),
                            "dict": [[h["start"], h["end"]] for h in det(t) if h["type"] == "PERSON"],
                            "ner": [[h["start"], h["end"], round(h["confidence"], 6)] for h in nh]})
        out[lang] = entries
        print(f"  {lang}: {len(entries)} docs cached", flush=True)
    os.makedirs(os.path.dirname(CACHE), exist_ok=True)
    json.dump(out, open(CACHE, "w"))
    print(f"wrote {CACHE}")


def rows_for(lang, entries):
    cfg = LANGS[lang]
    ev = _mod(cfg["eval"])
    cues = getattr(ctx, cfg["cues"])
    docs = {d["id"]: d for d in ev.load_docs()}
    rows = []
    for e in entries:
        d = docs[e["id"]]
        hits = H.layer_hits(e, "beam")
        cands = pccf.build_candidates(hits, "PERSON")
        gold = [g for g in ev.gold_spans(d) if g["type"] == "PERSON"]
        rows.append({"id": e["id"], "text": d["text"], "hits": hits, "cands": cands, "gold": gold,
                     "labels": [H.label(c, gold) for c in cands],
                     "feats": [ctx.features(d["text"], c, cues) for c in cands]})
    return rows


def gold_slice(text, g):
    if ctx.field_label(text, g["start"])[0] != "none":
        return "label-prefix"
    if text[g["end"]:g["end"] + 3].lstrip().startswith(":"):
        return "speaker-tag"
    return "free-text"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--build-cache", action="store_true")
    if ap.parse_args().build_cache:
        build_cache()
        return
    lines, results = [], {}
    say = lambda s="": (print(s), lines.append(s))  # noqa: E731
    say(HYP)
    say()
    cache = json.load(open(CACHE))

    # MEDDOCAN-calibrated rule-scorer thresholds (Spanish cues), coverage a=0.05
    mcache = json.load(open(H.CACHE))
    mdocs = {d["id"]: d for d in em.load_docs()}
    mrows = P2.fresh_rows(mcache["docs"], mdocs)
    mcal = P2.score_rows([r for r in mrows if r["split"] == "validation"], ctx.RuleScorer())
    model = P2.fit_pccf(mcal, dict(mode="coverage", alpha=0.05))
    say(f"MEDDOCAN-calibrated thresholds (rule scorer, a=0.05): "
        + ", ".join(f"{'+'.join(sorted(k))}={v:.3f}" for k, v in model.thresholds.items()))
    say()

    verdict_h10 = True
    for lang in LANGS:
        rows = rows_for(lang, cache[lang])
        res = {}
        say(f"=== {lang.upper()} ({len(rows)} docs) ===")
        sl = {}
        for r in rows:
            for g in r["gold"]:
                k = gold_slice(r["text"], g)
                sl[k] = sl.get(k, 0) + 1
        say(f"  T1 gold PERSON spans by slice: {sl} (total {sum(sl.values())})")
        res["slices"] = sl

        pt = H.pattern_table(rows)
        for k, d in pt.items():
            say(f"  pattern {k:9s} n={d['n']:4d} true={d['true']:4d} precision={d['precision']:.3f}")
        res["patterns"] = pt
        d1 = {k: ("feasible" if d["true"] >= 19 else "INFEASIBLE") for k, d in pt.items()}
        say(f"  D1 in-language calibration per pattern: {d1}")
        res["D1"] = d1

        base = {}
        for rule, acc in [("union", lambda c: True),
                          ("intersection", lambda c: c.mask == frozenset({"dict", "ner"}))]:
            base[rule] = P2.metrics(P2.apply_rule(rows, acc))
        for layer in ("dict", "ner"):
            base[f"{layer} alone"] = H.score([(list(r["hits"][layer]), r["gold"]) for r in rows])
        for k, m in base.items():
            say(f"  {k:14s} P={m['P']:.3f} R={m['R']:.3f} (TP={m['tp']} FP={m['fp']} FN={m['fn']})")
        res["baselines"] = base

        # T4: rule scorer with this language's cues, thresholds from MEDDOCAN
        srows = P2.score_rows(rows, ctx.RuleScorer())
        pd = P2.apply_rule(srows, model.accept)
        m = P2.metrics(pd)
        say(f"  T4 rule/{lang} cues + MEDDOCAN thresholds: P={m['P']:.3f} R={m['R']:.3f} "
            f"(dP={m['P']-base['union']['P']:+.3f} dR={m['R']-base['union']['R']:+.3f})")
        chk = P2.pattern_checks(pd, model)
        for k, d in sorted(chk.items()):
            ok = None
            if d["n_true"] >= 5:
                tol = 2 * (0.05 * 0.95 / d["n_true"]) ** 0.5
                ok = d["cand_recall"] >= 0.95 - tol
                verdict_h10 &= ok
            say(f"    {k:9s} n_true={d['n_true']:3d} cand_recall={d['cand_recall'] if d['cand_recall'] is None else round(d['cand_recall'], 3)} "
                f"kept={d['kept']} H10-check={'n/a (<5)' if ok is None else ('pass' if ok else 'FAIL')}")
        # descriptive: does the rule score separate true/false inside patterns here?
        au = {}
        for pat in ("dict", "ner", "dict+ner"):
            pos = [1 - c.scores[next(iter(c.scores))] for r in srows for c, y in zip(r["cands"], r["labels"])
                   if "+".join(sorted(c.mask)) == pat and y]
            neg = [1 - c.scores[next(iter(c.scores))] for r in srows for c, y in zip(r["cands"], r["labels"])
                   if "+".join(sorted(c.mask)) == pat and not y]
            au[pat] = H.auroc(pos, neg)
        say(f"  rule-score AUROC within pattern: " + ", ".join(
            f"{k}={'n/a' if v is None else round(v, 3)}" for k, v in au.items()))
        res["T4"] = {"metrics": m, "checks": chk, "auroc": au}
        results[lang] = res
        say()

    say(f"H10: {'SUPPORTED' if verdict_h10 else 'NOT SUPPORTED'}")
    results["H10"] = verdict_h10
    open(OUT_TXT, "w").write("\n".join(lines) + "\n")
    json.dump(results, open(OUT_JSON, "w"), indent=1, default=str)
    print(f"wrote {OUT_TXT}")


if __name__ == "__main__":
    main()
