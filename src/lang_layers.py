"""
Baseline PERSON detection layers for the phase-5 all-languages sweep
(PCCF_PHASE5_PREREGISTRATION.md). Additive: no existing {lang}_detect.py
or {lang}_ner.py is modified, and none of these layers is wired into the
production path.

For each language this gives the same two-layer shape the fusion
research needs: a dictionary layer and a NER layer. These are BASELINE
layers, built quickly and uniformly so every language can be run in one
sweep. They are not tuned language modules. Stated per language:

  id  dictionary: id_detect.scan_indonesian_names (existing, Faker id_ID).
      NER: spaCy xx_ent_wiki_sm (multilingual, label PER). NOT the
      IndoBERT model id_ner.py uses, because that needs a Hugging Face
      download that both Claude workspaces are blocked from. Its beam
      confidence was observed to be degenerate (constant), so for
      Indonesian the NER confidence feature carries no information.
  ko  dictionary: Faker ko_KR surname (1-2 syllables) + given name
      (2 syllables) at the START of a Hangul run. Korean attaches
      particles and honorifics AFTER the name (김민수씨는), so only the
      start is anchored. NER: spaCy ko_core_news_md, label PS.
  zh  dictionary: Faker zh_CN surname (1-2 chars) + given name (1-2 chars)
      anywhere in a Han run, longest match first. There is no word
      boundary signal, so expect false positives. NER: spaCy
      zh_core_web_md, label PERSON.
  in  (English/Hinglish, IndiaPII-Bench; NOT Hindi-language text)
      dictionary: Faker en_IN first/last names over capitalised word runs.
      NER: spaCy en_core_web_lg, label PERSON.
"""
import re
from functools import lru_cache

NER_MODEL = {"id": ("xx_ent_wiki_sm", "PER"), "ko": ("ko_core_news_md", "PS"),
             "zh": ("zh_core_web_md", "PERSON"), "in": ("en_core_web_lg", "PERSON"),
             # phase 5, second wave (see PCCF_PHASE5_PREREGISTRATION.md amendment 3)
             "de": ("de_core_news_md", "PER"), "it": ("it_core_news_md", "PER"), "nl": ("nl_core_news_md", "PERSON"),
             "pt": ("pt_core_news_md", "PER"), "fi": ("fi_core_news_md", "PERSON"), "ja": ("ja_core_news_md", "PERSON"),
             # phase 5, wave 3 (amendment 6): spaCy 3.8 pipelines where one exists ...
             "pl": ("pl_core_news_md", "persName"), "sv": ("sv_core_news_md", "PRS"), "lt": ("lt_core_news_md", "PERSON"),
             "ro": ("ro_core_news_md", "PERSON"), "el": ("el_core_news_md", "PERSON"), "da": ("da_core_news_md", "PER"),
             "sl": ("sl_core_news_md", "PER"), "hr": ("hr_core_news_md", "PER"),
             # ... and the multilingual xx model (the `id` baseline pattern) where none does
             **{l: ("xx_ent_wiki_sm", "PER") for l in ("bg", "cs", "et", "sk", "lv", "hu", "sr", "vi", "ms", "tl")}}

# amendment 6: dictionary locales for wave 3. Faker has no sr/ms/tl person
# provider, so those borrow the closest one (disclosed in the pre-registration).
WAVE3_LOCALES = {"bg": ("bg_BG",), "pl": ("pl_PL",), "cs": ("cs_CZ",), "lt": ("lt_LT",), "et": ("et_EE",),
                 "sv": ("sv_SE",), "sk": ("sk_SK",), "lv": ("lv_LV",), "hu": ("hu_HU",), "ro": ("ro_RO",),
                 "el": ("el_GR",), "da": ("da_DK",), "sl": ("sl_SI",), "hr": ("hr_HR",),
                 "sr": ("hr_HR",), "vi": ("vi_VN",), "ms": ("id_ID",), "tl": ("es_ES", "en_US")}

