"""PCCF phase 6: split-shift vs chance diagnostic (PCCF_PHASE6_PREREGISTRATION.md).

Reuses the phase-5 evaluation code and caches unchanged; only splits() is
swapped per seed. Resumable: results/<lang>__<protocol>.json.
Usage:  PCCF_PHASE5_CACHE=<cache dir> python pccf_phase6_split_diagnostic.py --langs it,hi --budget 150
        python pccf_phase6_split_diagnostic.py --judge
"""
import argparse, collections, json, math, os, random, statistics, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "phase5"))
sys.argv, _argv = [sys.argv[0]], sys.argv  # phase-5 module parses nothing at import, but be safe
import pccf_phase5_all_languages as P5  # noqa: E402
sys.argv = _argv

OUT = os.path.join(HERE, "results")
SEEDS = range(1, 51)
ELIGIBLE = ["fr", "ru", "de", "it", "nl", "pt", "fi", "tr", "hi", "te", "ar_wiki", "ja", "id", "id_hf",
            "ko", "zh", "in", "ko_kdpii"]
SHIFTED = {"fr", "de", "it", "nl", "pt", "fi", "ja", "hi", "te", "id", "id_hf", "ko_kdpii", "ar_wiki"}
A = 0.05

# --- memoise the heavy loads (evaluate() re-reads data + cache every call) ---
_load = P5.load
_mem = {}
P5.load = lambda lang: _mem.setdefault(("d", lang), _load(lang))


class _J:  # json shim for P5: memoised load, everything else delegated
    def __getattr__(self, k):
        return getattr(json, k)

    def load(self, f):
        key = ("c", getattr(f, "name", id(f)))
        if key not in _mem:
            _mem[key] = json.load(f)
        f.close()
        return _mem[key]


P5.json = _J()
CAL = {}


