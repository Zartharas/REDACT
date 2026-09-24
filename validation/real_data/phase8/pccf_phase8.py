"""PCCF phase 8 (PCCF_PHASE8_PREREGISTRATION.md): clustered small groups (CLUST),
weighted conformal (WCP) and adaptive conformal (ACI) vs PCCF, on the phase-5
caches with the original seed-0 splits. Resumable per row: results/<row>.json.
  PCCF_PHASE5_CACHE=<cache> python pccf_phase8.py --budget 40   (repeat until 'complete')
  python pccf_phase8.py --judge
"""
import argparse, bisect, collections, json, math, os, statistics, sys, time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "phase7"))
import pccf_phase7_baselines as P7  # noqa: E402

pccf, ctx, P2 = P7.pccf, P7.ctx, P7.P2
ROWS = P7.ROWS + P7.GL_ROWS
KNOWN = {("it", "ner"), ("hi", "ner"), ("id_hf", "ner"), ("tl_gl", "ner")}
OUT = os.path.join(HERE, "results")
A, MIN_TRUE, GAMMA = 0.05, 19, 0.005
key = lambda c: "+".join(sorted(c.mask))  # noqa: E731


def floor(nc, nt):
    return 1 - A - 2 * math.sqrt(A * (1 - A) * (1 / max(nc, 1) + 1 / nt))


def is_shifted(rows):
    by = collections.Counter(r["split"] for r in rows)
    return by.get("train", 0) >= 400 and len(by) > 1


def recall_by(ts, acc, keyf):
    agg = collections.defaultdict(lambda: [0, 0])
    for r in ts:
        for c, y in zip(r["cands"], r["labels"]):
            if y:
                agg[keyf(c)][0] += 1
                agg[keyf(c)][1] += id(c) in acc
    return {k: {"n_test": n, "recall": k2 / n} for k, (n, k2) in agg.items()}


