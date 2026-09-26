"""
PCCF phase 3, T7: names in log MESSAGE text (PCCF_PHASE3_PREREGISTRATION.md, T7).

T3's caveat: names were injected only into identity fields, so the
field-key cue was aligned with the gold labels by construction. This
harness keeps T3's corpora (identity-field injection via the unmodified
inject_and_evaluate.py builders) and adds, per line:
  - p=0.15: an appended sentence naming a person
    (50% spaced Faker name(), 50% flattened Faker user_name())
  - p=0.10 (exclusive): a distractor sentence naming a software product
    (no PII; this tests precision)
Template and product set A is used for the fit/calibrate datasets; set B
(disjoint) for the held-out dataset. The injection decisions are
identical for A and B; only the wording differs.

Configurations compared (all coverage, a=0.05):
  S1 KV-rule, mask groups            (the T3 configuration)
  S2 KV-rule, mask x key groups      (partition fix only)
  S3 Hybrid, mask x key groups       (KV rule keyed / text-LR unkeyed; the full fix)
  S4 KV-LR+text, mask x key groups   (one LR over all features)

  python validation/real_data/pccf_phase3b_message_text.py --build-cache   # resumable; repeat until "complete"
  python validation/real_data/pccf_phase3b_message_text.py
"""
import argparse
import json
import os
import random
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, HERE)

import numpy as np  # noqa: E402

import pccf  # noqa: E402
import pccf_context as ctx  # noqa: E402
import pccf_feasibility_meddocan as H  # noqa: E402
import pccf_phase3_telemetry as P3  # noqa: E402

CACHE = os.path.join(ROOT, "output", "pccf_phase3b_message_cache.json")
OUT_TXT = os.path.join(HERE, "pccf_phase3b_message_results.txt")
OUT_JSON = os.path.join(HERE, "pccf_phase3b_message_results.json")

HYP = """(Rows tagged [exploratory] were added after the pre-registered verdicts were first computed. They are not judged.)
H12: T3 config (KV rule, mask-only groups) recalls message-text names at < 0.80 x union's message-text recall.
H13: Hybrid + mask x key groups: pooled per-group candidate recall >= 0.95 - tol for every group with n_true >= 19.
H14: Hybrid + mask x key groups: message-text recall >= union's - 0.03 AND precision >= union + 0.05, CI of dP excludes 0.
H15 (descriptive): Hybrid vs production detect_all_field_gated, by gold location and name format."""

NAME_T = {
    "A": ["Password reset approved by {name}.", "Ticket updated by {name} after review.",
          "{name} reported the service outage.", "Escalated to {name} for follow-up."],
    "B": ["Change request signed off by {name}.", "Contact {name} regarding this session.",
          "{name} acknowledged the alert.", "Access review completed by {name}."],
}
DISTRACT_T = {
    "A": ["Restarted {prod} service.", "{prod} health check passed."],
    "B": ["Upgraded {prod} to the latest build.", "Scheduled scan by {prod} completed."],
}
PRODUCTS = {
    "A": ["Apache Tomcat", "Jenkins Pipeline", "Kafka Connect", "Redis Sentinel", "Nginx Proxy", "Grafana Agent"],
    "B": ["Windows Defender", "Oracle Database", "Splunk Forwarder", "Elastic Beats", "Postgres Replica", "Consul Agent"],
}


def decisions(seed, ds, n):
    """Per-line injection decisions, shared by template sets A and B."""
    from faker import Faker
    rng = random.Random(f"t7-{seed}-{ds}")
    fk = Faker()
    Faker.seed(seed + 1000 + P3.DATASETS.index(ds))
    out = []
    for _ in range(n):
        u = rng.random()
        ti, pi = rng.randrange(4), rng.randrange(6)
        flat = rng.random() < 0.5
        name = fk.user_name() if flat else fk.name()
        kind = "name" if u < 0.15 else ("distract" if u < 0.25 else None)
        out.append({"kind": kind, "ti": ti, "pi": pi, "name": name, "flat": flat})
    return out


