"""
PCCF phase 2 on MEDDOCAN: does a within-pattern context signal make
presence-conditioned calibration useful? Tests the revision recommended in
PCCF_FEASIBILITY_RESULTS.md.

Additive. It imports evaluate_meddocan.py, pccf_feasibility_meddocan.py,
src/pccf.py and src/pccf_context.py, and modifies none of them. Same 520
documents, same layers, same matching rule.

PROTOCOL (3-way split, so the scorer is never fit on calibration data):
  fit       = MEDDOCAN train (210 docs): fits the logistic scorer
  calibrate = MEDDOCAN validation (190 docs): conformal / LTT thresholds
  test      = MEDDOCAN test (120 docs): everything reported
The context cue lists in pccf_context.ES_CUES were fixed from the train
split only.

PRE-REGISTERED HYPOTHESES are printed verbatim at the top of every run and
judged mechanically at the end. Nothing is re-judged by hand.

Usage:
  python validation/real_data/pccf_phase2_meddocan.py --build-stress-cache   # ~60 s, needs es_core_news_md
  python validation/real_data/pccf_phase2_meddocan.py
"""
import argparse
import json
import os
import random
import statistics
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, HERE)

import evaluate_meddocan as em  # noqa: E402
import pccf  # noqa: E402
import pccf_context as ctx  # noqa: E402
import pccf_feasibility_meddocan as H  # noqa: E402

STRESS_CACHE = os.path.join(ROOT, "output", "pccf_meddocan_stress_cache.json")
OUT_TXT = os.path.join(HERE, "pccf_phase2_results.txt")
OUT_JSON = os.path.join(HERE, "pccf_phase2_results.json")

HYPOTHESES = """PRE-REGISTERED HYPOTHESES (fixed before the first test-split run):
  H1 redaction tier: LR-full coverage a=0.05 reaches test recall >= 0.92 AND
     precision >= union + 0.05, and the bootstrap 95% CI of dP vs union excludes 0.
  H2 alert tier: LR-full precision>=0.85 reaches test precision >= 0.85 AND
     recall above intersection, and the bootstrap 95% CI of dR vs intersection excludes 0.
  H3 template dependence: LR-noFieldLabel coverage a=0.05 keeps >= 50% of
     LR-full's precision gain over union (fails => gain is mostly MEDDOCAN's form header).
  H4 guarantees: across 5-fold CV, LR-full configs violate their guarantee in
     <= 10% of (fold, pattern) cells. Coverage is a guarantee in expectation, so a
     cell counts as violated only when candidate recall < 1-a - 2*sqrt(a(1-a)/n_true).
     Precision mode is a high-probability (1-delta) guarantee, so any kept precision
     below target counts as a violation. (Amended before any test-split run: a strict
     "< 1-a" rule would flag about half of all cells by sampling noise alone.)
  H5 stress (expectation, not a claim): with test names lowercased or flattened,
     candidate generation collapses for every rule, so end-to-end recall
     collapses whatever the fusion rule."""

SCORERS = ["beam", "LR-full", "LR-noFieldLabel", "rule"]
LEVELS = [("coverage", 0.02), ("coverage", 0.05), ("coverage", 0.10),
          ("precision", 0.80), ("precision", 0.85), ("precision", 0.90)]


def cfg_name(scorer, mode, v):
    return f"{scorer:16s} " + (f"coverage a={v:.2f}" if mode == "coverage" else f"precision>={v:.2f}")


def pccf_params(mode, v):
    return (dict(mode="coverage", alpha=v) if mode == "coverage"
            else dict(mode="precision", target_precision=v, delta=0.05))


# --------------------------------------------------------------------------
def fresh_rows(cache_docs, docs_by_id, texts=None):
    rows = H.prepare(cache_docs, docs_by_id, "beam")
    for r in rows:
        r["text"] = texts[r["id"]] if texts else docs_by_id[r["id"]]["text"]
        r["feats"] = [ctx.features(r["text"], c) for c in r["cands"]]
    return rows


