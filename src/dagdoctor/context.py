"""Find the source code that a failure points at."""

from __future__ import annotations

import ast
import re
from collections.abc import Iterable
from pathlib import Path

from .models import CodeContext, Frame

# Where DAG files usually live inside Airflow containers -> where they live in the repo.
DEFAULT_PATH_MAP: dict[str, str] = {
    "/opt/airflow/dags": "dags",
    "/usr/local/airflow/dags": "dags",
}
SKIP_DIRS = {".git", "__pycache__", ".venv", "venv", "node_modules", ".tox", "build", "dist"}
_LIBRARY_MARKERS = ("site-packages", "dist-packages", "/lib/python")
_SQL_REF = re.compile(r"""["']([\w./\-]+\.sql)["']""")

MAX_FUNCTION_LINES = 120
MAX_SQL_FILES = 3
MAX_SQL_CHARS = 6_000


def parse_path_map(items: Iterable[str]) -> dict[str, str]:
    """Turn ``["/opt/airflow/dags=dags"]`` into ``{"/opt/airflow/dags": "dags"}``."""
    mapping: dict[str, str] = {}
    for item in items:
        if "=" not in item:
            raise ValueError(f"path map entries look like SRC=DST, got {item!r}")
        src, dst = item.split("=", 1)
        mapping[src.rstrip("/")] = dst.strip("/")
    return mapping


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def resolve_frame_path(file: str, repo_root: Path, path_map: dict[str, str]) -> Path | None:
    """Map a path from a traceback (inside the worker) to a file in the repo."""
    for src, dst in sorted(path_map.items(), key=lambda kv: -len(kv[0])):
        src = src.rstrip("/")
        if file == src or file.startswith(src + "/"):
            relative = file[len(src) :].lstrip("/")
            candidate = repo_root / dst / relative
            if candidate.is_file() and _inside(candidate, repo_root):
                return candidate.resolve()
    candidate = Path(file) if Path(file).is_absolute() else repo_root / file
    if candidate.is_file() and _inside(candidate, repo_root):
        return candidate.resolve()
    return None


def pick_user_frame(
    frames: list[Frame], repo_root: Path, path_map: dict[str, str]
) -> tuple[Frame, Path] | None:
    """The deepest traceback frame that belongs to our repo rather than a library."""
    for frame in reversed(frames):
        if any(marker in frame.file for marker in _LIBRARY_MARKERS):
            continue
        path = resolve_frame_path(frame.file, repo_root, path_map)
        if path is not None:
            return frame, path
    return None


def iter_files(root: Path, suffixes: tuple[str, ...]) -> Iterable[Path]:
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.suffix in suffixes:
            if not any(part in SKIP_DIRS for part in path.relative_to(root).parts):
                yield path


def find_dag_file(repo_root: Path, dag_id: str) -> Path | None:
    """Locate the file that defines ``dag_id`` when the traceback doesn't say."""
    quoted = rf"""["']{re.escape(dag_id)}["']"""
    patterns = [
        re.compile(rf"dag_id\s*=\s*{quoted}"),
        re.compile(rf"\bDAG\(\s*{quoted}"),
        re.compile(rf"@dag\b[\s\S]{{0,400}}?\bdef\s+{re.escape(dag_id)}\s*\("),
    ]
    for path in iter_files(repo_root, (".py",)):
        text = path.read_text(errors="replace")
        if dag_id in text and any(p.search(text) for p in patterns):
            return path.resolve()
    return None


def enclosing_function(source: str, line: int) -> tuple[str, int, int] | None:
    """Name and line span of the innermost function containing ``line``."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    best: tuple[str, int, int] | None = None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            start = min([node.lineno] + [d.lineno for d in node.decorator_list])
            end = node.end_lineno or node.lineno
            if start <= line <= end and (best is None or start >= best[1]):
                best = (node.name, start, end)
    return best


def _number(lines: list[str], start: int, end: int, mark: int | None) -> str:
    out = []
    for n in range(start, end + 1):
        flag = ">>" if n == mark else "  "
        out.append(f"{flag}{n:>5} | {lines[n - 1]}")
    return "\n".join(out)


def find_sql_files(source: str, file_path: Path, repo_root: Path) -> dict[str, str]:
    """SQL files referenced by name in ``source`` (e.g. ``"sql/report.sql"``)."""
    found: dict[str, str] = {}
    bases = [file_path.parent, file_path.parent.parent, repo_root, repo_root / "sql"]
    for ref in dict.fromkeys(_SQL_REF.findall(source)):
        for base in bases:
            candidate = (base / ref).resolve()
            if candidate.is_file() and _inside(candidate, repo_root):
                rel = candidate.relative_to(repo_root.resolve()).as_posix()
                found[rel] = candidate.read_text(errors="replace")[:MAX_SQL_CHARS]
                break
        if len(found) >= MAX_SQL_FILES:
            break
    # Also pick up files loaded as  SQL_DIR / "name.sql"
    for match in re.finditer(r"""/\s*["']([\w.\-]+\.sql)["']""", source):
        name = match.group(1)
        for candidate in iter_files(repo_root, (".sql",)):
            if candidate.name == name:
                rel = candidate.resolve().relative_to(repo_root.resolve()).as_posix()
                found.setdefault(rel, candidate.read_text(errors="replace")[:MAX_SQL_CHARS])
                break
        if len(found) >= MAX_SQL_FILES:
            break
    return found


def build_code_context(repo_root: Path, path: Path, line: int | None, radius: int = 20) -> CodeContext:
    """Numbered source around ``line`` (the whole function when it is short)."""
    source = path.read_text(errors="replace")
    lines = source.splitlines() or [""]
    function = None
    if line is None or not 1 <= line <= len(lines):
        line = None
        start, end = 1, min(len(lines), 200)
    else:
        start, end = max(1, line - radius), min(len(lines), line + radius)
        found = enclosing_function(source, line)
        if found:
            function, f_start, f_end = found
            if f_end - f_start <= MAX_FUNCTION_LINES:
                start, end = min(start, f_start), max(end, f_end)
    return CodeContext(
        path=path.resolve().relative_to(repo_root.resolve()).as_posix(),
        line=line,
        function=function,
        snippet=_number(lines, start, end, line),
        sql_files=find_sql_files(source, path, repo_root),
    )


def locate(
    repo_root: Path,
    frames: list[Frame],
    dag_id: str | None,
    path_map: dict[str, str] | None = None,
) -> CodeContext | None:
    """Best-effort code context for a failure: traceback frame first, then dag_id."""
    mapping = DEFAULT_PATH_MAP if path_map is None else path_map
    picked = pick_user_frame(frames, repo_root, mapping)
    if picked:
        frame, path = picked
        return build_code_context(repo_root, path, frame.line)
    if dag_id:
        path = find_dag_file(repo_root, dag_id)
        if path:
            return build_code_context(repo_root, path, None)
    return None
