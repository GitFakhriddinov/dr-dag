import io
import json

import pytest

from dagdoctor import llm
from dagdoctor.diagnose import diagnose
from dagdoctor.llm import AnthropicClient, LLMError, ReplayClient, build_prompt, parse_response
from dagdoctor.triage import build_failure_context


class FakeClient:
    def __init__(self, reply: str):
        self.reply = reply
        self.prompts: list[str] = []

    def complete(self, system: str, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.reply


def test_parse_response_accepts_fenced_json():
    reply = 'Here you go:\n```json\n{"root_cause": "x", "fix_summary": "y", "confidence": 3}\n```'
    data = parse_response(reply)
    assert data["confidence"] == 1.0
    assert data["patch"] == ""
    assert data["needs_human"] is True


@pytest.mark.parametrize("reply", ["no json here", '{"root_cause": ""}', "{not json}"])
def test_parse_response_rejects_bad_replies(reply):
    with pytest.raises(LLMError):
        parse_response(reply)


def test_prompt_contains_code_and_no_secrets(read_log, examples):
    report = diagnose(read_log("clickhouse_missing_column.log"), repo_root=examples)
    prompt = build_prompt(report.context, report.code)
    assert "sum(amont)" in prompt
    assert "dags/shop_daily_sales.py" in prompt
    assert "S3cretPass" not in prompt


def test_llm_patch_is_checked_and_accepted(read_log, examples):
    client = ReplayClient(examples / "llm/missing_column.json")
    report = diagnose(read_log("clickhouse_missing_column.log"), repo_root=examples, client=client)
    assert report.diagnosis.source == "llm"
    assert report.diagnosis.action == "patch"
    assert report.patch_check.ok


def test_bad_llm_reply_falls_back_to_rules(read_log, examples):
    report = diagnose(
        read_log("clickhouse_missing_column.log"), repo_root=examples, client=FakeClient("sorry")
    )
    assert report.diagnosis.source == "rules"
    assert report.diagnosis.category == "missing_column"
    assert any("fell back" in note for note in report.diagnosis.notes)


def test_refused_patch_escalates(read_log, examples):
    reply = json.dumps(
        {"root_cause": "x", "fix_summary": "y", "confidence": 0.9, "patch": "--- a/x\n+++ b/x\n"}
    )
    report = diagnose(read_log("clickhouse_missing_column.log"), repo_root=examples, client=FakeClient(reply))
    assert report.diagnosis.action == "escalate"
    assert report.diagnosis.needs_human


def test_anthropic_client_request(monkeypatch):
    captured = {}

    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["headers"] = dict(request.header_items())
        captured["body"] = json.loads(request.data)
        return Response(json.dumps({"content": [{"type": "text", "text": "hello"}]}).encode())

    monkeypatch.setattr(llm.urllib.request, "urlopen", fake_urlopen)
    client = AnthropicClient("test-key", model="test-model")
    assert client.complete("sys", "hi") == "hello"
    assert captured["url"] == llm.API_URL
    assert captured["headers"]["X-api-key"] == "test-key"
    assert captured["body"]["model"] == "test-model"
    assert captured["body"]["messages"] == [{"role": "user", "content": "hi"}]


def test_from_env(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert AnthropicClient.from_env() is None
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setenv("DAGDOCTOR_MODEL", "m")
    assert AnthropicClient.from_env().model == "m"


def test_rules_only_context_has_no_patch(read_log):
    ctx = build_failure_context(read_log("postgres_connection_refused.log"))
    assert ctx.primary.retryable
