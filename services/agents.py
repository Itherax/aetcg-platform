"""
AI Agent Services for AETCG Platform

This module contains the AI agent implementations for triage and compliance analysis.
Each agent interacts with a configurable LLM endpoint to perform specialized analysis tasks.
"""
import json
import logging
import re
import time
from typing import Dict, Any, Optional
from openai import OpenAI
from pydantic import BaseModel, Field, ValidationError, field_validator
from config.settings import settings
from app.metrics import (
    LLM_API_CALLS_TOTAL,
    LLM_API_DURATION_SECONDS,
    LLM_API_TOKEN_USAGE,
    increment_counter,
    observe_histogram,
)

logger = logging.getLogger("aetcg_agents")

_FENCE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL)


class ModelOutputError(ValueError):
    """The model replied, but not in a shape the pipeline can trust."""


class TriageOutput(BaseModel):
    risk_score: int = Field(ge=1, le=10)
    category: str = "UNCLASSIFIED"
    verdict: str = ""

    @field_validator("risk_score", mode="before")
    @classmethod
    def coerce_score(cls, v):
        # Models sometimes quote numbers ("7"); accept that, reject words.
        if isinstance(v, str) and v.strip().lstrip("-").isdigit():
            return int(v.strip())
        return v


class CriticOutput(BaseModel):
    audit_passed: bool
    feedback: str = ""

    @field_validator("audit_passed", mode="before")
    @classmethod
    def strict_bool(cls, v):
        if isinstance(v, bool):
            return v
        raise ValueError("audit_passed must be a JSON boolean")


def _parse_json_reply(raw: str, model: type[BaseModel]) -> BaseModel:
    """Strip an optional ```json fence, parse, validate. Never returns garbage."""
    if not isinstance(raw, str):
        raise ModelOutputError(f"expected string content, got {type(raw).__name__}")
    text = raw.strip()
    fenced = _FENCE.match(text)
    if fenced:
        text = fenced.group(1)
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise ModelOutputError(f"reply is not JSON: {e}") from e
    if not isinstance(data, dict):
        raise ModelOutputError("reply JSON is not an object")
    try:
        return model.model_validate(data)
    except ValidationError as e:
        raise ModelOutputError(f"reply failed validation: {e.errors()[0]['msg']} at {e.errors()[0]['loc']}") from e


class TriageAgent:
    """
    Analyzes IT tickets to produce risk scores, categories, and reasoning.

    The agent evaluates ticket content and returns a structured assessment
    including risk level (1-10), category classification, and explanatory verdict.
    """

    def __init__(self, client: Optional[OpenAI] = None):
        """
        Initialize the Triage Agent with optional OpenAI client.

        Args:
            client: Pre-configured OpenAI client. If None, creates new client
                   using settings from configuration.
        """
        # Point the client to your custom endpoint
        self.client = client or OpenAI(
            base_url=settings.LLM_ENDPOINT_URL,
            api_key=settings.LLM_ACCESS_TOKEN
        )
        self.system_prompt = """
        You are an enterprise IT triage analyzer.
        Read the IT ticket and return ONLY a valid json object with three keys:
        - "risk_score" (integer 1-10)
        - "category" (string)
        - "verdict" (string explaining your reasoning)
        """

    def analyze(self, target_text: str) -> Dict[str, Any]:
        """
        Analyze IT ticket text to produce risk assessment.

        Args:
            target_text: The sanitized ticket text to analyze

        Returns:
            Dictionary containing:
                - risk_score: Integer from 1-10 indicating risk level
                - category: String classifying the ticket type
                - verdict: String explaining the reasoning behind the assessment

        Note:
            On failure or invalid output, returns risk_score None with
            degraded=True so the orchestrator routes the ticket to a human.
        """
        start_time = time.time()
        try:
            response = self.client.chat.completions.create(
                model="gpt-5.4-nano",  # Update this if your endpoint requires a specific model name
                # response_format={ "type": "json_object" },
                messages=[
                    {"role": "system", "content": self.system_prompt},
                    {"role": "user", "content": target_text}
                ],
                timeout=settings.EXTERNAL_API_TIMEOUT_SEC
            )
            raw_result = response.choices[0].message.content
            if not isinstance(raw_result, str):
                raise ValueError("Triage Agent returned empty or non-string content")

            # Record successful LLM API call metrics
            call_duration = time.time() - start_time
            increment_counter(LLM_API_CALLS_TOTAL, {"agent_name": "triage", "status": "success"})
            observe_histogram(LLM_API_DURATION_SECONDS, call_duration, {"agent_name": "triage"})

            # Try to extract token usage if available (this depends on the LLM provider)
            # For now, we'll skip token usage as it's not consistently available across providers
            # In a real implementation, you'd extract this from response.usage if available

            logger.debug(f"Triage Agent Result: {raw_result}")
            parsed = self.parse_analysis_result(raw_result)
            return {**parsed.model_dump(), "degraded": False, "error": None}

        except Exception as e:
            # Record failed LLM API call metrics
            call_duration = time.time() - start_time
            increment_counter(LLM_API_CALLS_TOTAL, {"agent_name": "triage", "status": "failure"})
            observe_histogram(LLM_API_DURATION_SECONDS, call_duration, {"agent_name": "triage"})

            kind = "invalid model output" if isinstance(e, ModelOutputError) else "LLM call failed"
            logger.error(f"Triage {kind}: {str(e)}. Ticket will be routed to human review.")
            # Fail CLOSED: no score means a human decides, never a default score.
            return {
                "risk_score": None,
                "category": "UNCLASSIFIED",
                "verdict": f"Triage unavailable ({kind}); human review required.",
                "degraded": True,
                "error": str(e),
            }

    @staticmethod
    def parse_analysis_result(raw_result: str) -> TriageOutput:
        """
        Parse and validate the raw LLM reply.

        Accepts plain JSON or JSON inside a ```json fence; coerces a quoted
        integer score. Raises ModelOutputError for anything else (missing
        keys, out-of-range score, non-numeric score, non-JSON text).
        """
        return _parse_json_reply(raw_result, TriageOutput)


