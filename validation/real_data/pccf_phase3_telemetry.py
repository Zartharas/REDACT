"""
PCCF phase 3, T3 (+ its T5 units): security telemetry, which is the
paper's domain (PCCF_PHASE3_PREREGISTRATION.md).

Corpora come from inject_and_evaluate.py's builders, imported and
unmodified: OpenSSH, Linux and Thunderbird (Loghub, user-field
injection), windows_event (real Security-Auditing samples) and cloudtrail
(real flaws.cloud records). Injection seeds are 42/43/44. Only PERSON is
scored.

Layers (K=2): "ner" = detect.scan_ner (English Presidio) and "flat" =
detect.scan_flattened. The production comparator is
detect.detect_all_field_gated(use_flattened=True): raw lines, plus the
header-stripped simulation for syslog, exactly as inject_and_evaluate.py
measures it.

Context features (pccf_context.kv_features): the field key or syslog
phrase right before the value is the telemetry analogue of MEDDOCAN's
"Nombre:" label.

Protocol: leave-one-dataset-out. For each held-out dataset and seed, the
other four datasets (same seed) are split 50/50 by line into fit and
calibrate.

  python validation/real_data/pccf_phase3_telemetry.py --build-cache   # resumable; repeat until "complete"
  python validation/real_data/pccf_phase3_telemetry.py
"""
import argparse
import hashlib
import json
import os
import random
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, HERE)

import pccf  # noqa: E402
import pccf_context as ctx  # noqa: E402
import pccf_feasibility_meddocan as H  # noqa: E402

CACHE = os.path.join(ROOT, "output", "pccf_phase3_telemetry_cache.json")
OUT_TXT = os.path.join(HERE, "pccf_phase3_telemetry_results.txt")
OUT_JSON = os.path.join(HERE, "pccf_phase3_telemetry_results.json")
SEEDS = (42, 43, 44)
DATASETS = ["OpenSSH", "Linux", "Thunderbird", "windows_event", "cloudtrail"]
LOG_TYPE = {"OpenSSH": "syslog", "Linux": "syslog", "Thunderbird": "syslog",
            "windows_event": "windows_event", "cloudtrail": "cloudtrail"}

HYP = """H8: KV rule scorer, coverage a=0.05, pooled over held-out datasets and seeds: precision >= union + 0.05,
    recall drop <= 0.03 vs union, bootstrap 95% CI of dP excludes 0.
H9 (descriptive): recall by name format (spaced vs flattened) for union, PCCF, production field-gated.
H8c (descriptive): PCCF vs production detect_all_field_gated, side by side.
T5 units: global Bonferroni alert tier (target 0.85) per (held-out dataset, seed); precision >= 0.85 where >= 20 kept."""


def corpora(seed):
    cwd = os.getcwd()
    os.chdir(HERE)
    try:
        import inject_and_evaluate as ie
        out = {}
        for name, pat in ie.USER_FIELD_DATASETS.items():
            out[name] = ie.build_user_field_corpus(name, pat, seed=seed)[0]
        out["windows_event"] = ie.build_windows_event_corpus(seed=seed)[0]
        out["cloudtrail"] = ie.build_cloudtrail_corpus(seed=seed)[0]
        return out, ie
    finally:
        os.chdir(cwd)


