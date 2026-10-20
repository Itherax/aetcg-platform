"""
Orchestrator routing rules with stubbed agents and a stubbed Redis.
"""
from unittest.mock import Mock, patch

import pytest

from pipeline.orchestrator import StateMachineOrchestrator, REVIEW_THRESHOLD


def make(triage, critic):
    with patch("pipeline.orchestrator.redis.Redis") as redis_cls:
        redis_cls.return_value = Mock()
        orch = StateMachineOrchestrator()
    orch.triage_agent = Mock(analyze=Mock(return_value=triage))
    orch.critic_agent = Mock(critique=Mock(return_value=critic))
    return orch


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