# Languages with no usable spaCy NER. spaCy's multilingual xx_ent_wiki_sm found
# nothing on Arabic and tagged Hindi words as MISC when smoke-tested, so these
# use Hugging Face token-classification models. They need a Hugging Face
# download, so they run only where that is reachable (the author's Docker
# image). They carry their own per-span confidence scores. Licenses and model
# availability are checked at run time, and failures are recorded in
# TROUBLESHOOTING.
HF_NER = {"ar": "Davlan/xlm-roberta-base-ner-hrl",
          # amendment 4: ai4bharat/IndicNER turned out to be a GATED repository (401 in the
          # author's run). The ai4privacy hi/te rows also carry 100% Latin-script names
          # embedded in Devanagari/Telugu text, so a multilingual Latin-capable NER fits
          # the data. Davlan XLM-R: AFL-3.0, ungated.
          "hi": "Davlan/xlm-roberta-base-ner-hrl", "te": "Davlan/xlm-roberta-base-ner-hrl",
          "tr": "savasy/bert-base-turkish-ner-cased",
          "tr_mit": "akdeniz27/bert-base-turkish-cased-ner",  # amendment 6; MIT confirmed via HF API 2026-09-26
          # (cardData.license == "mit"). "tr" (savasy) has NO stated licence and stays
          # internal-only: do not cite or publish results from it, use tr_mit instead.
          # amendment 4: second Indonesian row with the project's own IndoBERT (id_ner.py MODEL_NAME)
          "id_hf": "cahya/bert-base-indonesian-NER"}


_HF_PIPES = {}  # model id -> pipeline, or the exception its construction raised


def _hf(model):
    """Build each Hugging Face pipeline once per process.

    functools.lru_cache does not cache a call that raises, so a model whose
    construction failed (e.g. tokenizer conversion without protobuf) was
    rebuilt for EVERY document: hundreds of "Loading weights" bars in the
    phase-5 log. Failures are cached here too and re-raised immediately, so a
    broken model costs one load and shows up as a per-document error.
    """
    hit = _HF_PIPES.get(model)
    if hit is None:
        import os
        os.environ.setdefault("TRANSFORMERS_VERBOSITY", "error")
        os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
        try:
            from transformers import pipeline
            from transformers.utils import logging as hf_logging
            hf_logging.set_verbosity_error()
            hf_logging.disable_progress_bar()
            hit = pipeline("token-classification", model=model, aggregation_strategy="simple")
        except Exception as ex:  # noqa: BLE001
            hit = RuntimeError(f"HF pipeline {model} failed to build: {ex!r}"[:400])
        _HF_PIPES[model] = hit
    if isinstance(hit, Exception):
        raise hit
    return hit


def scan_ner_hf(lang, text):
    out = []
    for e in _hf(HF_NER[lang])(text):
        grp = str(e.get("entity_group", e.get("entity", ""))).upper()
        if grp in ("PER", "PERSON", "B-PER", "I-PER"):
            out.append({"type": "PERSON", "start": int(e["start"]), "end": int(e["end"]),
                        "method": f"hf:{HF_NER[lang]}", "confidence": float(e["score"])})
    return out


@lru_cache(maxsize=8)
def _nlp(model):
    import spacy
    return spacy.load(model)


# amendment 7: GLiNER multilingual (Apache-2.0, zero-shot) for the rows whose
# only spaCy option was the noisy xx_ent_wiki_sm. Keys are "<lang>_gl".
GLINER_MODEL = "urchade/gliner_multi-v2.1"
GLINER_THRESHOLD = 0.3  # high-recall operating point, fixed a priori; PCCF filters for precision
GLINER_LANGS = ("bg", "cs", "et", "sk", "lv", "hu", "sr", "vi", "ms", "tl")
_GLINER = {}


def _gliner():
    hit = _GLINER.get("m")
    if hit is None:
        try:
            from gliner import GLiNER
            hit = GLiNER.from_pretrained(GLINER_MODEL)
        except Exception as ex:  # noqa: BLE001  (cache the failure, as _hf does)
            hit = RuntimeError(f"GLiNER {GLINER_MODEL} failed to load: {ex!r}"[:400])
        _GLINER["m"] = hit
    if isinstance(hit, Exception):
        raise hit
    return hit


def scan_ner_gliner(text):
    return [{"type": "PERSON", "start": int(e["start"]), "end": int(e["end"]), "method": f"gliner:{GLINER_MODEL}",
             "confidence": float(e["score"])}
            for e in _gliner().predict_entities(text, ["person"], threshold=GLINER_THRESHOLD)]


