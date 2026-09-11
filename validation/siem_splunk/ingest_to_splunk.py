"""
Real HEC (HTTP Event Collector) ingestion of REDACT's real anonymized
output into a real, locally-running Splunk instance -- Ask 2's SIEM half
(the object-storage half is validation/cloud_loglake/).

Reuses validation/cloud_loglake/prepare_loglake_upload.py's PII-safe
projection directly (same import, not a second copy) rather than
re-deriving the "never upload/ingest the 'original' field" rule a second
time -- one real finding, one fix, reused everywhere it applies.

Sends one HEC event per log record, tagging each with sourcetype
"redact:anonymized" and an indexed "log_type" field (HEC's own
`fields` mechanism) so the verification step and any real Splunk search
can filter by it.
"""
import argparse
import json
import os
import sys
import urllib3

import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "cloud_loglake"))
from prepare_loglake_upload import partition  # noqa: E402

# The Splunk container uses a self-signed cert on 8088/8089 by default --
# expected and fine for a local, throwaway test instance; suppressing the
# resulting urllib3 warning here is a deliberate, narrow choice for this
# script only, not a blanket recommendation.
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


def ingest(records_dir: str, hec_url: str, hec_token: str, index: str) -> tuple[int, int]:
    sent, failed = 0, 0
    headers = {"Authorization": f"Splunk {hec_token}"}
    for dirpath, _, filenames in os.walk(records_dir):
        for name in filenames:
            with open(os.path.join(dirpath, name)) as f:
                for line in f:
                    rec = json.loads(line)
                    payload = {
                        "event": rec["anonymized"],
                        "sourcetype": "redact:anonymized",
                        "index": index,
                        "fields": {"log_type": rec.get("log_type", "unknown"),
                                   "detector_span_count": rec.get("detector_span_count")},
                    }
                    resp = requests.post(hec_url, headers=headers, json=payload,
                                          verify=False, timeout=10)
                    if resp.status_code == 200:
                        sent += 1
                    else:
                        failed += 1
                        print(f"  HEC rejected event: {resp.status_code} {resp.text[:200]}",
                              file=sys.stderr)
    return sent, failed


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--in", dest="in_path", default="output/anonymized.jsonl",
                         help="pipeline.py's real output; re-partitioned here via "
                              "prepare_loglake_upload.py before ingestion")
    parser.add_argument("--work-dir", default="output/splunk_upload")
    parser.add_argument("--hec-url", default="https://localhost:8088/services/collector")
    parser.add_argument("--hec-token", required=True)
    parser.add_argument("--index", default="main")
    args = parser.parse_args()

    if not os.path.exists(args.in_path):
        sys.exit(f"{args.in_path} not found -- run src/pipeline.py first.")

    os.makedirs(args.work_dir, exist_ok=True)
    counts = partition(args.in_path, args.work_dir)
    print(f"Prepared {sum(counts.values())} PII-safe records for ingestion "
          f"({args.work_dir}); 'original' field dropped (see prepare_loglake_upload.py).")

    sent, failed = ingest(args.work_dir, args.hec_url, args.hec_token, args.index)
    print(f"HEC ingestion: {sent} accepted, {failed} rejected.")
    if failed:
        sys.exit(1)
