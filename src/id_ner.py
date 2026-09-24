"""
Layer (opt-in, Docker-only): Indonesian NER via a HuggingFace transformers
pipeline -- NOT Presidio+spaCy, unlike every other language layer in this
project (es_ner.py/fr_ner.py/ru_ner.py all use Presidio's
NlpEngineProvider pointed at a spaCy model). This is the exact
architectural difference LANGUAGE_EXTENSION.md flagged in advance:
Indonesian has no official spaCy model at all (community IndoBERT-NER
fine-tunes exist instead), so `NlpEngineProvider({"nlp_engine_name":
"spacy", ...})` has nothing to point at -- there is no `id_core_news_*`
model to `spacy.load()`. Wiring in a HuggingFace token-classification
model directly (bypassing Presidio's spaCy-centric NlpEngineProvider
entirely) is the correct fix, not a workaround -- confirmed necessary
before writing any code, per this project's "address flagged gotchas from
the start" discipline (see ru_detect.py's docstring for the same
discipline applied to Russian's inflection problem).

MODEL CHOICE: `cahya/bert-base-indonesian-NER`, a community-maintained
(not "official," same framing LANGUAGE_EXTENSION.md itself used) BERT
model fine-tuned for Indonesian named entity recognition, predicting
standard CoNLL-style PER/ORG/LOC/MISC tags. Chosen because it is: (a)
openly licensed and freely downloadable from the HuggingFace model hub,
matching this project's "open-source / free tools only" constraint; (b)
specifically fine-tuned for Indonesian (not a multilingual model applied
zero-shot, avoiding the same "wrong model silently returns near-zero
recall" trap detect.py's own English analyzer would fall into on
non-English text -- see es_ner.py's/fr_ner.py's/ru_ner.py's identical
warning); (c) a PER label REDACT's canonical PERSON type maps onto
directly, same as Presidio's SpacyRecognizer already does for the other
four languages.

WHY DOCKER, NOT THIS PROJECT'S NORMAL SANDBOX: downloading model weights
from huggingface.co needs real internet access this sandbox's network
allowlist blocks (same restriction that already prevents every other
language's model download here). See Dockerfile.id_ner and run_id_ner.sh.

NOT A Presidio AnalyzerEngine, ON PURPOSE: Presidio's NlpEngineProvider
architecture assumes a spaCy-compatible NLP engine. Forcing a transformers
model through it would need Presidio's more involved (and, as of this
project's dependency pins, less battle-tested) TransformersNlpEngine
integration. Calling the HuggingFace `pipeline("ner", ...)` API directly
and mapping its output to this project's own hit-dict shape is simpler,
has fewer moving parts to debug inside a Docker-only environment nobody
can interactively poke at from this sandbox, and produces the exact same
{"type", "start", "end", "method"} shape every other scan_*() function in
this project already returns -- the abstraction this project actually
needs is "a function that returns PERSON spans," not "a Presidio
AnalyzerEngine specifically."
"""
from functools import lru_cache

MODEL_NAME = "cahya/bert-base-indonesian-NER"


@lru_cache(maxsize=1)
def _get_id_ner_pipeline():
    # Imported lazily, same reasoning as every other _ner.py in this
    # project: model loading is the expensive part, callers that don't
    # need it shouldn't pay for it, and importing transformers at module
    # level would make this file fail to import at all in this project's
    # normal (network-blocked, Docker-less, transformers-not-installed)
    # sandbox before anyone even calls it.
    from transformers import pipeline

    return pipeline(
        "ner",
        model=MODEL_NAME,
        tokenizer=MODEL_NAME,
        aggregation_strategy="simple",  # merges sub-word tokens into whole-entity spans
    )


def scan_id_ner(text: str, min_score: float = 0.5) -> list[dict]:
    """Indonesian NER -> PERSON hits only (the one ID-mapped type NER is
    relevant to -- EMAIL/CREDIT_CARD/SSN are already regex's/dictionary's
    job, see id_detect.py and ID_TYPE_MAPPING.md). Same hit-dict shape as
    detect.py's scan_ner()/scan_regex() and the other languages'
    scan_{lang}_ner(), so results concatenate directly.

    cahya/bert-base-indonesian-NER's label scheme uses "PER" for person
    entities (standard CoNLL-style tagging, same convention spaCy's
    Spanish/French/Russian PER label already uses) -- mapped to REDACT's
    canonical PERSON the same way Presidio's SpacyRecognizer does it for
    the other four languages."""
    ner = _get_id_ner_pipeline()
    results = ner(text)
    hits = []
    for r in results:
        # aggregation_strategy="simple" produces an "entity_group" key
        # (e.g. "PER", "ORG", "LOC") rather than per-token "B-"/"I-" tags.
        if r.get("entity_group") != "PER":
            continue
        if r.get("score", 0.0) < min_score:
            continue
        hits.append({
            "type": "PERSON",
            "start": r["start"],
            "end": r["end"],
            "method": "id_ner",
        })
    return hits
