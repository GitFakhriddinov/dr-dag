"""Rule-based triage of Airflow task logs.

This is the part of Dr. Dag that works without any LLM. It answers three
questions quickly and deterministically:

1. Which DAG and task failed?
2. What exception was raised, and from which line of *our* code?
3. What kind of failure is it — and is it worth retrying, fixing in code,
   or handing to a human?
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .models import FailureContext, Frame, TriageResult
from .redact import redact_log

# "[2026-10-09, 03:00:12 UTC] {taskinstance.py:2905} ERROR - " and similar prefixes.
_AIRFLOW_PREFIX = re.compile(r"^\[[^\]]*\] (?:\{[^}]*\} )?(?:[A-Z]+ - )?")
_FRAME = re.compile(r'File "([^"]+)", line (\d+), in (\S+)')
_EXCEPTION = re.compile(
    r"^((?:[A-Za-z_]\w*\.)*"
    r"(?:[A-Za-z_]\w*(?:Error|Exception|Timeout|Exit|Interrupt|Failure)|(?<=errors\.)[A-Z]\w*))"
    r"(?::\s*(.*))?$"
)
_DAG_ID = re.compile(r"\bdag_id=([\w.\-]+)")
_TASK_ID = re.compile(r"\btask_id=([\w.\-]+)")

MAX_EXCERPT_LINES = 150
MAX_EXCERPT_CHARS = 12_000


@dataclass(frozen=True)
class Rule:
    category: str
    title: str
    patterns: tuple[re.Pattern[str], ...]
    retryable: bool
    code_fixable: bool
    hint: str


def _p(*patterns: str) -> tuple[re.Pattern[str], ...]:
    return tuple(re.compile(p, re.IGNORECASE) for p in patterns)


# Ordered from most to least specific: the first matching rule is the primary verdict.
RULES: tuple[Rule, ...] = (
    Rule(
        "memory",
        "Out of memory",
        _p(
            r"MEMORY_LIMIT_EXCEEDED",
            r"Memory limit \(.*\) exceeded",
            r"\bMemoryError\b",
            r"return code -9\b",
            r"exit code 137\b",
            r"OOMKilled",
        ),
        retryable=False,
        code_fixable=False,
        hint="Reduce the data processed per run (narrower date window, pre-aggregation, "
        "streaming instead of loading into pandas) or raise the worker/query memory limit.",
    ),
    Rule(
        "disk",
        "Disk full",
        _p(r"No space left on device", r"NOT_ENOUGH_SPACE"),
        retryable=False,
        code_fixable=False,
        hint="Free disk space on the worker or database host; check temp files and old logs.",
    ),
    Rule(
        "task_timeout",
        "Task or query timed out",
        _p(r"AirflowTaskTimeout", r"TIMEOUT_EXCEEDED", r"Timeout exceeded: elapsed"),
        retryable=False,
        code_fixable=False,
        hint="The task ran longer than allowed. Profile the slow query or step before "
        "raising execution_timeout; a retry will usually time out again.",
    ),
    Rule(
        "auth",
        "Authentication or permission error",
        _p(
            r"AUTHENTICATION_FAILED",
            r"Authentication failed",
            r"password authentication failed",
            r"\b401\b.*Unauthorized",
            r"403 Forbidden",
            r"ACCESS_DENIED",
            r"Access denied for user",
        ),
        retryable=False,
        code_fixable=False,
        hint="Credentials were rejected. Check the Airflow connection and whether the "
        "password or token was rotated or the user lost a grant.",
    ),
    Rule(
        "missing_connection",
        "Airflow connection is not defined",
        _p(r"The conn_id `[^`]+` isn't defined"),
        retryable=False,
        code_fixable=False,
        hint="Create the connection in Airflow (UI, CLI or secrets backend) or fix the conn_id.",
    ),
    Rule(
        "connection",
        "Could not reach a service",
        _p(
            r"Connection refused",
            r"Name or service not known",
            r"Temporary failure in name resolution",
            r"could not connect to server",
            r"Connection reset by peer",
            r"Max retries exceeded with url",
            r"NetworkError",
        ),
        retryable=True,
        code_fixable=False,
        hint="Usually transient (restart, deploy, network blip). Retry; if it persists, "
        "check that the host is up and reachable from the worker.",
    ),
    Rule(
        "network_timeout",
        "Network timeout",
        _p(r"Read timed out", r"Connection timed out", r"timed out"),
        retryable=True,
        code_fixable=False,
        hint="A remote call took too long. Retry with backoff; check the remote side if it repeats.",
    ),
    Rule(
        "rate_limit",
        "Rate limited by an API",
        _p(r"\b429\b", r"Too Many Requests", r"rate limit"),
        retryable=True,
        code_fixable=False,
        hint="Back off and retry later; consider retry_exponential_backoff=True.",
    ),
    Rule(
        "missing_table",
        "Table or relation does not exist",
        _p(
            r"UNKNOWN_TABLE",
            r"Table \S+ does(?:n't| not) exist",
            r'relation "[^"]+" does not exist',
            r"no such table",
        ),
        retryable=False,
        code_fixable=True,
        hint="Check the table name and schema, and whether an upstream job that creates it ran first.",
    ),
    Rule(
        "missing_column",
        "Unknown column",
        _p(
            r"UNKNOWN_IDENTIFIER",
            r"Missing columns",
            r"Unknown (?:expression )?identifier",
            r'column "[^"]+" does not exist',
            r"no such column",
        ),
        retryable=False,
        code_fixable=True,
        hint="A column was renamed, dropped or misspelled. Compare the query with the current table schema.",
    ),
    Rule(
        "sql_syntax",
        "SQL syntax error",
        _p(
            r"SYNTAX_ERROR",
            r"DB::Exception: Syntax error",
            r"syntax error at or near",
            r"You have an error in your SQL syntax",
        ),
        retryable=False,
        code_fixable=True,
        hint="Fix the SQL syntax at the position reported by the database.",
    ),
    Rule(
        "type_mismatch",
        "Data type mismatch",
        _p(
            r"TYPE_MISMATCH",
            r"CANNOT_PARSE",
            r"Cannot parse",
            r"invalid input syntax for type",
            r"Cannot convert",
        ),
        retryable=False,
        code_fixable=True,
        hint="A value doesn't match the expected type. Cast explicitly or handle bad rows.",
    ),
    Rule(
        "import_error",
        "Missing Python module",
        _p(r"ModuleNotFoundError", r"\bImportError\b", r"No module named"),
        retryable=False,
        code_fixable=True,
        hint="Add the package to the worker image/requirements or fix the import path.",
    ),
    Rule(
        "missing_variable",
        "Airflow variable is not defined",
        _p(r"Variable \S+ does not exist"),
        retryable=False,
        code_fixable=False,
        hint="Create the Airflow Variable or give Variable.get a default_var.",
    ),
    Rule(
        "killed",
        "Task was killed",
        _p(r"Detected zombie job", r"received SIGTERM", r"Task received SIGTERM signal"),
        retryable=True,
        code_fixable=False,
        hint="The worker was restarted or the task was stopped externally. Retry; if it "
        "repeats, check worker health and memory.",
    ),
    Rule(
        "upstream_failed",
        "Upstream task failed",
        _p(r"upstream_failed", r"Dependencies not met"),
        retryable=False,
        code_fixable=False,
        hint="This task never ran. Diagnose the first failed upstream task instead.",
    ),
)


def strip_prefix(line: str) -> str:
    """Remove the Airflow timestamp/logger prefix from a log line."""
    return _AIRFLOW_PREFIX.sub("", line, count=1)


def _clean_lines(log: str) -> list[str]:
    return [strip_prefix(line) for line in log.splitlines()]


def triage(log: str) -> list[TriageResult]:
    """Return every rule that matches, most specific first."""
    lines = _clean_lines(log)
    results: list[TriageResult] = []
    for rule in RULES:
        hit = _last_match(rule, lines)
        if hit is None:
            continue
        line_no, text = hit
        results.append(
            TriageResult(
                category=rule.category,
                title=rule.title,
                evidence=text.strip()[:300],
                line_no=line_no,
                retryable=rule.retryable,
                code_fixable=rule.code_fixable,
                hint=rule.hint,
            )
        )
    if not results:
        exc_type, exc_msg, _ = parse_traceback(log)
        if exc_type:
            results.append(
                TriageResult(
                    category="python_exception",
                    title=f"Unhandled {exc_type}",
                    evidence=f"{exc_type}: {exc_msg or ''}".strip()[:300],
                    line_no=None,
                    retryable=False,
                    code_fixable=True,
                    hint="Read the failing line in the traceback; this is usually a code bug "
                    "or unexpected input data.",
                )
            )
    return results


def _last_match(rule: Rule, lines: list[str]) -> tuple[int, str] | None:
    for index in range(len(lines) - 1, -1, -1):
        if any(p.search(lines[index]) for p in rule.patterns):
            return index + 1, lines[index]
    return None


def parse_traceback(log: str) -> tuple[str | None, str | None, list[Frame]]:
    """Return (exception type, message, frames) for the last traceback in a log."""
    lines = _clean_lines(log)
    starts = [i for i, line in enumerate(lines) if line.strip().startswith("Traceback (most recent")]
    if not starts:
        for line in reversed(lines):
            match = _EXCEPTION.match(line.strip())
            if match and not line[:1].isspace():
                return match.group(1), (match.group(2) or "").strip() or None, []
        return None, None, []

    frames: list[Frame] = []
    for raw in lines[starts[-1] + 1 :]:
        frame = _FRAME.search(raw)
        if frame:
            frames.append(Frame(frame.group(1), int(frame.group(2)), frame.group(3)))
            continue
        if not raw.strip() or raw[:1].isspace():
            continue  # source line, caret marker or blank
        match = _EXCEPTION.match(raw.strip())
        if match:
            return match.group(1), (match.group(2) or "").strip() or None, frames
        break
    return None, None, frames


def extract_ids(log: str) -> tuple[str | None, str | None]:
    """Find the dag_id and task_id Airflow writes into its task logs."""
    dag_ids = _DAG_ID.findall(log)
    task_ids = _TASK_ID.findall(log)
    return (dag_ids[-1] if dag_ids else None, task_ids[-1] if task_ids else None)


def build_failure_context(log: str) -> FailureContext:
    """Parse a raw task log into a :class:`FailureContext`."""
    dag_id, task_id = extract_ids(log)
    exc_type, exc_msg, frames = parse_traceback(log)
    excerpt_lines = log.splitlines()[-MAX_EXCERPT_LINES:]
    excerpt = redact_log("\n".join(excerpt_lines))[-MAX_EXCERPT_CHARS:]
    return FailureContext(
        dag_id=dag_id,
        task_id=task_id,
        exception_type=exc_type,
        exception_message=redact_log(exc_msg) if exc_msg else None,
        frames=frames,
        triage=triage(log),
        log_excerpt=excerpt,
    )
