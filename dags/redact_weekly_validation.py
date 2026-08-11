"""
Weekly re-validation DAG (Section 7 of the chapter). Orchestrates the three
task functions in airflow_tasks.py, which are independently tested (see
README) -- this file wires them together and schedules them, it does not
reimplement their logic.

Written against Airflow 3.3.0's current API (airflow.sdk.DAG,
airflow.providers.standard.operators.python.PythonOperator). An earlier
draft of this file used Airflow 2.x-style imports (airflow.DAG,
airflow.operators.python.PythonOperator); those still work in 3.3.0 via a
backward-compatibility shim but emit a DeprecatedImportWarning on every
import, which is not something a chapter meant to reflect current practice
should ship. Confirmed this by actually importing both forms and observing
the warning -- see README for the exact commands run.
"""
import sys
import os
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

from airflow.sdk import DAG
from airflow.providers.standard.operators.python import PythonOperator

import airflow_tasks  # noqa: E402

DATA_DIR = os.environ.get("REDACT_DATA_DIR", "data")
OUTPUT_DIR = os.environ.get("REDACT_OUTPUT_DIR", "output")

default_args = {"retries": 2, "retry_delay": timedelta(minutes=10)}

with DAG(
    dag_id="redact_weekly_validation",
    schedule="@weekly",
    start_date=datetime(2026, 1, 1),
    default_args=default_args,
    catchup=False,
) as dag:

    sample_medium_confidence = PythonOperator(
        task_id="sample_medium_confidence_hits",
        python_callable=airflow_tasks.sample_medium_confidence_hits,
        op_kwargs={
            "anonymized_log_path": os.path.join(OUTPUT_DIR, "anonymized.jsonl"),
            "sample_rate": 0.05,
        },
    )

    check_field_drift = PythonOperator(
        task_id="check_taxonomy_drift",
        python_callable=airflow_tasks.check_taxonomy_drift,
        op_kwargs={
            "baseline_path": os.path.join(DATA_DIR, "baseline.jsonl"),
            "current_path": os.path.join(DATA_DIR, "current.jsonl"),
            "threshold": 0.05,
        },
    )

    # Engineering upgrade, 2026-08-11: check_taxonomy_drift's result used to
    # go nowhere but the Airflow task log -- nothing outside this DAG run
    # could see it without someone remembering to go read that log. This
    # pushes the same result to Prometheus so it shows up on the same
    # dashboard/Alertmanager stack as redact-service's live request
    # metrics. REDACT_PUSHGATEWAY_URL unset (the default in every
    # environment this project has actually run in) makes this a
    # documented no-op, not a failure -- see
    # push_drift_metrics_to_prometheus's own docstring.
    push_drift_metrics = PythonOperator(
        task_id="push_drift_metrics_to_prometheus",
        python_callable=airflow_tasks.push_drift_metrics_task,
        op_kwargs={
            "pushgateway_url": os.environ.get("REDACT_PUSHGATEWAY_URL"),
        },
    )

    rotate_pseudonym_key = PythonOperator(
        task_id="rotate_pseudonymization_key",
        python_callable=airflow_tasks.rotate_pseudonymization_key,
        op_kwargs={
            "current_key_path": os.path.join(OUTPUT_DIR, "pseudo_key.txt"),
            "retired_keys_dir": os.path.join(OUTPUT_DIR, "retired_keys"),
        },
    )

    # Engineering upgrade: TOKEN_KEY had no rotation task at all before
    # this, unlike PSEUDO_KEY above -- a real gap given TokenStore's
    # tokenize() is meant to sit in production indefinitely. See
    # rotate_token_key's own docstring in airflow_tasks.py for why this is
    # safe to run on a schedule without needing to touch any already-
    # minted token: resolution is lookup-table-based, not key-based, so
    # rotating this key only affects the guessability resistance of
    # future NEW tokens, not the resolvability of existing ones.
    rotate_token_key = PythonOperator(
        task_id="rotate_token_key",
        python_callable=airflow_tasks.rotate_token_key,
        op_kwargs={
            "current_key_path": os.path.join(OUTPUT_DIR, "token_key.txt"),
            "retired_keys_dir": os.path.join(OUTPUT_DIR, "retired_keys"),
        },
    )

    sample_medium_confidence >> check_field_drift >> push_drift_metrics >> rotate_pseudonym_key >> rotate_token_key
