"""
PCCF -- Presence-Conditioned Conformal Fusion (feasibility prototype).

Implements the decision rule designed in ALGORITHM_DESIGN.md Section 6, so
that its feasibility check (Section 7) can be run against real data before
anything is built for French/Russian. Research prototype: NOT wired into
detect.py, pipeline.py, or any {lang}_detect.py / {lang}_ner.py file, and
nothing in the production path imports it.

ADDITIVE, SAME DISCIPLINE AS es_detect.py / fr_detect.py / ru_detect.py:
this module never calls a detector itself and imports no model. It takes
hits that the existing, unmodified layers already produced, groups them into
candidate spans, and decides which candidates to keep. Swapping naive union
for PCCF is therefore a harness-level choice, reversible by not calling it.

WHAT IT COMPUTES (mapped to ALGORITHM_DESIGN.md Section 6):

  6.1 setup      build_candidates(): same-type hits from K layers that
                 overlap (transitively) become ONE candidate. The
                 candidate's presence mask S(x) is the set of layers with a
                 member in it. Each layer's nonconformity is
                 r_k = 1 - confidence_k, where confidence comes from the
                 hit's optional "confidence" key (default 1.0 = a binary
                 layer that either fires or doesn't).
  6.2 calibrate  PCCF.fit(): Mondrian calibration keyed on the presence
                 mask, on the PASC-style joint score R(x) = max_k r_k(x).
                 nested=True adds the CP-MDA-Nested-style augmentation: a
                 calibration candidate whose mask is a superset of s is
                 also counted toward s, rescored with only s's layers.
  6.3 infer      PCCF.accept(): keep x iff R(x) <= q_{S(x)}.

TWO DECISION MODES, because the feasibility check showed they answer
different questions and the design text had merged them:

  mode="coverage"  (the rule as written in Section 6.2). q_s is the
      conformal (1-alpha) quantile of R over TRUE-PII calibration
      candidates with mask s. What that guarantees is
          Pr(x kept | x is real PII, S(x)=s) >= 1-alpha
      i.e. a per-pattern RECALL (miss-rate) floor. It says nothing about
      precision. It can only raise precision if R varies inside a pattern
      and the low-R candidates are the true ones.

  mode="precision" (Learn-then-Test-style, added by this prototype). For
      each pattern, thresholds from a fixed grid are tested from strictest
      to loosest (fixed-sequence testing). A threshold passes when the
      one-sided Clopper-Pearson lower bound on precision among the kept
      calibration candidates is >= target_precision, at level
      delta / (#patterns) (Bonferroni across patterns). The loosest passing
      threshold is used. A pattern where nothing passes is dropped.
      Target: per-pattern precision >= target with prob >= 1-delta.
      Caveat: the bound treats kept candidates as i.i.d. Bernoulli draws,
      which holds approximately for document-level exchangeable data.
      Documents are exchangeable here; the spans inside one document are
      not strictly independent.

PARTITION: partition="mask" (default) is the Mondrian rule above.
partition="global" pools every candidate into one group. It was added in
phase 2 (PCCF_PHASE2_RESULTS.md): the per-pattern precision certificate
turned out too data-hungry for the small single-layer pools, while a
single global certificate was not.

SEARCH (precision mode): search="fixed_sequence" (default) is described
above. search="bonferroni" tests every threshold in an absolute grid
(R <= 0.01, 0.02, ..., 0.50 by default) at level delta/(|grid|*|groups|)
and keeps the loosest one that passes. This is the construction
pre-registered for phase 3 (PCCF_PHASE3_PREREGISTRATION.md, T5).

partition may also be a callable cand -> hashable group key (phase 3, T7:
presence mask x field-key status). Unseen groups fail open, as below.

SMALL-GROUP FALLBACK (phase 4, F1): with min_group_true=k, any group whose
calibration set has fewer than k true candidates uses the threshold fitted
on ALL calibration candidates pooled. Its guarantee is then marginal, not
per-group, and summary() records "fallback": True for that group. The
default (None) keeps the earlier behaviour. fallback="keep" (phase 5)
makes such groups fail OPEN (keep every candidate) instead of borrowing
the pooled threshold. Phase 4 showed that borrowing can cut minority-group
recall badly (French {dict}: 0.33); fail-open is the recall-safe choice
for the redaction tier.

UNSEEN PATTERNS AT INFERENCE fail OPEN (the candidate is kept), because in
a PII pipeline an unseen layer combination should not silently turn into a
miss. This is a deliberate choice, recorded in summary().

No third-party dependencies (the Clopper-Pearson bound is computed with
math.lgamma plus bisection), so the module runs in this project's
network-restricted sandbox.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from itertools import combinations

INF = float("inf")


# --------------------------------------------------------------------------
# Candidates
# --------------------------------------------------------------------------
@dataclass
class Candidate:
    etype: str
    start: int
    end: int
    members: list = field(default_factory=list)  # (layer, hit) pairs
    scores: dict = field(default_factory=dict)   # layer -> r_k in [0, 1]

    @property
    def mask(self) -> frozenset:
        return frozenset(self.scores)


def build_candidates(layer_hits: dict, etype: str | None = None) -> list:
    """layer_hits: {layer_name: [hit, ...]} where each hit uses the
    project-wide shape {"type", "start", "end", ...} and may carry an
    optional "confidence" in [0, 1]. Same-type hits that overlap, directly
    or through a chain, are merged into one Candidate. If a layer has
    several members in one candidate, its score is the most confident
    (lowest r) member."""
    flat = []
    for layer, hits in layer_hits.items():
        for h in hits:
            if etype is not None and h["type"] != etype:
                continue
            flat.append((layer, h))
    flat.sort(key=lambda lh: (lh[1]["type"], lh[1]["start"], lh[1]["end"]))

    cands = []
    for layer, h in flat:
        cur = cands[-1] if cands else None
        if cur is not None and cur.etype == h["type"] and h["start"] < cur.end:
            cur.end = max(cur.end, h["end"])
        else:
            cur = Candidate(etype=h["type"], start=h["start"], end=h["end"])
            cands.append(cur)
        cur.members.append((layer, h))
        r = 1.0 - float(h.get("confidence", 1.0))
        cur.scores[layer] = min(cur.scores.get(layer, INF), r)
    return cands


def joint_score(cand: Candidate, layers=None) -> float:
    """PASC-style joint nonconformity R = max over the present layers
    (restricted to `layers` when given, for nested masking)."""
    vals = [r for k, r in cand.scores.items() if layers is None or k in layers]
    return max(vals) if vals else INF


# --------------------------------------------------------------------------
# Statistics helpers (dependency-free)
# --------------------------------------------------------------------------
def conformal_quantile(values, alpha: float) -> float:
    """Split-conformal quantile: the ceil((n+1)(1-alpha))-th smallest
    value, or +inf if that index is above n (too few points to certify
    anything below 'keep everything')."""
    n = len(values)
    if n == 0:
        return INF
    k = math.ceil((n + 1) * (1 - alpha))
    if k > n:
        return INF
    return sorted(values)[k - 1]


def _log_binom_upper_tail(k: int, n: int, p: float) -> float:
    """log P(X >= k), X ~ Binomial(n, p)."""
    if k <= 0:
        return 0.0
    if p <= 0.0:
        return -INF
    if p >= 1.0:
        return 0.0
    lp, lq = math.log(p), math.log1p(-p)
    lc = math.lgamma(n + 1)
    terms = [lc - math.lgamma(i + 1) - math.lgamma(n - i + 1) + i * lp + (n - i) * lq
             for i in range(k, n + 1)]
    m = max(terms)
    return m + math.log(sum(math.exp(t - m) for t in terms))


def clopper_pearson_lower(k: int, n: int, delta: float) -> float:
    """One-sided (1-delta) lower confidence bound on a binomial proportion."""
    if n == 0 or k == 0:
        return 0.0
    target = math.log(delta)
    lo, hi = 0.0, k / n
    for _ in range(60):
        mid = (lo + hi) / 2
        if _log_binom_upper_tail(k, n, mid) > target:
            hi = mid
        else:
            lo = mid
    return lo


def _nonempty_subsets(mask: frozenset):
    items = sorted(mask)
    for r in range(1, len(items) + 1):
        for combo in combinations(items, r):
            yield frozenset(combo)


# --------------------------------------------------------------------------
# PCCF
# --------------------------------------------------------------------------
class PCCF:
    def __init__(self, mode: str = "coverage", alpha: float = 0.10, nested: bool = False,
                 target_precision: float | None = None, delta: float = 0.05, grid_size: int = 10,
                 partition: str = "mask", search: str = "fixed_sequence", abs_grid=None,
                 min_group_true: int | None = None, fallback: str = "global"):
        if mode not in ("coverage", "precision"):
            raise ValueError(mode)
        if not callable(partition) and partition not in ("mask", "global"):
            raise ValueError(partition)
        self.partition = partition
        if search not in ("fixed_sequence", "bonferroni"):
            raise ValueError(search)
        self.search = search
        self.abs_grid = abs_grid or [i / 100 for i in range(1, 51)]
        self.min_group_true = min_group_true
        if fallback not in ("global", "keep"):
            raise ValueError(fallback)
        self.fallback = fallback
        if mode == "precision" and target_precision is None:
            raise ValueError("precision mode needs target_precision")
        self.mode = mode
        self.alpha = alpha
        self.nested = nested
        self.target_precision = target_precision
        self.delta = delta
        self.grid_size = grid_size
        self.thresholds: dict = {}
        self.stats: dict = {}

    # -- calibration pools -------------------------------------------------
    GLOBAL = frozenset({"*"})

    def _key(self, cand):
        if callable(self.partition):
            return self.partition(cand)
        return self.GLOBAL if self.partition == "global" else cand.mask

    def _pools(self, candidates, labels):
        """pattern -> list of (R restricted to pattern, label)."""
        pools: dict = {}
        for c, y in zip(candidates, labels):
            if callable(self.partition) or self.partition == "global":
                pools.setdefault(self._key(c), []).append((joint_score(c), y))
            elif self.nested:
                for s in _nonempty_subsets(c.mask):
                    pools.setdefault(s, []).append((joint_score(c, s), y))
            else:
                pools.setdefault(c.mask, []).append((joint_score(c), y))
        return pools

    def fit(self, candidates, labels):
        pools = self._pools(candidates, labels)
        self.thresholds, self.stats = {}, {}
        n_patterns = max(len(pools), 1)
        global_q = None
        if self.min_group_true:
            allpool = [(joint_score(c), y) for c, y in zip(candidates, labels)]
            global_q = (conformal_quantile([r for r, y in allpool if y], self.alpha) if self.mode == "coverage"
                        else (self._fit_precision_bonferroni(allpool, self.delta) if self.search == "bonferroni"
                              else self._fit_precision(allpool, self.delta)))
        for s, pool in pools.items():
            n = len(pool)
            n_true = sum(1 for _, y in pool if y)
            if self.mode == "coverage":
                q = conformal_quantile([r for r, y in pool if y], self.alpha)
            else:
                q = (self._fit_precision_bonferroni(pool, self.delta / n_patterns)
                     if self.search == "bonferroni" else self._fit_precision(pool, self.delta / n_patterns))
            fallback = bool(self.min_group_true) and n_true < self.min_group_true
            if fallback:
                q = global_q if self.fallback == "global" else INF
            self.thresholds[s] = q
            kept = [(r, y) for r, y in pool if r <= q]
            self.stats[s] = {
                "n": n, "n_true": n_true,
                "calib_precision_all": n_true / n if n else 0.0,
                "threshold": q,
                "calib_kept": len(kept),
                "calib_precision_kept": (sum(y for _, y in kept) / len(kept)) if kept else None,
                "fallback": fallback,
            }
        return self

    def _fit_precision(self, pool, delta_s):
        rs = sorted(r for r, _ in pool)
        # Grid of candidate thresholds, strictest first; always ends at
        # "keep everything seen" (max R).
        grid = sorted({rs[min(len(rs) - 1, max(0, math.ceil(len(rs) * q / self.grid_size) - 1))]
                       for q in range(1, self.grid_size + 1)})
        best = -INF  # nothing passes -> drop the pattern
        for lam in grid:
            kept = [y for r, y in pool if r <= lam]
            lb = clopper_pearson_lower(sum(kept), len(kept), delta_s)
            if lb >= self.target_precision:
                best = lam
            else:
                break  # fixed-sequence testing: stop at first failure
        return best

    def _fit_precision_bonferroni(self, pool, delta_s):
        level = delta_s / len(self.abs_grid)
        best = -INF
        for lam in sorted(self.abs_grid):
            kept = [y for r, y in pool if r <= lam]
            if kept and clopper_pearson_lower(sum(kept), len(kept), level) >= self.target_precision:
                best = lam  # keep going: loosest passing threshold wins
        return best

    # -- inference ---------------------------------------------------------
    def accept(self, cand: Candidate) -> bool:
        q = self.thresholds.get(self._key(cand))
        if q is None:
            return True  # unseen pattern: fail open (see module docstring)
        return joint_score(cand) <= q

    def filter(self, candidates):
        return [c for c in candidates if self.accept(c)]

    def summary(self) -> dict:
        return {
            "mode": self.mode, "alpha": self.alpha, "nested": self.nested, "partition": self.partition if isinstance(self.partition, str) else "callable", "search": self.search,
            "target_precision": self.target_precision, "delta": self.delta,
            "unseen_pattern_policy": "fail-open (keep)",
            "patterns": {(("+".join(sorted(s))) if isinstance(s, frozenset) else str(s)): st
                         for s, st in self.stats.items()},
        }
