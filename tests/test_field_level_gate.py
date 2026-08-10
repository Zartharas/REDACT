"""
Tests the field-level NER gate (src/evaluate.py's _mask_regex_covered_fields
and the use_field_gate branch of run_evaluation) -- the engineering upgrade
meant to close the PERSON recall gap the whole-line tiered strategy has
(0.127 tiered vs. 0.404 naive, see README.md's Known Limitations section).

Cannot exercise this against the real spaCy/Presidio model in this
environment (no network access to fetch en_core_web_lg here, same
constraint noted throughout BUGS_AND_FIXES.md and tests/README.md).
Instead, monkeypatches detect.scan_ner to a stub that just records what
text it was called with, so these tests verify the actual claim that
matters mechanically: which characters get masked out before NER runs,
and when the NER call is skipped entirely vs. made. The real recall
numbers this is meant to improve need a live run of `python src/evaluate.py`
in an environment with the model available -- not claimed as already
measured here.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import detect  # noqa: E402
import evaluate  # noqa: E402


def test_mask_only_covers_regex_hit_fields_not_whole_line():
    """A windows_event line with an SSN in one field and a name in another:
    only the SSN field's characters should be masked, the name should
    survive untouched."""
    text = "TargetUserName=Timothy Wong TargetSSN=123-45-6789"
    regex_hits = detect.scan_regex(text)
    assert any(h["type"] == "SSN" for h in regex_hits)

    masked = evaluate._mask_regex_covered_fields(text, "windows_event", regex_hits)

    assert masked is not None
    assert "Timothy Wong" in masked, "name field must survive unmasked"
    assert "123-45-6789" not in masked, "regex-covered SSN field must be masked"
    assert len(masked) == len(text), "masking must preserve length so offsets stay valid"


def test_mask_returns_none_when_nothing_alphabetic_remains():
    """A line that's entirely numeric/structured fields, all regex-covered
    -- nothing left for NER to plausibly find, so the caller should skip
    the NER call entirely."""
    text = "SRC=10.0.0.5 DST=10.0.0.9"
    regex_hits = detect.scan_regex(text)
    assert any(h["type"] == "IP" for h in regex_hits)

    masked = evaluate._mask_regex_covered_fields(text, "windows_event", regex_hits)

    assert masked is None


def test_mask_falls_back_to_original_text_for_unrecognized_log_type():
    """No field structure known (e.g. log_type not in fields.py's coverage)
    -- must fall back to the original text, not silently skip NER. Falling
    back to the ORIGINAL text (not None) is the conservative, recall-safe
    default when field boundaries aren't known."""
    text = "some free text line with Timothy Wong in it, SSN 123-45-6789"
    regex_hits = detect.scan_regex(text)

    masked = evaluate._mask_regex_covered_fields(text, "some_unknown_type", regex_hits)

    assert masked == text


def test_run_evaluation_field_gate_calls_ner_on_masked_text(monkeypatch):
    """End-to-end through run_evaluation: confirms the field-gated branch
    actually reaches detect.scan_ner with the masked (not original, not
    skipped) text when a name field survives masking."""
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

    assert len(calls) == 1, "NER should be called exactly once, on the masked text"
    assert "123-45-6789" not in calls[0]
    assert "Timothy Wong" in calls[0]
    # PERSON should be found (tp=1, fn=0) since the name field wasn't masked
    assert per_type["PERSON"]["tp"] == 1
    assert per_type["PERSON"]["fn"] == 0


def test_run_evaluation_field_gate_skips_ner_when_fully_covered(monkeypatch):
    """The other half of the claim: when field-level masking leaves nothing
    alphabetic, run_evaluation must not call scan_ner at all -- this is
    where the throughput benefit over the naive (always-call) strategy
    comes from."""
    calls = []
    monkeypatch.setattr(detect, "scan_ner", lambda text, min_score=0.5: calls.append(text) or [])

    entries = [{
        "log": "SRC=10.0.0.5 DST=10.0.0.9",
        "log_type": "windows_event",
        "pii": [],
    }]

    evaluate.run_evaluation(entries, use_ner=True, use_field_gate=True)

    assert calls == [], "NER must not be called when nothing unmasked remains"
