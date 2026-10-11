"""Static review of DAG and SQL files for common Airflow anti-patterns.

Runs on source code only (via ``ast`` and regexes) — it never imports a DAG,
so it is safe to run in CI on any repository.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from .context import iter_files
from .models import Finding

RULES: dict[str, tuple[str, str]] = {
    "DD000": ("error", "File does not parse"),
    "DD001": ("warning", "No retries configured"),
    "DD002": ("error", "Dynamic start_date"),
    "DD003": ("info", "catchup not set explicitly"),
    "DD004": ("error", "Hard-coded secret"),
    "DD005": ("warning", "Expensive call at import time"),
    "DD006": ("info", "Deprecated schedule_interval"),
    "DD007": ("info", "No owner in default_args"),
    "DD008": ("info", "DAG has no tags"),
    "DD009": ("warning", "No execution_timeout"),
    "DD010": ("warning", "Bare except"),
    "SQL001": ("info", "SELECT *"),
    "SQL002": ("error", "DELETE/UPDATE without WHERE"),
}

_SECRET_NAME = re.compile(
    r"(?i)(?:^|_)(password|passwd|pwd|secret|token|api_?key|access_?key|private_?key)(?:$|_)"
)
_URI_WITH_PASSWORD = re.compile(r"\b[a-z][a-z0-9+.\-]*://[^\s:/@]+:[^\s@/]+@", re.I)
_DAG_CALLS = {"DAG", "models.DAG", "airflow.DAG"}
_IMPORT_TIME_CALLS = ("Variable.get", "BaseHook.get_connection", "get_connection")
_IMPORT_TIME_PREFIXES = ("requests.", "pd.read_sql", "pandas.read_sql", "urllib.request.urlopen")
_NOW_CALLS = {"now", "utcnow", "today"}


def _finding(path: str, line: int, rule: str, message: str) -> Finding:
    return Finding(path=path, line=line, rule=rule, severity=RULES[rule][0], message=message)


def _dotted(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    return None


def _call_name(node: ast.Call) -> str:
    return _dotted(node.func) or ""


def _is_dynamic_date(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    name = _call_name(node)
    return name.split(".")[-1] in _NOW_CALLS or name.endswith("days_ago")


def _dict_keys(node: ast.AST) -> dict[str, ast.AST]:
    """String keys of a dict literal or dict(...) call."""
    if isinstance(node, ast.Dict):
        return {
            k.value: v
            for k, v in zip(node.keys, node.values, strict=True)
            if isinstance(k, ast.Constant) and isinstance(k.value, str)
        }
    if isinstance(node, ast.Call) and _call_name(node) == "dict":
        return {kw.arg: kw.value for kw in node.keywords if kw.arg}
    return {}


def _import_time_calls(tree: ast.Module) -> list[ast.Call]:
    """Calls executed when the scheduler parses the file (outside functions)."""
    found: list[ast.Call] = []

    def visit(node: ast.AST) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
            # decorator arguments still run at import time
            for deco in getattr(node, "decorator_list", []):
                visit(deco)
            return
        if isinstance(node, ast.Call):
            found.append(node)
        for child in ast.iter_child_nodes(node):
            visit(child)

    for statement in tree.body:
        visit(statement)
    return found


def _secret_findings(tree: ast.Module, rel: str) -> list[Finding]:
    findings: list[Finding] = []

    def literal(node: ast.AST | None) -> bool:
        return (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and len(node.value) >= 4
            and not node.value.startswith(("{{", "http://", "https://", "***"))
        )

    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and literal(node.value):
            for target in node.targets:
                name = _dotted(target)
                if name and _SECRET_NAME.search(name.split(".")[-1]):
                    findings.append(
                        _finding(
                            rel,
                            node.lineno,
                            "DD004",
                            f"`{name}` is assigned a literal value; "
                            "use an Airflow connection, Variable or secrets backend",
                        )
                    )
        elif isinstance(node, ast.keyword) and node.arg and literal(node.value):
            if _SECRET_NAME.search(node.arg):
                findings.append(
                    _finding(rel, node.value.lineno, "DD004", f"argument `{node.arg}` is a literal value")
                )
        elif isinstance(node, ast.Dict):
            for key, value in _dict_keys(node).items():
                if _SECRET_NAME.search(key) and literal(value):
                    findings.append(
                        _finding(rel, value.lineno, "DD004", f"dict key `{key}` holds a literal value")
                    )
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and _URI_WITH_PASSWORD.search(node.value)
        ):
            findings.append(_finding(rel, node.lineno, "DD004", "connection URI with an embedded password"))
    return findings


def lint_python(path: Path, rel: str) -> list[Finding]:
    source = path.read_text(errors="replace")
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return [_finding(rel, exc.lineno or 1, "DD000", f"syntax error: {exc.msg}")]

    findings = _secret_findings(tree, rel)

    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler) and node.type is None:
            findings.append(
                _finding(
                    rel, node.lineno, "DD010", "bare `except:` hides real errors (and catches task timeouts)"
                )
            )

    for call in _import_time_calls(tree):
        name = _call_name(call)
        if name.endswith(_IMPORT_TIME_CALLS) or name.startswith(_IMPORT_TIME_PREFIXES):
            findings.append(
                _finding(
                    rel,
                    call.lineno,
                    "DD005",
                    f"`{name}(...)` runs every time the scheduler parses this "
                    "file; move it into a task or use a Jinja template",
                )
            )

    # Collect DAG definitions: DAG(...) calls and @dag(...) decorators.
    dag_calls: list[ast.Call] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _call_name(node) in _DAG_CALLS:
            dag_calls.append(node)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for deco in node.decorator_list:
                if isinstance(deco, ast.Call) and _call_name(deco).split(".")[-1] == "dag":
                    dag_calls.append(deco)

    default_args: dict[str, ast.AST] = {}
    all_keywords: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "default_args" for t in node.targets
        ):
            default_args.update(_dict_keys(node.value))
        if isinstance(node, ast.Call):
            for kw in node.keywords:
                if kw.arg:
                    all_keywords.add(kw.arg)
                    if kw.arg == "start_date" and _is_dynamic_date(kw.value):
                        findings.append(
                            _finding(
                                rel,
                                kw.value.lineno,
                                "DD002",
                                "start_date must be a fixed date, not now()/days_ago()",
                            )
                        )
    for key, value in default_args.items():
        if key == "start_date" and _is_dynamic_date(value):
            findings.append(
                _finding(rel, value.lineno, "DD002", "start_date must be a fixed date, not now()/days_ago()")
            )

    for dag_call in dag_calls:
        kwargs = {kw.arg: kw.value for kw in dag_call.keywords if kw.arg}
        inline_args = _dict_keys(kwargs["default_args"]) if "default_args" in kwargs else {}
        args = {**default_args, **inline_args}
        line = dag_call.lineno
        if "retries" not in args and "retries" not in all_keywords:
            findings.append(
                _finding(
                    rel, line, "DD001", "set retries in default_args so transient failures heal themselves"
                )
            )
        if "catchup" not in kwargs:
            findings.append(
                _finding(rel, line, "DD003", "set catchup explicitly; True can trigger a backfill storm")
            )
        if "schedule_interval" in kwargs:
            findings.append(
                _finding(
                    rel,
                    kwargs["schedule_interval"].lineno,
                    "DD006",
                    "use `schedule=`; schedule_interval was deprecated in Airflow 2.4 and removed in 3.0",
                )
            )
        if "owner" not in args:
            findings.append(_finding(rel, line, "DD007", "add an owner so failures reach the right person"))
        if "tags" not in kwargs:
            findings.append(_finding(rel, line, "DD008", "add tags to make the DAG findable"))
        if (
            "execution_timeout" not in args
            and "execution_timeout" not in all_keywords
            and "dagrun_timeout" not in kwargs
        ):
            findings.append(
                _finding(
                    rel, line, "DD009", "without execution_timeout a hung query can block a slot forever"
                )
            )
    return findings


def _blank_comments(sql: str) -> str:
    """Replace SQL comments with spaces, keeping offsets and line numbers intact."""
    sql = re.sub(r"/\*.*?\*/", lambda m: re.sub(r"[^\n]", " ", m.group()), sql, flags=re.S)
    return re.sub(r"--[^\n]*", lambda m: " " * len(m.group()), sql)


def lint_sql(path: Path, rel: str) -> list[Finding]:
    original = path.read_text(errors="replace")
    sql = _blank_comments(original)

    def line_of(offset: int) -> int:
        return sql.count("\n", 0, offset) + 1

    findings = [
        _finding(
            rel,
            line_of(m.start()),
            "SQL001",
            "SELECT * breaks silently when upstream columns change; list the columns",
        )
        for m in re.finditer(r"(?i)\bselect\s+(?:distinct\s+)?\*", sql)
    ]
    offset = 0
    for statement in sql.split(";"):
        stripped = statement.lstrip()
        start = offset + (len(statement) - len(stripped))
        verb = stripped.split(None, 1)[0].lower() if stripped else ""
        if verb in {"delete", "update"} and not re.search(r"(?i)\bwhere\b", stripped):
            findings.append(
                _finding(rel, line_of(start), "SQL002", f"{verb.upper()} without WHERE touches every row")
            )
        offset += len(statement) + 1
    return findings


def review(root: Path) -> list[Finding]:
    """Review every .py and .sql file under ``root``."""
    root = Path(root)
    findings: list[Finding] = []
    files = [root] if root.is_file() else list(iter_files(root, (".py", ".sql")))
    base = root.parent if root.is_file() else root
    for path in files:
        rel = path.relative_to(base).as_posix()
        if path.suffix == ".py":
            findings += lint_python(path, rel)
        elif path.suffix == ".sql":
            findings += lint_sql(path, rel)
    order = {"error": 0, "warning": 1, "info": 2}
    return sorted(set(findings), key=lambda f: (f.path, f.line, order[f.severity], f.rule))
