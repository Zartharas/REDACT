"""
Tests drift.compare_all() (src/drift.py) and the Prometheus Pushgateway
export path (src/airflow_tasks.py:push_drift_metrics_to_prometheus,
push_drift_metrics_task) added 2026-08-11 to "weaponize" the drift
detector -- see BUGS_AND_FIXES.md's "Engineering upgrade" entry for the
full context.

No live Pushgateway in this environment (no Docker daemon here), so the
push path is tested by monkeypatching prometheus_client.push_to_gateway
and asserting it was called with the right registry contents/job/gateway
-- the same "verify the actual mechanism, not just that no exception was
raised" discipline test_field_level_gate.py already uses for detect.py.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import drift  # noqa: E402
import airflow_tasks  # noqa: E402


def test_compare_all_includes_non_flagged_fields():
    """compare() only returns fields that crossed the threshold; compare_all()
    must return every sufficiently-sampled field, flagged or not, since a
    continuous exporter needs the current rate for fields that haven't
    drifted yet too (so a dashboard shows trend before a field flags)."""
    baseline = {
        ("syslog", "user"): {"total": 100, "critical_hits": 5},   # 5%
        ("syslog", "status"): {"total": 100, "critical_hits": 0},  # 0%
    }
    current = {
        ("syslog", "user"): {"total": 100, "critical_hits": 6},   # 6%, delta 1% -- below default 5% threshold
        ("syslog", "status"): {"total": 100, "critical_hits": 20},  # 20%, delta 20% -- flagged
    }

    flagged, _ = drift.compare(baseline, current, threshold=0.05)
    all_results, _ = drift.compare_all(baseline, current, threshold=0.05)

    # compare() only ever returns the one field that actually crossed 5%.
    assert len(flagged) == 1
    assert flagged[0]["field"] == "status"

    # compare_all() returns BOTH fields, each carrying its own flagged bool.
    assert len(all_results) == 2
    by_field = {r["field"]: r for r in all_results}
    assert by_field["status"]["flagged"] is True
    assert by_field["user"]["flagged"] is False
    # Same underlying arithmetic as compare() -- not a second, differently
    # computed number for the field that IS flagged.
    assert by_field["status"]["current_rate"] == flagged[0]["current_rate"]
    assert by_field["status"]["delta"] == flagged[0]["delta"]


def test_compare_all_excludes_insufficient_sample_fields():
    """Same MIN_SAMPLE_SIZE gate as compare() -- a field with too few
    observations in either window should not get a rate pushed to
    Prometheus at all (a noisy small-sample rate on a dashboard is worse
    than no data point)."""
    baseline = {("syslog", "rare_field"): {"total": 3, "critical_hits": 1}}
    current = {("syslog", "rare_field"): {"total": 3, "critical_hits": 2}}

    all_results, insufficient = drift.compare_all(baseline, current, threshold=0.05)
    assert all_results == []
    assert len(insufficient) == 1
    assert insufficient[0]["field"] == "rare_field"


def test_push_drift_metrics_noop_without_pushgateway_url(monkeypatch):
    """Must not attempt a push (or raise) when no Pushgateway is
    configured -- the default in every environment this project has
    actually run in so far."""
    called = {"push": False}

    def _fake_push_to_gateway(*args, **kwargs):
        called["push"] = True

    monkeypatch.setattr("prometheus_client.push_to_gateway", _fake_push_to_gateway)

    result = airflow_tasks.push_drift_metrics_to_prometheus(
        {"all_field_detail": [{"log_type": "syslog", "field": "user",
                                "current_rate": 0.2, "flagged": True}]},
        pushgateway_url=None,
    )

    assert result == {"pushed": False, "reason": "no pushgateway_url configured"}
    assert called["push"] is False


def test_push_drift_metrics_pushes_expected_gauges(monkeypatch):
    """With a pushgateway_url configured, confirms the actual gauge values
    and labels pushed match check_taxonomy_drift's own field detail --
    not just that push_to_gateway was called with *something*."""
    captured = {}

    def _fake_push_to_gateway(gateway, job, registry):
        captured["gateway"] = gateway
        captured["job"] = job
        # Pull the metric samples back out of the registry to verify
        # what was actually about to be pushed.
        samples = {}
        for metric in registry.collect():
            for sample in metric.samples:
                samples[(sample.name, sample.labels.get("log_type"), sample.labels.get("field"))] = sample.value
        captured["samples"] = samples

    monkeypatch.setattr("prometheus_client.push_to_gateway", _fake_push_to_gateway)

    result = airflow_tasks.push_drift_metrics_to_prometheus(
        {
            "all_field_detail": [
                {"log_type": "syslog", "field": "user", "current_rate": 0.2, "flagged": True},
                {"log_type": "syslog", "field": "status", "current_rate": 0.0, "flagged": False},
            ]
        },
        pushgateway_url="pushgateway:9091",
        job="test_job",
    )

    assert result == {"pushed": True, "fields_pushed": 2}
    assert captured["gateway"] == "pushgateway:9091"
    assert captured["job"] == "test_job"

    samples = captured["samples"]
    assert samples[("redact_drift_field_critical_hit_rate", "syslog", "user")] == 0.2
    assert samples[("redact_drift_field_flagged", "syslog", "user")] == 1.0
    assert samples[("redact_drift_field_critical_hit_rate", "syslog", "status")] == 0.0
    assert samples[("redact_drift_field_flagged", "syslog", "status")] == 0.0


def test_push_drift_metrics_task_pulls_xcom_explicitly():
    """The Airflow-facing wrapper must pull check_taxonomy_drift's result
    via explicit context access (ti.xcom_pull), not rely on Jinja
    auto-templating an op_kwargs string into a dict -- see
    push_drift_metrics_task's own docstring for why the latter would
    silently break (str(dict) instead of the dict itself) without this
    DAG setting render_template_as_native_obj=True, which it does not."""
    pushed_with = {}

    class _FakeTI:
        def xcom_pull(self, task_ids):
            assert task_ids == "check_taxonomy_drift"
            return {"all_field_detail": []}

    def _fake_push(result, pushgateway_url, job="redact_drift_check"):
        pushed_with["result"] = result
        pushed_with["pushgateway_url"] = pushgateway_url
        return {"pushed": False, "reason": "no pushgateway_url configured"}

    orig = airflow_tasks.push_drift_metrics_to_prometheus
    airflow_tasks.push_drift_metrics_to_prometheus = _fake_push
    try:
        airflow_tasks.push_drift_metrics_task(pushgateway_url=None, ti=_FakeTI())
    finally:
        airflow_tasks.push_drift_metrics_to_prometheus = orig

    assert pushed_with["result"] == {"all_field_detail": []}
    assert pushed_with["pushgateway_url"] is None
