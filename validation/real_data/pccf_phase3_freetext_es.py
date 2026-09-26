"""
PCCF phase 3, T2 (+ its T5 unit): Spanish FREE TEXT. Synthetic names are
injected into the real narrative of MEDDOCAN documents
(PCCF_PHASE3_PREREGISTRATION.md).

Why: phases 1-2 showed that 2,062 of MEDDOCAN's 2,085 gold names sit
directly under a name-type field label, so MEDDOCAN cannot test whether
presence-conditioned calibration helps on free text. This harness builds
that test from real carrier text plus synthetic, disclosed injections. It
follows the same methodology as inject_and_evaluate.py's Loghub
injection.

Injection (seeded, per document):
  - "Narrative lines" are lines of >= 120 chars whose field label is not a
    name- or place-type label.
  - 2 person sentences and 2 place sentences are inserted at sentence
    boundaries on narrative lines.
  - Person names: 50% Faker es_ES (inside the dictionary layer's own
    list) and 50% Faker es_MX/es_CO/es_AR names NOT in the es_ES list
    (out-of-dictionary), so the dictionary layer is not favoured by
    construction.
  - Places: a curated list of real Spanish/Latin American place names,
    filtered to those that COLLIDE with the es_ES name dictionary (the
    phase-1 failure mode).
  - Templates: set A for train and validation, set B (disjoint) for
    test.
Only candidates and gold on narrative lines are scored. The 4 real
unlabelled gold names stay in.

  python validation/real_data/pccf_phase3_freetext_es.py --build-cache   # ~110 s
  python validation/real_data/pccf_phase3_freetext_es.py
"""
import argparse
import json
import os
import random
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, HERE)

import evaluate_meddocan as em  # noqa: E402
import pccf  # noqa: E402
import pccf_context as ctx  # noqa: E402
import pccf_feasibility_meddocan as H  # noqa: E402
import pccf_phase2_meddocan as P2  # noqa: E402

CACHE = os.path.join(ROOT, "output", "pccf_phase3_freetext_cache.json")
OUT_TXT = os.path.join(HERE, "pccf_phase3_freetext_results.txt")
OUT_JSON = os.path.join(HERE, "pccf_phase3_freetext_results.json")
SEED = 7

HYP = """H6: LR-refit coverage a=0.05 on the test narrative: precision >= union + 0.05, bootstrap 95% CI of dP excludes 0.
H7: LR-refit coverage a=0.05: per-pattern test candidate recall >= 0.95 - tol for every pattern with n_true >= 19.
H6b (expectation): LR-full (fit on clean, header-dominated MEDDOCAN) gains less than LR-refit.
T5 unit: global Bonferroni alert tier (target 0.85) on LR-refit; precision >= 0.85 if >= 20 kept."""

PERSON_T = {
    "A": ["Fue valorada por la Dra. {full}.", "{full} acudió a revisión en consultas externas.",
          "Su hermano {first} presenta una clínica similar.", "Se comentó el caso con {full}, de guardia."],
    "B": ["La paciente fue atendida por el Dr. {full}.", "Según refiere {full}, los síntomas comenzaron hace meses.",
          "Acude acompañada de su hija {first}.", "{full} firmó el consentimiento informado."],
}
PLACE_T = {
    "A": ["Reside en {place} desde hace años.", "Fue trasladado al hospital de {place}."],
    "B": ["Natural de {place}, sin antecedentes de interés.", "Viajó recientemente a {place}."],
}
PLACES = ["Santiago", "Valencia", "Córdoba", "Segovia", "Rosario", "Soria", "León", "Ávila", "Teruel",
          "Lugo", "Toledo", "Murcia", "Zamora", "Guadalupe", "Mercedes", "Victoria", "Trinidad",
          "Concepción", "Dolores", "Florencia", "Sevilla", "Granada", "Salamanca", "Lorca", "Mérida",
          "Almería", "Oviedo", "Castellón", "Palencia", "Cuenca", "Huelva", "Medina", "Aranda", "Linares",
          "Andújar", "Villena", "Montoro", "Guadalajara", "Navarra", "Alicante", "Burgos", "Jaén"]


