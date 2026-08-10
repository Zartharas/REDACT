"""
Tests the field-level NER gate (src/evaluate.py's _build_ner_candidate,
_remap_hit, and the use_field_gate branch of run_evaluation) -- the
engineering upgrade meant to close the PERSON recall gap the whole-line
tiered strategy has (0.113 tiered vs. 0.359 naive on this project's own
current corpus, see README.md's comparison table).

Cannot exercise this against the real spaCy/Presidio model in this
environment (no network access to fetch en_core_web_lg here, same
constraint noted throughout BUGS_AND_FIXES.md and tests/README.md).
Instead, monkeypatches detect.scan_ner to a stub that just records what
text it was called with, so these tests verify the actual claims that
matter mechanically: which characters get excised before NER runs, that
the candidate is actually SHORTER (not just internally altered) when
something is excised, that a hit's offsets correctly round-trip back to
the original text's coordinate space, and when the NER call is skipped
entirely vs. made. The real recall/throughput numbers this is meant to
improve need a live run of `python src/evaluate.py` in an environment
with the model available.

HISTORY: this file originally tested `_mask_regex_covered_fields`, a
same-length masking approach (regex-covered spans replaced with '#'
placeholders). Measured against the real model 2026-08-09: the recall
fix worked, the throughput claim did not -- same-length masking doesn't
reduce what spaCy has to process, so field-gated measured SLOWER than
naive. Replaced the same day with `_build_ner_candidate`, which excises
(removes, doesn't just mask) regex-covered spans so the candidate is
genuinely shorter, plus `_remap_hit` to translate resulting offsets back
to the original text. This file was rewritten to match; see
src/evaluate.py's own docstring for the full before/after reasoning.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import detect  # noqa: E402
import evaluate  # noqa: E402


def test_excise_shortens_candidate_and_keeps_unmasked_content():
    """A windows_event line with an SSN in one field and a name in
    another: the SSN field's characters should be REMOVED (not just
    replaced), the name should survive untouched, and the candidate
    should be strictly shorter than the original -- this length
    reduction is the whole point of excising instead of masking."""
    text = "TargetUserName=Timothy Wong TargetSSN=123-45-6789"
    regex_hits = detect.scan_regex(text)
    assert any(h["type"] == "SSN" for h in regex_hits)

    candidate, segments = evaluate._build_ner_candidate(text, "windows_event", regex_hits)

    assert candidate is not None
    assert "Timothy Wong" in candidate, "name field must survive unexcised"
    assert "123-45-6789" not in candidate, "regex-covered SSN field must be excised"
    assert len(candidate) < len(text), "excising must actually shorten the candidate"
    assert len(candidate) == len(text) - len("123-45-6789")
    assert segments is not None


def test_build_candidate_returns_none_when_nothing_alphabetic_remains():
    """A line that's entirely numeric/structured fields, all regex-covered
    -- nothing left for NER to plausibly find, so the caller should skip
    the NER call entirely."""
    text = "SRC=10.0.0.5 DST=10.0.0.9"
    regex_hits = detect.scan_regex(text)
    assert any(h["type"] == "IP" for h in regex_hits)

    candidate, segments = evaluate._build_ner_candidate(text, "windows_event", regex_hits)

    assert candidate is None
    assert segments is None


def test_build_candidate_falls_back_to_original_text_for_unrecognized_log_type():
    """No field structure known (e.g. log_type not in fields.py's coverage)
    -- must fall back to the original text, not silently skip NER. Falling
    back to the ORIGINAL text (not None) with segments=None is the
    conservative, recall-safe default when field boundaries aren't known,
    and needs no offset remapping since nothing was excised."""
    text = "some free text line with Timothy Wong in it, SSN 123-45-6789"
    regex_hits = detect.scan_regex(text)

    candidate, segments = evaluate._build_ner_candidate(text, "some_unknown_type", regex_hits)

    assert candidate == text
    assert segments is None


def test_build_candidate_returns_unchanged_text_when_nothing_is_excised():
    """Fields are recognized, but none of them happen to overlap a regex
    hit -- nothing to excise, so the candidate should equal the original
    text (not an empty-excise-ranges edge case that accidentally mangles
    anything), with segments=None since no remapping is needed."""
    text = "TargetUserName=Timothy Wong TargetDept=Engineering"
    regex_hits = detect.scan_regex(text)
    assert regex_hits == []

    candidate, segments = evaluate._build_ner_candidate(text, "windows_event", regex_hits)

    assert candidate == text
    assert segments is None


def test_remap_hit_translates_offsets_back_to_original_text():
    """The correctness-critical piece: a hit's [start, end) reported
    against the (shorter) candidate must map back to the exact matching
    substring in the ORIGINAL text, not an off-by-N position shifted by
    however much was excised before it."""
    text = "TargetUserName=Timothy Wong TargetSSN=123-45-6789 EventTime=2024-01-01"
    regex_hits = detect.scan_regex(text)
    candidate, segments = evaluate._build_ner_candidate(text, "windows_event", regex_hits)

    idx = candidate.index("Timothy Wong")
    hit = {"type": "PERSON", "start": idx, "end": idx + len("Timothy Wong"), "method": "ner"}
    remapped = evaluate._remap_hit(hit, segments)

    assert text[remapped["start"]:remapped["end"]] == "Timothy Wong"


def test_remap_hit_after_excised_span_still_resolves_correctly():
    """A hit sitting AFTER the excised span in the candidate (so its
    candidate-relative offset is shifted left of where it'd be in the
    original text) must still remap to the correct original position --
    this is the case an off-by-a-constant-everywhere bug would miss,
    since it specifically requires per-segment offsets, not one global
    shift."""
    text = "SSNTargetSSN=123-45-6789 TargetUserName=Timothy Wong"
    regex_hits = detect.scan_regex(text)
    candidate, segments = evaluate._build_ner_candidate(text, "windows_event", regex_hits)

    idx = candidate.index("Timothy Wong")
    hit = {"type": "PERSON", "start": idx, "end": idx + len("Timothy Wong"), "method": "ner"}
    remapped = evaluate._remap_hit(hit, segments)

    assert text[remapped["start"]:remapped["end"]] == "Timothy Wong"


def test_run_evaluation_field_gate_calls_ner_on_excised_text_and_remaps_correctly(monkeypatch):
    """End-to-end through run_evaluation: confirms the field-gated branch
    reaches detect.scan_ner with the excised (shorter, not the original,
    not skipped) text, and that the resulting hit's offsets -- reported
    relative to the excised candidate -- get correctly remapped back to
    the ORIGINAL log line's coordinates before being scored against gold
    spans."""
    calls = []

    def fake_scan_ner(text, min_score=0.5):
        calls.append(text)
        if "Timothy Wong" in text:
            start = text.index("Timothy Wong")
            return [{"type": "PERSON", "start": start, "end": start + len("Timothy Wong"),
                      "method": "ner", "score": 0.9}]
        return []

    monkeypatch.setattr(detect, "scan_ner", fake_scan_ner)

    entries = [{
        "log": "TargetUserName=Timothy Wong TargetSSN=123-45-6789",
        "log_type": "windows_event",
        "pii": [
            {"start": 15, "end": 28, "type": "PERSON"},
            {"start": 39, "end": 50, "type": "SSN"},
        ],
    }]

    per_type, _ = evaluate.run_evaluation(entries, use_ner=True, use_field_gate=True)

    assert len(calls) == 1, "NER should be called exactly once, on the excised candidate"
    assert "123-45-6789" not in calls[0]
    assert "Timothy Wong" in calls[0]
    assert len(calls[0]) < len("TargetUserName=Timothy Wong TargetSSN=123-45-6789")
    # PERSON must be found at the ORIGINAL text's coordinates (gold span
    # is [15, 28) in "TargetUserName=Timothy Wong TargetSSN=..."), which
    # only works if _remap_hit's offset translation is correct -- a bug
    # there would silently produce a wrong-offset prediction that fails
    # to overlap the gold span, showing up as tp=0, fn=1 instead.
    assert per_type["PERSON"]["tp"] == 1
    assert per_type["PERSON"]["fn"] == 0


def test_run_evaluation_field_gate_skips_ner_when_fully_covered(monkeypatch):
    """The other half of the claim: when every alphabetic field value is
    regex-covered, run_evaluation must not call scan_ner at all -- this
    is the ONLY mechanism by which any throughput benefit over naive can
    come from this strategy (see src/evaluate.py's own docstring on why
    it's measured to fire rarely on lines that actually contain a
    PERSON, and often on lines that don't)."""
    calls = []
    monkeypatch.setattr(detect, "scan_ner", lambda text, min_score=0.5: calls.append(text) or [])

    entries = [{
        "log": "SRC=10.0.0.5 DST=10.0.0.9",
        "log_type": "windows_event",
        "pii": [],
    }]

    evaluate.run_evaluation(entries, use_ner=True, use_field_gate=True)

    assert calls == [], "NER must not be called when nothing unexcised remains"
