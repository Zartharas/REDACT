# Real cloud object-storage log lake (Azure Blob Storage)

Manuscript-revision ask: everything validated so far (Azure Standard Load
Balancer, Confluent Cloud Kafka) is pipeline/infrastructure-level. This is
a real (not simulated) integration touching an actual security-data-lake
pattern: REDACT's real anonymized output, uploaded to and downloaded back
from a real Azure Blob Storage container, using the same Azure for
Students subscription already validated for `run_azure_lb_test.sh`.

## A real finding, caught before running anything, not after

`src/pipeline.py` writes each processed record as
`{"log_type", "original", "anonymized", "detector_span_count"}` --
`"original"` is the raw pre-anonymization log text, kept there
deliberately for local evaluation. Uploading `output/anonymized.jsonl` to
cloud storage as-is would put real SSNs/credit-card numbers/names into
live cloud infrastructure -- exactly the failure this project exists to
prevent, not a hypothetical edge case. `prepare_loglake_upload.py` exists
specifically to close this: it builds the uploaded record via an explicit
three-key allow-list (`log_type`, `anonymized`, `detector_span_count`),
not a `del rec["original"]` on a copy, so a future schema change to
`pipeline.py` can't silently reopen this by renaming the sensitive field
instead of removing it.

## What this tests, and what it doesn't

Tests, for real: (1) a Hive-style partitioned upload (`log_type=<type>/`
directories), the standard layout convention for both Blob-backed lakes
and most SIEM bulk-ingestion paths; (2) a real round trip through Azure
Blob Storage (upload, live storage, download); (3) a DLP-style scan of
what actually came back, checking every ground-truth PII value from the
source corpus against the downloaded content verbatim.

Does not test: production-scale volume (this is a few hundred KB, not
terabytes), lifecycle/tiering policies, or a real analytics engine
(Synapse/Databricks) querying the lake -- none of that is claimed.

## The verification step is a real leak check, not a formality

`verify_no_pii_in_blob.py` checks the downloaded content against every
ground-truth PII value from the exact number of input records
`pipeline.py` actually processed. This catches two different things at
once, both real: a corrupted round trip through cloud storage, AND any
actual detector false negative that left real PII sitting in the
"anonymized" field before it was ever uploaded. A failure here is a real
finding to report -- about detection accuracy or about the upload step,
depending which -- not noise to explain away.

## Running it

```bash
# 1. Produce real anonymized output (needs spaCy/Presidio locally):
python3 src/pipeline.py --in data/synthetic_logs.jsonl \
    --out output/anonymized.jsonl --audit-out output/audit_log.jsonl

# 2. Everything else -- strip original, create real Storage Account,
#    upload, download, verify:
./run_azure_blob_loglake_test.sh
```

Pass a region as the first argument if `northcentralus` isn't allowed for
your subscription (see `run_azure_lb_test.sh`'s header for how to find
your subscription's allowed regions).

## Real result (run 2026-09-11, against a real Azure for Students subscription)

Ran end to end after fixing one real Azure CLI gotcha: `az storage account create` failed with a misleading `SubscriptionNotFound` -- the real cause (confirmed against Azure CLI's own issue tracker) was `Microsoft.Storage` not yet being registered on the subscription. Fixed with `az provider register --namespace Microsoft.Storage`, then the script completed successfully.

The verification step reported 959 of 6,537 ground-truth PERSON values present in the downloaded content. **This is not a new detection regression** -- checked carefully before concluding otherwise: it's a live, independent reproduction of this project's own already-documented flattened-username detection gap (see `flattened_names.py`'s docstring and `README.md`'s "Known Limitations" section), which this project has disclosed for months as its single most important measured finding. Split by format: flattened-style names ("wdavis") measured 52.8% recall live against a documented 50.3% for that layer alone; full "First Last"-style names measured 98.8% live, matching prior measurements. Two independently-built measurement methods -- the project's own `evaluate.py` span-matching harness, and this test's whole-corpus DLP scan -- landing on the same numbers is corroborating evidence, not a new finding. Full writeup in `BUGS_AND_FIXES.md`'s "Engineering upgrade 22 addendum".

The upload/DLP-check MECHANISM is confirmed working correctly either way: it correctly surfaced exactly the leak REDACT's own documentation says to expect, at the exact rate documented, on real data, through a real cloud round trip.

## Cost and cleanup

One Standard general-purpose v2 Storage Account (LRS, Hot tier) holding a
few hundred KB for the test's duration -- fractions of a cent, trivial
against Azure for Students' $100 credit. Nothing is deleted
automatically; the script prints the cleanup command
(`az group delete --resource-group redact-loglake-test --yes --no-wait`)
at the end.
