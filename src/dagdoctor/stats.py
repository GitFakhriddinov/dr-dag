"""Failure history and the monthly digest.

Each diagnosis can be appended to a JSON Lines file. The digest answers the
questions a data team lead actually asks: what breaks most, which DAGs are the
noisiest, and how much was handled without a human.
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from .models import Diagnosis


def append_history(path: Path, diagnosis: Diagnosis, when: datetime | None = None) -> None:
    entry = {
        "ts": (when or datetime.now(timezone.utc)).isoformat(timespec="seconds"),
        "dag_id": diagnosis.dag_id,
        "task_id": diagnosis.task_id,
        "category": diagnosis.category,
        "action": diagnosis.action,
        "source": diagnosis.source,
        "confidence": round(diagnosis.confidence, 2),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry) + "\n")


def load_history(path: Path) -> list[dict]:
    entries = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return entries


def summarize(entries: list[dict], month: str | None = None) -> dict:
    """Aggregate history, optionally for one month given as ``YYYY-MM``."""
    if month:
        entries = [e for e in entries if str(e.get("ts", "")).startswith(month)]
    total = len(entries)
    actions = Counter(e.get("action", "escalate") for e in entries)
    auto = actions["retry"] + actions["patch"]
    return {
        "month": month or "all time",
        "total": total,
        "by_category": Counter(e.get("category", "unknown") for e in entries).most_common(),
        "top_dags": Counter(e.get("dag_id") or "?" for e in entries).most_common(10),
        "by_action": dict(actions),
        "auto_handled_pct": round(100 * auto / total, 1) if total else 0.0,
    }


def render_stats(summary: dict) -> str:
    lines = [
        f"# 🩺 Dr. Dag digest — {summary['month']}",
        "",
        f"**{summary['total']}** failures diagnosed · "
        f"**{summary['auto_handled_pct']}%** handled without a human (retry or patch)",
    ]
    if not summary["total"]:
        return "\n".join(lines) + "\n"
    lines += ["", "## By category", "", "| Category | Failures |", "|---|---|"]
    lines += [f"| {cat} | {n} |" for cat, n in summary["by_category"]]
    lines += ["", "## Noisiest DAGs", "", "| DAG | Failures |", "|---|---|"]
    lines += [f"| `{dag}` | {n} |" for dag, n in summary["top_dags"]]
    lines += ["", "## Actions", ""]
    lines += [f"- {action}: {n}" for action, n in sorted(summary["by_action"].items())]
    return "\n".join(lines) + "\n"
