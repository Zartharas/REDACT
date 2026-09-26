"""PCCF phase 7: PCCF vs simpler thresholding baselines (PCCF_PHASE7_PREREGISTRATION.md).
Reuses phase-5 caches/splits/scorer. Resumable per row: results/<row>.json.
  PCCF_PHASE5_CACHE=<cache> python pccf_phase7_baselines.py --budget 40   (repeat until 'complete')
  python pccf_phase7_baselines.py --judge
"""
import argparse, collections, json, math, os, statistics, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "phase5"))
_argv, sys.argv = sys.argv, [sys.argv[0]]
import pccf_phase5_all_languages as P5  # noqa: E402
sys.argv = _argv
pccf, ctx, H, P2 = P5.pccf, P5.ctx, P5.H, P5.P2

ROWS = ["fr", "ru", "de", "it", "nl", "pt", "fi", "tr", "hi", "te", "ar_wiki", "ja", "id", "id_hf", "ko", "zh", "in",
        "ko_kdpii", "tr_mit", "ko_klue", "bg", "pl", "cs", "lt", "et", "sv", "sk", "lv", "hu", "ro", "el", "da", "sl",
        "hr", "sr", "vi", "ms", "tl"]
GL_ROWS = ["bg_gl", "cs_gl", "et_gl", "sk_gl", "lv_gl", "hu_gl", "sr_gl", "vi_gl", "ms_gl", "tl_gl"]  # phase 7b
OUT = os.path.join(HERE, "results")
A = 0.05


def floor(nc, nt):
    return 1 - A - 2 * math.sqrt(A * (1 - A) * (1 / max(nc, 1) + 1 / nt))


def prepare(lang):
    """Same row construction as phase-5 evaluate()."""
    docs = P5.load(lang)
    cache = json.load(open(os.path.join(P5.CACHE_DIR, f"{lang}.json")))
    cues = P5.GENERIC_CUES
    rows = []
    for d in docs:
        e = cache.get(d["id"])
        if e is None or "error" in e:
            continue
        hits = H.layer_hits(e, "beam")
        cands = pccf.build_candidates(hits, "PERSON")
        g = P5.gold(d)
        rows.append({"id": d["id"], "split": d["split"], "text": d["text"], "hits": hits, "cands": cands, "gold": g,
                     "labels": [H.label(c, g) for c in cands], "feats": e["ud"], "ud": e["ud"]})
    return P5.splits(rows)


def run_row(lang):
    fit, cal, tst = prepare(lang)
    ys = [y for r in fit for y in r["labels"]]
    sc = ctx.LogisticScorer(l2=1.0).fit([f for r in fit for f in r["feats"]], ys)
    cs, ts = P2.score_rows(cal, sc), P2.score_rows(tst, sc)
    cc = [c for r in cs for c in r["cands"]]
    cy = [y for r in cs for y in r["labels"]]
    R = [pccf.joint_score(c) for c in cc]
    # B1: F1-tuned single threshold on calibration candidates
    best, bt = -1, 1.0
    npos = sum(cy)
    order = sorted(zip(R, cy))
    tp = fp = 0
    for i, (r, y) in enumerate(order):
        tp += y
        fp += 1 - y
        if i + 1 < len(order) and order[i + 1][0] == r:
            continue
        f1 = 2 * tp / (tp + fp + npos) if npos else 0
        if f1 > best:
            best, bt = f1, r
    gm = pccf.PCCF(mode="coverage", alpha=A, partition="global").fit(cc, cy)
    mm = pccf.PCCF(mode="coverage", alpha=A, min_group_true=19, fallback="keep").fit(cc, cy)
    methods = {"B0 union": lambda c: True,
               "B1 F1-tuned": lambda c, t=bt: pccf.joint_score(c) <= t,
               "B2 global conformal": gm.accept,
               "B3 p>=0.5": lambda c: pccf.joint_score(c) <= 0.5,
               "PCCF": mm.accept}
    cal_true = collections.Counter("+".join(sorted(c.mask)) for c, y in zip(cc, cy) if y)
    out = {"cal_true": dict(cal_true), "cal_true_total": sum(cal_true.values()), "b1_threshold": bt}
    for name, acc in methods.items():
        pd = P2.apply_rule(ts, acc)
        m = P2.metrics(pd)
        ch = P2.pattern_checks(pd, None)
        nt = sum(d["n_true"] for d in ch.values())
        kept_true = sum(round(d["cand_recall"] * d["n_true"]) for d in ch.values() if d["n_true"])
        out[name] = {"P": m["P"], "R": m["R"],
                     "groups": {k: {"n_test": d["n_true"], "recall": d["cand_recall"]} for k, d in ch.items()},
                     "marginal_recall": kept_true / nt if nt else None, "n_test_true": nt}
    return out


