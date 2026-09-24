"""PCCF phase 11 analysis (PCCF_PHASE11_PREREGISTRATION.md): H43 on TAB, plus the
descriptive arms (pooled-split diagnostic, ACI, layer comparison, Enron). H41/H42
come from the phase-5 harness run on the phase-11 rows.
  PCCF_PHASE5_CACHE=<cache> python pccf_phase11.py --run   (resumable)
  python pccf_phase11.py --judge
"""
import argparse, collections, glob, json, math, os, statistics, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
for sub in ("phase6", "phase7", "phase8"):
    sys.path.insert(0, os.path.join(HERE, "..", sub))
import pccf_phase7_baselines as P7  # noqa: E402
import pccf_phase8 as P8  # noqa: E402
import pccf_phase6_split_diagnostic as P6  # noqa: E402

pccf, ctx, P2, P5 = P7.pccf, P7.ctx, P7.P2, P7.P5
OUT = os.path.join(HERE, "results")
ROWS = ["en_tab_redact", "en_tab_spacy", "en_btc_redact", "en_btc_spacy", "en_wnut_redact", "en_wnut_spacy",
        "de_germeval", "ru_factrueval", "ar_anercorp"]
DESC = ["en_enron_redact", "en_enron_spacy"]
TAB = ["en_tab_redact", "en_tab_spacy"]
A = 0.05
key = lambda c: "+".join(sorted(c.mask))  # noqa: E731


def have(lang):
    return P5.load(lang) is not None and os.path.exists(os.path.join(P5.CACHE_DIR, f"{lang}.json"))


def h43_row(lang):
    fit, cal, tst = P7.prepare(lang)
    docs = {d["id"]: d for d in P5.load(lang)}
    sc = ctx.LogisticScorer(l2=1.0).fit([f for r in fit for f in r["feats"]], [y for r in fit for y in r["labels"]])
    cs, ts = P2.score_rows(cal, sc), P2.score_rows(tst, sc)
    pc = pccf.PCCF(mode="coverage", alpha=A, min_group_true=19, fallback="keep").fit(
        [c for r in cs for c in r["cands"]], [y for r in cs for y in r["labels"]])
    agg = collections.defaultdict(lambda: [0, 0, 0])  # total, covered, kept
    for r in ts:
        for e in docs[r["id"]]["ents"]:
            if e["t"] != "PERSON":
                continue
            s, t = e["o"]
            ov = [c for c in r["cands"] if c.start < t and s < c.end]
            d = agg[e.get("idtype", "NA")]
            d[0] += 1
            d[1] += bool(ov)
            d[2] += any(pc.accept(c) for c in ov)
    return {k: {"total": v[0], "covered": v[1], "kept": v[2]} for k, v in agg.items()}


def run(budget):
    os.makedirs(OUT, exist_ok=True)
    t0 = time.perf_counter()
    todo = [("h43", l, h43_row) for l in TAB] + [("aci", l, P8.run_row) for l in ROWS + DESC]
    for kind, lang, fn in todo:
        p = os.path.join(OUT, f"{kind}_{lang}.json")
        if os.path.exists(p) or not have(lang):
            continue
        if time.perf_counter() - t0 > budget:
            print("run again"); return
        json.dump(fn(lang), open(p, "w"))
        print(f"  {kind} {lang} ({time.perf_counter() - t0:.0f}s)", flush=True)
    P6.OUT = os.path.join(OUT, "split_diag")
    shifted = [l for l in ROWS if have(l) and P8.is_shifted([r for part in P7.prepare(l) for r in part])]
    if shifted and not P6.run(shifted, budget - (time.perf_counter() - t0)):
        print("run again"); return
    print("complete")


