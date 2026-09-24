"""
Phase 4, F5/H21 (PCCF_PHASE4_PREREGISTRATION.md): PCCF on the LARGER
FR/RU samples fetched by fetch_large_multilang.sh (datasets/large/).
Protocol:
  FR: OpenPII-FR train rows split 50/50 by document into fit/calibrate; test on validation rows.
  RU: documents split (seed 0) 25% fit / 25% calibrate / 50% test.
Scorers: rule (per-language cues, no fitting), LR with hand cues, LR-UD
(src/pccf_ud.py, no word lists). All use coverage a=0.05 with the F1
small-group fallback (min_group_true=19).
  python .../pccf_phase4_multilang_large.py --build-cache [--data-dir D]   # resumable
  python .../pccf_phase4_multilang_large.py [--data-dir D]
"""
import argparse
import hashlib
import json
import os
import random
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
RD = os.path.dirname(HERE)
ROOT = os.path.abspath(os.path.join(RD, "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, RD)

import pccf  # noqa: E402
import pccf_context as ctx  # noqa: E402
import pccf_feasibility_meddocan as H  # noqa: E402
import pccf_phase2_meddocan as P2  # noqa: E402

DEFAULT_DIR = os.path.join(RD, "datasets", "large")
LANGS = {
    "fr": {"file": "OpenPII_FR_large.jsonl", "eval": "evaluate_fr", "det": ("fr_detect", "scan_french_names"),
           "ner": ("fr_ner", "scan_fr_ner"), "model": "fr_core_news_md", "cues": "FR_CUES"},
    "ru": {"file": "RU_PII_large.jsonl", "eval": "evaluate_ru", "det": ("ru_detect", "scan_russian_names"),
           "ner": ("ru_ner", "scan_ru_ner"), "model": "ru_core_news_md", "cues": "RU_CUES"},
}
HYP = """H21: per language, in-language calibration keeps per-group candidate recall >= 0.95 - tol for every group with
     n_true >= 19, AND LR-UD coverage a=0.05 gains >= +0.03 precision over union."""


def load(data_dir, lang):
    p = os.path.join(data_dir, LANGS[lang]["file"])
    if not os.path.exists(p):
        return None
    return [json.loads(l) for l in open(p, encoding="utf-8") if l.strip()]


def cache_path(data_dir):
    tag = hashlib.sha1(os.path.abspath(data_dir).encode()).hexdigest()[:8]
    return os.path.join(ROOT, "output", f"pccf_phase4_multilang_{tag}.json")


def build_cache(data_dir, budget=150):
    import spacy
    import ner_confidence
    import pccf_ud
    t0 = time.perf_counter()
    cp = cache_path(data_dir)
    cache = json.load(open(cp)) if os.path.exists(cp) else {}
    total = done = 0
    for lang, cfg in LANGS.items():
        docs = load(data_dir, lang)
        if not docs:
            print(f"  {lang}: no data (missing or empty file in {data_dir} (run fetch_large_multilang.sh)")
            continue
        det = getattr(__import__(cfg["det"][0]), cfg["det"][1])
        ner = getattr(__import__(cfg["ner"][0]), cfg["ner"][1])
        nlp = None
        total += len(docs)
        for d in docs:
            k = f"{lang}:{d['id']}"
            if k in cache:
                continue
            if time.perf_counter() - t0 > budget:
                break
            nlp = nlp or spacy.load(cfg["model"])
            t = d["text"]
            nh = ner_confidence.annotate(ner(t), t, cfg["model"], "PER")
            e = {"dict": [[h["start"], h["end"]] for h in det(t) if h["type"] == "PERSON"],
                 "ner": [[h["start"], h["end"], round(h["confidence"], 6)] for h in nh]}
            cands = pccf.build_candidates(H.layer_hits(e, "beam"), "PERSON")
            doc = nlp(t)
            e["ud"] = [pccf_ud.ud_features(doc, t, c) for c in cands]
            cache[k] = e
            done += 1
    json.dump(cache, open(cp, "w"))
    print(f"  cached {done} docs this call; {len(cache)}/{total}" + ("  -> complete" if len(cache) >= total else "  -> run again"))


def rows_for(lang, docs, cache):
    cfg = LANGS[lang]
    ev = __import__(cfg["eval"])
    cues = getattr(ctx, cfg["cues"])
    rows = []
    for d in docs:
        e = cache[f"{lang}:{d['id']}"]
        hits = H.layer_hits(e, "beam")
        cands = pccf.build_candidates(hits, "PERSON")
        gold = [g for g in ev.gold_spans(d) if g["type"] == "PERSON"]
        rows.append({"id": d["id"], "split": d["split"], "text": d["text"], "hits": hits, "cands": cands, "gold": gold,
                     "labels": [H.label(c, gold) for c in cands],
                     "feats": [ctx.features(d["text"], c, cues) for c in cands], "ud": e["ud"]})
    return rows


def splits(lang, rows):
    rng = random.Random(0)
    if lang == "fr":
        tr = [r for r in rows if r["split"] == "train"]
        rng.shuffle(tr)
        return tr[: len(tr) // 2], tr[len(tr) // 2:], [r for r in rows if r["split"] != "train"]
    rows = rows[:]
    rng.shuffle(rows)
    q = len(rows) // 4
    return rows[:q], rows[q:2 * q], rows[2 * q:]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--build-cache", action="store_true")
    ap.add_argument("--data-dir", default=DEFAULT_DIR)
    ap.add_argument("--out", default=os.path.join(HERE, "pccf_phase4_multilang_large_results"))
    a = ap.parse_args()
    if a.build_cache:
        build_cache(a.data_dir)
        return
    lines, res = [], {}
    say = lambda s="": (print(s), lines.append(s))  # noqa: E731
    say(HYP)
    say(f"data dir: {a.data_dir}")
    say()
    cache = json.load(open(cache_path(a.data_dir)))
    ok_all = True
    for lang in LANGS:
        docs = load(a.data_dir, lang)
        if not docs:
            say(f"=== {lang}: no data (missing or empty file) ===")
            ok_all = False
            continue
        rows = rows_for(lang, docs, cache)
        fit, cal, tst = splits(lang, rows)
        say(f"=== {lang.upper()}: {len(rows)} docs (fit {len(fit)} / calibrate {len(cal)} / test {len(tst)}) ===")
        for k, d in H.pattern_table(tst).items():
            say(f"  test pattern {k:9s} n={d['n']:5d} true={d['true']:5d} precision={d['precision']:.3f}")
        mu = P2.metrics(P2.apply_rule(tst, lambda c: True))
        say(f"  {'union':26s} P={mu['P']:.3f} R={mu['R']:.3f}")
        out = {"union": mu}
        variants = {"rule (lang cues)": (ctx.RuleScorer(), "feats"),
                    "LR hand cues": ("fit", "feats"), "LR-UD": ("fit", "ud")}
        lang_ok = True
        for name, (sc, fk) in variants.items():
            re_ = lambda rs: [{**r, "feats": r[fk]} for r in rs]  # noqa: E731
            f2, c2, t2 = re_(fit), re_(cal), re_(tst)
            if sc == "fit":
                ys = [y for r in f2 for y in r["labels"]]
                if len(set(ys)) < 2:
                    say(f"  {name:26s} skipped: fit split has {len(ys)} candidates / {len(set(ys))} class(es)")
                    if name == "LR-UD":
                        lang_ok = False
                    continue
                sc = ctx.LogisticScorer(l2=1.0).fit([f for r in f2 for f in r["feats"]], [y for r in f2 for y in r["labels"]])
            cs, ts = P2.score_rows(c2, sc), P2.score_rows(t2, sc)
            model = pccf.PCCF(mode="coverage", alpha=0.05, min_group_true=19).fit(
                [c for r in cs for c in r["cands"]], [y for r in cs for y in r["labels"]])
            pd = P2.apply_rule(ts, model.accept)
            m = P2.metrics(pd)
            ch = P2.pattern_checks(pd, model)
            fb = {"+".join(sorted(k)): st["fallback"] for k, st in model.stats.items()}
            floors = all(d["cand_recall"] >= 0.95 - 2 * (0.05 * 0.95 / d["n_true"]) ** 0.5
                         for d in ch.values() if d["n_true"] >= 19)
            if name == "LR-UD":
                lang_ok = floors and (m["P"] - mu["P"]) >= 0.03
            out[name] = {**m, "checks": ch, "fallback": fb, "floors_hold": floors}
            say(f"  {name:26s} P={m['P']:.3f} ({m['P']-mu['P']:+.3f}) R={m['R']:.3f} ({m['R']-mu['R']:+.3f}) "
                f"floors={'ok' if floors else 'FAIL'} fallback={fb}")
        res[lang] = out
        ok_all &= lang_ok
        say()
    say("=== Verdicts (mechanical) ===")
    say(f"  H21: {'SUPPORTED' if ok_all else 'NOT SUPPORTED'}")
    res["verdicts"] = {"H21": ok_all}
    open(a.out + ".txt", "w").write("\n".join(lines) + "\n")
    json.dump(res, open(a.out + ".json", "w"), indent=1, default=str)


if __name__ == "__main__":
    main()
