"""
Unit tests for the triage and critic agents with a fake OpenAI client.
No network, no Redis: these pin down how model output is parsed and how
failures are reported to the orchestrator.
"""
import json
from unittest.mock import Mock

import pytest

from services.agents import TriageAgent, ComplianceCriticAgent, TriageOutput, CriticOutput, ModelOutputError


def fake_client(content=None, exc=None):
    client = Mock()
    if exc is not None:
        client.chat.completions.create.side_effect = exc
    else:
        msg = Mock()
        msg.content = content
        choice = Mock()
        choice.message = msg
        resp = Mock()
        resp.choices = [choice]
        client.chat.completions.create.return_value = resp
    return client


# --- parsing -------------------------------------------------------------
def test_triage_parses_plain_json():
    out = TriageAgent.parse_analysis_result(json.dumps({"risk_score": 4, "category": "ACCESS", "verdict": "ok"}))
    assert out == TriageOutput(risk_score=4, category="ACCESS", verdict="ok")


def test_triage_parses_fenced_json():
    fenced = "```json\n" + json.dumps({"risk_score": 9, "category": "PRIVESC", "verdict": "bad"}) + "\n```"
    assert TriageAgent.parse_analysis_result(fenced).risk_score == 9


def test_triage_coerces_string_score():
    assert TriageAgent.parse_analysis_result('{"risk_score": "7", "category": "X", "verdict": "v"}').risk_score == 7


@pytest.mark.parametrize("raw", [
    '{"risk": 9, "category": "X", "verdict": "v"}',          # missing key
    '{"risk_score": 42, "category": "X", "verdict": "v"}',   # out of range
    '{"risk_score": "high", "category": "X", "verdict": "v"}',  # not a number
    'not json at all',
])
def test_triage_rejects_invalid_output(raw):
    with pytest.raises(ModelOutputError):
        TriageAgent.parse_analysis_result(raw)


def test_critic_parses_fenced_and_validates_bool():
    out = ComplianceCriticAgent.parse_critique_result("```json\n{\"audit_passed\": false, \"feedback\": \"no\"}\n```")
    assert out == CriticOutput(audit_passed=False, feedback="no")
    with pytest.raises(ModelOutputError):
        ComplianceCriticAgent.parse_critique_result('{"audit_passed": "maybe", "feedback": "x"}')


# --- failure reporting ---------------------------------------------------
def test_triage_failure_is_reported_not_scored():
    agent = TriageAgent(client=fake_client(exc=RuntimeError("endpoint down")))
    out = agent.analyze("ticket text")
    assert out["degraded"] is True
    assert out["risk_score"] is None
    assert out["category"] == "UNCLASSIFIED"
    assert "endpoint down" in out["error"]


def test_triage_invalid_output_is_reported_not_scored():
    agent = TriageAgent(client=fake_client(content='{"risk_score": 42, "category": "X", "verdict": "v"}'))
    out = agent.analyze("ticket text")
    assert out["degraded"] is True and out["risk_score"] is None


def test_triage_success_is_not_degraded():
    agent = TriageAgent(client=fake_client(content='{"risk_score": 2, "category": "ACCESS", "verdict": "routine"}'))
    out = agent.analyze("ticket text")
    assert out == {"risk_score": 2, "category": "ACCESS", "verdict": "routine", "degraded": False, "error": None}


def test_critic_failure_fails_safe_for_any_score():
    agent = ComplianceCriticAgent(client=fake_client(exc=RuntimeError("down")))
    assert agent.critique("v", 3)["audit_passed"] is False
    assert agent.critique("v", 9)["audit_passed"] is False
    assert agent.critique("v", 3)["degraded"] is True