def judge():
    L = []
    say = lambda s="": (print(s), L.append(s))  # noqa: E731
    say("# PCCF phase 11 results: real documents\n")
    f5 = sorted(glob.glob(os.path.join(HERE, "..", "phase5", "pccf_phase5_*en_tab_redact*_results.json")) +
                glob.glob(os.path.join(HERE, "docker_out", "pccf_phase5_*en_tab_redact*_results.json")), key=os.path.getmtime)
    if not f5:
        say("phase-5 harness results for the phase-11 rows not found"); return
    R5 = json.load(open(f5[-1]))
    res, ver = R5["results"], R5["verdicts"]
    say("## H41 / H42 (phase-5 harness; combined floor)\n")
    say("| row | union P / R | rule ΔP (floor) | LR-UD ΔP / ΔR (floor) | status |")
    say("|---|---|---|---|---|")
    for l in ROWS + DESC:
        r = res.get(l)
        if not r or "union" not in r:
            say(f"| {l} | no data | | | |"); continue
        u, ru, lr = r["union"], r["rule"], r["LR-UD"]
        if lr.get("skipped"):
            say(f"| {l} | {u['P']:.3f} / {u['R']:.3f} | | skipped | |"); continue
        st = ("descriptive" if l in DESC else ("not eligible" if ru.get("eligible_groups", 0) == 0 else
              ("useful+valid" if lr["floors_hold_cv"] and lr["P"] - u["P"] >= 0.03 else
               ("floor miss" if not lr["floors_hold_cv"] else "gain < 0.03"))))
        say(f"| {l} | {u['P']:.3f} / {u['R']:.3f} | {ru['P'] - u['P']:+.3f} ({'ok' if ru['floors_hold_cv'] else 'miss'}) | "
            f"{lr['P'] - u['P']:+.3f} / {lr['R'] - u['R']:+.3f} ({'ok' if lr['floors_hold_cv'] else 'miss'}) | {st} |")
    say("\n## H43: TAB direct identifiers kept by LR-UD PCCF\n")
    say("| row | idtype | gold | covered by a candidate | kept | kept / covered | floor |")
    say("|---|---|---|---|---|---|---|")
    h43 = True
    for l in TAB:
        p = os.path.join(OUT, f"h43_{l}.json")
        if not os.path.exists(p):
            h43 = False; say(f"| {l} | missing | | | | | |"); continue
        d = json.load(open(p))
        for it in ("DIRECT", "QUASI", "NO_MASK"):
            x = d.get(it)
            if not x:
                continue
            frac = x["kept"] / x["covered"] if x["covered"] else float("nan")
            fl = 0.95 - 2 * math.sqrt(0.0475 / x["covered"]) if x["covered"] else float("nan")
            if it == "DIRECT":
                h43 &= x["covered"] > 0 and frac >= fl
            say(f"| {l} | {it} | {x['total']} | {x['covered']} | {x['kept']} | {frac:.3f} | {fl:.3f}{' (judged)' if it == 'DIRECT' else ''} |")
    say("\n## Descriptive: ACI vs PCCF (phase-8 method) and pooled-split diagnostic\n")
    say("| row | shifted | PCCF P/R | ACI P/R | PCCF / ACI violations |")
    say("|---|---|---|---|---|")
    for l in ROWS + DESC:
        p = os.path.join(OUT, f"aci_{l}.json")
        if not os.path.exists(p):
            continue
        r = json.load(open(p))
        v = {}
        for m in ("PCCF", "ACI"):
            v[m] = sum(1 for g, d in r[m]["groups"].items() if d["n_test"] >= 19 and r["cal_true"].get(g, 0) >= 19
                       and d["recall"] < P8.floor(r["cal_true"][g], d["n_test"]))
        say(f"| {l} | {r['shifted']} | {r['PCCF']['P']:.3f}/{r['PCCF']['R']:.3f} | {r['ACI']['P']:.3f}/{r['ACI']['R']:.3f} | "
            f"{v['PCCF']} / {v['ACI']} |")
    sd = os.path.join(OUT, "split_diag")
    for e1 in sorted(glob.glob(os.path.join(sd, "*__E1.json"))):
        l = os.path.basename(e1).split("__")[0]
        rows = {}
        for proto in ("E1", "E2"):
            d = json.load(open(os.path.join(sd, f"{l}__{proto}.json")))
            vals = collections.defaultdict(list)
            for run_ in d.values():
                for g, x in run_["LR-UD"].items():
                    if x["n_test"] >= 19:
                        vals[g].append(x["recall"])
            rows[proto] = {g: statistics.mean(v) for g, v in vals.items()}
        say(f"- {l} pooled (E1) vs original (E2) LR-UD recall: " + "; ".join(
            f"{{{g}}} {rows['E1'][g]:.3f} vs {rows['E2'].get(g, float('nan')):.3f}" for g in sorted(rows["E1"])))
    say(f"\n=== Verdicts (mechanical) ===\n  H41: {'SUPPORTED' if ver.get('H41') else 'NOT SUPPORTED'}\n"
        f"  H42: {'SUPPORTED' if ver.get('H42') else 'NOT SUPPORTED'}\n  H43: {'SUPPORTED' if h43 else 'NOT SUPPORTED'}")
    open(os.path.join(HERE, "..", "..", "..", "PCCF_PHASE11_RESULTS.md"), "w").write("\n".join(L) + "\n")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--judge", action="store_true")
    ap.add_argument("--budget", type=float, default=1e9)
    a = ap.parse_args()
    run(a.budget) if a.run else judge()
