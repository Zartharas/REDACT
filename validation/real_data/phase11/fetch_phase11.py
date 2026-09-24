"""PCCF phase 11 data fetcher (PCCF_PHASE11_PREREGISTRATION.md). Runs in the
redact-pccf-multilang image (needs network) or locally for the parsers.
Writes <out>/{EN_TAB,EN_BTC,EN_WNUT,EN_ENRON,DE_GERMEVAL,RU_FACTRUEVAL,AR_ANERCORP}_large.jsonl
and <out>/PHASE11_MANIFEST.json. Rows: {"split","id","text","ents":[{"t","o":[s,e],"s", ...}]}.
  python fetch_phase11.py --out <datasets/large> [--only tab,btc,...] [--anercorp-dir <manual dir>]
"""
import argparse, collections, glob, io, json, os, random, re, sys, urllib.request, zipfile

CAP = {"train": 2000, "other": 1500}
TAB_URL = "https://raw.githubusercontent.com/NorskRegnesentral/text-anonymization-benchmark/master/echr_{}.json"
FRE_URL = "https://codeload.github.com/dialogue-evaluation/factRuEval-2016/zip/refs/heads/master"
ENRON_URLS = ["http://hevra.haifa.ac.il/~is-web/images/lecturers_files/einat_files/EnronMeetings-Minorthird.zip",
              "http://hevra.haifa.ac.il/~is-web/images/lecturers_files/einat_files/EnronRandom-Minorthird.zip"]


def _get(url, timeout=120):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (research; PCCF phase 11)"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def write(out, name, rows, info):
    with open(os.path.join(out, name), "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    person = sum(1 for r in rows for e in r["ents"] if e["t"] == "PERSON")
    info = {**info, "rows": len(rows), "person_spans": person,
            "by_split": dict(collections.Counter(r["split"] for r in rows))}
    print(f"  {name}: {info}", flush=True)
    return info


def capped(rows):
    by = collections.defaultdict(list)
    for r in rows:
        by["train" if r["split"] == "train" else "other"].append(r)
    out = []
    for k, lst in by.items():
        out += lst[: CAP[k]]
    return out


def bio_rows(tokens_list, tags_list, split, prefix, person_types):
    """BIO token rows -> text joined by single spaces with exact spans."""
    rows = []
    for i, (toks, tags) in enumerate(zip(tokens_list, tags_list)):
        toks = [str(t) for t in toks]
        text, offs, pos = " ".join(toks), [], 0
        for t in toks:
            offs.append((pos, pos + len(t)))
            pos += len(t) + 1
        ents, cur = [], None
        for (a, b), tag in zip(offs, tags):
            tag = str(tag)
            if tag == "O" or "-" not in tag:
                cur = None
                continue
            bi, typ = tag.split("-", 1)
            if bi == "B" or cur is None or cur["raw"] != typ:
                cur = {"raw": typ, "o": [a, b]}
                ents.append(cur)
            else:
                cur["o"][1] = b
        rows.append({"split": split, "id": f"{prefix}-{split}-{i}", "text": text,
                     "ents": [{"t": "PERSON" if e["raw"] in person_types else e["raw"], "o": e["o"],
                               "s": text[e["o"][0]:e["o"][1]]} for e in ents]})
    return rows


# ---------------- TAB ----------------
def parse_tab(docs, split):
    rows = []
    for d in docs:
        text = d["text"]
        ments = [m for a in d["annotations"].values() for m in a["entity_mentions"] if m["entity_type"] == "PERSON"]
        # union over annotators: merge overlapping PERSON mentions into gold spans
        spans = sorted((m["start_offset"], m["end_offset"], m.get("identifier_type", "NO_MASK")) for m in ments)
        merged = []
        for s, e, it in spans:
            if merged and s < merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], e)
                merged[-1][2].add(it)
            else:
                merged.append([s, e, {it}])
        ents = [{"t": "PERSON", "o": [s, e], "s": text[s:e],
                 "idtype": "DIRECT" if "DIRECT" in its else ("QUASI" if "QUASI" in its else "NO_MASK")}
                for s, e, its in merged]
        rows.append({"split": split, "id": f"tab-{d['doc_id']}", "text": text, "ents": ents,
                     "n_annotators": len(d["annotations"])})
    return rows


