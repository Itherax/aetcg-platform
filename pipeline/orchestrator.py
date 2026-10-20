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
    LLM_API_CALLS_TOTAL,
    LLM_API_DURATION_SECONDS,
    TICKET_PROCESSING_TIME_SECONDS
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
        triage_start = time.time()
        triage_output = self.triage_agent.analyze(state["sanitized_text"])
        triage_time = time.time() - triage_start
        state["risk_score"] = triage_output["risk_score"]
        state["category"] = triage_output.get("category", "UNCLASSIFIED")
        state["triage_verdict"] = triage_output["verdict"]
        observe_histogram(LLM_API_DURATION_SECONDS, triage_time, {"agent_name": "triage"})
        if isinstance(state["risk_score"], int):
            observe_histogram(TICKET_RISK_SCORE_DISTRIBUTION, state["risk_score"])
        increment_counter(TICKET_CATEGORY_TOTAL, {"category": state["category"]})

        # Step 3: Critic
        critic_start = time.time()
        critic_output = self.critic_agent.critique(state["triage_verdict"], state["risk_score"])
        critic_time = time.time() - critic_start
        state["critic_feedback"] = critic_output["feedback"]
        state["audit_passed"] = critic_output["audit_passed"]
        observe_histogram(LLM_API_DURATION_SECONDS, critic_time, {"agent_name": "critic"})
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
        observe_histogram(TICKET_PROCESSING_TIME_SECONDS, time.time() - start_time, {"status": "success"})
        return state
