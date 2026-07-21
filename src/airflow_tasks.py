"""
Task logic for the weekly Airflow DAG (Section 7 / Section 3.3 of the
chapter). Written as plain functions, independently testable without an
Airflow runtime, for the same reason src/service.py wraps the detection
logic instead of duplicating it: the DAG file should orchestrate this
logic, not reimplement it, and the logic should be testable on its own.
"""
import sys
import os
import json
import glob

sys.path.insert(0, os.path.dirname(__file__))
import drift  # noqa: E402


def check_taxonomy_drift(baseline_path: str, current_path: str,
                          threshold: float = 0.05) -> dict:
    """Runs the drift check from drift.py and returns a structured result
    an Airflow task can log, alert on, or fail on. Does not raise on
    finding drift -- flagging is a review signal, not necessarily a hard
    pipeline failure, matching the chapter's framing of this as something
    that routes to manual review."""
    with open(baseline_path) as f:
        baseline_entries = [json.loads(l) for l in f]
    with open(current_path) as f:
        current_entries = [json.loads(l) for l in f]

    baseline_stats = drift.field_stats(baseline_entries)
    current_stats = drift.field_stats(current_entries)
    flagged, insufficient = drift.compare(baseline_stats, current_stats, threshold)

    return {
        "baseline_entries": len(baseline_entries),
        "current_entries": len(current_entries),
        "fields_flagged": len(flagged),
        "flagged_detail": flagged,
        "fields_insufficient_data": len(insufficient),
    }


def sample_medium_confidence_hits(anonymized_log_path: str, sample_rate: float = 0.05,
                                   seed: int = 0) -> dict:
    """Pulls a random sample of processed entries for human review. This is
    the concrete implementation behind the audit-sampling claim in Section
    4's discussion of Medium-confidence detections: a mandatory, sized
    sample rather than an optional spot-check. Sampling here is over
    already-processed pipeline output (src/pipeline.py's anonymized.jsonl),
    since that is what a human reviewer actually needs to look at."""
    import random
    rng = random.Random(seed)

    if not os.path.exists(anonymized_log_path):
        return {"error": f"no processed log found at {anonymized_log_path}",
                "sampled": 0, "total": 0}

    with open(anonymized_log_path) as f:
        entries = [json.loads(l) for l in f]

    n_with_findings = [e for e in entries if e.get("detector_span_count", 0) > 0]
    sample_size = max(1, int(len(n_with_findings) * sample_rate)) if n_with_findings else 0
    sample = rng.sample(n_with_findings, min(sample_size, len(n_with_findings)))

    return {
        "total": len(entries),
        "total_with_findings": len(n_with_findings),
        "sample_size": len(sample),
        "sample": sample,
    }


def rotate_pseudonymization_key(current_key_path: str, retired_keys_dir: str) -> dict:
    """Retires the current pseudonymization key to a separately-tracked
    location and generates a new one. This does NOT attempt to re-encode
    already-written pseudonyms under the new key -- consistent with the
    chapter's dual-key-epoch discussion (Section 7), old pseudonyms remain
    valid only under the retired key, which is why it is preserved rather
    than discarded."""
    import secrets
    from datetime import datetime, timezone

    os.makedirs(retired_keys_dir, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    if os.path.exists(current_key_path):
        with open(current_key_path) as f:
            old_key = f.read().strip()
        retired_path = os.path.join(retired_keys_dir, f"key_retired_{timestamp}.txt")
        with open(retired_path, "w") as f:
            f.write(old_key)
    else:
        retired_path = None

    new_key = secrets.token_hex(32)
    with open(current_key_path, "w") as f:
        f.write(new_key)

    return {
        "rotated_at": timestamp,
        "retired_key_path": retired_path,
        "new_key_length_bytes": len(new_key) // 2,
    }
