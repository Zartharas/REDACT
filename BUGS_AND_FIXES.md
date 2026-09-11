# Known Issues and Fixes

A running record of bugs found while building and testing the REDACT
pipeline (Logstash → `src/service.py` → OpenSearch): why each one mattered
and how it got fixed. This lives here instead of only in commit messages
because several of these follow the same dangerous shape — "looks like
it's working, is actually silently destroying data" — and that's exactly
the kind of failure worth a permanent, findable record, for this project
and for anyone building something similar.

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
mishandled). **Status:** Verified fixed — gunicorn confirmed under a full
Docker Compose rerun; the residual startup-only cluster of timeouts
flagged below as unexplained has since been root-caused and fixed too —
see the updates through the end of this entry.

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
gunicorn (`--workers $(nproc)`) instead of `app.run(...)`. First
smoke-tested standalone (stub Flask app, identical `--chdir src ...
service:app` invocation, correct worker count, `/health` responding), then
confirmed under the full Docker Compose stack — see the full-stack rerun
immediately below.

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
18-check suite (18/18 passed at this point in time) were both re-run
against the regenerated, Bug-9-fixed corpus. **(This 18/18 result predates
Bug 11 below, which made drift detection flattened-layer-aware and
introduced 2 now-expected Section-5 failures — see Bug 11's own entry for
the current 16/18 state and why the 2 failures aren't a regression.)** The `98/1,668` pre-Layer-4 flattened-format baseline
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
unnoticed. At 100,000 lines sustained over the ~400 seconds the fully
fixed load test measured (see Bug 13 below and the load test's own
results — ~250 events/sec end-to-end on this machine), many genuinely
distinct audit events still land in the same wall-clock second *and*
share identical field content — the exact same
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

**Verified fixed via a completed clean-room rerun, same day.** A third
load-test run (the first two were cut short by the unrelated harness bug
documented in Bug 13 below) completed cleanly end to end:
`security-logs-anonymized-*` = 100,000 exact, `security-logs-quarantine-*`
= 0 exact, and — the number this fix specifically targets —
`redact-audit-trail-*` = **89,159**, an exact match to the ruby filter's
own fan-out count from the earlier diagnostic run (not
189,159-minus-quarantined as an earlier draft of this entry incorrectly
stated — 189,159 was the ruby filter's *total* output, 100,000 originals
plus 89,159 clones; only the 89,159 clones route to the audit-trail
index, the originals go to `security-logs-anonymized`). Zero collisions,
zero shortfall, at the same 100,000-line, sustained-throughput conditions
that produced the 55,577-of-89,159 shortfall before this fix.

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

**Second, independent occurrence of the exact same bug, found on the very
next run:** `run_load_test.sh`'s own polling loop makes its own separate
`curl` calls to check whether ingestion has stabilized (rather than reuse
`reconcile.py`), and those calls were missing the same
`track_total_hits=true` fix. Confirmed live: rerunning the load test after
the fix above, the poll loop's own (still-uncapped-fix) queries plateaued
at exactly 10,000 for three consecutive 15-second polls once the real
count crossed that threshold — read by the loop's stability check as
"ingestion finished," when the real count at that moment (confirmed by
the *final* reconciliation call, which does use the fixed `reconcile.py`)
was only 28,375 of 100,000. The run exited 118 seconds early as a direct
result. Fixed the same way, independently, in `run_load_test.sh` itself —
this bug needed fixing in two places because it was written in two
places, a reminder that "the same underlying API gotcha" and "already
fixed" are not the same claim when the same query pattern was
hand-written more than once rather than factored into one shared
function.

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

## 14. TokenStore's persistence was unsafe across processes -- crash risk plus real data loss under gunicorn's actual, already-shipped multi-worker deployment

**Impact:** Critical (silently breaks tokenize()'s core, stated guarantee
that a tokenized value can always be recovered later -- and, before the
fix, could crash a worker outright under real concurrent access). **Status:**
Verified fixed, 2026-08-08, via a before/after multi-process test
(`validation/multiprocess_tokenstore_test.py`) run entirely in a plain
Python environment (no Docker or Redis needed to find or fix this).

Found while building the multi-process Redis concurrency test ROADMAP
item 6 explicitly flagged as still needed (`redis_storage_provider_test.py`
had only ever tested multiple *threads* in one process, never the actual
production topology of multiple separate *processes* sharing one backend).
Investigating what that test should actually exercise led to a close read
of `TokenStore.save()` and `service.py`'s real usage of it — and a
realization, confirmed empirically before assuming it was real: `_store =
anonymize.TokenStore(...)` in `service.py` is module-level code, so under
gunicorn's default `--workers $(nproc)` deployment (`Dockerfile`, already
shipped, not a hypothetical future scale-out scenario), **each worker is a
separate OS process with its own `TokenStore`, all sharing one
`token_store.json` file via the `redact-output` volume, and `_store.save()`
fires after every single `/anonymize` request.** This is the exact
concurrent-access pattern this bug needed to manifest, already running in
every Docker Compose test this project has ever done that tokenized an
EMAIL/SSN/MRN/CREDIT_CARD value — not a scenario that needed Redis or
multiple replicas to exist.

**Two separate, compounding defects, found in this order:**

**(a) `FileStorageProvider.save()` was not atomic — a crash risk, not just
a race.** It opened the destination path directly in `"w"` mode, which
truncates the file immediately, before `json.dump()` has written anything
back. A concurrent `load()` in another process landing in that window
reads a truncated, invalid file and crashes with `json.JSONDecodeError`.
Confirmed live: the first version of the multi-process test crashed 5 of 8
worker processes this way within a handful of concurrent saves — worse
than silent data loss, since in a real deployment this would propagate up
through `service.py` as a request failure (`_httprequestfailure`,
quarantined by `redact-pipeline.conf`'s existing fail-closed logic — so no
PII would have leaked un-anonymized, but requests would fail for a reason
distinct from, and in addition to, Bug 3's already-documented startup
timeout cluster). **Fix:** write to a temp file in the same directory,
then `os.replace()` to the final path — atomic on POSIX, so a concurrent
reader always sees either the complete old file or the complete new one,
never a partial one.

**(b) `TokenStore.save()` was a blind overwrite, not a merge — the
deeper, provider-agnostic defect.** `__init__` loads persisted state
*once*, at construction. Every `save()` before this fix then persisted
only *this process's own local view*, completely replacing the backend
regardless of what any concurrent process had written since. Under
`service.py`'s real per-request `save()` pattern, this is not a rare
race — it is close to guaranteed, repeated loss: whichever worker's
`save()` lands last in any given window silently erases every
reverse-map entry a sibling worker wrote that this worker never itself
loaded. This directly breaks `tokenize()`'s own stated guarantee
(`anonymize.py`'s module docstring: "Exact original value can be
recovered by anyone with access to `store`") — an authorized
investigator's `detokenize()` call would silently fail to recover a
value that was, in fact, tokenized and should be recoverable, with no
error anywhere in the chain. The forward map (original → token) is a
redundant cache, not a correctness risk on its own — `get_or_create_token`
derives the token deterministically via HMAC, so any process recomputing
it for the same original value gets the identical token regardless of
whether it saw a sibling's entry. The **reverse map (token → original)
is the actual irreplaceable data**, and losing entries from it is what
this bug does.

**First fix attempt (read-merge-write) — measured as a real but
incomplete improvement, not assumed sufficient:** `save()` now reloads
current persisted state immediately before writing, merges this
process's own additions on top of it (not instead of it), and adopts the
merged result as its own in-memory state too. Measured directly: on the
same 8-process × 50-tokens-per-worker stress test that crashed workers
before, this fix alone brought zero-crash reliability (all 8 workers
completed) but still lost **57 of 400 tokens' reverse-map entries
(14.2%)** — a large improvement over the pre-fix 58.7% loss rate (and the
pre-fix run's 5 crashed workers), but nowhere near acceptable for a
guarantee this framework states as a plain fact in its own docstring.
The residual gap: two processes' `save()` calls can still interleave
within the window between one process's own `load()` and its subsequent
`save()` — read-merge-write narrows the race, it does not close it.

**Actual, complete fix: real cross-process locking around the entire
load-merge-save critical section**, not just a narrower race. Added
`StorageProvider.lock_for_save()` (default a no-op `contextlib.nullcontext`)
with real implementations in both providers:
- `FileStorageProvider.lock_for_save()`: a blocking exclusive
  `fcntl.flock()` on a sibling `.lock` file for the duration of the
  critical section — advisory (only code that also calls
  `lock_for_save()` is protected, fine here since `TokenStore` is the
  only caller), POSIX-only (guarded with a try/except `ImportError` so
  the module still imports on a non-POSIX dev machine, just without
  cross-process file locking there).
- `RedisStorageProvider.lock_for_save()`: a standard single-node Redis
  distributed lock (`SET key owner-token NX PX ttl` to acquire, a Lua
  script that only deletes-if-still-owner to release, capped exponential
  backoff while contended, a bounded 15s acquire timeout that raises
  `TimeoutError` rather than hanging forever or silently proceeding
  unlocked). Deliberately scoped to single-node correctness, not the
  full Redlock algorithm — `docker-compose.yml`'s Redis is explicitly
  single-node, matching this project's stated single-node scope
  everywhere else (`validation/load_test/README.md`).

**Confirmed live, same before/after test:** with locking in place, **0 of
400 tokens lost**, across all three stages measured on the identical
stress pattern — crash-prone and 58.7% loss (pre-fix), 0% crashes but
14.2% loss (read-merge-write alone), 0% loss (read-merge-write + real
locking). Single-process overhead confirmed negligible (50 sequential
saves: 0.018s before this work started, 0.022s after — the lock is
uncontended in the common case and costs almost nothing). The full
8-process/400-save concurrent test itself completes in ~0.5 seconds wall
clock.

**`validation/multiprocess_redis_test.py` confirmed live, 2026-08-08, run
by the user locally** (`docker run -d --rm -p 6379:6379 --name
redact-test-redis redis:7`, then `python validation/multiprocess_redis_test.py`
against the real client): **0 of 400 tokens lost**, same 8-process x 50
tokens x save-after-every-token stress pattern as the file-backend test.
`RedisStorageProvider.lock_for_save()`'s single-node `SET NX PX` +
Lua-release lock holds under real separate OS processes against a real
Redis instance, not just in the file-backend's logically-analogous but
distinct code path. This closes the one gap this bug's writeup originally
left open — `redis_storage_provider_test.py`'s pre-existing test only ever
exercised multiple *threads* in one process, which is exactly the kind of
gap that let this bug go undetected for as long as it did.

**Compliance note, stated as plainly as Bug 5/7/12's:** this is the same
"silently wrong, no crash, no error visible to the caller" shape (for the
read-merge-write-alone stage, and for the underlying blind-overwrite bug
before any fix) that this document's closing section already names as
the pattern across the worst bugs here — just found in the one component
whose entire purpose is a compliance-relevant reversibility guarantee. If
this framework's tokenization/reversibility claims are ever cited against
GDPR Article 32 or a similar audit-trail-integrity requirement, this
failure mode — found by a dedicated multi-process test that nothing in
this project's existing suite (`validate.py`, `evaluate.py`, the original
single-threaded Redis test) would ever have exercised — should be
disclosed alongside the fix, the same way Bug 12's audit-trail collision
was.

---

## 15. TokenStore.save() rewrites the entire store on every call -- O(n) per request, O(n^2) total, found at 1,000,000-line scale

**Impact:** Critical (not data loss -- throughput collapse severe enough
to make the pipeline practically unusable at sustained real-world token
volume, on a component every EMAIL/SSN/CREDIT_CARD/MRN value passes
through). **Status:** Root-caused 2026-08-08; debounce mitigation applied,
verified in-sandbox, and **confirmed live at the actual failing scale the
same day** -- a full 1,000,000-line rerun against the mitigation passed
reconciliation exactly (see "Confirmed at 1,000,000-line scale" below). A
full fix (append-only/WAL persistence, or incremental Redis writes) is
still NOT implemented -- see "What a real fix needs" below, which remains
open even though the mitigation resolved the immediate observed failure.
Before this confirmation, the 1,000,000-line load test had not yet been
re-run against the mitigation to confirm it resolves the observed
collapse at that scale.

**Found via ROADMAP item 9's follow-on**, the first run of this project's
load test beyond 100,000 lines (`validation/load_test/run_load_test.sh
1000000`, run by the user locally). The run's own reconciliation FAILED:
728,125 of 1,000,000 expected events landed in
`security-logs-anonymized-*`/`security-logs-quarantine-*` combined when
the test harness's stability-poll loop declared ingestion complete and
exited. Live diagnosis (not a guess -- see the exact sequence below)
found ingestion was still genuinely progressing, just extremely slowly:
polling `_search` directly minutes later showed the true count still
climbing (916,500 by the last check), `docker stats` showed
`redact-service` pegged at 109.80% CPU and 6.67GiB memory while producing
only ~3 events/sec, and `redact-logstash` sitting nearly idle at 0.69%
CPU -- meaning Logstash had already sent its requests and was simply
waiting on a severely bottlenecked `redact-service`. No errors anywhere
in any container's logs (`opensearch`, `redact-service`, `logstash` all
grepped for circuit breakers, OOM, timeouts, backpressure -- nothing);
disk was at 8% usage, not a watermark issue. This is the same "looks
fine, no crash, no error, just gets progressively wrong" shape this
document's closing section already names as the pattern across the worst
bugs here, just manifesting as catastrophic slowness instead of silent
data loss.