def scan_ner(lang, text):
    if lang.endswith("_gl") and lang[:-3] in GLINER_LANGS:
        return scan_ner_gliner(text)
    if lang in HF_NER:
        return scan_ner_hf(lang, text)
    model, label = NER_MODEL[lang]
    return [{"type": "PERSON", "start": e.start_char, "end": e.end_char, "method": f"ner:{model}"}
            for e in _nlp(model)(text).ents if e.label_ == label]


@lru_cache(maxsize=1)
def _ko():
    from faker.providers.person.ko_KR import Provider as P
    return set(P.last_names), set(P.first_names)


@lru_cache(maxsize=1)
def _zh():
    from faker.providers.person.zh_CN import Provider as P
    return set(P.last_names), set(P.first_names)


@lru_cache(maxsize=1)
def _in():
    from faker.providers.person.en_IN import Provider as P
    return {n.lower() for n in P.first_names} | {n.lower() for n in P.last_names}


@lru_cache(maxsize=16)
def _locale_names(loc):
    import importlib
    P = importlib.import_module(f"faker.providers.person.{loc}").Provider
    first = {_norm(n) for n in getattr(P, "first_names", ()) or ()}
    last = {_norm(n) for n in getattr(P, "last_names", ()) or ()}
    return first, last


_HARAKAT = re.compile(r"[\u064B-\u0652\u0670\u0640]")


def _norm(n):
    return _HARAKAT.sub("", str(n)).lower()


LATIN_LOCALES = {"de": ("de_DE",), "it": ("it_IT",), "nl": ("nl_NL",), "pt": ("pt_BR", "pt_PT"),
                 "tr": ("tr_TR",), "fi": ("fi_FI",)}
_LATIN_RUN = re.compile(r"[A-ZÀ-ÖØ-ÞİÇĞŞÜÖ][\w'’-]+(?: [A-ZÀ-ÖØ-ÞİÇĞŞÜÖ][\w'’-]+){0,3}")
_ARABIC_TOK = re.compile(r"[\u0600-\u06FF]+")
_DEVANAGARI_TOK = re.compile(r"[\u0900-\u097F]+")
_AR_CONNECT = {"بن", "ابن", "بنت", "آل", "عبد", "ال"}
_CJK = re.compile(r"[\u4e00-\u9fff\u3040-\u30ff]+")


def _latin_names(lang):
    first, last = set(), set()
    for loc in LATIN_LOCALES[lang]:
        try:
            f, l_ = _locale_names(loc)
            first |= f
            last |= l_
        except ModuleNotFoundError:
            pass
    return first | last


def _token_runs(text, tok_re, names, first, connectors=frozenset()):
    """Runs of adjacent script tokens that are all names (or connectors),
    kept when the run contains at least one given name."""
    hits, run = [], []
    toks = list(tok_re.finditer(text))
    for i, m in enumerate(toks + [None]):
        t = _norm(m.group(0)) if m else None
        adjacent = m is not None and run and text[run[-1].end():m.start()].strip() == ""
        if m is not None and (t in names or t in connectors) and (adjacent or not run):
            run.append(m)
            continue
        if run and any(_norm(x.group(0)) in first for x in run):
            while run and _norm(run[-1].group(0)) in connectors:
                run.pop()
            if run:
                hits.append((run[0].start(), run[-1].end()))
        run = [m] if m is not None and (t in names) else []
    return hits


JA_STANDALONE = True
_HANGUL = re.compile(r"[가-힣]+")
_HAN = re.compile(r"[一-鿿]+")
_CAP_RUN = re.compile(r"[A-Z][a-zA-Z]+(?: [A-Z][a-zA-Z]+){0,3}")


LATIN_MULTI_LOCALES = ("en_US", "en_GB", "fr_FR", "de_DE", "it_IT", "es_ES", "nl_NL", "pt_BR", "tr_TR",
                       "fi_FI", "pl_PL", "sv_SE", "da_DK", "cs_CZ", "hu_HU", "ro_RO", "el_GR")


