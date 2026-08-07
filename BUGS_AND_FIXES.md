# Known Issues and Fixes

A running record of bugs found during development and testing of the REDACT
pipeline (Logstash → `src/service.py` → OpenSearch), why each one mattered,
and how it was fixed. Kept here rather than only in commit messages because
several of these are the kind of failure mode ("looks like it's working,
actually silently destroying data") that's worth a permanent, findable
record — for this project and for anyone building something similar.

Status key: **Verified fixed** = confirmed by a completed clean-room
rebuild-and-rerun test after the fix. **Fix applied, verification pending** =
change made, not yet confirmed by a full end-to-end test.

---

## 1. PII leaking into container logs via a stray default pipeline

**Impact:** High. **Status:** Verified fixed.

The official Logstash image ships a default `logstash.conf` in its pipeline
directory. Because our own `redact-pipeline.conf` was added alongside it
rather than replacing it, Logstash ran both — including the default config's
behavior of echoing raw event content to stdout, which Docker captures as
container logs. Raw, un-anonymized log lines (i.e. the actual PII/PHI this
project exists to remove) were being written to `docker compose logs`
output, a location with no article of the framework's own access controls
applied to it.

**Fix:** `logstash/Dockerfile` now removes the default pipeline file at
build time (`RUN rm -f /usr/share/logstash/pipeline/logstash.conf`) so only
our own pipeline runs.

---

## 2. OpenSearch output duplicating documents on startup

**Impact:** Medium (data integrity / storage cost, not data loss).
**Status:** Verified fixed.

The `opensearch` output plugin's bulk-retry logic resubmits batches whose
first delivery may have already succeeded server-side but timed out
client-side — triggered here by Logstash starting to write before
OpenSearch was actually ready to accept connections. Without an explicit
`document_id`, each retry got a fresh random `_id`, producing genuine
duplicate documents. In one observed run this nearly doubled the anonymized
index (10,000 source lines → ~19,972 stored documents).

**Fix:** two parts, both needed.
- Added `document_id` (originally content-based, see Bug 4 below) to all
  three output blocks so retries overwrite rather than duplicate.
- Added Docker Compose `healthcheck` + `depends_on: condition:
  service_healthy` for `opensearch` and `redact-service`, so Logstash
  doesn't start writing until both are actually ready — fixing the race at
  its source rather than only patching around it.

---

## 3. Flask dev server timing out under concurrent load

**Impact:** Medium (real events misrouted to quarantine, not lost, but
mishandled). **Status:** Fix applied and confirmed to substantially reduce
the problem; a residual startup-only cluster of timeouts still occurs and is
not yet fully explained.

`src/service.py`'s Flask development server handles one request at a time
by default. Logstash's `http` filter is configured with `pipeline.workers
=> 8`, sending up to 8 concurrent `POST /anonymize` requests. Under load,
some requests exceeded the filter's timeout and were tagged
`_httprequestfailure`, which correctly (by design) routes them to
`sensitive_quarantine` rather than passing them through un-anonymized — but
that still means real, anonymizable events were needlessly quarantined
under normal operation.

**Fix:** `app.run(..., threaded=True)`. Documented in code as a stopgap, not
a production fix — the NER call inside `detect.detect_all()` is CPU-bound
and still serializes on Python's GIL, so `threaded=True` only buys
overlapping I/O. A production deployment should run this behind a
multi-process WSGI server (e.g. gunicorn, worker count matched to CPU
cores).

---

## 4. Content-based document IDs silently collapsing real events

**Impact:** Critical. **Status:** Verified fixed (root-caused twice; see
below).

This is the most serious bug found in this project so far, and it happened
in two stages.

**Stage 1 — root cause of Bug 2's original fix:** the `document_id` added to
fix Bug 2 was a SHA-256 fingerprint of `["message", "[log][file][path]"]`.
That correctly made retries of the *same* event idempotent, but real
security logs routinely contain the exact same line verbatim many times
(e.g. a fixed-field event like `EventID=4634 LogonType=3
TargetUserName=SYSTEM TargetDomainName=NT_AUTHORITY`, which has no variable
fields and recurs constantly). Every occurrence of identical text hashed to
the identical ID, so each new occurrence silently overwrote the previous one
in OpenSearch instead of being indexed as its own document. In one test run,
all 10,000 raw lines were confirmed processed by `redact-service`, but the
anonymized index topped out at only 579 stored documents — over 9,000
genuinely distinct security events were destroyed, not just deduplicated.
This is materially worse than the duplication it replaced: duplication
wastes storage, but collapsing repeated events destroys the volume/frequency
signal that is often the actual detection indicator (e.g. 200 identical
failed-logon lines in one minute is a brute-force indicator; collapsed to a
single document, that indicator is gone).

**Stage 1's attempted fix (superseded):** adding `@timestamp` to the
fingerprint source. This helped — 579 unique surviving documents became
984, then 1984, across successive test runs — but never approached the
expected ~9,984. Root cause of why it fell short: reading 10,000 lines off
local disk is fast enough that many lines land within the same
millisecond, and `@timestamp`'s resolution can't outrun that read speed —
so large batches of genuinely distinct events still shared an identical
timestamp and, combined with the low content diversity of realistic
security logs, still collided.

**Actual fix:** stopped deriving the ID from content or time at all. The
only property this ID ever needed was "retries of the same event keep the
same ID; two different events get different IDs" — it never needed to be
deterministic. Switched to `logstash-filter-uuid`, generating a random UUID
once per event, early in the pipeline. Retries resubmit the same in-memory
event object, so the UUID already written onto it survives unchanged
(idempotent); two distinct events get cryptographically independent UUIDs
regardless of timing or content similarity, eliminating collisions outright
rather than just making them less likely. Confirmed via live test: with
10,000 source lines and 24 quarantined, the anonymized index landed at
exactly 9,976 documents — an exact match.

**Compliance note:** this bug directly undermined the "faithfully preserves
security telemetry" claim this framework depends on. Any anonymization
pipeline that deduplicates by hashing content without also guaranteeing
per-occurrence uniqueness should be assumed to have this failure mode until
proven otherwise, particularly against boilerplate-heavy log sources
(Windows Security auditing, firewall accept/deny logs, health-check
endpoints).

---

## 5. Audit trail silently never wrote a single record

**Impact:** Critical. **Status:** Verified fixed.

Found while investigating why Bug 4's fix (random UUIDs) still showed
`docs.count == docs.deleted` on every run even after collisions were
provably eliminated. The pipeline runs under `pipeline.ecs_compatibility =>
v8` (Logstash's own startup log confirms this). Under ECS compatibility
mode, `logstash-filter-clone` does not set the legacy `[type]` field the way
the rest of this pipeline assumed — it tags the clone instead. The routing
logic (`if [type] == "audit_branch"`, used in both the `split` filter and
the output block) checked a field that was never actually being set, so it
never matched.

Confirmed two ways: `_cat/indices` never showed a `redact-audit-trail-*`
index at all, in any test run — the audit trail feature had never fired,
not once. And a sampled document pulled directly from the anonymized index
carried `tags: ["audit_branch"]`, proving the tag *was* being set reliably
while `[type]` was not.

Consequence: every audit-branch clone — generated for essentially every
event with detected PII, i.e. nearly all of them — silently fell through to
the `else` branch and was written into `security-logs-anonymized` using the
same `document_id` as its sibling original event, overwriting it in
OpenSearch. This is what produced the persistent `docs.count ==
docs.deleted` signature that outlasted the Bug 4 fix: it was never a retry
or collision problem at that point, it was dead routing logic silently
destroying half of every event's writes, and simultaneously meaning zero
audit records were ever produced.

**Fix:** route on `"audit_branch" in [tags]` instead of `[type] ==
"audit_branch"`, in both the split filter and the output block.

**Follow-on bug, found immediately after this fix:** switching to
tags-based routing exposed a second problem with `logstash-filter-clone`
itself — it was not deep-copying the `[tags]` array. Once the clone was
tagged `audit_branch`, that tag leaked back onto the *original* event too
(a shared/mutated array reference, not an independent copy), so the
original also matched `"audit_branch" in [tags]` in the output block and
was misrouted to `redact-audit-trail` instead of `security-logs-anonymized`.
Confirmed live: with 10,000 requests processed, `security-logs-anonymized`
landed at exactly 1,984 documents on two separate test runs against the
same static input dataset — consistent with only the zero-PII events (which
never trigger `clone` at all) surviving, while every PII-bearing original
was being diverted away. Fixed by abandoning `logstash-filter-clone`
entirely in favor of a `ruby` filter that fans out audit events manually
using `event.clone` (a real, independent deep copy at the JRuby level) and
tags only the new clone, never touching the original. Verified fixed: after
this change, `security-logs-anonymized` correctly reached the expected
9,984 documents.

**Compliance note, stated plainly:** an audit trail that silently never
writes anything is worse than no audit trail claim at all, because it
creates false assurance. If this framework's audit-trail component is ever
cited against NIST SP 800-53 audit-logging controls or GDPR Article 32, this
failure mode — and the fact that it was caught by manual document
inspection rather than by any automated check — should be disclosed
alongside the fix. A missing-audit-index check (e.g. an integration test
that asserts `redact-audit-trail-*` document count > 0 after a run with
known PII-bearing input) should be added so this class of bug can't recur
silently.

---

## 6. TokenStore race condition under concurrent access

**Impact:** Medium. **Status:** Fix committed (`src/anonymize.py`, part of
commit `db1fdae`); verified in isolated testing; **not yet confirmed
against a full Docker Compose end-to-end run.**

`src/anonymize.py`'s `TokenStore` performed dict read-modify-write
operations without synchronization. Under concurrent access (multiple
Logstash pipeline workers calling `/anonymize` simultaneously), this
produced `RuntimeError: dictionary changed size during iteration`.

**Fix:** wrapped the read-modify-write sequence in `threading.Lock()`.

**Verification gap, stated plainly:** this is the one fix on this list
that hasn't gone through the same end-to-end proof the other seven got.
The existing `validation/performance/docker_run.log` predates this fix
(it still shows the old default-pipeline/`elasticsearch` references from
before Bug 1's fix) and can't be used as evidence either way. To close
this out with the same rigor as the rest of this document, rerun the full
stack (`docker compose down -v && python src/export_raw_logs.py &&
docker compose up --build`) with this fix in place, then check the
`redact-service` container's own log output specifically, not just the
final OpenSearch counts:

```
docker compose logs redact-service | grep -i "RuntimeError\|dictionary changed size"
```

A clean run should return nothing. Zero hits, combined with the exact
9,984 + 16 = 10,000 reconciliation already established for the other
bugs, is what actually closes this item — not just the presence of the
`threading.Lock()` in the source.

---

## 7. Audit records overwriting each other under an unresolved field reference

**Impact:** Critical. **Status:** Verified fixed.

Found immediately after Bug 5's routing fix let audit events reach the
output block at all for the first time. The output block's document ID was
`document_id => "%{[audit_event][signature]}"`. `src/audit.py`'s
`build_audit_event()` has never produced a field called `signature` — its
actual fields are `field_type`, `method`, `policy_version`,
`original_value_fingerprint`, `timestamp`, and `authentication_tag`.
Logstash's `%{}` field reference does not raise an error when the referenced
field is missing; it silently leaves the literal unresolved string in
place. The practical effect: every audit event ever written by this
pipeline used the exact same document ID — the literal text
`%{[audit_event][signature]}` — each one silently overwriting the last.
Confirmed live: once Bug 5's fix let audit events flow, one test run showed
`redact-audit-trail` at `docs.count: 1, docs.deleted: 1286` — every one of
those 1,286 audit records had overwritten the previous one under the
identical literal ID.

**Fix:** reference the field that actually exists —
`document_id => "%{[audit_event][authentication_tag]}"`. The authentication
tag is an HMAC over the event's own content plus a timestamp, so it's unique
per event and safe to use as a document ID with the same idempotency
properties the pipeline's other document IDs rely on.

**Compliance note:** this is the second consecutive bug (after Bug 5) found
in the audit-trail component specifically, and both were invisible from the
outside — no error, no crash, `200 OK` on every request. An audit trail is
one of the few components in a compliance-oriented system where "silently
wrong" is arguably worse than "loudly broken," since it creates false
assurance that a record exists when it doesn't. This reinforces the
recommendation already noted under Bug 5: an automated post-run check
(audit-index document count roughly matching the count of anonymization
actions taken, not just "index exists and is non-empty") should be added
before this component is relied on for anything.

---

## 8. `_cat/indices` doc counts lagging behind the live searcher (a measurement pitfall, not a pipeline bug)

**Impact:** None to the pipeline itself — but nearly caused a false bug
report during verification, which is worth recording so it doesn't happen
again.

After fixing Bug 5's follow-on clone-tag-leak, `_cat/indices` continued to
show `security-logs-anonymized` capped at exactly 1,984 documents across
what looked like independent test runs, and `redact-audit-trail` capped at
1,090/197 (count/deleted). Both looked like the ceiling was real and the
fix hadn't worked. Querying the same indices with `_search` instead told a
different story: `security-logs-anonymized` actually held 9,984 documents
(the correct, expected count) and `redact-audit-trail` held 5,168 with zero
deletions. `_cat/indices`' `docs.count` reads from periodically-updated
cluster stats rather than the live searcher that `_search` hits in
near-real-time; under the write pressure of two busy indices sharing one
node, that stats snapshot lagged behind the actual committed state long
enough to look like a hard ceiling rather than a delay.

**Lesson:** when verifying document counts on a single-node, single-shard
OpenSearch instance under active write load, use `_search?size=0` (returns
`hits.total.value` from the live searcher) to get a trustworthy count, not
`_cat/indices`. This project's earlier "premature check" pattern (checking
`_cat/indices` while Logstash was still mid-run) and this staleness pattern
(checking it after the run finished, but too soon after the last write) are
two different failure modes that produce the same misleading symptom — an
artificially low count — and both should be ruled out with `_search` before
concluding a document count reveals a real bug.

---

## 9. Ground-truth generator silently under-labels a value that appears twice in one template

**Impact:** Low on its own (undercounts recall/inflates apparent false
positives slightly, on one template only), but worth recording because it
was discovered by a detector working correctly, not by a detector failing.
**Status:** Verified fixed (2026-08-07, source: `src/generate_logs.py`).

While building and measuring the new flattened-username layer (see below),
its false-positive rate looked alarming at first: 171 out of 1,013
predictions (83.1% precision) didn't match any gold `PERSON` span. Every
single one of those 171, on inspection, was the same root cause: the syslog
`sudo` template uses `{PERSON_name_flat}` twice --
`sudo[{pid}]: {PERSON_name_flat} : TTY=pts/0 ; PWD=/home/{PERSON_name_flat} ; ...`
-- and `generate_logs.py`'s `render()` locates each gold span with
`text.find(value)`, which only returns the *first* occurrence. The second,
identical occurrence (inside the `PWD=/home/...` path) is real PII, present
in the text, but never gets a gold-truth span. A detector accurate enough to
find both occurrences is then charged a false positive for correctly finding
the second one.

**Practical effect:** every recall/precision number computed against this
corpus for any detector that can find repeated values (this new layer, and
in principle Presidio's NER too, though NER's independent misses elsewhere
mask it) is very slightly pessimistic on precision and very slightly
pessimistic on recall for templates with a repeated slot. Only the `sudo`
template is affected; no other template in `generate_logs.py` reuses a PII
slot value twice.

**Fix, applied 2026-08-07:** `render()` now locates *all* occurrences of each
slot value via `re.finditer(re.escape(value), text)` instead of
`text.find(value)`, emitting one gold span per occurrence. The canonical
10,000-entry corpus (`data/synthetic_logs.jsonl`) was regenerated with the
same generation parameters (`--n 10000 --dirty-ratio 0.3`, fixed seed 42) —
entry count and entry text are bit-identical to before (confirmed: 10,000
entries both before and after), and gold PII span count went from 6,199 to
6,537, exactly +338, matching the count of `sudo`-template dirty entries
one-for-one (independently verified by counting them directly).

**Re-verification against the fixed corpus, this session:**
- The flattened-username layer's false positives, previously 171/1,013
  (83.1% precision) and suspected to be entirely this bug, are now
  confirmed to be **exactly 0** — precision is 100% with no caveat needed.
  Recall holds at the same 50.3%, now on the corrected denominator
  (1,010/2,006 vs. the earlier 839/1,668).
- `evaluate.py`'s regex-only condition (no NER, unaffected by the spaCy
  model-download limitation of this sandbox) was rerun: precision unchanged
  at 0.574, recall dropped from 0.572 to 0.542 — expected and mechanical,
  not a regression, since regex never detects PERSON at all and the fix
  only added PERSON gold spans, which purely increases the FN denominator.
- `src/analyze_entropy.py` was rerun and lands close to its prior number
  (33.9% clean-line false-alarm rate vs. 34.8% previously), consistent with
  this bug not touching which lines are clean.

**Closed out same day:** the NER-dependent reruns above were completed by
the user directly on their own machine (this sandbox still cannot reach
the spaCy model download — `raw.githubusercontent.com` and GitHub release
assets both return `403` through its proxy, confirmed again this session).
Results: the full `evaluate.py` ensemble table (all four conditions,
including the new flattened-layer combined run) and `validate.py`'s full
18-check suite (18/18 passed) were both re-run against the regenerated,
Bug-9-fixed corpus. The `98/1,668` pre-Layer-4 flattened-format baseline
was independently re-verified with a dedicated breakdown script
(`validation/breakdown_person_format.py`) and now reads `99/2,006` (4.9%)
on the corrected denominator — same finding, corrected number. See
`README.md`'s "What was actually measured" and Layer 4 sections for the
full updated tables.

---

## 10. Real-data evaluation script silently double-counted agreeing detections as false positives

**Impact:** Critical (measurement integrity, not a pipeline defect — but the
kind of silent-wrong-number failure this document exists to catch).
**Status:** Verified fixed, 2026-08-07, via a live before/after comparison
against all five Loghub datasets (source: `validation/real_data/inject_and_evaluate.py`).

Found while extending `inject_and_evaluate.py` to add a flattened-username-layer
condition (ROADMAP item 5). `evaluate()`'s matching loop combined
`detect.scan_regex()` and `detect.scan_ner()` predictions into one list and
matched them against gold spans one at a time, in order, marking each gold
span "matched" after the first prediction that hit it. Any *second*
prediction that correctly overlapped an **already-matched** gold span (e.g.
regex and NER both correctly flagging the same real IP address, which
happens on nearly every line with an IP) fell through to the `if not hit:
fp += 1` branch — a real, correct detection, from a second layer
independently agreeing with the first, counted as a false positive. This is
the exact class of bug `evaluate.py`'s own `run_evaluation()` already
guards against with an explicit dedup step; `inject_and_evaluate.py` never
had the equivalent.

**Effect, quantified:** estimated recovered false-positive counts (old
precision and TP held constant, solved for old FP, compared to the newly
measured FP with dedup in place):

| Dataset | Old reported precision | New precision (same TP, same recall) | Estimated old FP | New FP |
|---|---|---|---|---|
| OpenSSH | 0.507 | **0.974** | ~1,786 | 49 |
| Linux | 0.498 | **0.920** | ~1,413 | 122 |
| Thunderbird | 0.414 | **0.701** | ~914 | 276 |
| OpenStack | 0.497 | **0.989** | ~1,240 | 14 |
| Zookeeper | 0.340 | **0.476** | ~2,743 | 1,557 |

Recall is unaffected in every case (TP/FN counting was never wrong — only
FP was). This is not a small correction: on three of the five datasets,
corrected precision is now *higher* than the synthetic corpus's own
regex+NER precision (0.588), which directly contradicts this project's
previously stated finding that "precision is consistently lower than the
synthetic numbers alone would suggest" (`README.md`, "Does it hold up on
real data" section). That claim was true under the buggy measurement and is
no longer true under the corrected one — Zookeeper and, to a lesser extent,
Thunderbird still show real precision degradation (dominated by the same
private/internal-IP-range false positives documented in Finding 1 of the
main measurement section, a real detector limitation, not a script bug),
but OpenSSH, Linux, and OpenStack do not.

**Fix:** added the same prediction-dedup step `evaluate.py`'s
`run_evaluation()` already uses — before matching, drop any prediction
that overlaps an already-kept prediction of the same type — applied
uniformly whether or not the flattened layer is included.

**Compliance/integrity note, stated plainly:** this is the same failure
shape as bugs 1, 4, 5, and 7 above — no crash, no error, a plausible-looking
number that was silently wrong — just found in an evaluation script instead
of the production pipeline. It's flagged here with the same weight as those
because the numbers it produced were cited as this project's central
generalization evidence (synthetic-to-real precision comparison). Anyone
who has cited the original 0.34–0.51 real-data precision range from this
project's earlier README, chapter, or paper drafts should treat those
specific numbers as retracted and superseded by the table above — that
correction is outside this document's scope to make in those other
documents, but it needs to happen wherever those numbers were cited.

---

## Pattern across these bugs

Every critical-impact bug on this list (1, 4, 5, 7) shared the same shape:
the pipeline appeared to be working — no crash, no error thrown, requests
returning 200 — while silently destroying or never producing the data it
was supposed to produce. None of these were caught by the absence of
errors; all were caught by manually cross-checking document counts against
known-good baselines (raw line counts, expected quarantine rates) and, in
Bug 5 and 7's case, by directly inspecting sample documents rather than
trusting the aggregate count alone. This is the practical argument for
building an explicit reconciliation check (source line count == anonymized
count + quarantined count, and audit-index count > 0 whenever PII was
detected) into the pipeline itself, using `_search`-based counts rather
than `_cat/indices` (see Bug 8), rather than relying on manual spot checks
going forward.

**Final verification, this test run:** 10,000 source lines in, 9,984
landed in `security-logs-anonymized`, 16 in `security-logs-quarantine`
(9,984 + 16 = 10,000, exact), 5,168 signed records in `redact-audit-trail`,
and zero `docs.deleted` on any real index — no outstanding collisions or
overwrites anywhere in the pipeline.
