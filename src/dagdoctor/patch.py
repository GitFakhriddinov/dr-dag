"""Guardrails for machine-written patches.

A patch only becomes a pull request if every check here passes. The idea is
that Dr. Dag can be wrong, but it should never be able to do damage: patches
are small, stay inside DAG/SQL folders, add no secrets, and must apply cleanly.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from .redact import contains_secret

DEFAULT_ALLOWED_PREFIXES = ("dags/", "sql/", "include/", "plugins/")
DEFAULT_MAX_CHANGED_LINES = 60
_BLOCKED_NAMES = re.compile(r"(?i)(\.env|secret|credential|password|connections?\.(ya?ml|json))")
_DIFF_FILE = re.compile(r"^(?:---|\+\+\+) (?:[ab]/)?(\S+)")


@dataclass
class PatchCheck:
    ok: bool
    files: list[str] = field(default_factory=list)
    added: int = 0
    removed: int = 0
    reasons: list[str] = field(default_factory=list)


def inspect_patch(patch: str) -> tuple[list[str], int, int, list[str]]:
    """Files touched, lines added, lines removed and the added lines themselves."""
    files: list[str] = []
    added_lines: list[str] = []
    removed = 0
    for line in patch.splitlines():
        if line.startswith(("--- ", "+++ ")):
            match = _DIFF_FILE.match(line)
            if match and match.group(1) != "/dev/null" and match.group(1) not in files:
                files.append(match.group(1))
        elif line.startswith("+"):
            added_lines.append(line[1:])
        elif line.startswith("-"):
            removed += 1
    return files, len(added_lines), removed, added_lines


def git_apply_check(patch: str, repo_root: Path) -> str | None:
    """Return None if ``git apply --check`` accepts the patch, else the error.

    Paths in the patch are relative to ``repo_root``. git resolves them against
    the working directory, so this works whether ``repo_root`` is a git
    repository, a folder inside one, or not under version control at all.
    """
    args = ["git", "-C", str(repo_root), "apply", "--check", "-"]
    result = subprocess.run(args, input=patch, capture_output=True, text=True)
    return None if result.returncode == 0 else (result.stderr.strip() or "git apply failed")


def check_patch(
    patch: str,
    repo_root: Path,
    allowed_prefixes: tuple[str, ...] = DEFAULT_ALLOWED_PREFIXES,
    max_changed_lines: int = DEFAULT_MAX_CHANGED_LINES,
) -> PatchCheck:
    """Run every guardrail and report all the reasons a patch is refused."""
    if not patch.strip():
        return PatchCheck(ok=False, reasons=["patch is empty"])

    files, added, removed, added_lines = inspect_patch(patch)
    check = PatchCheck(ok=True, files=files, added=added, removed=removed)

    if not files:
        check.reasons.append("patch does not name any file")
    for name in files:
        path = PurePosixPath(name)
        if path.is_absolute() or ".." in path.parts:
            check.reasons.append(f"{name}: path escapes the repository")
        elif not name.startswith(allowed_prefixes):
            check.reasons.append(f"{name}: outside allowed folders {', '.join(allowed_prefixes)}")
        if _BLOCKED_NAMES.search(path.name):
            check.reasons.append(f"{name}: looks like a secrets/config file")
    if added + removed > max_changed_lines:
        check.reasons.append(f"patch changes {added + removed} lines; the limit is {max_changed_lines}")
    if any(contains_secret(line) for line in added_lines):
        check.reasons.append("patch adds something that looks like a secret")
    if not check.reasons:
        error = git_apply_check(patch, repo_root)
        if error:
            check.reasons.append(f"patch does not apply: {error}")

    check.ok = not check.reasons
    return check