class ComplianceCriticAgent:
    """
    Reviews triage agent output for governance compliance.

    The agent evaluates triage results against governance rules to determine
    if human review is required and provides feedback on the assessment.
    """

    def __init__(self, client: Optional[OpenAI] = None):
        """
        Initialize the Compliance Critic Agent with optional OpenAI client.

        Args:
            client: Pre-configured OpenAI client. If None, creates new client
                   using settings from configuration.
        """
        # Point the client to your custom endpoint
        self.client = client or OpenAI(
            base_url=settings.LLM_ENDPOINT_URL,
            api_key=settings.LLM_ACCESS_TOKEN
        )
        self.system_prompt = """
        You are a senior enterprise risk auditor.
        Review the Triage Agent's 'risk_score' and 'verdict'.

        Governance Rules:
        1. If risk_score >= 7, set audit_passed to false.
        2. If risk_score < 7, evaluate verdict logic. If routine, set audit_passed to true.

        Return ONLY a valid json object with exactly two keys:
        - "audit_passed" (boolean)
        - "feedback" (string)
        """

    def critique(self, verdict_text: str, risk_score: int) -> Dict[str, Any]:
        """
        Review triage agent output for compliance governance.

        Args:
            verdict_text: The verdict string from TriageAgent analysis
            risk_score: The risk score integer (1-10) from TriageAgent analysis

        Returns:
            Dictionary containing:
                - audit_passed: Boolean indicating if governance review passed
                - feedback: String explaining the compliance decision

        Note:
            On failure or invalid output, returns audit_passed False with
            degraded=True: an unreviewed verdict never counts as a pass.
        """
        start_time = time.time()
        try:
            user_content = json.dumps({
                "risk_score": risk_score,
                "triage_verdict": verdict_text
            })

            response = self.client.chat.completions.create(
                model="gpt-5.4-nano",  # Update to match your endpoint's model
                # response_format={ "type": "json_object" },
                messages=[
                    {"role": "system", "content": self.system_prompt},
                    {"role": "user", "content": user_content}
                ],
                timeout=settings.EXTERNAL_API_TIMEOUT_SEC
            )
            raw_result = response.choices[0].message.content
            if not isinstance(raw_result, str):
                raise ValueError("Critic Agent returned empty or non-string content")

            # Record successful LLM API call metrics
            call_duration = time.time() - start_time
            increment_counter(LLM_API_CALLS_TOTAL, {"agent_name": "critic", "status": "success"})
            observe_histogram(LLM_API_DURATION_SECONDS, call_duration, {"agent_name": "critic"})

            # Try to extract token usage if available (this depends on the LLM provider)
            # For now, we'll skip token usage as it's not consistently available across providers
            # In a real implementation, you'd extract this from response.usage if available

            parsed = self.parse_critique_result(raw_result)
            return {**parsed.model_dump(), "degraded": False}

        except Exception as e:
            # Record failed LLM API call metrics
            call_duration = time.time() - start_time
            increment_counter(LLM_API_CALLS_TOTAL, {"agent_name": "critic", "status": "failure"})
            observe_histogram(LLM_API_DURATION_SECONDS, call_duration, {"agent_name": "critic"})

            logger.error(f"Critic Agent call failed or returned invalid output: {str(e)}. Failing safe.")
            # Fail CLOSED regardless of score: an unreviewed verdict is not a pass.
            return {"audit_passed": False,
                    "feedback": "FALLBACK TRIGGERED: critic unavailable; human review mandated.",
                    "degraded": True}

    @staticmethod
    def parse_critique_result(raw_result: str) -> CriticOutput:
        """
        Parse and validate the critic's reply (plain or fenced JSON);
        audit_passed must be a real boolean. Raises ModelOutputError otherwise.
        """
        return _parse_json_reply(raw_result, CriticOutput)

