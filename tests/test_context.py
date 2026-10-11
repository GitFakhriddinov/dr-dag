from pathlib import Path

import pytest

from dagdoctor.context import (
    DEFAULT_PATH_MAP,
    enclosing_function,
    find_dag_file,
    locate,
    parse_path_map,
    resolve_frame_path,
)
from dagdoctor.models import Frame


def test_parse_path_map():
    assert parse_path_map(["/opt/airflow/dags/=dags/"]) == {"/opt/airflow/dags": "dags"}
    with pytest.raises(ValueError):
        parse_path_map(["no-equals-sign"])


def test_resolve_frame_path_maps_worker_paths(examples):
    path = resolve_frame_path("/opt/airflow/dags/shop_daily_sales.py", examples, DEFAULT_PATH_MAP)
    assert path == (examples / "dags" / "shop_daily_sales.py").resolve()


def test_resolve_frame_path_refuses_escape(examples):
    assert resolve_frame_path("/opt/airflow/dags/../../etc/passwd", examples, DEFAULT_PATH_MAP) is None


def test_locate_skips_library_frames_and_loads_sql(examples):
    frames = [
        Frame("/home/airflow/.local/lib/python3.12/site-packages/airflow/x.py", 10, "run"),
        Frame("/opt/airflow/dags/shop_daily_sales.py", 39, "build_rollup"),
        Frame("/home/airflow/.local/lib/python3.12/site-packages/clickhouse_connect/c.py", 1, "command"),
    ]
    code = locate(examples, frames, "shop_daily_sales")
    assert code.path == "dags/shop_daily_sales.py"
    assert code.line == 39
    assert code.function == "build_rollup"
    assert ">>   39 |" in code.snippet
    assert "sql/shop_daily_sales.sql" in code.sql_files
    assert "sum(amont)" in code.sql_files["sql/shop_daily_sales.sql"]


def test_locate_falls_back_to_dag_id(examples):
    code = locate(examples, [], "legacy_partner_export")
    assert code.path == "dags/legacy_partner_export.py"
    assert code.line is None


def test_find_dag_file_with_decorator(examples):
    assert find_dag_file(examples, "shop_daily_sales").name == "shop_daily_sales.py"
    assert find_dag_file(examples, "does_not_exist") is None


def test_enclosing_function_picks_innermost():
    source = "def outer():\n    def inner():\n        x = 1\n    return inner\n"
    assert enclosing_function(source, 3) == ("inner", 2, 3)
    assert enclosing_function("def broken(:\n", 1) is None


def test_locate_returns_none_without_clues(tmp_path: Path):
    assert locate(tmp_path, [], None) is None
