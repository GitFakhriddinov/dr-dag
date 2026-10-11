from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = ROOT / "examples"
LOGS = EXAMPLES / "logs"


@pytest.fixture
def examples() -> Path:
    return EXAMPLES


@pytest.fixture
def read_log():
    def _read(name: str) -> str:
        return (LOGS / name).read_text()

    return _read


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


@pytest.fixture
def example_repo(tmp_path: Path) -> Path:
    """A throwaway git repository holding a copy of examples/dags and examples/sql."""
    repo = tmp_path / "repo"
    repo.mkdir()
    shutil.copytree(EXAMPLES / "dags", repo / "dags")
    shutil.copytree(EXAMPLES / "sql", repo / "sql")
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    return repo
