"""Daily sales rollup for a demo online shop."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from airflow.decorators import dag, task
from airflow.hooks.base import BaseHook

SQL_DIR = Path(__file__).parent.parent / "sql"

default_args = {
    "owner": "analytics",
    "retries": 2,
    "retry_delay": timedelta(minutes=5),
    "execution_timeout": timedelta(minutes=30),
}


@dag(
    dag_id="shop_daily_sales",
    schedule="0 3 * * *",
    start_date=datetime(2026, 1, 1),
    catchup=False,
    default_args=default_args,
    tags=["demo", "sales"],
)
def shop_daily_sales():
    @task
    def build_rollup(ds: str | None = None) -> int:
        import clickhouse_connect

        conn = BaseHook.get_connection("clickhouse_default")
        client = clickhouse_connect.get_client(
            host=conn.host, port=conn.port, username=conn.login, password=conn.password
        )
        sql = (SQL_DIR / "shop_daily_sales.sql").read_text()
        summary = client.command(sql, parameters={"ds": ds})
        return int(summary.written_rows)

    build_rollup()


shop_daily_sales()
