"""PCCF phase 9 (PCCF_PHASE9_PREREGISTRATION.md).
Part A: certified NOISE-DROP of fail-open groups (Clopper-Pearson upper bound on precision).
Part B: labelling budget (calibration subsampling on pooled exchangeable splits, seeds 1-10).
Resumable: results/A_<row>.json, results/B_<row>.json.
  PCCF_PHASE5_CACHE=<cache> python pccf_phase9.py --budget 45   (repeat until 'complete')
  python pccf_phase9.py --judge
"""
import argparse, collections, json, math, os, random, statistics, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "phase7"))
import pccf_phase7_baselines as P7  # noqa: E402

pccf, ctx, P2, P5 = P7.pccf, P7.ctx, P7.P2, P7.P5
ROWS = P7.ROWS + P7.GL_ROWS
OUT = os.path.join(HERE, "results")
A, MIN_TRUE, DELTA, NOISE_P, MIN_N = 0.05, 19, 0.05, 0.05, 30
FRACS = (0.05, 0.1, 0.2, 0.4, 1.0)
SEEDS = range(1, 11)
key = lambda c: "+".join(sorted(c.mask))  # noqa: E731


def floor(nc, nt):
    return 1 - A - 2 * math.sqrt(A * (1 - A) * (1 / max(nc, 1) + 1 / nt))


def cp_upper(k, n, conf_delta):
    """One-sided Clopper-Pearson upper bound: largest p with P(X <= k; n, p) >= conf_delta."""
    if k >= n:
        return 1.0
    lo, hi = k / n, 1.0
    for _ in range(60):
        mid = (lo + hi) / 2
        cdf = 1.0 - math.exp(pccf._log_binom_upper_tail(k + 1, n, mid))  # P(X <= k)
        lo, hi = (mid, hi) if cdf >= conf_delta else (lo, mid)
    return lo


def fit_score(fit, *others):
    sc = ctx.LogisticScorer(l2=1.0).fit([f for r in fit for f in r["feats"]], [y for r in fit for y in r["labels"]])
    return [P2.score_rows(o, sc) for o in others]


def part_a(lang):
    fit, cal, tst = P7.prepare(lang)
    cs, ts = fit_score(fit, cal, tst)
    cc = [c for r in cs for c in r["cands"]]
    cy = [y for r in cs for y in r["labels"]]
    cal_true = collections.Counter(key(c) for c, y in zip(cc, cy) if y)
    cal_n = collections.Counter(key(c) for c in cc)
    pc = pccf.PCCF(mode="coverage", alpha=A, min_group_true=MIN_TRUE, fallback="keep").fit(cc, cy)
    small = [g for g in cal_n if cal_true.get(g, 0) < MIN_TRUE]
    m = max(len(small), 1)
    decisions = {}
    for g in small:
        n, k = cal_n[g], cal_true.get(g, 0)
        u = cp_upper(k, n, DELTA / m) if n >= MIN_N else None
        decisions[g] = {"n_cal": n, "k_cal": k, "upper": u, "drop": u is not None and u < NOISE_P}
    dropped = {g for g, d in decisions.items() if d["drop"]}
    nd = lambda c: key(c) not in dropped and pc.accept(c)  # noqa: E731
    out = {"decisions": decisions}
    for name, acc in (("UNION", lambda c: True), ("PCCF", pc.accept), ("NOISE-DROP", nd)):
        mm = P2.metrics(P2.apply_rule(ts, acc))
        out[name] = {"P": mm["P"], "R": mm["R"]}
    for g in dropped:  # test precision of each dropped group
        n = sum(1 for r in ts for c in r["cands"] if key(c) == g)
        k = sum(y for r in ts for c, y in zip(r["cands"], r["labels"]) if key(c) == g)
        decisions[g]["test_n"], decisions[g]["test_true"] = n, k
    return out