def key(text):
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def build_cache(budget=150):
    import detect
    import ner_confidence
    t0 = time.perf_counter()
    cache = json.load(open(CACHE)) if os.path.exists(CACHE) else {}
    todo = []
    for seed in SEEDS:
        corp, ie = corpora(seed)
        for ds, entries in corp.items():
            for e in entries:
                k = key(ds + "\x00" + e["log"])
                if k not in cache:
                    todo.append((k, ds, e["log"]))
    seen = set()
    todo = [t for t in todo if not (t[0] in seen or seen.add(t[0]))]
    print(f"  {len(cache)} cached, {len(todo)} to do", flush=True)
    done = 0
    for k, ds, text in todo:
        if time.perf_counter() - t0 > budget:
            break
        ner = [h for h in detect.scan_ner(text) if h["type"] == "PERSON"]
        ner = ner_confidence.annotate(ner, text, "en_core_web_lg", "PERSON") if ner else []
        flat = [h for h in detect.scan_flattened(text) if h["type"] == "PERSON"]
        fg = [h for h in detect.detect_all_field_gated(text, log_type=LOG_TYPE[ds], use_flattened=True)
              if h["type"] == "PERSON"]
        rec = {"ner": [[h["start"], h["end"], round(h["confidence"], 6)] for h in ner],
               "flat": [[h["start"], h["end"]] for h in flat],
               "fg": [[h["start"], h["end"]] for h in fg]}
        if LOG_TYPE[ds] == "syslog":
            pre, body = ie.strip_syslog_header(text)
            rec["fg_strip"] = [[h["start"] + pre, h["end"] + pre] for h in
                               detect.detect_all_field_gated(body, log_type="syslog", use_flattened=True)
                               if h["type"] == "PERSON"]
        cache[k] = rec
        done += 1
    json.dump(cache, open(CACHE, "w"))
    left = len(todo) - done
    print(f"  processed {done} lines in {time.perf_counter()-t0:.0f}s; {left} left"
          + ("  -> complete" if left == 0 else "  -> run --build-cache again"))


# --------------------------------------------------------------------------
def rows_for(seed, cache):
    corp, _ = corpora(seed)
    out = {}
    for ds, entries in corp.items():
        rows = []
        for i, e in enumerate(entries):
            rec = cache[key(ds + "\x00" + e["log"])]
            hits = {"ner": [{"type": "PERSON", "start": s, "end": t, "confidence": c} for s, t, c in rec["ner"]],
                    "flat": [{"type": "PERSON", "start": s, "end": t} for s, t in rec["flat"]]}
            cands = pccf.build_candidates(hits, "PERSON")
            gold = [{"type": "PERSON", "start": p["start"], "end": p["end"],
                     "fmt": "spaced" if " " in p["injected_value"] else "flattened"}
                    for p in e["pii"] if p["type"] == "PERSON"]
            rows.append({"id": f"{ds}:{seed}:{i}", "ds": ds, "text": e["log"], "hits": hits, "cands": cands,
                         "gold": gold, "labels": [H.label(c, gold) for c in cands],
                         "feats": [ctx.kv_features(e["log"], c) for c in cands],
                         "fg": rec["fg"], "fg_strip": rec.get("fg_strip")})
        out[ds] = rows
    return out


def score_rows(rows, scorer):
    out = []
    for r in rows:
        cands = pccf.build_candidates(r["hits"], "PERSON")
        if scorer is not None and cands:
            ctx.apply_scores(cands, scorer.predict(r["feats"]))
        out.append({**r, "cands": cands})
    return out


def apply_rule(rows, accept):
    per = []
    for r in rows:
        acc = {id(c) for c in r["cands"] if accept(c)}
        keep = {id(h) for c in r["cands"] if id(c) in acc for _, h in c.members}
        preds = [h for h in r["hits"]["ner"] + r["hits"]["flat"] if id(h) in keep]
        per.append((preds, r["gold"], r, acc))
    return per


def fixed(rows, field):
    return [([{"type": "PERSON", "start": s, "end": t} for s, t in (r[field] or [])], r["gold"], r, set())
            for r in rows]


def metrics(per):
    return H.score([(p, g) for p, g, _, _ in per])


def recall_by_format(per):
    out = {}
    for preds, gold, _, _ in per:
        dd = H.em._dedup(preds)
        for g in gold:
            d = out.setdefault(g["fmt"], [0, 0])
            d[0] += any(p["start"] < g["end"] and g["start"] < p["end"] for p in dd)
            d[1] += 1
    return {k: (v[0] / v[1] if v[1] else None, v[1]) for k, v in out.items()}


