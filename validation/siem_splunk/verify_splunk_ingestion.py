"""
Verifies a real Splunk ingestion, two ways, both against the live running
instance (not against local files) -- this is the point of testing a real
SIEM connector rather than just trusting that the HEC POSTs returned 200:

1. Count check: does the number of indexed events for sourcetype
   "redact:anonymized" match the number of records actually sent?
2. DLP-style leak check: pulling back every indexed event's raw text via
   Splunk's own search REST API, does any ground-truth PII value from the
   source corpus appear in what actually got indexed and is now
   searchable? Reuses cloud_loglake's verify_no_pii_in_blob.py ground-truth
   loader directly rather than a second copy of the same logic.

Uses Splunk's one-shot search export endpoint
(services/search/jobs/export, output_mode=json) rather than the
job-then-poll-then-results flow, since a small result set doesn't need
the async job lifecycle -- the export endpoint is Splunk's own documented
mechanism for exactly this case.
"""
import argparse
import json
import os
import sys

import requests
import urllib3

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "cloud_loglake"))
from verify_no_pii_in_blob import load_ground_truth_values, count_lines  # noqa: E402

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


def search_export(base_url: str, auth: tuple, search: str) -> list[dict]:
    resp = requests.post(
        f"{base_url}/services/search/jobs/export",
        auth=auth, verify=False, timeout=60,
        data={"search": search, "output_mode": "json", "earliest_time": "-1h", "count": "0"},
    )
    resp.raise_for_status()
    results = []
    for line in resp.text.splitlines():
        if not line.strip():
            continue
        obj = json.loads(line)
        if "result" in obj:
            results.append(obj["result"])
    return results


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="https://localhost:8089")
    parser.add_argument("--admin-user", default="admin")
    parser.add_argument("--admin-password", required=True)
    parser.add_argument("--index", default="main")
    parser.add_argument("--corpus", default="data/synthetic_logs.jsonl")
    parser.add_argument("--anonymized", default="output/anonymized.jsonl",
                         help="used only to count how many input records were actually sent")
    args = parser.parse_args()

    auth = (args.admin_user, args.admin_password)
    search = f'search index={args.index} sourcetype="redact:anonymized"'
    results = search_export(args.base_url, auth, search)

    n_expected = count_lines(args.anonymized)
    print(f"Splunk search '{search}' returned {len(results)} events "
          f"(expected {n_expected}, from {args.anonymized}).")
    count_ok = len(results) == n_expected
    print("Count check: " + ("PASS" if count_ok else "FAIL -- see note below"))
    if not count_ok:
        print("  A mismatch here can be real indexing lag (HEC-ingested events aren't "
              "always immediately searchable) rather than lost events -- rerun this "
              "script after a short wait before treating it as a real loss.")

    ground_truth = load_ground_truth_values(args.corpus, n_expected)
    haystack = "\n".join(r.get("_raw", "") for r in results)
    leaks = [(t, v) for (t, v) in ground_truth if v in haystack]

    print(f"\nDLP-style check: {len(ground_truth)} ground-truth PII values against "
          f"{len(results)} indexed events' raw text.")
    if leaks:
        print(f"FAIL: {len(leaks)} ground-truth PII values found verbatim in indexed, "
              f"searchable Splunk events:")
        by_type = {}
        for t, v in leaks:
            by_type.setdefault(t, []).append(v)
        for t, vals in sorted(by_type.items()):
            print(f"  {t}: {len(vals)} leaked (example: {vals[0]!r})")
        sys.exit(1)
    else:
        print(f"PASS: 0 of {len(ground_truth)} ground-truth PII values found in indexed "
              f"Splunk events.")


if __name__ == "__main__":
    main()