def augment(ds, entries, decs, tset):
    out = []
    for e, d in zip(entries, decs):
        log = e["log"]
        pii = [dict(p, loc="field") for p in e["pii"] if p["type"] == "PERSON"]
        if d["kind"]:
            if d["kind"] == "name":
                sent = NAME_T[tset][d["ti"]].format(name=d["name"])
            else:
                sent = DISTRACT_T[tset][d["ti"] % 2].format(prod=PRODUCTS[tset][d["pi"]])
            if ds == "cloudtrail" and log.endswith("}"):
                prefix = log[:-1] + ', "errorMessage": "'
                new = prefix + sent + '"}'
                base = len(prefix)
            else:
                prefix = log + " "
                new = prefix + sent
                base = len(prefix)
            if d["kind"] == "name":
                off = base + sent.index(d["name"])
                pii.append({"type": "PERSON", "start": off, "end": off + len(d["name"]),
                            "injected_value": d["name"], "loc": "message"})
            log = new
        out.append({"log": log, "pii": pii})
    return out


def corpora(seed):
    base, _ = P3.corpora(seed)
    res = {}
    for ds, entries in base.items():
        decs = decisions(seed, ds, len(entries))
        res[ds] = {t: augment(ds, entries, decs, t) for t in ("A", "B")}
    return res


def build_cache(budget=150):
    import detect
    import ner_confidence
    t0 = time.perf_counter()
    if os.path.exists(CACHE):
        cache = json.load(open(CACHE))
    else:
        cache = json.load(open(P3.CACHE)) if os.path.exists(P3.CACHE) else {}
    _, ie = P3.corpora(42)
    todo, seen = [], set()
    for seed in P3.SEEDS:
        for ds, versions in corpora(seed).items():
            for t, entries in versions.items():
                for e in entries:
                    k = P3.key(ds + "\x00" + e["log"])
                    if k not in cache and k not in seen:
                        seen.add(k)
                        todo.append((k, ds, e["log"]))
    print(f"  {len(cache)} cached, {len(todo)} to do", flush=True)
    done = 0
    for k, ds, text in todo:
        if time.perf_counter() - t0 > budget:
            break
        ner = [h for h in detect.scan_ner(text) if h["type"] == "PERSON"]
        ner = ner_confidence.annotate(ner, text, "en_core_web_lg", "PERSON") if ner else []
        flat = [h for h in detect.scan_flattened(text) if h["type"] == "PERSON"]
        fg = [h for h in detect.detect_all_field_gated(text, log_type=P3.LOG_TYPE[ds], use_flattened=True)
              if h["type"] == "PERSON"]
        rec = {"ner": [[h["start"], h["end"], round(h["confidence"], 6)] for h in ner],
               "flat": [[h["start"], h["end"]] for h in flat], "fg": [[h["start"], h["end"]] for h in fg]}
        if P3.LOG_TYPE[ds] == "syslog":
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
_TOK = re.compile(r"[A-Za-z]+")


def keystatus(text, cand):
    k = ctx.kv_key(text, cand.start)
    if k is None:
        return "no-key"
    ident = k in ctx.TELEMETRY_IDENTITY_KEYS or k.split(".")[-1] in ctx.TELEMETRY_IDENTITY_KEYS
    return "identity-key" if ident else "other-key"


def text_feats(text, cand, base):
    before = _TOK.findall(text[max(0, cand.start - 30):cand.start])
    after = _TOK.findall(text[cand.end:cand.end + 30])
    f = dict(base)
    f["_prev"] = before[-1].lower() if before else "<s>"
    f["_next"] = after[0].lower() if after else "</s>"
    f["sent_initial"] = float(bool(re.search(r'(^|[.!?"]\s*|\s{2,})$', text[max(0, cand.start - 3):cand.start])))
    return f


def rows_for(seed, cache):
    out = {}
    for ds, versions in corpora(seed).items():
        out[ds] = {}
        for t, entries in versions.items():
            rows = []
            for i, e in enumerate(entries):
                rec = cache[P3.key(ds + "\x00" + e["log"])]
                hits = {"ner": [{"type": "PERSON", "start": s, "end": x, "confidence": c} for s, x, c in rec["ner"]],
                        "flat": [{"type": "PERSON", "start": s, "end": x} for s, x in rec["flat"]]}
                cands = pccf.build_candidates(hits, "PERSON")
                gold = [{"type": "PERSON", "start": p["start"], "end": p["end"], "loc": p["loc"],
                         "fmt": "spaced" if " " in p["injected_value"] else "flattened"} for p in e["pii"]]
                rows.append({"id": f"{ds}:{seed}:{t}:{i}", "ds": ds, "text": e["log"], "hits": hits,
                             "cands": cands, "gold": gold, "labels": [H.label(c, gold) for c in cands],
                             "feats": [text_feats(e["log"], c, ctx.kv_features(e["log"], c)) for c in cands],
                             "keys": [keystatus(e["log"], c) for c in cands],
                             "fg": rec["fg"], "fg_strip": rec.get("fg_strip")})
            out[ds][t] = rows
    return out


