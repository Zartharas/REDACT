"""
Tests detect.py's _could_contain_ner_entity() early-exit gate and its
wiring into scan_ner() (added 2026-08-11, BUGS_AND_FIXES.md "Engineering
upgrade 3").

Cannot exercise the real spaCy/Presidio model in this environment (no
network access to fetch it here, same standing constraint elsewhere in
this project) -- these tests verify the gate's own boolean logic
directly, and that scan_ner() returns [] without ever calling
_get_analyzer() when the gate fires (confirmed via monkeypatch, not
assumed). The corpus-wide ground-truth safety check and the honest
near-zero-trigger-rate finding on realistic text both live in
validation/early_exit_gate_verify.py, not here -- this file is unit-level,
that one is corpus-level.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import detect  # noqa: E402


def test_gate_fires_on_truly_empty_content():
    assert detect._could_contain_ner_entity("") is False
    assert detect._could_contain_ner_entity("   ") is False
    assert detect._could_contain_ner_entity("---") is False
    assert detect._could_contain_ner_entity(".") is False
    assert detect._could_contain_ner_entity("a b c") is False  # single-char tokens only


def test_gate_does_not_fire_on_any_letter_run():
    # Even ordinary words -- not just names -- keep the gate from firing,
    # since the gate can't distinguish "could be a name" from "is a word"
    # without a live model to verify a more aggressive rule against (see
    # detect.py's own comment on why this is intentionally conservative).
    assert detect._could_contain_ner_entity("heartbeat ok") is True
    assert detect._could_contain_ner_entity("PING") is True
    assert detect._could_contain_ner_entity("hello") is True


def test_gate_does_not_fire_on_any_digit_run():
    assert detect._could_contain_ner_entity("42") is True
    assert detect._could_contain_ner_entity("---123---") is True


def test_gate_does_not_fire_on_real_pii_shapes():
    assert detect._could_contain_ner_entity("donaldgarcia") is True
    assert detect._could_contain_ner_entity("239-65-9864") is True
    assert detect._could_contain_ner_entity("196.89.6.126") is True
    assert detect._could_contain_ner_entity("user@example.com") is True


def test_scan_ner_skips_analyzer_call_when_gate_fires(monkeypatch):
    """Confirms the actual mechanism -- scan_ner() must return [] AND
    never touch _get_analyzer() (the expensive spaCy/Presidio load) when
    the gate fires. Not just checking the return value; checking that the
    expensive path was genuinely skipped."""
    called = {"analyzer": False}

    def _fake_get_analyzer():
        called["analyzer"] = True
        raise AssertionError("analyzer should not be constructed when the gate fires")

    monkeypatch.setattr(detect, "_get_analyzer", _fake_get_analyzer)

    result = detect.scan_ner("   ---   ")
    assert result == []
    assert called["analyzer"] is False


def test_scan_ner_still_calls_analyzer_when_gate_does_not_fire(monkeypatch):
    """The inverse check -- confirms this isn't accidentally short-circuiting
    every call. When the gate would NOT fire, scan_ner() must still reach
    the analyzer path (verified by making the fake raise, so a pass here
    means the code reached that point, not that it was skipped)."""
    def _fake_get_analyzer():
        raise RuntimeError("reached analyzer construction, as expected")

    monkeypatch.setattr(detect, "_get_analyzer", _fake_get_analyzer)

    try:
        detect.scan_ner("Contact John Smith about ticket 4521")
        raised = False
    except RuntimeError as e:
        raised = "reached analyzer construction" in str(e)

    assert raised is True