def make_splits(protocol, seed):
    def _s(rows):
        rng = random.Random(seed)
        by = collections.Counter(r["split"] for r in rows)
        if protocol == "E2" and by.get("train", 0) >= 400 and len(by) > 1:
            tr = [r for r in rows if r["split"] == "train"]
            rng.shuffle(tr)
            fit, cal, tst = tr[: len(tr) // 2], tr[len(tr) // 2:], [r for r in rows if r["split"] != "train"]
        else:
            rs = rows[:]
            rng.shuffle(rs)
            q = len(rs) // 4
            fit, cal, tst = rs[:q], rs[q:2 * q], rs[2 * q:]
        cnt = collections.Counter()
        for r in cal:
            for c, y in zip(r["cands"], r["labels"]):
                cnt["+".join(sorted(c.mask))] += y
        CAL.clear(); CAL.update(cnt)
        return fit, cal, tst
    return _s


def run(langs, budget):
    os.makedirs(OUT, exist_ok=True)
    t0 = time.perf_counter()
    for lang in langs:
        for proto in ("E1", "E2"):
            p = os.path.join(OUT, f"{lang}__{proto}.json")
            done = json.load(open(p)) if os.path.exists(p) else {}
            for seed in SEEDS:
                if str(seed) in done:
                    continue
                if time.perf_counter() - t0 > budget:
                    json.dump(done, open(p, "w")); print(f"  {lang} {proto}: {len(done)}/50 (budget)"); return False
                P5.splits = make_splits(proto, seed)
                _, res = P5.evaluate(lang, lambda s="": None)
                rec = {}
                for sc in ("rule", "LR-UD"):
                    rec[sc] = {g: {"n_test": d["n_true"], "n_cal": CAL.get(g, 0), "recall": d["cand_recall"]}
                               for g, d in res[sc]["checks"].items() if d["n_true"]}
                done[str(seed)] = rec
            json.dump(done, open(p, "w"))
            print(f"  {lang} {proto}: {len(done)}/50", flush=True)
    return True


def judge():
    phi = lambda z: 0.5 * (1 + math.erf(z / math.sqrt(2)))  # noqa: E731
    R = {}
    for lang in ELIGIBLE:
        for proto in ("E1", "E2"):
            p = os.path.join(OUT, f"{lang}__{proto}.json")
            if not os.path.exists(p) or len(json.load(open(p))) < 50:
                print(f"missing/incomplete: {lang} {proto}"); return
            R[(lang, proto)] = json.load(open(p))
    L = []
    say = lambda s="": (print(s), L.append(s))  # noqa: E731

    def agg(lang, proto, sc):
        out = {}
        runs = R[(lang, proto)].values()
        groups = {g for r in runs for g in r[sc]}
        for g in sorted(groups):
            xs = [r[sc][g] for r in runs if g in r[sc]]
            recs = [x["recall"] for x in xs]
            out[g] = {"mean": statistics.mean(recs), "sd": statistics.pstdev(recs) if len(recs) > 1 else 0.0,
                      "se": (statistics.stdev(recs) / math.sqrt(len(recs))) if len(recs) > 1 else float("inf"),
                      "n_test": statistics.mean(x["n_test"] for x in xs),
                      "n_cal": statistics.mean(x["n_cal"] for x in xs), "k": len(xs), "rows": xs}
        return out

    say("# PCCF phase 6 results (split-shift vs chance)\n")
    say("## H25: validity under exchangeable (E1) re-splits, 50 seeds\n")
    say("| lang | scorer | group | mean n_cal / n_test | mean recall | seed SD | floor (0.95 − 2·SE) | ok |")
    say("|---|---|---|---|---|---|---|---|")
    h25 = {"LR-UD": True, "rule": True}
    table = {}
    for lang in ELIGIBLE:
        for sc in ("LR-UD", "rule"):
            a = agg(lang, "E1", sc)
            table[(lang, "E1", sc)] = a
            for g, d in a.items():
                if d["n_test"] < 19:
                    continue
                ok = d["mean"] >= 0.95 - 2 * d["se"]
                h25[sc] &= ok
                say(f"| {lang} | {sc} | {{{g}}} | {d['n_cal']:.0f} / {d['n_test']:.0f} | {d['mean']:.3f} | {d['sd']:.3f} | "
                    f"{0.95 - 2 * d['se']:.3f} | {'ok' if ok else '**MISS**'} |")
    say(f"\n**H25 (LR-UD): {'SUPPORTED' if h25['LR-UD'] else 'NOT SUPPORTED'}**; rule-scorer control: "
        f"{'all ok' if h25['rule'] else 'MISS'}\n")

    say("## H26: split shift (E2 original protocol vs E1 pooled), LR-UD {ner}\n")
    say("| lang | shifted split | E1 mean ± SE | E2 mean ± SE | E2 − E1 | beyond 2 SE |")
    say("|---|---|---|---|---|---|")
    hits = 0
    for lang in ELIGIBLE:
        e1, e2 = agg(lang, "E1", "LR-UD").get("ner"), agg(lang, "E2", "LR-UD").get("ner")
        if not e1 or not e2 or e1["n_test"] < 19:
            continue
        diff = e2["mean"] - e1["mean"]
        thr = 2 * math.sqrt(e1["se"] ** 2 + e2["se"] ** 2)
        beyond = diff < -thr
        if lang in ("it", "hi", "id_hf") and beyond:
            hits += 1
        say(f"| {lang} | {'yes' if lang in SHIFTED else 'no'} | {e1['mean']:.3f} ± {e1['se']:.3f} | "
            f"{e2['mean']:.3f} ± {e2['se']:.3f} | {diff:+.3f} | {'**yes**' if beyond else 'no'} |")
    h26 = hits >= 2
    say(f"\n**H26: {'SUPPORTED' if h26 else 'NOT SUPPORTED'}** ({hits}/3 of it, hi, id_hf lower beyond 2 SE)\n")

    say("## H27: was the phase-5 tolerance too tight? (E1, LR-UD, all eligible groups × 50 seeds)\n")
    n = miss = 0
    pred = []
    for lang in ELIGIBLE:
        for g, d in table[(lang, "E1", "LR-UD")].items():
            if d["n_test"] < 19:
                continue
            for x in d["rows"]:
                if not x["n_test"] or not x["n_cal"]:
                    continue
                n += 1
                miss += x["recall"] < 0.95 - 2 * math.sqrt(A * (1 - A) / x["n_test"])
                pred.append(phi(-2 * math.sqrt(1 / x["n_test"]) / math.sqrt(1 / x["n_cal"] + 1 / x["n_test"])))
    rate, prate = miss / n, statistics.mean(pred)
    se = math.sqrt(prate * (1 - prate) / n)
    h27 = rate > 0.023 and rate <= prate + 2 * se
    say(f"checks {n}; observed miss rate {rate:.3f}; nominal 0.023; combined-variance prediction {prate:.3f} (± {2*se:.3f})")
    say(f"\n**H27: {'SUPPORTED' if h27 else 'NOT SUPPORTED'}**\n")

    say("## Sensitivity only (NOT a re-scoring of phase 5): phase-5 misses under the combined-variance floor\n")
    p5 = json.load(open(os.path.join(HERE, "..", "phase5", "docker_run",
        "pccf_phase5_fr_ru_de_it_nl_pt_fi_tr_hi_te_ar_ar_wiki_ja_id_id_hf_ko_zh_in_ko_kdpii_ko_legal_results.json")))["results"]
    for lang in ("ru", "it", "hi", "id_hf"):
        c = p5[lang]["LR-UD"]["checks"]["ner"]
        nc = statistics.mean(x["n_cal"] for x in agg(lang, "E2", "LR-UD")["ner"]["rows"])
        f = 0.95 - 2 * math.sqrt(A * (1 - A) * (1 / nc + 1 / c["n_true"]))
        say(f"- {lang} {{ner}}: recall {c['cand_recall']:.3f}; combined floor {f:.3f} (n_cal≈{nc:.0f}, n_test={c['n_true']}) "
            f"→ {'would pass' if c['cand_recall'] >= f else 'still misses'}")
    say(f"\n=== Verdicts (mechanical) ===\n  H25: {'SUPPORTED' if h25['LR-UD'] else 'NOT SUPPORTED'}\n"
        f"  H26: {'SUPPORTED' if h26 else 'NOT SUPPORTED'}\n  H27: {'SUPPORTED' if h27 else 'NOT SUPPORTED'}")
    open(os.path.join(HERE, "..", "..", "..", "PCCF_PHASE6_RESULTS_RAW.md"), "w").write("\n".join(L) + "\n")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--langs", default=",".join(ELIGIBLE))
    ap.add_argument("--budget", type=float, default=150)
    ap.add_argument("--judge", action="store_true")
    a = ap.parse_args()
    if a.judge:
        judge()
    else:
        print("complete" if run(a.langs.split(","), a.budget) else "run again")
