"""A deliberately bad DAG: run `dagdoctor review examples` to see every rule fire."""

import requests
from airflow import DAG
from airflow.models import Variable
from airflow.operators.python import PythonOperator
from airflow.utils.dates import days_ago

API_TOKEN = "demo-not-a-real-token-1234"
PARTNER_URL = Variable.get("partner_url")


def export_orders():
    try:
        rows = requests.get(PARTNER_URL, headers={"Authorization": API_TOKEN}, timeout=30).json()
    except:  # noqa: E722
        rows = []
    return len(rows)


with DAG(
    "legacy_partner_export",
    schedule_interval="@daily",
    start_date=days_ago(1),
) as dag:
    PythonOperator(task_id="export_orders", python_callable=export_orders)
