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
what the condition is. `bash -n` syntax-checked; not yet re-run against a
live Docker stack (that verification needs the user's machine -- see
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

**Still pending, disclosed plainly:** the per-replica distribution numbers themselves (does `redact-lb`'s nginx proxy actually spread the 20,000 requests roughly evenly across all 3 replicas, versus everything landing on one working replica while the other two sit idle -- reconciliation alone can't distinguish these, which is exactly why this check exists as a separate signal) and Part B (the queue-decoupled path, `logstash-queued` -> Redis list -> 3x `queue-consumer` -> `redact-lb` -> OpenSearch, never reached in either live run so far) both still need one more `./run_replica_and_queue_test.sh` pass with this second fix in place to move from "fixed, unit-checked" to "confirmed working" -- the same bar `redact-pipeline.conf` itself was held to before Bug 16's own first live run found a separate, real problem.

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

**Not yet re-confirmed live** -- Part B needs one more
`./run_replica_and_queue_test.sh` run with this fix in place to move
from "root-caused and fixed" to "confirmed working," the same bar every
other Docker-dependent fix in this document is held to. Part A's own
results from this same run (OOM fix confirmed, distribution confirmed
even) stand on their own and don't need re-running.

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
