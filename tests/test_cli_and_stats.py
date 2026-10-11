import json
from datetime import datetime, timezone

from dagdoctor.callback import context_to_log, make_failure_callback
from dagdoctor.cli import main
from dagdoctor.models import Diagnosis
from dagdoctor.notify import chunks
from dagdoctor.stats import append_history, load_history, render_stats, summarize


def test_diagnose_json(capsys, examples):
    code = main(["diagnose", str(examples / "logs/task_timeout.log"), "--no-llm", "--json"])
    data = json.loads(capsys.readouterr().out)
    assert code == 0
    assert data["category"] == "task_timeout"
    assert data["action"] == "escalate"


def test_diagnose_markdown_retry(capsys, examples):
    main(["diagnose", str(examples / "logs/postgres_connection_refused.log"), "--no-llm"])
    out = capsys.readouterr().out
    assert "Retry" in out
    assert "Very$ecret1" not in out


def test_fix_dry_run_with_replayed_llm(capsys, examples):
    code = main(
        [
            "fix",
            str(examples / "logs/clickhouse_missing_column.log"),
            "--repo",
            str(examples),
            "--replay-llm",
            str(examples / "llm/missing_column.json"),
        ]
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "Dry run" in out
    assert "+    sum(amount)" in out


def test_fix_apply_commits(capsys, example_repo, examples):
    code = main(
        [
            "fix",
            str(examples / "logs/clickhouse_missing_column.log"),
            "--repo",
            str(example_repo),
            "--replay-llm",
            str(examples / "llm/missing_column.json"),
            "--apply",
        ]
    )
    assert code == 0
    assert "on branch dagdoctor/shop_daily_sales-build_rollup-" in capsys.readouterr().out


def test_review_exit_codes(capsys, examples):
    assert main(["review", str(examples)]) == 1
    assert main(["review", str(examples / "dags/shop_daily_sales.py"), "--fail-on", "warning"]) == 0
    assert main(["review", str(examples), "--fail-on", "never", "--format", "json"]) == 0


def _diag(category, action, dag="d"):
    return Diagnosis(dag, "t", category, "r", 0.5, "f", False, True, "rules", action=action)


def test_history_and_digest(tmp_path, capsys):
    history = tmp_path / "h.jsonl"
    october = datetime(2026, 10, 3, tzinfo=timezone.utc)
    append_history(history, _diag("connection", "retry", "a"), october)
    append_history(history, _diag("missing_column", "patch", "a"), october)
    append_history(history, _diag("memory", "escalate", "b"), october)
    append_history(history, _diag("memory", "escalate", "b"), datetime(2026, 9, 1, tzinfo=timezone.utc))
    history.write_text(history.read_text() + "not json\n")

    summary = summarize(load_history(history), "2026-10")
    assert summary["total"] == 3
    assert summary["auto_handled_pct"] == 66.7
    assert summary["top_dags"][0] == ("a", 2)
    assert "66.7%" in render_stats(summary)

    assert main(["stats", str(history)]) == 0
    assert "4" in capsys.readouterr().out


def test_callback_never_raises_and_reports(tmp_path):
    class TI:
        dag_id, task_id, run_id = "d", "t", "r1"

    try:
        {}["missing"]
    except KeyError as exc:
        error = exc

    messages = []
    callback = make_failure_callback(
        history_path=tmp_path / "h.jsonl", notifier=messages.append, use_llm=False
    )
    callback({"task_instance": TI(), "exception": error})
    assert "KeyError" in messages[0]
    assert load_history(tmp_path / "h.jsonl")[0]["dag_id"] == "d"

    broken = make_failure_callback(notifier=lambda text: 1 / 0, use_llm=False)
    broken({"task_instance": TI(), "exception": error})  # must not raise


def test_context_to_log_without_exception():
    assert context_to_log({}) == "dag_id=None, task_id=None, run_id=None"


def test_chunks_respects_limit():
    text = ("x" * 30 + "\n") * 10 + "y" * 120
    parts = chunks(text, size=50)
    assert all(len(p) <= 50 for p in parts)
    assert "".join(parts) == text
