"""Plug Dr. Dag into Airflow with ``on_failure_callback``.

Usage in a DAG file::

    from dagdoctor.callback import make_failure_callback

    default_args = {
        "on_failure_callback": make_failure_callback(repo_root="/opt/airflow"),
    }

The callback never raises: if Dr. Dag itself fails, it logs the error and the
task's own failure handling carries on as usual.
"""

from __future__ import annotations

import logging
import traceback
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .diagnose import diagnose
from .llm import AnthropicClient
from .report import render_diagnosis
from .stats import append_history

log = logging.getLogger(__name__)


def context_to_log(context: dict[str, Any]) -> str:
    """Rebuild a log-like text (ids + traceback) from an Airflow callback context."""
    ti = context.get("task_instance") or context.get("ti")
    dag_id = getattr(ti, "dag_id", None)
    task_id = getattr(ti, "task_id", None)
    run_id = getattr(ti, "run_id", None) or context.get("run_id")
    header = f"dag_id={dag_id}, task_id={task_id}, run_id={run_id}"
    exc = context.get("exception")
    if exc is None:
        return header
    if isinstance(exc, BaseException):
        tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    else:
        tb = str(exc)
    return f"{header}\n{tb}"


def make_failure_callback(
    repo_root: str | Path | None = None,
    path_map: dict[str, str] | None = None,
    history_path: str | Path | None = None,
    notifier: Callable[[str], None] | None = None,
    use_llm: bool = True,
) -> Callable[[dict[str, Any]], None]:
    def on_failure(context: dict[str, Any]) -> None:
        try:
            client = AnthropicClient.from_env() if use_llm else None
            report = diagnose(
                context_to_log(context),
                repo_root=Path(repo_root) if repo_root else None,
                path_map=path_map,
                client=client,
            )
            if history_path:
                append_history(Path(history_path), report.diagnosis)
            text = render_diagnosis(report)
            if notifier:
                notifier(text)
            else:
                log.warning("%s", text)
        except Exception:  # noqa: BLE001 - a broken doctor must never break the patient
            log.exception("dagdoctor: failure callback crashed")

    return on_failure