def make_scorer(name, fit_rows):
    if name == "beam":
        return None
    if name == "rule":
        return ctx.RuleScorer()
    drop = ctx.FIELD_LABEL_FEATURES if name == "LR-noFieldLabel" else ()
    feats = [f for r in fit_rows for f in r["feats"]]
    ys = [y for r in fit_rows for y in r["labels"]]
    return ctx.LogisticScorer(l2=1.0, drop=drop).fit(feats, ys)


def score_rows(rows, scorer):
    """Return a copy of the rows with context scores applied (candidates are
    rebuilt so that different scorers never share mutated state)."""
    out = []
    for r in rows:
        cands = pccf.build_candidates(r["hits"], "PERSON")
        if scorer is not None and cands:
            ctx.apply_scores(cands, scorer.predict(r["feats"]))
        out.append({**r, "cands": cands})
    return out


def fit_pccf(cal_rows, params):
    m = pccf.PCCF(**params)
    m.fit([c for r in cal_rows for c in r["cands"]], [y for r in cal_rows for y in r["labels"]])
    return m


def apply_rule(rows, accept):
    per_doc = []
    for r in rows:
        acc = {id(c) for c in r["cands"] if accept(c)}
        per_doc.append((H.preds_from(r["cands"], acc, r["hits"]), r["gold"], r, acc))
    return per_doc


def metrics(per_doc):
    return H.score([(p, g) for p, g, _, _ in per_doc])


def pattern_checks(per_doc, model):
    """(pattern, n_true, candidate recall, n_kept, kept precision, threshold)"""
    agg = {}
    for _, _, r, acc in per_doc:
        for c, y in zip(r["cands"], r["labels"]):
            k = "+".join(sorted(c.mask))
            d = agg.setdefault(k, [0, 0, 0, 0])
            d[0] += y
            if id(c) in acc:
                d[1] += y
                d[2] += 1
                d[3] += y
    out = {}
    for k, (nt, kt, nk, kt2) in agg.items():
        thr = model.thresholds.get(frozenset(k.split("+"))) if model else None
        out[k] = {"n_true": nt, "cand_recall": kt / nt if nt else None, "kept": nk,
                  "kept_precision": kt2 / nk if nk else None, "threshold": thr}
    return out


def slice_metrics(per_doc):
    """Header lines ('Label: ...') vs narrative text, for preds and gold alike."""
    res = {}
    for want in ("header", "narrative"):
        pairs = []
        for preds, gold, r, _ in per_doc:
            t = r["text"]
            sl = lambda sp: ("header" if ctx.field_label(t, sp["start"])[0] != "none" else "narrative")  # noqa: E731
            pairs.append(([p for p in preds if sl(p) == want], [g for g in gold if sl(g) == want]))
        res[want] = H.score(pairs)
    return res


# --------------------------------------------------------------------------
def perturb(text, gold, how):
    chars = list(text)
    for g in gold:
        seg = text[g["start"]:g["end"]].lower()
        if how == "flattened":
            seg = seg.replace(" ", ".")
        chars[g["start"]:g["end"]] = list(seg)  # same length: offsets unchanged
    return "".join(chars)


def build_stress_cache():
    import es_detect
    import es_ner
    import ner_confidence
    docs = [d for d in em.load_docs() if d["split"] == "test"]
    out = {}
    for how in ("lowercase", "flattened"):
        entries = []
        for d in docs:
            t = perturb(d["text"], H.gold_person(d), how)
            nh = ner_confidence.annotate(es_ner.scan_es_ner(t), t, H.MODEL, "PER")
            entries.append({"id": d["id"], "split": "test",
                            "dict": [[h["start"], h["end"]] for h in es_detect.scan_spanish_names(t)],
                            "ner": [[h["start"], h["end"], round(h["confidence"], 6)] for h in nh]})
        out[how] = entries
        print(f"  stress cache: {how} done", flush=True)
    with open(STRESS_CACHE, "w") as f:
        json.dump(out, f)
    print(f"wrote {STRESS_CACHE}")


# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--build-stress-cache", action="store_true")
    args = ap.parse_args()
    if args.build_stress_cache:
        build_stress_cache()
        return

    lines, results = [], {}
    say = lambda s="": (print(s), lines.append(s))  # noqa: E731
    say(HYPOTHESES)
    say()

    cache = json.load(open(H.CACHE))
    docs = em.load_docs()
    by_id = {d["id"]: d for d in docs}
    rows = fresh_rows(cache["docs"], by_id)
    fit = [r for r in rows if r["split"] == "train"]
    cal = [r for r in rows if r["split"] == "validation"]
    tst = [r for r in rows if r["split"] == "test"]

    scorers = {s: make_scorer(s, fit) for s in SCORERS}
    say("=== Scorer audit: LR-full weights (fit on train only; positive = more name-like) ===")
    for k, v in sorted(scorers["LR-full"].weights().items(), key=lambda kv: -abs(kv[1])):
        say(f"  {k:18s} {v:+.2f}")
    say()

    # within-pattern separation on the calibration split, per scorer
    say("=== Within-pattern AUROC on the calibration split (phase 1 beam: 0.59-0.61) ===")
    for s in SCORERS[1:]:
        sr = score_rows(cal, scorers[s])
        cells = []
        for pat in ("dict", "ner", "dict+ner"):
            pos = [1 - c.scores[next(iter(c.scores))] for r in sr for c, y in zip(r["cands"], r["labels"])
                   if "+".join(sorted(c.mask)) == pat and y]
            neg = [1 - c.scores[next(iter(c.scores))] for r in sr for c, y in zip(r["cands"], r["labels"])
                   if "+".join(sorted(c.mask)) == pat and not y]
            a = H.auroc(pos, neg)
            cells.append(f"{pat}={a:.3f}" if a is not None else f"{pat}=n/a")
        say(f"  {s:16s} " + "  ".join(cells))
    say()

    # ---- primary test results ----
    say("=== Test split (120 docs): fit=train, calibrate=validation ===")
    base_u = apply_rule(tst, lambda c: True)
    base_i = apply_rule(tst, lambda c: c.mask == frozenset({"dict", "ner"}))
    mu, mi = metrics(base_u), metrics(base_i)
    table, store = {}, {"union": base_u, "intersection": base_i}

    def row(name, m):
        say(f"  {name:38s} P={m['P']:.3f} ({m['P']-mu['P']:+.3f})  R={m['R']:.3f} ({m['R']-mu['R']:+.3f})  "
            f"F1={m['F1']:.3f}  F2={m['F2']:.3f}")
        table[name] = m
    row("union", mu)
    row("intersection (agreement only)", mi)

    checks = {}
    for s in SCORERS:
        cal_s, tst_s = score_rows(cal, scorers[s]), score_rows(tst, scorers[s])
        for mode, v in LEVELS:
            model = fit_pccf(cal_s, pccf_params(mode, v))
            pd = apply_rule(tst_s, model.accept)
            name = cfg_name(s, mode, v)
            row(name, metrics(pd))
            store[name] = pd
            checks[name] = pattern_checks(pd, model)
        if s != "beam":
            # non-conformal reference: one global threshold on p, tuned on
            # validation for max F1 (no guarantee of any kind)
            best = max((metrics(apply_rule(cal_s, lambda c, t=t: 1 - c.scores[next(iter(c.scores))] >= t))["F1"], t)
                       for t in [i / 100 for i in range(1, 100)])
            t = best[1]
            pd = apply_rule(tst_s, lambda c, t=t: 1 - c.scores[next(iter(c.scores))] >= t)
            name = f"{s:16s} tuned-F1 threshold (no cert.)"
            row(name, metrics(pd))
            store[name] = pd
    results["test"] = table
    results["pattern_checks"] = checks
    say()

    say("  Per-pattern test checks for LR-full (candidate recall must be >= 1-a; kept precision >= target):")
    for mode, v in LEVELS:
        name = cfg_name("LR-full", mode, v)
        cells = [f"{k}: rec={d['cand_recall']:.3f} kept={d['kept']} P={(d['kept_precision'] or 0):.3f}"
                 for k, d in sorted(checks[name].items())]
        say(f"    {name:36s} " + " | ".join(cells))
    say()

    say("  Header vs narrative slices (test). NOTE: 2,081 of MEDDOCAN's 2,085 gold PERSON spans sit on")
    say("  'Label:' lines, so the narrative slice cannot test free-text disambiguation at all:")
    slices = {}
    for name in ["union", "intersection", cfg_name("LR-full", "coverage", 0.05),
                 cfg_name("LR-full", "precision", 0.85), cfg_name("LR-noFieldLabel", "coverage", 0.05)]:
        sm = slice_metrics(store[name])
        slices[name] = sm
        say(f"    {name:36s} " + " | ".join(
            f"{k}: P={m['P']:.3f} R={m['R']:.3f} gold={m['tp']+m['fn']}" for k, m in sm.items()))
    results["slices"] = slices
    say()

    say("  Paired bootstrap (2,000 resamples over test docs):")
    boots = {
        "H1 LR-full cov a=0.05 vs union": H.bootstrap(store["union"], store[cfg_name("LR-full", "coverage", 0.05)]),
        "H2 LR-full prec>=0.85 vs intersection": H.bootstrap(store["intersection"], store[cfg_name("LR-full", "precision", 0.85)]),
        "H3 LR-noFieldLabel cov a=0.05 vs union": H.bootstrap(store["union"], store[cfg_name("LR-noFieldLabel", "coverage", 0.05)]),
    }
    for k, b in boots.items():
        say(f"    {k:40s} dP=[{b['dP_95ci'][0]:+.3f},{b['dP_95ci'][1]:+.3f}] "
            f"dR=[{b['dR_95ci'][0]:+.3f},{b['dR_95ci'][1]:+.3f}] dF1=[{b['dF1_95ci'][0]:+.3f},{b['dF1_95ci'][1]:+.3f}]")
    results["bootstrap"] = boots
    say()

    # ---- CV robustness + guarantee violation rates ----
    say("=== 5-fold document CV (seed 0; each outer-train split 50/50 into fit/calibrate) ===")
    rng = random.Random(0)
    ids = [r["id"] for r in rows]
    rng.shuffle(ids)
    folds = [set(ids[i::5]) for i in range(5)]
    cv = {}
    viol = {"coverage": [0, 0], "precision": [0, 0]}
    for fi, fset in enumerate(folds):
        rest = [r for r in rows if r["id"] not in fset]
        rr = random.Random(100 + fi)
        rest_ids = [r["id"] for r in rest]
        rr.shuffle(rest_ids)
        fit_ids = set(rest_ids[: len(rest_ids) // 2])
        f_fit = [r for r in rest if r["id"] in fit_ids]
        f_cal = [r for r in rest if r["id"] not in fit_ids]
        f_tst = [r for r in rows if r["id"] in fset]
        mu_f = metrics(apply_rule(f_tst, lambda c: True))
        mi_f = metrics(apply_rule(f_tst, lambda c: c.mask == frozenset({"dict", "ner"})))
        cv.setdefault("intersection", []).append((mi_f["P"] - mu_f["P"], mi_f["R"] - mu_f["R"], mi_f["F1"] - mu_f["F1"]))
        for s in ("LR-full", "LR-noFieldLabel", "rule"):
            sc = make_scorer(s, f_fit)
            cs, ts = score_rows(f_cal, sc), score_rows(f_tst, sc)
            for mode, v in LEVELS:
                model = fit_pccf(cs, pccf_params(mode, v))
                pd = apply_rule(ts, model.accept)
                m = metrics(pd)
                cv.setdefault(cfg_name(s, mode, v), []).append((m["P"] - mu_f["P"], m["R"] - mu_f["R"], m["F1"] - mu_f["F1"]))
                if s == "LR-full":
                    for k, d in pattern_checks(pd, model).items():
                        if mode == "coverage" and d["n_true"]:
                            viol["coverage"][1] += 1
                            tol = 2 * (v * (1 - v) / d["n_true"]) ** 0.5
                            viol["coverage"][0] += d["cand_recall"] < 1 - v - tol
                        if mode == "precision" and d["kept"]:
                            viol["precision"][1] += 1
                            viol["precision"][0] += d["kept_precision"] < v
    for name, vals in cv.items():
        dP, dR, dF = zip(*vals)
        say(f"  {name:38s} dP={statistics.mean(dP):+.3f}±{statistics.stdev(dP):.3f}  "
            f"dR={statistics.mean(dR):+.3f}±{statistics.stdev(dR):.3f}  dF1={statistics.mean(dF):+.3f}±{statistics.stdev(dF):.3f}")
    say(f"  LR-full guarantee violations: coverage {viol['coverage'][0]}/{viol['coverage'][1]} (fold,pattern) cells; "
        f"precision {viol['precision'][0]}/{viol['precision'][1]}")
    results["cv"] = {k: v for k, v in cv.items()}
    results["cv_violations"] = viol
    say()

    # ---- E1: exploratory, post-hoc (NOT pre-registered) ----
    say("=== E1 (exploratory, added AFTER H2 failed; not pre-registered): global alert-tier certificate ===")
    say("  Per-pattern precision certification never kept a single-layer candidate. Does one")
    say("  global certificate over all candidates (partition='global') recover recall?")
    e1 = {}
    cal_lr, tst_lr = score_rows(cal, scorers["LR-full"]), score_rows(tst, scorers["LR-full"])
    for t in (0.80, 0.85, 0.90):
        model = pccf.PCCF(mode="precision", target_precision=t, delta=0.05, partition="global").fit(
            [c for r in cal_lr for c in r["cands"]], [y for r in cal_lr for y in r["labels"]])
        pd = apply_rule(tst_lr, model.accept)
        name = f"LR-full global precision>={t:.2f}"
        row(name, metrics(pd))
        store[name] = pd
        e1[name] = metrics(pd)
    b = H.bootstrap(store["intersection"], store["LR-full global precision>=0.85"])
    say(f"  bootstrap vs intersection (global >=0.85): dP=[{b['dP_95ci'][0]:+.3f},{b['dP_95ci'][1]:+.3f}] "
        f"dR=[{b['dR_95ci'][0]:+.3f},{b['dR_95ci'][1]:+.3f}]")
    e1_cv, e1_viol = [], [0, 0]
    for fi, fset in enumerate(folds):
        rest = [r for r in rows if r["id"] not in fset]
        rr = random.Random(100 + fi)
        rest_ids = [r["id"] for r in rest]
        rr.shuffle(rest_ids)
        fit_ids = set(rest_ids[: len(rest_ids) // 2])
        sc_ = make_scorer("LR-full", [r for r in rest if r["id"] in fit_ids])
        cs = score_rows([r for r in rest if r["id"] not in fit_ids], sc_)
        ts = score_rows([r for r in rows if r["id"] in fset], sc_)
        model = pccf.PCCF(mode="precision", target_precision=0.85, delta=0.05, partition="global").fit(
            [c for r in cs for c in r["cands"]], [y for r in cs for y in r["labels"]])
        m = metrics(apply_rule(ts, model.accept))
        mi_f = metrics(apply_rule(ts, lambda c: c.mask == frozenset({"dict", "ner"})))
        e1_cv.append((m["P"] - mi_f["P"], m["R"] - mi_f["R"]))
        e1_viol[1] += 1
        e1_viol[0] += m["P"] < 0.85
    dP, dR = zip(*e1_cv)
    say(f"  5-fold CV vs intersection (global >=0.85): dP={statistics.mean(dP):+.3f}±{statistics.stdev(dP):.3f} "
        f"dR={statistics.mean(dR):+.3f}±{statistics.stdev(dR):.3f}; folds below 0.85 precision: {e1_viol[0]}/{e1_viol[1]}")
    results["E1"] = {"test": e1, "bootstrap_vs_intersection": b, "cv": e1_cv, "cv_violations": e1_viol}
    say()

    # ---- stress ----
    stress = {}
    if os.path.exists(STRESS_CACHE):
        say("=== Stress: test names lowercased / flattened; scorer + thresholds from clean data ===")
        sc = json.load(open(STRESS_CACHE))
        lr = scorers["LR-full"]
        cal_lr = score_rows(cal, lr)
        m_cov = fit_pccf(cal_lr, pccf_params("coverage", 0.05))
        m_pre = fit_pccf(cal_lr, pccf_params("precision", 0.85))
        for how, entries in sc.items():
            texts = {d["id"]: perturb(d["text"], H.gold_person(d), how) for d in docs if d["split"] == "test"}
            srows = score_rows(fresh_rows(entries, by_id, texts), lr)
            res = {"union": metrics(apply_rule(srows, lambda c: True)),
                   "intersection": metrics(apply_rule(srows, lambda c: c.mask == frozenset({"dict", "ner"}))),
                   "LR-full cov a=0.05": metrics(apply_rule(srows, m_cov.accept)),
                   "LR-full prec>=0.85": metrics(apply_rule(srows, m_pre.accept))}
            ncand = sum(len(r["cands"]) for r in srows)
            say(f"  [{how}] candidates={ncand}")
            for k, m in res.items():
                say(f"    {k:24s} P={m['P']:.3f} R={m['R']:.3f} (TP={m['tp']} FN={m['fn']})")
            stress[how] = res
        results["stress"] = stress
        say()
    else:
        say("(stress cache missing: run with --build-stress-cache to add H5)")

    # ---- mechanical verdicts ----
    say("=== Verdicts (mechanical) ===")
    c05 = table[cfg_name("LR-full", "coverage", 0.05)]
    p85 = table[cfg_name("LR-full", "precision", 0.85)]
    nfl = table[cfg_name("LR-noFieldLabel", "coverage", 0.05)]
    b1, b2 = boots["H1 LR-full cov a=0.05 vs union"], boots["H2 LR-full prec>=0.85 vs intersection"]
    v = {
        "H1": c05["R"] >= 0.92 and c05["P"] >= mu["P"] + 0.05 and b1["dP_95ci"][0] > 0,
        "H2": p85["P"] >= 0.85 and p85["R"] > mi["R"] and b2["dR_95ci"][0] > 0,
        "H3": (nfl["P"] - mu["P"]) >= 0.5 * (c05["P"] - mu["P"]) if c05["P"] > mu["P"] else False,
        "H4": (viol["coverage"][0] + viol["precision"][0]) <= 0.10 * max(1, viol["coverage"][1] + viol["precision"][1]),
    }
    if stress:
        v["H5 (expectation)"] = all(stress[h]["union"]["R"] < 0.5 for h in stress)
    for k, ok in v.items():
        say(f"  {k}: {'SUPPORTED' if ok else 'NOT SUPPORTED'}")
    results["verdicts"] = v

    with open(OUT_TXT, "w") as f:
        f.write("\n".join(lines) + "\n")
    with open(OUT_JSON, "w") as f:
        json.dump(results, f, indent=1, default=str)
    print(f"\nwrote {OUT_TXT}")


if __name__ == "__main__":
    main()