def run_row(lang):
    fit, cal, tst = P7.prepare(lang)
    shifted = is_shifted(fit + cal + tst)
    sc = ctx.LogisticScorer(l2=1.0).fit([f for r in fit for f in r["feats"]], [y for r in fit for y in r["labels"]])
    cs, ts = P2.score_rows(cal, sc), P2.score_rows(tst, sc)
    cc = [c for r in cs for c in r["cands"]]
    cy = [y for r in cs for y in r["labels"]]
    cal_true = collections.Counter(key(c) for c, y in zip(cc, cy) if y)
    small = {key(c) for c in cc} | {key(c) for r in ts for c in r["cands"]}
    small = {g for g in small if cal_true.get(g, 0) < MIN_TRUE}
    clus = lambda c: "SMALL" if key(c) in small else key(c)  # noqa: E731
    cal_true_cl = collections.Counter(clus(c) for c, y in zip(cc, cy) if y)
    test_cands = [c for r in ts for c in r["cands"]]

    pc = pccf.PCCF(mode="coverage", alpha=A, min_group_true=MIN_TRUE, fallback="keep").fit(cc, cy)
    gl = pccf.PCCF(mode="coverage", alpha=A, min_group_true=MIN_TRUE, fallback="global").fit(cc, cy)
    cl = pccf.PCCF(mode="coverage", alpha=A, partition=clus, min_group_true=MIN_TRUE, fallback="keep").fit(cc, cy)
    acc = {"PCCF": {id(c) for c in test_cands if pc.accept(c)},
           "GLOBAL-FB": {id(c) for c in test_cands if gl.accept(c)},
           "CLUST": {id(c) for c in test_cands if cl.accept(c)}}

    # ---- WCP: domain classifier cal (0) vs unlabelled test (1) ----
    masks = sorted({key(c) for c in cc} | {key(c) for c in test_cands})

    def dfeat(rows):
        return [{**f, **{f"mask={m}": float(key(c) == m) for m in masks}}
                for r in rows for c, f in zip(r["cands"], r["feats"])]
    dc = ctx.LogisticScorer(l2=1.0).fit(dfeat(cs) + dfeat(ts), [0] * len(cc) + [1] * len(test_cands))
    ratio = len(cc) / max(len(test_cands), 1)
    w_of = lambda p: np.clip(p / np.clip(1 - p, 1e-9, None) * ratio, 0.05, 20.0)  # noqa: E731
    w_cal, w_test = w_of(dc.predict(dfeat(cs))), w_of(dc.predict(dfeat(ts)))
    per_g = collections.defaultdict(list)
    for c, y, w in zip(cc, cy, w_cal):
        if y:
            per_g[key(c)].append((pccf.joint_score(c), float(w)))
    tabs = {}
    for g, lst in per_g.items():
        lst.sort()
        vals = [v for v, _ in lst]
        cum = list(np.cumsum([w for _, w in lst]))
        tabs[g] = (vals, cum, cum[-1])
    wacc = set()
    for c, wt in zip(test_cands, w_test):
        g = key(c)
        if cal_true.get(g, 0) < MIN_TRUE:
            wacc.add(id(c)); continue
        vals, cum, W = tabs[g]
        target = (1 - A) * (W + float(wt))
        k = bisect.bisect_left(cum, target)
        thr = math.inf if k >= len(vals) else vals[k]
        if pccf.joint_score(c) <= thr:
            wacc.add(id(c))
    acc["WCP"] = wacc

    # ---- ACI: online per-group alpha, feedback on true candidates ----
    cal_vals = {g: [v for v, _ in lst] for g, lst in per_g.items()}
    alpha_t = collections.defaultdict(lambda: A)
    aacc = set()
    for r in ts:
        for c, y in zip(r["cands"], r["labels"]):
            g = key(c)
            if cal_true.get(g, 0) < MIN_TRUE:
                aacc.add(id(c)); continue
            at = alpha_t[g]
            thr = math.inf if at <= 0 else pccf.conformal_quantile(cal_vals[g], min(at, 0.999))
            ok = pccf.joint_score(c) <= thr
            if ok:
                aacc.add(id(c))
            if y:
                alpha_t[g] = at + GAMMA * (A - (0 if ok else 1))
    acc["ACI"] = aacc

    out = {"shifted": shifted, "cal_true": dict(cal_true), "cal_true_cluster": dict(cal_true_cl), "small": sorted(small)}
    base = P2.metrics(P2.apply_rule(ts, lambda c: True))
    out["UNION"] = {"P": base["P"], "R": base["R"]}
    for name, s_ in acc.items():
        m = P2.metrics(P2.apply_rule(ts, lambda c, s_=s_: id(c) in s_))
        out[name] = {"P": m["P"], "R": m["R"], "groups": recall_by(ts, s_, key)}
        if name == "CLUST":
            out[name]["clusters"] = recall_by(ts, s_, clus)
    return out


def run(budget):
    os.makedirs(OUT, exist_ok=True)
    t0 = time.perf_counter()
    for lang in ROWS:
        p = os.path.join(OUT, f"{lang}.json")
        if os.path.exists(p):
            continue
        if time.perf_counter() - t0 > budget:
            print("run again"); return
        json.dump(run_row(lang), open(p, "w"))
        print(f"  {lang} done ({time.perf_counter() - t0:.0f}s)", flush=True)
    print("complete")


