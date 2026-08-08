"""
Shared reconciliation helper for the load test (run_load_test.sh) and for
manual checks. Same methodology used throughout BUGS_AND_FIXES.md (Bug 6,
Bug 8): query with _search?size=0 against the live searcher, not
_cat/indices, which can lag behind the actual committed state under write
pressure (see Bug 8 -- a whole entry exists in BUGS_AND_FIXES.md about that
exact false alarm).

Usage: python3 reconcile.py [opensearch_host]
Prints anonymized/quarantine/audit counts and the reconciliation check.
Exit code 0 if anonymized + quarantine == expected total (read from
data/raw/*.log line counts), 1 otherwise.
"""
import sys
import json
import os
import urllib.request


def count(host: str, index_pattern: str) -> int:
    url = f"{host}/{index_pattern}/_search?size=0"
    with urllib.request.urlopen(url, timeout=10) as resp:
        data = json.loads(resp.read())
    if "hits" not in data:
        raise RuntimeError(f"unexpected response from {url}: {data}")
    return data["hits"]["total"]["value"]


def expected_total(raw_dir: str = "data/raw") -> int:
    total = 0
    for fname in ("windows_events.log", "syslog", "cloudtrail.json"):
        path = os.path.join(raw_dir, fname)
        if not os.path.exists(path):
            continue
        with open(path) as f:
            total += sum(1 for _ in f)
    return total


def main():
    host = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:9200"

    anonymized = count(host, "security-logs-anonymized-*")
    quarantine = count(host, "security-logs-quarantine-*")
    try:
        audit = count(host, "redact-audit-trail-*")
    except Exception as e:  # noqa: BLE001
        audit = None
        print(f"(audit-trail count unavailable: {e})")

    expected = expected_total()

    print(f"Expected total (raw exported lines): {expected}")
    print(f"security-logs-anonymized-*:          {anonymized}")
    print(f"security-logs-quarantine-*:          {quarantine}")
    if audit is not None:
        print(f"redact-audit-trail-*:                {audit}")
    print(f"anonymized + quarantine:             {anonymized + quarantine}")

    ok = (anonymized + quarantine) == expected and expected > 0
    print("RECONCILIATION: " + ("PASS" if ok else "FAIL"))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