class TextLR:
    """L2 logistic regression over kv_features + learned prev/next-word
    indicators (vocabulary from the fit split, min count 5)."""

    def fit(self, feats, labels):
        from collections import Counter
        cnt = Counter([f["_prev"] for f in feats]) + Counter(["n:" + f["_next"] for f in feats])
        self.vocab = sorted(w for w, c in cnt.items() if c >= 5)
        self.lr = ctx.LogisticScorer(l2=1.0).fit(self._x(feats), labels)
        return self

    def _x(self, feats):
        out = []
        for f in feats:
            g = {k: v for k, v in f.items() if not k.startswith("_")}
            for w in self.vocab:
                g["w=" + w] = float(f["_prev"] == w if not w.startswith("n:") else "n:" + f["_next"] == w)
            out.append(g)
        return out

    def predict(self, feats):
        return self.lr.predict(self._x(feats)) if feats else np.array([])


class Hybrid:
    """KV rule for keyed candidates, TextLR for unkeyed ones."""

    def __init__(self, fit_rows):
        feats = [f for r in fit_rows for f, k in zip(r["feats"], r["keys"]) if k == "no-key"]
        labels = [y for r in fit_rows for y, k in zip(r["labels"], r["keys"]) if k == "no-key"]
        self.text = TextLR().fit(feats, labels)
        self.rule = ctx.KVRuleScorer()

    def predict_row(self, r):
        p = np.array(self.rule.predict(r["feats"]), dtype=float)
        idx = [i for i, k in enumerate(r["keys"]) if k == "no-key"]
        if idx:
            p[idx] = self.text.predict([r["feats"][i] for i in idx])
        return p


class HybridNoLex(Hybrid):
    """EXPLORATORY (post-hoc, added after H13/H14 failed): same as Hybrid,
    but the unkeyed text LR gets NO learned word indicators, only the
    generic kv_features and sent_initial. Tests the diagnosis that the
    lexical features overfit template phrasing and broke exchangeability."""

    def __init__(self, fit_rows):
        feats = [{k: v for k, v in f.items() if not k.startswith("_")}
                 for r in fit_rows for f, k in zip(r["feats"], r["keys"]) if k == "no-key"]
        labels = [y for r in fit_rows for y, k in zip(r["labels"], r["keys"]) if k == "no-key"]
        self.lr = ctx.LogisticScorer(l2=1.0).fit(feats, labels)
        self.rule = ctx.KVRuleScorer()

    def predict_row(self, r):
        p = np.array(self.rule.predict(r["feats"]), dtype=float)
        idx = [i for i, k in enumerate(r["keys"]) if k == "no-key"]
        if idx:
            p[idx] = self.lr.predict([{k: v for k, v in r["feats"][i].items() if not k.startswith("_")} for i in idx])
        return p


class AllLR:
    def __init__(self, fit_rows):
        self.m = TextLR().fit([f for r in fit_rows for f in r["feats"]], [y for r in fit_rows for y in r["labels"]])

    def predict_row(self, r):
        return self.m.predict(r["feats"])


class RuleOnly:
    def predict_row(self, r):
        return ctx.KVRuleScorer().predict(r["feats"])


def score_rows(rows, scorer):
    out = []
    for r in rows:
        cands = pccf.build_candidates(r["hits"], "PERSON")
        for c, k in zip(cands, r["keys"]):
            c.keystatus = k
        if cands:
            ctx.apply_scores(cands, scorer.predict_row(r))
        out.append({**r, "cands": cands})
    return out


def mask_key(c):
    return ("+".join(sorted(c.mask)), c.keystatus)


