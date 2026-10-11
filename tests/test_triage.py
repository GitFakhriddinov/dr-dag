import pytest

from dagdoctor.triage import build_failure_context, extract_ids, parse_traceback, strip_prefix, triage


@pytest.mark.parametrize(
    ("log_file", "category", "retryable"),
    [
        ("clickhouse_missing_column.log", "missing_column", False),
        ("postgres_connection_refused.log", "connection", True),
        ("task_timeout.log", "task_timeout", False),
        ("import_error.log", "import_error", False),
        ("clickhouse_memory_limit.log", "memory", False),
    ],
)
def test_primary_category_for_example_logs(read_log, log_file, category, retryable):
    results = triage(read_log(log_file))
    assert results[0].category == category
    assert results[0].retryable is retryable


@pytest.mark.parametrize(
    ("line", "category"),
    [
        ('psycopg2.errors.UndefinedTable: relation "mart.orders" does not exist', "missing_table"),
        ("Code: 60. DB::Exception: Table raw.orderz does not exist. (UNKNOWN_TABLE)", "missing_table"),
        ('ERROR:  column "amont" does not exist', "missing_column"),
        ("Code: 62. DB::Exception: Syntax error: failed at position 41", "sql_syntax"),
        (
            "airflow.exceptions.AirflowNotFoundException: The conn_id `ch_prod` isn't defined",
            "missing_connection",
        ),
        ("requests.exceptions.HTTPError: 429 Client Error: Too Many Requests for url", "rate_limit"),
        ("Code: 516. DB::Exception: etl: Authentication failed", "auth"),
        ("OSError: [Errno 28] No space left on device", "disk"),
        ("Task received SIGTERM signal", "killed"),
    ],
)
def test_rules_for_single_lines(line, category):
    assert triage(line)[0].category == category


def test_unknown_exception_falls_back_to_python_exception():
    log = (
        "Traceback (most recent call last):\n"
        '  File "/opt/airflow/dags/x.py", line 3, in f\n'
        "    1 / 0\n"
        "ZeroDivisionError: division by zero\n"
    )
    [result] = triage(log)
    assert result.category == "python_exception"
    assert "ZeroDivisionError" in result.evidence


def test_nothing_matches_returns_empty():
    assert triage("all good here\nnothing to see") == []


def test_parse_traceback_finds_user_frame_and_exception(read_log):
    exc_type, message, frames = parse_traceback(read_log("clickhouse_missing_column.log"))
    assert exc_type == "clickhouse_connect.driver.exceptions.DatabaseError"
    assert message.startswith("HTTPDriver for")
    user = [f for f in frames if f.file.startswith("/opt/airflow/dags")]
    assert user[0].line == 39
    assert user[0].function == "build_rollup"


def test_parse_traceback_handles_psycopg_error_classes():
    log = (
        "Traceback (most recent call last):\n"
        '  File "/opt/airflow/dags/x.py", line 9, in load\n'
        "    cur.execute(sql)\n"
        'psycopg2.errors.UndefinedColumn: column "amont" does not exist\n'
    )
    exc_type, message, _ = parse_traceback(log)
    assert exc_type == "psycopg2.errors.UndefinedColumn"
    assert message == 'column "amont" does not exist'


def test_extract_ids(read_log):
    assert extract_ids(read_log("task_timeout.log")) == ("events_hourly", "aggregate_events")


def test_strip_prefix_keeps_indentation():
    line = '[2026-10-09, 03:00:06 UTC] {taskinstance.py:3311} ERROR -   File "x.py", line 1, in f'
    assert strip_prefix(line) == '  File "x.py", line 1, in f'


def test_failure_context_is_redacted(read_log):
    ctx = build_failure_context(read_log("clickhouse_missing_column.log"))
    assert "S3cretPass" not in ctx.log_excerpt
    assert "S3cretPass" not in (ctx.exception_message or "")
    assert ctx.dag_id == "shop_daily_sales"
    assert ctx.primary.category == "missing_column"
