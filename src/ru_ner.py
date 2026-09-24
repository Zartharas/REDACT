"""
Layer (opt-in, Docker-only): Russian NER via Presidio, for the Russian PII
pilot sample's PERSON recall/precision -- the one number ru_detect.py's
lemmatized dictionary layer could not measure honestly, because this
project's own sandbox blocks the spaCy model download entirely (same
documented limitation as es_ner.py/fr_ner.py; note this is a DIFFERENT
limitation from ru_detect.py's pymorphy2 lemmatizer, which installs fine
directly via pip in this sandbox -- see ru_detect.py's docstring).

WHY THIS IS A SEPARATE MODULE, AND SEPARATE FROM detect.py's ANALYZER:
detect.py's _get_analyzer() loads Presidio's DEFAULT AnalyzerEngine, which
uses an English spaCy model. Running that against Russian (Cyrillic) text
would not error -- it would silently return near-zero real recall, which
would misleadingly look like "NER doesn't help here" when the real story
is "the wrong language model was loaded." This module builds a second,
independent Presidio AnalyzerEngine explicitly configured for Russian
(ru_core_news_md), imported and used ONLY by evaluate_ru.py's `--with-ner`
path -- detect.py's own analyzer and every existing English-corpus
recall/precision number are completely untouched by this file's
existence, same "additive, not modifying" discipline as fr_ner.py/
es_ner.py.

WHY DOCKER, NOT THIS PROJECT'S NORMAL SANDBOX: `python -m spacy download
ru_core_news_md` needs real internet access to spaCy's model release
infra, which this sandbox's network allowlist blocks. See Dockerfile.ru_ner
and run_ru_ner.sh for the actual run mechanics -- near-identical to the
Spanish/French versions with only the model name and entrypoint swapped.

Presidio's built-in SpacyRecognizer maps spaCy's Russian NER labels
(PER/LOC/ORG, the label set ru_core_news_md's pipeline produces) to
Presidio's canonical entities the same way it already does for English,
Spanish, and French (PER -> PERSON) -- no extra per-language recognizer
configuration needed beyond pointing NlpEngineProvider at the Russian
model. Whether spaCy's Russian NER handles inflected surnames better than
the lemmatized dictionary layer (the whole reason NER exists as a
comparison point here) is an empirical question for --diagnose to answer,
not assumed.
"""
from functools import lru_cache


@lru_cache(maxsize=1)
def _get_ru_analyzer():
    # Imported lazily, same reasoning as detect.py's/es_ner.py's/fr_ner.py's
    # lazy analyzer construction: model loading is the expensive part,
    # callers that don't need it shouldn't pay for it, and importing
    # presidio_analyzer at module level would make this file fail to
    # import at all in this project's normal (network-blocked, Docker-less)
    # sandbox before anyone even calls it.
    from presidio_analyzer import AnalyzerEngine
    from presidio_analyzer.nlp_engine import NlpEngineProvider

    configuration = {
        "nlp_engine_name": "spacy",
        "models": [{"lang_code": "ru", "model_name": "ru_core_news_md"}],
    }
    provider = NlpEngineProvider(nlp_configuration=configuration)
    nlp_engine = provider.create_engine()
    return AnalyzerEngine(nlp_engine=nlp_engine, supported_languages=["ru"])


def scan_ru_ner(text: str, min_score: float = 0.5) -> list[dict]:
    """Russian NER -> PERSON hits only (the one RU-mapped type NER is
    relevant to -- EMAIL/CREDIT_CARD/SSN/MRN/IP are already regex's job,
    see ru_detect.py and RU_TYPE_MAPPING.md). Same hit-dict shape as
    detect.py's scan_ner()/scan_regex(), es_ner.py's/fr_ner.py's
    scan_es_ner()/scan_fr_ner(), and ru_detect.py's scan_ru(), so results
    concatenate directly."""
    analyzer = _get_ru_analyzer()
    results = analyzer.analyze(text=text, language="ru", entities=["PERSON"])
    return [
        {"type": "PERSON", "start": r.start, "end": r.end, "method": "ru_ner"}
        for r in results
        if r.score >= min_score
    ]
