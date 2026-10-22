"""
Orchestrator routing rules with stubbed agents and a stubbed Redis.
"""
from unittest.mock import Mock, patch

import pytest

from pipeline.orchestrator import StateMachineOrchestrator, REVIEW_THRESHOLD


def make(triage, critic):
    with patch("pipeline.orchestrator.redis.Redis", return_value=Mock()), \
         patch("pipeline.orchestrator.TriageAgent", return_value=Mock(analyze=Mock(return_value=triage))), \
         patch("pipeline.orchestrator.ComplianceCriticAgent", return_value=Mock(critique=Mock(return_value=critic))):
        return StateMachineOrchestrator()


OK_CRITIC = {"audit_passed": True, "feedback": "routine", "degraded": False}
PAYLOAD = {"ticket_id": "TCK-U", "reporter_email": "a@enterprise.com", "raw_text": "text"}


def test_low_score_completes():
    orch = make({"risk_score": 2, "category": "ACCESS", "verdict": "v", "degraded": False, "error": None}, OK_CRITIC)
    state = orch.execute_workflow(PAYLOAD)
    assert state["status"] == "COMPLETED" and state["audit_passed"] is True


def test_threshold_pauses():
    orch = make({"risk_score": REVIEW_THRESHOLD, "category": "X", "verdict": "v", "degraded": False, "error": None},
                {"audit_passed": False, "feedback": "high", "degraded": False})
    assert orch.execute_workflow(PAYLOAD)["status"] == "PAUSED_FOR_REVIEW"


def test_degraded_triage_pauses_and_records_reason():
    orch = make({"risk_score": None, "category": "UNCLASSIFIED", "verdict": "Triage failed", "degraded": True,
                 "error": "boom"}, OK_CRITIC)
    state = orch.execute_workflow(PAYLOAD)
    assert state["status"] == "PAUSED_FOR_REVIEW"
    assert state["risk_score"] is None and state["category"] == "UNCLASSIFIED"
    assert "boom" in state["review_reason"]


def test_critic_veto_pauses_low_score():
    orch = make({"risk_score": 2, "category": "ACCESS", "verdict": "v", "degraded": False, "error": None},
                {"audit_passed": False, "feedback": "disagree", "degraded": False})
    state = orch.execute_workflow(PAYLOAD)
    assert state["status"] == "PAUSED_FOR_REVIEW" and state["audit_passed"] is False