def fetch_tab(out, local=None):
    rows = []
    for split in ("train", "dev", "test"):
        raw = open(os.path.join(local, f"echr_{split}.json"), "rb").read() if local else _get(TAB_URL.format(split), 300)
        rows += parse_tab(json.loads(raw), split)
    return write(out, "EN_TAB_large.jsonl", rows, {"source": "TAB (NorskRegnesentral), MIT repo"})


# ---------------- HF token datasets ----------------
def _hf(ids, **kw):
    from datasets import load_dataset
    last = None
    for i in ids:
        for rev in (None, "refs/convert/parquet"):
            try:
                return load_dataset(i, revision=rev, **kw) if rev else load_dataset(i, **kw), i, rev
            except Exception as ex:  # noqa: BLE001
                last = ex
    raise RuntimeError(f"could not load any of {ids}: {last!r}")


def fetch_hf_bio(out, ids, fname, prefix, person_types, tag_col="ner_tags"):
    dd, used, rev = _hf(ids)
    rows = []
    for split, ds in dd.items():
        feat = ds.features[tag_col]
        names = getattr(getattr(feat, "feature", None), "names", None)
        sp = "train" if split == "train" else split
        tags = [[names[t] if names and isinstance(t, int) else t for t in r[tag_col]] for r in ds]
        rows += bio_rows([r["tokens"] for r in ds], tags, sp, prefix, person_types)
    return write(out, fname, capped(rows), {"source": used, "revision": rev})


# ---------------- FactRuEval ----------------
def parse_factrueval(files, split_name):
    """files: dict basename -> text content for one split directory."""
    rows = []
    for base in sorted({k.rsplit(".", 1)[0] for k in files}):
        if base + ".txt" not in files or base + ".spans" not in files or base + ".objects" not in files:
            continue
        text = files[base + ".txt"]
        spans = {}
        for line in files[base + ".spans"].splitlines():
            p = line.split("#")[0].split()
            if len(p) >= 4:
                spans[p[0]] = (p[1], int(p[2]), int(p[3]))
        ents, bad = [], 0
        for line in files[base + ".objects"].splitlines():
            p = line.split("#")[0].split()
            if len(p) >= 3 and p[1] == "Person":
                for sid in p[2:]:
                    if sid in spans:
                        typ, s, ln = spans[sid]
                        ents.append({"t": "PERSON", "o": [s, s + ln], "s": text[s:s + ln], "part": typ})
        rows.append({"split": split_name, "id": f"fre-{split_name}-{base}", "text": text, "ents": ents})
    return rows


def fetch_factrueval(out, local=None):
    files = {"train": {}, "test": {}}
    if local:
        for sp, d in (("train", "devset"), ("test", "testset")):
            for f in glob.glob(os.path.join(local, d, "*")):
                files[sp][os.path.basename(f)] = open(f, encoding="utf-8").read()
    else:
        z = zipfile.ZipFile(io.BytesIO(_get(FRE_URL, 300)))
        for n in z.namelist():
            for sp, d in (("train", "/devset/"), ("test", "/testset/")):
                if d in n and not n.endswith("/"):
                    files[sp][os.path.basename(n)] = z.read(n).decode("utf-8")
    rows = parse_factrueval(files["train"], "train") + parse_factrueval(files["test"], "test")
    mism = sum(1 for r in rows for e in r["ents"] if not e["s"].strip())
    return write(out, "RU_FACTRUEVAL_large.jsonl", rows,
                 {"source": "FactRuEval-2016 (MIT); devset->train, testset->test", "empty_span_text": mism})


# ---------------- Enron (Minorthird) ----------------
_MT = re.compile(r"addToType\s+(\S+)\s+(\d+)\s+(\d+)\s+(\S+)")


