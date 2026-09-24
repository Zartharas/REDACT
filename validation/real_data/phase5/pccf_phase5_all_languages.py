"""
PCCF phase 5: all-languages sweep (PCCF_PHASE5_PREREGISTRATION.md).
One harness for fr, ru, id, ko, zh and in. Every language runs inside its
own try/except, so a bug in one never stops the others, and a
troubleshooting report is written next to the results.

  python .../pccf_phase5_all_languages.py --build-cache [--langs fr,ko] [--budget 150]   # resumable
  python .../pccf_phase5_all_languages.py [--langs ...]
Data: validation/real_data/datasets/large/ (fetch_large_multilang.sh).
Results: validation/real_data/phase5/.
"""
import argparse
import collections
import json
import os
import random
import sys
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
RD = os.path.dirname(HERE)
ROOT = os.path.abspath(os.path.join(RD, "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, RD)

import pccf  # noqa: E402
import pccf_context as ctx  # noqa: E402
import pccf_feasibility_meddocan as H  # noqa: E402
import pccf_phase2_meddocan as P2  # noqa: E402

DATA = os.path.join(RD, "datasets", "large")
# amendment 5: PCCF_PHASE5_CACHE lets the Docker run keep caches on the host
# (the container copy of the repo is thrown away, so every run recomputed de..fi).
CACHE_DIR = os.environ.get("PCCF_PHASE5_CACHE") or os.path.join(ROOT, "output", "phase5")
PERSON_LABELS = {"person", "person_name", "givenname", "surname", "first_name", "last_name",
                 "middle_name", "name", "per", "ps", "이름", "성명", "인명",
                 "full_name", "given_name", "family_name"}
GENERIC_CUES = {"name_labels": set(), "place_labels": set(), "honorifics": set(), "place_preps": set(),
                "eponym_heads": ()}
LANGS = {
    "fr": {"file": "OpenPII_FR_large.jsonl", "model": ("fr_core_news_md", "PER"), "cues": "FR_CUES"},
    "ru": {"file": "RU_PII_large.jsonl", "model": ("ru_core_news_md", "PER"), "cues": "RU_CUES"},
    "id": {"file": "ID_OpenPII_large.jsonl", "model": ("xx_ent_wiki_sm", "PER"), "cues": None},
    "ko": {"file": "KO_BCCard_large.jsonl", "model": ("ko_core_news_md", "PS"), "cues": None},
    "zh": {"file": "ZH_piibench_large.jsonl", "model": ("zh_core_web_md", "PERSON"), "cues": None},
    "in": {"file": "IN_indiapii_large.jsonl", "model": ("en_core_web_lg", "PERSON"), "cues": None},
    # --- amendment 3: second wave (baseline layers in src/lang_layers.py) ---
    "de": {"file": "DE_OpenPII_large.jsonl", "model": ("de_core_news_md", "PER"), "cues": None},
    "it": {"file": "IT_OpenPII_large.jsonl", "model": ("it_core_news_md", "PER"), "cues": None},
    "nl": {"file": "NL_OpenPII_large.jsonl", "model": ("nl_core_news_md", "PERSON"), "cues": None},
    "pt": {"file": "PT_OpenPII_large.jsonl", "model": ("pt_core_news_md", "PER"), "cues": None},
    "fi": {"file": "FI_OpenPII_large.jsonl", "model": ("fi_core_news_md", "PERSON"), "cues": None},
    "ja": {"file": "JA_OpenPII_large.jsonl", "model": ("ja_core_news_md", "PERSON"), "cues": None},
    # Hugging Face NER (no usable spaCy NER); "model" here only parses for UD features (xx has no parser)
    "ar": {"file": "AR_sitr_large.jsonl", "model": ("xx_ent_wiki_sm", "PER"), "cues": None, "hf_ner": True},
    # amendment 5: sitr-arabic-pii has NO person label (0 PERSON gold); WikiANN-ar proxy supplies PER
    "ar_wiki": {"file": "AR_wikiann_large.jsonl", "model": ("xx_ent_wiki_sm", "PER"), "cues": None,
                "hf_ner": True, "layers_as": "ar"},
    "tr": {"file": "TR_kvkk_large.jsonl", "model": ("xx_ent_wiki_sm", "PER"), "cues": None, "hf_ner": True},
    "hi": {"file": "HI_OpenPII_large.jsonl", "model": ("xx_ent_wiki_sm", "PER"), "cues": None, "hf_ner": True},
    "te": {"file": "TE_OpenPII_large.jsonl", "model": ("xx_ent_wiki_sm", "PER"), "cues": None, "hf_ner": True},
    # amendment 4: second Indonesian row, IndoBERT NER (the project's own id_ner.py model) instead of xx
    "id_hf": {"file": "ID_OpenPII_large.jsonl", "model": ("xx_ent_wiki_sm", "PER"), "cues": None, "hf_ner": True},
    # amendment 6: MIT-licensed Turkish NER (akdeniz27) on the same data; KLUE-NER Korean (CC BY-SA 4.0)
    "tr_mit": {"file": "TR_kvkk_large.jsonl", "model": ("xx_ent_wiki_sm", "PER"), "cues": None, "hf_ner": True},
    "ko_klue": {"file": "KO_KLUE_large.jsonl", "model": ("ko_core_news_md", "PS"), "cues": None, "layers_as": "ko"},
    # KDPII (Zenodo 10.5281/zenodo.10968609, CC BY 4.0): open Korean dialogue PII corpus.
    "ko_kdpii": {"file": "KO_KDPII_large.jsonl", "model": ("ko_core_news_md", "PS"), "cues": None, "layers_as": "ko"},
    # K-LegalDeID: CC BY-NC-SA, research-only, obtained from the authors, gitignored
    # (see convert_k_legaldeid.py). Uses the same baseline Korean layers as "ko".
    "ko_legal": {"file": os.path.join("..", "restricted", "KO_LEGAL_K-LegalDeID.jsonl"),
                 "model": ("ko_core_news_md", "PS"), "cues": None, "layers_as": "ko"},
}
# amendment 6, wave 3: 18 more ai4privacy openpii-1.5m languages (baseline layers in src/lang_layers.py)
import lang_layers as _LL  # noqa: E402
for _l in ("bg", "pl", "cs", "lt", "et", "sv", "sk", "lv", "hu", "ro", "el", "da", "sl", "hr", "sr", "vi", "ms", "tl"):
    LANGS[_l] = {"file": f"{_l.upper()}_OpenPII_large.jsonl", "model": _LL.NER_MODEL[_l], "cues": None}
# amendment 7: the ten xx-NER rows again, with GLiNER multilingual as the NER layer
for _l in _LL.GLINER_LANGS:
    LANGS[f"{_l}_gl"] = {"file": f"{_l.upper()}_OpenPII_large.jsonl", "model": _LL.NER_MODEL[_l], "cues": None,
                         "hf_ner": True}
WAVE4_KEYS = {f"{_l}_gl" for _l in _LL.GLINER_LANGS}
WAVE3_KEYS = {"bg", "pl", "cs", "lt", "et", "sv", "sk", "lv", "hu", "ro", "el", "da", "sl", "hr", "sr", "vi", "ms",
              "tl", "tr_mit", "ko_klue"}
HYP = open(os.path.join(ROOT, "PCCF_PHASE5_PREREGISTRATION.md")).read().split("**Hypotheses")[1].split("**Troubleshooting")[0]


def layers(lang, text):
    if lang == "fr":
        import fr_detect
        import fr_ner
        return fr_detect.scan_french_names(text), fr_ner.scan_fr_ner(text)
    if lang == "ru":
        import ru_detect
        import ru_ner
        return ru_detect.scan_russian_names(text), ru_ner.scan_ru_ner(text)
    import lang_layers
    lang = LANGS.get(lang, {}).get("layers_as", lang)
    return lang_layers.scan_dict(lang, text), lang_layers.scan_ner(lang, text)


def load(lang):
    p = os.path.join(DATA, LANGS[lang]["file"])
    if not os.path.exists(p):
        return None
    return [json.loads(l) for l in open(p, encoding="utf-8") if l.strip()]


def is_person_label(t):
    t = str(t).lower()
    return t in PERSON_LABELS or t.startswith(("ps_", "per_", "person"))


def gold(doc):
    return [{"type": "PERSON", "start": e["o"][0], "end": e["o"][1]} for e in doc["ents"] if is_person_label(e["t"])]


def build_cache(langs, budget):
    import spacy
    import ner_confidence
    import pccf_ud
    os.makedirs(CACHE_DIR, exist_ok=True)
    t0 = time.perf_counter()
    for lang in langs:
        docs = load(lang)
        if not docs:
            print(f"  {lang}: no data file", flush=True)
            continue
        cp = os.path.join(CACHE_DIR, f"{lang}.json")
        cache = json.load(open(cp)) if os.path.exists(cp) else {}
        model, label = LANGS[lang]["model"]
        nlp, done, errors = None, 0, 0
        t_lang = time.perf_counter()
        for d in docs:
            if d["id"] in cache:
                continue
            if time.perf_counter() - t0 > budget:
                break
            try:
                nlp = nlp or spacy.load(model)
                t = d["text"]
                dh, nh = layers(lang, t)
                dh = [h for h in dh if h["type"] == "PERSON"]
                nh = [h for h in nh if h["type"] == "PERSON"]
                if not LANGS[lang].get("hf_ner"):  # HF models already return per-span confidence
                    nh = ner_confidence.annotate(nh, t, model, label)
                e = {"dict": [[h["start"], h["end"]] for h in dh],
                     "ner": [[h["start"], h["end"], round(h["confidence"], 6)] for h in nh]}
                cands = pccf.build_candidates(H.layer_hits(e, "beam"), "PERSON")
                doc = nlp(t)
                e["ud"] = [pccf_ud.ud_features(doc, t, c) for c in cands]
            except Exception as ex:  # noqa: BLE001
                errors += 1
                e = {"error": repr(ex)[:300]}
            cache[d["id"]] = e
            done += 1
            if done % 250 == 0:  # checkpoint, so an interrupted run resumes
                json.dump(cache, open(cp, "w"))
                rate = done / max(time.perf_counter() - t_lang, 1e-9)
                print(f"    {lang}: {len(cache)}/{len(docs)} ({errors} errors, {rate:.1f} docs/s, "
                      f"~{(len(docs) - len(cache)) / max(rate, 1e-9) / 60:.0f} min left)", flush=True)
            if done == 25 and errors == 25:  # fail fast: first 25 docs all errored
                print(f"  {lang}: ABORTED after 25/25 errors: {e['error']}", flush=True)
                break
        json.dump(cache, open(cp, "w"))
        left = len(docs) - len(cache)
        print(f"  {lang}: +{done} docs ({errors} errors), {len(cache)}/{len(docs)}"
              + ("  complete" if left == 0 else "  -> run again"), flush=True)


def splits(rows):
    by = collections.Counter(r["split"] for r in rows)
    rng = random.Random(0)
    if by.get("train", 0) >= 400 and len(by) > 1:
        tr = [r for r in rows if r["split"] == "train"]
        rng.shuffle(tr)
        return tr[: len(tr) // 2], tr[len(tr) // 2:], [r for r in rows if r["split"] != "train"]
    rows = rows[:]
    rng.shuffle(rows)
    q = len(rows) // 4
    return rows[:q], rows[q:2 * q], rows[2 * q:]


def evaluate(lang, say):
    docs = load(lang)
    rep = {"lang": lang}
    if not docs:
        rep["status"] = "no data"
        say(f"=== {lang}: no data file ===")
        return rep, None
    cache = json.load(open(os.path.join(CACHE_DIR, f"{lang}.json")))
    cues = getattr(ctx, LANGS[lang]["cues"]) if LANGS[lang]["cues"] else GENERIC_CUES
    rows, errs = [], 0
    for d in docs:
        e = cache.get(d["id"])
        if e is None or "error" in e:
            errs += 1
            continue
        hits = H.layer_hits(e, "beam")
        cands = pccf.build_candidates(hits, "PERSON")
        g = gold(d)
        rows.append({"id": d["id"], "split": d["split"], "text": d["text"], "hits": hits, "cands": cands, "gold": g,
                     "labels": [H.label(c, g) for c in cands],
                     "feats": [ctx.features(d["text"], c, cues) for c in cands], "ud": e["ud"]})
    labels = collections.Counter(str(e["t"]) for d in docs for e in d["ents"])
    rep.update({"docs": len(docs), "docs_uncached_or_error": errs, "gold_person": sum(len(r["gold"]) for r in rows),
                "label_inventory": labels.most_common(25),
                "person_labels_matched": sorted({l for l in labels if is_person_label(l)}),
                "dict_hits": sum(len(r["hits"]["dict"]) for r in rows), "ner_hits": sum(len(r["hits"]["ner"]) for r in rows),
                "ner_backend": ("hf" if LANGS[lang].get("hf_ner") else "spacy/presidio"),
                "cache_errors_sample": [cache[d["id"]]["error"] for d in docs
                                        if "error" in cache.get(d["id"], {})][:5]})
    fit, cal, tst = splits(rows)
    say(f"=== {lang.upper()}: {len(rows)} docs (fit {len(fit)} / cal {len(cal)} / test {len(tst)}); "
        f"gold person {rep['gold_person']}; person labels {rep['person_labels_matched']}; "
        f"dict hits {rep['dict_hits']}, ner hits {rep['ner_hits']}; uncached/errors {errs} ===")
    pt = H.pattern_table(tst)
    rep["test_patterns"] = pt
    for k, d in pt.items():
        say(f"  test pattern {k:9s} n={d['n']:5d} true={d['true']:5d} precision={d['precision']:.3f}")
    mu = P2.metrics(P2.apply_rule(tst, lambda c: True))
    say(f"  {'union':34s} P={mu['P']:.3f} R={mu['R']:.3f}")
    res = {"union": mu}
    variants = [("rule", ctx.RuleScorer(), "feats", "keep")]
    if LANGS[lang]["cues"]:
        variants.append(("LR hand cues", "fit", "feats", "keep"))
    variants += [("LR-UD", "fit", "ud", "keep"), ("LR-UD [fallback=global, H24]", "fit", "ud", "global")]
    for name, sc, fk, fb in variants:
        re_ = lambda rs: [{**r, "feats": r[fk]} for r in rs]  # noqa: E731
        f2, c2, t2 = re_(fit), re_(cal), re_(tst)
        if sc == "fit":
            ys = [y for r in f2 for y in r["labels"]]
            if len(set(ys)) < 2:
                say(f"  {name:34s} skipped: fit split has {len(ys)} candidates / {len(set(ys))} class(es)")
                res[name] = {"skipped": True}
                continue
            sc = ctx.LogisticScorer(l2=1.0).fit([f for r in f2 for f in r["feats"]], ys)
        cs, ts = P2.score_rows(c2, sc), P2.score_rows(t2, sc)
        model = pccf.PCCF(mode="coverage", alpha=0.05, min_group_true=19, fallback=fb).fit(
            [c for r in cs for c in r["cands"]], [y for r in cs for y in r["labels"]])
        pd = P2.apply_rule(ts, model.accept)
        m = P2.metrics(pd)
        ch = P2.pattern_checks(pd, model)
        fbk = {"+".join(sorted(k)): st["fallback"] for k, st in model.stats.items()}
        elig = {k: d for k, d in ch.items() if d["n_true"] >= 19}
        floors = all(d["cand_recall"] >= 0.95 - 2 * (0.05 * 0.95 / d["n_true"]) ** 0.5 for d in elig.values())
        # amendment 6 / phase-6 forward rule: floor also accounts for calibration-set size
        cal_true = collections.Counter("+".join(sorted(c.mask)) for r in c2 for c, y in zip(r["cands"], r["labels"]) if y)
        floors_cv = all(d["cand_recall"] >= 0.95 - 2 * (0.05 * 0.95 * (1 / max(cal_true.get(k, 0), 1) + 1 / d["n_true"])) ** 0.5
                        for k, d in elig.items())
        res[name] = {**m, "checks": ch, "fallback": fbk, "floors_hold": floors, "floors_hold_cv": floors_cv,
                     "cal_true": dict(cal_true), "eligible_groups": len(elig)}
        say(f"  {name:34s} P={m['P']:.3f} ({m['P']-mu['P']:+.3f}) R={m['R']:.3f} ({m['R']-mu['R']:+.3f}) "
            f"floors={'ok' if floors else 'FAIL'} floors_cv={'ok' if floors_cv else 'FAIL'} (eligible groups {len(elig)}) fallback={fbk}  "
            + " | ".join(f"{k}: rec={d['cand_recall']:.2f}/n={d['n_true']}" for k, d in sorted(ch.items())
                         if d["cand_recall"] is not None))
    rep["status"] = "ok"
    return rep, res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--build-cache", action="store_true")
    ap.add_argument("--langs", default=",".join(LANGS))
    ap.add_argument("--budget", type=float, default=150)
    a = ap.parse_args()
    langs = [l for l in a.langs.split(",") if l in LANGS]
    if a.build_cache:
        build_cache(langs, a.budget)
        return
    lines, results, trouble = [], {}, {}
    say = lambda s="": (print(s, flush=True), lines.append(s))  # noqa: E731
    say("Hypotheses" + HYP)
    for lang in langs:
        try:
            rep, res = evaluate(lang, say)
        except Exception:  # noqa: BLE001
            rep, res = {"lang": lang, "status": "EXCEPTION", "traceback": traceback.format_exc()}, None
            say(f"=== {lang}: EXCEPTION (see troubleshooting report) ===")
        trouble[lang] = rep
        if res:
            results[lang] = res
        say()
    wave3 = {l: r for l, r in results.items() if l in WAVE3_KEYS and r.get("rule", {}).get("eligible_groups", 0) > 0}
    wave4 = {l: r for l, r in results.items() if l in WAVE4_KEYS and r.get("rule", {}).get("eligible_groups", 0) > 0}
    results_p5 = {l: r for l, r in results.items() if l not in WAVE3_KEYS and l not in WAVE4_KEYS}
    eligible = {l: r for l, r in results_p5.items() if r.get("rule", {}).get("eligible_groups", 0) > 0}
    h22 = bool(eligible) and all(r["rule"]["floors_hold"] for r in eligible.values())
    ud_ok = [l for l, r in eligible.items() if not r["LR-UD"].get("skipped") and r["LR-UD"]["floors_hold"]
             and r["LR-UD"]["P"] - r["union"]["P"] >= 0.03]
    h23 = bool(eligible) and len(ud_ok) >= len(eligible) / 2
    missing = [l for l in langs if trouble[l].get("status") != "ok"]
    say(f"eligible languages: {sorted(eligible)}; LR-UD useful+valid in: {ud_ok}")
    say("=== Verdicts (mechanical) ===" + (f"  PROVISIONAL: no results for {missing}" if missing else ""))
    say(f"  H22: {'SUPPORTED' if h22 else 'NOT SUPPORTED'}")
    say(f"  H23: {'SUPPORTED' if h23 else 'NOT SUPPORTED'}")
    h28 = h29 = None
    if wave3:  # amendment 6: judged separately, with the phase-6 combined-variance floor
        h28 = all(r["rule"]["floors_hold_cv"] for r in wave3.values())
        ok29 = [l for l, r in wave3.items() if not r["LR-UD"].get("skipped") and r["LR-UD"]["floors_hold_cv"]
                and r["LR-UD"]["P"] - r["union"]["P"] >= 0.03]
        h29 = len(ok29) >= len(wave3) / 2
        say(f"wave-3 eligible: {sorted(wave3)}; LR-UD useful+valid (combined floor) in: {ok29}")
        say(f"  H28: {'SUPPORTED' if h28 else 'NOT SUPPORTED'}")
        say(f"  H29: {'SUPPORTED' if h29 else 'NOT SUPPORTED'}")
    h33 = h34 = None
    if wave4:  # amendment 7
        f1 = lambda m: 2 * m["P"] * m["R"] / (m["P"] + m["R"]) if m["P"] + m["R"] else 0.0  # noqa: E731
        better = [l for l in wave4 if l[:-3] in results and f1(wave4[l]["union"]) > f1(results[l[:-3]]["union"])]
        h33 = len(better) >= 7
        ok34 = [l for l, r in wave4.items() if r["rule"]["floors_hold_cv"] and not r["LR-UD"].get("skipped")
                and r["LR-UD"]["floors_hold_cv"] and r["LR-UD"]["P"] - r["union"]["P"] >= 0.03]
        h34 = all(r["rule"]["floors_hold_cv"] for r in wave4.values()) and len(ok34) >= len(wave4) / 2
        say(f"wave-4 (GLiNER) rows: {sorted(wave4)}; union F1 better than xx in: {better}; LR-UD useful+valid in: {ok34}")
        say(f"  H33: {'SUPPORTED' if h33 else 'NOT SUPPORTED'}")
        say(f"  H34: {'SUPPORTED' if h34 else 'NOT SUPPORTED'}")
    tag = "_".join(langs) if a.langs != ",".join(LANGS) else "all"
    open(os.path.join(HERE, f"pccf_phase5_{tag}_results.txt"), "w").write("\n".join(lines) + "\n")
    json.dump({"results": results, "verdicts": {"H22": h22, "H23": h23, "H28": h28, "H29": h29, "H33": h33, "H34": h34}}, open(
        os.path.join(HERE, f"pccf_phase5_{tag}_results.json"), "w"), indent=1, default=str)
    json.dump(trouble, open(os.path.join(HERE, f"TROUBLESHOOTING_{tag}.json"), "w"), indent=1, default=str,
              ensure_ascii=False)
    print(f"\nwrote results + TROUBLESHOOTING_{tag}.json to {HERE}")


if __name__ == "__main__":
    main()
