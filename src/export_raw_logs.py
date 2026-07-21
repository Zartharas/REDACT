"""
Splits the synthetic_logs.jsonl corpus (one JSON record per line, with a
log_type field) into three separate raw-text files, one per source, matching
what logstash/redact-pipeline.conf's file input actually expects to read.
This is what a real deployment would look like too -- Logstash tails raw
log files per source, not a JSONL corpus with embedded ground truth -- so
this script also strips the "pii" ground-truth field, which exists for
evaluation purposes (see evaluate.py) and would not be present in real logs.
"""
import json
import argparse
import os

FILE_NAMES = {
    "windows_event": "windows_events.log",
    "syslog": "syslog",
    "cloudtrail": "cloudtrail.json",
}


def main(input_path: str, output_dir: str):
    os.makedirs(output_dir, exist_ok=True)
    handles = {
        log_type: open(os.path.join(output_dir, filename), "w")
        for log_type, filename in FILE_NAMES.items()
    }
    counts = {log_type: 0 for log_type in FILE_NAMES}

    try:
        with open(input_path) as f:
            for line in f:
                entry = json.loads(line)
                log_type = entry.get("log_type")
                if log_type not in handles:
                    continue
                handles[log_type].write(entry["log"] + "\n")
                counts[log_type] += 1
    finally:
        for h in handles.values():
            h.close()

    for log_type, filename in FILE_NAMES.items():
        print(f"{filename}: {counts[log_type]} lines")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default="data/synthetic_logs.jsonl")
    parser.add_argument("--output-dir", default="data/raw")
    args = parser.parse_args()
    main(args.input, args.output_dir)