def fit_model(rows, params):
    return pccf.PCCF(**params).fit([c for r in rows for c in r["cands"]], [y for r in rows for y in r["labels"]])


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
    by_seed = {s: rows_for(s, cache) for s in SEEDS}

    say("=== Corpus (seed 42) ===")
    for ds in DATASETS:
        rows = by_seed[42][ds]
        pt = H.pattern_table(rows)
        say(f"  {ds:13s} lines={len(rows):5d} gold={sum(len(r['gold']) for r in rows):4d}  " + "  ".join(
            f"{k}:n={d['n']},P={d['precision']:.2f}" for k, d in pt.items()))
    say()

    configs = {"union": None, "intersection": None, "ner alone": None, "flat alone": None,
               "production field-gated (raw)": None, "production field-gated (header-stripped sim.)": None}
    pooled = {k: [] for k in configs}
    SCORERS = {"KV-rule": lambda fit: ctx.KVRuleScorer(),
               "KV-LR": lambda fit: ctx.LogisticScorer(l2=1.0).fit([f for r in fit for f in r["feats"]],
                                                                    [y for r in fit for y in r["labels"]])}
    for sn in SCORERS:
        for a in (0.02, 0.05, 0.10):
            pooled[f"{sn} coverage a={a:.2f}"] = []
    pooled["T5 alert (KV-rule, global bonferroni >=0.85)"] = []
    t5_units, per_ds, cov_checks = [], {}, []

    for seed in SEEDS:
        for held in DATASETS:
            tst = by_seed[seed][held]
            rest = [r for ds in DATASETS if ds != held for r in by_seed[seed][ds]]
            rng = random.Random(seed * 100 + DATASETS.index(held))
            ids = [r["id"] for r in rest]
            rng.shuffle(ids)
            fit_ids = set(ids[: len(ids) // 2])
            fit = [r for r in rest if r["id"] in fit_ids]
            cal = [r for r in rest if r["id"] not in fit_ids]
            units = {
                "union": apply_rule(tst, lambda c: True),
                "intersection": apply_rule(tst, lambda c: c.mask == frozenset({"ner", "flat"})),
                "ner alone": [([h for h in r["hits"]["ner"]], r["gold"], r, set()) for r in tst],
                "flat alone": [([h for h in r["hits"]["flat"]], r["gold"], r, set()) for r in tst],
                "production field-gated (raw)": fixed(tst, "fg"),
                "production field-gated (header-stripped sim.)": fixed(tst, "fg_strip" if LOG_TYPE[held] == "syslog" else "fg"),
            }
            for sn, mk in SCORERS.items():
                sc = mk(fit)
                cs, ts = score_rows(cal, sc), score_rows(tst, sc)
                for a in (0.02, 0.05, 0.10):
                    model = fit_model(cs, dict(mode="coverage", alpha=a))
                    pd = apply_rule(ts, model.accept)
                    units[f"{sn} coverage a={a:.2f}"] = pd
                    if sn == "KV-rule" and a == 0.05:
                        for c, y in zip([c for r in ts for c in r["cands"]], [y for r in ts for y in r["labels"]]):
                            pass
                        agg = {}
                        for _, _, r, acc in pd:
                            for c, y in zip(r["cands"], r["labels"]):
                                k = "+".join(sorted(c.mask))
                                d = agg.setdefault(k, [0, 0])
                                d[0] += y and id(c) in acc
                                d[1] += y
                        cov_checks.append((seed, held, agg))
                if sn == "KV-rule":
                    am = fit_model(cs, dict(mode="precision", target_precision=0.85, delta=0.05,
                                            partition="global", search="bonferroni"))
                    apd = apply_rule(ts, am.accept)
                    units["T5 alert (KV-rule, global bonferroni >=0.85)"] = apd
                    kept = sum(len(acc) for *_, acc in apd)
                    t5_units.append({"seed": seed, "held": held, "kept": kept, **metrics(apd)})
            for k, v in units.items():
                pooled[k] += v
                per_ds.setdefault(held, {}).setdefault(k, []).extend(v)

    say("=== Pooled over held-out datasets x seeds ===")
    mu = metrics(pooled["union"])
    table = {}
    for k, v in pooled.items():
        m = metrics(v)
        table[k] = m
        say(f"  {k:48s} P={m['P']:.3f} ({m['P']-mu['P']:+.3f})  R={m['R']:.3f} ({m['R']-mu['R']:+.3f})  F1={m['F1']:.3f}")
    say()
    say("=== Per held-out dataset (pooled over seeds): union | KV-rule a=0.05 | KV-LR a=0.05 | field-gated raw | field-gated strip ===")
    ds_table = {}
    for ds in DATASETS:
        cells = []
        for k in ("union", "KV-rule coverage a=0.05", "KV-LR coverage a=0.05", "production field-gated (raw)",
                  "production field-gated (header-stripped sim.)"):
            m = metrics(per_ds[ds][k])
            ds_table.setdefault(ds, {})[k] = m
            cells.append(f"P={m['P']:.2f} R={m['R']:.2f}")
        say(f"  {ds:13s} " + " | ".join(cells))
    say()
    say("=== H9: recall by name format (pooled) ===")
    h9 = {}
    for k in ("union", "ner alone", "flat alone", "KV-rule coverage a=0.05", "production field-gated (raw)",
              "production field-gated (header-stripped sim.)"):
        rb = recall_by_format(pooled[k])
        h9[k] = rb
        say(f"  {k:48s} " + "  ".join(f"{f}: R={v[0]:.3f} (n={v[1]})" for f, v in sorted(rb.items())))
    say()
    say("=== KV-rule a=0.05 per-pattern candidate recall (pooled over units) ===")
    agg = {}
    for _, _, a in cov_checks:
        for k, (kt, nt) in a.items():
            d = agg.setdefault(k, [0, 0])
            d[0] += kt
            d[1] += nt
    for k, (kt, nt) in sorted(agg.items()):
        say(f"  {k:9s} n_true={nt:5d} cand_recall={kt/nt if nt else float('nan'):.3f}")
    say()
    b = H.bootstrap(pooled["union"], pooled["KV-rule coverage a=0.05"], n_boot=1000)
    say(f"  Bootstrap (1,000 resamples over lines) KV-rule a=0.05 vs union: dP=[{b['dP_95ci'][0]:+.3f},{b['dP_95ci'][1]:+.3f}] "
        f"dR=[{b['dR_95ci'][0]:+.3f},{b['dR_95ci'][1]:+.3f}]")
    say()
    say("=== T5 units (KV-rule, global bonferroni >=0.85) ===")
    for u in t5_units:
        say(f"  seed={u['seed']} held={u['held']:13s} kept={u['kept']:4d} P={u['P']:.3f} R={u['R']:.3f}")
    say()
    m8 = table["KV-rule coverage a=0.05"]
    v = {"H8": m8["P"] >= mu["P"] + 0.05 and (mu["R"] - m8["R"]) <= 0.03 and b["dP_95ci"][0] > 0}
    say("=== Verdicts (mechanical) ===")
    for k, ok in v.items():
        say(f"  {k}: {'SUPPORTED' if ok else 'NOT SUPPORTED'}")
    results.update({"pooled": table, "per_dataset": ds_table, "H9": h9, "bootstrap": b,
                    "t5_units": t5_units, "verdicts": v, "pattern_recall": agg})
    open(OUT_TXT, "w").write("\n".join(lines) + "\n")
    json.dump(results, open(OUT_JSON, "w"), indent=1, default=str)
    print(f"wrote {OUT_TXT}")


if __name__ == "__main__":
    main()
