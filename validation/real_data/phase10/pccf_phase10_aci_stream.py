"""PCCF phase 10 part A (PCCF_PHASE10_PREREGISTRATION.md): ACI with audited, delayed
analyst feedback on streaming log telemetry (phase-3b T7 corpora, S2 config).
Resumable per seed: results/seed<seed>.json.
  python pccf_phase10_aci_stream.py            (repeat until 'complete')
  python pccf_phase10_aci_stream.py --judge
"""
import argparse, collections, json, math, os, random, statistics, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
_argv, sys.argv = sys.argv, [sys.argv[0]]
import pccf_phase3b_message_text as T7  # noqa: E402
sys.argv = _argv
P3, pccf = T7.P3, T7.pccf
OUT = os.path.join(HERE, "results")
A, GAMMA = 0.05, 0.005
SETTINGS = [(1.0, 0), (0.1, 0), (0.1, 500)]


def floor(nc, nt):
    return 1 - A - 2 * math.sqrt(A * (1 - A) * (1 / max(nc, 1) + 1 / nt))


def aci_stream(ts, cal_vals, rho, delay, rng):
    alpha = collections.defaultdict(lambda: A)
    pending = collections.deque()  # (apply_at_line, group, err)
    acc = set()
    for i, r in enumerate(ts):
        while pending and pending[0][0] <= i:
            _, g, err = pending.popleft()
            alpha[g] += GAMMA * (A - err)
        audited = rng.random() < rho
        for c, y in zip(r["cands"], r["labels"]):
            g = T7.mask_key(c)
            vals = cal_vals.get(g, [])
            at = alpha[g]
            thr = math.inf if at <= 0 or not vals else pccf.conformal_quantile(vals, min(at, 0.999))
            ok = pccf.joint_score(c) <= thr
            if ok:
                acc.add(id(c))
            if audited and y:
                pending.append((i + delay, g, 0 if ok else 1))
    return acc


def run_seed(seed, cache):
    rows = T7.rows_for(seed, cache)
    out = {}
    for held in P3.DATASETS:
        tst = rows[held]["B"]
        rest = [r for ds in P3.DATASETS if ds != held for r in rows[ds]["A"]]
        rng = random.Random(seed * 100 + P3.DATASETS.index(held))
        ids = [r["id"] for r in rest]
        rng.shuffle(ids)
        fit_ids = set(ids[: len(ids) // 2])
        cal = [r for r in rest if r["id"] not in fit_ids]
        sc = T7.RuleOnly()
        cs, ts = T7.score_rows(cal, sc), T7.score_rows(tst, sc)
        cc = [c for r in cs for c in r["cands"]]
        cy = [y for r in cs for y in r["labels"]]
        static = pccf.PCCF(mode="coverage", alpha=A, partition=T7.mask_key).fit(cc, cy)
        cal_vals = collections.defaultdict(list)
        for c, y in zip(cc, cy):
            if y:
                cal_vals[T7.mask_key(c)].append(pccf.joint_score(c))
        cal_true = {"|".join(g): len(v) for g, v in cal_vals.items()}
        methods = {"STATIC": {id(c) for r in ts for c in r["cands"] if static.accept(c)}}
        for rho, d in SETTINGS:
            methods[f"ACI rho={rho} D={d}"] = aci_stream(ts, cal_vals, rho, d,
                                                        random.Random(9000 + seed * 10 + P3.DATASETS.index(held)))
        res = {"cal_true": cal_true, "n_lines": len(ts)}
        half = len(ts) // 2
        for name, acc in methods.items():
            pd = P3.apply_rule(ts, lambda c, acc=acc: id(c) in acc)
            m = P3.metrics(pd)
            grp = collections.defaultdict(lambda: [0, 0])
            halves = [[0, 0], [0, 0]]
            for i, r in enumerate(ts):
                for c, y in zip(r["cands"], r["labels"]):
                    if y:
                        g = "|".join(T7.mask_key(c))
                        grp[g][0] += 1; grp[g][1] += id(c) in acc
                        h = halves[i >= half]; h[0] += 1; h[1] += id(c) in acc
            res[name] = {"tp": m.get("tp"), "fp": m.get("fp"), "fn": m.get("fn"), "P": m["P"], "R": m["R"],
                         "R_field": T7.loc_recall(pd, "field"), "R_message": T7.loc_recall(pd, "message"),
                         "groups": {g: {"n_test": n, "recall": k / n} for g, (n, k) in grp.items()},
                         "cand_recall_halves": [h[1] / h[0] if h[0] else None for h in halves]}
        out[held] = res
    return out


def run():
    os.makedirs(OUT, exist_ok=True)
    cache = None
    for seed in P3.SEEDS:
        p = os.path.join(OUT, f"seed{seed}.json")
        if os.path.exists(p):
            continue
        cache = cache or json.load(open(T7.CACHE))
        t0 = time.perf_counter()
        json.dump(run_seed(seed, cache), open(p, "w"))
        print(f"  seed {seed} done ({time.perf_counter() - t0:.0f}s)", flush=True)
        return print("run again")
    print("complete")


def judge():
    L = []
    say = lambda s="": (print(s), L.append(s))  # noqa: E731
    R = {s: json.load(open(os.path.join(OUT, f"seed{s}.json"))) for s in P3.SEEDS}
    names = ["STATIC"] + [f"ACI rho={r} D={d}" for r, d in SETTINGS]
    say("# PCCF phase 10A results: ACI with audited, delayed analyst feedback (log telemetry)\n")
    viol = {n: [0, 0] for n in names}
    tot = {n: [0, 0, 0] for n in names}
    rf, rm, h1, h2 = ({n: [] for n in names} for _ in range(4))
    for s, byds in R.items():
        for ds, res in byds.items():
            for n in names:
                x = res[n]
                tot[n][0] += x["tp"]; tot[n][1] += x["fp"]; tot[n][2] += x["fn"]
                rf[n].append(x["R_field"]); rm[n].append(x["R_message"])
                a, b = x["cand_recall_halves"]
                if a is not None: h1[n].append(a)
                if b is not None: h2[n].append(b)
                for g, d in x["groups"].items():
                    nc = res["cal_true"].get(g, 0)
                    if d["n_test"] >= 19 and nc >= 19:
                        viol[n][1] += 1
                        viol[n][0] += d["recall"] < floor(nc, d["n_test"])
    say("| method | floor violations (cells) | pooled P | pooled R | R field | R message | cand. recall 1st / 2nd half |")
    say("|---|---|---|---|---|---|---|")
    P = {}
    for n in names:
        tp, fp, fn = tot[n]
        P[n] = tp / (tp + fp) if tp + fp else 0.0
        say(f"| {n} | {viol[n][0]}/{viol[n][1]} | {P[n]:.3f} | {tp / (tp + fn):.3f} | {statistics.mean(rf[n]):.3f} | "
            f"{statistics.mean(rm[n]):.3f} | {statistics.mean(h1[n]):.3f} / {statistics.mean(h2[n]):.3f} |")
    key = "ACI rho=0.1 D=500"
    h40 = viol[key][0] < viol["STATIC"][0] and P[key] >= P["STATIC"] - 0.02
    say(f"\n=== Verdict (mechanical) ===\n  H40: {'SUPPORTED' if h40 else 'NOT SUPPORTED'}")
    open(os.path.join(HERE, "..", "..", "..", "PCCF_PHASE10A_RESULTS.md"), "w").write("\n".join(L) + "\n")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--judge", action="store_true")
    a = ap.parse_args()
    judge() if a.judge else run()
