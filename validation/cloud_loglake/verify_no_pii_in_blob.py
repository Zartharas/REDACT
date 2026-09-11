"""
Real DLP-style check on what actually reached (and was downloaded back
from) cloud object storage: does ANY raw, real PII value from the source
corpus appear verbatim anywhere in the uploaded/downloaded lake files?

This is deliberately corpus-wide, not per-line: it doesn't try to line up
which output record came from which input record (pipeline.py doesn't
carry an index, and partitioning by log_type would lose input order
anyway). Instead it collects every ground-truth PII value from the exact
number of input records src/pipeline.py actually processed (respecting
--limit, which truncates from the start of the input file -- confirmed by
reading process_file()'s loop directly) and scans every downloaded lake
file for a verbatim substring match against any of them.

This check serves two purposes at once, both real: (1) confirms the cloud
round-trip (upload, storage, download) didn't somehow reintroduce or
corrupt anything, and (2) is a genuine end-to-end leak check on the
detector-driven anonymization itself -- if REDACT's detector missed a real
entity (a false negative), that raw value would legitimately still be
sitting in the "anonymized" field pipeline.py wrote, and this script will
correctly flag it as a leak. That is not a bug in this script if it
happens; it is this script doing its job. Any hit here must be reported
as a real finding, not treated as a false alarm to explain away.
"""
import argparse
import json
import os
import sys


def load_ground_truth_values(corpus_path: str, n_records: int) -> list[tuple[str, str]]:
    """Returns [(pii_type, value), ...] for the first n_records lines of
    corpus_path -- matches src/pipeline.py's own top-to-bottom, --limit-
    truncates-from-the-start processing order exactly."""
    values = []
    with open(corpus_path) as f:
        for i, line in enumerate(f):
            if i >= n_records:
                break
            rec = json.loads(line)
            log = rec["log"]
            for span in rec.get("pii", []):
                values.append((span["type"], log[span["start"]:span["end"]]))
    return values


def count_lines(path: str) -> int:
    with open(path) as f:
        return sum(1 for _ in f)


def scan_directory(root_dir: str) -> str:
    """Concatenates every file under root_dir into one string for a simple,
    exhaustive substring scan. Fine at this corpus's scale (a few hundred
    KB); would need a streaming approach for a real multi-GB lake, which
    this experiment explicitly is not."""
    chunks = []
    for dirpath, _, filenames in os.walk(root_dir):
        for name in filenames:
            with open(os.path.join(dirpath, name), errors="replace") as f:
                chunks.append(f.read())
    return "\n".join(chunks)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--corpus", default="data/synthetic_logs.jsonl")
    parser.add_argument("--anonymized", default="output/anonymized.jsonl",
                         help="used only to count how many input records pipeline.py actually processed")
    parser.add_argument("--downloaded-dir", required=True,
                         help="local directory the cloud blobs were downloaded back into")
    args = parser.parse_args()

    if not os.path.exists(args.anonymized):
        sys.exit(f"{args.anonymized} not found -- run src/pipeline.py first.")
    if not os.path.isdir(args.downloaded_dir):
        sys.exit(f"{args.downloaded_dir} is not a directory -- download the blobs there first.")

    n_records = count_lines(args.anonymized)
    ground_truth = load_ground_truth_values(args.corpus, n_records)
    print(f"Checking {len(ground_truth)} ground-truth PII values from the first "
          f"{n_records} records of {args.corpus} against {args.downloaded_dir} ...")

    haystack = scan_directory(args.downloaded_dir)
    leaks = [(t, v) for (t, v) in ground_truth if v in haystack]

    print(f"\nDownloaded lake content: {len(haystack)} bytes across "
          f"{sum(len(files) for _, _, files in os.walk(args.downloaded_dir))} files.")

    if leaks:
        print(f"\nFAIL: {len(leaks)} of {len(ground_truth)} ground-truth PII values found "
              f"verbatim in the downloaded lake content:")
        by_type = {}
        for t, v in leaks:
            by_type.setdefault(t, []).append(v)
        for t, vals in sorted(by_type.items()):
            print(f"  {t}: {len(vals)} leaked (example: {vals[0]!r})")
        print("\nThis means real PII reached cloud storage -- either the detector missed "
              "these spans (a real false negative, report as such) or the upload step "
              "included a field it shouldn't have (check prepare_loglake_upload.py's output "
              "directly before assuming it's a detection gap).")
        sys.exit(1)
    else:
        print(f"\nPASS: 0 of {len(ground_truth)} ground-truth PII values found in the "
              f"downloaded lake content. Nothing from the source corpus's known-sensitive "
              f"values survived the trip through cloud storage.")


if __name__ == "__main__":
    main()
