"""
End-to-end pipeline: detect PII in a log line, anonymize each finding per the
policy in anonymize.py, and emit a signed audit event per action taken.

Usage:
    python src/pipeline.py --in data/synthetic_logs.jsonl --out output/anonymized.jsonl \
        --audit-out output/audit_log.jsonl --limit 500

Runs on the detector's actual output, not on ground truth, so this reflects
what a reader running the tool against their own (unlabeled) logs would get.
"""
import sys
import json
import argparse
import os

sys.path.insert(0, os.path.dirname(__file__))
import detect      # noqa: E402
import anonymize   # noqa: E402
import audit        # noqa: E402

POLICY_VERSION = "redact-v0.1"
PSEUDO_KEY = "demo-pseudonymization-key-do-not-use-in-prod"
AUDIT_KEY = "demo-audit-signing-key-do-not-use-in-prod"
FINGERPRINT_KEY = "demo-fingerprint-key-do-not-use-in-prod"
TOKEN_KEY = "demo-token-key-do-not-use-in-prod"


def process_file(in_path: str, out_path: str, audit_out_path: str,
                  token_store_path: str, limit: int | None = None):
    store = anonymize.TokenStore(token_store_path, token_key=TOKEN_KEY)
    n_processed = 0
    n_findings = 0

    with open(in_path) as f_in, \
         open(out_path, "w") as f_out, \
         open(audit_out_path, "w") as f_audit:

        for line in f_in:
            if limit and n_processed >= limit:
                break
            entry = json.loads(line)
            text = entry["log"]
            log_type = entry.get("log_type")

            # Engineering upgrade, 2026-08-09: switched from
            # detect.detect_all() (naive) to detect.detect_all_field_gated(),
            # same change and same reasoning as src/service.py's
            # /anonymize endpoint -- see detect.build_ner_candidate's
            # docstring and README.md's comparison table for the full
            # three-iteration measurement history behind this default.
            # log_type was already being read from each entry (used below
            # for the output record) but never passed into detection --
            # that's the one line this upgrade actually changes.
            spans = detect.detect_all_field_gated(text, log_type=log_type)
            # entropy hits carry type HIGH_ENTROPY, which anonymize_by_policy
            # does not route anywhere (falls through untouched) -- entropy is
            # a review signal in this pipeline, not an auto-anonymization
            # trigger, matching the chapter's discussion of its false-alarm
            # rate on structured fields.
            typed_spans = [s for s in spans if s["type"] != "HIGH_ENTROPY"]
            # regex and NER frequently agree on the same span; dedup once,
            # here, so both the anonymization pass and the audit trail below
            # see one finding per actual PII instance, not one per detector
            # that happened to notice it.
            typed_spans = anonymize.dedup_spans(typed_spans)

            anonymized_text = anonymize.anonymize_by_policy(
                text, typed_spans, key=PSEUDO_KEY, store=store
            )

            for span in typed_spans:
                original_value = text[span["start"]:span["end"]]
                if span["type"] in anonymize.PSEUDONYMIZE_TYPES:
                    method = "pseudonymize"
                elif span["type"] in anonymize.TOKENIZE_TYPES:
                    method = "tokenize"
                else:
                    method = "redact"
                event = audit.build_audit_event(
                    field_type=span["type"], method=method,
                    policy_version=POLICY_VERSION,
                    original_value=original_value, audit_key=AUDIT_KEY,
                    fingerprint_key=FINGERPRINT_KEY,
                )
                f_audit.write(json.dumps(event) + "\n")
                n_findings += 1

            f_out.write(json.dumps({
                "log_type": entry.get("log_type"),
                "original": text,
                "anonymized": anonymized_text,
                "detector_span_count": len(typed_spans),
            }) + "\n")
            n_processed += 1

    store.save()
    return n_processed, n_findings


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--in", dest="in_path", default="data/synthetic_logs.jsonl")
    parser.add_argument("--out", dest="out_path", default="output/anonymized.jsonl")
    parser.add_argument("--audit-out", dest="audit_out_path", default="output/audit_log.jsonl")
    parser.add_argument("--token-store", dest="token_store_path", default="output/token_store.json")
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()

    os.makedirs(os.path.dirname(args.out_path), exist_ok=True)
    n_processed, n_findings = process_file(
        args.in_path, args.out_path, args.audit_out_path,
        args.token_store_path, args.limit,
    )
    print(f"Processed {n_processed} log entries, {n_findings} anonymization actions taken.")
    print(f"Anonymized log: {args.out_path}")
    print(f"Audit trail:    {args.audit_out_path}")
    print(f"Token store:    {args.token_store_path}")