def name_pools():
    import es_detect
    from faker.providers.person.es_AR import Provider as AR
    from faker.providers.person.es_CO import Provider as CO
    from faker.providers.person.es_ES import Provider as ES
    from faker.providers.person.es_MX import Provider as MX
    dict_all = es_detect.FIRST_NAMES_ES | es_detect.LAST_NAMES_ES
    in_first = sorted(set(ES.first_names))
    in_last = sorted(set(ES.last_names))
    out_first = sorted({n for P in (MX, CO, AR) for n in getattr(P, "first_names", ())
                        if n.lower() not in dict_all and " " not in n})
    out_last = sorted({n for P in (MX, CO, AR) for n in getattr(P, "last_names", ())
                       if n.lower() not in dict_all and " " not in n})
    places = [p for p in PLACES if p.lower() in dict_all]
    return in_first, in_last, out_first, out_last, places


def narrative_line_spans(text):
    spans, pos = [], 0
    for line in text.split("\n"):
        s, e = pos, pos + len(line)
        pos = e + 1
        if len(line) < 120:
            continue
        cat, _ = ctx.field_label(text, s + len(line) - 1)
        if cat in ("name", "place"):
            continue
        spans.append((s, e))
    return spans


def in_spans(x, spans):
    return any(s <= x < e for s, e in spans)


def inject(doc, rng, tset, pools):
    in_first, in_last, out_first, out_last, places = pools
    text = doc["text"]
    gold = [dict(g) for g in em.gold_spans(doc) if g["type"] == "PERSON"]
    nspans = narrative_line_spans(text)
    bounds = [m.end() for m in re.finditer(r"\. (?=[A-ZÁÉÍÓÚÑ])", text) if in_spans(m.end(), nspans)]
    if len(bounds) < 4:
        return None
    positions = sorted(rng.sample(bounds, 4), reverse=True)
    kinds = ["person", "person", "place", "place"]
    rng.shuffle(kinds)
    meta = []
    for pos, kind in zip(positions, kinds):
        if kind == "person":
            oov = rng.random() < 0.5
            first = rng.choice(out_first if oov else in_first)
            last = rng.choice(out_last if oov else in_last)
            tpl = rng.choice(PERSON_T[tset])
            full = f"{first} {last}"
            sent = tpl.format(full=full, first=first) + " "
            val = full if "{full}" in tpl else first
            off = pos + sent.index(val)
            cat = ("OOV" if oov else "in-dict") + ("/honorific" if "Dr" in tpl else "/no-honorific")
            new = [{"type": "PERSON", "start": off, "end": off + len(val), "inj": cat}]
            meta.append({"kind": "person", "oov": oov, "honorific": "Dr" in tpl, "tpl": tpl})
        else:
            sent = rng.choice(PLACE_T[tset]).format(place=rng.choice(places)) + " "
            new = []
            meta.append({"kind": "place"})
        text = text[:pos] + sent + text[pos:]
        for g in gold:
            if g["start"] >= pos:
                g["start"] += len(sent)
                g["end"] += len(sent)
        gold += new
    return {"id": doc["id"], "split": doc["split"], "text": text, "gold": gold, "meta": meta}


def build_cache():
    import es_detect
    import es_ner
    import ner_confidence
    pools = name_pools()
    print(f"  pools: in-dict first/last={len(pools[0])}/{len(pools[1])}, out-of-dict "
          f"first/last={len(pools[2])}/{len(pools[3])}, colliding places={len(pools[4])}: {pools[4]}", flush=True)
    rng = random.Random(SEED)
    out, skipped = [], 0
    for d in em.load_docs():
        inj = inject(d, rng, "B" if d["split"] == "test" else "A", pools)
        if inj is None:
            skipped += 1
            continue
        t = inj["text"]
        inj["dict"] = [[h["start"], h["end"]] for h in es_detect.scan_spanish_names(t)]
        inj["ner"] = [[h["start"], h["end"], round(h["confidence"], 6)]
                      for h in ner_confidence.annotate(es_ner.scan_es_ner(t), t, H.MODEL, "PER")]
        out.append(inj)
    json.dump({"docs": out, "skipped": skipped, "places": pools[4]}, open(CACHE, "w"))
    print(f"wrote {CACHE}: {len(out)} docs injected, {skipped} skipped (< 4 narrative boundaries)")