@lru_cache(maxsize=1)
def _latin_multi():
    names = set()
    for loc in LATIN_MULTI_LOCALES:
        try:
            f, l_ = _locale_names(loc)
            names |= {n for n in f | l_ if n.isascii() or any(c.isalpha() for c in n)}
        except ModuleNotFoundError:
            pass
    return names


def _scan_latin_multi(text, tag):
    names = _latin_multi()
    return [{"type": "PERSON", "start": m.start(), "end": m.end(), "method": f"dict:{tag}:latin-multi"}
            for m in _LATIN_RUN.finditer(text)
            if any(len(w) >= 3 and _norm(w) in names for w in m.group(0).split())]


_WORD = re.compile(r"[^\W\d_][\w'’-]*")


def _cap_runs(text, max_len=4):
    """Script-agnostic runs of up to max_len adjacent capitalised words
    (works for Latin, Cyrillic and Greek alike)."""
    out, run = [], []
    for m in list(_WORD.finditer(text)) + [None]:
        ok = m is not None and m.group(0)[0].isupper()
        adjacent = ok and run and text[run[-1].end():m.start()] == " " and len(run) < max_len
        if ok and (adjacent or not run):
            run.append(m)
            continue
        if run:
            out.append(run)
        run = [m] if ok else []
    return out


@lru_cache(maxsize=32)
def _wave3_names(lang):
    # Wider than _locale_names (left unchanged so earlier waves reproduce):
    # every first/middle/last-name list the provider defines, male/female too.
    import importlib
    names = set()
    for loc in WAVE3_LOCALES[lang]:
        P = importlib.import_module(f"faker.providers.person.{loc}").Provider
        for attr in dir(P):
            if attr.startswith(("first_name", "last_name", "middle_name")) and attr.endswith(("s", "names")):
                v = getattr(P, attr)
                if isinstance(v, (tuple, list, dict, set, frozenset)):
                    names |= {t for n in v if isinstance(n, str) for t in _norm(n).split()}
    return frozenset(names)


