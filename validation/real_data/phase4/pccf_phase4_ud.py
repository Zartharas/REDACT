"""
Phase 4, F4 (PCCF_PHASE4_PREREGISTRATION.md): language-agnostic UD
features (src/pccf_ud.py) vs the hand cue lists.
  H20: T2 Spanish free-text protocol (pccf_phase3_freetext_es.py's corpus and split).
  descriptive: MEDDOCAN phase-2 protocol.
  python .../pccf_phase4_ud.py --build-cache   # parses docs with es_core_news_md; resumable
  python .../pccf_phase4_ud.py
"""
import argparse
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
RD = os.path.dirname(HERE)
ROOT = os.path.abspath(os.path.join(RD, "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, RD)

import evaluate_meddocan as em  # noqa: E402
import pccf_context as ctx  # noqa: E402
import pccf_feasibility_meddocan as H  # noqa: E402
import pccf_phase2_meddocan as P2  # noqa: E402
import pccf_phase3_freetext_es as T2  # noqa: E402

CACHE = os.path.join(ROOT, "output", "pccf_phase4_ud_cache.json")
OUT_TXT = os.path.join(HERE, "pccf_phase4_ud_results.txt")
OUT_JSON = os.path.join(HERE, "pccf_phase4_ud_results.json")
HYP = """H20: on the T2 test narrative, LR-UD coverage a=0.05 recovers >= 70% of LR-refit's precision gain over union
     (LR-refit: +0.252 in phase 3), with per-pattern candidate recall >= 0.95 - tol (n_true >= 19).
MEDDOCAN phase-2 protocol: descriptive."""


def corpora():
    t2 = T2.narrative_rows(json.load(open(T2.CACHE))["docs"])
    mdocs = {d["id"]: d for d in em.load_docs()}
    med = P2.fresh_rows(json.load(open(H.CACHE))["docs"], mdocs)
    return {"t2": t2, "meddocan": med}


def build_cache(budget=150):
    import spacy
    nlp = spacy.load(H.MODEL)
    t0 = time.perf_counter()
    cache = json.load(open(CACHE)) if os.path.exists(CACHE) else {}
    done = 0
    for name, rows in corpora().items():
        for r in rows:
            k = f"{name}:{r['id']}"
            if k in cache:
                continue
            if time.perf_counter() - t0 > budget:
                break
            doc = nlp(r["text"])
            import pccf_ud
            cache[k] = [pccf_ud.ud_features(doc, r["text"], c) for c in r["cands"]]
            done += 1
    json.dump(cache, open(CACHE, "w"))
    total = sum(len(v) for v in corpora().values())
    print(f"  parsed {done} docs this call; {len(cache)}/{total} cached" + ("  -> complete" if len(cache) >= total else ""))


def with_ud(rows, name, cache):
    return [{**r, "feats": cache[f"{name}:{r['id']}"]} for r in rows]


def evaluate(name, rows, fit, cal, tst, say, key_scorers):
    mu = P2.metrics(P2.apply_rule(tst, lambda c: True))
    out = {"union": mu}
    say(f"  {'union':30s} P={mu['P']:.3f} R={mu['R']:.3f}")
    checks = {}
    for sname, (fitrows, calrows, tstrows) in key_scorers.items():
        sc = ctx.LogisticScorer(l2=1.0).fit([f for r in fitrows for f in r["feats"]], [y for r in fitrows for y in r["labels"]])
        cs, ts = P2.score_rows(calrows, sc), P2.score_rows(tstrows, sc)
        model = P2.fit_pccf(cs, dict(mode="coverage", alpha=0.05))
        pd = P2.apply_rule(ts, model.accept)
        m = P2.metrics(pd)
        out[sname] = m
        checks[sname] = P2.pattern_checks(pd, model)
        say(f"  {sname:30s} P={m['P']:.3f} ({m['P']-mu['P']:+.3f}) R={m['R']:.3f} ({m['R']-mu['R']:+.3f})  "
            + " | ".join(f"{k}: rec={d['cand_recall']:.3f} n_true={d['n_true']}" for k, d in sorted(checks[sname].items())
                         if d["cand_recall"] is not None))
    return out, checks


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--build-cache", action="store_true")
    if ap.parse_args().build_cache:
        build_cache()
        return
    lines, res = [], {}
    say = lambda s="": (print(s), lines.append(s))  # noqa: E731
    say(HYP)
    say()
    cache = json.load(open(CACHE))
    cor = corpora()
    for name in ("t2", "meddocan"):
        rows = cor[name]
        ud = with_ud(rows, name, cache)
        sp = lambda rs, s: [r for r in rs if r["split"] == s]  # noqa: E731
        fit_h, cal_h, tst_h = sp(rows, "train"), sp(rows, "validation"), sp(rows, "test")
        fit_u, cal_u, tst_u = sp(ud, "train"), sp(ud, "validation"), sp(ud, "test")
        say(f"=== {name}: fit=train, calibrate=validation, test=test; coverage a=0.05 ===")
        out, checks = evaluate(name, rows, fit_h, cal_h, tst_h, say, {
            "LR hand cues (phase 2/3)": (fit_h, cal_h, tst_h),
            "LR-UD (no word lists)": (fit_u, cal_u, tst_u),
        })
        res[name] = {"metrics": out, "checks": checks}
        say()
    t = res["t2"]["metrics"]
    gain_h = t["LR hand cues (phase 2/3)"]["P"] - t["union"]["P"]
    gain_u = t["LR-UD (no word lists)"]["P"] - t["union"]["P"]
    floor = all(d["cand_recall"] >= 0.95 - 2 * (0.05 * 0.95 / d["n_true"]) ** 0.5
                for d in res["t2"]["checks"]["LR-UD (no word lists)"].values() if d["n_true"] >= 19)
    h20 = gain_h > 0 and gain_u >= 0.7 * gain_h and floor
    say(f"  T2 precision gain: hand cues {gain_h:+.3f}, UD {gain_u:+.3f} ({(gain_u/gain_h if gain_h else float('nan')):.0%} recovered); floors hold: {floor}")
    say("=== Verdicts (mechanical) ===")
    say(f"  H20: {'SUPPORTED' if h20 else 'NOT SUPPORTED'}")
    res["verdicts"] = {"H20": h20}
    open(OUT_TXT, "w").write("\n".join(lines) + "\n")
    json.dump(res, open(OUT_JSON, "w"), indent=1, default=str)


if __name__ == "__main__":
    main()
