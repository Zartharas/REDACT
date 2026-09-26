"""
Phase 4, F2 (PCCF_PHASE4_PREREGISTRATION.md): flattened-name fixes.
  F2a  identity-field pseudonymization policy   (src/field_policy.py)
  F2b  extended segmenter on real SSA/Census     (src/flattened_names_ext.py)
Corpora: T7's template-set-B versions of all 5 datasets, seeds 42/43/44
(pccf_phase3b_message_text.py, imported unmodified; its layer cache is
reused). Results go to this phase4/ directory.
"""
import collections
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
RD = os.path.dirname(HERE)
ROOT = os.path.abspath(os.path.join(RD, "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, RD)
sys.path.insert(0, os.path.join(ROOT, "validation", "real_name_frequency"))

import field_policy  # noqa: E402
import flattened_names_ext as fx  # noqa: E402
import pccf_feasibility_meddocan as H  # noqa: E402
import pccf_phase3_telemetry as P3  # noqa: E402
import pccf_phase3b_message_text as T7  # noqa: E402

OUT_TXT = os.path.join(HERE, "pccf_phase4_flattened_results.txt")
OUT_JSON = os.path.join(HERE, "pccf_phase4_flattened_results.json")
HYP = """(Rows tagged [exploratory] were added after the pre-registered verdicts were first computed. They are not judged.
 field_policy.py's value-extent rule was bug-fixed after the first run: generic keys now take a single token. H16 was SUPPORTED both before and after.)
H16: F2a raises identity-field flattened recall from 0.504 to >= 0.95 (utility cost reported).
H17: F2b raises message-text flattened recall by >= +0.15 over the old flattened layer, pooled precision drop <= 0.02.
H17b (descriptive): F2b recall on the real SSA/Census flattened sample (baseline 15.2%)."""


def recall_split(pairs):
    out = {}
    for preds, gold in pairs:
        dd = H.em._dedup(preds)
        for g in gold:
            d = out.setdefault(f"{g['loc']}/{g['fmt']}", [0, 0])
            d[0] += any(p["start"] < g["end"] and g["start"] < p["end"] for p in dd)
            d[1] += 1
    return {k: v[0] / v[1] for k, v in sorted(out.items())}


def main():
    lines, res = [], {}
    say = lambda s="": (print(s), lines.append(s))  # noqa: E731
    say(HYP)
    say()
    say(f"F2b real-name lists available: {fx.new_shapes_available()} "
        f"(given names >= {fx.GIVEN_MIN_COUNT} SSA births: {len(fx._lists()[0])}; Census top {fx.SURNAME_TOP_N}: {len(fx._lists()[1])})")
    cache = json.load(open(T7.CACHE))
    conf = {k: [] for k in ("union (old flattened)", "union (extended flattened)",
                            "policy + union (old)", "policy + union (extended)",
                            "E4 [exploratory] union (ext. without initial+surname)",
                            "E4 [exploratory] policy + union (ext. without initial+surname)")}
    new_shape_tp, new_shape_fp, fp_examples = collections.Counter(), collections.Counter(), collections.Counter()
    policy_nongold, policy_allow, policy_nongold_vals = 0, 0, collections.Counter()
    for seed in P3.SEEDS:
        for ds, versions in T7.rows_for(seed, cache).items():
            for r in versions["B"]:
                t, gold = r["text"], r["gold"]
                ner, flat = r["hits"]["ner"], r["hits"]["flat"]
                ext = fx.scan_flattened_ext(t)
                allv = field_policy.identity_values(t)
                pol = [h for h in allv if not h["allowlisted"]]
                policy_allow += sum(h["allowlisted"] for h in allv)
                for h in pol:
                    if not any(h["start"] < g["end"] and g["start"] < h["end"] for g in gold):
                        policy_nongold += 1
                        policy_nongold_vals[h["value"].lower()] += 1
                for h in ext:
                    if h["method"].startswith("flattened_ext"):
                        ok = any(h["start"] < g["end"] and g["start"] < h["end"] for g in gold)
                        (new_shape_tp if ok else new_shape_fp)[h["method"]] += 1
                        if not ok:
                            fp_examples[t[h["start"]:h["end"]]] += 1
                ext2 = fx.scan_flattened_ext(t, shapes=("given+surname", "given+digits"))
                conf["E4 [exploratory] union (ext. without initial+surname)"].append((ner + ext2, gold))
                conf["E4 [exploratory] policy + union (ext. without initial+surname)"].append((pol + ner + ext2, gold))
                conf["union (old flattened)"].append((ner + flat, gold))
                conf["union (extended flattened)"].append((ner + ext, gold))
                conf["policy + union (old)"].append((pol + ner + flat, gold))
                conf["policy + union (extended)"].append((pol + ner + ext, gold))
    say()
    say("=== Pooled (5 datasets x 3 seeds, template set B) ===")
    table = {}
    for k, pairs in conf.items():
        m = H.score(pairs)
        m["recall_split"] = recall_split(pairs)
        table[k] = m
        say(f"  {k:60s} P={m['P']:.3f} R={m['R']:.3f}  " + "  ".join(f"{a}={b:.3f}" for a, b in m["recall_split"].items()))
    say()
    say("  New-shape hits (F2b), gold-overlapping vs not: " + ", ".join(
        f"{k}: {new_shape_tp[k]} TP / {new_shape_fp[k]} FP" for k in sorted(set(new_shape_tp) | set(new_shape_fp))))
    say(f"  Most frequent F2b false-positive tokens: {fp_examples.most_common(12)}")
    say(f"  F2a utility cost: {policy_nongold} non-gold identity values pseudonymized "
        f"(top: {policy_nongold_vals.most_common(12)}); {policy_allow} allowlisted values left in clear")
    res.update({"table": table, "new_shape_tp": new_shape_tp, "new_shape_fp": new_shape_fp,
                "fp_examples": fp_examples.most_common(30), "policy_nongold": policy_nongold,
                "policy_nongold_top": policy_nongold_vals.most_common(30), "policy_allowlisted": policy_allow})
    say()

    # H17b: real SSA/Census flattened sample (build_real_name_test imported unmodified)
    try:
        import build_real_name_test as brt
        given, sur = brt.load_ssa_given_names(), brt.load_census_surnames()
        toks = [g + s for g, s in zip(brt.weighted_sample(given, 2000, 20260808),
                                      brt.weighted_sample(sur, 2000, 20260808 + 1))]
        old_hit = brt.measure_recall(toks)[0]
        new_hit = sum(any(h["start"] == 12 for h in fx.scan_flattened_ext(
            f"sudo[1234]: {tok} : USER=root ; COMMAND=/usr/bin/true")) for tok in toks)
        say(f"H17b real SSA/Census sample (n=2000): old layer {old_hit/2000:.1%}, extended {new_hit/2000:.1%}")
        res["H17b"] = {"old": old_hit / 2000, "extended": new_hit / 2000}
    except Exception as e:  # noqa: BLE001
        say(f"H17b not run: {e!r}")
    say()

    old, ext = table["union (old flattened)"], table["union (extended flattened)"]
    pol = table["policy + union (old)"]
    v = {"H16": pol["recall_split"]["field/flattened"] >= 0.95,
         "H17": (ext["recall_split"]["message/flattened"] - old["recall_split"]["message/flattened"]) >= 0.15
                and (old["P"] - ext["P"]) <= 0.02}
    say("=== Verdicts (mechanical) ===")
    for k, ok in v.items():
        say(f"  {k}: {'SUPPORTED' if ok else 'NOT SUPPORTED'}")
    res["verdicts"] = v
    open(OUT_TXT, "w").write("\n".join(lines) + "\n")
    json.dump(res, open(OUT_JSON, "w"), indent=1, default=str)


if __name__ == "__main__":
    main()
