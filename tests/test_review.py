from dagdoctor.review import review


def rules_for(findings, path):
    return {f.rule for f in findings if f.path == path}


def test_bad_dag_triggers_every_python_rule(examples):
    findings = review(examples)
    assert rules_for(findings, "dags/legacy_partner_export.py") == {
        "DD001",
        "DD002",
        "DD003",
        "DD004",
        "DD005",
        "DD006",
        "DD007",
        "DD008",
        "DD009",
        "DD010",
    }


def test_good_dag_is_clean(examples):
    assert rules_for(review(examples), "dags/shop_daily_sales.py") == set()


def test_sql_rules(examples):
    findings = [f for f in review(examples) if f.path == "sql/legacy_cleanup.sql"]
    assert {(f.rule, f.line) for f in findings} == {("SQL002", 2), ("SQL001", 4)}


def test_secret_message_never_contains_the_secret(examples):
    for finding in review(examples):
        assert "demo-not-a-real-token" not in finding.message


def test_sql_comments_and_where_clauses(tmp_path):
    (tmp_path / "ok.sql").write_text(
        "-- SELECT * FROM x;\n/* DELETE FROM y; */\nDELETE FROM t WHERE day < today() - 30;\n"
        "SELECT count(*) FROM t;\n"
    )
    assert review(tmp_path) == []


def test_syntax_error_is_reported(tmp_path):
    (tmp_path / "broken.py").write_text("def f(:\n")
    [finding] = review(tmp_path)
    assert finding.rule == "DD000"


def test_review_single_file(examples):
    findings = review(examples / "sql" / "legacy_cleanup.sql")
    assert {f.path for f in findings} == {"legacy_cleanup.sql"}


def test_inline_default_args_count(tmp_path):
    (tmp_path / "d.py").write_text(
        "from datetime import datetime, timedelta\n"
        "from airflow import DAG\n"
        "with DAG('d', schedule=None, start_date=datetime(2026, 1, 1), catchup=False, tags=['x'],\n"
        "         default_args={'owner': 'a', 'retries': 1, 'execution_timeout': timedelta(minutes=5)}):\n"
        "    pass\n"
    )
    assert review(tmp_path) == []
