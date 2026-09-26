"""
Language-agnostic structural features for PCCF (phase 4, F4). Additive.

This replaces the per-language cue WORD LISTS in pccf_context.ES_CUES /
FR_CUES / RU_CUES with Universal Dependencies structure read from any
spaCy pipeline that has a tagger and parser (es/fr/ru/en news/web
models): the POS and dependency relation of the neighbouring tokens and
of the span's head. No word identity is used anywhere, so the features
carry across languages and are not tied to one corpus's template
wording. That is the phase-3 T7 lesson: lexical features broke
exchangeability.

The only non-UD structural cue kept is whether the line starts with a
"Label:"-shaped prefix. Which label it is (name vs place) is NOT used,
because that would be a word list again.
"""
import pccf_context as ctx

UPOS = ["ADJ", "ADP", "ADV", "AUX", "CCONJ", "DET", "INTJ", "NOUN", "NUM", "PART", "PRON", "PROPN",
        "PUNCT", "SCONJ", "SYM", "VERB", "X"]
DEPS = ["case", "det", "punct", "nsubj", "obj", "obl", "nmod", "appos", "flat", "compound", "ROOT", "amod",
        "conj", "iobj", "advmod"]


def _onehot(prefix, val, vocab):
    return {f"{prefix}={v}": float(val == v) for v in vocab} | {f"{prefix}=other": float(val not in vocab)}


def ud_features(doc, text, cand) -> dict:
    span = doc.char_span(cand.start, cand.end, alignment_mode="expand")
    toks = list(span) if span is not None else []
    first = toks[0].i if toks else None
    prev = doc[first - 1] if first not in (None, 0) else None
    nxt = doc[toks[-1].i + 1] if toks and toks[-1].i + 1 < len(doc) else None
    root = span.root if span is not None and len(span) else None
    f = {"bias": 1.0,
         "pat_dict": float(cand.mask == frozenset({"dict"})),
         "pat_ner": float(cand.mask == frozenset({"ner"})),
         "pat_both": float(cand.mask == frozenset({"dict", "ner"})),
         "ner_conf": 1.0 - cand.scores["ner"] if "ner" in cand.scores else 0.0,
         "has_ner_conf": float("ner" in cand.scores),
         "n_words": min(len(toks), 5) / 5.0,
         "has_digit": float(any(ch.isdigit() for ch in text[cand.start:cand.end])),
         "line_label_prefix": float(ctx.field_label(text, cand.start)[0] != "none"),
         "sent_initial": float(bool(toks) and bool(toks[0].is_sent_start)),  # None when the model has no sentence boundaries (xx)
         "frac_propn": (sum(t.pos_ == "PROPN" for t in toks) / len(toks)) if toks else 0.0,
         "frac_noun": (sum(t.pos_ == "NOUN" for t in toks) / len(toks)) if toks else 0.0}
    f |= _onehot("prev_pos", prev.pos_ if prev is not None else "<s>", UPOS)
    f |= _onehot("prev_dep", prev.dep_ if prev is not None else "<s>", DEPS)
    f |= _onehot("next_pos", nxt.pos_ if nxt is not None else "</s>", UPOS)
    f |= _onehot("root_dep", root.dep_ if root is not None else "none", DEPS)
    f |= _onehot("head_pos", root.head.pos_ if root is not None else "none", UPOS)
    return f
