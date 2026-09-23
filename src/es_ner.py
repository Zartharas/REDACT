"""
Layer 6 (opt-in, Docker-only): Spanish NER via Presidio, for MEDDOCAN's
PERSON recall/precision -- the one number es_detect.py's dictionary layer
(Task 5) could not measure honestly, because this project's own sandbox
blocks the spaCy model download entirely (see detect.py's own documented
network-restriction comments, repeated project-wide).

WHY THIS IS A SEPARATE MODULE, AND SEPARATE FROM detect.py's ANALYZER:
detect.py's _get_analyzer() loads Presidio's DEFAULT AnalyzerEngine, which
uses an English spaCy model. Running that against MEDDOCAN's Spanish text
would not error -- it would silently return near-zero real recall, which
would misleadingly look like "NER doesn't help on this dataset" when the
real story is "the wrong language model was loaded." This module builds a
second, independent Presidio AnalyzerEngine explicitly configured for
Spanish (es_core_news_md), imported and used ONLY by
evaluate_meddocan.py's `--with-ner` path -- detect.py's own analyzer,
its cache, and every existing English-corpus recall/precision number are
completely untouched by this file's existence, same "additive, not
modifying" discipline as es_detect.py.

WHY DOCKER, NOT THIS PROJECT'S NORMAL SANDBOX: `python -m spacy download
es_core_news_md` needs real internet access to spaCy's model release
infra, which this sandbox's network allowlist blocks (same restriction
that already prevents detect.py's own English model from loading here --
see BUGS_AND_FIXES.md). See Dockerfile.meddocan_ner and
run_meddocan_ner.sh for the actual run mechanics.

Presidio's built-in SpacyRecognizer maps spaCy's Spanish NER labels
(PER/LOC/ORG/MISC, the label set es_core_news_md's pipeline produces) to
Presidio's canonical entities the same way it already does for English
(PER -> PERSON) -- no extra per-language recognizer configuration needed
beyond pointing NlpEngineProvider at the Spanish model.
"""
from functools import lru_cache


@lru_cache(maxsize=1)
def _get_es_analyzer():
    # Imported lazily, same reasoning as detect.py's _get_analyzer(): model
    # loading is the expensive part, callers that don't need it shouldn't
    # pay for it, and importing presidio_analyzer at module level would
    # make this file fail to import at all in this project's normal
    # (network-blocked, Docker-less) sandbox before anyone even calls it.
    from presidio_analyzer import AnalyzerEngine
    from presidio_analyzer.nlp_engine import NlpEngineProvider

    configuration = {
        "nlp_engine_name": "spacy",
        "models": [{"lang_code": "es", "model_name": "es_core_news_md"}],
    }
    provider = NlpEngineProvider(nlp_configuration=configuration)
    nlp_engine = provider.create_engine()
    return AnalyzerEngine(nlp_engine=nlp_engine, supported_languages=["es"])


def scan_es_ner(text: str, min_score: float = 0.5) -> list[dict]:
    """Spanish NER -> PERSON hits only (the one MEDDOCAN-mapped type NER is
    relevant to -- EMAIL/MRN/SSN are already regex's job, see
    es_detect.py). Same hit-dict shape as detect.py's scan_ner()/
    scan_regex() and es_detect.py's scan_es(), so results concatenate
    directly."""
    analyzer = _get_es_analyzer()
    results = analyzer.analyze(text=text, language="es", entities=["PERSON"])
    return [
        {"type": "PERSON", "start": r.start, "end": r.end, "method": "es_ner"}
        for r in results
        if r.score >= min_score
    ]
