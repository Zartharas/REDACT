"""
Phase 4, F5: fetch LARGER French/Russian PII samples (fixes the 11/26-doc
pilots). This needs internet access to Hugging Face, which BOTH Claude
workspaces are blocked from by egress policy, so the author runs it on
their Mac through the redact-pccf Docker image
(fetch_large_multilang.sh). It writes NEW files only:
  datasets/large/OpenPII_FR_large.jsonl  (ai4privacy/open-pii-masking-500k-ai4privacy, CC BY 4.0)
  datasets/large/RU_PII_large.jsonl      (redmadrobot-rnd/pii_benchmark, MIT)
Same schema as the existing pilot files:
  {"split","id","text","ents":[{"t","s","o":[start,end]}]}
Every span is verified with text[start:end] == value and dropped if it
doesn't match. The counts are printed and written to a manifest.
"""
import argparse
import json
import os
import sys

N_FR = {"train": 2000, "validation": 1500}


def fr(out):
    from datasets import load_dataset
    rows, bad = [], 0
    for split, cap in N_FR.items():
        ds = load_dataset("ai4privacy/open-pii-masking-500k-ai4privacy", split=split, streaming=True)
        n = 0
        for r in ds:
            if r.get("language") != "fr":
                continue
            text = r["source_text"]
            pm = r["privacy_mask"]
            pm = json.loads(pm) if isinstance(pm, str) else pm
            ents = []
            for e in pm:
                s, t_end, lab, val = int(e["start"]), int(e["end"]), e["label"], e.get("value")
                if lab == "LANGUAGEPLACEHOLDER":
                    continue
                if val is not None and text[s:t_end] != val:
                    bad += 1
                    continue
                ents.append({"t": lab, "s": text[s:t_end], "o": [s, t_end]})
            rows.append({"split": split, "id": f"openpii-fr-{r.get('uid', n)}", "text": text, "ents": ents})
            n += 1
            if n >= cap:
                break
        print(f"  FR {split}: {n} rows", flush=True)
    with open(os.path.join(out, "OpenPII_FR_large.jsonl"), "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return {"rows": len(rows), "span_mismatches_dropped": bad}


def ru(out):
    from datasets import load_dataset
    dd = load_dataset("redmadrobot-rnd/pii_benchmark")
    rows, bad, joined = [], 0, 0
    # Debug sample: the first run found NO token in its row's "text" for all
    # 2,841 rows, so the column layout differs from what the pilot assumed.
    # Record the real schema and 3 rows so the fix can be verified.
    first = next(iter(dd.values()))
    with open(os.path.join(out, "RU_SCHEMA_SAMPLE.json"), "w", encoding="utf-8") as f:
        json.dump({"columns": first.column_names, "features": str(first.features),
                   "rows": [first[i] for i in range(min(3, len(first)))]}, f, ensure_ascii=False, indent=1, default=str)
    for split, ds in dd.items():
        feat = ds.features.get("ner_tags")
        names = getattr(getattr(feat, "feature", None), "names", None)
        for i, r in enumerate(ds):
            # In this dataset "tokens" and "ner_tags" are JSON-encoded STRINGS
            # (features: Value('string')), not lists. The first fetch iterated
            # over their characters, which produced 2,841 unusable rows.
            toks = json.loads(r["tokens"]) if isinstance(r["tokens"], str) else r["tokens"]
            raw_tags = json.loads(r["ner_tags"]) if isinstance(r["ner_tags"], str) else r["ner_tags"]
            tags = [names[t] if names and isinstance(t, int) else t for t in raw_tags]
            if len(tags) != len(toks):
                bad += 1
                continue
            toks = [str(t) for t in toks]

            def locate(txt):
                pos, offs = 0, []
                for tok in toks:  # sequential left-to-right search, as in prepare_ru_dataset.py
                    j = txt.find(tok, pos)
                    if j == -1:
                        return None
                    offs.append((j, j + len(tok)))
                    pos = j + len(tok)
                return offs

            text = r.get("text") if isinstance(r.get("text"), str) else None
            offs = locate(text) if text else None
            if offs is None:
                # Fallback: rebuild the text by joining tokens with single
                # spaces. This is disclosed and counted; the spans are then
                # exact by construction.
                text = " ".join(toks)
                offs = locate(text)
                joined += 1
            if offs is None:
                bad += 1
                continue
            ents, cur = [], None
            for (s, e), tag in zip(offs, tags):
                if tag in ("O", None):
                    cur = None
                    continue
                typ = tag.split("-", 1)[-1]
                if tag.startswith("B-") or cur is None or cur["t"] != typ:
                    cur = {"t": typ, "o": [s, e]}
                    ents.append(cur)
                else:
                    cur["o"][1] = e
            for en in ents:
                en["s"] = text[en["o"][0]:en["o"][1]]
            rows.append({"split": split, "id": f"ru-pii-{split}-{i}", "text": text, "ents": ents})
    with open(os.path.join(out, "RU_PII_large.jsonl"), "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"  RU: {len(rows)} rows ({bad} dropped: token not found; {joined} texts rebuilt by joining tokens)")
    return {"rows": len(rows), "dropped_token_not_found": bad, "text_rebuilt_from_tokens": joined}


# ---------------------------------------------------------------------------
# Phase 5 sources (PCCF_PHASE5_PREREGISTRATION.md). All are char-offset
# annotated. Their exact column names were NOT verified from a Claude
# workspace (Hugging Face is blocked there), so extraction is schema-
# tolerant: it probes common key names, writes SCHEMA_SAMPLE_<lang>.json
# with the real columns and 3 raw rows, and counts every row or span it
# had to drop. Check the counts in MANIFEST.json before trusting results.
# ---------------------------------------------------------------------------
TEXT_KEYS = ("source_text", "text", "content", "document", "sentence", "input", "utterance", "utt",
             "message", "form", "turn_text")
SPAN_KEYS = ("privacy_mask", "entities", "spans", "labels", "annotations", "pii_spans", "ents",
             "PII_set", "pii_set")  # PII_set: KDPII (found from SCHEMA_SAMPLE_ko_kdpii.json after the first fetch)
START_KEYS = ("start", "begin", "char_start", "start_offset", "s")
END_KEYS = ("end", "char_end", "end_offset", "e")
LABEL_KEYS = ("label", "type", "entity_type", "entity", "tag", "category", "t")
VALUE_KEYS = ("value", "text", "entity_text", "span", "surface", "form")

SOURCES = {
    # lang: (dataset id, row filter, per-split caps, output file)
    "ko": ("BCCard/privacy-filter-openpii-masking", "hangul", {"*": 3500}, "KO_BCCard_large.jsonl"),
    "zh": ("wan9yu/pii-bench-zh", None, {"*": 4000}, "ZH_piibench_large.jsonl"),
    "in": ("maskflow-ai/indiapii-bench", None, {"*": 5000}, "IN_indiapii_large.jsonl"),
    # amendment 4: Arabic and Turkish are NOT in either ai4privacy set (see MANIFEST languages_seen)
    "ar": ("mabahboh/sitr-arabic-pii", None, {"*": 4000}, "AR_sitr_large.jsonl"),         # Apache-2.0
    "tr": ("newmindai/nm-kvkk-pii-6K", None, {"*": 4000}, "TR_kvkk_large.jsonl"),         # Apache-2.0, synthetic
}
SOURCE_CONFIGS = {"tr": ["spans"]}  # nm-kvkk ships spans/bio/gliner configs; use the char-offset one


def _first(d, keys):
    for k in keys:
        if k in d and d[k] is not None:
            return k
    return None


def _spans(row, text):
    k = _first(row, SPAN_KEYS)
    if k is None:
        return None
    items = row[k]
    if isinstance(items, str):
        try:
            items = json.loads(items)
        except ValueError:
            return None
    if isinstance(items, dict):  # columnar {"start":[...],"end":[...],"label":[...]}
        sk, ek, lk = _first(items, START_KEYS), _first(items, END_KEYS), _first(items, LABEL_KEYS)
        if not (sk and ek and lk):
            return None
        items = [{"start": a, "end": b, "label": c} for a, b, c in zip(items[sk], items[ek], items[lk])]
    out, bad = [], 0
    for it in items or []:
        if not isinstance(it, dict):
            continue
        sk, ek, lk = _first(it, START_KEYS), _first(it, END_KEYS), _first(it, LABEL_KEYS)
        if not (sk and ek and lk):
            bad += 1
            continue
        s_, e_ = int(it[sk]), int(it[ek])
        vk = _first({k2: v for k2, v in it.items() if k2 != lk}, VALUE_KEYS)
        if vk is not None and isinstance(it[vk], str) and text[s_:e_] != it[vk]:
            bad += 1
            continue
        out.append({"t": str(it[lk]), "s": text[s_:e_], "o": [s_, e_]})
    return out, bad


def fetch_generic(lang, out):
    from datasets import get_dataset_config_names, load_dataset
    ds_id, filt, caps, fname = SOURCES[lang]
    try:
        configs = SOURCE_CONFIGS.get(lang) or get_dataset_config_names(ds_id)
    except Exception:  # noqa: BLE001
        configs = [None]
    rows, dropped_rows, dropped_spans, scanned, sample = [], 0, 0, 0, None
    total_cap = sum(caps.values())
    for cfg in configs:
        dd = load_dataset(ds_id, cfg, streaming=True) if cfg else load_dataset(ds_id, streaming=True)
        for split, ds in dd.items():
            cap = caps.get(split, caps.get("*", 0))
            if cap == 0:
                continue
            n = 0
            for r in ds:
                scanned += 1
                if sample is None:
                    sample = {"dataset": ds_id, "config": cfg, "split": split, "columns": list(r.keys()), "rows": []}
                if len(sample["rows"]) < 3:
                    sample["rows"].append({k: (str(v)[:400]) for k, v in r.items()})
                if scanned > 2_000_000:
                    break
                if callable(filt) and not filt(r):
                    continue
                tk = _first(r, TEXT_KEYS)
                if tk is None or not isinstance(r[tk], str):
                    dropped_rows += 1
                    continue
                text = r[tk]
                if filt == "hangul" and not any("\uac00" <= ch <= "\ud7a3" for ch in text):
                    continue
                sp = _spans(r, text)
                if sp is None:
                    dropped_rows += 1
                    continue
                ents, bad = sp
                dropped_spans += bad
                rows.append({"split": split, "id": f"{lang}-{cfg or 'default'}-{split}-{n}", "text": text, "ents": ents,
                             "lang": r.get("language") or r.get("lang")})
                n += 1
                if n >= cap or len(rows) >= total_cap:
                    break
            print(f"  {lang.upper()} {cfg or ''} {split}: {n} rows (scanned so far {scanned})", flush=True)
            if len(rows) >= total_cap:
                break
    json.dump(sample, open(os.path.join(out, f"SCHEMA_SAMPLE_{lang}.json"), "w"), ensure_ascii=False, indent=1)
    with open(os.path.join(out, fname), "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return {"dataset": ds_id, "rows": len(rows), "rows_dropped_no_text_or_spans": dropped_rows,
            "spans_dropped_mismatch_or_malformed": dropped_spans, "rows_scanned": scanned,
            "label_counts": dict(__import__("collections").Counter(e["t"] for r in rows for e in r["ents"]).most_common(40))}


# ---------------------------------------------------------------------------
# ai4privacy multi-language passes (phase 5, amendment 3). ONE streaming pass
# per dataset collects every requested language at once, so the 1.5M-row set
# is not scanned once per language. Output: <LANG>_OpenPII_large.jsonl. The
# manifest's "languages_seen" records every language code actually present,
# so a language the dataset lacks shows up as 0 rows rather than a silent gap.
# ---------------------------------------------------------------------------
AI4P_500K = "ai4privacy/open-pii-masking-500k-ai4privacy"
AI4P_15M = "ai4privacy/pii-masking-openpii-1.5m"
AI4P_GROUPS = {AI4P_500K: ("de", "it", "nl", "hi", "te"), AI4P_15M: ("id", "ja", "pt", "fi")}
AI4P_CAPS = {"train": 2000, "validation": 1500}


def _lang_match(code, value):
    v = str(value or "").lower().replace("_", "-")
    return v == code or v.startswith(code + "-")


def fetch_ai4p(dataset, langs, out):
    from collections import Counter
    from datasets import load_dataset
    rows = {l: [] for l in langs}
    counts = {l: Counter() for l in langs}
    seen, bad, scanned = Counter(), Counter(), 0
    for split, cap in AI4P_CAPS.items():
        try:
            ds = load_dataset(dataset, split=split, streaming=True)
        except Exception as ex:  # noqa: BLE001
            print(f"  {dataset} {split}: cannot open ({ex!r})", flush=True)
            continue
        for r in ds:
            scanned += 1
            lv = r.get("language")
            seen[str(lv)] += 1
            for l in langs:
                if counts[l][split] < cap and _lang_match(l, lv):
                    text = r.get("source_text") or r.get("text")
                    sp = _spans(r, text) if isinstance(text, str) else None
                    if sp is None:
                        bad[l] += 1
                        break
                    ents = [e for e in sp[0] if e["t"] != "LANGUAGEPLACEHOLDER"]
                    bad[l] += sp[1]
                    rows[l].append({"split": split, "id": f"openpii-{l}-{r.get('uid', scanned)}", "text": text,
                                    "ents": ents})
                    counts[l][split] += 1
                    break
            if all(counts[l][split] >= cap for l in langs):
                break
            if scanned % 100000 == 0:
                print(f"  {dataset} {split}: scanned {scanned}, have " +
                      ", ".join(f"{l}={counts[l][split]}" for l in langs), flush=True)
    res = {}
    for l in langs:
        fn = "ID_OpenPII_large.jsonl" if l == "id" else f"{l.upper()}_OpenPII_large.jsonl"
        with open(os.path.join(out, fn), "w", encoding="utf-8") as f:
            for r in rows[l]:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        res[l] = {"dataset": dataset, "rows": len(rows[l]), "by_split": dict(counts[l]),
                  "rows_or_spans_dropped": bad[l]}
        print(f"  {l.upper()}: {len(rows[l])} rows {dict(counts[l])}", flush=True)
    res["_scan"] = {"dataset": dataset, "rows_scanned": scanned, "languages_seen": dict(seen.most_common(80))}
    return res


# ---------------------------------------------------------------------------
# KDPII: Korean Dialogic Dataset for PII De-identification (Yonsei University
# + TSCIENTIFIC; IEEE Access 2024; Zenodo 10.5281/zenodo.10968609; CC BY 4.0,
# open access). Downloaded straight from Zenodo (train/valid/test.json).
# The annotation format was NOT visible from Claude's workspace, so parsing
# is schema-tolerant: nested dialogues are flattened to utterances, then
# either char-offset spans or token BIO are used. SCHEMA_SAMPLE_ko_kdpii.json
# records what was really there.
# ---------------------------------------------------------------------------
KDPII_FILES = {"train": "train.json", "validation": "valid.json", "test": "test.json"}
TOKEN_KEYS = ("tokens", "words", "token")
TAG_KEYS = ("ner_tags", "tags", "labels", "bio", "label")


def _load_any(raw):
    raw = raw.strip()
    if raw.startswith("["):
        return json.loads(raw)
    try:
        d = json.loads(raw)
        for k in ("data", "instances", "dialogues", "documents", "conversations", "utterances"):
            if isinstance(d, dict) and isinstance(d.get(k), list):
                return d[k]
        return [d]
    except ValueError:
        return [json.loads(l) for l in raw.splitlines() if l.strip()]


def _flatten(obj, depth=0):
    """Yield dicts that carry a text field; descend into nested lists
    (dialogue -> turns -> utterances)."""
    if isinstance(obj, dict):
        if _first(obj, TEXT_KEYS) and isinstance(obj[_first(obj, TEXT_KEYS)], str):
            yield obj
            return
        if depth < 4:
            for v in obj.values():
                if isinstance(v, (list, dict)):
                    yield from _flatten(v, depth + 1)
    elif isinstance(obj, list) and depth < 4:
        for v in obj:
            yield from _flatten(v, depth + 1)


def _bio(row, text):
    tk = _first(row, TOKEN_KEYS)
    gk = _first({k: v for k, v in row.items() if k != tk}, TAG_KEYS)
    if not tk or not gk:
        return None
    toks, tags = row[tk], row[gk]
    toks = json.loads(toks) if isinstance(toks, str) else toks
    tags = json.loads(tags) if isinstance(tags, str) else tags
    if not isinstance(toks, list) or not isinstance(tags, list) or len(toks) != len(tags):
        return None
    pos, offs = 0, []
    for t in map(str, toks):
        j = text.find(t, pos)
        if j == -1:
            return None
        offs.append((j, j + len(t)))
        pos = j + len(t)
    ents, cur = [], None
    for (a, b), tag in zip(offs, map(str, tags)):
        if tag in ("O", "0"):
            cur = None
            continue
        typ = tag.split("-", 1)[-1]
        if tag.startswith("B-") or cur is None or cur["t"] != typ:
            cur = {"t": typ, "o": [a, b]}
            ents.append(cur)
        else:
            cur["o"][1] = b
    for en in ents:
        en["s"] = text[en["o"][0]:en["o"][1]]
    return ents


def fetch_kdpii(out):
    import urllib.request
    rows, stats, sample = [], __import__("collections").Counter(), {"record": "10.5281/zenodo.10968609", "files": {}}
    for split, fname in KDPII_FILES.items():
        url = f"https://zenodo.org/records/10968609/files/{fname}?download=1"
        raw = urllib.request.urlopen(url, timeout=300).read().decode("utf-8", errors="replace")
        data = _load_any(raw)
        sample["files"][fname] = {"top_level_type": type(data).__name__, "n_top": len(data),
                                  "first_items": [str(x)[:600] for x in data[:2]]}
        n = 0
        for u in _flatten(data):
            text = u[_first(u, TEXT_KEYS)]
            ents = None
            if _first(u, SPAN_KEYS):
                sp = _spans(u, text)
                if sp is not None:
                    ents, bad = sp
                    stats["spans_dropped"] += bad
            if ents is None:
                ents = _bio(u, text)
                if ents is not None:
                    stats["units_bio"] += 1
            if ents is None:
                stats["units_dropped_unrecognized"] += 1
                continue
            rows.append({"split": split, "id": f"kdpii-{split}-{n}", "text": text, "ents": ents})
            n += 1
        print(f"  KDPII {split}: {n} text units", flush=True)
    json.dump(sample, open(os.path.join(out, "SCHEMA_SAMPLE_ko_kdpii.json"), "w"), ensure_ascii=False, indent=1)
    with open(os.path.join(out, "KO_KDPII_large.jsonl"), "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return {"dataset": "KDPII (Zenodo 10968609, CC BY 4.0)", "rows": len(rows), **stats,
            "label_counts": dict(__import__("collections").Counter(e["t"] for r in rows for e in r["ents"]).most_common(40))}


def ar_wiki(out):
    """amendment 5: Arabic PERSON gold. mabahboh/sitr-arabic-pii has no
    person-name label, so it cannot score PERSON. WikiANN-ar (Wikipedia-derived
    silver NER, CC BY-SA text) supplies PER spans. Texts are rebuilt by joining
    tokens with single spaces, so spans are exact by construction. Not PII-style
    text: short Wikipedia fragments; disclosed as a proxy in the results."""
    from datasets import load_dataset
    want = {"train": 2000, "validation": 1000, "test": 1000}
    rows = []
    for split, k in want.items():
        ds = load_dataset("unimelb-nlp/wikiann", "ar", split=split)
        names = ds.features["ner_tags"].feature.names
        for i, r in enumerate(ds.select(range(min(k, len(ds))))):
            toks = [str(t) for t in r["tokens"]]
            tags = [names[t] for t in r["ner_tags"]]
            text, offs, pos = " ".join(toks), [], 0
            for t in toks:
                offs.append((pos, pos + len(t)))
                pos += len(t) + 1
            ents, cur = [], None
            for (a, b), tag in zip(offs, tags):
                if tag == "O":
                    cur = None
                    continue
                typ = tag.split("-", 1)[-1]
                if tag.startswith("B-") or cur is None or cur["t"] != typ:
                    cur = {"t": typ, "o": [a, b]}
                    ents.append(cur)
                else:
                    cur["o"][1] = b
            for en in ents:
                en["s"] = text[en["o"][0]:en["o"][1]]
            rows.append({"split": split, "id": f"ar-wikiann-{split}-{i}", "text": text, "ents": ents})
    with open(os.path.join(out, "AR_wikiann_large.jsonl"), "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    per = sum(1 for r in rows for e in r["ents"] if e["t"] == "PER")
    print(f"  AR_WIKI: {len(rows)} rows, PER spans {per}")
    return {"rows": len(rows), "per_spans": per, "source": "unimelb-nlp/wikiann:ar"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--only", default=None,
                    help="comma-separated subset of fr,ru,de,it,nl,hi,te,id,ja,pt,ar,tr,fi,ko,zh,in,ko_kdpii (default: all)")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    mp = os.path.join(a.out, "MANIFEST.json")
    man = json.load(open(mp)) if os.path.exists(mp) else {}
    only = set(a.only.split(",")) if a.only else None
    jobs = [("fr", fr), ("ru", ru)] + [(lg, (lambda out, lg=lg: fetch_generic(lg, out))) for lg in SOURCES] \
        + [("ko_kdpii", fetch_kdpii), ("ar_wiki", ar_wiki)]
    for ds_id, group in AI4P_GROUPS.items():
        want = [l for l in group if not only or l in only]
        if want:
            try:
                r = fetch_ai4p(ds_id, want, a.out)
                scan = r.pop("_scan")
                man.update(r)
                man.setdefault("_scans", {})[ds_id] = scan
            except Exception as e:  # noqa: BLE001
                for l in want:
                    man[l] = {"error": repr(e)}
                print(f"  {ds_id} FAILED: {e!r}", file=sys.stderr)
    for name, fn in jobs:
        if only and name not in only:
            continue
        try:
            man[name] = fn(a.out)
        except Exception as e:  # noqa: BLE001
            man[name] = {"error": repr(e)}
            print(f"  {name} FAILED: {e!r}", file=sys.stderr)
    json.dump(man, open(os.path.join(a.out, "MANIFEST.json"), "w"), indent=1)
    print(json.dumps(man, indent=1))


if __name__ == "__main__":
    main()
