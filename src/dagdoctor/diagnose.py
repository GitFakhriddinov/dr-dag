"""Put triage, code context, the LLM and the patch guardrails together."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .context import locate
from .llm import Completer, LLMError, diagnose_with_llm
from .models import CodeContext, Diagnosis, FailureContext
from .patch import PatchCheck, check_patch
from .triage import build_failure_context


@dataclass
class Report:
    diagnosis: Diagnosis
    context: FailureContext
    code: CodeContext | None
    patch_check: PatchCheck | None = None


def rules_diagnosis(ctx: FailureContext) -> Diagnosis:
    """A diagnosis from the triage rules alone (no LLM)."""
    primary = ctx.primary
    exception = f"{ctx.exception_type}: {ctx.exception_message}".rstrip(": ") if ctx.exception_type else None
    if primary is None:
        return Diagnosis(
            dag_id=ctx.dag_id,
            task_id=ctx.task_id,
            category="unknown",
            root_cause=exception or "No known failure pattern found in the log.",
            confidence=0.1,
            fix_summary="Read the full task log; no rule matched this failure.",
            retryable=False,
            needs_human=True,
            source="rules",
        )
    return Diagnosis(
        dag_id=ctx.dag_id,
        task_id=ctx.task_id,
        category=primary.category,
        root_cause=f"{primary.title}. {exception or primary.evidence}",
        confidence=0.6,
        fix_summary=primary.hint,
        retryable=primary.retryable,
        needs_human=not primary.retryable,
        source="rules",
    )


def decide_action(diagnosis: Diagnosis, patch_check: PatchCheck | None) -> str:
    if diagnosis.patch and patch_check is not None and patch_check.ok:
        return "patch"
    if diagnosis.retryable and not diagnosis.patch:
        return "retry"
    return "escalate"


def diagnose(
    log: str,
    repo_root: Path | None = None,
    path_map: dict[str, str] | None = None,
    client: Completer | None = None,
) -> Report:
    """Diagnose one failed task log end to end."""
    ctx = build_failure_context(log)
    code = locate(Path(repo_root), ctx.frames, ctx.dag_id, path_map) if repo_root else None

    diagnosis: Diagnosis | None = None
    notes: list[str] = []
    if client is not None:
        try:
            diagnosis = diagnose_with_llm(ctx, code, client)
        except LLMError as exc:
            notes.append(f"LLM diagnosis failed, fell back to rules: {exc}")
    if diagnosis is None:
        diagnosis = rules_diagnosis(ctx)
    diagnosis.notes.extend(notes)

    patch_check = None
    if diagnosis.patch:
        if repo_root is None:
            diagnosis.notes.append("Patch not checked: no repository given.")
        else:
            patch_check = check_patch(diagnosis.patch, Path(repo_root))
            if not patch_check.ok:
                diagnosis.needs_human = True
                diagnosis.notes += [f"Patch refused: {reason}" for reason in patch_check.reasons]

    diagnosis.action = decide_action(diagnosis, patch_check)
    return Report(diagnosis=diagnosis, context=ctx, code=code, patch_check=patch_check)
