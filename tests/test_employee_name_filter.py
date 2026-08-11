"""
Tests src/employee_name_filter.py's HashedNameFilter -- the peppered
Bloom filter for employee-name matching (BUGS_AND_FIXES.md, "Engineering
upgrade 5" / ROADMAP.md item 13).

All deterministic, no external dependency (no Redis, no live AD/Okta
export, no NER model) -- the hashing/bit-array logic is fully testable
on its own, unlike the parts of this project that need a live spaCy/
Presidio model or a Docker daemon.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import pytest  # noqa: E402
from employee_name_filter import HashedNameFilter, build_from_names_file  # noqa: E402

PEPPER_A = b"test-pepper-a-do-not-use-in-prod"
PEPPER_B = b"test-pepper-b-different-value"


def test_rejects_empty_pepper():
    with pytest.raises(ValueError):
        HashedNameFilter(pepper=b"", capacity=100)


def test_no_false_negatives_across_many_names():
    """The one guarantee a Bloom filter must never break: every name that
    was actually added must always test as a match. Checked across 500
    names, not just a couple of hand-picked examples."""
    names = [f"person{i} lastname{i}" for i in range(500)]
    filt = HashedNameFilter(pepper=PEPPER_A, capacity=len(names), error_rate=0.01)
    for name in names:
        filt.add(name)
    for name in names:
        assert filt.might_contain(name) is True, f"false negative on {name!r}"


def test_names_not_added_are_usually_absent():
    """Not a hard guarantee (false positives are allowed by design -- see
    module docstring), but the measured false-positive rate on names that
    were genuinely never added should stay close to the configured
    error_rate, not be wildly off (which would indicate a real bug in the
    hashing/sizing, not just Bloom-filter noise)."""
    added = [f"person{i} lastname{i}" for i in range(1000)]
    filt = HashedNameFilter(pepper=PEPPER_A, capacity=len(added), error_rate=0.01)
    for name in added:
        filt.add(name)

    not_added = [f"nobody{i} nowhere{i}" for i in range(2000)]
    false_positives = sum(1 for name in not_added if filt.might_contain(name))
    fp_rate = false_positives / len(not_added)
    # Generous bound (5x target) -- this is a statistical property, not
    # an exact one, and this test must not be flaky across runs.
    assert fp_rate < 0.05, f"false-positive rate {fp_rate:.4f} far exceeds target 0.01"


def test_different_pepper_produces_different_membership():
    """This IS the actual security property this class exists for --
    confirmed directly, not just argued in the docstring. A filter built
    with one pepper must not report the same names as present when
    queried (hypothetically) with a different pepper -- an attacker
    without the real pepper gets no useful signal."""
    names = ["Alice Johnson", "Bob Smith", "Carol Lee"]
    filt_a = HashedNameFilter(pepper=PEPPER_A, capacity=len(names))
    for name in names:
        filt_a.add(name)

    # Build a second filter with the SAME names but a DIFFERENT pepper --
    # simulates an attacker who has the algorithm and a guessed name list
    # but not the real pepper, checking whether their own filter's bit
    # pattern would match the real one's.
    filt_b = HashedNameFilter(pepper=PEPPER_B, capacity=len(names))
    for name in names:
        filt_b.add(name)

    assert bytes(filt_a.bits) != bytes(filt_b.bits), (
        "same names under different peppers produced identical bit arrays -- "
        "this would mean the pepper isn't actually affecting the hash, "
        "defeating the entire point of this class"
    )


def test_normalization_is_consistent_between_add_and_query():
    """Case/whitespace differences at insert time vs query time must not
    cause a false negative -- a real deployment's log text and its AD/
    Okta export will not always agree on capitalization/spacing."""
    filt = HashedNameFilter(pepper=PEPPER_A, capacity=10)
    filt.add("  Donald   Garcia  ")
    assert filt.might_contain("donald garcia") is True
    assert filt.might_contain("DONALD GARCIA") is True
    assert filt.might_contain("Donald Garcia") is True


def test_save_and_load_roundtrip(tmp_path):
    names = ["Alice Johnson", "Bob Smith"]
    filt = HashedNameFilter(pepper=PEPPER_A, capacity=len(names))
    for name in names:
        filt.add(name)

    path = str(tmp_path / "filter.bin")
    filt.save(path)

    loaded = HashedNameFilter.load(path, pepper=PEPPER_A)
    for name in names:
        assert loaded.might_contain(name) is True

    # Loading with the WRONG pepper must not silently work -- membership
    # results should be garbage (effectively random), not the real answer.
    loaded_wrong_pepper = HashedNameFilter.load(path, pepper=PEPPER_B)
    # At least one of these should now report absent -- if the wrong
    # pepper still finds all of them, something is broken (pepper isn't
    # actually being used in the hash at query time).
    still_all_present = all(loaded_wrong_pepper.might_contain(n) for n in names)
    assert not still_all_present, (
        "loading with the wrong pepper still found all original names -- "
        "the pepper isn't actually gating membership queries"
    )


def test_save_never_persists_the_pepper(tmp_path):
    """Confirms the on-disk format genuinely doesn't leak the pepper --
    checked directly against the saved file's bytes, not just trusting
    save()'s docstring."""
    filt = HashedNameFilter(pepper=PEPPER_A, capacity=10)
    filt.add("Alice Johnson")
    path = str(tmp_path / "filter.bin")
    filt.save(path)

    with open(path, "rb") as f:
        raw = f.read()
    assert PEPPER_A not in raw


def test_build_from_names_file(tmp_path):
    names_file = tmp_path / "names.txt"
    names_file.write_text("Alice Johnson\nBob Smith\n\nCarol Lee\n")

    filt = build_from_names_file(str(names_file), pepper=PEPPER_A)
    assert filt._count == 3  # blank line correctly skipped
    assert filt.might_contain("Alice Johnson") is True
    assert filt.might_contain("Bob Smith") is True
    assert filt.might_contain("Carol Lee") is True
    assert filt.might_contain("Nobody Nowhere") is False
