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

    rotate_pseudonym_key = PythonOperator(
        task_id="rotate_pseudonymization_key",
        python_callable=airflow_tasks.rotate_pseudonymization_key,
        op_kwargs={
            "current_key_path": os.path.join(OUTPUT_DIR, "pseudo_key.txt"),
            "retired_keys_dir": os.path.join(OUTPUT_DIR, "retired_keys"),
        },
    )

    sample_medium_confidence >> check_field_drift >> rotate_pseudonym_key
