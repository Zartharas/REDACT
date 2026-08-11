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
