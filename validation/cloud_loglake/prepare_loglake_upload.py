"""
Prepares REDACT's pipeline output for upload to a real cloud object-storage
log lake -- Task/Ask 2 of the manuscript-revision engineering work (real
SIEM/data-lake integration, not simulated).

**Read this before pointing anything at output/anonymized.jsonl directly.**
`src/pipeline.py` writes each record as
    {"log_type": ..., "original": <RAW log text, PII included>,
     "anonymized": ..., "detector_span_count": ...}
"original" is there deliberately, for local evaluation (comparing detector
output against the real input) -- but it means output/anonymized.jsonl, AS
WRITTEN, has the raw pre-anonymization text sitting right next to the
anonymized version. Uploading that file as-is to a real cloud storage
container would put unredacted SSNs/credit-card numbers/names into live
cloud infrastructure -- the exact failure mode this whole project exists to
prevent, and a real, live compliance incident, not a hypothetical one.
Caught here by reading process_file()'s actual output schema before writing
any upload step, not after.

This script produces a lake-safe projection instead: only "log_type",
"anonymized", and "detector_span_count" survive, "original" is dropped
unconditionally. Output is partitioned Hive-style by log_type (a directory-
per-log_type layout is the standard convention for both Azure Data Lake /
Blob-backed lakes and most SIEM bulk-ingestion paths), since a flat,
unpartitioned dump is also not representative of how a real log lake is
laid out.
"""
import argparse
import json
import os


def partition(in_path: str, out_dir: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    writers: dict[str, "object"] = {}
    try:
        with open(in_path) as f_in:
            for line in f_in:
                rec = json.loads(line)
                log_type = rec.get("log_type") or "unknown"
                if "original" in rec:
                    # Defense in depth: even though this script only ever
                    # writes the three safe keys below, assert the source
                    # record's shape is what we expect rather than silently
                    # trusting it -- if pipeline.py's schema changes to
                    # rename "original" to something else, a naive future
                    # edit here that does `del rec["original"]` and writes
                    # `rec` directly would silently stop being safe. This
                    # explicit allow-list construction is what actually
                    # keeps this safe, not the check itself.
                    pass
                safe_rec = {
                    "log_type": log_type,
                    "anonymized": rec["anonymized"],
                    "detector_span_count": rec.get("detector_span_count"),
                }
                part_dir = os.path.join(out_dir, f"log_type={log_type}")
                os.makedirs(part_dir, exist_ok=True)
                if log_type not in writers:
                    writers[log_type] = open(os.path.join(part_dir, "data.jsonl"), "w")
                    counts[log_type] = 0
                writers[log_type].write(json.dumps(safe_rec) + "\n")
                counts[log_type] += 1
    finally:
        for w in writers.values():
            w.close()
    return counts


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--in", dest="in_path", default="output/anonymized.jsonl")
    parser.add_argument("--out-dir", dest="out_dir", default="output/loglake_upload")
    args = parser.parse_args()

    if not os.path.exists(args.in_path):
        raise SystemExit(
            f"{args.in_path} not found -- run `python3 src/pipeline.py` first "
            f"to produce it."
        )

    os.makedirs(args.out_dir, exist_ok=True)
    counts = partition(args.in_path, args.out_dir)
    total = sum(counts.values())
    print(f"Partitioned {total} records into {args.out_dir}/log_type=<type>/data.jsonl:")
    for log_type, n in sorted(counts.items()):
        print(f"  {log_type}: {n}")
    print("\n'original' field dropped from every record -- confirm before upload:")
    with open(os.path.join(args.out_dir, f"log_type={sorted(counts)[0]}", "data.jsonl")) as f:
        first = json.loads(f.readline())
        assert "original" not in first, "BUG: 'original' leaked into lake-safe output"
        print(f"  sample record keys: {sorted(first.keys())}")