def part_b(lang):
    orig = P5.splits
    P5.splits = lambda rows: (rows, [], [])
    try:
        rows, _, _ = P7.prepare(lang)
    finally:
        P5.splits = orig
    res = []
    for seed in SEEDS:
        rs = rows[:]
        random.Random(seed).shuffle(rs)
        q = len(rs) // 4
        fit, cal_all, tst = rs[:q], rs[q:2 * q], rs[2 * q:]
        cs_all, ts = fit_score(fit, cal_all, tst)
        u = P2.metrics(P2.apply_rule(ts, lambda c: True))
        for f in FRACS:
            cs = cs_all[: max(1, math.ceil(f * len(cs_all)))]
            cc = [c for r in cs for c in r["cands"]]
            cy = [y for r in cs for y in r["labels"]]
            cal_true = collections.Counter(key(c) for c, y in zip(cc, cy) if y)
            pc = pccf.PCCF(mode="coverage", alpha=A, min_group_true=MIN_TRUE, fallback="keep").fit(cc, cy)
            pd = P2.apply_rule(ts, pc.accept)
            mm = P2.metrics(pd)
            ch = P2.pattern_checks(pd, None)
            res.append({"seed": seed, "f": f, "dP": mm["P"] - u["P"], "dR": mm["R"] - u["R"],
                        "groups": {g: {"n_cal": cal_true.get(g, 0), "n_test": d["n_true"], "recall": d["cand_recall"]}
                                   for g, d in ch.items() if d["n_true"]}})
    return res


def run(budget):
    os.makedirs(OUT, exist_ok=True)
    t0 = time.perf_counter()
    for part, fn in (("A", part_a), ("B", part_b)):
        for lang in ROWS:
            p = os.path.join(OUT, f"{part}_{lang}.json")
            if os.path.exists(p):
                continue
            if time.perf_counter() - t0 > budget:
                print("run again"); return
            json.dump(fn(lang), open(p, "w"))
            print(f"  {part} {lang} ({time.perf_counter() - t0:.0f}s)", flush=True)
    print("complete")


