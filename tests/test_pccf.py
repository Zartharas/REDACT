"""Unit tests for src/pccf.py (pure-Python, no models, no network)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import pccf  # noqa: E402


def _h(s, e, conf=None):
    h = {"type": "PERSON", "start": s, "end": e}
    if conf is not None:
        h["confidence"] = conf
    return h


def test_conformal_quantile_index_and_inf():
    assert pccf.conformal_quantile(list(range(10)), 0.10) == 9
    assert pccf.conformal_quantile(list(range(5)), 0.10) == float("inf")  # too few points
    assert pccf.conformal_quantile([], 0.10) == float("inf")


def test_clopper_pearson_known_value():
    # one-sided 95% lower bound for 8/10 is 0.4931
    assert abs(pccf.clopper_pearson_lower(8, 10, 0.05) - 0.4931) < 1e-3
    assert pccf.clopper_pearson_lower(0, 10, 0.05) == 0.0


def test_candidates_merge_transitive_overlap_and_mask():
    c = pccf.build_candidates({"dict": [_h(0, 5)], "ner": [_h(3, 9, 0.7), _h(20, 25, 0.2)]})
    assert [(x.start, x.end) for x in c] == [(0, 9), (20, 25)]
    assert c[0].mask == frozenset({"dict", "ner"})
    assert abs(pccf.joint_score(c[0]) - 0.3) < 1e-9  # max(r_dict=0, r_ner=0.3)


def test_unseen_pattern_fails_open():
    m = pccf.PCCF(mode="coverage", alpha=0.1)
    m.fit(pccf.build_candidates({"dict": [_h(0, 3)]}), [True])
    unseen = pccf.build_candidates({"ner": [_h(0, 3, 0.1)]})[0]
    assert m.accept(unseen)


def test_precision_mode_drops_low_precision_pattern():
    cands, labels = [], []
    for i in range(200):  # pattern {dict}: 10% true
        cands += pccf.build_candidates({"dict": [_h(i * 10, i * 10 + 3)]})
        labels.append(i % 10 == 0)
    for i in range(200):  # pattern {dict, ner}: 95% true
        cands += pccf.build_candidates({"dict": [_h(5000 + i * 10, 5000 + i * 10 + 3)],
                                        "ner": [_h(5000 + i * 10, 5000 + i * 10 + 3, 0.9)]})
        labels.append(i % 20 != 0)
    m = pccf.PCCF(mode="precision", target_precision=0.8, delta=0.05).fit(cands, labels)
    assert m.thresholds[frozenset({"dict"})] == float("-inf")
    assert m.thresholds[frozenset({"dict", "ner"})] > 0


def test_global_partition_uses_one_threshold():
    cands = pccf.build_candidates({"dict": [_h(0, 3)], "ner": [_h(10, 13, 0.9)]})
    m = pccf.PCCF(mode="coverage", alpha=0.5, partition="global").fit(cands, [True, True])
    assert list(m.thresholds) == [pccf.PCCF.GLOBAL]
    assert all(m.accept(c) for c in cands)


def test_bonferroni_search_picks_loosest_passing_threshold():
    cands, labels = [], []
    for i in range(300):
        conf = 0.99 if i < 200 else 0.6   # r = 0.01 (all true) vs r = 0.4 (all false)
        cands += pccf.build_candidates({"ner": [_h(i * 10, i * 10 + 3, conf)]})
        labels.append(i < 200)
    m = pccf.PCCF(mode="precision", target_precision=0.9, delta=0.05, partition="global",
                  search="bonferroni").fit(cands, labels)
    q = m.thresholds[pccf.PCCF.GLOBAL]
    assert 0.01 <= q < 0.4 + 1e-9 and q < 0.4  # never admits the r=0.4 block


def test_callable_partition_groups_and_fails_open():
    cands = pccf.build_candidates({"ner": [_h(0, 3, 0.9), _h(10, 13, 0.2)]})
    grp = lambda c: ("A" if c.start < 5 else "B")  # noqa: E731
    m = pccf.PCCF(mode="coverage", alpha=0.5, partition=grp).fit(cands[:1], [True])
    assert set(m.thresholds) == {"A"}
    assert m.accept(cands[1])  # group B unseen -> fail open


def test_small_group_fallback_uses_global_threshold():
    cands, labels = [], []
    for i in range(40):  # big group {ner}: r spread 0..0.39, all true
        cands += pccf.build_candidates({"ner": [_h(i * 10, i * 10 + 3, 1 - i / 100)]})
        labels.append(True)
    small = pccf.build_candidates({"dict": [_h(5000, 5003)]})
    cands += small
    labels.append(True)
    m = pccf.PCCF(mode="coverage", alpha=0.1, min_group_true=19).fit(cands, labels)
    assert m.stats[frozenset({"dict"})]["fallback"] is True
    assert m.stats[frozenset({"ner"})]["fallback"] is False
    assert m.thresholds[frozenset({"dict"})] == pccf.conformal_quantile([pccf.joint_score(c) for c in cands], 0.1)


def test_small_group_fallback_keep_fails_open():
    cands, labels = [], []
    for i in range(40):
        cands += pccf.build_candidates({"ner": [_h(i * 10, i * 10 + 3, 1 - i / 100)]})
        labels.append(True)
    cands += pccf.build_candidates({"dict": [_h(5000, 5003)]})
    labels.append(True)
    m = pccf.PCCF(mode="coverage", alpha=0.1, min_group_true=19, fallback="keep").fit(cands, labels)
    assert m.thresholds[frozenset({"dict"})] == float("inf")
