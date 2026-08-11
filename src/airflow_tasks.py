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
    all_compared, _ = drift.compare_all(baseline_stats, current_stats, threshold)

    return {
        "baseline_entries": len(baseline_entries),
        "current_entries": len(current_entries),
        "fields_flagged": len(flagged),
        "flagged_detail": flagged,
        "fields_insufficient_data": len(insufficient),
        # Full per-field detail (not just flagged fields), for
        # push_drift_metrics_to_prometheus below -- a dashboard/alert rule
        # needs the current rate for every monitored field, not only the
        # ones that already crossed the threshold. See drift.compare_all's
        # own docstring for why this is a second function, not a change
        # to drift.compare()'s return shape.
        "all_field_detail": all_compared,
    }


def push_drift_metrics_to_prometheus(result: dict, pushgateway_url: str | None,
                                      job: str = "redact_drift_check") -> dict:
    """Pushes check_taxonomy_drift's per-field results to a Prometheus
    Pushgateway, so Alertmanager can fire on a field's critical-hit-rate
    the same way it already fires on redact-service's live request
    metrics (see src/service.py) -- this is what turns drift.py from a
    script someone has to remember to read into a signal the existing
    Prometheus/Alertmanager stack can act on directly.

    Two gauges per (log_type, field):
      redact_drift_field_critical_hit_rate -- the field's current-window
        critical-tier hit rate, always exported (not only when flagged),
        so a dashboard shows the trend before a field ever crosses the
        alert threshold.
      redact_drift_field_flagged -- 1 if drift.compare()'s own threshold
        logic flagged this field this run, else 0. The Alertmanager rule
        fires directly on this value rather than reimplementing the
        threshold comparison in PromQL, so there is exactly one place
        (drift.compare/compare_all) that decides what counts as drift --
        the same "one implementation, not two" principle src/service.py's
        own docstring states for the HMAC/token-store logic.

    A batch Airflow task, not a long-running process, so this uses
    Prometheus's Pushgateway pattern (push once per run) rather than
    /metrics scraping, which only works for something Prometheus can poll
    continuously.

    Deliberately a no-op, not an error, when pushgateway_url is falsy --
    this task must not break check_taxonomy_drift's existing behavior (or
    the DAG, or this project's tests) in any environment that doesn't have
    a Pushgateway configured, which is every environment this project has
    actually been able to run in so far (see BUGS_AND_FIXES.md's standing
    disclosure pattern for anything requiring live infrastructure this
    sandbox doesn't have)."""
    if not pushgateway_url:
        return {"pushed": False, "reason": "no pushgateway_url configured"}

    from prometheus_client import CollectorRegistry, Gauge, push_to_gateway

    registry = CollectorRegistry()
    rate_gauge = Gauge(
        "redact_drift_field_critical_hit_rate",
        "Current-window critical-tier PII hit rate for this (log_type, field), "
        "from the same computation drift.py's CLI report uses.",
        ["log_type", "field"], registry=registry,
    )
    flagged_gauge = Gauge(
        "redact_drift_field_flagged",
        "1 if this field's rate moved past the drift threshold this run, else 0. "
        "Matches drift.compare()'s own flagging logic exactly -- Alertmanager "
        "should fire on this value directly rather than re-deriving a threshold.",
        ["log_type", "field"], registry=registry,
    )

    for entry in result.get("all_field_detail", []):
        labels = {"log_type": entry["log_type"], "field": entry["field"]}
        rate_gauge.labels(**labels).set(entry["current_rate"])
        flagged_gauge.labels(**labels).set(1 if entry["flagged"] else 0)

    push_to_gateway(pushgateway_url, job=job, registry=registry)
    return {"pushed": True, "fields_pushed": len(result.get("all_field_detail", []))}


def push_drift_metrics_task(pushgateway_url: str | None, **context) -> dict:
    """Airflow-facing wrapper around push_drift_metrics_to_prometheus.

    Pulls check_taxonomy_drift's return value from XCom explicitly via
    the task-instance context, rather than relying on Jinja
    auto-templating a "{{ ti.xcom_pull(...) }}" string in the DAG's
    op_kwargs -- that trick only preserves the dict's actual type if the
    DAG sets render_template_as_native_obj=True, which this DAG does not,
    so a templated string there would silently become str(the_dict)
    instead of the dict itself, and result.get(...) inside
    push_drift_metrics_to_prometheus would break. Explicit context access
    has no such footgun. Kept as a thin wrapper (not merged into
    push_drift_metrics_to_prometheus itself) so that function stays a
    plain, directly testable function with no Airflow context dependency
    at all."""
    ti = context["ti"]
    result = ti.xcom_pull(task_ids="check_taxonomy_drift")
    return push_drift_metrics_to_prometheus(result or {}, pushgateway_url)


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


def rotate_token_key(current_key_path: str, retired_keys_dir: str) -> dict:
    """Retires the current TOKEN_KEY (anonymize.py's TokenStore, used by
    tokenize()/get_or_create_token()) and generates a new one, mirroring
    rotate_pseudonymization_key's file-based approach above.

    Added as an engineering upgrade: TOKEN_KEY previously had no rotation
    mechanism at all, unlike PSEUDO_KEY -- a real gap, since a compromised
    static key that's never rotated exposes every value ever tokenized
    under it, indefinitely.

    IMPORTANT semantic difference from rotate_pseudonymization_key, stated
    explicitly so this isn't assumed to work the same way: pseudonymize()
    is a pure one-way HMAC with no lookup table, so an old pseudonym is
    ONLY ever valid under the key that produced it -- rotating the key
    genuinely invalidates old pseudonyms' verifiability. tokenize(), by
    contrast, is reversible via TokenStore's own forward/reverse dict
    lookup (see TokenStore.resolve() in anonymize.py), NOT by recomputing
    the HMAC from the key -- resolve()/detokenize() work identically
    before and after this rotation, for every token ever minted, because
    they were never key-dependent to begin with. What TOKEN_KEY actually
    controls is the guessability resistance of a *newly minted* token for
    someone who sees only the anonymized output without TokenStore access
    (see get_or_create_token's own docstring in anonymize.py). Rotating it
    here means: future first-time-seen values get tokenized under a fresh,
    unguessable key; values already tokenized keep their existing token
    (get_or_create_token's forward-map cache returns the same token for a
    repeated original value regardless of which key is currently active,
    preserving cross-event correlation for that value) and remain fully
    resolvable either way. This is a real security improvement (limits how
    long a single compromised key's guessability weakness applies to
    NEW values) but it is not, and does not need to be, a
    re-tokenization of existing data the way a pseudonymization-key
    rotation implies for existing pseudonyms."""
    return rotate_pseudonymization_key(current_key_path, retired_keys_dir)
