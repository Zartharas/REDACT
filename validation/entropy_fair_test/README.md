# Entropy layer, tested against its actual intended use case

The main synthetic corpus (`data/synthetic_logs.jsonl`) doesn't contain API
keys, session tokens, or opaque hashes — the category `scan_entropy()`
(`src/detect.py`) is actually built for. Measured against that corpus, the
entropy layer looks weak (README.md's main measurement section: 2.3% unique
recall, 34.8% false-alarm rate at the most permissive threshold tested) —
but that's a measurement of the wrong target, honestly flagged as such at
the time rather than presented as a final verdict on entropy detection.

This directory is that fairer test (ROADMAP item 11).

```bash
python validation/entropy_fair_test/generate_secrets_corpus.py
python validation/entropy_fair_test/evaluate_entropy.py
```

`generate_secrets_corpus.py` builds a small (2,000-entry) corpus of
realistic log lines carrying genuinely secret-shaped tokens (JWT-style
bearer tokens, AWS-style access/secret key pairs, GitHub-style PATs,
session cookies, SHA-256 hashes) alongside clean lines carrying
long-but-not-secret tokens — including UUIDs deliberately, since a UUIDv4's
fixed hyphen positions and constrained version/variant nibbles make it
look random at a glance while carrying meaningfully less entropy per
character than a true secret. All tokens are generated with `Random(42)`
from `random`/`string` — no real credentials, no real service, nothing
that could accidentally be a working secret.

## Results (2,000 entries, 982 with a real secret, 1,018 clean)

| min_len | threshold | Precision | Recall | F1 | False-alarm rate |
|---|---|---|---|---|---|
| 12 | 3.3 | 0.470 | 1.000 | 0.640 | 62.3% |
| 12 | 3.6 | 0.553 | 1.000 | 0.712 | 54.3% |
| 12 | 3.9 | 0.633 | 0.883 | 0.738 | 37.2% |
| **12** | **4.2** | **0.811** | **0.812** | **0.811** | **9.7%** |
| 20 | 3.6 | 0.641 | 1.000 | 0.781 | 42.0% |

Full sweep (12 combinations) in the script's own output.

**On the correct target, the entropy layer performs respectably** — best
F1 of 0.811 at `min_len=12, threshold=4.2` (precision 0.811, recall 0.812,
9.7% false-alarm rate), a large gap from the 2.3%-recall/34.8%-false-alarm
characterization on the main corpus. This doesn't mean the main corpus's
number was wrong — it was an honest measurement of a real thing (entropy
detection against structured, non-secret log fields), just not the thing
this layer is designed to catch.

**The remaining false positives at the best threshold are concentrated
almost entirely in UUIDs** embedded in URLs and query parameters
(`/api/v1/orders/06d7e805-...`, `request_id=06d7e805-...`) — confirmed by
direct inspection of the flagged tokens, not assumed. This is the
deliberately hard negative case this corpus was built to include, and it's
a genuine, still-open false-positive source: a UUID-shape exclusion (fixed
hyphen positions, version/variant nibble check) would likely improve
precision further without hurting recall on real secrets, but that
refinement hasn't been built or measured — noted as a concrete next step,
not implemented here.

**What this test doesn't establish:** recall/precision on any secret
format not included above (e.g. UUIDv4-shaped API keys some services
actually use, which would collide directly with the false-positive case
just described), or on real production log volume and format diversity —
this is a small, hand-built corpus of common shapes, the same honesty
standard the main synthetic corpus is held to.
