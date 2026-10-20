import json
import time
import redis
from pipeline.validation import DataSanitizationPipeline
from services.agents import TriageAgent, ComplianceCriticAgent
from config.settings import settings
from app.metrics import (
    increment_counter,
    observe_histogram,
    TICKET_RISK_SCORE_DISTRIBUTION,
    TICKET_CATEGORY_TOTAL,
    COMPLIANCE_PASS_TOTAL,
    COMPLIANCE_FAIL_TOTAL,
)

# Tickets at or above this score always wait for a human.
REVIEW_THRESHOLD = 7


class StateMachineOrchestrator:
    def __init__(self):
        self.scrubber = DataSanitizationPipeline()
        self.triage_agent = TriageAgent()
        self.critic_agent = ComplianceCriticAgent()
        # Use settings for Redis configuration consistently
        self.redis_client = redis.Redis(
            host=settings.REDIS_HOST,
            port=settings.REDIS_PORT,
            db=0
        )

    def persist_state(self, state: dict) -> None:
        """Store the final state under the job id and index it by ticket id."""
        ttl = settings.STATE_TTL_SEC
        pipe = self.redis_client.pipeline()
        pipe.set(f"state:{state['job_id']}", json.dumps(state), ex=ttl)
        pipe.set(f"ticket:{state['ticket_id']}", state["job_id"], ex=ttl)
        pipe.execute()

    def execute_workflow(self, raw_job_data: dict, job_id: str = None) -> dict:
        start_time = time.time()
        job_id = job_id or raw_job_data.get("job_id") or raw_job_data["ticket_id"]

        # Step 1: Ingestion & Sanitization
        clean_text = self.scrubber.scrub_pii(raw_job_data["raw_text"])

        # State Initialization
        state = {
            "job_id": job_id,
            "ticket_id": raw_job_data["ticket_id"],
            "sanitized_text": clean_text,
            "risk_score": None,
            "category": "UNCLASSIFIED",
            "triage_verdict": "",
            "critic_feedback": "",
            "audit_passed": None,
            "review_reason": None,
            "human_approval": "PENDING",
            "status": "PROCESSING"
        }

        # Step 2: Triage
        # LLM call counts and durations are recorded inside the agents.
        triage_output = self.triage_agent.analyze(state["sanitized_text"])
        state["risk_score"] = triage_output["risk_score"]
        state["category"] = triage_output.get("category", "UNCLASSIFIED")
        state["triage_verdict"] = triage_output["verdict"]
        if isinstance(state["risk_score"], int):
            observe_histogram(TICKET_RISK_SCORE_DISTRIBUTION, state["risk_score"])
        increment_counter(TICKET_CATEGORY_TOTAL, {"category": state["category"]})

        # Step 3: Critic
        critic_output = self.critic_agent.critique(state["triage_verdict"], state["risk_score"])
        state["critic_feedback"] = critic_output["feedback"]
        state["audit_passed"] = critic_output["audit_passed"]
        if state["audit_passed"]:
            increment_counter(COMPLIANCE_PASS_TOTAL)
        else:
            increment_counter(COMPLIANCE_FAIL_TOTAL)

        # Step 4: Routing. A human decides whenever the agents could not,
        # disagreed, or the score reaches the threshold. Never fail open.
        reasons = []
        if triage_output.get("degraded"):
            reasons.append(f"triage unavailable or invalid: {triage_output.get('error')}")
        elif state["risk_score"] >= REVIEW_THRESHOLD:
            reasons.append(f"risk_score {state['risk_score']} >= {REVIEW_THRESHOLD}")
        if not state["audit_passed"]:
            reasons.append("critic did not pass the audit" + (" (critic unavailable)" if critic_output.get("degraded") else ""))

        if reasons:
            state["status"] = "PAUSED_FOR_REVIEW"
            state["review_reason"] = "; ".join(reasons)
            self.persist_state(state)
            return state

        state["status"] = "COMPLETED"
        # Every outcome is stored: an autonomous close must be auditable too.
        self.persist_state(state)
        # Processing time is observed once, by the worker, around the whole task.
        return state
