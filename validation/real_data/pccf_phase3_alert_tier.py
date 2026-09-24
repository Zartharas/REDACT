"""
PCCF phase 3, T5: the alert tier with its construction fixed in advance
(PCCF_PHASE3_PREREGISTRATION.md). The construction is partition="global",
precision certificate, search="bonferroni" over R in {0.01..0.50},
target 0.85, delta 0.05.

Evaluation units:
  - MEDDOCAN 5-fold CV (LR-full; same folds and seed as phase 2), run here
  - T2 test set, read from pccf_phase3_freetext_results.json
  - T3 held-out dataset x seed, read from pccf_phase3_telemetry_results.json
Run the T2 and T3 harnesses first.
"""
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
import pccf_feasibility_meddocan as H  # noqa: E402
import pccf_phase2_meddocan as P2  # noqa: E402

OUT_TXT = os.path.join(HERE, "pccf_phase3_alert_results.txt")
OUT_JSON = os.path.join(HERE, "pccf_phase3_alert_results.json")
HYP = """H11a (validity): test precision >= 0.85 in >= 90% of evaluation units that keep >= 20 candidates.
H11b (utility): on MEDDOCAN CV, mean recall exceeds the intersection rule's mean recall while H11a holds."""


def main():
    lines = []
    say = lambda s="": (print(s), lines.append(s))  # noqa: E731
    say(HYP)
    say()
    cache = json.load(open(H.CACHE))
    by = {d["id"]: d for d in em.load_docs()}
    rows = P2.fresh_rows(cache["docs"], by)
    rng = random.Random(0)
    ids = [r["id"] for r in rows]
    rng.shuffle(ids)
    folds = [set(ids[i::5]) for i in range(5)]
    units, rec_alert, rec_inter = [], [], []
    say("=== MEDDOCAN 5-fold CV (LR-full) ===")
    for fi, fset in enumerate(folds):
        rest = [r for r in rows if r["id"] not in fset]
        rr = random.Random(100 + fi)
        rid = [r["id"] for r in rest]
        rr.shuffle(rid)
        fit_ids = set(rid[: len(rid) // 2])
        sc = P2.make_scorer("LR-full", [r for r in rest if r["id"] in fit_ids])
        cs = P2.score_rows([r for r in rest if r["id"] not in fit_ids], sc)
        ts = P2.score_rows([r for r in rows if r["id"] in fset], sc)
        m = pccf.PCCF(mode="precision", target_precision=0.85, delta=0.05, partition="global",
                      search="bonferroni").fit([c for r in cs for c in r["cands"]], [y for r in cs for y in r["labels"]])
        pd = P2.apply_rule(ts, m.accept)
        met = P2.metrics(pd)
        kept = sum(len(a) for *_, a in pd)
        mi = P2.metrics(P2.apply_rule(ts, lambda c: c.mask == frozenset({"dict", "ner"})))
        rec_alert.append(met["R"])
        rec_inter.append(mi["R"])
        units.append({"source": f"MEDDOCAN fold {fi}", "kept": kept, "P": met["P"], "R": met["R"]})
        say(f"  fold {fi}: threshold R<={m.thresholds.get(pccf.PCCF.GLOBAL)} kept={kept} P={met['P']:.3f} R={met['R']:.3f} "
            f"| intersection P={mi['P']:.3f} R={mi['R']:.3f}")
    say(f"  mean recall: alert={statistics.mean(rec_alert):.3f} intersection={statistics.mean(rec_inter):.3f}")
    say()

    t2 = json.load(open(os.path.join(HERE, "pccf_phase3_freetext_results.json")))["T5_unit"]
    units.append({"source": "T2 Spanish free text", "kept": t2["kept"], "P": t2["metrics"]["P"], "R": t2["metrics"]["R"]})
    for u in json.load(open(os.path.join(HERE, "pccf_phase3_telemetry_results.json")))["t5_units"]:
        units.append({"source": f"T3 {u['held']} seed {u['seed']}", "kept": u["kept"], "P": u["P"], "R": u["R"]})

    say("=== All evaluation units ===")
    elig = [u for u in units if u["kept"] >= 20]
    for u in units:
        flag = "" if u["kept"] < 20 else ("  ok" if u["P"] >= 0.85 else "  VIOLATION")
        say(f"  {u['source']:28s} kept={u['kept']:4d} P={u['P']:.3f} R={u['R']:.3f}{flag}")
    ok = sum(u["P"] >= 0.85 for u in elig)
    h11a = ok >= 0.9 * len(elig)
    h11b = h11a and statistics.mean(rec_alert) > statistics.mean(rec_inter)
    say()
    say(f"  eligible units (kept >= 20): {len(elig)}; precision >= 0.85 in {ok}")
    say("=== Verdicts (mechanical) ===")
    say(f"  H11a: {'SUPPORTED' if h11a else 'NOT SUPPORTED'}")
    say(f"  H11b: {'SUPPORTED' if h11b else 'NOT SUPPORTED'}"
        + ("" if h11a else " (conditional on H11a)"))
    open(OUT_TXT, "w").write("\n".join(lines) + "\n")
    json.dump({"units": units, "H11a": h11a, "H11b": h11b,
               "meddocan_mean_recall": {"alert": statistics.mean(rec_alert), "intersection": statistics.mean(rec_inter)}},
              open(OUT_JSON, "w"), indent=1)


if __name__ == "__main__":
    main()
