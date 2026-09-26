"""
Convert K-LegalDeID (Korean court-judgment de-identification, EACL 2026,
aclanthology.org/2026.eacl-long.103) into this project's jsonl schema, for
the phase-5 sweep (language key "ko_legal").

LICENSE / USE CONDITIONS -- READ FIRST
  * The underlying data is CC BY-NC-SA 4.0 (per this project's earlier
    license research and the paper's own licence discussion). Use is
    RESEARCH-ONLY here: no training or tuning of anything that ships in a
    commercial product.
  * NO redistribution. The converted file goes to
    validation/real_data/datasets/restricted/, which .gitignore
    excludes, so it cannot end up in the repo or its Zenodo archive.
    Share-alike applies to any derived dataset you publish. It does not
    apply to REDACT's code, which does not adapt the data.
  * Attribute the K-LegalDeID paper in any write-up that uses these
    numbers.

NO PUBLIC DOWNLOAD WAS FOUND. As of 2026-09-24 neither the paper nor its
ACL Anthology page links to a code or data repository. Obtain the files
from the authors, then run:
  python validation/real_data/phase5/convert_k_legaldeid.py PATH [PATH ...]
PATH may be files or directories (.json / .jsonl). Splits are taken from
a "split" field if present, otherwise from the filename
(train / valid|dev|validation / test).

FORMAT: the paper describes sentence-level instances carrying span info
(label, start, end) and BIO labels over KLUE-BERT tokens. Both are
handled:
  (a) char-offset spans: probed with the same tolerant key lists as the
      phase-5 fetcher (text/sentence; entities/spans/labels/...; start/end;
      label/type/...), and verified with text[start:end] == value when a
      value is present
  (b) token BIO: tokens + tags. Spans are rebuilt with a sequential
      left-to-right search in the text, or by joining tokens when no text
      field exists (counted and disclosed)
Every dropped row and span is counted in RESTRICTED_MANIFEST.json.
"""
import json
import os
import re
import sys
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "phase4"))
import fetch_large_multilang as F  # noqa: E402  (shared, schema-tolerant span extraction)

OUT_DIR = os.path.join(os.path.dirname(HERE), "datasets", "restricted")
OUT = os.path.join(OUT_DIR, "KO_LEGAL_K-LegalDeID.jsonl")
TOKEN_KEYS = ("tokens", "words", "token")
TAG_KEYS = ("ner_tags", "tags", "labels", "bio", "label")


def split_of(path, row):
    s = str(row.get("split", "")).lower()
    name = (s or os.path.basename(path)).lower()
    if re.search(r"test", name):
        return "test"
    if re.search(r"valid|dev", name):
        return "validation"
    return "train"


def rows_in(path):
    txt = open(path, encoding="utf-8").read().strip()
    if not txt:
        return []
    if txt[0] == "[":
        return json.loads(txt)
    if txt[0] == "{" and "\n" not in txt:
        d = json.loads(txt)
        for k in ("data", "instances", "sentences", "documents"):
            if isinstance(d.get(k), list):
                return d[k]
        return [d]
    return [json.loads(l) for l in txt.splitlines() if l.strip()]


def from_bio(row):
    tk = F._first(row, TOKEN_KEYS)
    gk = F._first({k: v for k, v in row.items() if k != tk}, TAG_KEYS)
    if not tk or not gk:
        return None
    toks = json.loads(row[tk]) if isinstance(row[tk], str) else row[tk]
    tags = json.loads(row[gk]) if isinstance(row[gk], str) else row[gk]
    if not isinstance(toks, list) or not isinstance(tags, list) or len(toks) != len(tags):
        return None
    cont = [str(t).startswith("##") for t in toks]  # KLUE-BERT word-piece continuations
    toks = [str(t)[2:] if c else str(t) for t, c in zip(toks, cont)]
    tkey = F._first(row, F.TEXT_KEYS)
    text = row[tkey] if tkey and isinstance(row[tkey], str) else None
    joined = False
    offs = None
    if text:
        pos, offs = 0, []
        for t in toks:
            j = text.find(t, pos)
            if j == -1:
                offs = None
                break
            offs.append((j, j + len(t)))
            pos = j + len(t)
    if offs is None:
        # join word pieces WITHOUT a space, whole tokens WITH one
        text, offs, joined = "", [], True
        for t, c in zip(toks, cont):
            if text and not c:
                text += " "
            offs.append((len(text), len(text) + len(t)))
            text += t
    ents, cur = [], None
    for (s, e), tag in zip(offs, tags):
        tag = str(tag)
        if tag in ("O", "0", "None"):
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
    return text, ents, joined


def main(paths):
    files = []
    for p in paths:
        if os.path.isdir(p):
            files += [os.path.join(dp, f) for dp, _, fs in os.walk(p) for f in fs if f.endswith((".json", ".jsonl"))]
        else:
            files.append(p)
    os.makedirs(OUT_DIR, exist_ok=True)
    out, stats = [], Counter()
    for path in sorted(files):
        for i, r in enumerate(rows_in(path)):
            if not isinstance(r, dict):
                stats["rows_not_objects"] += 1
                continue
            tk = F._first(r, F.TEXT_KEYS)
            got = None
            if tk and isinstance(r[tk], str) and F._first(r, F.SPAN_KEYS):
                sp = F._spans(r, r[tk])
                if sp is not None:
                    got = (r[tk], sp[0], False)
                    stats["spans_dropped"] += sp[1]
            if got is None:
                got = from_bio(r)
                if got is not None:
                    stats["rows_bio"] += 1
            if got is None:
                stats["rows_dropped_unrecognized"] += 1
                continue
            text, ents, joined = got
            stats["texts_rebuilt_from_tokens"] += joined
            out.append({"split": split_of(path, r), "id": f"klegal-{os.path.basename(path)}-{i}", "text": text,
                        "ents": ents})
    with open(OUT, "w", encoding="utf-8") as f:
        for r in out:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    man = {"rows": len(out), "splits": dict(Counter(r["split"] for r in out)), **stats,
           "labels": dict(Counter(e["t"] for r in out for e in r["ents"]).most_common(30)),
           "license": "CC BY-NC-SA 4.0 -- research-only, no redistribution (gitignored)"}
    json.dump(man, open(os.path.join(OUT_DIR, "RESTRICTED_MANIFEST.json"), "w"), ensure_ascii=False, indent=1)
    print(json.dumps(man, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    main(sys.argv[1:])