def scan_dict(lang, text):
    hits = []
    if lang == "id_hf":
        lang = "id"
    if lang == "tr_mit":
        lang = "tr"
    if lang.endswith("_gl"):
        lang = lang[:-3]
    if lang in WAVE3_LOCALES:
        names = _wave3_names(lang)
        return [{"type": "PERSON", "start": r[0].start(), "end": r[-1].end(), "method": f"dict:{lang}"}
                for r in _cap_runs(text) if any(len(m.group(0)) >= 3 and _norm(m.group(0)) in names for m in r)]
    if lang == "id":
        import id_detect
        return [h for h in id_detect.scan_indonesian_names(text) if h["type"] == "PERSON"]
    if lang == "ko":
        sur, giv = _ko()
        for m in _HANGUL.finditer(text):
            run = m.group(0)
            for sl in (2, 1):
                if run[:sl] in sur and run[sl:sl + 2] in giv:
                    hits.append({"type": "PERSON", "start": m.start(), "end": m.start() + sl + 2, "method": "dict:ko"})
                    break
    elif lang == "zh":
        sur, giv = _zh()
        for m in _HAN.finditer(text):
            run, i = m.group(0), 0
            while i < len(run):
                found = False
                for sl in (2, 1):
                    if run[i:i + sl] in sur:
                        for gl in (2, 1):
                            if len(run[i + sl:i + sl + gl]) == gl and run[i + sl:i + sl + gl] in giv:
                                hits.append({"type": "PERSON", "start": m.start() + i,
                                             "end": m.start() + i + sl + gl, "method": "dict:zh"})
                                i += sl + gl
                                found = True
                                break
                    if found:
                        break
                if not found:
                    i += 1
    elif lang in LATIN_LOCALES:
        names = _latin_names(lang)
        for m in _LATIN_RUN.finditer(text):
            if any(len(w) >= 3 and _norm(w) in names for w in m.group(0).split()):
                hits.append({"type": "PERSON", "start": m.start(), "end": m.end(), "method": f"dict:{lang}"})
    elif lang == "ar":
        f1, l1 = _locale_names("ar_AA")
        f2, l2 = _locale_names("ar_SA")
        first = {t for n in f1 | f2 for t in n.split()} - _AR_CONNECT
        names = first | {t for n in l1 | l2 for t in n.split()} - _AR_CONNECT
        hits += [{"type": "PERSON", "start": a, "end": b, "method": "dict:ar"}
                 for a, b in _token_runs(text, _ARABIC_TOK, names, first, _AR_CONNECT)]
    elif lang == "hi":
        first, last = _locale_names("hi_IN")
        hits += [{"type": "PERSON", "start": a, "end": b, "method": "dict:hi"}
                 for a, b in _token_runs(text, _DEVANAGARI_TOK, first | last, first)]
        hits += _scan_latin_multi(text, "hi")  # amendment 4: the dataset's names are Latin-script
    elif lang == "te":
        hits += _scan_latin_multi(text, "te")  # amendment 4: no Faker te_IN; names are Latin-script
    elif lang == "ja" and JA_STANDALONE:
        # amendment 4 repair: the surname+given combination rule produced 0 hits
        # in the author's run, because the gold names are mostly single
        # surnames (三浦, 青木 ...). Match any Faker ja_JP name of >= 2
        # characters on its own; a 1-character name only before さん/様/氏/君.
        giv, sur = _locale_names("ja_JP")
        vocab = sorted(giv | sur, key=len, reverse=True)
        for m in _CJK.finditer(text):
            run, i = m.group(0), 0
            while i < len(run):
                for v in vocab:
                    if run.startswith(v, i) and (len(v) >= 2 or run[i + len(v):i + len(v) + 2].startswith(("さん", "様", "氏", "君"))):
                        hits.append({"type": "PERSON", "start": m.start() + i, "end": m.start() + i + len(v),
                                     "method": "dict:ja"})
                        i += len(v)
                        break
                else:
                    i += 1
    elif lang == "ja":
        giv, sur = _locale_names("ja_JP")  # (first_names, last_names)
        for m in _CJK.finditer(text):
            run, i = m.group(0), 0
            while i < len(run):
                found = False
                for sl in (3, 2, 1):
                    if run[i:i + sl] in sur:
                        for gl in (3, 2, 1):
                            g = run[i + sl:i + sl + gl]
                            if len(g) == gl and g in giv:
                                hits.append({"type": "PERSON", "start": m.start() + i, "end": m.start() + i + sl + gl,
                                             "method": "dict:ja"})
                                i += sl + gl
                                found = True
                                break
                    if found:
                        break
                if not found:
                    i += 1
    elif lang == "in":
        names = _in()
        for m in _CAP_RUN.finditer(text):
            if any(len(w) >= 3 and w.lower() in names for w in m.group(0).split()):
                hits.append({"type": "PERSON", "start": m.start(), "end": m.end(), "method": "dict:in"})
    return hits


# ---------------------------------------------------------------------------
# Phase 11 (PCCF_PHASE11_PREREGISTRATION.md): English layer sets.
#   en_redact: production REDACT detector (detect.scan_ner = Presidio + spaCy
#              en_core_web_lg; detect.scan_flattened = flattened-name layer)
#   en_spacy : spaCy en_core_web_lg PERSON directly + SSA/Census name
#              dictionary over capitalised word runs
# ---------------------------------------------------------------------------
def scan_dict_en(kind, text):
    if kind == "en_redact":
        import detect
        return [h for h in detect.scan_flattened(text) if h["type"] == "PERSON"]
    import flattened_names_ext as FX
    giv, sur = FX._lists()
    names = (giv or set()) | (sur or set())
    out = []
    for r in _cap_runs(text):  # keep maximal sub-runs of consecutive name tokens
        cur = []
        for m in r + [None]:
            if m is not None and len(m.group(0)) >= 3 and m.group(0).lower() in names:
                cur.append(m)
                continue
            if cur:
                out.append({"type": "PERSON", "start": cur[0].start(), "end": cur[-1].end(),
                            "method": "dict:en-ssa-census"})
            cur = []
    return out


def scan_ner_en(kind, text):
    if kind == "en_redact":
        import detect
        return [h for h in detect.scan_ner(text) if h["type"] == "PERSON"]
    return [{"type": "PERSON", "start": e.start_char, "end": e.end_char, "method": "ner:en_core_web_lg"}
            for e in _nlp("en_core_web_lg")(text).ents if e.label_ == "PERSON"]
