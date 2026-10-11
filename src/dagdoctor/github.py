"""Turn an accepted patch into a branch and a *draft* pull request.

Draft only, on purpose: a human always reviews and merges.
"""

from __future__ import annotations

import json
import re
import subprocess
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

GITHUB_API = "https://api.github.com"


class GitError(RuntimeError):
    pass


def _git(repo: Path, *args: str, stdin: str | None = None) -> str:
    result = subprocess.run(["git", "-C", str(repo), *args], input=stdin, capture_output=True, text=True)
    if result.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def branch_name(dag_id: str | None, task_id: str | None, now: datetime | None = None) -> str:
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%d%H%M")
    slug = "-".join(part for part in (dag_id, task_id) if part) or "fix"
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", slug).strip("-")[:60]
    return f"dagdoctor/{slug}-{stamp}"


def create_fix_branch(repo: Path, branch: str, patch: str, message: str) -> str:
    """Create ``branch`` from HEAD, apply the patch and commit it. Returns the commit sha."""
    if _git(repo, "rev-parse", "--show-prefix").strip():
        raise GitError("--repo must point at the root of the git repository")
    if _git(repo, "status", "--porcelain").strip():
        raise GitError("working tree has uncommitted changes; refusing to touch it")
    _git(repo, "checkout", "-b", branch)
    try:
        _git(repo, "apply", "--index", "-", stdin=patch)
        _git(repo, "commit", "-m", message)
    except GitError:
        _git(repo, "checkout", "-")
        _git(repo, "branch", "-D", branch)
        raise
    return _git(repo, "rev-parse", "HEAD").strip()


def push_branch(repo: Path, branch: str, remote: str = "origin") -> None:
    _git(repo, "push", "--set-upstream", remote, branch)


def open_draft_pr(
    repo_slug: str,
    head: str,
    base: str,
    title: str,
    body: str,
    token: str,
    api: str = GITHUB_API,
) -> str:
    """Open a draft PR and return its URL."""
    payload = json.dumps({"title": title, "head": head, "base": base, "body": body, "draft": True}).encode()
    request = urllib.request.Request(
        f"{api}/repos/{repo_slug}/pulls",
        data=payload,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)["html_url"]
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:500]
        raise GitError(f"GitHub returned HTTP {exc.code}: {detail}") from exc
