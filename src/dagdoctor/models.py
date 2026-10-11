"""Plain data types shared across Dr. Dag."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field


@dataclass(frozen=True)
class Frame:
    """One frame of a Python traceback."""

    file: str
    line: int
    function: str


@dataclass(frozen=True)
class TriageResult:
    """A rule-based guess at what kind of failure a log shows."""

    category: str
    title: str
    evidence: str
    line_no: int | None
    retryable: bool
    code_fixable: bool
    hint: str


@dataclass
class FailureContext:
    """Everything Dr. Dag extracted from a failed task's log."""

    dag_id: str | None
    task_id: str | None
    exception_type: str | None
    exception_message: str | None
    frames: list[Frame]
    triage: list[TriageResult]
    log_excerpt: str

    @property
    def primary(self) -> TriageResult | None:
        return self.triage[0] if self.triage else None


@dataclass
class CodeContext:
    """The source code around the failing line, plus any SQL files it loads."""

    path: str
    line: int | None
    function: str | None
    snippet: str
    sql_files: dict[str, str] = field(default_factory=dict)


@dataclass
class Diagnosis:
    """The final verdict for one failure."""

    dag_id: str | None
    task_id: str | None
    category: str
    root_cause: str
    confidence: float
    fix_summary: str
    retryable: bool
    needs_human: bool
    source: str  # "rules" or "llm"
    patch: str = ""
    action: str = "escalate"  # "retry", "patch" or "escalate"
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class Finding:
    """One issue found by the static DAG/SQL review."""

    path: str
    line: int
    rule: str
    severity: str  # "error", "warning" or "info"
    message: str