def judge():
    L = []
    say = lambda s="": (print(s), L.append(s))  # noqa: E731
    RA = {l: json.load(open(os.path.join(OUT, f"A_{l}.json"))) for l in ROWS}
    RB = {l: json.load(open(os.path.join(OUT, f"B_{l}.json"))) for l in ROWS}
    say("# PCCF phase 9 results: certified noise-group dropping and labelling budget\n")
    say("## Part A: NOISE-DROP\n")
    fo = [l for l, r in RA.items() if r["decisions"]]
    drops = [(l, g, d) for l, r in RA.items() for g, d in r["decisions"].items() if d["drop"]]
    sound = [x for x in drops if x[2]["test_n"] == 0 or x[2]["test_true"] / x[2]["test_n"] <= 0.10]
    affected = sorted({l for l, _, _ in drops})
    dR = [RA[l]["NOISE-DROP"]["R"] - RA[l]["PCCF"]["R"] for l in affected]
    gain = [l for l in fo if RA[l]["NOISE-DROP"]["P"] - RA[l]["PCCF"]["P"] >= 0.03]
    h38a = bool(drops) and len(sound) / len(drops) >= 0.90
    h38b = (statistics.mean(dR) if dR else 0.0) >= -0.01
    h38c = len(gain) >= 0.25 * len(fo)
    say(f"Rows with a fail-open group: {len(fo)}; drop decisions: {len(drops)} in {len(affected)} rows.")
    say(f"(a) dropped groups with test precision ≤ 0.10: {len(sound)}/{len(drops)}")
    say(f"(b) mean recall change vs PCCF over affected rows: {statistics.mean(dR) if dR else 0:+.4f}")
    say(f"(c) rows with ≥ +0.03 precision gain: {len(gain)}/{len(fo)}\n")
    say("| row | dropped group(s) (cal n / true; CP upper) | test precision of dropped | PCCF P/R | NOISE-DROP P/R |")
    say("|---|---|---|---|---|")
    for l in affected:
        r = RA[l]
        ds = [(g, d) for g, d in r["decisions"].items() if d["drop"]]
        say(f"| {l} | " + "; ".join(f"{{{g}}} {d['n_cal']}/{d['k_cal']}; u={d['upper']:.3f}" for g, d in ds) + " | "
            + "; ".join(f"{d['test_true']}/{d['test_n']}" for _, d in ds)
            + f" | {r['PCCF']['P']:.3f}/{r['PCCF']['R']:.3f} | {r['NOISE-DROP']['P']:.3f}/{r['NOISE-DROP']['R']:.3f} |")
    say("\n## Part B: labelling budget (pooled splits, seeds 1-10)\n")
    bins = [(19, 49), (50, 99), (100, 199), (200, 399), (400, 10 ** 9)]
    agg = {b: [0, 0, []] for b in bins}
    big_hold = [0, 0]
    for l, runs in RB.items():
        for x in runs:
            for g, d in x["groups"].items():
                if d["n_test"] < 19 or d["n_cal"] < 19:
                    continue
                ok = d["recall"] >= floor(d["n_cal"], d["n_test"])
                for b in bins:
                    if b[0] <= d["n_cal"] <= b[1]:
                        agg[b][0] += ok; agg[b][1] += 1; agg[b][2].append(d["recall"])
                if d["n_cal"] >= 100:
                    big_hold[0] += ok; big_hold[1] += 1
    say("| true calibration names in the group | draws | floor holds | mean recall | recall 5th pct |")
    say("|---|---|---|---|---|")
    for b in bins:
        h, n, rec = agg[b]
        if n:
            rec = sorted(rec)
            say(f"| {b[0]}–{'' if b[1] > 10**8 else b[1]}{'+' if b[1] > 10**8 else ''} | {n} | {h / n:.1%} | "
                f"{statistics.mean(rec):.3f} | {rec[int(0.05 * len(rec))]:.3f} |")
    elig, good = 0, 0
    say("\n| row | full-calibration ΔP (median) | smallest f with main group ≥ 100 true | ΔP there (median) | ratio |")
    say("|---|---|---|---|---|")
    for l, runs in RB.items():
        full = statistics.median(x["dP"] for x in runs if x["f"] == 1.0)
        if full < 0.03:
            continue
        pick = None
        for f in P7_FR:
            xs = [x for x in runs if x["f"] == f]
            mains = [max((d["n_cal"] for d in x["groups"].values()), default=0) for x in xs]
            if statistics.median(mains) >= 100:
                pick = f; break
        if pick is None:
            continue
        elig += 1
        dp = statistics.median(x["dP"] for x in runs if x["f"] == pick)
        good += dp >= 0.8 * full
        say(f"| {l} | {full:+.3f} | {pick} | {dp:+.3f} | {dp / full:.2f} |")
    h39 = big_hold[1] > 0 and big_hold[0] / big_hold[1] >= 0.90 and elig > 0 and good / elig >= 0.80
    say(f"\nFloor holds for n_cal_true ≥ 100: {big_hold[0]}/{big_hold[1]} ({big_hold[0] / max(big_hold[1], 1):.1%}); "
        f"rows reaching ≥ 80 % of full gain at the ≥ 100-name budget: {good}/{elig}")
    say(f"\n=== Verdicts (mechanical) ===\n  H38: {'SUPPORTED' if h38a and h38b and h38c else 'NOT SUPPORTED'} "
        f"(a={h38a}, b={h38b}, c={h38c})\n  H39: {'SUPPORTED' if h39 else 'NOT SUPPORTED'}")
    open(os.path.join(HERE, "..", "..", "..", "PCCF_PHASE9_RESULTS.md"), "w").write("\n".join(L) + "\n")


P7_FR = FRACS

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--budget", type=float, default=45)
    ap.add_argument("--judge", action="store_true")
    a = ap.parse_args()
    judge() if a.judge else run(a.budget)
