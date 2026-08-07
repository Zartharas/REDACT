# REDACT — Improvement Roadmap

Prioritized next steps for the product, split by commercial (production-readiness) and research (measurement/validity) value. Written 2026-08-07 after adding the flattened-username detection layer (`src/flattened_names.py`) and auditing the current codebase state — see `PROJECT_STATUS.md` for the session log this came out of, and `BUGS_AND_FIXES.md` for everything already found and fixed.

## Immediate (no new environment needed)

1. **DONE (2026-08-07).** Closed the Bug 9 corpus-labeling gap (`BUGS_AND_FIXES.md`): `render()` now uses `re.finditer` instead of `text.find`, the canonical corpus was regenerated (6,199 → 6,537 gold spans, +338 exactly matching the affected `sudo` entries), and every number re-derivable without NER was re-verified (flattened-layer FPs 171→0, recall holds at 50.3% on the corrected denominator, regex-only precision/recall re-run). Numbers that depend on NER are flagged stale in README/BUGS_AND_FIXES.md pending a rerun in a spaCy-capable environment — see item 3 below, now the more specific blocker.
2. Commit and push is no longer blocked — the working tree matches `origin/main` as of this session (confirmed via `git fetch` + `git rev-list --left-right --count HEAD...origin/main` = `0 0`); the prior `.git/index.lock` issue from the previous session already resolved itself.

## Needs a Docker + spaCy-capable environment (not available in this sandbox)

3. **Run the combined NER+flattened-layer evaluation** (`evaluate.py`'s new fourth condition) and the full `validate.py` 18-check suite with the new layer present, to confirm nothing regressed and to get the real combined-ensemble numbers rather than the standalone ones measured this session.
4. **Close Bug 6** (`TokenStore` concurrency lock — fix committed, not yet confirmed under real concurrent load): rerun the full Docker Compose stack per the exact steps already in `BUGS_AND_FIXES.md`.
5. **Validate the flattened-name layer against real data**, not just the Faker-derived synthetic corpus (`validation/real_data/`, the same Loghub datasets already used elsewhere in this project). The 50.3% recall number is honestly caveated as potentially optimistic since the name dictionary and the corpus's own name generator share a source — this is the check that answers whether the gain is real.

## Commercial / production-readiness

6. **Token store**: replace the flat-JSON `TokenStore` with an abstract `StorageProvider` interface, backed by Redis or HashiCorp Vault in production, file-backed only for local dev/testing. Currently the single biggest gap between "proof of concept" and "something you'd actually deploy," and it's explicitly flagged as such in the README's own limitations section.
7. **Service layer**: move off Flask's development server (`threaded=True` is documented in the code itself as a stopgap). A multi-process WSGI server (gunicorn, worker count matched to CPU cores) is the immediate fix; FastAPI/Uvicorn is a further-out rewrite, not a drop-in swap, and only worth it if async I/O actually becomes the bottleneck (right now the NER call is CPU-bound and GIL-serialized regardless of web framework).
8. **Drift detection coverage**: `drift.py` explicitly excludes syslog because there's no reliable field-boundary parser for it (`fields.py`'s stated scope). A syslog-specific field extractor is the concrete next step if a production deployment logs meaningful PII through syslog sources.
9. **Load testing beyond demo scale**: everything verified so far (Docker Compose end-to-end, the 9,984+16=10,000 reconciliation) is single-node, single-shard, one Docker Desktop machine, 10,000 lines. None of it has been tested at anything resembling production log volume (terabytes/day). This matters before any claim about "runs at scale."

## Research value

10. **Characterize the flattened-name layer's precision on a larger, non-Faker name corpus** before citing the 50.3% number as generalizable — swap in a real-world name-frequency list (e.g. US Census given/surname data, or a locale-appropriate list) and re-measure, per the caveat already stated in `flattened_names.py`'s own docstring.
11. **Entropy layer**: already honestly characterized as weak on this dataset's structured fields (2.3% unique recall, 34.8% false-alarm rate at the most permissive threshold tested) — but the dataset doesn't include the category entropy detection is actually built for (API keys, session tokens, opaque hashes). Worth a dedicated small corpus of exactly that category to give the entropy layer a fair test before drawing a final conclusion about it.