def recall_split(per):
    out = {}
    for preds, gold, _, _ in per:
        dd = H.em._dedup(preds)
        for g in gold:
            d = out.setdefault(f"{g['loc']}/{g['fmt']}", [0, 0])
            d[0] += any(p["start"] < g["end"] and g["start"] < p["end"] for p in dd)
            d[1] += 1
    return {k: (v[0] / v[1], v[1]) for k, v in sorted(out.items())}


def loc_recall(per, loc):
    hit = tot = 0
    for preds, gold, _, _ in per:
        dd = H.em._dedup(preds)
        for g in gold:
            if g["loc"] == loc:
                tot += 1
                hit += any(p["start"] < g["end"] and g["start"] < p["end"] for p in dd)
    return hit / tot if tot else float("nan")


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
    by_seed = {s: rows_for(s, cache) for s in P3.SEEDS}

    CONFIGS = {
        "S1 KV-rule, mask groups (T3 config)": (lambda fit: RuleOnly(), "mask"),
        "S2 KV-rule, mask x key groups": (lambda fit: RuleOnly(), mask_key),
        "S3 Hybrid, mask x key groups": (lambda fit: Hybrid(fit), mask_key),
        "S4 KV-LR+text, mask x key groups": (lambda fit: AllLR(fit), mask_key),
        "E3 [exploratory] Hybrid without lexical features, mask x key": (lambda fit: HybridNoLex(fit), mask_key),
    }
    names = ["union", "production field-gated (raw)", "production field-gated (header-stripped sim.)"] + list(CONFIGS) \
        + ["E2 [exploratory] T3 model as deployed (no unkeyed names in calibration)"]
    pooled = {k: [] for k in names}
    grp_agg, grp_all = {}, {}
    # E2 [exploratory]: the model exactly as T3 deployed it, calibrated on
    # identity-field-only data (no unkeyed names), applied to the message test.
    t3cache = json.load(open(P3.CACHE))
    t3rows = {s_: P3.rows_for(s_, t3cache) for s_ in P3.SEEDS}
    pooled["E2 [exploratory] T3 model as deployed (no unkeyed names in calibration)"] = []
    for seed in P3.SEEDS:
        for held in P3.DATASETS:
            tst = by_seed[seed][held]["B"]
            rest = [r for ds in P3.DATASETS if ds != held for r in by_seed[seed][ds]["A"]]
            rng = random.Random(seed * 100 + P3.DATASETS.index(held))
            ids = [r["id"] for r in rest]
            rng.shuffle(ids)
            fit_ids = set(ids[: len(ids) // 2])
            fit = [r for r in rest if r["id"] in fit_ids]
            cal = [r for r in rest if r["id"] not in fit_ids]
            rest3 = [r for ds in P3.DATASETS if ds != held for r in t3rows[seed][ds]]
            ids3 = [r["id"] for r in rest3]
            random.Random(seed * 100 + P3.DATASETS.index(held)).shuffle(ids3)
            cal3 = P3.score_rows([r for r in rest3 if r["id"] not in set(ids3[: len(ids3) // 2])], ctx.KVRuleScorer())
            m3 = pccf.PCCF(mode="coverage", alpha=0.05).fit([c for r in cal3 for c in r["cands"]],
                                                            [y for r in cal3 for y in r["labels"]])
            pooled["E2 [exploratory] T3 model as deployed (no unkeyed names in calibration)"] += P3.apply_rule(
                score_rows(tst, RuleOnly()), m3.accept)
            pooled["union"] += P3.apply_rule(tst, lambda c: True)
            pooled["production field-gated (raw)"] += P3.fixed(tst, "fg")
            pooled["production field-gated (header-stripped sim.)"] += P3.fixed(
                tst, "fg_strip" if P3.LOG_TYPE[held] == "syslog" else "fg")
            for name, (mk, part) in CONFIGS.items():
                sc = mk(fit)
                cs, ts = score_rows(cal, sc), score_rows(tst, sc)
                model = pccf.PCCF(mode="coverage", alpha=0.05, partition=part).fit(
                    [c for r in cs for c in r["cands"]], [y for r in cs for y in r["labels"]])
                pd = P3.apply_rule(ts, model.accept)
                pooled[name] += pd
                if name[:2] in ("S2", "S3", "E3"):
                    tag = name[:2]
                    for _, _, r, acc in pd:
                        for c, y in zip(r["cands"], r["labels"]):
                            d = grp_all.setdefault(tag, {}).setdefault(mask_key(c), [0, 0])
                            d[0] += y and id(c) in acc
                            d[1] += y
                if name.startswith("S3"):
                    for _, _, r, acc in pd:
                        for c, y in zip(r["cands"], r["labels"]):
                            g = mask_key(c)
                            d = grp_agg.setdefault(g, [0, 0, 0, 0])
                            d[0] += y and id(c) in acc
                            d[1] += y
                            d[2] += id(c) in acc
                            d[3] += 1

    mu = P3.metrics(pooled["union"])
    u_msg = loc_recall(pooled["union"], "message")
    say("=== Pooled over held-out datasets x seeds (held-out uses template set B) ===")
    table = {}
    for k in names:
        m = P3.metrics(pooled[k])
        m["R_message"] = loc_recall(pooled[k], "message")
        m["R_field"] = loc_recall(pooled[k], "field")
        table[k] = m
        say(f"  {k:48s} P={m['P']:.3f} ({m['P']-mu['P']:+.3f})  R={m['R']:.3f} ({m['R']-mu['R']:+.3f})  "
            f"R_field={m['R_field']:.3f}  R_message={m['R_message']:.3f}")
    say()
    say("=== Recall by gold location / name format ===")
    split = {}
    for k in names:
        rs = recall_split(pooled[k])
        split[k] = rs
        say(f"  {k:48s} " + "  ".join(f"{g}={v[0]:.3f}(n={v[1]})" for g, v in rs.items()))
    say()
    say("=== S3 per-group candidate recall (pooled) ===")
    h13 = True
    for g, (kt, nt, kept, n) in sorted(grp_agg.items()):
        chk = "n/a"
        if nt >= 19:
            tol = 2 * (0.05 * 0.95 / nt) ** 0.5
            ok = kt / nt >= 0.95 - tol
            h13 &= ok
            chk = "pass" if ok else "FAIL"
        say(f"  {g[0]:9s} {g[1]:13s} n={n:5d} n_true={nt:5d} cand_recall={(kt/nt if nt else float('nan')):.3f} kept={kept:5d} check={chk}")
    say()
    say("=== Per-group candidate recall, no-key groups only (S2 / S3 / E3) ===")
    for tag, gd in sorted(grp_all.items()):
        cells = [f"{g[0]}: {kt}/{nt}={kt/nt:.3f}" for g, (kt, nt) in sorted(gd.items()) if g[1] == "no-key" and nt]
        say(f"  {tag}: " + " | ".join(cells))
    results["no_key_groups"] = {t: {f"{a}|{b_}": v_ for (a, b_), v_ in gd.items()} for t, gd in grp_all.items()}
    say()
    b = H.bootstrap(pooled["union"], pooled["S3 Hybrid, mask x key groups"], n_boot=1000)
    say(f"  Bootstrap (1,000 over lines) S3 vs union: dP=[{b['dP_95ci'][0]:+.3f},{b['dP_95ci'][1]:+.3f}] "
        f"dR=[{b['dR_95ci'][0]:+.3f},{b['dR_95ci'][1]:+.3f}]")
    say()
    s1, s3 = table["S1 KV-rule, mask groups (T3 config)"], table["S3 Hybrid, mask x key groups"]
    v = {"H12": s1["R_message"] < 0.80 * u_msg, "H13": h13,
         "H14": s3["R_message"] >= u_msg - 0.03 and s3["P"] >= mu["P"] + 0.05 and b["dP_95ci"][0] > 0}
    say("=== Verdicts (mechanical) ===")
    for k, ok in v.items():
        say(f"  {k}: {'SUPPORTED' if ok else 'NOT SUPPORTED'}")
    results.update({"pooled": table, "recall_split": split, "groups": {f"{a}|{b_}": v_ for (a, b_), v_ in grp_agg.items()},
                    "bootstrap": b, "verdicts": v})
    open(OUT_TXT, "w").write("\n".join(lines) + "\n")
    json.dump(results, open(OUT_JSON, "w"), indent=1, default=str)
    print(f"wrote {OUT_TXT}")


if __name__ == "__main__":
    main()
