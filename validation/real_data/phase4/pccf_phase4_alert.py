"""
Phase 4, F3 (PCCF_PHASE4_PREREGISTRATION.md): alert-tier fixes for the T5
failure, where precision certified on other log sources did not hold on
an unseen source.
  F3a  per-source calibration: first 30% of the source's own lines calibrate, the rest test
  F3b  online adaptive threshold (ACI-style) driven by analyst verdicts on alerts
Corpora and cache: T3 (pccf_phase3_telemetry.py, imported unmodified).
Scorer: KV-LR (pccf_context.kv_features), fit on the other four datasets.
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
RD = os.path.dirname(HERE)
ROOT = os.path.abspath(os.path.join(RD, "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, RD)

import pccf  # noqa: E402
import pccf_context as ctx  # noqa: E402
import pccf_phase3_telemetry as P3  # noqa: E402

OUT_TXT = os.path.join(HERE, "pccf_phase4_alert_results.txt")
OUT_JSON = os.path.join(HERE, "pccf_phase4_alert_results.json")
TARGET, DELTA, ETA, BURN = 0.85, 0.05, 0.01, 100
HYP = """H18 (F3a per-source calibration): precision >= 0.85 in >= 90% of units that keep >= 20.
H19 (F3b online ACI-style): on every held-out source with >= 50 kept after a 100-candidate burn-in,
    post-burn-in precision >= 0.83. Recall reported alongside."""


def lr_for(fit_rows):
    return ctx.LogisticScorer(l2=1.0).fit([f for r in fit_rows for f in r["feats"]],
                                          [y for r in fit_rows for y in r["labels"]])


def scored(rows, sc):
    return P3.score_rows(rows, sc)


def cert(rows):
    return pccf.PCCF(mode="precision", target_precision=TARGET, delta=DELTA, partition="global",
                     search="bonferroni").fit([c for r in rows for c in r["cands"]], [y for r in rows for y in r["labels"]])


def p_of(c):
    return 1 - c.scores[next(iter(c.scores))]


def main():
    lines, res = [], {"F3a": [], "F3b": []}
    say = lambda s="": (print(s), lines.append(s))  # noqa: E731
    say(HYP)
    say()
    cache = json.load(open(P3.CACHE))
    say("=== F3a per-source calibration (30% calibrate / 70% test, in line order) | cross-source T5 for reference ===")
    for seed in P3.SEEDS:
        rows = P3.rows_for(seed, cache)
        for held in P3.DATASETS:
            sc = lr_for([r for ds in P3.DATASETS if ds != held for r in rows[ds]])
            own = scored(rows[held], sc)
            k = int(0.3 * len(own))
            m = cert(own[:k])
            per = P3.apply_rule(own[k:], m.accept)
            met = P3.metrics(per)
            kept = sum(len(a) for *_, a in per)
            xm = cert(scored([r for ds in P3.DATASETS if ds != held for r in rows[ds]], sc))
            xper = P3.apply_rule(own[k:], xm.accept)
            xmet, xkept = P3.metrics(xper), sum(len(a) for *_, a in xper)
            res["F3a"].append({"seed": seed, "held": held, "kept": kept, "P": met["P"], "R": met["R"],
                               "cross_kept": xkept, "cross_P": xmet["P"], "cross_R": xmet["R"]})
            say(f"  seed={seed} {held:13s} per-source: kept={kept:4d} P={met['P']:.3f} R={met['R']:.3f} | "
                f"cross-source: kept={xkept:4d} P={xmet['P']:.3f} R={xmet['R']:.3f}")
    say()

    say(f"=== F3b online adaptive threshold (target {TARGET}, eta {ETA}, burn-in {BURN} alerts) ===")
    for seed in P3.SEEDS:
        rows = P3.rows_for(seed, cache)
        for held in P3.DATASETS:
            others = [r for ds in P3.DATASETS if ds != held for r in rows[ds]]
            sc = lr_for(others)
            xm = cert(scored(others, sc))
            q = xm.thresholds.get(pccf.PCCF.GLOBAL, -1)
            lam = 1 - q if q not in (None, float("-inf")) and q >= 0 else 0.5
            lam0 = lam
            stream = scored(rows[held], sc)
            n_alert = tp_post = fp_post = 0
            true_post = 0  # true candidates seen after the burn-in point (candidate-level recall denominator)
            for r in stream:
                for c, y in zip(r["cands"], r["labels"]):
                    if n_alert >= BURN:
                        true_post += y
                    if p_of(c) >= lam:
                        n_alert += 1
                        if n_alert > BURN:
                            tp_post += y
                            fp_post += (not y)
                        err = 0 if y else 1
                        lam = min(1.0, max(0.0, lam + ETA * (err - (1 - TARGET))))
            kept_post = tp_post + fp_post
            prec = tp_post / kept_post if kept_post else float("nan")
            crec = tp_post / true_post if true_post else float("nan")
            res["F3b"].append({"seed": seed, "held": held, "lambda0": lam0, "lambda_end": lam, "alerts": n_alert,
                               "kept_post": kept_post, "P_post": prec, "true_alerts_post": tp_post,
                               "candidate_recall_post": crec})
            say(f"  seed={seed} {held:13s} lambda {lam0:.2f}->{lam:.2f} alerts={n_alert:5d} post-burn-in kept={kept_post:5d} "
                f"P={prec:.3f} candidate recall={crec:.3f}")
    say()
    el = [u for u in res["F3a"] if u["kept"] >= 20]
    h18 = sum(u["P"] >= TARGET for u in el) >= 0.9 * len(el) if el else False
    el2 = [u for u in res["F3b"] if u["kept_post"] >= 50]
    h19 = bool(el2) and all(u["P_post"] >= TARGET - 0.02 for u in el2)
    say(f"  F3a eligible units: {len(el)}, precision >= {TARGET}: {sum(u['P'] >= TARGET for u in el)}")
    say(f"  F3b eligible units: {len(el2)}, precision >= {TARGET-0.02:.2f}: {sum(u['P_post'] >= TARGET - 0.02 for u in el2)}")
    say("=== Verdicts (mechanical) ===")
    say(f"  H18: {'SUPPORTED' if h18 else 'NOT SUPPORTED'}")
    say(f"  H19: {'SUPPORTED' if h19 else 'NOT SUPPORTED'}")
    res["verdicts"] = {"H18": h18, "H19": h19}
    open(OUT_TXT, "w").write("\n".join(lines) + "\n")
    json.dump(res, open(OUT_JSON, "w"), indent=1, default=str)


if __name__ == "__main__":
    main()