def narrative_rows(entries):
    rows = []
    for e in entries:
        t = e["text"]
        ns = narrative_line_spans(t)
        keep = lambda s: in_spans(s, ns)  # noqa: E731
        hits = H.layer_hits({"dict": [x for x in e["dict"] if keep(x[0])],
                             "ner": [x for x in e["ner"] if keep(x[0])]}, "beam")
        cands = pccf.build_candidates(hits, "PERSON")
        gold = [g for g in e["gold"] if keep(g["start"])]
        rows.append({"id": e["id"], "split": e["split"], "text": t, "hits": hits, "cands": cands,
                     "gold": gold, "labels": [H.label(c, gold) for c in cands],
                     "feats": [ctx.features(t, c) for c in cands]})
    return rows


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
    c = json.load(open(CACHE))
    say(f"Injected docs: {len(c['docs'])} (skipped {c['skipped']}); colliding places used: {len(c['places'])}")
    rows = narrative_rows(c["docs"])
    fit = [r for r in rows if r["split"] == "train"]
    cal = [r for r in rows if r["split"] == "validation"]
    tst = [r for r in rows if r["split"] == "test"]
    say(f"Narrative-only gold PERSON: fit={sum(len(r['gold']) for r in fit)} cal={sum(len(r['gold']) for r in cal)} "
        f"test={sum(len(r['gold']) for r in tst)}")
    pt = H.pattern_table(tst)
    for k, d in pt.items():
        say(f"  test pattern {k:9s} n={d['n']:4d} true={d['true']:4d} precision={d['precision']:.3f}")
    say()

    # scorers
    mcache = json.load(open(H.CACHE))
    mdocs = {d["id"]: d for d in em.load_docs()}
    clean_train = [r for r in P2.fresh_rows(mcache["docs"], mdocs) if r["split"] == "train"]
    scorers = {
        "beam": None,
        "LR-full (clean MEDDOCAN)": P2.make_scorer("LR-full", clean_train),
        "LR-refit (injected train)": P2.make_scorer("LR-full", fit),
        "rule": ctx.RuleScorer(),
    }
    mu = P2.metrics(P2.apply_rule(tst, lambda c: True))
    mi = P2.metrics(P2.apply_rule(tst, lambda c: c.mask == frozenset({"dict", "ner"})))
    store = {"union": P2.apply_rule(tst, lambda c: True)}
    table = {"union": mu, "intersection": mi}

    def row(name, m):
        table[name] = m
        say(f"  {name:48s} P={m['P']:.3f} ({m['P']-mu['P']:+.3f})  R={m['R']:.3f} ({m['R']-mu['R']:+.3f})  F1={m['F1']:.3f}")
    say("=== Test narrative (template set B) ===")
    row("union", mu)
    row("intersection", mi)
    checks = {}
    for sname, sc in scorers.items():
        cs, ts = P2.score_rows(cal, sc), P2.score_rows(tst, sc)
        for a in (0.02, 0.05, 0.10):
            model = P2.fit_pccf(cs, dict(mode="coverage", alpha=a))
            pd = P2.apply_rule(ts, model.accept)
            name = f"{sname} coverage a={a:.2f}"
            row(name, P2.metrics(pd))
            store[name] = pd
            checks[name] = P2.pattern_checks(pd, model)
        au = {}
        for pat in ("dict", "ner", "dict+ner"):
            pos = [1 - c.scores[next(iter(c.scores))] for r in ts for c, y in zip(r["cands"], r["labels"])
                   if "+".join(sorted(c.mask)) == pat and y]
            neg = [1 - c.scores[next(iter(c.scores))] for r in ts for c, y in zip(r["cands"], r["labels"])
                   if "+".join(sorted(c.mask)) == pat and not y]
            au[pat] = H.auroc(pos, neg)
        say(f"    {sname}: within-pattern test AUROC " + ", ".join(
            f"{k}={'n/a' if v is None else round(v, 3)}" for k, v in au.items()))
    say()

    key = "LR-refit (injected train) coverage a=0.05"
    say(f"  Per-pattern checks for {key}:")
    h7 = True
    for k, d in sorted(checks[key].items()):
        ok = None
        if d["n_true"] >= 19:
            tol = 2 * (0.05 * 0.95 / d["n_true"]) ** 0.5
            ok = d["cand_recall"] >= 0.95 - tol
            h7 &= ok
        say(f"    {k:9s} n_true={d['n_true']:4d} cand_recall={d['cand_recall'] if d['cand_recall'] is None else round(d['cand_recall'], 3)} "
            f"kept={d['kept']} kept_P={round(d['kept_precision'] or 0, 3)} check={'n/a' if ok is None else ('pass' if ok else 'FAIL')}")

    # recall by injected-name type (descriptive)
    say("  Recall of injected names by type (test):")
    by = {}
    for name in ("union", "intersection-rule", key):
        pdocs = store[name] if name != "intersection-rule" else P2.apply_rule(tst, lambda c: c.mask == frozenset({"dict", "ner"}))
        cnt = {}
        for preds, gold, r, _ in pdocs:
            dd = em._dedup(preds)
            for g in gold:
                if "inj" not in g:
                    continue
                d = cnt.setdefault(g["inj"], [0, 0])
                d[0] += any(p["start"] < g["end"] and g["start"] < p["end"] for p in dd)
                d[1] += 1
        by[name] = cnt
        say(f"    {name:44s} " + "  ".join(f"{k}={v[0]}/{v[1]}" for k, v in sorted(cnt.items())))
    results["recall_by_type"] = by
    say()

    b = H.bootstrap(store["union"], store[key])
    say(f"  Bootstrap {key} vs union: dP=[{b['dP_95ci'][0]:+.3f},{b['dP_95ci'][1]:+.3f}] "
        f"dR=[{b['dR_95ci'][0]:+.3f},{b['dR_95ci'][1]:+.3f}]")
    say()

    # T5 unit
    sc = scorers["LR-refit (injected train)"]
    cs, ts = P2.score_rows(cal, sc), P2.score_rows(tst, sc)
    am = pccf.PCCF(mode="precision", target_precision=0.85, delta=0.05, partition="global",
                   search="bonferroni").fit([x for r in cs for x in r["cands"]], [y for r in cs for y in r["labels"]])
    apd = P2.apply_rule(ts, am.accept)
    amet = P2.metrics(apd)
    kept = sum(len(acc) for _, _, _, acc in apd)
    row("T5 alert tier (global, bonferroni, >=0.85)", amet)
    results["T5_unit"] = {"metrics": amet, "kept": kept, "threshold": am.thresholds.get(pccf.PCCF.GLOBAL)}
    say()

    m6 = table[key]
    lf = table["LR-full (clean MEDDOCAN) coverage a=0.05"]
    v = {"H6": m6["P"] >= mu["P"] + 0.05 and b["dP_95ci"][0] > 0, "H7": h7,
         "H6b (expectation)": (lf["P"] - mu["P"]) < (m6["P"] - mu["P"])}
    say("=== Verdicts (mechanical) ===")
    for k, ok in v.items():
        say(f"  {k}: {'SUPPORTED' if ok else 'NOT SUPPORTED'}")
    results.update({"table": table, "checks": checks, "bootstrap": b, "verdicts": v, "patterns": pt})
    open(OUT_TXT, "w").write("\n".join(lines) + "\n")
    json.dump(results, open(OUT_JSON, "w"), indent=1, default=str)
    print(f"wrote {OUT_TXT}")


if __name__ == "__main__":
    main()