**Root cause, confirmed directly:** `docker exec redact-service ls -la
/app/output/` showed `token_store.json` at 12.4MB, 93,279 entries. A
single live request timed with `time curl -X POST .../anonymize`
against the running stack took **2.927 seconds**. `TokenStore.save()`
(added by Bug 14's fix, `src/anonymize.py`) does a full read-merge-write
on every call: load the ENTIRE persisted store from the backend, merge
in this process's own additions, write the ENTIRE merged store back --
`FileStorageProvider.save()` rewrites the whole JSON file,
`RedisStorageProvider.save()` (checked directly -- same defect, not
backend-specific) does `DELETE` then a full `HSET` of the whole hash on
every call, not an incremental update, even though Redis natively
supports atomic per-field writes and never needed a delete-and-rewrite
pattern. `service.py` calls `_store.save()` after EVERY single
`/anonymize` request (`_store.save()` at the end of the request handler,
unconditional). Cost per request is therefore O(current store size), and
since the store only grows, total cost across a run is O(n^2) in the
number of distinct EMAIL/SSN/CREDIT_CARD/MRN values ever tokenized --
invisible at every scale this project tested before now (10,000 lines:
trivially small store; 100,000 lines, ROADMAP item 9's first load test:
still small enough to stay sub-second), and only became a practical wall
once the store crossed roughly 90,000+ entries.

**This is the direct, unintended cost of Bug 14's own fix.** Bug 14
correctly closed a cross-process data-loss race by making `save()` do a
locked read-merge-write instead of a blind overwrite -- but the read-
merge-write pattern itself was applied uniformly to both storage
backends without separately asking whether each backend actually needed
a full rewrite to stay correct. It's the right fix for
`FileStorageProvider` (a flat JSON file has no other way to do a partial
update atomically). It was never necessary for `RedisStorageProvider` --
Redis's own `HSET` already performs an atomic, race-free partial update
of a single field, which is exactly what Bug 14's problem needed and
what this defect never used.

**Mitigation applied and verified in-sandbox, 2026-08-08 (NOT a full
fix, stated as plainly as Bug 14's own first, incomplete attempt was):**
`TokenStore` gained a `save_every_n_calls` constructor parameter
(default 1, preserving the exact existing behavior every current test
assumes and verifies -- `validation/multiprocess_tokenstore_test.py` and
`validation/multiprocess_redis_test.py` both construct `TokenStore` with
no override and still get a real write on every single `save()` call, so
their own zero-loss guarantees are completely unaffected by this
change). `service.py` now defaults to `REDACT_TOKEN_STORE_SAVE_EVERY=25`
(configurable), performing the actual expensive write once every 25
requests instead of every one. **Confirmed directly, not just reasoned
about**, via a new dedicated in-sandbox test
(`validation/tokenstore_save_scaling_test.py`, no Docker or Redis
needed, pure `FileStorageProvider` since it shares the same read-merge-
write code path): minting 6,000 tokens with `save_every_n_calls=1`
showed per-call cost growing from 0.22ms to ~19-24ms (10-22x slower at
23x the store size, directly confirming O(n) growth, not just inferring
it from the live run's single 2.927s data point); the identical run with
`save_every_n_calls=50` performed exactly the expected 120 real writes
(confirmed by instrumenting `TokenStore`'s own debounce counter, not
guessed from timing), each individually costing about the same as a
baseline write at an equivalent store size (as expected -- the
mitigation doesn't change what a write costs, only how often it's paid),
for a **~47x reduction in total wall-clock time spent in `save()`**
across the full run.

**What this mitigation does NOT do, stated as plainly as the improvement
itself:** it does not change the underlying O(n) per-write cost or the
O(n^2) total-cost shape -- it divides the constant factor by roughly the
debounce value, which pushes the point where this becomes a practical
problem out by roughly that same factor, not eliminates it. At
sufficiently large scale (a real production deployment sustaining
EMAIL/SSN/CREDIT_CARD/MRN tokenization over weeks or months, not just a
one-off 1,000,000-line batch test) this will eventually become a problem
again. It also introduces a new, explicit tradeoff that didn't exist
before: if a worker process crashes (or is killed without a clean
shutdown -- there is no signal handler forcing a final flush) between
real writes, up to `save_every_n_calls - 1` requests' worth of
reverse-map entries exist only in that worker's memory and are lost.
This is a bounded, documented risk, not a silent one -- but it is a real
regression in the crash-recovery guarantee Bug 14 established, traded
deliberately for throughput.

**Confirmed at 1,000,000-line scale, 2026-08-08 (run by the user
locally):** after tearing down the original stuck stack
(`docker compose down -v`) and rebuilding with the mitigation in place,
`validation/load_test/run_load_test.sh 1000000` was run fresh. The
harness's own poll loop again reported a `RECONCILIATION: FAIL` at exit
(938,000 of 1,000,000, after 240 polls / ~3,677s) -- but this time for a
different, more benign reason than the original failure: manually
re-running `reconcile.py` minutes later showed the count still climbing
steadily (971,625, then 981,750 after another 180s, ~56 events/sec, not
stalled), and a further 600s wait produced a clean, complete result:
**`security-logs-anonymized-*` = 1,000,000 exact,
`security-logs-quarantine-*` = 0 exact, `redact-audit-trail-*` = 893,150,
`RECONCILIATION: PASS`.** The 89.315% audit fan-out rate (893,150 /
1,000,000) closely matches the 100,000-line run's 89.159% (89,159 /
100,000), a reassuring consistency check that the pipeline's correctness
holds at 10x that scale, not just the raw completion count. Total time to
full completion was under ~75 minutes from the start of `docker compose
up`, at an average of roughly 224 lines/sec across the *entire* run
(1,000,000 lines / ~4,457s) -- close to the ~250 lines/sec baseline
established at 100,000-line scale, meaning the mitigation's effect was
strong enough that the run's OVERALL average throughput barely degraded
despite the store growing to hundreds of thousands of entries along the
way.

**A second, smaller, separate finding from this rerun**: `run_load_test.sh`'s
own poll loop gave up after a fixed number of iterations (240 * 15s = 1
hour) rather than a truly adaptive stability check, so it reported `FAIL`
here even though the pipeline was healthy and steadily converging, not
stalled -- a false negative from the harness's own patience budget, not a
real regression. This was a minor, separate scoping gap in the test
tooling (not the pipeline).

**Fixed, 2026-08-08.** `run_load_test.sh`'s poll loop now uses a
wall-clock deadline (`REDACT_LOAD_TEST_MAX_WAIT_SECONDS`, default 14400s
/ 4 hours) instead of a fixed 240-iteration count, so the ceiling scales
with how long a run actually takes rather than an assumption baked in at
100,000-line scale. The stability decision itself is unchanged (still 3
consecutive polls with an unchanged anonymized+quarantine total); the fix
only changes when the harness gives up waiting for that condition, not
what the condition is. **Re-verified live, 2026-08-10**: `run_1m_load_test.sh`
(repo root) calls this exact harness unmodified for the field-gated
1,000,000-line run described in Bug 16's addendum below -- that run took
~2,276s and reported a clean `RECONCILIATION: PASS`, well inside the new
4-hour deadline and with no false `FAIL` from the old fixed-iteration cap.
`bash -n` syntax-checked before that run too, but this is the actual live
confirmation, not just the syntax check (that verification needed the
user's machine -- see
ROADMAP item 9).

**What a real fix still needs (as originally written, 2026-08-08, before
the fix below landed the same day -- kept here verbatim so the "still
needs" framing that follows isn't silently rewritten after the fact):**
1. For `RedisStorageProvider` specifically: replace the delete-then-
   full-`HSET` pattern with an incremental `HSET` of only the NEW
   entries since the last save, using Redis's own atomic per-field
   write instead of re-deriving TokenStore's file-backend-oriented
   read-merge-write pattern. This would very likely make Redis's
   `save()` genuinely O(1) per new entry (aside from the still-O(n)
   initial `load()` at process startup, which is a one-time cost, not a
   per-request one), eliminating the need for Bug 14's cross-process
   lock on the Redis path entirely (an incremental per-key `HSET` from
   a private, non-overlapping key set is already safe without it).
2. For `FileStorageProvider`: an append-only/write-ahead-log persistence
   format (append only the new entries to a log file on every save,
   periodically compact into the canonical JSON snapshot in the
   background) would avoid ever re-writing entries that haven't
   changed, a real architectural change beyond a debounce parameter.
3. The debounce mitigation's O(n^2/k) shape means a sufficiently larger
   run (10,000,000 lines, or sustained production volume over weeks/
   months) would still eventually hit the same wall this bug describes,
   just further out. The mitigation is confirmed sufficient for the
   scale this project has actually tested (1,000,000 lines, confirmed
   above); it is not a claim that the underlying problem is gone.

---

**REAL FIX implemented, 2026-08-08, same day as the mitigation above.**
Both items 1 and 2 from the list just above are now done, closing the
gap the debounce mitigation always disclosed it left open. New method on
`StorageProvider`: `save_incremental(new_forward, new_reverse) -> bool`,
which persists ONLY the entries minted since the last successful save --
not the full accumulated store `save()` always required. `TokenStore`
now tracks a `_pending_forward`/`_pending_reverse` delta (populated in
`get_or_create_token()`, cleared once a save actually persists it) and
`save()` tries `save_incremental()` first, falling back to the original
full read-merge-write only for a provider that doesn't override it
(returns `False` by default, preserving exact prior behavior for any
future provider that hasn't implemented the incremental path yet).

- **`RedisStorageProvider.save_incremental()`**: `HSET` of only the new
  batch, exactly item 1 above. Deliberately does NOT take
  `lock_for_save()` -- per-key `HSET` from two processes writing
  different keys is safe without it, and if two processes independently
  mint a token for the identical original value (the only way they'd
  write the *same* key), `get_or_create_token`'s HMAC is deterministic,
  so both compute the identical token and the second `HSET` just
  overwrites the first with an identical value. This isn't a corner cut
  -- it's removing a lock the old delete-then-full-rewrite design forced
  onto this path as a side effect of using a destructive write instead
  of an additive one; the lock is still used, unchanged, by the
  fallback `save()` path.
- **`FileStorageProvider.save_incremental()`**: appends only the new
  batch as one JSON line to a sibling write-ahead log (`<path>.wal`),
  exactly item 2 above, still guarded by `lock_for_save()` (the file
  backend's plain append CAN interleave between two processes writing at
  once, unlike Redis's atomic per-key `HSET`) -- but the critical section
  is now bounded by batch size, not total store size, which is the
  actual fix: the lock itself was never the O(n) cost, holding it across
  a full-store rewrite was. `load()` now reads the canonical JSON
  snapshot and replays any WAL lines on top of it, so nothing between the
  last compaction and now is lost. A new `compact()` method folds the WAL
  back into the snapshot and truncates it -- still an O(n) operation, but
  `save_incremental()` only triggers it once every
  `wal_compact_threshold_lines` batches (default 200), not on every
  call, so the expensive full rewrite this bug is about is now paid on
  the order of hundreds of times less often than before, not once per
  request.

**Verified in-sandbox, 2026-08-08 (no Docker or Redis needed for the
`FileStorageProvider` path; the `RedisStorageProvider` path still needs
live-Redis re-verification, see below):**
- `validation/multiprocess_tokenstore_test.py` re-run against the new
  code: **0/400 reverse-map entries lost**, unchanged from Bug 14's own
  result -- the incremental-write path does not reopen the cross-process
  race that fix closed.
- `validation/tokenstore_save_scaling_test.py`, re-run and rewritten to
  reflect the new result (its original text documented the mitigation-
  only O(n) growth curve; that text is preserved in the script's own
  docstring under a "HISTORY" section rather than deleted, so the two
  runs don't read as contradictory): **growth factor 1.1x at 23x the
  store size**, down from the pre-fix 10-22x -- the O(n) shape this test
  was built to detect is gone even at `save_every_n_calls=1` (no
  debounce at all), not just reduced by a constant factor the way the
  mitigation-only result showed.
- New test, `validation/wal_compaction_correctness_test.py`: 5,000 tokens
  minted with a deliberately small `wal_compact_threshold_lines=50`
  (forcing ~100 compactions within the run, instead of relying on luck to
  cross the production default of 200 a handful of times) -- **0/5,000
  tokens lost** both resolving within the same process and resolving via
  a completely fresh `TokenStore`/`FileStorageProvider` instance pointed
  at the same path (simulating a process restart or a sibling gunicorn
  worker), and the WAL's line count stayed at or below the configured
  threshold throughout the run, confirming `compact()` fires on its
  configured cadence rather than the WAL being left to grow unboundedly
  (which would have just moved this bug's O(n) problem into a different
  file rather than fixing it).

**Redis path confirmed, 2026-08-08, run by the user locally:**
`validation/multiprocess_redis_test.py` (the same 8-processes-x-50-tokens
test that confirmed Bug 14's fix against real Redis) was re-run against a
live `redis:7` container (`docker run -d --rm -p 6379:6379 --name
redact-test-redis redis:7`) after this change: **0 of 400 reverse-map
entries lost.** `RedisStorageProvider.save_incremental()`'s per-key
`HSET` path holds under real cross-process concurrency, closing the one
gap left open when this fix first landed -- both providers are now
verified live for the incremental-write path, not just the file backend.

`compact()`'s own O(n) cost is also unavoidable by
design (the snapshot format is a single JSON object, not itself
append-only) -- this fix reduces how often that cost is paid by roughly
`wal_compact_threshold_lines`-fold, it does not eliminate it, which is
the same honest framing the original debounce mitigation used for the
same reason.

**Compliance note, same standing as Bug 14's:** this doesn't touch the
tokenize()/detokenize() reversibility guarantee itself when the debounce
default resolves without a crash, but the crash-window tradeoff
introduced by the mitigation is directly relevant to any GDPR Article 32
/ audit-integrity claim about this framework's reversibility guarantee
and should be disclosed alongside Bug 14's own disclosure, not treated
as settled just because Bug 14 is.

---

## 16. Logstash config hash literal using comma separators -- a hard parse error, invisible until the container was inspected directly

**Found:** 2026-08-10, during the first attempt at rerunning the
1,000,000-line load test with field-gated NER wired in as
`redact-service`'s default detection path (see `src/detect.py`'s
`detect_all_field_gated`). That change needed `log_type` forwarded from
Logstash to `redact-service` for the first time ever at this scale, so
`logstash/redact-pipeline.conf`'s `http` filter body was edited from a
single-key hash to a two-key one:

```
# BROKEN -- comma between hash entries
body => { "log" => "%{message}", "log_type" => "%{log_type}" }
```

This is valid Ruby and valid JSON, both of which use commas between
hash/object entries -- and Logstash's config DSL looks enough like both
that this reads as correct on sight. It isn't: Logstash config hash
literals separate entries with whitespace only, no comma. The comma is a
hard `LogStash::ConfigurationError` at pipeline startup, not a runtime
warning.

**Why this stayed invisible longer than it should have:** the `logstash`
service in `docker-compose.yml` has no healthcheck, so a crashed pipeline
still shows as `Up`/`Started` under `docker compose ps` -- there is
nothing in Compose's own state that distinguishes "Logstash is running
and processing events" from "Logstash's container process is alive but
the pipeline inside it never started." The load test ran its full
corpus-generation and stack-startup sequence, then polled OpenSearch for
30-45 seconds, saw `anonymized=0 quarantine=0 total=0` on every single
poll, and its own stability check -- three consecutive *identical*
values -- read that as "ingestion has stabilized," not "ingestion never
started." It exited "successfully," reconciliation printed
`RECONCILIATION: FAIL` (expected 1,000,000 vs. actual 0, so the numeric
check itself did catch the mismatch), but the throughput line above it
still printed a fabricated `~5,208.3 lines/sec` computed from
`elapsed_seconds` alone with no gate on whether reconciliation had
actually passed -- exactly the kind of plausible-looking wrong number
this project's `README.md` and this file both already warn against
trusting.

**Root cause, found by direct inspection, not guesswork:** `docker compose
logs logstash --tail 200` (not `redact-logstash` -- that's the
`container_name:`, not the Compose service name; the first attempt to
check logs used the wrong one and got "no such service") showed the
exact `LogStash::ConfigurationError` and line number pointing straight at
the extra comma.

**Fix, three parts, in order of when each one matters:**

1. **The actual bug:** removed the comma —
   `body => { "log" => "%{message}" "log_type" => "%{log_type}" }` — and
   added an inline comment on this exact spot in
   `logstash/redact-pipeline.conf` documenting the syntax rule, since it's
   the only multi-key hash literal in the file and the next person editing
   it (including a future instance of this project's own author) will hit
   the same instinct to reach for a comma.
2. **The load-test harness's blind spot:** `run_load_test.sh`'s stability
   check now requires `TOTAL > 0` in addition to three consecutive
   identical readings -- an all-zero "stable" reading no longer exits the
   poll loop early, and a `PREV_TOTAL -eq 0` warning at the end of the
   loop points directly at `docker compose logs logstash` instead of
   letting the run fall through to a misleadingly clean-looking summary.
   The throughput line itself is now gated on `RECONCILE_STATUS`: a
   failed reconciliation prints `n/a (reconciliation did not pass...)`
   instead of computing a number from wall-clock time alone.
3. **Catching the next instance of this bug class before a full run, not
   after:** `run_1m_load_test.sh` now runs
   `docker compose run --rm logstash bin/logstash --config.test_and_exit
   -f /usr/share/logstash/pipeline/redact-pipeline.conf` as an explicit
   pre-flight step. This validates config syntax in seconds without
   starting the pipeline or needing OpenSearch/`redact-service` reachable
   at all -- the fastest possible feedback loop for this exact mistake,
   and one that should run after any future edit to
   `redact-pipeline.conf`, not just before a full load test.

**Confirmed fixed, same day, full clean rerun:** `docker compose down -v`
then `run_1m_load_test.sh` again -- pre-flight printed `Configuration OK`
/ `Logstash config syntax OK.`, then the full 1,000,000-line run
completed with `RECONCILIATION: PASS`:
`security-logs-anonymized-*` = 1,000,000 exact,
`security-logs-quarantine-*` = 0 exact,
`redact-audit-trail-*` = 893,150,
wall clock 2,276s, ~439.4 lines/sec end-to-end. This is this project's
first 1,000,000-line run with field-gated NER as the live default
detection path (see `src/detect.py` and `src/service.py`) and with
`log_type` actually flowing through Logstash into `redact-service`
end-to-end, not just unit- and evaluation-script-tested in isolation.

**Read the 893,150 audit-trail count carefully before assuming it proves
field-gating changed nothing:** it is identical, to the exact digit, to
the 2026-08-08 1,000,000-line run's audit count (see Bug 15 above), which
predates field-gating entirely. That's plausible, not alarming: the
corpus generator is seeded (`Faker.seed(42)`), so the same 1,000,000 raw
lines exist in both runs, and the large majority of audit events come
from regex-detected types (EMAIL, SSN, CREDIT_CARD, IP, MRN) that are
byte-for-byte identical between the naive and field-gated detection
paths -- only PERSON detections can differ, and this project's own
extensive same-session real-data validation (`validation/real_data/`)
already found field-gated's recall statistically indistinguishable from
naive's after the key-prefix excision fix. An exact match at the
aggregate level is consistent with that finding, not independent proof
of it -- if this number needs to be relied on as evidence of parity in
the chapter, break it down by detected type
(`redact_detections_total{type=...}` via the Prometheus metrics endpoint
added earlier this session) rather than citing the aggregate count alone.

**Verified directly, same day, rather than left as a plausible
explanation.** A live OpenSearch aggregation against the actual
2026-08-10 run (`redact-audit-trail-*`, `audit_event.field_type.keyword`,
terms agg) confirmed the buckets sum to exactly 893,150 -- matching the
reconciled total precisely, so the numbers are trustworthy despite
`terminated_early: true` in the response (that flag looked like a repeat
of Bug 13's silent-cap failure at first; it wasn't -- the exact bucket-sum
match rules that out here). Breakdown: `IP` 500,631, `PERSON` 267,018,
`EMAIL` 50,575, `CREDIT_CARD` 25,176, `SSN` 24,980, `MRN` 24,770 -- a
29.90% PERSON share of all detections.

`validation/load_test/verify_type_breakdown.py` (new this session) then
independently reproduced this locally: sampled 3,000 lines per log type
from the same seeded raw corpus still on disk from the 1,000,000-line
run, ran both `detect_all` (naive) and `detect_all_field_gated` on
identical text, and tallied by type. **First pass surfaced a second real
finding, not a bug in the underlying detection code:** naive's
regex-covered-type counts (IP/EMAIL/SSN/CREDIT_CARD/MRN) came back at
almost exactly 2x field-gated's. Root cause: `scan_ner()`'s Presidio
`AnalyzerEngine.analyze()` call requests
`entities=list(_PRESIDIO_TO_CANONICAL.keys())` (`src/detect.py:66`),
which includes `EMAIL_ADDRESS`/`IP_ADDRESS`/`US_SSN`/`CREDIT_CARD`, not
just `PERSON` -- Presidio's own built-in recognizers for those types
independently re-detect the same substrings this project's own
`scan_regex()` already caught, any time NER runs on text that still
contains them. `detect_all` (naive) calls `scan_ner` on the full
original line every single time, so every regex-covered value picks up a
same-type overlapping duplicate hit from Presidio's built-in recognizer
on top of `scan_regex`'s own hit; `detect_all_field_gated`'s
`build_ner_candidate` excises exactly those spans before calling
`scan_ner`, so Presidio's built-in recognizers never see that text there
and can't produce the duplicate. This is not a production bug --
`src/service.py`'s real pipeline (line 257-259) already filters
`HIGH_ENTROPY` and calls `anonymize.dedup_spans()` before anything is
counted or audited, which collapses same-type overlapping spans down to
one and erases this exact artifact -- but it meant the verification
script's first pass was measuring raw, pre-dedup ensemble output instead
of what's actually audited, and needed the identical two-step filter
`service.py` uses to be a fair comparison. Fixed the same way, same day.

**Confirmed, second pass, matching `service.py`'s real pipeline exactly:**
every regex-covered type came back byte-identical between naive and
field-gated (as expected by construction once dedup is applied). PERSON
-- the only type that can actually differ -- came back naive=2,391 vs.
field-gated=2,346 on the local sample (a 1.9% gap, field-gated's PERSON
share 29.08% vs. naive's 29.47%), closely bracketing the live production
run's 29.90% PERSON share. All three numbers (local naive, local
field-gated, live production) cluster within about 1-3 percentage points
of each other -- direct, independently reproduced confirmation that the
893,150 exact match is a real consequence of recall parity between the
two detection strategies on this corpus, not evidence that field-gating
silently failed to engage in production, closing the question this
section originally left open.

**Throughput note, stated with the same hedging this project's own A/B
test (same session) earned the hard way:** 439.4 lines/sec end-to-end is
notably higher than the 2026-08-08 run's ~224 lines/sec. This is a
single, uncontrolled run against a different Docker Desktop session on a
different day -- image layer caching, host machine load, and general
run-to-run variance are all live confounds, and this project's own
order-controlled A/B test earlier this session found field-gated and
naive detection statistically indistinguishable at the algorithm level.
Do not cite 439.4 vs. 224 lines/sec as evidence that field-gating (or
anything else) made the pipeline faster without a controlled rerun
isolating the variable -- it is reported here as a data point, not a
conclusion.

---

## 17. AWS account IDs colliding with the CREDIT_CARD regex -- a real, systematic false-positive source found only by testing against genuinely real cloud log data

Found 2026-08-10, closing the last genuinely open real-data-validation
gap (ROADMAP item 10/11's own honest disclosure that windows_event and
cloudtrail had never been checked against real data, only synthetic).
Sourced 33 real Microsoft-Windows-Security-Auditing records
(`validation/real_data/datasets/WindowsEventSamples_raw.jsonl`) and 2,000
real flaws.cloud CloudTrail events (Summit Route's public 2020 release,
via `validation/real_data/prepare_cloudtrail_dataset.py`), extended
`inject_and_evaluate.py` with `build_windows_event_corpus()` and
`build_cloudtrail_corpus()`, and ran both through the same naive/
field-gated ensemble already validated against OpenSSH/Linux/
Thunderbird/OpenStack/Zookeeper.

windows_event (n=33, too small to be statistically meaningful on its own,
consistent with the already-documented flat-name NER weakness): naive
P=0.333 R=0.429 (TP=3 FP=6 FN=4); field-gated identical numbers --
field-gating engaged on all 33/33 lines but had zero effect on any single
prediction.

cloudtrail (n=2,000, a much larger and clearer signal): naive P=0.310
R=0.846 (TP=2079 FP=4627 FN=379); field-gated nearly identical, P=0.313
R=0.846 (TP=2079 FP=4562 FN=379). A precision collapse far worse than any
other real-data condition tested this project (OpenSSH FP=49, Linux
FP=122) -- and the fact that naive and field-gated came back almost
identical already ruled out anything in field-gating's excision logic as
the cause before this was even root-caused, pointing straight at the
shared regex layer both strategies call.

**Root cause, confirmed mechanically before anything was changed** (the
same "confirm the magnitude before trusting a plausible-sounding theory"
discipline Bug 9's fix required): wrote a dedicated, no-spaCy diagnostic,
`validation/real_data/diagnose_cloudtrail_false_positives.py`, and ran it
against the actual downloaded data rather than guessing. AWS account IDs
are always exactly 12 digits -- the low end of `src/detect.py`'s
`CREDIT_CARD` regex range, `\b\d{12,19}\b`. A real CloudTrail event's
`userIdentity.accountId` field, AND the same 12-digit account ID embedded
in the `arn` field (e.g. `arn:aws:iam::811596193553:user/Level6` --
colons satisfy `\b` the same way whitespace does), each independently
trigger a false `CREDIT_CARD` match. Confirmed live against all 2,000
real lines: 4,399 total 12-19-digit matches, of which 3,775 (85.8% of all
CREDIT_CARD-shaped matches; 81.6% of the naive run's total FP count of
4,627) were an exact match of a real `accountId` value present on that
same line. This is a genuine, structural collision between a real
cloud-native identifier format and this project's regex, not a diffuse
NER weakness -- the same "specific, mechanical, fixable cause" shape as
the `rhost=` dangling-key bug found earlier this project, just in the
regex layer instead of the field-extraction layer.

**What was explicitly ruled out first:** narrowing `CREDIT_CARD`'s regex
range from `\d{12,19}` to `\d{13,19}` would have "fixed" this in one
line. Checked whether that was actually safe before doing it: grepped
`src/generate_logs.py` and confirmed its `CREDIT_CARD_num` ground-truth
slot is `fake.credit_card_number()` (line 32) -- this project's own
synthetic-corpus generator, whose already-published CREDIT_CARD recall
numbers this project has cited all session. Ran `fake.credit_card_number()`
2,000 times with a fixed seed and inspected the digit-length distribution
directly: `[12, 13, 14, 15, 16, 19]` -- Faker legitimately produces
exactly-12-digit values for some card network formats. A blanket range
narrowing would have silently regressed this project's own already-
measured synthetic recall to fix a real-data problem -- exactly the kind
of "fix one number, quietly break another" mistake this document's own
review discipline exists to catch, so it was rejected.

**Fix:** a narrow, context-aware exclusion in `scan_regex()`
(`src/detect.py`), the same shape as the existing `_UUID_RE` exclusion in
`scan_entropy()` and the manual lookback pattern `build_ner_candidate`
already uses (Python's `re` module has no variable-length lookbehind, so
this is a manual "search the text immediately before the match" check,
not a true lookbehind assertion). A `CREDIT_CARD` match is suppressed
ONLY when it is exactly 12 digits long AND is immediately preceded by
either an AWS ARN's account-ID position (`arn:<partition>:<service>:
<region>:`, matched via `_AWS_ARN_ACCOUNT_ID_PREFIX_RE`) or a
`"accountId":`/`"recipientAccountId":` JSON key (`_AWS_ACCOUNT_ID_KEY_RE`).
Both context shapes are structurally specific enough that a real credit
card number could not coincidentally match either one. Critically, this
leaves untouched: every 13-19 digit match (AWS account IDs are never any
other length); any 12-digit match NOT in one of these two specific
contexts, including Faker's own synthetic `CREDIT_CARD_num` values in the
project's own synthetic corpus.

**Verified, not just asserted to work:** `validation/aws_account_id_credit_card_exclusion_test.py`
(new, wired into `tests/test_fast_validation.py` as
`test_aws_account_id_credit_card_exclusion`) checks, with no
Docker/spaCy/live data required: (1) the `accountId` JSON-key shape is
suppressed, using the real account ID pulled verbatim from the diagnostic
run's own confirmation; (2) the `arn`-field shape is suppressed, same
real account ID; (3) `recipientAccountId` (the other real CloudTrail
field name that carries a 12-digit account ID on some event types) is
also suppressed; (4) a bare 12-digit number with no AWS context at all is
still detected -- the direct regression guard for Faker's own synthetic
values; (5) a 16-digit number sitting immediately next to `arn:aws` text
is still detected -- AWS account IDs are never anything but exactly 12
digits, so length alone should never be swallowed by this exclusion; (6)
a 12-digit number following an unrelated JSON key (`transactionId`, not
`accountId`/`recipientAccountId`, no `arn`) is still detected, guarding
against the exclusion being loose enough to eat any quoted 12-digit value
near any key. First run caught a real bug in the fix itself before it
shipped: the initial `_AWS_ARN_ACCOUNT_ID_PREFIX_RE` mis-modeled the ARN
format with one extra required colon (assumed
`arn:aws:<service>:<region>::` when the actual format is
`arn:<partition>:<service>:<region>:<account>`, i.e. one fewer colon than
first assumed), which silently failed to match the real `arn:aws:iam::
811596193553:user/Level6` shape and left check (2) failing. Fixed by
correcting the prefix regex to `arn:aws[a-z0-9-]*:[^:]*:[^:]*:$`, matched
against the real string, all 6 checks passing. Full pytest suite
re-run after the fix: 54 passed, 2 skipped (up from the prior 53/2
baseline by exactly the one new test added here) -- no regression to any
other detection path.

**Re-confirmed against the live 2,000-line real dataset, same day.**
Rerunning `inject_and_evaluate.py`'s cloudtrail condition: **naive
precision 0.310 → 0.750 (FP 4627 → 692, TP/FN unchanged at 2079/379);
field-gated precision 0.313 → 0.754 (FP 4562 → 678).** The drop is
larger than the ~852-865 predicted from the diagnostic's own count alone
(4627 − 3775 = 852) -- consistent with the diagnostic having measured
only exact accountId-field-value matches per line, while the fix's
`_AWS_ARN_ACCOUNT_ID_PREFIX_RE` branch also suppresses the same ID's
second, separate occurrence embedded in the `arn` field on the same
line, which the diagnostic's simpler count did not separately attribute.
Recall is unchanged in both conditions (0.846), exactly as expected --
the fix only removes false positives in a narrowly scoped context, it
cannot affect true-positive detection anywhere. This is now, by a wide
margin, the largest single false-positive class fixed on any real-data
condition tested this project.

The remaining 692/678 false positives on cloudtrail are NOT further
investigated in this fix -- disclosed as a real, open gap, not implied
to be resolved. windows_event's own small-sample FPs (6 out of 33) were
also not investigated further this round -- deprioritized in favor of
the much larger, clearer cloudtrail signal, and still an open item if
windows_event's real-data sample is ever grown past its current n=33.

---

## 18. `docker compose --scale redact-service=N` OOM-killed OpenSearch -- gunicorn's per-container worker count silently multiplied by replica count

Found 2026-08-10, the first live run of `run_replica_and_queue_test.sh`
(ROADMAP item 12). Part A (`docker compose up --build -d --scale
redact-service=3`) OOM-killed OpenSearch (exit 137) before reconciliation
could complete.

**Root cause:** `Dockerfile`'s gunicorn `CMD` used a bare `--workers
$(nproc)` -- sized correctly for a *single* container (matched to
available CPU cores, standard sync-worker sizing for CPU-bound work), but
with no awareness of replica count at all. Each gunicorn worker imports
`service.py` independently after forking and warms its own full
spaCy/Presidio model copy (`Dockerfile`'s own pre-existing comment
already documented this for the single-replica case: `worker_count x
model-memory` at steady state). Scaling to 3 replicas multiplies that
same `nproc`-sized worker count by 3 -- so actual memory demand became
`3 x nproc x model-memory`, not `3x` a single replica's already-known,
already-sized footprint. On a host with, say, 8 cores, that's 24 total
gunicorn workers each holding a full NER model, competing with
OpenSearch's own `-Xms1g -Xmx1g` heap for whatever memory Docker Desktop
had allocated -- more than enough to explain an OOM kill.

**Worked around live, in the moment, not fixed:** retrying with
`--scale redact-service=2` succeeded (all containers reported healthy).
This reduced the multiplier from `3x` to `2x` but didn't address the
underlying issue -- the same multiplication is still present at any
replica count above 1, it just hadn't crossed the host's memory ceiling
at 2x yet.

**Fixed, same day:** `GUNICORN_WORKERS` is now a configurable environment
variable. `Dockerfile`'s `CMD` changed from `--workers $(nproc)` to
`--workers ${GUNICORN_WORKERS:-$(nproc)}` (falls back to the original
`$(nproc)` behavior if unset, so a single-replica, non-Compose deployment
of this image is unaffected). `docker-compose.yml`'s `redact-service`
environment block sets `GUNICORN_WORKERS=${GUNICORN_WORKERS:-2}` --
deliberately conservative (chosen to survive the OOM this project
actually hit, not benchmarked as an optimal throughput number) so `N`
replicas now cost `N x 2` total workers regardless of host core count,
not `N x nproc`. `run_replica_and_queue_test.sh` updated to print the
effective per-replica worker count at Part A's start and to document the
override (`GUNICORN_WORKERS=N ./run_replica_and_queue_test.sh`, or
reducing `--scale` to 2) for hosts that are still memory-constrained even
at the new default.

**Re-run live, same day -- the OOM is confirmed fixed, and a second, unrelated bug turned up immediately after it.** `--scale redact-service=3` with `GUNICORN_WORKERS` defaulting to 2 completed cleanly: all 3 replicas + OpenSearch reported healthy in ~5s (versus the OOM crash at the same scale before this fix), and reconciliation passed exactly -- `security-logs-anonymized-*` = 20,000, `security-logs-quarantine-*` = 0, `redact-audit-trail-*` = 17,819, `RECONCILIATION: PASS`. That's direct confirmation the memory fix itself works, not just a plausible theory.

Immediately after, the script's own per-replica distribution check killed the entire run silently: `docker compose logs redact-service | grep -oE '^[a-zA-Z0-9._-]+-redact-service-[0-9]+' | sort | uniq -c` matched zero lines against the real `docker compose logs` prefix format on the user's machine (exact cause not yet confirmed -- a Compose-version difference in how the log-line prefix is rendered is the leading suspect, not yet verified against the actual raw output), and with no `|| true` fallback on that pipe, `set -euo pipefail` aborted the whole script right there -- Part B never ran, as a direct, mechanical consequence of this one regex being too strict, not a real infrastructure problem. This is a small, script-level bug in the test harness itself, not in `redact-service`/`redact-lb`/anything shipped to users, but worth being just as honest about as any other bug in this document, since it's exactly the kind of silent-stop failure this project's own bug history has repeatedly flagged as the dangerous shape to miss. **Fixed**: the check is now unanchored (`grep -oE 'redact-service-[0-9]+'`, matches the container/replica identifier wherever it appears in the line rather than requiring it be the literal first token) and wrapped in `|| true` with a raw-log fallback dump, so a genuine zero-match result reports diagnosable data instead of silently ending the run.

**Both closed out by the next two live runs, not left open.** The second run (same day) got the distribution numbers this fix was written for: 6,658 / 6,711 / 6,745 requests across the 3 replicas -- genuinely even, direct confirmation `redact-lb` load-balances rather than an inference from reconciliation alone. That same run's Part B surfaced a separate, unrelated bug (double-processing every line -- see Bug 19 below), fixed there and re-confirmed clean on a third run. See Bug 19 and ROADMAP.md item 12 for the complete closing sequence.

---

## 19. `docker compose --profile queued up` didn't exclude the default synchronous pipeline -- both paths double-processed every line

Found 2026-08-11, the second live rerun of `run_replica_and_queue_test.sh`
after Bug 18's OOM and distribution-check fixes. Part A this run
confirmed cleanly on both fronts: `--scale redact-service=3` completed
with no OOM (Bug 18's fix holding), and the per-replica distribution
numbers came back genuinely even -- 6,658 / 6,711 / 6,745 requests across
the 3 replicas, direct, positive confirmation that `redact-lb`'s nginx
proxy is actually load-balancing, not just that reconciliation happens
to pass with a single working replica. Part B, run right after, failed:
`security-logs-anonymized-*` = 40,000 against an expected 20,000 --
`RECONCILIATION: FAIL`.

**Root cause:** Docker Compose's `profiles` mechanism only gates
opt-**IN** services -- a service tagged `profiles: ["queued"]` starts
only when that profile is explicitly active. It does NOT gate opt-**OUT**:
a service with no `profiles:` key at all always starts, regardless of
which `--profile` flags are passed on the command line. `docker-compose.yml`'s
`logstash` service (the default, always-on synchronous pipeline) has no
`profiles:` key -- so `docker compose --profile queued up ...` started
`logstash-queued` (correctly, via the profile) AND `logstash` (because it
has no profile gate to exclude it), side by side. Both independently
tail the exact same `data/raw` files this project's ingestion has always
used -- `run_replica_and_queue_test.sh`'s own header comment already
disclosed the *risk* ("both pipelines tail the same data/raw files
independently -- running both at once would double-process every line")
without the actual `docker compose` invocation ever enforcing it. Every
line got processed and written twice, exactly matching the observed
40,000 = 2 x 20,000.

**Fixed:** rather than adding a second Compose-profile mechanism (which
would need `logstash` to gain its own profile AND every existing
plain-`docker compose up` workflow -- the 10,000/100,000/1,000,000-line
load tests, all of README's and ROADMAP's already-verified runs -- to
start explicitly activating it, a much larger blast radius for a
one-line bug), Part B's `docker compose up` call now explicitly names
the services it wants (`opensearch redis redact-service redact-lb
logstash-queued queue-consumer`), omitting `logstash`. Compose only
starts services you name (plus their own `depends_on` dependencies) when
you list them on the command line, regardless of profile tags -- naming
is what actually excludes `logstash` here, not the `--profile queued`
flag alone (kept in the command for documentation/clarity, but proven
live, the hard way, not to be sufficient by itself). This leaves every
other existing `docker compose up` invocation in this project completely
unchanged -- Part A, the 10K/100K/1M load tests, and manual ad hoc runs
all still start `logstash` exactly as before.

**Re-confirmed live, 2026-08-11, third run of this script -- both parts now pass clean.** Part A: `RECONCILIATION: PASS` exactly at 20,000/20,000 again, distribution 6,653 / 6,803 / 6,652 across the 3 replicas (consistent with the prior run's 6,658/6,711/6,745 -- genuinely even both times, not a fluke). Part B, with `logstash` correctly excluded this time: `RECONCILIATION: PASS` exactly at 20,000/20,000 (not 40,000), queue depth 0 at the end (all 3 `queue-consumer` processes kept up with ingestion, nothing left un-popped). This closes ROADMAP item 12 completely -- multi-replica `redact-service` behind a real load-balancing proxy, and the queue-decoupled ingestion path, are both now genuinely confirmed working against live Docker infrastructure, not just "implemented, unit-checked."

---

## 20. CI's container-scan job was failing outright -- root cause was a real upstream supply-chain compromise, not a routine broken pin

Found 2026-08-11, the user noticed the CI badge going red and asked
about it directly rather than it being caught proactively -- worth
saying plainly, since every other bug in this document was found by
this project's own testing discipline, not by a user flagging a visibly
broken status badge first.

**Symptom:** `container-scan` failing at the very first step with
`Unable to resolve action aquasecurity/trivy-action@0.28.0, unable to
find version 0.28.0`. `fast-tests` and `redis-tests` were both green
(confirmed by checking the actual job logs on GitHub, not assumed) --
isolated entirely to the one step referencing this specific action.

**Root cause, and it's more serious than a stale version pin:** on
2026-03-19, `aquasecurity/trivy-action` suffered a real supply-chain
compromise (CVE-2026-33634 / GHSA-69fq-xp46-6x23) -- attackers force-pushed
a credential-stealing payload into every release tag except `v0.35.0`,
which GitHub's immutable-releases protection (enabled 2026-03-04, just
before that tag shipped) happened to keep intact. `0.28.0`, the tag this
workflow was pinned to, was one of the poisoned tags -- it stopped
resolving because it was pulled/quarantined after the compromise was
discovered, not because of an unrelated versioning mistake. That's
actually the fortunate outcome here: a resolvable-but-poisoned tag would
have silently run a credential stealer against this repo's CI secrets on
every push, with the scan still appearing to complete normally (per the
public incident writeups on this compromise) -- the loud failure is what
prevented that, not anything this project did to prevent it in advance.

**Also caught and corrected in the same pass:** this file's own earlier
comment (in `ci.yml` directly, added when the non-root Dockerfile
paragraph was corrected elsewhere in this same repo-cleanup session)
claimed `container-scan` "has since run against many real pushes to
main" -- stated without actually checking the Actions tab. It was wrong;
the job had been failing this whole time. Corrected in `ci.yml`'s own
comment, and noted here as a reminder that "this probably still works"
is exactly the kind of unverified claim this document's own discipline
exists to catch, including when the claim is about CI itself.

**Fixed:** the Trivy step is now pinned by its exact commit SHA
(`57a97c7e7821a5776cebc9bb87c984fa69cba8f1`, the commit backing the one
safe tag, `v0.35.0`) instead of a mutable tag name -- the standard
supply-chain-hardening practice for third-party GitHub Actions, and
specifically the one that would have prevented this exact class of
incident (a tag getting silently repointed to something malicious)
regardless of which tag had been chosen. `actions/checkout` bumped
`v4` → `v7` and `actions/setup-python` bumped `v5` → `v6` in the same
pass, clearing two "Node.js 20 is deprecated" warnings that showed up
alongside the real failure (both were warnings, not failures, but no
reason to leave them once the workflow file was already open for a fix).

**Not yet re-confirmed live** -- this needs the next real push to `main`
to show a green `container-scan` job on GitHub's actual runners, the
same "syntax-checked here, confirmed there" bar every other CI/Docker
change in this document is held to. `python3 -c "import yaml;
yaml.safe_load(...)"` confirms the file parses as valid YAML, which is
not the same thing as the workflow actually running clean.

**Incident scope, checked directly rather than assumed: this repo was never exposed.**
The user asked whether this needed reporting anywhere -- checked GitHub's
own run history instead of guessing. `container-scan`'s very first run
in this repo's history (run #1, commit `e9b3ee9`, pushed 2026-08-10 --
this workflow's own creation date) failed with the identical "Unable to
resolve action aquasecurity/trivy-action@0.28.0" error, 2 seconds in,
same as every run since. The poisoned tag was already pulled/quarantined
upstream by the time this workflow first existed, months after the
2026-03-19 compromise -- there was never a run, in this repo's entire
history, where the compromised code actually executed. Also checked:
`ci.yml` has no `secrets.*` references anywhere, no cloud credentials, no
deploy/publish tokens -- only the default job-scoped `GITHUB_TOKEN`,
which expires when each job ends -- so even a hypothetical successful run
would have had a small blast radius. Conclusion: no credentials to
rotate, no incident to disclose, nothing to report to GitHub or Aqua
Security (the compromise itself is already public: CVE-2026-33634 /
GHSA-69fq-xp46-6x23, already fixed upstream at v0.35.0). This was
defensive hardening against a known upstream incident this repo was
adjacent to via a broken pin, not a response to an actual breach here.

---

## 21. OpenSearch became a real 3-node cluster (ROADMAP item 12's last open piece) -- and a stray `git push` was found sitting inside a test harness along the way

**Impact:** N/A (feature closure, not a defect in previously-shipped
behavior). **Status:** Verified fixed, 2026-08-11 -- cluster formation
and full pipeline reconciliation both confirmed live (see addenda
below). ROADMAP item 12 is now closed on all three pieces (multi-replica
`redact-service`, queue-decoupled ingestion, and multi-node OpenSearch).

Closes the one item ROADMAP.md's item 12 had continued to list as
"explicitly out of reach" even after the multi-replica/queue-decoupling
work in Bugs 18-19 was confirmed live: `docker-compose.yml`'s single-node
`opensearch` service is now a real 3-node cluster,
`opensearch-node1`/`opensearch-node2`/`opensearch-node3`, using
OpenSearch's current cluster-formation settings (`cluster.name`,
`node.name`, `discovery.seed_hosts`,
`cluster.initial_cluster_manager_nodes` -- verified against OpenSearch's
own current documentation, not assumed from Elasticsearch's differently
named legacy settings). 3 nodes, not the 2 shown in OpenSearch's own
official multi-node example
(`raw.githubusercontent.com/opensearch-project/documentation-website/main/assets/examples/docker-compose.yml`,
fetched 2026-08-11): a 2-node cluster can't demonstrate a real
cluster-manager election under a node failure, since two participants
can't break a 1-1 tie without a third voting member.

**Deliberate deviation from OpenSearch's own official example, disclosed
rather than silently matched:** that example enables the security plugin
with self-signed certs and a default admin password. This project kept
`DISABLE_SECURITY_PLUGIN=true` on every node instead, for consistency
with the single-node service it replaces and to avoid reintroducing a
self-signed-cert trust problem for Logstash's and `queue_consumer.py`'s
plain-HTTP clients -- both of which this project has deliberately avoided
everywhere else. Same demo-scope caveat as before applies (see that
flag's own comment in `docker-compose.yml`): not for real PII-bearing
data.

**Downstream changes needed to actually use the cluster, not just stand
it up:**
- `logstash/redact-pipeline.conf`'s three `opensearch` output blocks
  (quarantine, audit-trail, anonymized) now list all three node hosts
  directly (`hosts => ["http://opensearch-node1:9200", ...]`) instead of
  interpolating a single `OPENSEARCH_HOST` env var -- the
  `opensearch-output` plugin distributes/fails over across a hosts list
  itself, so this gives the pipeline real resilience to one node's
  failure, not just resilience at the storage layer while the client
  stays a single point of failure pointed at one node.
- **Found and fixed as a side effect of touching this code:** the old
  single-value default in those three blocks was `"https://opensearch:9200"`
  (https) even though `docker-compose.yml`'s `OPENSEARCH_HOST` always set
  `http` explicitly, so that default fallback value had never actually
  been exercised -- a latent, harmless inconsistency, but a stale default
  is still a stale default, and this project doesn't leave those in place
  once found (see Bugs 7 and 16 for others caught the same way).
- `src/queue_consumer.py`'s `index_document()` (the queued path's direct
  HTTP-PUT writer) now takes a comma-separated `OPENSEARCH_HOSTS` list and
  tries each host in order, moving on only on a connection-level failure
  (a node down or unreachable), not on an OpenSearch-level error response
  every node would return identically. Disclosed, real limitation of this
  specific fix: it's ordered failover, not load distribution -- every
  healthy write goes to `opensearch-node1` by default, so that node
  carries disproportionate write traffic day to day. A hash-based
  round-robin would spread load more evenly but would also make "which
  node took this write" non-deterministic when debugging a failed run --
  judged not worth that tradeoff while this path is still unverified
  against live infrastructure. Revisit if node1 specifically becomes a
  throughput bottleneck under real load.
- Node1's healthcheck was strengthened from "is OpenSearch responding" to
  `grep -q '"number_of_nodes":3'` against `/_cluster/health` -- catches
  the specific failure mode this item exists to guard against (node1
  comes up fine standalone but never actually forms a cluster with
  node2/node3, e.g. from a bad `discovery.seed_hosts` value or a Docker
  network issue), which a plain "node1 is responding" check would miss
  entirely. `logstash` and `queue-consumer`'s `depends_on` blocks now gate
  on `opensearch-node1: condition: service_healthy` specifically, so the
  rest of the stack won't start ingesting against a cluster that isn't
  really a cluster yet.
- `run_replica_and_queue_test.sh` and `validation/load_test/run_load_test.sh`
  both polled `docker compose ps opensearch | grep -q "healthy"` to detect
  startup -- that service name no longer exists. Both updated to poll
  `opensearch-node1` instead, matching the stronger 3-node healthcheck
  above. `docker compose ps opensearch` would otherwise have exited
  non-zero on every poll, and `run_replica_and_queue_test.sh` runs under
  `set -euo pipefail`, so this would have killed the script silently on
  its very first healthcheck loop -- the same class of `set -e`-plus-
  unanchored-check failure mode Bug 18's addendum already found once in
  this same script.

**Unrelated, but found while reading through this script line by line to
make the above fix:** `run_replica_and_queue_test.sh` had a bare
`git push origin main` sitting right after `set -euo pipefail`, before
anything else runs. Nothing in the script's own header comment describes
or explains it, and a load-testing harness has no legitimate reason to
push the repo's current state to `origin/main` as a side effect of
running a local Docker Compose test -- worst case, it could push a
work-in-progress commit that happened to be checked out locally at
whatever moment someone ran this script. Removed. Flagged here rather
than silently dropped, in case it was intentional and this write-up is
the first place anyone notices it's gone.

**Not yet verified against a live 3-node run in this sandbox** -- no
Docker daemon here, same disclosure as every other Docker Compose claim
in this project until it's actually been run. `docker-compose.yml`
parses as valid YAML (`python3 -c "import yaml; yaml.safe_load(open('docker-compose.yml'))"`),
`src/queue_consumer.py` compiles clean and its own unit tests
(`tests/test_queue_consumer.py`, which patch `index_document()` at the
function boundary rather than depending on its internal host-list logic)
still pass unmodified, and both edited shell scripts pass `bash -n`.
None of that substitutes for actually bringing the cluster up and
confirming `number_of_nodes:3`, a clean reconciliation run through it,
and (ideally) killing one node mid-run to confirm the cluster survives
losing a member -- the specific behavior this item exists to demonstrate,
not just the config that should produce it.

**First live run, 2026-08-11 (the user's machine) -- cluster formation
confirmed, one real environmental gotcha found and fixed along the way.**
`docker compose up --build -d` initially failed with `Error response from
the daemon: ... Bind for 0.0.0.0:9200 failed: port is already allocated`,
and the first successful-looking `_cluster/health` poll came back
`"number_of_nodes": 1`, not 3. Root cause, confirmed from the run's own
output: `docker compose down -v` (without `--remove-orphans`) only stops
containers for services still *defined* in the current compose file --
since the single-node `opensearch` service no longer exists under that
name, the container from the *previous* version of this file
(`redact-opensearch`) was left running as an orphan, still bound to host
port 9200. The new `opensearch-node1` couldn't bind that same port, so it
never started -- but the old orphan container answered `_cluster/health`
requests on port 9200 in its place, reporting itself as a healthy
single-node cluster, which is exactly the misleading "looks like it
worked, actually didn't" shape this document exists to catch. **Not a bug
in the new 3-node config itself** -- a Compose lifecycle gotcha specific
to renaming a service across a `docker compose down`/`up` cycle, not
something `docker-compose.yml`'s own contents could have prevented.
Fixed by running `docker compose down -v --remove-orphans` (plus an
explicit `docker rm -f redact-opensearch` and `docker network prune -f`
as a belt-and-suspenders cleanup) before the next `up`. **Confirmed
clean after that fix:** `_cluster/health` returned `"cluster_name":
"redact-cluster"`, `"status": "green"`, `"number_of_nodes": 3`,
`"number_of_data_nodes": 3`, `"active_shards_percent_as_number": 100.0`
-- a green status specifically requires every node to see every other
node and every shard to have its replicas assigned, so this is a real,
positive confirmation of 3-node cluster formation, not just three
containers that happen to be running side by side. Full pipeline
reconciliation against this cluster (`run_replica_and_queue_test.sh`)
was kicked off immediately after and is expected to close this item
completely, matching the standard Bugs 18/19 were held to before ROADMAP
item 12's other two pieces were called done.

**Full pipeline confirmed against the live 3-node cluster, same session,
2026-08-11 -- both parts PASS clean, this item closed completely.**
`run_replica_and_queue_test.sh` run against the now-healthy 3-node
cluster (no orphan-container interference this time). **Part A**
(multi-replica `redact-service` behind `redact-lb`): all 3 replicas +
`opensearch-node1` reported healthy in ~5s, `RECONCILIATION: PASS`
exactly 20,000/20,000, per-replica gunicorn request distribution
6,683/6,703/6,730 -- genuinely even, consistent with the two prior
single-node-OpenSearch runs' own distribution numbers (6,658/6,711/6,745
and 6,653/6,803/6,652), confirming the load-balancing behavior itself is
unaffected by the storage layer underneath it, as expected since they're
independent concerns. **Part B** (queue-decoupled ingestion,
`logstash-queued` -> Redis list -> 3x `queue-consumer` ->
`OPENSEARCH_HOSTS` failover list -> OpenSearch): `RECONCILIATION: PASS`
exactly 20,000/20,000, queue depth 0 at the end -- direct confirmation
that `queue_consumer.py`'s new `OPENSEARCH_HOSTS` failover list (this
item's replacement for the old single-value `OPENSEARCH_HOST`) writes
correctly against the 3-node cluster, not just that the config parses.
Both parts' `redact-audit-trail-*` count landed at 17,819 -- identical
between Part A and Part B, as expected from the same seeded corpus and
deterministic fan-out logic, and consistent with this project's
established pattern of that count varying by corpus content/dirty-ratio
rather than by which infrastructure path produced it. **What this run
does not cover, stated plainly rather than implied:** it did not include
killing a node mid-run to confirm the cluster tolerates losing a member
-- both parts ran against all 3 nodes healthy throughout. That remains
the one piece of this item's original scope ("does the cluster actually
survive losing a node, not just start with 3") not yet exercised; a
reasonable follow-up test, not required to call the feature itself
closed, since ROADMAP item 12's own stated goal was closing the "not a
real cluster" gap, not building out full chaos-engineering coverage.

---

## 22. Redis became a real master/2-replica/3-sentinel topology -- two disclosed client-side gaps, not yet tested against a live failover

**Impact:** N/A (feature closure, not a defect in previously-shipped
behavior). **Status:** Sentinel-level failover confirmed live, 2026-08-11
-- see Bug 23 for the fix this took and the confirmed run. Client-level
behavior under live traffic (`queue_consumer.py`'s reconnect path,
`logstash-queued`'s disclosed limitation reproducing as predicted) is
the one piece of this item still unexercised -- see Bug 23's own closing
paragraph.

The user asked to work through ROADMAP.md's remaining "explicitly out of
reach" items after the OpenSearch multi-node closure (Bug 21). Of the
three named there -- a real Kafka cluster, a real cloud load balancer,
and terabytes/day volume -- the user confirmed (via direct question) they
don't currently have a cloud account set up, so the cloud-dependent two
were deferred rather than started (account/billing setup is its own
multi-session effort, not something to begin mid-task on someone else's
behalf). The Kafka item was re-examined instead of attempted as
originally scoped: this project never actually adopted Kafka (see
`logstash-queued`'s own comment in `docker-compose.yml` -- the official
`logstash-output-redis` plugin doesn't support Streams/XADD, and an
unverified third-party plugin was rejected given this project's own Bug
16), so "a real Kafka cluster" was a hypothetical comparison point, not
a real gap in the technology this project ships. What actually needed
closing was HA for the message-queue backend this project DOES use: a
single-node Redis.

**What changed:** `docker-compose.yml`'s single-node `redis` service is
now `redis-master` + `redis-replica-1`/`redis-replica-2` (async
replication, `--appendonly yes`) + `redis-sentinel-1/2/3` (quorum 2,
`resolve-hostnames`/`announce-hostnames yes` so Sentinel tracks Compose's
stable service hostnames rather than container IPs -- see
`redis/sentinel.conf`'s own comment for why each of the 3 sentinel
containers copies a shared read-only template into its own writable path
rather than three processes sharing one host-mounted config file).
`RedisStorageProvider` (`src/anonymize.py`) gained optional
`sentinels`/`sentinel_service_name` constructor parameters using
redis-py's own `Sentinel` client; `src/queue_consumer.py` gained an
equivalent `REDIS_SENTINELS`/`REDIS_SENTINEL_MASTER_NAME` env-var-driven
path. Both fall back to their prior fixed-host connection behavior when
Sentinel isn't configured, so neither
`validation/redis_storage_provider_test.py`,
`validation/multiprocess_redis_test.py`, nor CI's `redis-tests` job
(all of which connect to a plain single-instance `redis:7` service
container) needed any changes -- confirmed by re-running the full local
suite unmodified (54 passed / 2 skipped) after these edits.

**Two disclosed, real limitations found while implementing this, not
discovered by accident and not glossed over:**

1. **`logstash-queued`'s producer side has no Sentinel awareness.** The
   official `logstash-output-redis` plugin's `host` config takes a fixed
   hostname (or a list, for round-robining across independent shards --
   not the same mechanism as Sentinel-style master discovery), with no
   built-in way to ask Sentinel "who's the current master." Pointed at
   `redis-master` directly, matching the topology's naming, but if
   `redis-master` genuinely fails and Sentinel promotes a replica, this
   producer keeps retrying the now-dead hostname rather than picking up
   the new master -- Logstash's own reconnect/retry logic just keeps
   failing against a host that no longer accepts writes. `queue_consumer.py`
   on the other side of this same queue does not have this limitation,
   since it uses the Python `redis` client's own Sentinel support
   directly rather than a Logstash output plugin.

2. **A real correctness window around the failover moment itself, in
   `RedisStorageProvider.lock_for_save()`.** This lock's original
   correctness argument (see its own comment, `src/anonymize.py`) relied
   on single-node Redis's inherent linearizability -- true regardless of
   this change, for any SINGLE master at a time. What changes: Redis's
   default asynchronous replication means a lock (or a completed,
   already-released save) on the master immediately before it fails is
   not guaranteed to have already reached whichever replica Sentinel
   promotes next. Two concrete failure windows follow from this, neither
   fixed by anything in this class: a lock that vanishes on the newly
   promoted master while the original holder's save() is still in
   flight (letting a second process acquire what looks like a fresh
   lock), and unreplicated writes from immediately before the failure
   being lost the same way any unreplicated write to a failed master is
   lost. This is the standard, well-documented tradeoff of Sentinel's
   asynchronous replication model, not something unique to this
   implementation, and not something a smarter lock alone fixes -- full
   Redlock (spanning multiple independent masters, not one master with
   async replicas) is the documented next step if this specific race
   ever needs closing. Not implemented here; disclosed instead, the same
   standard this project already applied to BLPOP's own non-redelivery
   gap and the ordered-vs-load-balanced OpenSearch failover tradeoff in
   Bug 21.

**Not yet verified against a live failover in this sandbox** -- no
Docker daemon here, same disclosure as every other Docker Compose claim
in this project until it's actually been run. `docker-compose.yml`
parses as valid YAML, `src/anonymize.py` and `src/queue_consumer.py`
both compile clean, the `REDIS_SENTINELS` env-var parsing logic was
checked directly (`python3 -c "..."`, confirming both the populated and
empty-string cases produce the expected `[(host, port), ...]` list or
`[]`), and the full local test suite still passes unmodified. None of
that substitutes for actually bringing the topology up, killing
`redis-master` mid-run, and confirming Sentinel promotes a replica while
the Python consumer/storage-provider paths keep working through it (and
that the Logstash producer's disclosed limitation reproduces exactly as
predicted, not some different failure mode that would mean this writeup
is wrong about the mechanism). Handed off to the user as the next live
test, following the same "implemented and reasoned about here,
confirmed there" pattern every other Docker-dependent claim in this
document has been held to.

---

## 23. Sentinel's hostname-based master tracking broke failover outright -- Docker deregisters a dead container's DNS name, so Sentinel got stuck unable to ever announce the promotion

**Impact:** High for the feature this item exists to demonstrate (Redis
HA is not actually HA if failover never completes) -- zero impact on
anything shipped before this session, since Bug 22's Sentinel topology
was brand new and had not yet been claimed working. **Status:** Verified
fixed, 2026-08-11 -- confirmed via a live failover on the user's machine
(see addendum below for the completed run).

**Found during the very first live failover test of Bug 22's new
Redis Sentinel topology.** The user brought the cluster up, confirmed
`sentinel master redact-master` showed a healthy master with
`num-slaves:2`, then ran `docker kill redact-redis-master` to simulate a
real master failure -- the actual test this whole item exists to prove
out, not a config-parsing check. 15 seconds later,
`sentinel get-master-addr-by-name redact-master` still reported the
dead master's original address. `docker compose logs redis-sentinel-1`
showed why: `Failed to resolve hostname 'redis-master'`, repeating once
per second, continuously, for the full minute-plus the user let it run.

**Root cause:** `redis/sentinel.conf` originally used `sentinel
resolve-hostnames yes` plus `sentinel monitor redact-master redis-master
6379 2` -- tracking the master by its Compose *service* hostname rather
than a fixed address. That looked like the more robust choice going in
(a hostname should survive a container restart better than a
dynamically-assigned IP), but it has a fatal interaction with how Docker
Compose actually implements service-name DNS: `redis-master` is only
resolvable while at least one container backing that service is running
and registered with Docker's embedded DNS (127.0.0.11). The instant
`redact-redis-master` was killed, that DNS entry disappeared --
immediately, not after some grace period. Sentinel's `resolve-hostnames`
setting requires it to periodically re-resolve its monitored master's
configured hostname to keep its tracked address current (this is
documented Redis 6.2+ behavior, not a bug in Redis itself), and once
that re-resolution started failing, Sentinel got stuck unable to
validate or rewrite the master's address at all -- which blocked it from
completing the failover it had otherwise correctly detected was needed
(SDOWN detection itself, based on missed PINGs to the already-known IP,
is independent of hostname resolution and was not the blocked step;
what got stuck was the address-tracking/announcement machinery
`resolve-hostnames` gates).

**Fix:** stopped relying on DNS entirely for the address that needs to
survive its own container's death. `docker-compose.yml`'s `redact-net`
network gained an explicit subnet (`172.28.0.0/16`), and
`redis-master`/`redis-replica-1`/`redis-replica-2` each got a fixed
`ipv4_address` (`.240`/`.241`/`.242`) pinned near the top of that range
to avoid Compose's own dynamic allocation (which starts from the low
end). `redis/sentinel.conf` now monitors `172.28.0.240` directly and
sets `resolve-hostnames no` / `announce-hostnames no` -- both settings
exist specifically to translate between hostnames and IPs, which is no
longer relevant once the monitored target is a fixed IP that was never a
DNS entry Docker could deregister. `redis-replica-1`/`redis-replica-2`'s
own `--replicaof redis-master 6379` startup command was deliberately
LEFT as a hostname, not changed to match -- that command is resolved
once at container startup while `redis-master` is guaranteed to be up
(gated by `depends_on: redis-master: condition: service_healthy`), not
continuously re-resolved the way Sentinel's own address tracking is; when
Sentinel actually promotes a replica, it does so by sending that replica
a direct `REPLICAOF` command over an already-open connection, bypassing
this startup-time command entirely. Only Sentinel's own long-lived,
continuously-re-validated address tracking needed the fixed-IP fix.

**Confirmed live, same session, after the fix.** Re-ran the exact same
test against the fixed topology: bring the stack up, confirm
`sentinel master redact-master` shows a healthy master with
`num-slaves:2`, `docker kill redact-redis-master`, poll
`get-master-addr-by-name`. This time it returned `172.28.0.241` --
`redis-replica-1` -- a genuinely different address from the killed
master's `172.28.0.240`, direct confirmation the promotion actually
happened rather than the previous run's stuck state. `redis-sentinel-1`'s
own log gives the complete, clean sequence: `+sdown master redact-master
172.28.0.240 6379` -> `+new-epoch 1` -> `+vote-for-leader ...` ->
`+config-update-from ...` -> `+switch-master redact-master 172.28.0.240
6379 172.28.0.241 6379` -> the surviving replica (`172.28.0.242`)
immediately reparented to follow the new master -> the old master,
still down, correctly marked `+sdown slave` once Sentinel tried to
reach it as a demoted replica too. From `+sdown` to `+switch-master`
completing was under one second (`13:43:22.803` to `13:43:23.355`) --
`down-after-milliseconds` (5000ms) governs how long Sentinel waits
before DECLARING the master down in the first place, not how long the
subsequent election/promotion takes once it has; the whole sequence
finished well inside the 10s `failover-timeout`. **This confirms the
core claim ROADMAP item 12's Redis-HA piece exists to demonstrate --
Sentinel genuinely promotes a replica on real master failure, using this
project's own topology, not just on paper.** Worth stating plainly: this
bug was found by actually running the failover this item exists to
demonstrate, on the first attempt, not caught by any of the syntax/logic
checks this project otherwise relies on before a live run
(`docker-compose.yml` parsed as valid YAML the whole time; nothing about
`resolve-hostnames yes` was itself invalid config, it was just wrong for
this specific environment's DNS lifecycle) -- another entry in this
document's own running argument that some classes of bug are only
findable by triggering the actual failure mode, not by inspection.

**Not yet exercised, disclosed rather than implied covered:** this test
confirmed Sentinel's own promotion mechanics, at the Redis level, with
no client traffic in flight. It did NOT yet confirm the two client-side
pieces Bug 22 added on top of this: whether `queue_consumer.py`'s
`REDIS_SENTINELS` connection-error/retry loop actually resumes cleanly
against the newly promoted master while events are actively being
processed (rather than just parsing correctly, which is all that's been
checked so far), and whether `logstash-queued`'s disclosed
non-Sentinel-aware limitation reproduces exactly as predicted (keeps
retrying the dead master's fixed IP rather than failing over). A
reasonable follow-up -- run `run_replica_and_queue_test.sh`'s Part B and
kill `redact-redis-master` partway through -- not required to call this
specific bug (Sentinel's own failover mechanics) closed, since that's
what this entry was scoped to.

---

## 24. queue_consumer.py crashed outright under a live Sentinel failover -- caught the wrong exception type, verified against real production traffic, not assumed from the library docs

**Impact:** High under the exact scenario this feature exists for --
every `queue-consumer` replica crashed and stayed dead, permanently
stranding whatever was already queued (47,694 of ~47,808 remaining
events in this test) until manually restarted. Zero impact on the
synchronous (non-queued) pipeline, which doesn't touch this code path
at all. **Status:** Verified fixed, live, same scenario, same day (see
addendum below).

**Found by the user's `run_redis_failover_test.sh` run -- the follow-up
test explicitly written after Bug 23 to check whether
`queue_consumer.py`'s Sentinel reconnect logic actually holds up under
real BLPOP traffic, not just when idle.** It didn't. The run's own
"predicted vs. genuine problem" summary (baked into that script after
Bug 23, on the theory that a partial reconciliation total needed
context to interpret correctly) called this exactly: queue depth stuck
at 47,694 and never draining, reconciliation frozen at 12,306 for the
full 3-minute post-kill poll window, and all three `queue-consumer-N`
containers' logs showing a full, uncaught Python traceback ending in
`redis.exceptions.TimeoutError: Timeout connecting to server` -- not the
"Redis connection/timeout error (retrying)" message this script's own
`except` clause should have printed if it had actually caught anything.

**Root cause, confirmed directly rather than assumed from the redis-py
docs:**

```
>>> import redis.exceptions as e
>>> issubclass(e.TimeoutError, e.ConnectionError)
False
>>> e.ConnectionError.__mro__
(ConnectionError, RedisError, Exception, BaseException, object)
>>> e.TimeoutError.__mro__
(TimeoutError, RedisError, Exception, BaseException, object)
```

`main()`'s retry loop (added alongside Bug 22's Sentinel support) only
caught `redis.exceptions.ConnectionError`. `TimeoutError` is a SIBLING
of `ConnectionError` in this library's exception hierarchy -- both
inherit directly from `RedisError`, neither from the other -- so a
timeout connecting to the just-promoted (correctly reachable) new
master was never caught by that clause. It propagated all the way out
of `main()` uncaught, which is exactly what killed every
`queue-consumer` replica outright rather than letting the retry logic
do its job. Sentinel's own promotion had already completed correctly
and fast (confirmed independently, Bug 23's own log evidence) -- this
was purely a gap in this script's exception handling, not a Sentinel or
Redis-side failure. Worth naming plainly: this is the exact class of
mistake that "verified against the library's stated behavior" doesn't
catch -- both exceptions are individually well-documented, but their
NOT sharing an inheritance relationship is the kind of specific detail
that's easy to get wrong writing the `except` clause from general
familiarity with the library rather than actually checking.

**Fix:** `except (redis.exceptions.ConnectionError,
redis.exceptions.TimeoutError) as exc:` -- both exception types now
caught explicitly, not relying on an inheritance relationship that
doesn't exist. Also added explicit `socket_connect_timeout=5,
socket_timeout=10` to both the `Sentinel(...)` client and
`master_for(...)` call: without a bound, a connection attempt to a
still-unreachable address can hang for a long time at the OS/TCP level
(Linux's default connect-timeout backoff can run well past a minute)
before redis-py raises anything at all for the `except` clause to
catch -- a related, disclosed hardening found worth doing at the same
time, not a separate confirmed bug on its own (the live test's timeline
didn't isolate exactly how much of the delay before the crash was this
specific cause versus other factors, so this is stated as a reasonable
precaution rather than a second confirmed root cause).

**Confirmed live, same day, exact same test re-run against the fix.**
Fresh 60,000-line corpus, kill triggered at ~20% processed (12,508
events in), `docker kill redact-redis-master`. This time all three
`queue-consumer` logs showed exactly the intended behavior instead of a
crash: `Redis connection/timeout error (retrying): Error 111 connecting
to 172.28.0.240:6379. Connection refused.` immediately followed by
`Redis connection/timeout error (retrying): Timeout connecting to
server`, then normal `BLPOP` polling resumed with no further errors --
the anonymized count climbed steadily from 12,508 to 38,670 across the
3-minute post-kill observation window at roughly the pre-kill rate,
confirming this wasn't a fluke recovery. Left running afterward, the
queue continued draining on its own (21,238 → 7,048 remaining across two
manual checks a few minutes apart) and **`validation/load_test/reconcile.py`
eventually reported a clean `PASS`, exactly `60000/60000`, zero events
lost** -- full recovery from a real master failure under live traffic,
with the bug that would have prevented it now closed.

**Root cause confirmed with certainty**, unrelated to the live
re-confirmation above: checked directly against the installed `redis`
package's actual class hierarchy in this sandbox, not assumed from
documentation or memory, and it exactly explains the original crash
symptom (an uncaught traceback matching the exact exception type this
check confirms was never caught).

**Addendum: the disclosed producer-side limitation (`logstash-queued`
having no Sentinel awareness, see this item's own docker-compose.yml
comment and Bug 22) was NOT actually exercised by either failover test
run, stated plainly rather than left ambiguous.** Grepping
`logstash-queued`'s full log for any Redis-related activity around the
kill found nothing -- no write attempts, no errors, nothing but its own
startup message. Working theory, and the arithmetic supports it:
`logstash-queued`'s file-tailing + `RPUSH` job is far faster than the
downstream anonymization pipeline it feeds, so by the time either test's
kill-trigger fired (timed against *processing* progress, i.e. 20% of
events fully anonymized), `logstash-queued` had very likely already
finished reading and enqueueing the *entire* corpus -- the post-kill
queue-depth-plus-processed arithmetic came out to within a rounding
margin of the full 60,000-line corpus both times, consistent with
production having completed well before the kill rather than being
interrupted by it. This means the producer-side gap remains reasoned
directly from the official `logstash-output-redis` plugin's documented
lack of Sentinel support (an architectural fact, not something that
needs empirical reproduction to be credible) but has not been watched
failing live the way Bug 24's consumer-side crash was. A test actually
designed to catch `logstash-queued` mid-write -- triggering the kill
against ingestion progress rather than processing progress, or on a
corpus large enough that ingestion itself takes several minutes -- is a
reasonable further follow-up, not undertaken here since the consumer-side
bug this test was built to find (and did find) is now closed, and the
producer-side claim was never resting on an unverified assumption to
begin with.

---

## Engineering upgrade 1: Aho-Corasick for Layer 4's dictionary scan (2026-08-11)

**Not a bug — a measured performance change**, done as part of a broader batch of
improvements suggested by an external review of REDACT against commercial
alternatives (Presidio, Nightfall). Most of that review's suggestions were
evaluated and several were rejected or descoped as overstated or mismatched
to this project's actual scale (see `PROJECT_STATUS.md` for the full
critique); this one held up.

**What changed:** `src/flattened_names.py`'s `_segment_match()` originally
checked every split point of a candidate token with a Python-level loop
(up to ~24 iterations for a 30-character token) and two set-membership
checks per split. Replaced with a single Aho-Corasick automaton
(`pyahocorasick`) built once at import time over the union of
`FIRST_NAMES`/`LAST_NAMES`: one linear scan of the token finds every
dictionary-word substring occurrence in a single pass, and a lightweight
adjacency check (does some match start at index 0 and another end at the
token's length, with compatible first/last roles and no gap between them)
replaces the manual split loop. This is the textbook Aho-Corasick
application — matching a fixed dictionary against input text in one pass.

**Verified, not assumed:** `validation/aho_corasick_layer4_verify.py`
reimplements the original split-loop algorithm verbatim for direct
comparison, then runs both implementations across the full 10,000-line
canonical synthetic corpus (`data/synthetic_logs.jsonl`). Result: **0
mismatches across all 10,000 lines**, 1,013 hits from both implementations,
byte-identical output. This is a real correctness guarantee, not an
assumption that the rewrite preserves behavior.

**Throughput, measured honestly:** 57,134 lines/sec (old) -> 66,757
lines/sec (new), a **1.17x speedup** on this sandbox's hardware. This is
real but modest — nowhere near the "up to 4x" figure the external review's
ONNX-quantization suggestion claimed for a different part of the pipeline
(and that figure was never adopted here precisely because it wasn't
backed by a measurement against REDACT's actual architecture; see the
ONNX spike task in `PROJECT_STATUS.md`). At this token-level scale, the
original split-loop's per-iteration cost was already small (set lookups
are O(1)), so Aho-Corasick's real win is structural — one automaton pass
instead of a Python-level loop with repeated set lookups — rather than a
change in asymptotic complexity that would show up as a dramatic number
at this input size. Worth keeping regardless of the modest measured
speedup, since it removes a manual loop in favor of a well-tested library
primitive built exactly for this problem shape.

---

## Engineering upgrade 2: drift detector wired to Prometheus/Alertmanager (2026-08-11)

Second item from the same external-review batch as Engineering upgrade 1.
The review's framing ("Presidio and Google Cloud DLP treat redaction as a
silent utility — REDACT can win commercially by making telemetry
sanitation a core security visibility tool") was itself dismissed earlier
as marketing framing this project doesn't need (see `PROJECT_STATUS.md`'s
critique), but the concrete underlying suggestion — actually alert on
drift instead of leaving it as a script someone has to remember to run
and read — was real and cheap, and REDACT already had every piece except
the wiring.

**What existed before this:** `drift.py`'s `field_stats()`/`compare()`
were correct (Bug 11 fixed the flattened-username blind spot in this
exact code) but only ever ran as a CLI script or inside the weekly
Airflow DAG's `check_taxonomy_drift` task — the result went into an
Airflow task log and nowhere else. `service.py` already exports
Prometheus metrics (`redact_anonymize_request_seconds`,
`redact_detections_total`, etc.), but nothing scraped them, and no
Prometheus/Alertmanager/Pushgateway infrastructure existed anywhere in
this repo.

**What changed:**
- `src/drift.py`: added `compare_all()`, a sibling to `compare()` that
  returns every sufficiently-sampled field's current rate (not only
  fields that crossed the threshold), each carrying its own `flagged`
  boolean computed via the exact same arithmetic `compare()` uses.
  Deliberately a new function, not a change to `compare()`'s return
  shape — `compare()` is the CLI report's existing contract.
- `src/airflow_tasks.py`: `check_taxonomy_drift` now also returns
  `all_field_detail` (from `compare_all()`). New
  `push_drift_metrics_to_prometheus(result, pushgateway_url, job)`
  pushes two gauges per `(log_type, field)` to a Prometheus Pushgateway —
  `redact_drift_field_critical_hit_rate` (always) and
  `redact_drift_field_flagged` (1/0, mirrors `compare()`'s own decision
  exactly — the Alertmanager rule fires on this value directly rather
  than re-deriving a threshold in PromQL, keeping exactly one
  implementation of "what counts as drift," the same principle
  `service.py`'s own docstring states for the HMAC/token-store logic).
  Deliberately a no-op (not an error) when no Pushgateway is configured —
  the default in every environment this project has run in. A thin
  `push_drift_metrics_task` wrapper pulls the XCom result via explicit
  `ti.xcom_pull()` inside the Airflow task context, rather than the more
  fragile route of templating `"{{ ti.xcom_pull(...) }}"` directly into
  `op_kwargs` — that only preserves the dict's real type if the DAG sets
  `render_template_as_native_obj=True`, which this DAG does not, so the
  templated route would have silently passed a stringified dict instead
  of the real object.
- `dags/redact_weekly_validation.py`: new `push_drift_metrics_to_prometheus`
  task wired in right after `check_taxonomy_drift`, reading
  `REDACT_PUSHGATEWAY_URL` from the environment (unset by default, so the
  DAG's existing behavior is unchanged unless someone deliberately
  configures a Pushgateway).
- `docker-compose.yml`: new `monitoring` profile (opt-in, same pattern as
  the existing `queued` profile) adding `pushgateway`, `prometheus`, and
  `alertmanager` containers.
- `monitoring/prometheus.yml`, `monitoring/alert_rules.yml`,
  `monitoring/alertmanager.yml`: scrape config (Pushgateway +
  `redact-service`'s own `/metrics`), the actual `RedactFieldDriftDetected`
  and `RedactServiceHighLatency` alert rules, and a deliberately-stub
  Alertmanager receiver (no real Slack/PagerDuty endpoint exists to point
  this at from this environment — documented as a stub, not silently
  left looking like a real integration).

**Verified, not assumed:** `tests/test_drift_prometheus_export.py` (5
tests) — `compare_all()` returns both flagged and non-flagged fields with
identical arithmetic to `compare()` for the field that IS flagged;
sufficiently-sampled-only fields get excluded the same way `compare()`
excludes them; the push function is a confirmed no-op with no Pushgateway
configured; the push function is confirmed to push the *specific* gauge
values/labels expected (not just "no exception raised") via a
monkeypatched `push_to_gateway` that inspects the actual `CollectorRegistry`
contents; the Airflow wrapper is confirmed to pull XCom via explicit
`ti.xcom_pull(task_ids="check_taxonomy_drift")`, not template
interpolation. Full suite: 59 passed (up from 54), 2 skipped, 0 failed.

**Disclosed, not silently claimed working:** `docker-compose.yml`,
`monitoring/prometheus.yml`, `monitoring/alert_rules.yml`, and
`monitoring/alertmanager.yml` are syntax-checked (valid YAML, matches
each tool's documented config schema as understood from their docs) but
**not yet run against a live Prometheus/Alertmanager instance** — no
Docker daemon in this sandbox, same standing limitation as every other
piece of Docker-dependent infrastructure in this project. Specifically
unverified: whether Prometheus's `headers:` scrape-config field (used to
inject `/metrics`'s required `X-Redact-Api-Key` header) is accepted
exactly as written by the pinned Prometheus image version, and whether
the DNS-based single-target scrape of `redact-service:8080` behaves as
expected when `--scale redact-service=N` is also in play (the existing,
already-documented per-worker/per-replica metric-scoping limitation
applies here too — see `service.py`'s own comment). Needs the same live
confirmation pass every other new piece of infrastructure in this
project has gone through before any of this can be called "confirmed."

---

## Engineering upgrade 3: NER early-exit gate — implemented, honestly near-useless as scoped (2026-08-11)

Third item from the same external-review batch as upgrades 1 and 2. The
review proposed a Rust-based pre-filter to skip the NER call entirely on
lines with no PII-shaped content (e.g. "a pure system heartbeat log").
Rejected the Rust part per this project's own evaluation
(`PROJECT_STATUS.md`): reach for native code only if a cheap Python check
first proves insufficient, not before. This entry is that cheap Python
check — built, verified, and reported honestly, including the negative
result.

**What was built:** `detect._could_contain_ner_entity(text)` — returns
`False` only when `text` has neither a 2+ character letter run nor a 2+
character digit run anywhere. `scan_ner()` now returns `[]` immediately
when this is `False`, skipping the expensive `_get_analyzer()` /
`analyzer.analyze()` call path entirely.

**Why this specific, narrow condition and not something broader:** this
sandbox has no network access to the spaCy/Presidio model (same standing
constraint noted throughout this file), so nothing here can be verified
against the real model's actual behavior. A broader heuristic like "skip
if no capitalized word" is a real, plausible next step, but shipping it
without live verification would be asserting a recall claim this project
has no way to check — exactly the kind of thing this project's own
discipline exists to prevent. The condition actually shipped is instead
provable by construction, independent of the model: all six of
`scan_ner`'s canonical entity types (PERSON, EMAIL, IP, SSN, CREDIT_CARD,
MRN) structurally require at least a 2-character letter or digit run
somewhere (a name, an email local-part, an IP octet, digits of an
SSN/credit-card/MRN) — a text with neither cannot contain any of them,
regardless of what the model would have said.

**Verified two ways, and the second one is the actually important
result:**
1. `validation/early_exit_gate_verify.py` runs the gate against the full
   10,000-line canonical corpus and its gold PII spans directly (not a
   plausibility argument) — **0 false skips** across all 10,000 lines:
   the gate never fires on a line the ground truth says contains a real
   PII span. Safety confirmed against real labeled data.
2. **The same script found the gate's real-world trigger rate on this
   corpus is 0/10,000 (0.0%)** — every line in the canonical corpus has
   *some* digit run somewhere (a timestamp, a JSON field, an ID),
   which alone is enough to keep the gate's structural condition from
   ever being satisfied. A follow-up check against five representative
   heartbeat/health-check-style example lines ("heartbeat ok", "PING",
   "status: alive", "health check passed", "keepalive") found the gate
   **does not fire on any of them either** — ordinary English words
   ("heartbeat", "alive") are themselves 2+ character letter runs, so the
   gate can't distinguish "this could be a name" from "this is a real
   word" without a live model to verify a tighter rule against. **Honest
   conclusion: as scoped, this gate is close to useless on realistic log
   text.** It is not wrong, and it is genuinely free (see below), but it
   essentially only fires on lines with zero alphabetic content and zero
   multi-digit runs at all — a case rare enough that this project's own
   10,000-line corpus contains exactly none of it, and neither did five
   hand-picked examples of the motivating case from the original memo.

**Overhead, measured:** ~2.1 million calls/sec in this sandbox (4.76ms
for 10,000 calls) — genuinely negligible regardless of trigger rate, so
keeping this costs effectively nothing even though it rarely helps.

**Kept anyway, and here's why that's still the right call:** the gate is
zero-risk (proven against ground truth) and zero-cost (measured), so
there's no argument for reverting it even though its measured value on
realistic text is close to nil. The actually useful version of this idea
— a heuristic that can tell "ordinary word" from "plausibly a name" —
needs the live NER model this sandbox doesn't have, to verify it doesn't
quietly cost recall. That's the real follow-up, tracked as its own item
rather than shipped here without verification.

**Full test suite:** 65 passed (up from 59), 2 skipped, 0 failed —
`tests/test_early_exit_gate.py` (6 tests) covers the gate's own boolean
logic and confirms, via monkeypatching `_get_analyzer` to raise, that
`scan_ner()` genuinely never constructs the analyzer when the gate fires,
and genuinely still does when it doesn't (checking the mechanism, not
just the return value either way).

---

## Spike: ONNX/INT8 quantization for the NER pipeline — not applicable as proposed (2026-08-11)

Fourth and last item investigated from the external-review batch. The
review proposed converting "your spaCy pipeline or Custom RoBERTa models"
to ONNX with INT8 quantization for "up to 4x" throughput. This was
already flagged as suspect when the review first came in — REDACT has no
custom RoBERTa model, and the claim was unsourced (see
`PROJECT_STATUS.md`'s critique) — this spike checks the actual claim
against REDACT's real pipeline rather than just asserting the suspicion.

**What REDACT actually runs, confirmed directly, not assumed:**
`detect.py`'s `_get_analyzer()` calls plain `AnalyzerEngine()` with no
custom `NlpEngineProvider`/`nlp_configuration` — Presidio's own default
config (`presidio_analyzer/conf/default.yaml`, read directly from the
installed package in this sandbox) resolves that to `SpacyNlpEngine`
loading `en_core_web_lg`, matching the `en_core_web_lg` download this
project's own `Dockerfile`/`README.md` already document. `en_core_web_lg`
is spaCy's CNN-based (tok2vec + transition-based parser/NER) pipeline —
**not a transformer, not RoBERTa, not anything `spacy-transformers` or
Hugging Face `optimum` (the tooling that actually does ONNX/quantization
well) has first-class support for.**

**Directly verified, not assumed:** spaCy 3.8.15 (installed in this
sandbox) has zero ONNX-related surface anywhere in its API or CLI
(`[c for c in dir(spacy) if 'onnx' in c.lower()]` → `[]`, same for
`spacy.cli`, same for `python -m spacy --help`). Neither
`spacy-transformers` nor `optimum` — the two packages that together make
ONNX export/quantization a real, documented path — are installed, and
neither would apply even if installed, because that toolchain targets
transformer-backed spaCy pipelines (`en_core_web_trf`), not the
CNN-based `en_core_web_lg` this project actually uses. Community/
third-party attempts at ONNX-exporting spaCy's non-transformer
tok2vec/parser components exist but are not officially maintained by
Explosion (spaCy's maintainer) and were not evaluated further here —
adopting unmaintained third-party model-conversion tooling for the exact
model every accuracy number in this project's README, `BUGS_AND_FIXES.md`,
and the JCST manuscript is anchored to is a real-money, real-accuracy
risk that needs far more than a spike to justify.

**The honest options, laid out plainly:**
1. **Switch to `en_core_web_trf`** to make ONNX/quantization tooling
   applicable at all. Rejected for this spike: this is not a
   drop-in speed optimization, it's a different model with a different
   accuracy profile — every precision/recall number this project has
   ever published (Bugs 9/10's corrections, the synthetic-vs-real-data
   tables, the JCST manuscript's Section 5) would need full
   re-measurement against the new model before any of it could be
   trusted again. Transformer models are also often *slower* than a CNN
   pipeline on CPU-only inference even after quantization, so the
   throughput win itself isn't guaranteed either — an unverified
   assumption stacked on top of an unverified assumption.
2. **Unofficial ONNX conversion of `en_core_web_lg` directly.** Not
   pursued: unmaintained tooling, uncertain fidelity, no official
   Explosion support — high engineering risk for an uncertain and
   unmeasured payoff.
3. **A genuinely lower-risk alternative, found while investigating this:**
   Presidio ships its own officially-supported pipeline-component
   pruning via `NlpEngineProvider(nlp_configuration=...)`. Its own
   `SlimSpacyNlpEngine` (`presidio_analyzer/nlp_engine/slim_spacy_nlp_engine.py`,
   read directly from the installed package) loads spaCy models with
   `disable=["ner", "parser"]` for exactly this reason — "reducing memory
   usage and load time." It isn't directly usable as-is (it returns *no*
   entities at all, since it's meant for "Presidio v3" architectures
   where entity extraction is handled by self-contained recognizers, not
   REDACT's current setup), but its existence confirms a real,
   first-class, officially-supported configuration knob for disabling
   pipeline components REDACT's PERSON detection doesn't need (e.g.
   `parser`, `attribute_ruler`) while keeping `ner` enabled — a much
   lower-risk lever than ONNX, since it doesn't touch the model's
   weights or change detection behavior at all, only which computed
   outputs get produced. **Not implemented here** — whether
   Presidio's `SpacyRecognizer`/context-enhancement logic secretly
   depends on lemma or POS output that a narrower disable list would
   remove is exactly the kind of claim this sandbox has no live model to
   verify, the same standing constraint as everything else in this file
   that needs Docker/a real model download. Logged as a real, specific,
   low-risk follow-up for whoever next has a live spaCy/Presidio
   environment to test against — see ROADMAP.md.

**Conclusion: the external review's ONNX/quantization suggestion, as
written, does not apply to REDACT's actual pipeline and was correctly
not adopted.** The investigation wasn't wasted, though — it surfaced a
real, concrete, much-lower-risk alternative (component pruning via
Presidio's own supported configuration API) that the original suggestion
would never have found, since it was written against an architecture
REDACT doesn't have.

---

## Engineering upgrade 4: peppered Bloom filter for employee-name matching (2026-08-11)

Fifth and last item from the external-review batch (after Aho-Corasick,
the drift-detector wiring, the NER early-exit gate, and the ONNX spike
above). The review's original framing called this "zero-knowledge
identity matching" against a "Redis Bloom Filter" built from
`SHA-256(lowercase(firstname + lastname))`. Corrected on two points
before building anything, not after:

1. **"Zero-knowledge" is the wrong term.** It's a specific cryptographic
   term (a proof system with a formal no-extra-knowledge guarantee) that
   what's actually implementable here — a keyed Bloom filter — does not
   satisfy. Using the precise name matters given this project's own
   stated commitment to not overclaim what's verified.
2. **The bare-SHA-256 construction was a real, flagged security flaw,
   not a style nitpick.** Human names have low entropy, and this
   project's own `validation/real_name_frequency/` already has real
   SSA/Census name-frequency data — an attacker with the filter's
   contents could hash every name in that same public dataset and fully
   reconstruct "who's on this list" in minutes. Real HR/personnel data
   should not be that cheaply reversible.

**What was built:** `src/employee_name_filter.py`'s `HashedNameFilter` —
a Bloom filter where every hash is `HMAC-SHA256(pepper, normalized_name)`
instead of bare `SHA-256`. Without the pepper, an attacker with only the
filter's contents cannot replicate the hash function at all; brute-force
enumeration over a name-frequency dictionary produces nothing usable.
**Disclosed, not glossed over: this narrows the attack surface, it does
not eliminate it** — an attacker who compromises both the filter's
contents AND the pepper's storage location can still run the same
brute-force attack the unsalted version was vulnerable to. The real
property is defense-in-depth (splitting one secret into two
independently-compromisable pieces), not immunity — see the module's own
docstring for the full reasoning, including why pepper storage/rotation
is a real operational requirement (this class takes the pepper as a
constructor argument and does not manage its storage — the same division
of responsibility `VaultStorageProvider` already uses for `TOKEN_KEY`/
`PSEUDO_KEY`), and why deletion (offboarding a departed employee) needs a
full rebuild rather than incremental removal (a standard Bloom filter,
which this is, cannot remove individual items).

**Why a plain Python bit array instead of the review's literal "Redis
Bloom Filter" proposal:** this project's `docker-compose.yml` runs plain
`redis:7-alpine`, not `redis/redis-stack-server` (the image that actually
bundles the RedisBloom module) — adding that module would mean a new
infrastructure dependency and an image swap. A Bloom filter's whole
performance point is that querying it needs no network round-trip if it
fits in memory (a few hundred thousand employee names is single-digit
MB as a bit array), so this loads once per worker process at startup —
the same per-process warm-up pattern this project already uses for the
spaCy/Presidio analyzer (`detect._get_analyzer()`) — with no new
infrastructure dependency and no network call on the hot path.

**Verified, not assumed:** `tests/test_employee_name_filter.py` (8
tests), all fully deterministic (no Redis, no live AD/Okta export, no
NER model needed):
- Zero false negatives across 500 inserted names (the one guarantee a
  Bloom filter must never break).
- Measured false-positive rate on 2,000 genuinely-never-added names stays
  well under a generous bound of the configured target — not just
  trusting the standard sizing formula.
- **The actual security property, confirmed directly:** building the
  same name list under two different peppers produces different bit
  arrays — the pepper genuinely changes the hash output, not just in
  theory.
- Loading a saved filter with the WRONG pepper does not silently return
  correct results — confirmed the wrong-pepper case actually fails to
  find at least one of the originally-added names.
- The saved file format is confirmed, by direct byte-string search, to
  never contain the pepper.
- Normalization (case/whitespace) is consistent between `add()` and
  `might_contain()`, and `build_from_names_file()` correctly skips blank
  lines.

Full suite: 73 passed (up from 65), 2 skipped, 0 failed.

**Disclosed, not silently claimed integrated:** this module is
standalone and opt-in — **not yet wired into `detect.py`'s detection
ensemble** (`scan_flattened()`/`scan_regex()`) as an active Layer 4
companion. No real AD/Okta export exists in this environment to build a
real filter against or measure real recall gain from (all tests above
use synthetic name lists). Wiring it in and measuring its real-world
effect needs both a real deployment's employee list and a live NER
environment to compare against — the same standing constraint as every
other environment-blocked claim in this file. See ROADMAP.md item 13 for
this as an explicit next step, not an implied-done one.

---

## Engineering upgrade 5: FF3-1 format-preserving encryption, evaluated as an optional TokenStore alternative (2026-08-11)

Last item from the external-review batch. The review proposed
"Stateless AES-FFX Encryption" as a wholesale replacement for token
mapping tables, framed as a pure win — no lookup table, no HA problem
for it, authorized teams "decrypt it statelessly." Evaluated honestly
rather than adopted on that framing: this is a real, useful technique
with a real, serious cost the review's framing left out entirely.

**What was built:** `src/fpe_provider.py`'s `FPEDigitsProvider`, using
the `ff3` PyPI package (a real NIST SP 800-38G Revision 1 implementation)
— **FF3-1 specifically, confirmed by reading the installed library's
source directly**, not FF3, the original construction NIST deprecated
after a 2017 published cryptanalytic attack. The library supports both;
this module always uses a 56-bit tweak to select FF3-1's corrected code
path (`calculate_tweak64_ff3_1`), never the legacy 64-bit tweak that
would silently opt back into the weaker construction.

**Deliberately scoped to digit-only fields, unlike the review's
unscoped proposal:** format-preserving encryption requires a fixed
alphabet/radix — there's no coherent "format-preserving" encryption of a
PERSON value like "Timothy Wong," and the review never addressed this.
`FPEDigitsProvider` only handles SSN/credit-card-shaped digit sequences
(radix 10), with `encrypt_formatted()`/`decrypt_formatted()` wrapping the
raw digit encryption to reinsert dashes/spaces at their original
positions. PERSON/EMAIL stay on TokenStore's existing path, full stop.

**Two real costs, disclosed plainly, that the review's "stateless"
framing presented as pure upside:**
1. **Smaller security margin than standard AES.** A 9-digit SSN has a
   plaintext domain of 10^9 (~30 bits) — FF3-1 is the correct,
   NIST-standardized construction for this problem, but its practical
   brute-force resistance is bounded by the domain size itself, not by
   the underlying AES key strength, unlike a non-format-preserving
   encryption of the same value.
2. **Key rotation is structurally worse than `TOKEN_KEY`'s.**
   `rotate_token_key` (`dags/redact_weekly_validation.py`) is safe today
   specifically because `TokenStore`'s resolution is lookup-table-based,
   not key-based — every already-minted token stays resolvable after
   rotation, only future tokens gain the new key's guessability
   resistance. FPE cannot offer this: decryption IS the reverse of
   encryption under the same key by construction, so rotating this
   module's key makes every previously-encrypted value permanently
   unrecoverable unless it's fully re-encrypted under the new key first
   — a real reprocessing pass over historical data, not a background
   task. This is a genuine structural downgrade, not a minor
   inconvenience, and the review's "stateless" framing never mentioned
   it.

**Verified, not assumed:** `tests/test_fpe_provider.py` (9 tests) —
round-trip correctness on SSN- and credit-card-shaped values with
separators; confirmed the encrypted output preserves format/length and
genuinely differs from the input (not an accidental no-op); **confirmed
directly that two different keys produce different ciphertext for the
same input, and that decrypting with the wrong key produces the wrong
plaintext** (the concrete mechanism behind the rotation-danger claim
above, not just an assertion about it); confirmed FF3-1's own minimum
domain requirement (6+ digits for radix 10) is enforced, not silently
worked around; confirmed `DEFAULT_TWEAK` is genuinely 56 bits, not the
deprecated 64-bit legacy length. These tests check this module's own
wrapper logic and the concrete rotation/key-dependency properties — they
do not, and do not claim to, independently cryptanalyze FF3-1 itself;
that's NIST's and the library's responsibility.

Full suite: 82 passed (up from 73), 2 skipped, 0 failed.

**Disclosed, not silently claimed integrated:** `ff3` is deliberately
kept out of the default `requirements.txt` (a new
`requirements-fpe.txt`, same pattern as `requirements-redis.txt`/
`requirements-vault.txt`/`requirements-airflow.txt`) — this module is
**not wired into `anonymize.py`'s live pseudonymization path** and is
not part of `redact-service`'s default image. It exists as a standalone,
evaluated, opt-in alternative with its real tradeoffs on the record, not
an active part of the pipeline. See ROADMAP.md item 13.

---

## Pattern across these bugs

Every critical-impact bug on this list (1, 4, 5, 7, 12, 14) shared the same
shape: the pipeline appeared to be working — no crash, no error thrown,
requests returning 200 — while silently destroying or never producing the
data it was supposed to produce. Bug 15 is a related but genuinely
distinct shape, worth naming separately rather than folded into the list
above: not silently wrong output, but silently, catastrophically
*slower* output, with no error anywhere to signal it — `docker stats`
and a manually timed `curl` request were what surfaced it, not a log
line or a failed check. Every scale this project tested before the
1,000,000-line load test (10,000, then 100,000 lines) was too small to
ever cross the point where `TokenStore.save()`'s O(n) per-call cost
became visible, which is exactly why nothing short of testing at a full
order of magnitude beyond the last verified scale would have found it —
the same argument ROADMAP item 9's own "scoped honestly, not oversold"
framing already made about vertical-scale load testing in general, now
with a second concrete bug (after Bug 12) to point to as evidence it
wasn't a hypothetical concern. Bug 14 is the same shape in a different
layer: not a document-ID collision or a missed detection, but
`TokenStore.save()`'s blind-overwrite silently dropping a sibling
process's reverse-map entries on every concurrent save — `resolve()`
would return the wrong (or a stale) value with no exception anywhere in
the call path, and `detokenize()` would silently fail its own documented
guarantee. None of these were caught by the absence of errors; all were
caught by manually cross-checking document counts against known-good
baselines (raw line counts, expected quarantine rates, and — for Bug 12
specifically, the first bug this document needed a second signal for —
Logstash's own pipeline stats API showing what was actually *sent* versus
what OpenSearch actually *stored*) and, in Bug 5, 7, and 12's case, by
directly inspecting sample documents or plugin-level counters rather than
trusting the aggregate index count alone; Bug 14 required going one step
further and building a dedicated multi-process stress test, since no
existing test in the project ever exercised more than one OS process
against the same persistence backend. This is the
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

---

## Engineering upgrade 6: floci-based ALB test written, hand-off pending (2026-08-11)

ROADMAP item 13's floci follow-up. `docker-compose.yml` gained an
opt-in `floci` service (`cloud-sim` profile) and `run_floci_elbv2_test.sh`
was written to test REDACT behind a floci-emulated AWS ALB — creating a
real VPC/subnets/target group/listener via the actual `aws elbv2`/
`aws ec2` CLI commands (real, documented AWS API shapes, not invented),
registering the scaled `redact-service` replicas' container IPs as
targets, and testing both the control plane (can these resources be
created/described at all) and the data plane (does traffic sent to the
ALB's DNS name actually reach a `redact-service` replica) separately.

**Honest uncertainty flagged up front, not resolved by assumption:**
floci's own documentation lists ELB v2 as an "In-process" implementation
(unlike MSK/RDS/ElastiCache, which are "Real Docker"), with "ALB, NLB,
target groups, listeners, routing rules" as stated features — this could
mean a full data-plane emulation that actually proxies HTTP traffic, or
a control-plane-only emulation that accepts the right API calls without
forwarding anything. The script tests both separately and prints which
one is actually true, rather than the write-up asserting an answer this
sandbox has no way to check.

**Not run — cannot be run in this sandbox** (no Docker daemon here, same
standing constraint as every other Compose-dependent test in this
project). Syntax-checked only (`bash -n`, every AWS CLI command's
parameters checked against real, documented `aws elbv2`/`aws ec2`
syntax). Handed off for the user to run and report back which of the two
outcomes (full data-plane proxy vs. control-plane-only) floci actually
provides — that result determines whether this genuinely closes the
"real cloud load balancer" gap or only partially does (API/architecture
fidelity without traffic-forwarding fidelity).

---

## Engineering upgrade 7: Kafka-shaped queue alternative -- real redelivery-guarantee improvement over Redis-list, floci test written (2026-08-11)

ROADMAP item 13's second floci-based prototype (after the ELB v2 test in
"Engineering upgrade 6" above). Unlike that one, this genuinely closes a
disclosed gap rather than leaving an open question -- floci's own service
table lists MSK as "Real Docker" (a genuine Redpanda broker), not
"In-process," so there's real confidence real traffic actually flows,
not just that the right API calls succeed.

**The real problem this closes, not just a different transport for its
own sake:** `queue_consumer.py`'s Redis-list path (BLPOP) has always
disclosed a genuine gap -- BLPOP removes an item the instant it's
popped, so if this process crashes after popping but before finishing
(the redact-service call or the OpenSearch write), that event is lost,
not redelivered. A real Kafka consumer group's offset-commit model
avoids this by construction: an event isn't "done" until this process
explicitly commits its offset, so a crash between poll and commit means
the next consumer in the group re-reads it.

**What was built:**
- `src/queue_consumer.py`: `_run_kafka_consumer()`, dispatched from
  `main()` when `KAFKA_BROKERS` is set (falls through to the existing
  Redis/Sentinel path unchanged otherwise -- fully backward compatible).
  Uses `kafka-python` with `enable_auto_commit=False` and an explicit
  `consumer.commit()` call placed AFTER `process_event()` succeeds --
  this ordering is the entire mechanism that makes the redelivery
  guarantee real rather than just claimed. Both transports call the
  exact same `process_event()`, so no business logic is duplicated
  between them.
- `logstash/redact-pipeline-kafka.conf`: producer side, using the
  official `logstash-output-kafka` plugin. **A genuinely better producer
  story than the Redis path's own disclosed gap:** `logstash-output-redis`
  has no Sentinel awareness at all (see "Engineering upgrade" entries in
  the Redis HA bugs above); Kafka's own client protocol has real
  broker-discovery and partition-leadership-tracking built in as
  standard behavior, closing the producer-side half of this gap for
  real, not just moving it.
- `logstash/Dockerfile`: explicitly installs `logstash-integration-kafka`
  -- disclosed honestly that this plugin is widely documented as bundled
  by default in the standard Logstash image (unlike the Redis/OpenSearch
  plugins, which were confirmed NOT bundled), but that has not been
  independently verified against a live container here; the install
  command is idempotent either way, so it costs nothing if already
  present and closes the gap outright if not.
- `docker-compose.yml`: new `logstash-kafka`/`queue-consumer-kafka`
  services under an opt-in `kafka-queued` profile, mutually exclusive
  with the default and `queued` profiles against the same raw log files
  (same double-processing warning the `queued` profile's own services
  already carry).
- **A real gap found and fixed while wiring this up, not left implicit:**
  `queue-consumer-kafka` shares `queue-consumer`'s Dockerfile/image, which
  only installed `requirements-redis.txt`, not the new
  `requirements-kafka.txt` -- as originally configured, this service
  would have failed at runtime with `ModuleNotFoundError: No module
  named 'kafka'`. Fixed by adding `requirements-kafka.txt` to the shared
  image's base install, same precedent and same disclosed cost
  (`requirements-redis.txt`'s own comment already established this
  exact reasoning for why a shared multi-entrypoint image accepts every
  entrypoint's dependencies rather than fragmenting into per-transport
  images) -- this would NOT have been caught without actually tracing
  through which image builds which service, exactly the kind of gap that
  "looks right" in a diff but fails the first time someone actually runs
  it.
- `run_floci_kafka_test.sh`: provisions a cluster via floci's `aws kafka`
  API, extracts the real broker address, wires `KAFKA_BROKERS` through to
  both new Compose services, runs a real 20,000-line corpus through, and
  reconciles via `validation/load_test/reconcile.py`. **Caught and fixed
  two invented-flag mistakes while writing this** -- an earlier draft
  called `reconcile.py` with `--expected`/`--anonymized-index`/
  `--quarantine-index`/`--opensearch-url` flags that don't exist (that
  script actually takes one positional `opensearch_host` argument and
  computes the expected count from `data/raw/*.log`'s own line counts,
  confirmed by reading the script directly rather than assumed from
  similarity to other scripts), and `src/generate_logs.py`/
  `export_raw_logs.py` calls originally used made-up `--count`/`--output`
  flags instead of those scripts' real `--n`/`--out`/`--dirty-ratio`/
  `--input`/`--output-dir` flags (confirmed by grepping how
  `run_replica_and_queue_test.sh` actually calls them). Both fixed before
  this was written up as done, not left as a "should work" claim that
  would have failed on first real use.
- `tests/test_queue_consumer.py`: 4 new tests (10 total in that file
  now), all mocking `kafka.KafkaConsumer` at its source (the import is
  lazy, same pattern as the Redis import) rather than needing a live
  broker. **Directly confirms the actual redelivery mechanism, not just
  that no exception was raised:** `commit()` is called exactly once after
  a successful `process_event()`, `commit()` is NOT called after a
  failed one (the concrete behavior behind the redelivery-guarantee
  claim above), and a genuinely unparseable message IS still committed
  (so a permanently-malformed message doesn't block that partition
  forever on every restart, matching the Redis path's own
  drop-and-continue behavior for the same case).

Full suite: 86 passed (up from 82), 2 skipped, 0 failed.

**Disclosed, not silently claimed live-confirmed:** none of the new
Compose services, the Dockerfile plugin install, or
`run_floci_kafka_test.sh` have been run in this sandbox (no Docker
daemon here) -- syntax-checked and logic-checked only (`bash -n`,
`py_compile`, YAML validation, every CLI flag verified against the real
target script's actual `argparse` definition rather than assumed).
Handed off for the user to run.

---

## Engineering upgrade 8: 5,000,000-line load test harness -- deadline-scaling gap found and fixed, run handed off (2026-08-11)

ROADMAP item 9's stretch goal (Task #47): push the local-scale load test
beyond the existing 1,000,000-line high-water mark. Reuses
`validation/load_test/run_load_test.sh` unmodified in structure (same
corpus generation, `docker stats` capture, `track_total_hits=true`
reconciliation poll loop) via a new `run_5m_load_test.sh` wrapper at 5x
scale, following the same pattern as the existing `run_1m_load_test.sh`.

**Real gap found and fixed before hand-off, not left for the run itself
to discover:** `run_load_test.sh`'s poll-loop deadline
(`REDACT_LOAD_TEST_MAX_WAIT_SECONDS`) defaulted to a fixed 14,400s
(4 hours) -- sized correctly for the 1,000,000-line runs this project
has actually completed (the slower of the two measured rates, ~224
lines/sec, finished in ~75 minutes), but not scaled for anything larger.
At 5,000,000 lines, even the faster measured rate (~439 lines/sec)
implies ~3.2 hours, and the slower one implies ~6.2 hours -- past the
old fixed deadline. Had this shipped unfixed, a genuinely healthy,
still-converging 5,000,000-line run would have been killed by the poll
loop and falsely reported a deadline-exceeded failure, the same
false-FAIL shape Bug 15's own iteration-count fix was written to
prevent, just for wall-clock time instead of poll count. **Fixed at the
source, in `run_load_test.sh` itself**, not worked around in the new
wrapper script: the default deadline now scales with `N`
(`max(14400, N / 100 * 1.5)` seconds -- a conservative 100 lines/sec
floor, below both measured 1,000,000-line rates, times a 1.5x safety
margin), still fully overridable via the same env var. This protects
every future run at any scale, not just the 5,000,000-line one.

`run_5m_load_test.sh`'s header comment carries disk-space and
expected-runtime estimates extrapolated from the 1,000,000-line runs'
own measured numbers (~190 bytes/line corpus size at 10,000 lines,
scaled up; ~9.3% token-store growth rate; ~89.3% audit fan-out) --
explicitly labeled as estimates, not measurements, since this sandbox
has no Docker daemon to actually run this against and confirm them.

Both `run_5m_load_test.sh` and the modified `run_load_test.sh` are
syntax-checked (`bash -n`) only. Handed off for the user to run; not yet
executed anywhere.

---

## Engineering upgrade 9: standalone WebAssembly port of Layer 1 + Layer 4 (Task #48, 2026-08-11)

ROADMAP item 13's edge-scrubbing pillar, deliberately scoped down per
the project's own critique of the original external-review memo: get a
real, correct, standalone Wasm module built and proven equivalent to
the Python detection logic FIRST, before attempting any Vector/Fluent
Bit/Envoy plugin integration (Task #49, not attempted here).

**Real environment constraint, disclosed rather than worked around
silently:** the memo proposed Rust targeting wasm32. This sandbox has
no Rust toolchain and no way to install one (no `rustc`/`cargo`,
`rustup`'s installer unreachable, no root/sudo for `apt`). Built in
AssemblyScript instead -- a real, working choice given what's actually
available (npm/Node), not a downgrade to something untestable. See
`wasm/layer1_4/README.md` for the full reasoning, including how a
future Rust port would translate directly from this same code (no regex
engine used in either language, by necessity).

**What was built:** `wasm/layer1_4/assembly/index.ts` -- hand-written
character scanners for every Layer 1 pattern (SSN, EMAIL, CREDIT_CARD
with the AWS-account-ID-in-ARN exclusion, IP, MRN), each reasoned
through against Python's actual `\b`/backtracking semantics (documented
per-function in the source), plus Layer 4's flattened-name segmentation
-- simplified from a full Aho-Corasick automaton port to direct
hash-set prefix/suffix lookups, which is PROVABLY equivalent output for
this specific bounded (<=30-char token) use case, not a shortcut that
changes behavior (see the module's own header comment for the direct
argument, and `src/flattened_names.py`'s own engineering-note for why
the Python side uses the automaton in the first place -- efficiency,
not different semantics).

**Verified, not just argued:**
- **10,000/10,000 lines byte-identical** against `src/detect.py`'s real
  `scan_regex()` and `src/flattened_names.py`'s real
  `scan_flattened_names()` over the canonical synthetic corpus.
- **12,033/12,033 lines byte-identical** against real Loghub log data
  (Linux/OpenSSH/OpenStack/Thunderbird/Zookeeper) plus real CloudTrail
  and Windows Event samples, matching how
  `validation/real_data/inject_and_evaluate.py` actually constructs the
  text `detect.py` scans.

**A real bug found and fixed by the real-data test specifically, not
the synthetic one:** the first version of the CREDIT_CARD digit-run
scanner checked run length only, never separately verifying the leading
`\b` boundary -- `Linux_2k.log`'s `n219076184117.netvigator.com`
contains a 12-digit run immediately preceded by `n` (a `\w` character,
so no boundary exists), which the first Wasm version incorrectly
flagged as CREDIT_CARD while Python correctly excluded it. Fixed by
adding the same `boundaryAt()` check every other scanner already had.
Directly confirms this project's own stated reason for testing against
real, messy data and not just its own synthetic corpus -- this exact
class of gap was invisible against 10,000 lines of synthetic data and
found on the first pass against real log lines.

**Disclosed, not oversold:** a quick, honestly-measured throughput
comparison (10,000 lines, this sandbox) found the compiled Wasm module,
called through Node's JSON-string interface, ran SLOWER (~21,800
lines/sec) than the same detection logic in plain Python (~27,400
lines/sec) -- the JSON `stringify`/`parse` round trip and JS-string-to-
Wasm-memory marshalling dominate at this workload size. This is not a
demonstrated performance win, and is stated as such rather than left
implied -- a real edge-deployment throughput case would need a typed,
allocation-light interface this task deliberately didn't build, since
proving detection-logic correctness was the actual scope here. See
`wasm/layer1_4/README.md`'s own "Performance" section for the full
numbers and reasoning.

**Known, disclosed gap:** `scanEmail()`'s boundary handling is verified
against every realistic email shape this project's synthetic and real
corpora actually contain, but not proven equivalent to true regex
backtracking for fully pathological inputs -- see that function's own
comment.

---

## Engineering upgrade 10: edge collector integration scoping -- Task #49's own recommendation corrected (2026-08-11)

Task #49 (follow-on to the Task #48 Wasm port) asked for a scoping doc
for Vector/Fluent Bit/Envoy plugin integration, with the task's own
description recommending Vector "since it has the most straightforward
Wasm/VRL extension story." **Checked that claim against current reality
before scoping around it, rather than taking it at face value:**

- **Vector removed its `wasm` transform in v0.17.0 (October 2021)** and
  has not brought it back -- confirmed via Vector's own removal
  announcement (`vector.dev/highlights/2021-08-23-removing-wasm`), not
  assumed from general familiarity. VRL being compiled to Wasm for a
  browser playground (a real, separate thing search results surface) is
  not the same as Vector supporting third-party Wasm plugins in a
  running pipeline -- confirmed the distinction directly rather than
  conflating the two.
- **Fluent Bit has real, current, functional Wasm filter/input plugin
  support**, confirmed via `docs.fluentbit.io`'s own developer docs
  (fetched directly, not summarized secondhand): a defined C-ABI
  function signature (`char* c_filter(char*, int, uint32_t, uint32_t,
  char*, int)`), supported toolchains (Rust `wasm32-unknown-unknown`,
  TinyGo `wasm32-wasi`, WASI SDK), real example filters in Fluent Bit's
  own repo.

**Recommendation reversed from the task's own premise: Fluent Bit, not
Vector.** Full reasoning, the exact ABI Task #48's module would need to
grow a second entry point for, the real added scope (redaction, not
just detection -- pulling in a slice of `anonymize.py`'s
responsibility), an unresolved open design question (PERSON-type
pseudonym consistency across independent edge nodes -- flagged, not
answered, since answering it needs a real product decision this project
hasn't made), and a phased effort estimate are all in
`wasm/EDGE_COLLECTOR_INTEGRATION_SCOPING.md`. No integration build was
attempted -- this task was scoping only, per its own description.

**Also disclosed:** an edge Wasm filter built from Task #48's module can
only ever cover Layer 1 (regex) + Layer 4 (flattened names) detection --
Presidio's NER (Layer 2), responsible for the large majority of
normally-formatted PERSON recall per this project's own measured
numbers, cannot run in a Wasm sandbox. An edge filter is a pre-filter,
not a `redact-service` replacement -- stated explicitly in the scoping
doc so this isn't discovered as a surprise later.

---

## Engineering upgrade 11: drift-alert source-attribution design doc (Task #50, 2026-08-11)

Design/dependency doc only, per Task #50's own instruction ("write the
design/dependency doc first before committing to a specific
integration") -- no code changes. See
`monitoring/SOURCE_ATTRIBUTION_DESIGN.md` for the full doc.

**Real gap found while scoping, stated before anything else in the
doc:** REDACT's pipeline carries no source metadata finer than
`log_type` (only 3 fixed values: `windows_event`/`syslog`/`cloudtrail`,
assigned purely by which file path a raw log line was read from). "Map
log source metadata back to an owning team," this task's own phrasing,
implicitly assumes finer-grained metadata already exists -- it doesn't,
in REDACT's current scope. Flagged as a real precondition, not silently
assumed solvable.

**Two real options scoped, one recommended:** a static
`log_type` -> team YAML mapping consumed directly by Alertmanager's own
native routing tree (zero REDACT code changes, works today at the
current 3-log-type scale, real staleness risk disclosed) versus a real
service-catalog integration (Backstage-style -- organizationally
correct long-term, but REDACT has no existing dependency on any catalog
and building one speculatively against nothing real would be
untestable and possibly wrong for whatever a real deploying
organization actually runs). Recommended: ship the static mapping now,
treat the catalog integration as an explicit future step gated on a
real target catalog existing, with the integration point identified
(`src/airflow_tasks.py`'s `push_drift_metrics_to_prometheus()`) if that
day comes.

---

## Bug 21: optional-dependency import aborted the entire test suite, not just one file (2026-08-11)

Found by the user, not caught in this sandbox first -- a real gap in
this project's own verification discipline, worth stating plainly. A
fresh `pip install -r requirements.txt` followed by `pytest tests/`
(exactly `tests/README.md`'s own documented quick-start) failed
outright:

```
ImportError while importing test module '.../tests/test_fpe_provider.py'
src/fpe_provider.py:107: in <module>
    from ff3 import FF3Cipher
E   ModuleNotFoundError: No module named 'ff3'
Interrupted: 1 error during collection
```

**Root cause:** `src/fpe_provider.py` (Engineering upgrade 5) imported
`ff3` unconditionally at module level. Every OTHER optional dependency
in this project (`kafka-python`, `hvac`, `redis`, `prometheus-client`)
is lazily imported inside the specific function/class that actually
needs it, precisely so a missing optional dependency degrades to "that
one feature/test file isn't available" -- `fpe_provider.py` was the one
exception, and pytest's collection phase treats an `ImportError` as
fatal to the ENTIRE run, not just the one file, so this one unguarded
import silently would have broken `fast-tests` in CI too (confirmed:
`ci.yml`'s `fast-tests` job only ever installed `requirements.txt`,
never `requirements-fpe.txt`) -- not caught earlier because this
sandbox had `ff3` already installed from earlier work in the same long
session, so the gap was invisible here until a genuinely fresh
environment (the user's) hit it.

**Why this wasn't caught by this project's own "run the suite before
every commit" discipline:** that discipline checked THAT the suite
passed in this sandbox, correctly, every time -- but never checked
WHETHER this sandbox's environment still matched what
`tests/README.md`'s own documented install instructions would actually
produce on a clean machine. A real process gap, not just a code one.

**Fixed on three levels, not just the one import:**
1. `src/fpe_provider.py`: `from ff3 import FF3Cipher` moved from module
   level into `FPEDigitsProvider.__init__`, matching the lazy-import
   pattern already established everywhere else.
2. `tests/test_fpe_provider.py`: added `pytest.importorskip("ff3")`
   before importing `fpe_provider`, so this file cleanly SKIPS instead
   of erroring even if the lazy-import fix above were ever
   accidentally reverted.
3. `tests/test_queue_consumer.py`: the three tests that `patch("kafka.KafkaConsumer", ...)`
   (which requires importing the real `kafka` module to resolve the
   patch target) are now marked `@pytest.mark.skipif` on
   `importlib.util.find_spec("kafka") is None` -- these were not
   actually broken (queue_consumer.py's own `kafka` import was already
   lazy), but would have failed at test-EXECUTION time with a
   confusing `ModuleNotFoundError` on a machine without kafka-python,
   rather than skipping cleanly like everything else in this suite
   does for a missing optional dependency.
4. `.github/workflows/ci.yml`'s `fast-tests` job now installs
   `requirements-fpe.txt` and `requirements-kafka.txt` too (neither
   needs a live external service, unlike Redis/Vault, so it's safe and
   cheap to add here) -- this project's real CI coverage now actually
   includes these 13 tests (9 fpe + 4 kafka) instead of silently never
   running them.

**Verified, not just argued:** simulated a fresh-environment collection
attempt via `pytest.importorskip` against a genuinely nonexistent
package name (confirmed: cleanly skips, doesn't abort the run) and
confirmed `importlib.util.find_spec` returns `None` (not an exception)
for a genuinely missing package -- both are the real mechanisms the
fixes above depend on. Full suite re-confirmed passing in this sandbox
afterward: 86 passed, 2 skipped, unchanged from before this fix (this
sandbox already has both optional dependencies installed, so this fix
is invisible here -- its effect is only visible in the fresh
environment that found the bug in the first place).

---

## Bug 22: required KAFKA_BROKERS variable broke every docker compose command in the file, plus two related script gaps (2026-08-11)

Found live by the user, not caught in this sandbox (no Docker daemon
here to actually run any `docker compose` command against). Both
`./run_floci_kafka_test.sh` and `./run_floci_elbv2_test.sh` failed
immediately:

```
error while interpolating services.queue-consumer-kafka.environment.[]:
required variable KAFKA_BROKERS is missing a value: set to a real
broker address, e.g. via run_floci_kafka_test.sh
```

**The important part: `run_floci_elbv2_test.sh` failed too, and that
script has nothing to do with Kafka at all.** That's the real signal
this wasn't a Kafka-test-specific problem.

**Root cause:** `docker-compose.yml`'s `logstash-kafka`/
`queue-consumer-kafka` services (Engineering upgrade 7) used
`${KAFKA_BROKERS:?set to a real broker address, ...}` -- the "required,
error if unset" interpolation syntax. Docker Compose interpolates and
validates required variables for the WHOLE compose file up front,
before filtering by `--profile` or by which service names were actually
requested on the command line -- confirmed via Compose's own documented
behavior and community-reported issues, not assumed. A service gated
behind `profiles: ["kafka-queued"]` that never gets started in a given
invocation still has its `:?` variables validated, and a missing one
fails the ENTIRE `docker compose` invocation, not just that service.
Every other required variable in this file (`REDACT_PSEUDO_KEY`,
`REDACT_SERVICE_API_KEY`, etc.) is required by `redact-service`, which
has no `profiles:` key and therefore always starts -- so requiring
those unconditionally was always correct. `KAFKA_BROKERS` was the first
required variable belonging ONLY to a profile-gated, opt-in service,
and that combination is what broke every unrelated invocation.

**Fixed:** `KAFKA_BROKERS` changed to `${KAFKA_BROKERS:-}` (defaults to
empty rather than erroring). Safe specifically because both Kafka
services are already profile-gated and only ever started by
`run_floci_kafka_test.sh`, which exports a real `KAFKA_BROKERS` before
invoking `docker compose --profile kafka-queued up` -- an empty value
only reaches these containers if someone starts them directly outside
that script, in which case the Kafka client fails to connect at runtime
(a normal, visible error) instead of silently succeeding with a bad
value.

**Two more real gaps found and fixed while investigating this, neither
previously caught because neither script had actually been run
before:**
1. Neither `run_floci_kafka_test.sh` nor `run_floci_elbv2_test.sh`
   bootstrapped the 5 required `.env` keys (`REDACT_PSEUDO_KEY`,
   `REDACT_AUDIT_KEY`, `REDACT_SERVICE_API_KEY`,
   `REDACT_FINGERPRINT_KEY`, `REDACT_TOKEN_KEY`) the way
   `run_1m_load_test.sh`/`run_5m_load_test.sh` already do -- both would
   have hit a second, legitimate required-variable error on
   `redact-service` itself the moment the `KAFKA_BROKERS` bug above was
   fixed. Fixed by adding the identical bootstrap block both other
   scripts already use.
2. `run_floci_elbv2_test.sh`'s Part 4 `curl` call referenced
   `${REDACT_API_KEY:-}` -- not a real variable anywhere in this
   project (the actual name, used everywhere else including this same
   script's own comments, is `REDACT_SERVICE_API_KEY`). Harmless in
   practice only because `/health` is the one route exempt from the
   API-key check (`src/service.py`'s `_require_api_key`), but a script
   meant to demonstrate the real path should use the real variable name
   regardless. Fixed.

**Verified, not just argued:** confirmed via `yaml.safe_load` that
`docker-compose.yml` still parses correctly after the fix, `bash -n` on
both scripts, and the full pytest suite still passing (86 passed, 2
skipped, unaffected -- this bug was purely in Compose interpolation,
not Python code). **Still not run against a live Docker daemon in this
sandbox** -- the user's next run of either script is the actual
confirmation this fix works, same disclosed limitation as every other
Docker-dependent claim in this project.

**Process gap, worth stating plainly:** neither of these scripts had
actually been executed anywhere before this bug was found -- they were
syntax-checked (`bash -n`) and had every `aws`/`docker` command's
parameters checked against real documented syntax, which is real
verification, but is not the same as running the script and watching it
fail. `bash -n` cannot catch a Compose-level interpolation-ordering
interaction like this one; only actually running `docker compose`
against the real file can. This is the same category of gap Bug 21
(the ff3 import) represents -- verification that's real but doesn't
cover the specific failure mode that only shows up in a genuinely fresh
environment or a genuine execution attempt, not a rehearsed one.

---

## Bug 23: live Kafka test found a real 3x document over-count -- root cause not fully isolated, fixed at the point that actually matters (2026-08-11)

The user ran `./run_floci_kafka_test.sh` after Bug 22's fix -- it
completed end to end for the first time (provisioned a real floci/
Redpanda MSK cluster, built and ran the full stack, processed a real
20,000-line corpus) but reconciliation FAILED with a striking number:

```
Expected total (raw exported lines): 20000
security-logs-anonymized-*:          60000
security-logs-quarantine-*:          0
redact-audit-trail-*:                53507
anonymized + quarantine:             60000
RECONCILIATION: FAIL
```

**60,000 is exactly 3.0x 20,000, not a random overcount** -- and the
audit fan-out ratio (53,507/60,000 = 89.2%) is right in line with every
prior run's measured ratio (~89.2-89.3%), which is the important
diagnostic signal: the DETECTION/anonymization logic itself produced
correct, consistent output -- something upstream of it really did call
`process_event()` roughly 3 times per raw line, not a bug in what
`process_event()` does with a given line.

**Root cause not fully isolated in this sandbox** (no Docker daemon
here to inspect the actual Kafka consumer group's rebalance/commit
sequence, or Logstash's producer-side retry logs, from the live run
that found this). Two plausible mechanisms, both real and
well-documented for Kafka clients in general, not specific to floci:

1. **Consumer-group rebalance during a slow processing batch.**
   `_run_kafka_consumer()`'s `KafkaConsumer` used the library's default
   `max_poll_records=500` -- one `poll()` cycle can hand back up to 500
   messages, which the loop then processes ONE AT A TIME with a real,
   synchronous `redact-service` HTTP call each. No further `poll()`
   happens (and therefore no progress against `max_poll_interval_ms`,
   the "call poll() again within this time or the group coordinator
   presumes you're dead" timer every real Kafka client enforces) until
   that entire batch finishes. Under real load -- this exact test run
   had a corpus generator, a full multi-container Docker build, and the
   Kafka/OpenSearch/redact-service stack all competing for one machine's
   CPU at the same time -- a slow enough batch could plausibly exceed
   that window mid-batch, triggering a rebalance that reassigns
   already-processed-but-not-yet-committed messages for reprocessing.
2. **Producer-side retry duplicating publishes.** `logstash-output-kafka`
   is configured with `acks => "all"` but without an explicit idempotent-
   producer setting -- a transient ack timeout against floci's Redpanda
   broker under load could cause Logstash to retry (and therefore
   re-publish) a message the broker had actually already accepted.

**Fixed at the point that actually matters, regardless of which
mechanism (or both) caused this specific run's over-count:**
`process_event()` (`src/queue_consumer.py`) used to mint a fresh
`uuid.uuid4()` on every call, with no way to tell "this is the first
time I've seen this message" from "this is a redelivery of a message
I've already fully processed" -- so ANY reprocessing, from either
mechanism above, always produced a brand-new OpenSearch document
instead of overwriting the existing one. Kafka's delivery guarantee is
fundamentally *at-least-once* by design (that's the entire redelivery
mechanism Engineering upgrade 7 exists to provide over BLPOP's weaker
guarantee) -- a consumer that isn't idempotent downstream will always be
vulnerable to exactly this class of duplication under real-world
conditions, this run's specific trigger aside. Added `doc_id_base`
(optional, defaults to the original `uuid4()` behavior for the Redis
path, which has no equivalent stable identifier BLPOP could offer):
`_run_kafka_consumer()` now builds `f"{message.topic}-{message.partition}-{message.offset}"`
-- a Kafka message's own natural, stable identity -- and passes it
through. Redelivery of the identical message now overwrites the same
document instead of creating a new one; audit sub-documents get
`f"{doc_id_base}-audit-{i}"`, preserving Bug 15's own guarantee (audit
IDs must be independent per event within one message) while still being
stable across redelivery of that message as a whole.

**Also mitigated the more-likely-mechanism directly**, not just its
symptom: `max_poll_records` dropped from the library default (500) to
10, so each poll/heartbeat cycle only has to clear a small amount of
synchronous processing before the consumer's group membership renews --
shrinking the window in which a slow batch could threaten the
`max_poll_interval_ms` rejoin timer, without eliminating the underlying
one-message-at-a-time synchronous design (a real, larger redesign --
batching or async processing -- not attempted here).

**Verified, not just argued:** 3 new tests (`test_queue_consumer.py`,
89 total suite tests now, up from 86) confirm directly -- calling
`process_event()` twice with the identical `doc_id_base` produces
exactly one distinct document ID despite two calls (the actual
idempotency guarantee), two different `doc_id_base` values still
produce different IDs (confirming this doesn't collapse genuinely
different messages together), and `_run_kafka_consumer()` itself
actually constructs and passes the `topic-partition-offset` string
through to `process_event()`, not just that `process_event()` supports
the parameter in isolation. **Not yet confirmed against a live rerun**
-- this fix has not been re-run against floci/Redpanda in this sandbox
(no Docker daemon here); the user re-running
`./run_floci_kafka_test.sh` is the actual confirmation that
reconciliation now passes.

---

## Engineering upgrade 6 addendum: floci ELB v2 confirmed control-plane-only, live (2026-08-11)

`./run_floci_elbv2_test.sh` (after Bug 22's fix) ran end to end for the
first time -- provisioned a VPC/subnets/target group/ALB/listener via
real `aws elbv2`/`aws ec2` calls against floci, registered 3 real
`redact-service` replica IPs as targets, and tested both the
control-plane and data-plane separately, exactly as the script's own
"HONEST UNCERTAINTY" header comment said it would.

**Result: floci's ELB v2 is control-plane-only, confirmed live, not
just inferred from floci's own service table.** Every `aws elbv2`/
`aws ec2` command succeeded (VPC, subnets, target group, ALB, listener
all created and described correctly, real ARNs returned) -- the API
surface is real and functional. But `describe-target-health` reported
all 3 targets stuck in `initial` state (never transitioning to
`healthy`, meaning floci never actually ran the configured health
checks against them), and the actual data-plane test -- a real HTTP
request to the ALB's own DNS name
(`redact-alb-<id>.elb.localhost.floci.io`) -- did not succeed. This
matches floci's own documentation listing ELB v2 as "In-process" rather
than "Real Docker" (unlike MSK, which Bug 23 above confirms DOES
forward real traffic): a control-plane emulation that lets tooling
built against the real AWS API shape create/describe/tag these
resources correctly, without floci actually proxying requests through
them.

**Practical implication, stated plainly:** floci's ELB v2 emulation is
useful for testing that infrastructure-as-code / CLI automation against
the real ALB API shape works correctly (a real, useful thing to
validate for free, locally), but it cannot be used as a stand-in for
verifying actual load-balancing BEHAVIOR (traffic distribution,
health-check-driven target removal, connection draining) the way
floci's MSK emulation can for Kafka. `docker compose --scale
redact-service=N` + `redact-lb`'s own nginx-based proxy (already built
and documented, see `docker-compose.yml`'s `redact-lb` comment) remains
the actual verified way this project tests real load-balancing behavior
locally -- this floci ELB v2 test was always scoped as a control-plane/
API-shape check, and that's exactly what it confirmed, cleanly, rather
than something to treat as a load-balancer functionality gap in floci
worth working around.

No code changes from this finding -- a clean negative result,
confirming the disclosed uncertainty in "Engineering upgrade 6" rather
than contradicting it.

---

## Bug 24: stale same-day OpenSearch data + a naive poll-stability check made the Bug 23 rerun look worse, not better (2026-08-11)

The user re-ran `./run_floci_kafka_test.sh` after Bug 23's fix (the
`doc_id_base` idempotency change), doing everything asked --
`docker compose --profile kafka-queued down` /
`docker compose --profile cloud-sim down`, then `git pull` before
re-running. Result: reconciliation FAILED again, this time with **77,812
documents for a 20,000-line corpus -- MORE than the pre-fix run's
60,000, not less.**

**That direction of change is the important diagnostic signal.** A fix
that makes writes more idempotent cannot legitimately raise a document
count above what it was before the fix existed -- if it had, that would
mean the fix made things worse, which would be a real, serious problem
worth taking very seriously. It didn't: the actual cause was two
pre-existing gaps in this test script itself, neither related to
`doc_id_base`'s correctness.

**Gap 1: no clean-slate teardown, and `docker compose down` (without
`-v`) does not delete OpenSearch's data.** `security-logs-anonymized-*`
indices are DATE-suffixed at day granularity
(`redact-pipeline-kafka.conf`/`queue_consumer.py`'s own `date_suffix`),
so a second run on the same calendar day writes into the exact same
index the first run left behind. The user's teardown commands (as
instructed) removed containers, but OpenSearch's named volumes
(`opensearch-data1/2/3`) persisted -- the FIRST run's 60,000 documents
(minted before the Bug 23 fix existed, all with independent random
UUIDs) were still sitting in that index when the second run started.
77,812 total is consistent with 60,000 stale + this run's own
contribution, not a fresh, comparable count at all.
`validation/load_test/run_load_test.sh` already solved this exact
problem correctly (`docker compose down -v`, its own "Tearing down any
previous stack (clean slate)" step) -- `run_floci_kafka_test.sh` never
had the equivalent step, a real gap introduced when that script was
first written (Task #46) and not caught until this live rerun exposed
it.

**Gap 2, compounding it: the poll loop broke on a single `TOTAL >= N`
check, not real stability.** Because the leftover 60,000 already
exceeded `N=20000` before this run's own Logstash/queue-consumer had
processed anything, the very first poll iteration likely satisfied
`TOTAL >= N` and broke immediately -- the live output shows only ONE
poll line before reconciliation ran, consistent with this. That means
this run's OWN processing may not have even finished before
reconciliation was checked, on top of measuring against already-
contaminated data.
`validation/load_test/run_load_test.sh`'s poll loop already solved this
correctly too (3 consecutive unchanged reads, with `TOTAL > 0` required
so an all-zero reading never counts as "stable," originally fixed for a
different reason in that script -- see Bug 13) -- `run_floci_kafka_test.sh`
reinvented a materially weaker version of an already-solved problem
instead of reusing it.

**Both fixed, mirroring `run_load_test.sh`'s proven patterns exactly
rather than inventing new ones:**
- `run_floci_kafka_test.sh` and `run_floci_elbv2_test.sh` both now run
  `docker compose --profile ... down -v` as their very first action,
  before anything else, guaranteeing a genuinely clean slate every run
  (OpenSearch volumes included) regardless of what the previous
  invocation left behind or whether the user remembered to pass `-v`
  themselves.
- `run_floci_kafka_test.sh`'s poll loop now requires 3 consecutive
  identical, non-zero `TOTAL` readings before declaring ingestion
  finished, identical logic to `run_load_test.sh`'s own.

**Not yet re-confirmed against a live rerun** -- this sandbox has no
Docker daemon to run either script. The user's next `./run_floci_kafka_test.sh`
is the actual test of whether Bug 23's `doc_id_base` fix produces a
clean 20,000/20,000 reconciliation once measured against a genuinely
fresh index and a real completion signal, which this rerun -- through no
fault of the underlying fix -- never actually tested.

**Process note, stated plainly:** this is the second time a script
written in this project (Task #46, `run_floci_kafka_test.sh`) reinvented
logic that an earlier, already-debugged script in the same project
(`run_load_test.sh`) had already solved correctly, instead of reusing
it -- Bug 22's missing `.env` bootstrap was the first instance of this
same pattern. Worth naming directly: when writing a new script that
does something structurally similar to an existing, battle-tested one
in this repo, the existing one's solved problems should be checked and
reused deliberately, not re-derived from scratch and re-discovered the
hard way a second time.

---

## Bug 23/24 CLOSED: clean live rerun confirms both fixes, reconciliation PASS (2026-08-11)

The user reran `./run_floci_kafka_test.sh` after Bug 24's fix (clean-
slate `-v` teardown + real 3-poll stability check), on top of Bug 23's
`doc_id_base` idempotency fix. Full clean run against a real floci/
Redpanda broker:

```
Expected total (raw exported lines): 20000
security-logs-anonymized-*:          20000
security-logs-quarantine-*:          0
redact-audit-trail-*:                17819
anonymized + quarantine:             20000
RECONCILIATION: PASS
```

**Exact match, first time this path has ever reconciled cleanly.** The
poll log this time shows real, steadily increasing counts across 48
polls (0 -> 218 -> 574 -> ... -> 20,000, holding flat for exactly 3
consecutive polls before stopping) -- genuine evidence of the new
stability check working as designed, not a lucky single-shot read. The
audit fan-out ratio (17,819/20,000 = 89.1%) lands in the same
neighborhood as every prior run's measured ratio (89.159-89.315% across
the 100K/1M-line synchronous-path runs) -- consistent with, not
necessarily identical to, since this is a different corpus size/sample,
matching how this project has always interpreted this ratio elsewhere
(a consistency check, not an exact-match requirement).

**What this confirms, concretely:**
- Bug 23's `doc_id_base` fix (stable `topic-partition-offset` document
  IDs) does what it was built to do: this run's Kafka consumer group
  handled real message delivery over floci's actual Redpanda broker with
  zero duplicate documents, whatever combination of the two originally-
  hypothesized mechanisms (rebalance-driven reprocessing, producer-side
  retry) was actually at play before -- this is the correctness property
  that matters, not proof of which specific mechanism it was.
- Bug 24's clean-slate teardown and real poll-stability fix both worked
  exactly as designed on a genuine rerun.
- The full Kafka-shaped queue path -- floci MSK provisioning,
  `logstash-kafka` (producer), `queue-consumer-kafka` (consumer group,
  offset-commit redelivery guarantee), OpenSearch write -- is now
  **CONFIRMED LIVE end to end**, not just syntax-checked and unit-tested,
  closing the last open disclosure on Engineering upgrade 7/Bug 23/Bug
  24 in one clean pass.

ROADMAP.md item 13's Kafka bullet updated from "IN PROGRESS" to "DONE,
CONFIRMED LIVE."

---

## 25. 5M-line load test's own pre-flight step could never pass, against the new 3-node OpenSearch cluster

**Impact:** High (blocked Task #47's 5,000,000-line run from starting at
all -- not a data-integrity bug, a hard startup failure). **Status:**
Verified fixed, 2026-08-12 -- the user's rerun completed a full,
clean `RECONCILIATION: PASS` at 5,000,000 lines (see the dedicated
entry below this one for the complete result).

Found from the user's first live attempt at `./run_5m_load_test.sh`
(Task #47's stretch-goal run). The stack failed during startup, before a
single corpus line was processed:

```
Container redact-opensearch-node1 Error dependency opensearch-node1 failed to start
dependency failed to start: container redact-opensearch-node1 is unhealthy
```

**Root cause:** the Logstash config pre-flight step (`docker compose run
--rm logstash bin/logstash --config.test_and_exit ...`, added 2026-08-10
per Bug 16 to catch pipeline syntax errors in seconds instead of minutes)
only starts the containers `logstash` itself directly lists under
`depends_on` in `docker-compose.yml`: `opensearch-node1`, `redact-service`,
`redact-lb`. `docker compose run` does not start every service in the
file, only the named service's own transitive dependency graph -- and
nothing in that graph names `opensearch-node2` or `opensearch-node3`.
That was harmless when OpenSearch was a single-node service (this
pre-flight step's original design target), but since ROADMAP item 12
replaced it with a real 3-node cluster, `opensearch-node1`'s own
healthcheck specifically requires all 3 nodes to have joined
(`"number_of_nodes":3` in its `_cluster/health` response, see that
service's own healthcheck comment in `docker-compose.yml`) -- so node1
can never pass health in this isolated context, `docker compose run`
correctly reports its dependency as permanently unhealthy, and the
pre-flight step fails before Logstash's config is ever actually tested.

This pre-flight step exists in two scripts (`run_1m_load_test.sh` and
`run_5m_load_test.sh`, identical code, `run_5m_load_test.sh` copied from
the other per that script's own header comment) and both had the
identical latent bug -- it just hadn't been triggered yet in
`run_1m_load_test.sh` because that script hasn't been rerun since the
3-node OpenSearch change landed. Confirmed this is specifically an
isolation problem, not a real cluster-formation problem, by cross-
referencing against `run_floci_kafka_test.sh`'s own invocation earlier
the same day: `docker compose up -d --build redact-service redact-lb
opensearch-node1 opensearch-node2 opensearch-node3` (explicitly naming
all 3 nodes) formed a healthy 3-node cluster without incident, using this
exact same `docker-compose.yml` service definition.

**Fix:** both scripts now explicitly bring up the full dependency set --
all 3 OpenSearch nodes, `redact-service`, `redact-lb` -- via `docker
compose up -d --build` immediately before the `docker compose run --rm
logstash ...` pre-flight call, mirroring `run_floci_kafka_test.sh`'s own
proven, explicitly-scoped invocation rather than relying on `docker
compose run`'s implicit (and, for a multi-node service, incomplete)
dependency resolution. By the time the pre-flight `run` executes,
`opensearch-node1` can actually become healthy because all 3 real nodes
already exist.

**Confirmed live, 2026-08-12.** The user reran `./run_5m_load_test.sh`
after pulling this fix. Pre-flight step: `docker compose up -d --build
redact-service redact-lb opensearch-node1 opensearch-node2
opensearch-node3` brought up the full 3-node cluster, `opensearch-node1`
went `Healthy` in 47.5s (well inside the ~195s healthcheck window), and
`docker compose run --rm logstash --config.test_and_exit ...` then
passed immediately (`Configuration OK`) instead of failing on an
unreachable dependency. The subsequent full-stack `docker compose up
--build -d` -- now genuinely starting the 3-node OpenSearch cluster, the
6-container Redis Sentinel HA stack, `redact-service`, `redact-lb`, and
Logstash all together for the first time -- also went healthy without
incident (node1 healthy in 47.5s again, well within budget); the
resource-contention risk flagged when this fix was first applied did not
materialize. This closes Bug 25 as fully verified, not just
syntax-checked. See the dedicated entry below for the full 5,000,000-line
run result this fix unblocked.

---

## 26. Task #47 confirmed live: 5,000,000-line load test, clean PASS

**Impact:** N/A (a confirmation entry, not a bug). **Status:** Verified,
2026-08-12.

With Bug 25's pre-flight fix in place, `./run_5m_load_test.sh` completed
a full, clean run end to end -- the first time this project has been
verified at 5x its previous high-water mark (the 1,000,000-line run in
Bug 15/ROADMAP item 9). Final reconciliation:

```
Expected total (raw exported lines): 5000000
security-logs-anonymized-*:          5000000
security-logs-quarantine-*:          0
redact-audit-trail-*:                4460786
anonymized + quarantine:             5000000
RECONCILIATION: PASS
```

**Exact match** -- every one of the 5,000,000 exported lines landed
correctly anonymized, zero quarantined. Audit fan-out (4,460,786/5,000,000
= 89.2%) lands in the same neighborhood as every prior run's measured
ratio (89.159% at 100,000 lines, 89.315% at 1,000,000 lines) -- a real
consistency check, not a coincidence, confirming correctness held at 5x
further scale rather than just completion at scale.

**Timing:** 22,900s wall clock (~6.4 hours), ~218.3 lines/sec end-to-end
throughput -- closely matching the pre-run estimate (~224 lines/sec,
extrapolated from the slower of the two 1,000,000-line runs' measured
rates) documented in `run_5m_load_test.sh`'s own header comment. The poll
loop ran 1,505 polls before declaring 3 consecutive stable reads and
exiting -- `run_load_test.sh`'s auto-scaled `DEFAULT_MAX_WAIT_SECONDS`
fix (added specifically anticipating this run, previously unverified)
worked exactly as designed: no false timeout, no premature exit.

**What this confirms, concretely:**
- Bug 25's pre-flight fix (bringing up all 3 OpenSearch nodes explicitly
  before the Logstash config-test step) resolved the real blocker, not
  just a superficial symptom.
- The auto-scaling poll deadline added to `run_load_test.sh` for exactly
  this scale of run holds up under a real ~6.4-hour execution.
- The full stack -- 3-node OpenSearch, 6-container Redis Sentinel HA,
  `redact-service`, `redact-lb`, Logstash -- starting and running
  together at once does not introduce the resource-contention risk that
  was flagged (but unconfirmed either way) when Bug 25's fix was applied.
- This project's largest verified single-machine run is now 5,000,000
  lines, up from 1,000,000 -- Task #47 (ROADMAP item 9's stretch goal) is
  closed.

**Scoped honestly, unchanged from every prior load-test entry:** this
remains a single-machine, single-shard-per-node vertical-scaling test.
It does not establish anything about real multi-machine infrastructure or
actual terabytes/day production volume -- see item 9/item 12's own
closing caveats in `ROADMAP.md`, restated here rather than re-argued.

ROADMAP.md item 9 updated with the full 5,000,000-line result; Task #47
marked complete.

---

## Engineering upgrade 12: SASL_SSL support added for real managed Kafka (Task #53)

**Status:** Implemented and syntax/unit-tested, 2026-09-05; not yet
live-confirmed (needs the user's Confluent Cloud cluster and their
machine to actually run against it).

Task #53 needs the Kafka-shaped queue path (already confirmed live
against floci's local, unauthenticated Redpanda emulation -- see "Bug
23/24 CLOSED" above) validated against a real managed Kafka too. Real
managed Kafka services -- Confluent Cloud specifically, per
`CLOUD_SETUP.md`'s reasoning for why it's a better fit here than real
AWS MSK (MSK has no free tier and bills per broker-hour immediately) --
require SASL_SSL authentication, which neither `src/queue_consumer.py`'s
Kafka consumer nor `logstash/redact-pipeline-kafka.conf`'s Kafka
producer previously supported at all -- both were written only against
floci's PLAINTEXT-only local broker.

**Added, both sides of the queue:**
- `src/queue_consumer.py`: `KAFKA_SECURITY_PROTOCOL` (default
  `PLAINTEXT`, unchanged from before), `KAFKA_SASL_MECHANISM`,
  `KAFKA_SASL_USERNAME`, `KAFKA_SASL_PASSWORD` -- all opt-in, all empty/
  off by default. `_run_kafka_consumer()` now builds its `KafkaConsumer`
  kwargs as a dict and only adds the `sasl_*` keys when
  `KAFKA_SASL_MECHANISM` is actually set, rather than always passing an
  empty string for it -- deliberate, since an empty-but-present
  `sasl_mechanism` kwarg is not guaranteed to behave identically to the
  kwarg being absent across client library versions, and this project
  has already been burned once (Bug 22) by assuming an empty/default
  value would be silently harmless without checking.
- `logstash/redact-pipeline-kafka.conf`: matching `security_protocol`/
  `sasl_mechanism`/`sasl_jaas_config` fields on the `kafka` output
  block, same default-to-PLAINTEXT-and-empty pattern via Logstash's own
  `${VAR:default}` interpolation.
- `docker-compose.yml`: both `logstash-kafka` and `queue-consumer-kafka`
  service blocks gained the matching environment passthrough
  (`KAFKA_SECURITY_PROTOCOL`, `KAFKA_SASL_MECHANISM`,
  `KAFKA_SASL_USERNAME`/`KAFKA_SASL_JAAS_CONFIG` as appropriate to each
  side) -- without this, exporting the right values in a shell before
  `docker compose up` would have done nothing, since Compose only passes
  through environment variables a service's own `environment:` block
  actually references.
- `run_confluent_kafka_test.sh` (new, repo root): the real-cluster
  counterpart to `run_floci_kafka_test.sh`. Checks for
  `CONFLUENT_BOOTSTRAP_SERVERS`/`CONFLUENT_API_KEY`/
  `CONFLUENT_API_SECRET` in `.env` (real secrets from the user's own
  Confluent Cloud console -- never invented or defaulted the way this
  project's own five internal `REDACT_*_KEY` values are), exports the
  SASL_SSL wiring, and reuses `run_floci_kafka_test.sh`'s already-proven
  clean-slate teardown and 3-consecutive-stable-polls reconciliation
  logic rather than rewriting it. Uses a smaller corpus (2,000 lines,
  vs. floci's 20,000) specifically because this run has a real, if
  small, per-GB Confluent Cloud data-transfer and storage cost --
  confirming the architecture works against real infrastructure doesn't
  need re-measuring throughput floci's local test already covered.
  Ends with an explicit cleanup reminder to delete the Confluent
  cluster/topic afterward, since it keeps existing (and could keep
  costing something) until the user does that themselves.

**Verified so far:** syntax-checked (`bash -n` on the new script,
`python3 -m py_compile` on `queue_consumer.py`), and the full existing
`tests/test_queue_consumer.py` suite (13 tests, all mocking the network
layer) still passes unchanged -- the SASL additions are purely additive
kwargs, nothing about the existing PLAINTEXT/floci code path changed.

**Confirmed live, 2026-09-05/06.** The user ran `run_confluent_kafka_test.sh`
against their real Confluent Cloud cluster (`Redact_1`, Basic tier, AWS
us-east-2, topic `redact-raw-events`). Full clean run:

```
Expected total (raw exported lines): 2000
security-logs-anonymized-*:          2000
security-logs-quarantine-*:          0
redact-audit-trail-*:                1757
anonymized + quarantine:             2000
RECONCILIATION: PASS
```

Exact match, zero quarantined, real SASL_SSL authentication (both the
Logstash Kafka producer and `queue_consumer.py`'s Kafka consumer group)
against a genuine external managed-Kafka control plane, not floci's
local emulation. Audit fan-out (1,757/2,000 = 87.9%) sits in the same
neighborhood as every other run's ~89% -- the small delta is consistent
with normal sample-to-sample variance on a corpus this size (2,000
lines, deliberately smaller than floci's 20,000 given this run's real
per-GB cost), not a new defect.

`queue-consumer-kafka`'s log showed one cosmetic item, not a real
problem: a `DeprecationWarning` from kafka-python about
`value_deserializer` not implementing its `Deserializer` interface --
a library-level style warning, harmless, doesn't affect correctness
(confirmed by the exact reconciliation match above). `logstash-kafka`'s
log showed repeated `Unable to retrieve license information` /
`elasticsearch: Name or service not known` errors -- also harmless:
that's Logstash's own built-in X-Pack monitoring/license-checker trying
to phone home to a literal hostname `elasticsearch` that doesn't exist
in this project's topology (which uses `opensearch-node1/2/3`, not
Elasticsearch) -- unrelated to the Kafka output this test actually
exercises, and present in every Logstash container this project has
ever run, not something new introduced by this change.

**This closes Task #53.** Both floci's local emulation (Bug 23/24,
above) and now a real external managed Kafka control plane have
confirmed the same Kafka-shaped queue path -- producer, consumer group,
offset-commit redelivery guarantee, doc_id_base idempotency (Bug 23) --
end to end.

---

## Engineering upgrade 13: Task #52 (real Azure Load Balancer), five real bugs found and fixed across successive live reruns

**Status:** Done, confirmed live 2026-09-06. Five real, live bugs
(a)-(e) below were found and fixed across successive reruns against a
real Azure for Students subscription; the final rerun confirmed
Azure's Standard Load Balancer does real data-plane traffic
proxying, closing Task #52.

`run_azure_lb_test.sh` (added for Task #52, mirroring
`run_floci_elbv2_test.sh`'s exact scope against floci's local ELB v2
emulation) hit two real, live issues on its first run, neither of
which showed up in this sandbox's syntax-check (no Azure account
available here to test against).

**Bug (a): Azure for Students subscriptions are region-restricted by
policy, and the restricted list is subscription-specific.** The
script's hardcoded `LOCATION="eastus"` failed with
`(RequestDisallowedByAzure)`: "This policy maintains a set of best
available regions where your subscription can deploy resources."
There is no fixed public list of allowed regions -- it varies per
subscription. Found the real list for this subscription via:

```
az policy assignment list --output json | python3 -c \
  "import sys,json; [print(a['displayName'], a.get('parameters',{}).get('listOfAllowedLocations',{}).get('value')) for a in json.load(sys.stdin) if 'listOfAllowedLocations' in a.get('parameters',{})]"
```

which returned `['northcentralus', 'denmarkeast', 'westus',
'belgiumcentral', 'mexicocentral']` for this specific subscription.
**Fix:** `LOCATION` is now the script's first positional argument
(default `eastus` preserved for accounts without this restriction)
instead of hardcoded, with the discovery command above documented
directly in the script's own comment so this doesn't need rediscovering
from scratch on a different subscription.

**Follow-on gotcha, also found live:** a resource group's location is
fixed at creation and cannot be changed by rerunning `az group create`
with a different `--location` -- the second attempt (now with the
correct region argument) failed with `(InvalidResourceGroupLocation)`
because the first attempt had already created `redact-lb-test` in the
disallowed `eastus` region before the VNet step (the actual
region-restricted resource) failed. Resolved by deleting the empty
resource group (`az group delete --resource-group redact-lb-test
--yes`) and rerunning clean -- not a script bug per se, just a real
consequence of Bug (a) above that's worth recording so a future rerun
under different circumstances isn't surprised by it.

**Bug (b): `az vm create` has no `--lb`/`--backend-pool-name` flags.**
The script's original VM-creation step invoked
`az vm create ... --lb "$LB" --backend-pool-name "$BACKEND_POOL"`,
which failed with `unrecognized arguments`. Root cause: I wrote this
by pattern-matching against `az vmss create` (Virtual Machine Scale
Sets), which genuinely does have `--load-balancer`/`--backend-pool-
name` flags for wiring a whole scale set into a backend pool at
creation time, without checking `az vm create`'s own actual parameter
list first for a plain single VM. This is exactly the class of mistake
this document exists to catch and record rather than paper over --
same shape as Bug 22's "invented shell invocation instead of checking
real syntax," just for the Azure CLI instead of `docker compose`.

**Fix:** the documented, correct pattern for attaching a single VM to
an existing Standard Load Balancer's backend pool: create the VM's NIC
explicitly first (`az network nic create`, in the target subnet),
attach that NIC's IP configuration to the backend pool
(`az network nic ip-config address-pool add --nic-name ... --ip-
config-name ipconfig1 --lb-name ... --address-pool ...` --
`ipconfig1` is Azure CLI's own documented default IP-config name for a
newly created NIC, not invented), then create the VM referencing the
NIC by name (`--nics`) instead of the VM-level `--vnet-name`/
`--subnet`/`--nsg` flags (which become the NIC's own settings instead
once a VM is created against an explicit NIC list).

With bugs (a) and (b) fixed, the next live rerun got all the way
through resource group, VNet/NSG/subnet, public IP, Standard Load
Balancer, health probe, LB rule, and the first VM's NIC creation plus
backend-pool attachment -- confirming both fixes actually work against
real Azure, not just syntactically. It then hit a third real, live
issue at the VM-creation step itself.

**Bug (c): `Standard_B1s` capacity unavailable in `northcentralus` at
the time of the run.** Failed with `(SkuNotAvailable)`: "Following
SKUs have failed for Capacity Restrictions: Standard_B1s ... currently
not available in location 'northcentralus'. Please try another size or
deploy to a different location or different zone." This is a real,
transient per-region/per-time Azure capacity limit, not a
configuration mistake or a script bug -- it can appear and disappear
independently of anything in this repo.

**Fix:** VM size is now the script's second positional argument
(`VM_SIZE="${2:-Standard_B2s}"`, mirroring how `LOCATION` was made the
first positional argument for Bug (a) above), defaulting to
`Standard_B2s` instead of the previously-hardcoded `Standard_B1s`. Both
`az vm create` calls in the per-VM loop now reference `"$VM_SIZE"`.
If `Standard_B2s` also hits a capacity error, the fix is the same
pattern: pass a third size, or fall back to one of the other allowed
regions from Bug (a)'s discovery command
(`denmarkeast`/`westus`/`belgiumcentral`/`mexicocentral`), e.g.:

```
./run_azure_lb_test.sh northcentralus Standard_B2s
```

**Update, same day:** the live rerun with `Standard_B2s` hit the exact
same `(SkuNotAvailable)` error, still in `northcentralus`. Confirms
this is genuinely a transient regional capacity issue rather than
anything specific to `Standard_B1s` -- consistent with Microsoft's own
documented behavior for this error (see below), not something a
different single guessed size was ever guaranteed to fix.

**Researched against official Microsoft documentation** (Microsoft
Learn, "SKU not available errors - Azure Resource Manager":
learn.microsoft.com/azure/azure-resource-manager/troubleshooting/
error-sku-not-available) rather than continuing to guess sizes one at
a time:

- The documented command to check whether a size is usable in a
  region/subscription is `az vm list-skus --location <region> --size
  <size> --all --output table`, and to list every size with zero known
  restrictions: `az vm list-skus --location <region> --resource-type
  virtualMachines --query "[?length(restrictions)==\`0\`]"`.
- Critically, Microsoft's own doc only promises this reports
  subscription/region-level *restrictions* (things like
  `NotAvailableForSubscription`) -- it explicitly does not expose
  real-time capacity. There is no documented Azure API that does. A
  size can show zero restrictions and still fail at the moment of
  `az vm create` if that region's physical capacity for that size is
  temporarily exhausted, which is exactly what's been happening here.
- A separate, real failure mode with an easily-confused symptom: zero
  quota for a VM family produces a similarly-worded failure that has
  nothing to do with capacity and needs a quota increase instead
  (`az vm list-usage --location <region> --output table` to check).

**Fix:** added a pre-flight step before VM creation that runs both
`az vm list-skus` commands above against `$LOCATION`/`$VM_SIZE` and
prints the result -- informational only (explicitly documented in the
script's own comment as non-blocking and non-guaranteeing, per
Microsoft's own caveat above), so a rerun surfaces genuinely-viable
candidate sizes before spending another full `az vm create` attempt on
one likely to fail the same way. This doesn't eliminate the
possibility of another `SkuNotAvailable` -- nothing can, by Microsoft's
own admission -- but it replaces blind guessing with the documented
diagnostic path.

**Update, same day, live rerun:** with the pre-flight check in place,
`Standard_B2s` failed identically to `Standard_B1s` -- but this time
the pre-flight's own `az vm list-skus --all` output showed exactly
why: both sizes are listed as `NotAvailableForSubscription, type:
Location, locations: northcentralus` for this specific Azure for
Students subscription. That's a documented subscription/region
restriction, not the transient-capacity theory assumed after the
first failure. Guessing a third size by hand would have cost another
full manual rerun to find out the same way.

**Fix, escalated:** rather than keep guessing one size at a time,
the script now builds a real candidate list from the sizes its own
pre-flight check reports as unrestricted in `$LOCATION` (ARM64
"p"-suffixed B-series sizes filtered out, since `--image Ubuntu2204`
resolves to x64 and isn't guaranteed compatible with Ampere Altra/
Cobalt silicon), and the VM-creation loop tries them in order,
falling through to the next candidate on failure instead of stopping
at the first `SkuNotAvailable`. The size that succeeds for the first
VM is reused as the first candidate for the second, so both backends
land on the same size. If every candidate fails, the script now exits
with a pointer to try a different allowed region from Bug (a)'s
discovery command, rather than a bare stack trace.

**Bug (d), found on the very next live rerun: `mapfile` doesn't exist
on macOS's system bash.** The auto-fallback fix above used `mapfile
-t` to build the candidate-size array. Failed immediately with
`mapfile: command not found`. Root cause: `mapfile` (aka `readarray`)
was added in bash 4.0, but macOS ships bash 3.2 as `/bin/bash` and has
since Apple stopped updating it years ago (a licensing choice, not an
oversight -- bash moved to GPLv3 after 3.2, which Apple won't ship).
The user's Mac has a newer bash available via Homebrew (installed
earlier in this same session for `az`), but this script's own
`#!/bin/bash` shebang resolves to the system one regardless. This
sandbox's own bash is 5.1, which is exactly why `bash -n` here didn't
catch it -- a syntax check doesn't verify that every builtin the
syntax uses actually exists in the shell that will run it. Genuinely
useful lesson for this repo generally: **any script meant to run on
the user's own Mac needs checking against bash 3.2 features, not just
against this sandbox's newer bash.**

**Fix:** replaced `mapfile -t UNRESTRICTED_SIZES < <(...)` with a
portable `while IFS= read -r size_name; do ... done < <(...)` loop,
which works identically on bash 3.2 and any newer bash. While auditing
for this, also hardened `"${UNRESTRICTED_SIZES[@]}"` (and the
`CANDIDATE_SIZES` array built from it) with a `:-` empty-array
fallback and an explicit empty-string skip, since old bash versions
(pre-4.4) are known to throw an unbound-variable error under `set -u`
when expanding an empty array without one -- this script already runs
under `set -euo pipefail`, so an all-restricted `az vm list-skus`
response could otherwise have crashed the pre-flight step itself
rather than degrading to "just try `$VM_SIZE`."

**Confirmed live, 2026-09-06.** With Bug (d) fixed, the rerun's
auto-fallback loop tried `Standard_B2s`, `Standard_B16als_v2`, and
`Standard_B16as_v2` (all three failed -- the first two `Not
AvailableForSubscription`, matching Bug (c)'s finding; the CLI's own
already-noted `RuntimeError: The content for this response was
already consumed` cosmetic bug fired for each, but the loop correctly
read the real exit code underneath it and moved on regardless), then
succeeded on `Standard_B2als_v2` for both backend VMs.

**Bug (e), found on this same rerun: `az network lb
show-backend-health` does not exist.** All 6 polls in the health-probe
step printed only `(not ready yet)` -- the command itself was failing
silently every time (its stderr was redirected to `/dev/null`), not
reporting an actual unhealthy state. Checked against the complete,
official `az network lb` command reference
(learn.microsoft.com/cli/azure/network/lb) -- no `show-backend-health`
subcommand exists there or anywhere else in the current Azure CLI. I'd
invented it by pattern-matching `run_floci_elbv2_test.sh`'s `aws elbv2
describe-target-health` without checking the real Azure CLI surface --
the same class of mistake as Bug (b), just one this project caught a
step later because its failure was accidentally swallowed by the
loop's own error handling instead of surfacing immediately.

**Fix:** replaced the fake health-probe poll with a plain 90-second
wait for cloud-init to finish booting, plus a pointer to the real
mechanism (Microsoft Learn, "Manage Azure Load Balancer health
status": a per-rule REST call, async via a Location header, or the
Azure portal's own health panel -- not a single synchronous CLI
command, and not worth inventing an unverified REST flow for here when
Part 5 already gives a stronger, more direct answer).

**Part 5 (the test's actual point) passed on the very first attempt:**
a real `curl` through the load balancer's public IP
(`64.236.128.11:8080/health`) returned `"OK from redact-backend-1"`.
**This closes Task #52.** Azure's Standard Load Balancer does real
data-plane traffic proxying -- both the control plane (resource
creation, backend-pool attachment) and the data plane (actual request
forwarding to a live backend) are now confirmed against a real cloud
account, the exact question `run_floci_elbv2_test.sh` could not answer
for floci's local emulation.

**Five real, live bugs found and fixed across this one Task #52 test
script**, none of which showed up in this sandbox's own syntax
checking, each documented above as it was found rather than glossed
over: (a) subscription-specific region restrictions, (b) invented
`az vm create` flags, (c) a real subscription-level size restriction
initially mistaken for transient capacity, (d) a macOS bash 3.2
portability gap (`mapfile`), and (e) an invented, nonexistent Azure CLI
subcommand. Cleanup reminder from the script itself, restated here
since these resources bill by the hour until removed:

```
az group delete --resource-group redact-lb-test --yes --no-wait
```

Sources:
- [SKU not available errors - Azure Resource Manager](https://learn.microsoft.com/en-us/azure/azure-resource-manager/troubleshooting/error-sku-not-available)
- [Backend Pool Management - Azure Load Balancer](https://learn.microsoft.com/en-us/azure/load-balancer/backend-pool-management) (confirms the NIC-based backend-pool attach pattern used in Bug (b)'s fix above matches Microsoft's own documented CLI example)

## Engineering upgrade 14: `download_loghub.sh` was referenced but never written (found during pre-resubmission dataset research, 2026-09)

**Status:** Fixed, not yet run (needs real internet access, same disclosed limitation as `prepare_cloudtrail_dataset.py`).

While researching additional real-world datasets to strengthen the paper before resubmission, found that `validation/real_data/inject_and_evaluate.py`'s own module docstring has always instructed `Run download_loghub.sh first.` -- but that file never existed anywhere in the repository. Not a regression (the five raw Loghub samples it should fetch, `OpenSSH_2k.log`/`Linux_2k.log`/`Thunderbird_2k.log`/`OpenStack_2k.log`/`Zookeeper_2k.log`, are all correctly `.gitignore`d, consistent with this project's "no real data anywhere in this repository" claim) -- but a genuine dangling reference: anyone following the script's own documented instruction, including a fresh chat with no memory of this project, would hit a missing-file error with no path to resolving it from the repo alone.

**Real, useful side effect of chasing this down:** two of `inject_and_evaluate.py`'s five real-data conditions -- `IP_ONLY_DATASETS = ['OpenStack', 'Zookeeper']` -- have apparently never actually been run, since the file that would fetch their input never existed. These two are the ones that matter most for a gap this project's own README already discloses about itself: the CloudTrail sample's `sourceIPAddress` field is real-*shaped* but publisher-anonymized (run through a format-preserving substitution tool before release, see `prepare_cloudtrail_dataset.py`'s docstring), so it can only validate "does the IP regex fire on a realistic value," not "does this work against a genuinely real, unmodified IP." OpenStack/Zookeeper's connection and API logs contain real, unmodified IPs with no substitution -- confirmed directly by fetching `OpenStack_2k.log` and checking line 1: a real `10.11.10.1` embedded in a genuine `nova-api` request log line, not a placeholder.

**Fix:** wrote `validation/real_data/download_loghub.sh`, fetching all five raw samples from Loghub's own GitHub (`raw.githubusercontent.com/logpai/loghub/master/<System>/<System>_2k.log` -- confirmed reachable and correctly shaped via a direct fetch during this research pass, no request form needed for these 2k samples specifically, unlike some systems' full raw logs). Matches `prepare_cloudtrail_dataset.py`'s existing disclosure: must run outside this project's sandbox, whose network allowlist blocks `raw.githubusercontent.com`.

**Confirmed live, 2026-09.** Run by the user: all five samples were already present locally (never committed, per `.gitignore`, but already downloaded from an earlier session). `inject_and_evaluate.py` ran clean end to end after one environment fix (`pyahocorasick` wasn't installed in the local, non-Docker Python environment this script runs in -- `pip install -r requirements.txt` resolved it). Results, first time these two conditions have ever actually executed:

- **OpenStack: P=0.989, R=1.000 (TP=1225, FP=14, FN=0).** A clean, genuinely new data point: real, unmodified IPs correctly detected at effectively the same quality as every synthetic-corpus IP result already published.
- **Zookeeper: P=0.476, R=1.000 (TP=1413, FP=1557, FN=0).** Recall perfect, but precision collapsed -- more false positives than true positives. See "Engineering upgrade 15" below for the root cause and fix.

**Separately, also researched for the same resubmission-strengthening pass, not yet acted on:**
- **n2c2 (formerly i2b2) de-identification corpus** -- the actual gold-standard PHI de-identification benchmark (real clinical notes, authentic PHI replaced with realistic surrogates, HIPAA Safe Harbor categories), freely available via Harvard DBMI under a data use agreement. REDACT's PHI claim currently rests on MRN-shaped regex alone, with no real PHI-labeled ground truth anywhere in the validation suite -- this would close that specific gap. Caveat found during research: at least one source reported the Harvard portal intermittently unavailable as recently as mid-2026, and DUA approval isn't instant, so this is a "start now" item, not a same-session one.
- **PIIBench** (arXiv:2604.15776, April 2026) -- a new unified 48-entity-type, 3.35M-mention benchmark across ten general-text NER/PII datasets. Best published system (Presidio) scored F1=0.14 with zero recall on most entity types. Worth running REDACT's detection layer against as a cross-check of the underlying Presidio component specifically, but its constituent datasets are general free text (financial documents, Wikipedia NER, synthetic PII), not log-shaped -- reporting this against REDACT's *log pipeline* claim rather than its *NER component* claim would be an honest-sounding overclaim a reviewer would rightly catch.

## Engineering upgrade 15: Zookeeper's real-IP condition found a second CREDIT_CARD collision -- fixed with a Luhn check instead of a third exclusion

**Status:** Fix applied, NOT yet verified live -- this session's sandbox shell is unavailable (see the standing note about a wedged, unrecoverable "no space left on device" state), so `pytest tests/` and `validate.py` could not be re-run here to confirm no regression. Needs that confirmation before being treated as settled.

Zookeeper's real, unmodified-IP condition (see Engineering upgrade 14 above) measured P=0.476 with 1,557 false positives against only 1,413 true IP spans on its first-ever run -- more false positives than true positives, a real precision collapse on real data, same shape as the CloudTrail account-ID collision (Bug 17) but not the same cause.

**Root-caused by direct inspection** (reading `Zookeeper_2k.log` and grepping for the suspect pattern, no dedicated diagnostic script needed this time): ZooKeeper's `SendWorker`/`RecvWorker` log lines embed a recurring internal thread ID, `188978561024` -- exactly 12 digits, the same low end of `CREDIT_CARD`'s `\d{12,19}` regex range that Bug 17 already found colliding with AWS account IDs. Confirmed via `Grep`: this exact 12-digit string appears in 1,128 of the file's 2,000 lines -- the clear majority of the 1,557 false positives. `_is_aws_account_id_context()` (Bug 17's fix) correctly does NOT suppress this match, because it isn't the AWS ARN/JSON shape -- this is a different, genuinely new context colliding with the same regex, not a bug in that fix.

**Fix, generalized rather than repeated:** a third source-specific exclusion (after AWS's) would mean every future log format with an incidental 12+-digit constant needs its own hand-written carve-out, indefinitely. Added a Luhn checksum (`_luhn_valid()` in `src/detect.py`) as a supplementary filter on all `CREDIT_CARD` regex hits instead: real credit card numbers (and Faker-synthetic ones -- Faker's card provider computes a real check digit, standard practice) are Luhn-valid by construction; incidental long integers like thread IDs and zxids almost never are. Verified by hand for this exact case: `188978561024` sums to 56 under the standard Luhn doubling procedure -- not a multiple of 10, correctly fails the check. The existing AWS-specific exclusion was kept rather than removed (defense in depth; not every AWS account ID's Luhn-failure was individually re-verified here).

**Real risk this fix could introduce, disclosed rather than assumed away:** if Faker's `credit_card_number()` output for any card network/locale this project's synthetic corpus uses turns out NOT to be Luhn-valid, this filter would silently regress synthetic-corpus `CREDIT_CARD` recall -- exactly the "fix one number, quietly break another" mistake Bug 17's own fix was written to avoid. Not verified here because this session has no working shell. **Required before this is considered done:**

```
python3 -m pytest tests/
python3 validate.py
cd validation/real_data && python3 inject_and_evaluate.py
```

The last command should show Zookeeper's precision recover sharply (ideally close to OpenStack's 0.989) with recall still 1.000; the first two should show no change at all in synthetic-corpus `CREDIT_CARD` numbers. If synthetic recall drops, this fix is wrong as written and needs to be reverted or scoped differently -- report back either way.

**Confirmed live, 2026-09.** Run by the user: `validate.py`'s synthetic-corpus `CREDIT_CARD` numbers unchanged (1.000/1.000/1.000, 68/0/0) -- no regression. Zookeeper's precision jumped from 0.476 to 0.910 (FP 1557 -> 140), recall unchanged at 1.000. The fix generalized further than the one dataset that surfaced it: OpenStack FP 14 -> 2, CloudTrail naive FP 692 -> 253 (P 0.750 -> 0.892) and field-gated FP 678 -> 239 (P 0.754 -> 0.897) -- both on top of Bug 17's already-applied AWS-specific exclusion, confirming there were other non-AWS-context digit-run collisions in that data too. Linux FP 122 -> 108, Thunderbird FP 276 -> 266. OpenSSH unaffected (FP 49 both times -- no CREDIT_CARD collisions in that format).

`pytest tests/` initially failed on `test_aws_account_id_credit_card_exclusion` -- correctly, not a bug in the fix. Two of that regression test's own literal test values (`411111111111`, a truncated variant of the well-known 16-digit Visa test number, and a reused AWS account ID string) were never actually Luhn-valid to begin with -- hand-verified both fail the checksum (sum to 24 and 56 respectively, neither a multiple of 10). The test fixtures were stale, not the code: `CREDIT_CARD` now means "a digit run that also passes a checksum," so its own regression guard needed genuinely valid examples. Replaced both with hand-constructed Luhn-valid 12-digit values (`411111111117`, `555555555559`, verified by hand to sum to 30 and 40) and added a new check (2b) using the exact real value found live (`188978561024`, from Zookeeper) to directly guard against this exact regression reappearing. All checks pass now.

The `validate.py` drift-detection failures seen in this same run ("3 fields incorrectly flagged" / "4 fields flagged total") are unrelated to this change -- consistent in count and description with the already-documented pre-existing flakiness in README.md (flattened-username layer's ~50% recall producing sample-to-sample noise against a fixed 5% drift threshold, see Bug 11). Not independently re-verified against a pre-fix baseline this session, but the mechanism described has nothing to do with CREDIT_CARD detection.

Committed: `src/detect.py`, `validation/aws_account_id_credit_card_exclusion_test.py`, this file. Pushed (`6aaa30b`).

**Zookeeper's remaining 140 false positives: root-caused and fixed, see "Engineering upgrade 16" below.**

**`data/confluent_kafka_test_corpus_2000.jsonl` found untracked in `git status`.** Same synthetic, fixed-seed-generated shape as every other corpus this project already gitignores (`data/kafka_queue_test_corpus_*.jsonl` etc.) -- `run_confluent_kafka_test.sh` was added after those `.gitignore` rules and never got its own line. Added `data/confluent_kafka_test_corpus_*.jsonl` to `.gitignore`, matching the existing convention exactly.

**n2c2/i2b2 PHI de-identification corpus: access path documented, not yet obtained.** This one genuinely can't be completed by this project's tooling or an assistant -- it requires an individual data use agreement through Harvard DBMI tied to the requester's own identity, not a direct download. Wrote `validation/real_data/PHI_DATASET_ACCESS.md` with the current portal URL, DUA link, and a disclosed caveat (at least one source reported the dataset "temporarily unavailable" via this portal as recently as mid-2026). Deliberately did NOT write a `prepare_n2c2_dataset.py` parser against a remembered/assumed file format -- this project's own repeated lesson (Bug 17, the Azure `show-backend-health` mistake) is to confirm a real file's actual structure before building a parser against it, not before. That script gets written once a real n2c2 sample is actually in hand.

## Engineering upgrade 16: Zookeeper's residual false positives root-caused -- Log4j-style logger-context tags misread as PERSON

**Status:** Fix applied, NOT yet verified live (same standing sandbox-shell limitation as Engineering upgrade 15).

`diagnose_zookeeper_false_positives.py` (written for exactly this purpose, see the note above) was run live by the user against `Zookeeper_2k.log`: all 140 remaining false positives were type PERSON, and 130 of them (93%) were the exact same recurring string, `QuorumPeer[myid=1]/0:0:0:0:0:0:0:0:2181` -- a Log4j-style bracketed logger/thread context tag (class name, `[myid=1]` config, an IPv6 zero-address, and a port number), not natural language, that spaCy's NER misreads as a person's name. A handful of other hits shared the same shape (`0x14ed93111f20005`-style session IDs, one `/10.10.34.13:47234` connection fragment); the remaining 4 (`sessionid` x2, `Linux` x2) were plain alphabetic words with no distinguishing structure -- a genuinely diffuse NER weakness, not something a targeted fix should chase.

**Fix:** `scan_ner()` in `src/detect.py` now discards a PERSON hit if its matched text contains any of `[ ] / : $ @`. Deliberately general (a character-class filter, not a match against the one literal string this bug happened to surface) and deliberately scoped to punctuation, not digits -- a username containing a digit is a real, legitimate case this project's own synthetic corpus produces (Faker's `user_name()` provider), and this session has no way to run Faker/spaCy to re-verify that empirically, so digits were left alone rather than risk a regression that couldn't be checked. None of the six excluded punctuation characters appear in any name or username this project generates or has ever seen in real data, under either of its two supported PERSON conventions (spaced "First Last" or flat "firstlast").

Added `validation/person_structural_exclusion_test.py` and wired it into `tests/test_fast_validation.py::test_person_structural_exclusion`: checks the real Zookeeper string is excluded, a *different* bracketed logger-context tag is also excluded (guards against overfitting to the one literal value), and an ordinary spaced person name ("John Smith") is unaffected -- the actual regression risk being guarded against, not just "does it catch the bug."

**Not yet verified live:** needs

```
python3 -m pytest tests/
python3 validate.py
cd validation/real_data && python3 inject_and_evaluate.py
```

Expect: the new test passes, `validate.py`'s PERSON recall numbers are unchanged (this filter only removes hits that were never real names to begin with), and Zookeeper's precision improves further from 0.910 toward something close to OpenStack's 0.998-0.989 range, with the 4 diffuse "sessionid"/"Linux" false positives as an accepted, disclosed residual. If PERSON recall drops anywhere else, this fix is wrong as written and needs to be reverted or scoped differently -- report back either way, same standard as every other entry in this document.

**Confirmed live, 2026-09.** Run by the user. `validate.py`'s PERSON recall unchanged (0.372, identical to before); precision *improved* as a side benefit (0.657 -> 0.740, FP 171 -> 115) -- this filter's punctuation exclusion apparently also caught some log4j-shaped false positives in the main synthetic-corpus validation, not just Zookeeper. Real-data PERSON spaced recall unaffected everywhere: OpenSSH 99.1%, Linux 98.4%, Thunderbird 100.0%, all identical to pre-fix numbers -- direct proof this filter never touches a real name. Zookeeper's precision climbed from 0.910 to **0.994** (FP 140 -> 9, recall still 1.000) -- the 9 remaining are the already-disclosed diffuse "sessionid"/"Linux" residual, now the accepted floor. Bonus, unplanned improvement: CloudTrail's precision also rose again, naive 0.892 -> 0.945 (FP 253 -> 122) and field-gated 0.897 -> 0.951 (FP 239 -> 108) -- the same punctuation-shaped false positives were apparently present in CloudTrail's JSON structure too (colons, brackets, slashes are common in ARNs and nested paths), closing more of the gap Bug 17 first found without any CloudTrail-specific change.

`test_person_structural_exclusion` initially failed -- correctly identified as a bug in the TEST, not the fix: check 3's assertion required the matched text to equal exactly `"John Smith"`, but spaCy returned a merged span, `"User John Smith"` (folding the sentence-initial capitalized "User" into the entity boundary, a real but harmless spaCy quirk, not something either the old or new code controls). Fixed the assertion to check containment (`"John Smith" in text`) instead of exact equality -- the real safety property (a genuine name still gets caught) was proven independently by `inject_and_evaluate.py`'s own unchanged spaced-recall numbers regardless of this test's own bug.

Committed and pushed (`7a503ad`, plus the test-assertion fix). This closes both the Luhn (Engineering upgrade 15) and structural-exclusion (Engineering upgrade 16) fixes with real, confirmed, no-regression results across every real-data condition this project has.

## Engineering upgrade 17: PIIBench evaluation harness added and run live; detection/policy decoupling scoped (both landscape-scan follow-ups)

**Status:** Both pieces complete. PIIBench evaluation confirmed live against the full 5,000-record paper-comparison subset, 2026-09, run by the user locally -- three real, distinct methodology bugs found and fixed along the way (all in this project's own evaluation script, not in REDACT's detection code), each root-caused before being fixed rather than assumed.

**PIIBench.** Confirmed real, current structure by fetching the paper (arXiv:2604.15776) and the benchmark repo (`github.com/pritesh-2711/pii-bench`) directly rather than assuming a format -- same discipline this project already applies to n2c2 above. Unlike n2c2, PIIBench needs no DUA: it's Apache-2.0 code with data on HuggingFace (`Pritesh-2711/pii-bench`). Wrote `validation/piibench/evaluate_piibench.py` (loads the benchmark's own fixed `test_5k.jsonl` paper-comparison subset, converts its BIO labels to gold character spans exactly -- no fuzzy search needed, since this script constructs the joined text itself -- and scores REDACT's `detect.detect_all()` against it) and `validation/piibench/README.md` (methodology and devil's-advocate writeup). Two decisions matter and are stated explicitly in both files: (1) REDACT is scored only on the five entity types it actually claims (PERSON, EMAIL, SSN, CREDIT_CARD, IP) -- scoring it against PIIBench's full 48/82-type taxonomy would produce a misleadingly low number for types REDACT was never built to detect; (2) PIIBench's constituent datasets are general free text, not log-shaped, so this is a cross-check of REDACT's detection *component* out-of-domain, not a validation of REDACT's log-*pipeline* claim -- must be reported as exactly that wherever cited. Also flagged: several PIIBench constituent datasets (wikiann, few-nerd, conll2003, multinerd) are built from real Wikipedia/Reuters text, not synthetic surrogates -- do not quote raw PIIBench examples in the book chapter, per this project's own no-real-data-in-publication rule; report only aggregate metrics, and use REDACT's own Faker-based corpus for any worked example.

**Three real bugs found getting the PIIBench pipeline running at all, none in REDACT's own code:**

1. `load_dataset('wikiann', 'en')` and `load_dataset('conll2003', ...)` in the PIIBench repo's own `src/download_datasets.py` both use bare, unnamespaced HuggingFace repo IDs -- `huggingface_hub` >= 1.16 (what `pip install` resolved) rejects these outright (`HfUriError: Repository id must be 'namespace/name'`), a real compatibility break between the upstream repo's pinned `datasets>=2.12.0` requirement (which resolves to whatever is current, here 5.0.1) and the API surface it was written against. Fixed by pointing both calls at their current namespaced locations, `unimelb-nlp/wikiann` and `eriktks/conll2003` -- confirmed via each dataset's own HuggingFace page that these are the exact same underlying data under its current canonical location, not a different mirror needing independent verification.
2. `OSError: Too many open files` downloading `Babelscape/multinerd` -- a macOS shell file-descriptor `ulimit`, not a code issue. Fixed with `ulimit -n 10240` before rerunning.
3. **The real methodological one, found via a live 200-record smoke test before committing to the full run:** the first version of `evaluate_piibench.py` ran REDACT against `" ".join(tokens)` (matching PIIBench's own documented reconstruction for scoring its published baselines) and measured **IP recall at exactly 0%** -- every one of 15 gold IP spans missed. Diagnosed directly (`validation/piibench/diagnose_ip_and_person_misses.py`) rather than assumed: every gold IP span showed literal internal spaces after reconstruction (a real IPv4 span rendered as `'140 . 115 . 236 . 150'`), because several sources are wordpiece-tokenized and whitespace-joining reinserts a space at every subword boundary -- no IPv4/IPv6 regex matches that, REDACT's included. Worse, the token stream also turned out lowercased and accent-stripped relative to the real source text (French `chere` for the real `chère`), corrupting the NER layer's input too. **Fixed** by rerunning PIIBench's own pipeline with its documented `--include-text` flag (preserves each record's real source string) and rewriting `evaluate_piibench.py` to scan that real text, re-anchoring each gold BIO span into it via a small per-token regex (`##`-continuation pieces glue with no gap, other token boundaries allow `\s*`, case-insensitive) rather than trusting the corrupted reconstruction's own offsets. Re-run, same 200 records: IP recall 0% -> 80% (12/15), confirming the fix and that neither of REDACT's own detectors was ever actually broken.

**A second, smaller diagnosed gap, found the same way:** PERSON precision was still poor after the reconstruction fix (24 TP / 161 FP on the 200-record smoke test). `validation/piibench/diagnose_sibling_labels.py` checked whether "false positives" were actually correct detections scored against an excluded sibling label -- confirmed: PIIBench's taxonomy splits "this text names a specific individual" across `PERSON`/`NAME`/`FIRST_NAME`/`LAST_NAME`, and "this is a payment card number" across `CREDIT_CARD`/`CREDIT_CARD_NUMBER`/`CREDIT_DEBIT_CARD`. 16 of 160 PERSON FPs and 2 of 6 CREDIT_CARD FPs were real, correct detections scored wrong purely because of this label split. `PIIBENCH_TO_REDACT` now maps all the siblings onto REDACT's single corresponding type -- a taxonomy-alignment fix, not scope creep, since it adds no PIIBench type REDACT wasn't already conceptually claiming. Disclosed, not fixed further: FIRST_NAME/LAST_NAME sometimes tag as two separate adjacent BIO spans rather than one merged PERSON span, so a single correct full-name detection can leave one sibling span uncredited as a false negative -- a minor recall under-credit, not an inflated precision, not fixed here (would need span-merging across adjacent same-type BIO entities, out of scope for what this evaluation is meant to be).

**Full 5,000-record result, confirmed live, 2026-09:**

```
overall: P=0.354 R=0.650 (TP=2328 FP=4253 FN=1255)
  CREDIT_CARD  P=0.342 R=0.166 (TP=27  FP=52  FN=136)
  EMAIL        P=0.858 R=0.904 (TP=593 FP=98  FN=63)  [2 gold excluded, unanchorable]
  IP           P=0.613 R=0.803 (TP=257 FP=162 FN=63)
  PERSON       P=0.264 R=0.589 (TP=1399 FP=3904 FN=976)
  SSN          P=0.584 R=0.754 (TP=52  FP=37  FN=17)
```

**CREDIT_CARD's low recall (0.166) was diagnosed, not left as a mystery** (`validation/piibench/diagnose_credit_card_recall.py`, run against all 136 missed spans): 95 (70%) fail REDACT's own Luhn-checksum filter (`_luhn_valid()`, "Engineering upgrade 15" above) -- PIIBench's synthetic generators apparently don't compute a real check digit the way Faker's `credit_card_number()` provider does, so this is this project's own already-disclosed precision/recall tradeoff paying its cost on genuinely different synthetic data, not a new bug. Of the remaining 41, most are card **brand name strings** (`'jcb'`, `'mastercard'`, `'diners_club'`) that PIIBench tags as `CREDIT_CARD` entities -- REDACT was never built to flag a brand name as PII (it isn't identifying information on its own), so these are a taxonomy scope mismatch in the benchmark, not a REDACT gap. **Zero misses were unexplained.**

**How to read the headline number, stated as plainly as this project states every other measurement caveat:** REDACT's scoped F1 here (~0.46, computed from the numbers above) looks far better than PIIBench's own published best baseline (Presidio, F1=0.1385) -- but this is NOT a valid head-to-head comparison and must never be reported as one. REDACT is scored only on the 5 entity types it actually claims; PIIBench's own baselines are scored across its full 82-type taxonomy, the large majority of which REDACT was never built to detect at all. The correct claim is: "REDACT's detection component, scoped to its own claimed entity types, evaluated out-of-domain against PIIBench, achieves P=0.354/R=0.650" -- a cross-check of the underlying regex/NER component on general text, not a validation of REDACT's log-pipeline claim and not a claim of beating Presidio on this benchmark.

Reproduction commands, for the record:

```
git clone https://github.com/pritesh-2711/pii-bench
cd pii-bench && pip install -r requirements.txt
ulimit -n 10240   # only if Babelscape/multinerd's download hits "Too many open files"
python run_data_pipeline.py --include-text && python create_evaluation_subset.py
cd /path/to/REDACT
python validation/piibench/evaluate_piibench.py --test-file /path/to/pii-bench/data/test_5k.jsonl --output validation/piibench/results.json
```

**Detection/policy decoupling.** Read Streamdal's and AnonShield's (arXiv:2606.15650) actual architectures directly rather than going by name association. Confirmed REDACT's own policy layer (`anonymize.py`'s `PSEUDONYMIZE_TYPES`/`TOKENIZE_TYPES`/`REDACT_TYPES`) is three hardcoded module-level Python sets today -- changing policy needs a code edit and redeploy, unlike AnonShield's `--entities-to-preserve`/`fields_to_exclude`/`anonymization_config` or Streamdal's hot-swappable rule console. Wrote `DETECTION_POLICY_DECOUPLING_SCOPING.md`: a 3-phase proposal (externalize policy to a config file → authenticated hot-reload → AnonShield-style skip-detection-entirely for schema-declared fields), with a devil's-advocate pass on each phase -- a fail-closed validator for Phase 1 so an incomplete policy file can't silently leave a type unprotected; an auth/audit-log requirement for Phase 2 so policy can't change live with no record of who/when; and, for Phase 3, disclosure that AnonShield's own false-negative data shows schema-trust already has failure modes even with full detection running, so a fully-skipped field needs `drift.py` extended to periodically re-check a sample of excluded fields, not left as a pure, unmonitored trust assumption. **Recommendation: build Phase 1 only for now** -- no new infrastructure needed, Phases 2/3 both carry real unresolved risk this pass declined to resolve by guessing. No implementation attempted, per this task's own scoping-only framing (matching the existing convention in `wasm/EDGE_COLLECTOR_INTEGRATION_SCOPING.md`/`monitoring/SOURCE_ATTRIBUTION_DESIGN.md`).

Both pieces added to `ROADMAP.md` (item 14) and `.gitignore` (PIIBench's per-run `results.json`, same "generated, not hand-authored" policy as every other results artifact in this project).

## Engineering upgrade 18: Phase 1 of detection/policy decoupling implemented (config/policy.json)

**Status:** Implemented, tested, confirmed live by the user (`pytest tests/test_policy_config.py -v`: 10/10 passed; full suite re-run after this change: 100 passed, 2 skipped, no regressions). Follows directly from Engineering upgrade 17's `DETECTION_POLICY_DECOUPLING_SCOPING.md`, which recommended building Phase 1 only, for the reasons stated there.

**What changed.** `src/anonymize.py`'s `PSEUDONYMIZE_TYPES`/`TOKENIZE_TYPES`/`REDACT_TYPES` were three hardcoded module-level Python sets — changing REDACT's policy meant editing this file and rebuilding/redeploying. Added `anonymize.load_policy(path=None)`, which optionally overrides these three sets from an external JSON file (`config/policy.json` by default, or `REDACT_POLICY_FILE`/an explicit `path` argument), and calls it at module-import time in both `src/service.py` and `src/pipeline.py` — same placement rule as `detect._get_analyzer()`'s existing warmup call (must run unconditionally at import, not inside `if __name__ == "__main__":`, since gunicorn imports `service.py` directly and never executes that block).

**The fail-closed requirement the scoping doc called out by name, actually built, not just proposed:** a canonical type (`PERSON`, `EMAIL`, `IP`, `SSN`, `CREDIT_CARD`, `MRN` — `anonymize.CANONICAL_TYPES`) left out of all three buckets, or assigned to more than one, or a file that's simply invalid JSON, raises `anonymize.PolicyConfigError` — which propagates up through `load_policy()`'s caller and fails the whole process's startup. An operator who breaks the policy file gets a service that refuses to start, not one that silently serves with an incomplete policy — the specific GDPR Article 32 audit gap the scoping doc's devil's-advocate section flagged. Deliberately handled differently: a policy file that simply doesn't exist at the resolved path fails OPEN to this module's built-in defaults (a warning printed to stderr, not an exception) — "nothing configured yet" is exactly today's pre-this-change behavior and isn't a security regression, since the built-in defaults already cover every canonical type.

**Verified with 10 new tests** (`tests/test_policy_config.py`): missing-file fallback, a valid custom override actually taking effect, the shipped `config/policy.json` itself validating cleanly, a missing-canonical-type failure, a type-in-two-buckets failure, malformed JSON, a non-list value, extra/ignored top-level keys (`_comment`/`_legal_note`), and two end-to-end tests that actually import `src/service.py` with `REDACT_POLICY_FILE` pointing at a bad file (confirms the real failure mode — the module fails to import — not just that the underlying function raises in isolation) and a good file (confirms the override actually reaches `anonymize.PSEUDONYMIZE_TYPES`/`TOKENIZE_TYPES` as read by every existing caller).

**Docker/Compose wiring, so this is operationally real and not just a code path that exists:** `Dockerfile` now `COPY`s `config/` into the image so a fresh container has a valid default without any operator action; `docker-compose.yml`'s `redact-service` bind-mounts the host's own `./config/policy.json` over that path read-only, plus a `REDACT_POLICY_FILE` env var default matching it — so changing policy in the reference deployment is genuinely "edit a host file, `docker compose restart redact-service`," no rebuild, matching what the scoping doc set out to achieve. `config/policy.json` itself carries an inline `_legal_note` field (ignored by the loader, read by a human) restating the pseudonymize-vs-tokenize-vs-redact GDPR distinction from README.md, directly on the file an operator would actually be editing — the scoping doc's cross-cutting recommendation ("a warning at edit time, not just prose in a README a different person may never open") acted on, not just noted.

**Deliberately NOT done, per the scoping doc's own recommendation:** no hot-reload (Phase 2 — needs its own auth layer and audit log, neither of which exists yet) and no schema-aware detection-skip (Phase 3 — needs `drift.py` extended to sample-check excluded fields first). Both remain scoped-only in `DETECTION_POLICY_DECOUPLING_SCOPING.md`.

`ROADMAP.md` item 14b updated from "SCOPED" to "DONE" for Phase 1 specifically (Phases 2/3 remain scoped, not implemented).

## Engineering upgrade 19: Phase 2 of detection/policy decoupling implemented (authenticated hot-reload)

**Status:** Implemented, tested, confirmed live by the user (`pytest tests/test_policy_hot_reload.py -v`: 14/14 passed; full suite re-run: 114 passed, 2 pre-existing skips, no regressions). Designed first per this task's own "design first, build after" instruction, then built -- unlike Phase 1's already-settled design, Phase 2 required real new decisions the scoping doc had deliberately left open (auth scheme, audit-log schema, and -- found only while designing, not anticipated in the scoping doc -- the multi-worker propagation problem below).

**The core design decision the scoping doc didn't resolve, worked through here:** gunicorn runs multiple worker processes, each an independent OS process with its own copy of `anonymize.py`'s module-level policy sets (same reason each worker warms its own spaCy model). A single admin API call can only update the ONE worker that happens to handle that request -- there is no shared memory between workers for one to push policy into its siblings. Resolved by making a background poller (`src/policy_watcher.py`'s `PolicyWatcher`, one instance per worker, started at module-import time) the PRIMARY propagation mechanism: every worker independently re-checks `config/policy.json`'s content hash on an interval (`REDACT_POLICY_POLL_INTERVAL_S`, default 10s) and converges on its own. A second reason polling (not inotify) was chosen over a filesystem watch, found while designing rather than assumed: Docker bind mounts (`docker-compose.yml`'s `./config/policy.json:/app/config/policy.json:ro`) don't reliably deliver host-side inotify events into the container across all storage-driver/host-OS combinations -- polling costs nothing to trust across that gap.

**Authentication, per the scoping doc's explicit Phase 2 requirement ("its own authentication, separate from anything else REDACT exposes today"):** a new `REDACT_POLICY_ADMIN_KEY`, checked via `hmac.compare_digest` against a new `X-Redact-Policy-Admin-Key` header on two new routes (`GET /admin/policy`, `POST /admin/policy/reload`), which are explicitly EXEMPTED from the existing `X-Redact-Api-Key` check rather than requiring both -- confirmed by test that holding only `SERVICE_API_KEY` doesn't grant admin access and, the reverse, that holding only `POLICY_ADMIN_KEY` doesn't grant `/anonymize` access (`tests/test_policy_hot_reload.py::test_admin_policy_key_does_not_grant_anonymize_access`). Two genuinely separate authorization domains, not one check with two valid keys.

**Fail-closed at runtime, distinct from Phase 1's fail-closed-at-startup:** `PolicyWatcher.check_now()` never raises `PolicyConfigError` -- an invalid edit rejects and logs, but a worker that's already serving live traffic must survive a bad edit rather than crash mid-service (crashing at STARTUP, Phase 1's behavior, is correct precisely because nothing is being served yet). On rejection, the watcher deliberately does NOT advance its last-known-good hash, so the same still-broken file keeps registering as "changed" and keeps retrying every poll rather than silently giving up on detecting a later fix -- confirmed directly (`test_check_now_retries_broken_file_until_fixed`).

**Audit trail:** `src/policy_audit.py` adds a second, parallel HMAC-signed event stream (`build_policy_audit_event()`/`verify_policy_audit_event()`, same authentication-tag pattern as `audit.py`'s existing per-redaction events, deliberately reusing `AUDIT_KEY` rather than minting a fourth key -- reasoned through explicitly in that module's docstring for why the narrow-audience concern that splits `FINGERPRINT_KEY` from `AUDIT_KEY` doesn't apply here) appended to `output/policy_audit_log.jsonl`, guarded by the same `fcntl.flock`-based cross-process lock pattern as `anonymize.py`'s `FileStorageProvider` -- needed here for a reason Phase 1 never had: multiple workers' independent pollers can genuinely append to this file concurrently, not just multiple threads in one process. Deliberately does NOT log "unchanged" checks (would grow without bound recording nothing informative); records only "reloaded" and "rejected" outcomes.

**A real race found and diagnosed live while testing, not shipped un-investigated:** the first test run showed two failures where a manually-triggered reload reported "unchanged" instead of "reloaded". Root-caused (not assumed) to a genuine, correct race: the background poller's one-time immediate check at `.start()` and the test's own manual trigger can both see the same file change, and whichever wins updates the shared state first -- the SECOND caller then correctly (if confusingly) reports "unchanged" since the policy had, in fact, already converged. This is not a bug in the reload logic (the policy always ends up correct either way) but a real property of "multiple independent checkers racing to notice the same change," which the tests initially didn't account for. Fixed on the test side, not by changing the implementation: assert durable end-state (the live policy content, and the append-only audit log's contents) rather than a single shared `last_outcome` field that a concurrent, equally-valid check can legitimately overwrite moments later.

**Observability:** three new Prometheus metrics (`redact_policy_last_reload_timestamp_seconds`, `redact_policy_last_check_timestamp_seconds`, `redact_policy_reload_total{outcome}`) exist specifically so a poller thread that silently died (guarded against inside `policy_watcher.py`'s own poll loop, but not provably impossible) would show up as a stalled `last_check_timestamp` from the outside, rather than only being detectable by noticing policy changes silently stopped taking effect.

**Docker/Compose wiring:** `REDACT_POLICY_ADMIN_KEY` added as a required (`:?`) secret alongside the existing keys; `REDACT_POLICY_POLL_INTERVAL_S` as an optional (`:-10`) tuning knob. `.gitignore` gained `output/policy_audit_log.jsonl` and its `.lock` file, added proactively rather than waiting for the same near-miss the CloudTrail/Kafka-corpus entries elsewhere in this file document.

**Deliberately NOT done:** `pipeline.py` (a one-shot batch script) has no poller wired in -- there's no long-running process for a background thread to live in, and no benefit to one. `ROADMAP.md` item 14b updated: Phase 2 now DONE; Phase 3 (schema-aware detection bypass) remains scoped only.

## Engineering upgrade 20: Phase 3 of detection/policy decoupling implemented (schema-trust detection bypass), scoped down from its original sketch by a real finding

**Status:** Implemented, tested, confirmed live by the user (`pytest tests/test_schema_trust.py -v`: 22/22 passed; full suite re-run: 136 passed, 2 pre-existing skips, no regressions). Designed first, and the design phase itself changed the scope significantly -- worth recording precisely, since what got built is honestly narrower than what `DETECTION_POLICY_DECOUPLING_SCOPING.md`'s original sketch described.

**The finding that changed the plan.** The scoping doc's Phase 3 sketch used CloudTrail's `sourceIPAddress` as its illustrative example. Tracing `fields.py`'s actual CloudTrail path before building against it (not assumed safe by analogy) found two real problems: (1) `extract_fields_cloudtrail()` is a fully generic recursive JSON flattener with no enumerated field list at all -- there was no existing "field recognition" to extend, this needed a genuinely new declaration surface; (2) excising a declared field's value out of raw JSON text and re-running `fields.py`'s own JSON parser on the remainder breaks `json.loads()` outright, silently disabling field-gating for every OTHER field on that line too -- a real regression, not hypothetical, that the excise-and-recurse mechanism (safe and already proven for KV-shaped `windows_event`/`syslog` text via `build_ner_candidate()`) cannot avoid for JSON. A second finding from the same trace: REDACT's existing field-gated NER already excludes regex-matched spans (which includes IP) from the NER call today, so an IP-typed field specifically gets little marginal benefit from Phase 3 regardless of format.

**Decision, put to the user rather than built past silently:** scope Phase 3 to `windows_event`/`syslog` only, explicitly excluding the very CloudTrail example the original doc used to illustrate it, and be upfront that this does NOT deliver AnonShield's own reported ~47x throughput number (which was measured on structured/JSON data specifically -- the one format this pass found isn't safely excisable with REDACT's current single-pass whole-line architecture). User chose to proceed with this narrower, honest version rather than skip Phase 3 entirely.

**What was built:**
- `config/schema_trust.json` -- `{log_type: {field_name: canonical_type}}`, ships EMPTY for both `windows_event` and `syslog` by default (unlike Phase 1's built-in defaults, there is no safe non-empty default here -- any declared field is detection this project's numbers were never measured against, so Phase 3 must be strictly opt-in).
- `src/schema_trust.py` -- `load_schema_trust()`/`SchemaTrustConfigError`, same fail-closed-on-invalid/fail-open-on-missing split as Phase 1's `anonymize.load_policy()`. Rejects (not silently ignores) a `cloudtrail` key at load time, and every declared type is validated against `anonymize.CANONICAL_TYPES`.
- `src/detect.py`'s new `detect_all_schema_aware()` -- for each declared field, emits a span of the declared type directly with NO regex/NER/entropy/flattened call against that field's value, using the same excise-and-remap machinery (`build_ner_candidate()`/`remap_hit()`'s underlying technique, reimplemented as a standalone `_splice_excising()` helper rather than reaching into that already-tested function) already proven safe for KV text. **A second, explicit `log_type not in SUPPORTED_LOG_TYPES` guard exists inside `detect_all_schema_aware()` itself, not just at the config-loading layer** -- found necessary by writing the "CloudTrail must always be a no-op" test FIRST and watching it fail against an earlier version that relied solely on the loader's own guarantee; genuine defense in depth, not assumed sufficient from one layer alone. **With an empty (default) config, this function's output is byte-identical to `detect_all_field_gated()`'s own** -- a real regression test (`test_detect_all_schema_aware_matches_field_gated_when_config_empty`), the actual proof this changes nothing until an operator opts in.
- **The mandatory mitigation, built as a hard requirement per the scoping doc's own devil's-advocate section, not skipped:** `src/schema_trust_sampler.py`'s `SchemaTrustSampler` -- a background daemon thread (same shape as Phase 2's `PolicyWatcher`) draining a bounded, backpressure-safe queue of sampled (declared_type, field_value) pairs (`REDACT_SCHEMA_TRUST_SAMPLE_RATE`, default 1%, enqueued via a non-blocking `maybe_sample()` call on the request path -- the actual detection re-check happens later, off the hot path, never synchronously, which would defeat the entire point of skipping detection). Three outcomes, deliberately NOT collapsed to a binary consistent/inconsistent: "consistent" (independent re-detection confirms the declared type), "no_signal" (found nothing at all -- NOT treated as drift, since an isolated field value without its line's context is a known harder case for NER, the same context-starvation limitation this project's own PIIBench work already measured), "drift_detected" (found a specific, different type -- the strong, actionable signal). `src/schema_trust_audit.py` records every sample outcome to a second signed audit stream (reusing `AUDIT_KEY`), deliberately WITHOUT the sampled value itself or even a fingerprint of it -- writing the value into a second log would defeat the point of sampling it in the first place.
- `service.py`: `schema_trust.load_schema_trust()` at import time (fail-closed), `SchemaTrustSampler` started at import time, `/anonymize` switched from `detect_all_field_gated()` to `detect_all_schema_aware()` (safe no-op with the shipped empty config, confirmed by test), a new read-only `GET /admin/schema_trust` route (reuses Phase 2's `POLICY_ADMIN_KEY` auth domain), three new Prometheus metrics (`redact_schema_trust_sample_total{outcome}`, `..._queue_depth`, `..._samples_dropped_total`).
- Docker/Compose: `config/schema_trust.json` shipped in the image and bind-mounted for host-editability, `REDACT_SCHEMA_TRUST_SAMPLE_RATE` configurable. **Explicitly, unlike `policy.json`, there is no hot-reload for this file** -- Phase 3's own devil's-advocate concern was the schema-trust bypass risk itself, not config hot-reload, so `load_schema_trust()` only ever runs once at startup; a schema-trust config change needs a container restart, not just a poll interval.
- **Deliberately NOT wired into `pipeline.py`**, consistent with Phase 2's own scope boundary there.

22 new tests (`tests/test_schema_trust.py`) across five tiers (config load/validate, signed audit events, `detect_all_schema_aware()` itself, the async sampler, and the new admin route). One test failure during development, diagnosed rather than worked around: `test_detect_all_schema_aware_syslog_field` initially used a wrong assumed field name ("rhost", then "src_ip") for the sshd auth-message extractor's source-IP field -- the real name, confirmed by reading `fields.py`'s `extract_fields_syslog()` directly, is tag-prefixed (`"sshd.src_ip"`, from that function's own `f"{tag}.{k}"` construction) -- fixed by reading the actual code instead of guessing a second time. See `DETECTION_POLICY_DECOUPLING_SCOPING.md`'s Phase 3 section for the full finding-by-finding writeup.

## Engineering upgrade 21: privacy-vs-investigative-utility experiment (manuscript revision support)

**Status:** Implemented and run in-sandbox (pure Python, no Docker/spaCy needed since this measures `anonymize.py`'s methods against ground truth, not detection). The accepted-with-minor-revisions manuscript needed measurable evidence, not argument alone, on how redaction/pseudonymization/tokenization each affect investigative utility.

**First step was checking feasibility against existing data before building anything.** The main 10,000-line corpus (`data/synthetic_logs.jsonl`) turned out structurally unable to answer this: `src/generate_logs.py`'s `make_slots()` mints every value fresh per record, so IP/SSN/CREDIT_CARD/MRN show essentially zero natural recurrence across the whole corpus (confirmed directly: 2,327 IP mentions, 2,327 distinct values, 0 repeats; identical story for the other three). Only PERSON has any (353/2,629 names repeat), and that's an incidental Faker-pool-size collision, not an intentional feature. An investigation is fundamentally a question about recurrence ("has this actor shown up before, and can I get back to the real value") — a corpus where four of five types never recur can't honestly support measuring that.

**Built instead of skipped:** `validation/investigative_utility/generate_scenario_corpus.py`, a small (400-record) supplementary corpus reusing `src/generate_logs.py`'s existing template/slot/render machinery directly (imported, not duplicated) but with a fixed pool of 3-8 "actor" values per type (IP/PERSON/EMAIL/SSN/CREDIT_CARD/MRN) deliberately drawn with 75% probability per PII slot, versus a guaranteed-unique noise value the other 25% — the inverse of the main corpus's ~0% recurrence, since this corpus's only job is giving the correlation metric something real to measure. Confirmed post-generation: every type now shows real recurrence (e.g. PERSON: 82 unique values, 23 recur, accounting for 285 of 344 mentions).

`validation/investigative_utility/measure_investigative_utility.py` then ran REDACT's real, unmodified `anonymize.redact()`/`pseudonymize()`/`tokenize()`+`TokenStore` against this corpus's ground-truth spans (deliberately not detector output — isolates what the anonymization methods themselves do to investigative utility from detection accuracy, a separate axis already measured via the PIIBench/Loghub work; stated explicitly in both the script's docstring and the directory README so it can't be mistaken for an end-to-end number) and measured two things per entity type:

1. **Correlation** — across different records, does equal real value produce equal output (`correlation_recall`, an investigator's ability to link two mentions of the same entity) and does unequal real value avoid producing equal output (`false_linkage_rate`, avoiding a wrong link between two different entities)? Enumerated as real cross-record mention pairs (deterministic stride-sampled if the pair count would be large, not random, so results are exactly reproducible), explicitly excluding same-record duplicate mentions (e.g. a username appearing twice in one `sudo` log line) from the pair set since that's not a cross-record correlation case.
2. **Reversibility** — redact: measured directly at 0.0 (confirmed every value maps to the same fixed placeholder, nothing of the original survives in any form). Pseudonymize: direct blind reversal stated (not "tested") at 0.0, since demonstrating a correctly-implemented HMAC-SHA256's non-invertibility isn't a meaningful computation to run — but a separate, more realistic sub-case IS tested directly: **candidate-list verification**, where an investigator already holds a short list of suspected values (e.g. "was it one of these five accounts") plus the pseudonymization key, and recomputes the same HMAC over each candidate to confirm/rule out. Tokenize: measured directly via the real `TokenStore.resolve()` path.

**Result, run live in-sandbox, consistent across all six entity types:** `correlation_recall = 1.0` for every method including redact — flagged explicitly in the README as *not* a point in redaction's favor, since it's an artifact of the metric (a constant placeholder trivially "links" everything). The number that actually separates the methods is `false_linkage_rate`: redaction measures **1.0** (any two different real entities of the same type become indistinguishable — an active, wrong signal to a downstream tool, not merely an uninformative one) while pseudonymize and tokenize both hold **0.0**, zero real hash/token collisions observed. Reversibility: redact 0.0; pseudonymize 0.0 direct / **1.0** candidate-list verification (30/30 trials, 5-candidate lists, given the key); tokenize **1.0** given store access.

**Honest limitations, stated in `validation/investigative_utility/README.md` rather than left implicit:** this is a structural demonstration on a small synthetic corpus (400 records, 3-8 recurring actors per type), not a production-scale collision study — HMAC-SHA256's collision resistance is an established cryptographic property this run doesn't meaningfully stress-test. The candidate-list-verification result demonstrates the mechanism works exactly as the cryptography predicts; it says nothing about how often a real investigation actually has an accurate short candidate list to test in the first place, which this experiment can't and doesn't answer. All three methods' "reversibility" numbers assume some privileged access already (TokenStore access, or the pseudonymization key) — consistent with, not weaker than, this project's existing audit-key/StorageProvider access-control assumptions elsewhere. Detection false negatives are explicitly out of scope (measured elsewhere) and must travel with any number quoted from this script.

Both scripts are deterministic (fixed random seeds) and reproduce `results.json` exactly on rerun against an unmodified `anonymize.py`. `ROADMAP.md` item 15 added with the full result.

## Engineering upgrade 22: real cloud object-storage log lake + real local SIEM ingestion (manuscript revision support, Ask 2)

**Status:** Both scripts written and syntax-checked; Python components round-trip-tested against fixture data in-sandbox (not against real Azure or a real Docker daemon -- neither exists in this sandbox). Genuinely unverified end-to-end until run live, same disclosure this project has applied to every new cloud/Docker script before its first real run (e.g. `run_azure_lb_test.sh`, `run_confluent_kafka_test.sh`).

**The real finding that shaped both halves of this work, caught before writing any upload/ingestion code:** `src/pipeline.py`'s `process_file()` writes each record as `{"log_type", "original", "anonymized", "detector_span_count"}`. `"original"` is the raw, pre-anonymization log text -- present deliberately, for local evaluation (comparing detector output against real input), but it means `output/anonymized.jsonl`, exactly as `pipeline.py` writes it today, has real SSNs/credit-card numbers/names sitting right next to the anonymized version. Uploading or ingesting that file unmodified into any external system -- cloud storage, a SIEM, anything outside REDACT's own local filesystem -- would be a real, live compliance incident, not a hypothetical one. Found by reading `process_file()`'s actual output schema before writing a single line of upload code, not discovered after a real upload.

**Fixed once, reused twice:** `validation/cloud_loglake/prepare_loglake_upload.py`'s `partition()` builds every uploaded/ingested record via an explicit three-key allow-list (`log_type`, `anonymized`, `detector_span_count`) rather than `del rec["original"]` on a copy -- an allow-list can't silently reopen this if `pipeline.py`'s schema later renames the sensitive field instead of removing it; a delete-based approach could. `validation/siem_splunk/ingest_to_splunk.py` imports this same `partition()` function directly rather than re-deriving the fix a second time for the SIEM path.

**Real cloud object-storage log lake (`run_azure_blob_loglake_test.sh`):** provisions a real Azure Storage Account (StorageV2, LRS, Hot tier) against the same Azure for Students subscription already validated for Task #52's Load Balancer test, uploads the PII-safe output partitioned Hive-style (`log_type=<type>/data.jsonl`, the standard layout convention for Blob-backed lakes and most SIEM bulk-ingestion paths), downloads it back into a separate directory, then runs `validation/cloud_loglake/verify_no_pii_in_blob.py`: a real DLP-style scan that collects every ground-truth PII value from the exact number of input records `pipeline.py` actually processed (respecting `--limit`, which truncates from the start of the input file -- confirmed by reading `process_file()`'s loop directly) and checks the downloaded content for a verbatim substring match against any of them. A hit here is deliberately specified as a real finding to report (either a genuine detector false negative, or an upload-step regression), not something to explain away -- confirmed working correctly against a fixture: a deliberately-injected simulated false negative ("John Smith" left unredacted in a fake `anonymized` field) was correctly caught and reported by name.

**Real local SIEM ingestion (`run_splunk_siem_test.sh`):** runs Splunk's official `splunk/splunk` Docker image as a standalone single container. Chosen over Microsoft Sentinel specifically because Sentinel carries real per-GB ingestion cost once its Log Analytics workspace has paid ingestion enabled -- a real budget risk this manuscript-support work was explicitly told not to spend without asking; Splunk's official image instead runs as a time-limited Enterprise trial that reverts automatically to the permanently-free Splunk Free tier (500MB/day), $0, no signup, well within this test's scale.

**A second real gotcha, caught the same way as the first -- by checking upstream documentation before building around an assumption:** the official image's `SPLUNK_HEC_TOKEN` environment variable looked like the obvious way to provision an HEC (HTTP Event Collector) token at container startup. A live web check against the actual `splunk/docker-splunk` project found a real, long-standing open issue (`splunk/docker-splunk#40`, filed 2018, title "SPLUNK_HEC_TOKEN is not supported") stating that `splunk-ansible`'s HEC-provisioning role (`set_as_hec_receiver.yml`) is only wired up for forwarder/indexer roles, not the standalone role this single-container test uses -- meaning the env var this script almost depended on may silently do nothing for a standalone instance. `run_splunk_siem_test.sh` avoids the risk entirely: after confirming the container's management REST API (`:8089`) is reachable, it enables HEC (`POST .../data/inputs/http/http`) and creates a token (`POST .../data/inputs/http`) itself via that REST API, which is documented to work regardless of role.

`validation/siem_splunk/ingest_to_splunk.py` sends one real HEC event per log record (tagged `sourcetype="redact:anonymized"`, with `log_type` as an indexed HEC field). `validation/siem_splunk/verify_splunk_ingestion.py` verifies against the LIVE running instance's own search REST API (`services/search/jobs/export`, not local files) two things: an ingested-event count check, and the same DLP-style ground-truth leak check reused directly from `cloud_loglake/verify_no_pii_in_blob.py` (`load_ground_truth_values()`/`count_lines()` imported, not copied) applied to every indexed event's real `_raw` text pulled back from Splunk's own search.

**What this does and doesn't show, stated plainly:** both scripts test a real round trip (upload/ingest, live storage/indexing, download/search-back) at a scale of a few hundred KB -- neither tests production-scale volume, and neither has actually been run against real Azure or a real Docker daemon yet, since this sandbox has neither. That live run, and the real pass/fail result it produces, is the next step, not claimed here. Full methodology and scope in `validation/cloud_loglake/README.md` and `validation/siem_splunk/README.md`. `ROADMAP.md` item 16 added with the full writeup.

## Engineering upgrade 22 addendum: real Azure run, a real Azure CLI gotcha, and a corrected finding

**Real Azure CLI gotcha, found and fixed live:** `az storage account create` failed with `(SubscriptionNotFound) Subscription ... was not found` immediately after `az group create` succeeded against the exact same subscription -- ruling out an actual auth/subscription problem (`az account show` confirmed the correct subscription was active). Checked against Azure CLI's own GitHub issue tracker (`Azure/azure-cli#30550`, `#29267`, and related issues) before guessing further: this is a known, actively misleading error message -- the real cause is the `Microsoft.Storage` resource provider not yet being registered on the subscription, which several storage data-plane commands report as `SubscriptionNotFound` instead of the correct `MissingSubscriptionRegistration`. Fixed live: `az provider register --namespace Microsoft.Storage`, polled via `az provider show --namespace Microsoft.Storage --query registrationState -o tsv` until `Registered` (a few minutes), then the script re-ran and completed successfully end to end -- real Storage Account created, real upload, real download, real verification query.

**The verification step's real result, and a real self-correction on how it was first read:** the DLP check reported 959 of 6,537 ground-truth PERSON values present in the downloaded lake content. First-pass framing treated this as a new ~26-point recall regression by comparing this run's PERSON-only recall against `README.md`'s published "0.854" ensemble figure -- **an apples-to-oranges error, caught before it went further**: 0.854 is the MICRO-AVERAGE recall across all 6 entity types (IP/SSN/CREDIT_CARD/EMAIL/PERSON/MRN) for the naive-NER-plus-flattened-layer ensemble, not a PERSON-specific number. The correct comparison is against `README.md`'s own already-published PERSON-only recall for that ensemble (0.681, 2038/2993 TP) -- and the live run's PERSON recall (2034/2993 = 0.6797) lands within 4 records of it, not a meaningful gap by any measure.

Broken down by name format, the live run independently reproduces two more already-documented numbers, not new ones: flattened-username-style PERSON recall measured 52.8% live (947/2006 missed) against the documented 50.3% for the flattened-name layer alone (`flattened_names.py`'s own docstring); full "First Last"-style recall measured 98.8% live (12/987 missed), matching the documented near-total NER recall on space-separated names. This is two independently-built measurement methods -- `evaluate.py`'s span-matching harness (built months earlier, purely for accuracy measurement) and this task's whole-corpus DLP substring scan (built for a completely different purpose, checking what left the perimeter) -- landing on the same numbers for the same known limitation. That's corroborating evidence for something already disclosed as this project's "single most important measured finding" (`flattened_names.py`'s own words), not a new defect requiring a pre-deadline fix.

**Lesson recorded plainly, since it's a real mistake, not just a fixed one:** before framing any live-run number as a regression or new finding, check it against the ALREADY-PUBLISHED number using the same metric definition (per-type recall vs. micro-average recall are not interchangeable), not just a plausible-sounding headline figure remembered from earlier in the same document. `ROADMAP.md` item 16 updated with the corrected finding and the live Azure result.

## Engineering upgrade 22, closing note: Splunk SIEM half confirmed live, three real fixes along the way

**Status: DONE.** `run_splunk_siem_test.sh` now runs clean end to end: real HEC ingestion (10,000/10,000 events accepted, 0 rejected), a count check against the live instance's own search API (10,000/10,000 indexed and searchable, PASS), and the same DLP-style leak check reused from the object-storage half.

Three real, live-only problems, each found from an actual error and fixed from what it said, not guessed in advance:

1. **Port 8000 conflict.** The first live run failed immediately: `docker` reported port 8000 already allocated. Cause not investigated further at the time since the fix (remap to 8001) was low-risk and Splunk Web isn't used by the script itself.
2. **Port 8089 conflict, investigated properly this time.** The retry failed the same way on 8089. `lsof -i :8000 -i :8088 -i :8089` plus `docker ps -a` (both run live) found the real cause: an entirely unrelated, already-running Splunk container from a different project on the same machine (`opencti8431lab-splunk-1`, up 17 hours, bound to exactly 8000 and 8089 -- Splunk's own two most common default ports). Not a REDACT bug at all, just two independent Splunk instances wanting the same well-known ports. Remapped the management API to 8091 rather than asking an unrelated project's container to be stopped; confirmed via the same tool that 8088 (HEC) was genuinely free.
3. **Missing `SPLUNK_GENERAL_TERMS`.** With both ports clear, the container itself refused to start, logging its own explicit instruction: `splunk/splunk:latest`'s currently-published build requires `SPLUNK_GENERAL_TERMS=--accept-sgt-current-at-splunk-com` in addition to `SPLUNK_START_ARGS=--accept-license` -- checked against `splunk.github.io/docker-splunk`'s own current documentation page first (`ADVANCED.md`), which still only shows the single `--accept-license` flag in its examples; the second flag is a licensing-gate addition in a newer image build than that doc page currently reflects. Added directly from the container's own error text once found, not from the docs (which don't yet mention it).

**Final result, both halves of Ask 2 now independently confirmed:** the Splunk run's DLP check found the exact same 959 of 6,537 PERSON values present in indexed, searchable content as the Azure Blob run found in downloaded content -- expected, since both ingest the identical `output/anonymized.jsonl` and the underlying cause (the already-documented flattened-username detection gap, see the addendum above) is a property of the anonymization output itself, not of either destination. Two independently real systems -- a cloud object store and a locally-run SIEM -- landing on the identical leak count is a second, stronger confirmation of the same already-disclosed finding, not a new one. `ROADMAP.md` item 16 marked DONE with both final results.

## Engineering upgrade 23: adversarial evasion-testing experiment, confirmed live

**Status: DONE, CONFIRMED LIVE, 2026-09-11.** Manuscript revision follow-up, third and final item of the "all three, in that order" plan (detection tradeoffs synthesis -> taxonomy reference -> this). `validation/adversarial_evasion/` built 13 evasion techniques across all 6 canonical PII types, each targeting one specific, cited mechanism in `src/detect.py`, substituted into real ground-truth spans from `data/synthetic_logs.jsonl` in their real log-line context (18,322 variant records from 19,310 spans). Run against the real production `detect_all_field_gated()` by the user locally (this sandbox has no spaCy/Presidio model, standing limitation).

**Result: 3 of 6 types evade completely (100%) via ordinary, non-adversarial formats** — EMAIL (`[at]`/`[dot]` obfuscation and a zero-width-space split), IP (defanging, a real SOC/threat-intel writing convention), MRN (lowercase/no-dash). **SSN partially evades**: no-dashes 100%, but spaced/dotted 0% — most likely explained by Presidio's own separate `US_SSN` recognizer independently tolerating those formats (REDACT's layered design catching what one layer misses; not independently confirmed against Presidio's source). **PERSON, already the weakest baseline (31.9% miss on un-evaded names), degrades further under every technique except capitalization**: `all_caps` +1.9pp only, `unicode_homoglyph` to 68.4%, `leetspeak` to 93.6%, `flattened_digit_insertion` (targeting Layer 4's dictionary segmentation specifically) to 99.2% — the single most effective technique measured.

**CREDIT_CARD group-splitting's blended 50.2% was investigated further, not left as an unexplained number.** A dedicated diagnostic (`diagnose_credit_card_partial_evasion.py`) broke it down by original card digit-length, run live by the user: 16-digit cards (dominant Visa/Mastercard shape, 111/237 of the corpus's values) evaded only 10.8%, every other length (12/13/14/19 digits) evaded 100%, 15-digit (Amex-shaped) landed at 45.7%. Confirms Presidio's own CREDIT_CARD recognizer has real format-grouping tolerance specifically for the standard 4x4-digit shape and little to none elsewhere — REDACT's actual resistance to this technique is concentrated in one card format, not evenly spread across every length it claims to detect.

**Devil's-advocate framing recorded explicitly in the README, per this project's standing discipline:** three of six types evading completely via formats seen in ordinary, non-adversarial security writing (a SOC analyst defanging an IOC, a human obfuscating an email against spam harvesters) is a GDPR Article 32 concern distinct from an occasional edge-case miss — systematic, reproducible, and requiring no sophistication to trigger. No mitigation proposed or built (out of this session's explicit scope: "don't build speculative features unrelated to the asks"). Full method, per-technique rationale (each cites the exact `src/detect.py` mechanism it targets), results tables, and "what this does and doesn't show" caveats in `validation/adversarial_evasion/README.md`.
