"""Ask an LLM for a root cause and a minimal patch.

Dr. Dag talks to the Anthropic Messages API with nothing but the standard
library, so the package has no runtime dependencies. Anything that implements
``complete(system, prompt) -> str`` can be plugged in instead (see
:class:`ReplayClient`, used for demos and tests).
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Protocol

from .models import CodeContext, Diagnosis, FailureContext
from .redact import redact_code

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"
DEFAULT_MODEL = "claude-sonnet-5-5"

SYSTEM_PROMPT = """\
You are Dr. Dag, an on-call data engineer who diagnoses failed Apache Airflow tasks.

You receive a failed task's log excerpt, a rule-based triage, and the relevant source
code (Python DAG files and SQL). Find the root cause and, only when you are confident,
propose the smallest possible fix.

Rules:
- Base every claim on the log and code you were given. Never invent table names,
  columns or APIs that do not appear in them.
- Code is shown with line numbers and a "|" separator; these are NOT part of the file.
  ">>" marks the failing line.
- A patch must be a unified diff relative to the repository root, using "a/" and "b/"
  prefixes, touching only files you were shown. Keep it minimal: fix the bug, nothing else.
- Never put credentials, tokens or hostnames in a patch.
- If the cause is infrastructure (network, memory, permissions, a missing connection),
  or you are not sure, return an empty patch and set needs_human to true.

Reply with one JSON object and nothing else:
{"category": str, "root_cause": str, "confidence": float between 0 and 1,
 "fix_summary": str, "needs_human": bool, "patch": str}
"""


class LLMError(RuntimeError):
    """The LLM call failed or returned something unusable."""


class Completer(Protocol):
    def complete(self, system: str, prompt: str) -> str: ...


class AnthropicClient:
    """Minimal Messages API client built on urllib."""

    def __init__(
        self,
        api_key: str,
        model: str = DEFAULT_MODEL,
        url: str = API_URL,
        timeout: float = 120,
        max_tokens: int = 4000,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.url = url
        self.timeout = timeout
        self.max_tokens = max_tokens

    @classmethod
    def from_env(cls) -> AnthropicClient | None:
        key = os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            return None
        return cls(key, model=os.environ.get("DAGDOCTOR_MODEL", DEFAULT_MODEL))

    def complete(self, system: str, prompt: str) -> str:
        body = json.dumps(
            {
                "model": self.model,
                "max_tokens": self.max_tokens,
                "system": system,
                "messages": [{"role": "user", "content": prompt}],
            }
        ).encode()
        request = urllib.request.Request(
            self.url,
            data=body,
            method="POST",
            headers={
                "x-api-key": self.api_key,
                "anthropic-version": API_VERSION,
                "content-type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                data = json.load(response)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:500]
            raise LLMError(f"API returned HTTP {exc.code}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise LLMError(f"API request failed: {exc}") from exc
        text = "".join(
            block.get("text", "") for block in data.get("content", []) if block.get("type") == "text"
        )
        if not text:
            raise LLMError("API response contained no text")
        return text


class ReplayClient:
    """Returns a saved response instead of calling an API (demos and tests)."""

    def __init__(self, path: Path) -> None:
        self.text = Path(path).read_text()

    def complete(self, system: str, prompt: str) -> str:
        return self.text


def build_prompt(ctx: FailureContext, code: CodeContext | None) -> str:
    """Assemble the user message. Everything in it is already redacted."""
    parts = [
        "## Failure",
        f"DAG: {ctx.dag_id or 'unknown'}",
        f"Task: {ctx.task_id or 'unknown'}",
        f"Exception: {ctx.exception_type or 'unknown'}: {ctx.exception_message or ''}".rstrip(),
        "",
        "## Rule-based triage",
    ]
    if ctx.triage:
        parts += [f"- {t.category}: {t.title} (evidence: {t.evidence})" for t in ctx.triage]
    else:
        parts.append("- no rule matched")
    parts += ["", "## Log excerpt (redacted)", "```", ctx.log_excerpt, "```"]
    if code:
        where = f"{code.path}" + (f", failing line {code.line}" if code.line else "")
        if code.function:
            where += f", function {code.function}()"
        parts += ["", f"## Source: {where}", "```", redact_code(code.snippet), "```"]
        for path, sql in code.sql_files.items():
            parts += ["", f"## SQL file: {path}", "```sql", redact_code(sql), "```"]
    else:
        parts += ["", "## Source", "Not available; diagnose from the log only and do not patch."]
    return "\n".join(parts)


def parse_response(text: str) -> dict:
    """Pull the JSON object out of the model's reply and validate it."""
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise LLMError("no JSON object in the response")
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise LLMError(f"response is not valid JSON: {exc}") from exc
    for key in ("root_cause", "fix_summary"):
        if not isinstance(data.get(key), str) or not data[key].strip():
            raise LLMError(f"response is missing {key!r}")
    try:
        confidence = float(data.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    patch = data.get("patch") or ""
    if not isinstance(patch, str):
        patch = ""
    if patch and not patch.endswith("\n"):
        patch += "\n"
    return {
        "category": str(data.get("category") or "unknown"),
        "root_cause": data["root_cause"].strip(),
        "confidence": max(0.0, min(1.0, confidence)),
        "fix_summary": data["fix_summary"].strip(),
        "needs_human": bool(data.get("needs_human", not patch)),
        "patch": patch,
    }


def diagnose_with_llm(ctx: FailureContext, code: CodeContext | None, client: Completer) -> Diagnosis:
    data = parse_response(client.complete(SYSTEM_PROMPT, build_prompt(ctx, code)))
    primary = ctx.primary
    return Diagnosis(
        dag_id=ctx.dag_id,
        task_id=ctx.task_id,
        category=data["category"],
        root_cause=data["root_cause"],
        confidence=data["confidence"],
        fix_summary=data["fix_summary"],
        retryable=bool(primary and primary.retryable),
        needs_human=data["needs_human"],
        source="llm",
        patch=data["patch"] if code else "",
    )