def parse_minorthird(zbytes, tag):
    z = zipfile.ZipFile(io.BytesIO(zbytes))
    names = [n for n in z.namelist() if not n.endswith("/")]
    labels = collections.defaultdict(list)
    types = collections.Counter()
    for n in names:
        if n.endswith(".labels") or "label" in os.path.basename(n).lower():
            for line in z.read(n).decode("latin-1").splitlines():
                m = _MT.search(line)
                if m:
                    doc, s, ln, typ = m.group(1), int(m.group(2)), int(m.group(3)), m.group(4)
                    labels[os.path.basename(doc)].append((s, s + ln, typ))
                    types[typ] += 1
    docs = {os.path.basename(n): z.read(n).decode("latin-1") for n in names
            if not (n.endswith(".labels") or "label" in os.path.basename(n).lower())}
    rows, missing = [], 0
    for doc, labs in labels.items():
        if doc not in docs:
            missing += 1
            continue
        text = docs[doc]
        ents = [{"t": "PERSON" if typ.lower() in ("name", "person", "personname", "extracted_name", "true_name")
                 else typ, "o": [s, e], "s": text[s:e]} for s, e, typ in labs]
        rows.append({"split": "all", "id": f"enron-{tag}-{doc}", "text": text, "ents": ents})
    for doc, text in docs.items():  # unlabelled docs of the corpus = docs with no names
        if doc not in labels and not doc.lower().endswith((".txt~",)) and len(text) > 20 and labels:
            rows.append({"split": "all", "id": f"enron-{tag}-{doc}", "text": text, "ents": []})
    return rows, {"label_types": dict(types), "docs_in_zip": len(docs), "labelled_docs_missing_text": missing,
                  "zip_members_sample": names[:8]}


def fetch_enron(out):
    rows, info = [], {"source": "Minkov et al. Enron-Meetings/Random (no licence stated)"}
    for url, tag in zip(ENRON_URLS, ("meetings", "random")):
        r, i = parse_minorthird(_get(url, 300), tag)
        rows += r
        info[tag] = i
    return write(out, "EN_ENRON_large.jsonl", rows, info)


# ---------------- ANERcorp (manual download) ----------------
def fetch_anercorp(out, d):
    if not d or not os.path.isdir(d):
        print("  ANERCORP: no manual directory; skipped (see PCCF_PHASE11_PREREGISTRATION.md)")
        return {"status": "no data (manual download needed)"}
    rows = []
    for f in sorted(glob.glob(os.path.join(d, "**", "*"), recursive=True)):
        if not os.path.isfile(f) or not re.search(r"(train|test)", os.path.basename(f), re.I):
            continue
        split = "train" if re.search("train", os.path.basename(f), re.I) else "test"
        toks, tags, sents = [], [], []
        for line in open(f, encoding="utf-8", errors="replace"):
            p = line.strip().split()
            if len(p) < 2:
                if toks:
                    sents.append((toks, tags)); toks, tags = [], []
                continue
            toks.append(p[0]); tags.append(p[-1])
            if p[0] in (".", "؟", "!") and len(toks) > 3:
                sents.append((toks, tags)); toks, tags = [], []
        if toks:
            sents.append((toks, tags))
        rows += bio_rows([s[0] for s in sents], [s[1] for s in sents], split, "anercorp", {"PERS", "PER"})
    return write(out, "AR_ANERCORP_large.jsonl", capped(rows), {"source": "ANERcorp CAMeL splits (CC BY-SA 4.0), manual"})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--only", default="tab,btc,wnut,germeval,factrueval,enron,anercorp")
    ap.add_argument("--anercorp-dir", default=None)
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    mp = os.path.join(a.out, "PHASE11_MANIFEST.json")
    man = json.load(open(mp)) if os.path.exists(mp) else {}
    jobs = {"tab": lambda: fetch_tab(a.out),
            "btc": lambda: fetch_hf_bio(a.out, ["GateNLP/broad_twitter_corpus"], "EN_BTC_large.jsonl", "btc", {"PER"}),
            "wnut": lambda: fetch_hf_bio(a.out, ["leondz/wnut_17", "wnut_17"], "EN_WNUT_large.jsonl", "wnut", {"person"}),
            "germeval": lambda: fetch_hf_bio(a.out, ["GermanEval/germeval_14", "germeval_14"], "DE_GERMEVAL_large.jsonl",
                                             "germeval", {"PER"}),
            "factrueval": lambda: fetch_factrueval(a.out),
            "enron": lambda: fetch_enron(a.out),
            "anercorp": lambda: fetch_anercorp(a.out, a.anercorp_dir)}
    for k in a.only.split(","):
        try:
            man[k] = jobs[k]()
        except Exception as ex:  # noqa: BLE001
            man[k] = {"error": repr(ex)[:500]}
            print(f"  {k} FAILED: {ex!r}", file=sys.stderr)
    json.dump(man, open(mp, "w"), indent=1, ensure_ascii=False)


if __name__ == "__main__":
    main()
