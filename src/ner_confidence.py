"""
Per-span NER confidence for PCCF (src/pccf.py) -- additive, research-only.

WHY THIS EXISTS: ALGORITHM_DESIGN.md Section 6.1 assumes "NER emits a model
confidence score natively". Through Presidio it does not. Presidio's
SpacyRecognizer gives every spaCy entity the same fixed score (measured on
MEDDOCAN: all 320 PERSON hits in 60 documents scored exactly 0.85). A
constant score makes PCCF's per-pattern threshold all-or-nothing. That
would make the feasibility check fail by construction, not on the merits.

WHAT IT DOES: it does NOT change which spans the NER layer emits. Spans
still come from the unmodified {lang}_ner.py / Presidio path. This helper
loads the SAME spaCy pipeline, runs spaCy's own beam search over the
entity recognizer (beam_parse + moves.get_beam_parses), and reads off the
beam-marginal probability of each emitted span having the target label.
It is the model's own confidence and adds no new model or training.

Scoring rule for an emitted span: the beam marginal of the exact
(start_char, end_char, label) span if the beam contains it; otherwise the
largest marginal of any same-label beam span that overlaps it (greedy and
beam decoding can disagree on boundaries); otherwise 0.0.

Language-agnostic: pass the model name (es_core_news_md, fr_core_news_md,
ru_core_news_md, ...) and that model's label for persons ("PER" for the
es/fr/ru spaCy news models).
"""
from collections import defaultdict
from functools import lru_cache


@lru_cache(maxsize=4)
def _load(model_name: str):
    import spacy
    return spacy.load(model_name)


def beam_marginals(text: str, model_name: str, label: str = "PER",
                   beam_width: int = 16, beam_density: float = 1e-4) -> dict:
    """{(start_char, end_char): marginal probability} for `label` spans."""
    nlp = _load(model_name)
    with nlp.select_pipes(disable=["ner"]):
        doc = nlp(text)
    ner = nlp.get_pipe("ner")
    beams = ner.beam_parse([doc], beam_width=beam_width, beam_density=beam_density)
    out = defaultdict(float)
    for beam in beams:
        for prob, ents in ner.moves.get_beam_parses(beam):
            for s, e, lab in ents:
                if lab == label:
                    span = doc[s:e]
                    out[(span.start_char, span.end_char)] += prob
    return dict(out)


def annotate(hits: list, text: str, model_name: str, label: str = "PER") -> list:
    """Return copies of `hits` with a "confidence" key added (see module
    docstring for the matching rule). Input hits are not mutated."""
    marg = beam_marginals(text, model_name, label)
    out = []
    for h in hits:
        conf = marg.get((h["start"], h["end"]))
        if conf is None:
            conf = max((p for (s, e), p in marg.items() if s < h["end"] and h["start"] < e),
                       default=0.0)
        out.append({**h, "confidence": min(1.0, float(conf))})
    return out
