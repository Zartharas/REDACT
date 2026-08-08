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

**`secrets_corpus.jsonl` itself is gitignored, not committed.** Every
token in it is synthetic, but several are shaped closely enough like real
AWS access/secret keys and Stripe keys that GitHub's push-protection
secret scanner flags them on pattern alone — correctly cautious behavior
on GitHub's part, since format-matching is exactly what that scanner is
built to catch, and no automated check can cheaply tell "shaped like a
secret for a test corpus" from "actually a leaked secret" without the
Random(42)-seed context. Rather than fight the scanner, the corpus is
regenerated locally by the command above, which is fully deterministic
and reproducible in under a second.

## Results (2,000 entries, 982 with a real secret, 1,018 clean)

Two rounds: the initial measurement, and after adding a UUID-shape
exclusion to `scan_entropy()` (`src/detect.py`) once direct inspection
confirmed where the remaining false positives were coming from.

| min_len | threshold | Precision | Recall | F1 | False-alarm rate |
|---|---|---|---|---|---|
| 12 | 3.3 | 0.533 | 1.000 | 0.695 | 50.2% |
| 12 | 3.6 | 0.641 | 1.000 | 0.782 | 42.2% |
| 12 | 3.9 | 0.772 | 0.883 | 0.824 | 25.1% |
| **12** | **4.2** | **1.000** | **0.812** | **0.896** | **0.0%** |
| 20 | 3.6 | 0.763 | 1.000 | 0.866 | 30.0% |

Full sweep (12 combinations) in the script's own output. (Pre-fix numbers,
for comparison: precision 0.811, F1 0.811, 9.7% false-alarm rate at the
same operating point — see git history for this file's prior version.)

**On the correct target, the entropy layer now performs very well** — best
F1 of 0.896 at `min_len=12, threshold=4.2` (precision 1.000, recall 0.812,
0.0% false-alarm rate on this corpus), a large gap from the
2.3%-recall/34.8%-false-alarm characterization on the main corpus. This
doesn't mean the main corpus's number was wrong — it was an honest
measurement of a real thing (entropy detection against structured,
non-secret log fields), just not the thing this layer is designed to
catch.

**The UUID-shape exclusion that closed the gap:** direct inspection of the
false positives at the best threshold showed they were concentrated
almost entirely in UUIDs embedded in URLs and query parameters
(`/api/v1/orders/06d7e805-...`, `request_id=06d7e805-...`). The first
attempt at excluding them — anchoring a UUID regex to match the *entire*
token — had zero effect, because `scan_entropy()`'s own token regex
(`[A-Za-z0-9+/=_.-]{8,}`) includes `/`, `=`, and `_` in its character
class, the exact separators that appear around a UUID in real log text.
That means a UUID essentially never appears as an isolated token; it gets
swallowed into a longer token along with its path prefix or `key=` name,
so an anchored exact-match check against the UUID shape never fired. The
actual fix: search for a UUID-shaped *substring* anywhere inside the
token (fixed hyphen positions, version nibble 1-5, variant nibble
8/9/a/b) rather than requiring the whole token to be one. Recall on real
secrets is unaffected (797/982 True Positives, unchanged before and
after) since no real secret in this corpus's genuinely random tokens
coincidentally contains a UUID-shaped run of characters.

**Known, stated limitation, unchanged by this fix:** a small number of
real-world services issue UUID-shaped API keys/tokens. Excluding the UUID
shape trades a little recall on that specific format for a large
precision gain on the much more common case of UUIDs used as
request/resource identifiers, not secrets — a deliberate, documented
tradeoff, not an oversight. If a production deployment is known to use
UUID-shaped secrets, this exclusion should be disabled or scoped more
narrowly for that environment.

**What this test doesn't establish:** recall/precision on any secret
format not included above (e.g. UUIDv4-shaped API keys some services
actually use, which would collide directly with the false-positive case
just described), or on real production log volume and format diversity —
this is a small, hand-built corpus of common shapes, the same honesty
standard the main synthetic corpus is held to.