def judge():
    L = []
    say = lambda s="": (print(s), L.append(s))  # noqa: E731
    R = {l: json.load(open(os.path.join(OUT, f"{l}.json"))) for l in ROWS}
    say("# PCCF phase 8 results: clustered small groups and shift correction\n")
    # ---- Part A ----
    fo_rows = [l for l, r in R.items() if r["small"]]
    gain = [l for l in fo_rows if R[l]["CLUST"]["P"] - R[l]["PCCF"]["P"] >= 0.03]
    cells = viol = 0
    for l in fo_rows:
        r = R[l]
        for k, d in r["CLUST"]["clusters"].items():
            if d["n_test"] >= 19:
                cells += 1
                viol += d["recall"] < floor(r["cal_true_cluster"].get(k, 0), d["n_test"])
    h35 = len(gain) >= len(fo_rows) / 2 and (cells == 0 or (cells - viol) / cells >= 0.90)
    say("## Part A: CLUST vs fail-open (PCCF) vs global fallback\n")
    say(f"Rows with at least one fail-open group: {len(fo_rows)}. CLUST gains ≥ +0.03 P in {len(gain)}. "
        f"Cluster-level floors: {cells - viol}/{cells} cells hold.\n")
    say("| row | PCCF P/R | CLUST P/R | GLOBAL-FB P/R | small-group recall PCCF / CLUST / GLOBAL-FB |")
    say("|---|---|---|---|---|")
    pooled = {m: [0, 0] for m in ("PCCF", "CLUST", "GLOBAL-FB")}
    for l in fo_rows:
        r = R[l]
        sg = {}
        for m in pooled:
            n = sum(d["n_test"] for g, d in r[m]["groups"].items() if g in r["small"])
            k = sum(d["recall"] * d["n_test"] for g, d in r[m]["groups"].items() if g in r["small"])
            pooled[m][0] += n; pooled[m][1] += k
            sg[m] = f"{k / n:.2f}" if n else "–"
        say(f"| {l} | {r['PCCF']['P']:.3f}/{r['PCCF']['R']:.3f} | {r['CLUST']['P']:.3f}/{r['CLUST']['R']:.3f} | "
            f"{r['GLOBAL-FB']['P']:.3f}/{r['GLOBAL-FB']['R']:.3f} | {sg['PCCF']} / {sg['CLUST']} / {sg['GLOBAL-FB']} |")
    say("\nPooled recall inside originally-small groups: " + ", ".join(
        f"{m} {v[1] / v[0]:.3f} (n={v[0]})" for m, v in pooled.items() if v[0]))
    # ---- Part B ----
    sh = [l for l, r in R.items() if r["shifted"]]
    say("\n## Part B: shift correction on shifted rows (original protocol)\n")
    tot = {m: [0, 0] for m in ("PCCF", "WCP", "ACI")}
    known = {m: 0 for m in tot}
    dP = {m: [] for m in tot}
    for l in sh:
        r = R[l]
        for m in tot:
            dP[m].append(r[m]["P"] - r["PCCF"]["P"])
            for g, d in r[m]["groups"].items():
                if d["n_test"] >= 19 and r["cal_true"].get(g, 0) >= MIN_TRUE:
                    bad = d["recall"] < floor(r["cal_true"][g], d["n_test"])
                    tot[m][0] += bad; tot[m][1] += 1
                    if (l, g) in KNOWN and not bad:
                        known[m] += 1
    say("| method | violations / shifted cells | known misses restored (of 4) | mean ΔP vs PCCF | mean ΔR vs PCCF |")
    say("|---|---|---|---|---|")
    for m in tot:
        dR = statistics.mean(R[l][m]["R"] - R[l]["PCCF"]["R"] for l in sh)
        say(f"| {m} | {tot[m][0]}/{tot[m][1]} | {known[m]} | {statistics.mean(dP[m]):+.3f} | {dR:+.3f} |")
    say("\nKnown-miss cells ({ner} recall): " + "; ".join(
        f"{l}: PCCF {R[l]['PCCF']['groups']['ner']['recall']:.3f}, WCP {R[l]['WCP']['groups']['ner']['recall']:.3f}, "
        f"ACI {R[l]['ACI']['groups']['ner']['recall']:.3f}" for l, _ in sorted(KNOWN)))
    h36 = known["WCP"] >= 3 and tot["WCP"][0] <= tot["PCCF"][0]
    h37 = known["ACI"] >= 3 and statistics.mean(dP["ACI"]) >= -0.02
    say(f"\n=== Verdicts (mechanical) ===\n  H35: {'SUPPORTED' if h35 else 'NOT SUPPORTED'}\n"
        f"  H36: {'SUPPORTED' if h36 else 'NOT SUPPORTED'}\n  H37: {'SUPPORTED' if h37 else 'NOT SUPPORTED'}")
    open(os.path.join(HERE, "..", "..", "..", "PCCF_PHASE8_RESULTS.md"), "w").write("\n".join(L) + "\n")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--budget", type=float, default=40)
    ap.add_argument("--judge", action="store_true")
    a = ap.parse_args()
    judge() if a.judge else run(a.budget)