def run(budget, rows=None):
    os.makedirs(OUT, exist_ok=True)
    t0 = time.perf_counter()
    for lang in rows or ROWS:
        p = os.path.join(OUT, f"{lang}.json")
        if os.path.exists(p):
            continue
        if time.perf_counter() - t0 > budget:
            print("run again"); return
        json.dump(run_row(lang), open(p, "w"))
        print(f"  {lang} done ({time.perf_counter() - t0:.0f}s)", flush=True)
    print("complete")


def judge(rows=None, outname="PCCF_PHASE7_RESULTS.md", title="# PCCF phase 7 results: PCCF vs simpler thresholding\n"):
    L = []
    say = lambda s="": (print(s), L.append(s))  # noqa: E731
    res = {l: json.load(open(os.path.join(OUT, f"{l}.json"))) for l in (rows or ROWS)}
    meths = ["B0 union", "B1 F1-tuned", "B2 global conformal", "B3 p>=0.5", "PCCF"]
    viol = {m: [0, 0] for m in meths}
    marg = [0, 0]
    dP = {m: [] for m in meths}
    dR = {m: [] for m in meths}
    b1_trade = 0
    per_row = []
    for l, r in res.items():
        u = r["B0 union"]
        cells = [g for g, d in r["PCCF"]["groups"].items() if d["n_test"] >= 19]
        line = [l]
        for m in meths:
            dP[m].append(r[m]["P"] - u["P"]); dR[m].append(r[m]["R"] - u["R"])
            v = 0
            for g in cells:
                d = r[m]["groups"][g]
                bad = d["recall"] < floor(r["cal_true"].get(g, 0), d["n_test"])
                viol[m][0] += bad; viol[m][1] += 1; v += bad
            line.append(f"{r[m]['P']:.3f}/{r[m]['R']:.3f}" + (f" ({v}✗)" if v else ""))
        b2 = r["B2 global conformal"]
        marg[1] += 1
        marg[0] += b2["marginal_recall"] >= floor(r["cal_true_total"], b2["n_test_true"])
        b1, pc = r["B1 F1-tuned"], r["PCCF"]
        b1_trade += b1["P"] > pc["P"] and b1["R"] < pc["R"]
        per_row.append("| " + " | ".join(line) + " |")
    rate = {m: viol[m][0] / viol[m][1] for m in meths}
    say(title)
    say(f"Cells (row × group with n_true_test ≥ 19): {viol['PCCF'][1]} across {len(res)} rows.\n")
    say("| method | floor violations | rate | mean ΔP vs union | mean ΔR vs union |")
    say("|---|---|---|---|---|")
    for m in meths:
        say(f"| {m} | {viol[m][0]}/{viol[m][1]} | {rate[m]:.1%} | {statistics.mean(dP[m]):+.3f} | {statistics.mean(dR[m]):+.3f} |")
    h30 = rate["PCCF"] <= 0.10 and rate["B1 F1-tuned"] - rate["PCCF"] >= 0.10 and rate["B3 p>=0.5"] - rate["PCCF"] >= 0.10
    h31 = marg[0] / marg[1] >= 0.90 and rate["B2 global conformal"] - rate["PCCF"] >= 0.05
    say(f"\nB2 marginal recall check met in {marg[0]}/{marg[1]} rows.")
    say(f"B1 has higher precision AND lower recall than PCCF in {b1_trade}/{len(res)} rows.\n")
    say("## Per row: P/R (✗ = floor violations in that row)\n")
    say("| row | " + " | ".join(meths) + " |")
    say("|" + "---|" * (len(meths) + 1))
    for x in per_row:
        say(x)
    say(f"\n=== Verdicts (mechanical) ===\n  H30: {'SUPPORTED' if h30 else 'NOT SUPPORTED'}\n  H31: {'SUPPORTED' if h31 else 'NOT SUPPORTED'}")
    open(os.path.join(HERE, "..", "..", "..", outname), "w").write("\n".join(L) + "\n")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--budget", type=float, default=40)
    ap.add_argument("--judge", action="store_true")
    ap.add_argument("--b", action="store_true", help="phase 7b: all 48 rows (38 + GLiNER)")
    a = ap.parse_args()
    rows = ROWS + GL_ROWS if a.b else None
    if a.judge:
        judge(rows, *(("PCCF_PHASE7B_RESULTS.md", "# PCCF phase 7b results: 48 rows (38 + 10 GLiNER)\n") if a.b else ()))
    else:
        run(a.budget, rows)
