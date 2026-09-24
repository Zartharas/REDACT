"""
Layer (opt-in, Docker-only): French NER via Presidio, for the OpenPII French
sample's PERSON recall/precision -- the one number fr_detect.py's dictionary
layer could not measure honestly, because this project's own sandbox blocks
the spaCy model download entirely (see detect.py's own documented
network-restriction comments, repeated project-wide, and es_ner.py's
identical rationale for Spanish).

WHY THIS IS A SEPARATE MODULE, AND SEPARATE FROM detect.py's ANALYZER:
detect.py's _get_analyzer() loads Presidio's DEFAULT AnalyzerEngine, which
uses an English spaCy model. Running that against French text would not
error -- it would silently return near-zero real recall, which would
misleadingly look like "NER doesn't help here" when the real story is "the
wrong language model was loaded." This module builds a second, independent
Presidio AnalyzerEngine explicitly configured for French (fr_core_news_md),
imported and used ONLY by evaluate_fr.py's `--with-ner` path -- detect.py's
own analyzer, its cache, and every existing English-corpus recall/precision
number are completely untouched by this file's existence, same
"additive, not modifying" discipline as fr_detect.py and es_ner.py.

WHY DOCKER, NOT THIS PROJECT'S NORMAL SANDBOX: `python -m spacy download
fr_core_news_md` needs real internet access to spaCy's model release infra,
which this sandbox's network allowlist blocks (same restriction that
already prevents detect.py's own English model and es_ner.py's Spanish
model from loading here). See Dockerfile.fr_ner and run_fr_ner.sh for the
actual run mechanics -- both are near-identical copies of the meddocan/es
versions with only the image name, model name, and script swapped in.

Presidio's built-in SpacyRecognizer maps spaCy's French NER labels
(PER/LOC/ORG/MISC, the label set fr_core_news_md's pipeline produces) to
Presidio's canonical entities the same way it already does for English and
Spanish (PER -> PERSON) -- no extra per-language recognizer configuration
needed beyond pointing NlpEngineProvider at the French model.
"""
from functools import lru_cache


@lru_cache(maxsize=1)
def _get_fr_analyzer():
    # Imported lazily, same reasoning as detect.py's _get_analyzer() and
    # es_ner.py's _get_es_analyzer(): model loading is the expensive part,
    # callers that don't need it shouldn't pay for it, and importing
    # presidio_analyzer at module level would make this file fail to
    # import at all in this project's normal (network-blocked, Docker-less)
    # sandbox before anyone even calls it.
    from presidio_analyzer import AnalyzerEngine
    from presidio_analyzer.nlp_engine import NlpEngineProvider

    configuration = {
        "nlp_engine_name": "spacy",
        "models": [{"lang_code": "fr", "model_name": "fr_core_news_md"}],
    }
    provider = NlpEngineProvider(nlp_configuration=configuration)
    nlp_engine = provider.create_engine()
    return AnalyzerEngine(nlp_engine=nlp_engine, supported_languages=["fr"])


def scan_fr_ner(text: str, min_score: float = 0.5) -> list[dict]:
    """French NER -> PERSON hits only (the one OpenPII-mapped type NER is
    relevant to -- EMAIL/CREDIT_CARD are already regex's job, see
    fr_detect.py and FR_TYPE_MAPPING.md). Same hit-dict shape as
    detect.py's scan_ner()/scan_regex(), es_ner.py's scan_es_ner(), and
    fr_detect.py's scan_fr(), so results concatenate directly."""
    analyzer = _get_fr_analyzer()
    results = analyzer.analyze(text=text, language="fr", entities=["PERSON"])
    return [
        {"type": "PERSON", "start": r.start, "end": r.end, "method": "fr_ner"}
        for r in results
        if r.score >= min_score
    ]
