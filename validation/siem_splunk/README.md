# Real SIEM ingestion path (Splunk, local, free tier)

Manuscript-revision ask: a real (not simulated) SIEM connector test, as an
alternative to a cloud SIEM. Microsoft Sentinel was considered and set
aside deliberately: it carries real per-GB ingestion cost once enabled,
which the manuscript-support budget explicitly flagged as something not
to spend without asking. Splunk, run locally via its official Docker
image, is genuinely real (real HEC ingestion, real indexing, real search
API) at zero cost: the image runs as a time-limited Enterprise trial that
reverts automatically to the permanently-free Splunk Free tier
(500MB/day), no signup, no card.

## A real gotcha caught before building around it

The official image's `SPLUNK_HEC_TOKEN` environment variable looked like
the obvious way to provision an HEC token at container startup. Checked
against the actual upstream project before relying on it: a real,
long-standing issue (`splunk/docker-splunk#40`) states that
`splunk-ansible`'s HEC-provisioning role only applies to forwarder/indexer
roles, not the standalone role this single-container test uses -- meaning
the env var may silently do nothing here. `run_splunk_siem_test.sh` avoids
depending on it at all: it enables HEC and creates the token itself via
Splunk's management REST API (`:8089`) after the container is confirmed
up, which is documented to work for any role.

## Reuses Ask 2a's real finding, doesn't repeat it

`src/pipeline.py`'s output carries an `"original"` (raw, pre-anonymization)
field next to `"anonymized"` -- see `validation/cloud_loglake/README.md`
for the full writeup. `ingest_to_splunk.py` imports
`prepare_loglake_upload.partition()` directly rather than re-deriving the
same "strip original via an explicit allow-list" fix a second time. The
same real risk (raw PII reaching an external system) applies identically
whether that system is object storage or a SIEM's ingestion endpoint.

## What's actually verified, and how

Two checks, both run against the **live running Splunk instance**, not
against local files -- the point of testing a real SIEM connector rather
than trusting that HEC's POST responses returned 200:

1. **Count check**: does the number of events Splunk's search API reports
   for `sourcetype="redact:anonymized"` match the number of records sent?
2. **DLP-style leak check**: pulling every indexed event's raw text back
   via Splunk's own `services/search/jobs/export` REST endpoint, does any
   ground-truth PII value from the source corpus appear in what actually
   got indexed and is now searchable? Reuses
   `cloud_loglake/verify_no_pii_in_blob.py`'s ground-truth loader directly.

## What this does and doesn't show

Tests real HEC ingestion, real indexing, and a real post-indexing search
query against the live instance. Does not test Splunk's own detection
rules/dashboards/alerting, or any volume beyond a few hundred KB -- none
of that is claimed.

## Running it

```bash
# 1. Produce real anonymized output (needs spaCy/Presidio locally):
python3 src/pipeline.py --in data/synthetic_logs.jsonl \
    --out output/anonymized.jsonl --audit-out output/audit_log.jsonl

# 2. Start Splunk, provision HEC, ingest, verify:
./run_splunk_siem_test.sh
```

Splunk Web is reachable at `http://localhost:8001` (`admin` /
`Redact-Test-Pw1`, matching `docker-compose-splunk.yml` -- host port 8001
maps to the container's own 8000, remapped after a live run found 8000
already bound to something else on the test machine) while the container
is running, if you want to look at the indexed events directly.

## Real result (run 2026-09-11, against a real local Splunk instance)

Ran end to end after three real, live-only fixes: two port conflicts (host 8000 and 8089 were both already bound -- 8089 turned out to belong to an entirely unrelated, already-running Splunk container from a different project on the same machine, found via `lsof`/`docker ps`; remapped to 8001/8091 rather than touching that unrelated container) and a missing `SPLUNK_GENERAL_TERMS` environment variable the currently-published `splunk/splunk:latest` image requires beyond what its own current docs show, taken directly from the container's own error text. See `docker-compose-splunk.yml`'s comments and `BUGS_AND_FIXES.md`'s closing note on "Engineering upgrade 22" for the full detail on each.

Real HEC ingestion: 10,000/10,000 events accepted, 0 rejected. Count check against the live instance's own search API: 10,000/10,000 indexed and searchable, PASS. DLP check: **959 of 6,537 PERSON values found in indexed content** -- the exact same count `validation/cloud_loglake/`'s Azure run found. That's expected, not a new problem: both ingest the identical `output/anonymized.jsonl`, and the cause is REDACT's own already-documented flattened-username detection gap (see that directory's README for the full recall breakdown), a property of the anonymization output itself, not of either destination. Two independently real systems landing on the identical leak count is a second, stronger confirmation of the same already-disclosed finding.

## Cleanup

```bash
docker compose -f docker-compose-splunk.yml down -v
```

No ongoing cost if left running (unlike the Azure Blob Storage test), but
it does hold local disk/memory until stopped.
