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
mishandled). **Status:** Fix applied; the residual startup-only cluster of
timeouts flagged below as unexplained has since been root-caused and fixed
— see the update at the end of this entry.

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

**Done, 2026-08-07:** `Dockerfile`'s `CMD` now runs `redact-service` under
gunicorn (`--workers $(nproc)`) instead of `app.run(...)`. Smoke-tested
(stub Flask app, identical `--chdir src ... service:app` invocation,
correct worker count, `/health` responding) but not yet re-run through the
full Docker Compose stack the way the rest of this file's numbers are —
see ROADMAP.md item 7 for the follow-up verification this still needs.

**Full-stack rerun, completed 2026-08-07 (run by the user locally):** fresh
`docker compose down -v && python src/export_raw_logs.py && docker compose
up --build`. `redact-service`'s log shows 8 `Booting worker with pid: N`
lines (matching the host's core count) followed by `Control socket
listening`, and the container reports healthy quickly. Final
reconciliation via `_search?size=0`: `security-logs-anonymized-*` = 10,000,
`security-logs-quarantine-*` = 0 — every one of the 10,000 exported lines
(3,359 Windows events + 3,382 syslog + 3,259 CloudTrail) landed correctly
anonymized under gunicorn, with zero quarantined. gunicorn is confirmed
working end-to-end, not just smoke-tested standalone.

**Residual timeout cluster, root-caused and fixed 2026-08-07:** the
startup-only timeout burst mentioned above (previously "not yet fully
explained") was found while re-verifying Bug 6 below. Root cause:
`detect._get_analyzer()` is `@lru_cache(maxsize=1)`'d, so the expensive
spaCy/Presidio model load only happens on the *first* real `/anonymize`
call, not at process startup. `docker-compose.yml`'s healthcheck for
`redact-service` only hits `/health`, which never touches the analyzer —
so the container reports healthy and Logstash starts sending its
configured 8 concurrent requests (`pipeline.workers => 8`) before the
model is loaded. During that multi-second, GIL-holding load, every request
queues; enough exceed the http filter's timeout to get quarantined.
Confirmed live: a fresh `docker compose up --build` produced a burst of
"Read timed out" errors in Logstash's log in roughly the first 30-60
seconds, then zero for the rest of the run. **Fix:** `src/service.py` now
calls `detect._get_analyzer()` once before `app.run()`, so the model
loads during container startup (while the healthcheck is still failing
and Logstash's `depends_on: condition: service_healthy` is correctly
holding it back) instead of during the first wave of real traffic.

**Fix re-verified, same day:** ran a fresh `docker compose down -v &&
python src/export_raw_logs.py && docker compose up --build` with the fix
in place. `redact-service`'s own log shows `* Running on
http://127.0.0.1:8080` (meaning the warm-up call had already completed)
followed immediately by a passing `/health` check, well before Logstash
finished its own ~38-second startup and began sending `/anonymize`
traffic. `docker compose logs logstash | grep -c "Read timed out"`
returned **0** (previously dozens in the first 30-60 seconds of the same
test). Startup timeout cluster confirmed closed, not just theoretically
fixed.

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

**Impact:** Medium. **Status:** Verified fixed, 2026-08-07 — full
end-to-end Docker Compose confirmation completed (see below).

`src/anonymize.py`'s `TokenStore` performed dict read-modify-write
operations without synchronization. Under concurrent access (multiple
Logstash pipeline workers calling `/anonymize` simultaneously), this
produced `RuntimeError: dictionary changed size during iteration`.

**Fix:** wrapped the read-modify-write sequence in `threading.Lock()`.

**Verification, completed 2026-08-07:** ran the exact steps this entry
previously called for (`docker compose down -v && python
src/export_raw_logs.py && docker compose up --build`) against the current
codebase (including the Bug 9/Bug 10 corpus and measurement fixes from
earlier the same day). `docker compose logs redact-service | grep -i
"RuntimeError\|dictionary changed size"` returned zero hits. Final
reconciliation via `_search` (not `_cat/indices`, see Bug 8):
`security-logs-anonymized-*` = 9,968, `security-logs-quarantine-*` = 32,
sum = 10,000 exact; `redact-audit-trail-*` = 5,964 signed records. The
`threading.Lock()` fix holds under real concurrent load — this closes the
item with the same rigor as the rest of this document, not just the
presence of the fix in source.

**Unrelated but found during this same verification run:** an early
timeout burst in Logstash's log turned out to be the previously-unexplained
residual startup timeout cluster from Bug 3 above — root-caused and fixed
in that entry, not this one. It didn't affect this bug's own reconciliation
(quarantine correctly absorbed the 32 affected events; nothing was lost or
duplicated), but is worth knowing about if you see the same log pattern
when reproducing this test.

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

## 11. Drift detection was structurally blind to flattened-username PII, in every log type

**Impact:** High (a real gap in production-relevant detection coverage,
though found before it reached any live deployment). **Status:** Verified
fixed, 2026-08-07, via a live injection test (source: `src/drift.py`).

Found while measuring the new syslog field extractor (Bug 8/ROADMAP item
8 above) — the coverage numbers alone weren't the interesting part; testing
whether `drift.py` actually caught injected drift in a newly-covered field
was. `field_stats()` combined `detect.scan_regex()` and `detect.scan_ner()`
only. It never called `detect.scan_flattened()` — the fourth detection
layer added earlier this session specifically to close the flattened-name
recall gap (5.9%/4.9% under regex+NER alone, up to 50.3% with this layer;
see the main measurement section of `README.md`). `detect.detect_all()`
has included this layer by default since it was added; `drift.py` had
simply never been updated to match, so every field-level drift check this
project could run was checking against the weaker of the two detection
configurations without anyone deciding that on purpose.

**Confirmed live**, mirroring `validate.py`'s own drift-detection check
(Section 5) exactly: injected a Faker-generated flattened username into
the syslog `sudo.USER` field (constant `"root"` in the unmodified corpus,
so a real 0% baseline critical-hit-rate) across a held-out half of the
syslog entries. Before this fix: **not flagged at all** — the exact
"silent failure, no crash, no error" shape this document's closing section
already names as the pattern across the worst bugs here. After the fix:
correctly flagged, `sudo.USER` baseline 0% → current 36%.

**Fix:** `field_stats()` now includes `scan_flattened()` unconditionally
(cheap, no external model dependency, unlike NER) alongside `scan_regex()`,
with `scan_ner()` gated behind a new `use_ner: bool = True` parameter so
this function — like `evaluate.py`'s `run_evaluation()` already does — can
be exercised in an environment without a spaCy model available.

**Honest side-effect, found by the same test, not itself a bug:** comparing
two stable halves of the same corpus with the fix in place produced one
false-positive flag, `syslog.sshd.user` (50.8% → 45.6%, just past the
default 5% threshold). This is expected, not a defect: a detector with
~50% recall (the flattened layer, on this field) produces more
sample-to-sample variance in its measured hit rate than a near-100%-recall
detector would, so the same fixed 5% drift threshold that works well for
near-perfectly-recalled fields (spaced names, IPs) is more prone to noise
on fields only a partial-recall layer covers. Not fixed here — flagged as
worth knowing before treating every flag on a flattened-name-carrying
field as necessarily real drift, and as a candidate for a
per-field-confidence-aware threshold if this becomes a practical nuisance
in a real deployment, rather than a blanket 5% cutoff everywhere.

**`validate.py` Section 5 rerun, completed 2026-08-07 (run by the user
locally, spaCy-dependent):** confirms the side-effect above is real and
reproducible outside the dedicated injection test, not just a property of
that test's specific setup. `validate.py`'s own drift checks — which
predate the flattened-layer fix — now show 2 of 18 total checks failing,
both in Section 5, both consistent with the documented side-effect rather
than a new defect: "no false positives when comparing a stable corpus
against itself" failed with 3 fields incorrectly flagged, and "nothing
else is falsely flagged alongside the real drift" failed with 4 fields
flagged total (the injected-drift check itself still correctly caught the
real injection — `{'cloudtrail.requestParameters.targetUser',
'cloudtrail.requestParameters.reason', 'syslog.sshd.user',
'syslog.sudo.PWD'}` — but with `syslog.sshd.user` again riding along as a
same-shape false positive, the same field flagged in the dedicated
injection test above). All 16 other checks passed, including the two most
safety-critical categories (audit-trail signature verification and
tamper-rejection; anonymization correlation and reversibility) — this
side-effect is isolated to Section 5's drift-threshold sensitivity, as
predicted, not a regression anywhere else. **Conclusion:** the fix itself
is correct (it catches real drift that was previously invisible) and the
documented side-effect (occasional threshold crossings on
flattened-layer-covered fields due to that layer's partial recall) is now
confirmed on two independent test setups rather than resting on one. The
per-field-confidence-aware threshold noted above remains the concrete next
step if this becomes a practical nuisance; not implemented here.

---

## 12. Audit-trail document ID collisions under sustained load (Bug 4's failure mode, reintroduced)

**Impact:** Critical (silent data loss in the audit trail specifically,
not the main pipeline). **Status:** Verified fixed, 2026-08-07, found and
fixed the same day it was discovered via the ROADMAP item 9 load test.

Found while running `validation/load_test/run_load_test.sh 100000` for the
first time — the first real test of this pipeline beyond the 10,000-line
demo scale it had been exclusively verified against until this point (see
`validation/load_test/README.md`).

**How it surfaced:** the load test's reconciliation check
(`validation/load_test/reconcile.py`) initially failed with
`security-logs-anonymized-*` capped at exactly 10,000 regardless of the
100,000-line input — which turned out to be a *different*, harmless bug in
the reconciliation script itself (see the entry immediately below this
one), not a real ceiling. Once that was fixed and the real counts pulled
with `track_total_hits=true`, `security-logs-anonymized-*` matched
exactly (100,000/100,000, `relation: "eq"`) and `security-logs-quarantine-*`
correctly showed 0 — the main pipeline handled the full 10x-scale load
without any data loss. But `redact-audit-trail-*` landed at only 55,577
documents.

**Root-caused via Logstash's own pipeline stats API**
(`GET /_node/stats/pipelines/main`, reached with `docker exec redact-logstash
curl ...` since Logstash's monitoring port isn't published to the host in
`docker-compose.yml`): the `ruby` filter that fans out audit events
correctly produced 189,159 total events from 100,000 inputs (100,000
originals + 89,159 audit clones — the difference is entries with zero
detected PII, which never trigger the fan-out), and the audit-trail
`opensearch` output plugin reported successfully **sending** all 89,159 of
them (`in: 89159, out: 89159`, no errors). The gap was specifically between
what Logstash reported *sending* and what OpenSearch actually *stored* —
ruling out both a Logstash-side drop and Bug 8's `_cat/indices`-staleness
measurement pitfall (this used `_search` with `track_total_hits=true`
throughout, not `_cat/indices`).

**Root cause:** `logstash/redact-pipeline.conf`'s audit-trail output used
`document_id => "%{[audit_event][authentication_tag]}"` (fixed in Bug 4/7
above, at the time correctly closing a different, more severe bug —
literally every audit record sharing one hardcoded ID string). But
`authentication_tag` (`src/audit.py`'s `build_audit_event()`) is an HMAC
over `field_type`, `method`, `policy_version`, `original_value_fingerprint`,
and `timestamp` — where `timestamp` is `int(time.time())`, **second**
granularity. At 10,000-line demo scale (roughly 10-90 seconds of wall
time depending on which fix era), collisions were rare enough to go
unnoticed. At 100,000 lines sustained over roughly 90-100 seconds of
`redact-service` throughput (~950-1,000 events/sec, see the load test's own
measurement), many genuinely distinct audit events land in the same
wall-clock second *and* share identical field content — the exact same
low-content-diversity, high-repetition property of this project's
synthetic corpus that caused Bug 4 in the first place (`EventID=4634`,
`TargetUserName=SYSTEM`, and similar fixed-field system events recur
constantly and verbatim). Two distinct audit events with identical
`field_type` + `original_value_fingerprint` + the same second of wall
time produce the **identical** `authentication_tag`, and therefore the
identical `document_id`, silently overwriting one another on write — this
is Bug 4's Stage 1 failure mode, precisely, just reintroduced in a branch
that Bug 4's actual fix (a random, non-content-derived ID) was never
applied to. The retry-idempotency goal that motivated using
`authentication_tag` as the ID in the first place was a real, legitimate
requirement — it was the choice of a content-derived ID to satisfy it that
reintroduced the collision risk.

**Fix:** mirrors Bug 4's actual fix exactly. The `ruby` filter that creates
each audit clone now also generates a fresh random UUID
(`java.util.UUID.randomUUID.toString` — JRuby interop, no gem `require`
needed, avoiding any uncertainty about stdlib availability inside
Logstash's bundled JRuby) and stores it at `[@metadata][audit_doc_id]` on
that clone specifically, not inherited from the parent event (each of the
up-to-several audit clones fanned out from one input event needs its own
independent ID, not a shared one). The output block's `document_id` now
references `%{[@metadata][audit_doc_id]}` instead of the content-derived
tag. Retries stay idempotent for the same reason the main event's UUID
already does: Logstash's output retry logic resubmits the *same*
in-memory cloned event object, so the UUID already set on it survives the
retry unchanged; two genuinely distinct audit events get cryptographically
independent UUIDs regardless of timing or content similarity.
`authentication_tag` remains in the document as a field (verified via
`audit.py`'s `verify_audit_event()`), it simply no longer doubles as the
document's primary key.

**Not yet re-verified against a live run** — the fix was made and
documented the same session this bug was found, but a fresh
`validation/load_test/run_load_test.sh 100000` (or larger) confirming
`redact-audit-trail-*` now lands at exactly 189,159-minus-quarantined
audit events, matching the ruby filter's own fan-out count, still needs
to be run. This is the concrete next step before this entry is upgraded
from "verified fixed in source, logic re-derived from Bug 4's precedent"
to "verified fixed via a completed clean-room rerun," the same bar every
other entry in this document is held to.

**Compliance note:** this is a second consecutive finding (after the
`_search` cap below) that only became visible once this project tested
beyond demo scale — a strong argument, independent of any specific number
in this project, for why "verified at 10,000 lines" and "verified at
production volume" are different claims that should never be conflated
in anything citing this framework's audit-trail reliability, especially
given the audit trail's role in the NIST SP 800-53 / GDPR Article 32
compliance mapping this project's chapter work discusses elsewhere.

---

## 13. `_search` silently caps reported document counts at 10,000 without `track_total_hits`

**Impact:** None to the pipeline itself (a measurement pitfall in this
project's own test tooling, not a real ceiling) — but it's what initially
made Bug 12 above look like total data loss on the main index too, before
being isolated to just the audit trail. Recorded with the same weight as
Bug 8 (the `_cat/indices` staleness pitfall) because it's the same class
of problem and just as capable of producing a false alarm.

**Status:** Verified fixed, 2026-08-07 (source:
`validation/load_test/reconcile.py`).

Running `validation/load_test/run_load_test.sh 100000` for the first time,
reconciliation failed with `security-logs-anonymized-*` reporting exactly
10,000 documents no matter how large the actual input was — which looked,
at first glance, exactly like Bug 12 (a silent ceiling on writes). The tell
that it wasn't a real ceiling: the number was *exactly* 10,000, not
"roughly 10% of input" or any number that would plausibly result from a
resource limit or partial failure — an exact round number reported
identically regardless of true document count is the signature of a
reporting cap, not a real one (the same category of red flag Bug 8's
entry already describes for `_cat/indices`, just a different endpoint
producing it).

**Root cause:** Elasticsearch/OpenSearch's `_search` API only tracks
`hits.total.value` *accurately* up to 10,000 documents by default (the
`track_total_hits` setting, which defaults to `10000`); past that, the
reported total is silently capped at exactly 10,000 with
`relation: "gte"` (at least 10,000, not exactly) instead of the real
count, unless the request explicitly asks for full accuracy. Every prior
reconciliation check in this project's history (`BUGS_AND_FIXES.md` bugs
1-11) stayed at or under 10,000 total documents, so this cap was never
crossed and never exposed as a problem until a load test intentionally
went beyond that scale.

**Confirmed directly:** re-querying the same indices with
`track_total_hits=true` immediately showed the real counts —
`security-logs-anonymized-*` at exactly 100,000 (`relation: "eq"`,
matching the 100,000-line input exactly) and `security-logs-quarantine-*`
at 0 — both correct, and consistent with Logstash's own pipeline stats
(see Bug 12) showing the main pipeline handled the full load without
error. Only the audit trail (Bug 12, a real and separate bug) showed an
actual shortfall once measured correctly.

**Fix:** `reconcile.py`'s `count()` function now appends
`&track_total_hits=true` to every `_search` call, and additionally checks
`hits.total.relation == "eq"`, raising an error rather than silently
trusting an inexact count if that check ever fails for any reason (a
belt-and-suspenders check, since `track_total_hits=true` should always
produce `"eq"`, but asserting it explicitly costs nothing and catches a
future regression immediately rather than reintroducing this exact
false-alarm risk silently).

**Lesson, stated the same way Bug 8's entry states its own:** any
reconciliation or count-based verification against Elasticsearch/
OpenSearch that might exceed 10,000 total matching documents needs
`track_total_hits=true` (or a value higher than the expected count) on
every `_search` call, the same way Bug 8 already established that
`_search` should be preferred over `_cat/indices` for accuracy under
write pressure. Both are instances of the same general principle: the
default, most-obvious way to ask Elasticsearch/OpenSearch "how many
documents are in this index" is optimized for search-result-page
performance, not exact counting, and silently gives an approximate
answer unless told not to.

---

## Pattern across these bugs

Every critical-impact bug on this list (1, 4, 5, 7, 12) shared the same
shape: the pipeline appeared to be working — no crash, no error thrown,
requests returning 200 — while silently destroying or never producing the
data it was supposed to produce. None of these were caught by the absence
of errors; all were caught by manually cross-checking document counts
against known-good baselines (raw line counts, expected quarantine rates,
and — for Bug 12 specifically, the first bug this document needed a
second signal for — Logstash's own pipeline stats API showing what was
actually *sent* versus what OpenSearch actually *stored*) and, in Bug 5,
7, and 12's case, by directly inspecting sample documents or plugin-level
counters rather than trusting the aggregate index count alone. This is the
practical argument for building an explicit reconciliation check (source
line count == anonymized count + quarantined count, and audit-index count
matching the fan-out count whenever PII was detected) into the pipeline
itself, using `_search`-based counts with `track_total_hits=true` rather
than `_cat/indices` or an unqualified `_search` (see Bugs 8 and 13),
rather than relying on manual spot checks going forward. **Bug 12 also
adds a new instance of a narrower, recurring sub-pattern already seen in
Bug 4 and Bug 7: a document ID derived from event content plus a
coarse-grained timestamp is not actually collision-resistant once volume
or throughput increases enough for genuinely distinct events to share
both — every document_id in this pipeline that matters for correctness
should be reasoned about as "does this stay unique under 10x the load
it was last verified at," not just "was this unique in the last test
run."**

**Final verification, this test run:** 10,000 source lines in, 9,984
landed in `security-logs-anonymized`, 16 in `security-logs-quarantine`
(9,984 + 16 = 10,000, exact), 5,168 signed records in `redact-audit-trail`,
and zero `docs.deleted` on any real index — no outstanding collisions or
overwrites anywhere in the pipeline.
