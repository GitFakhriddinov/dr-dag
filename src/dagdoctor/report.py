"""Markdown rendering for terminals, pull requests and chat messages."""

from __future__ import annotations

from collections import Counter, defaultdict

from .diagnose import Report
from .models import Finding
from .review import RULES

_ACTION_LABEL = {
    "retry": "🔁 Retry — looks transient",
    "patch": "🩹 Patch proposed — passed all guardrails",
    "escalate": "🙋 Needs a human",
}


def render_diagnosis(report: Report) -> str:
    d = report.diagnosis
    lines = [
        f"## 🩺 Dr. Dag: `{d.dag_id or '?'}` / `{d.task_id or '?'}`",
        "",
        f"**Verdict:** {_ACTION_LABEL.get(d.action, d.action)}",
        f"**Category:** `{d.category}` · **Confidence:** {d.confidence:.0%} · **Source:** {d.source}",
        "",
        "### Root cause",
        d.root_cause,
        "",
        "### What to do",
        d.fix_summary,
    ]
    ctx = report.context
    if ctx.exception_type:
        lines += ["", "### Exception", "```", f"{ctx.exception_type}: {ctx.exception_message or ''}", "```"]
    if report.code:
        where = report.code.path + (f":{report.code.line}" if report.code.line else "")
        lines += [
            "",
            f"**Code:** `{where}`" + (f" in `{report.code.function}()`" if report.code.function else ""),
        ]
        if report.code.sql_files:
            lines.append("**SQL:** " + ", ".join(f"`{p}`" for p in report.code.sql_files))
    if d.patch:
        lines += ["", "### Proposed patch", "```diff", d.patch.rstrip(), "```"]
    if d.notes:
        lines += ["", "### Notes"] + [f"- {note}" for note in d.notes]
    return "\n".join(lines) + "\n"


def render_review(findings: list[Finding], target: str) -> str:
    counts = Counter(f.severity for f in findings)
    lines = [
        f"# 🩺 Dr. Dag review: `{target}`",
        "",
        f"**{counts['error']}** errors · **{counts['warning']}** warnings · **{counts['info']}** notes",
    ]
    if not findings:
        return "\n".join(lines + ["", "No issues found. 🎉"]) + "\n"

    by_rule = Counter(f.rule for f in findings)
    lines += ["", "| Rule | Severity | Count | Check |", "|---|---|---|---|"]
    for rule in sorted(by_rule):
        severity, title = RULES[rule]
        lines.append(f"| {rule} | {severity} | {by_rule[rule]} | {title} |")

    by_file: dict[str, list[Finding]] = defaultdict(list)
    for finding in findings:
        by_file[finding.path].append(finding)
    icon = {"error": "❌", "warning": "⚠️", "info": "ℹ️"}
    lines += ["", "## Findings"]
    for path, items in by_file.items():
        lines += ["", f"### `{path}`"]
        lines += [f"- {icon[f.severity]} L{f.line} · **{f.rule}** · {f.message}" for f in items]
    return "\n".join(lines) + "\n"
