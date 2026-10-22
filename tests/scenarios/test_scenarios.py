"""
Scenario suite — the Module 8 audit experiments written as executable
expectations of *correct* behaviour.  Every scenario runs the real API, a
real Redis, the real RQ worker (in-process) and a mock LLM whose replies we
control.  A scenario that fails documents a defect; the suite is the
before/after yardstick for the remediation sprint.

Scenarios for defects not yet fixed are marked xfail(strict=True) so the
whole suite stays green on every branch; a fix PR removes the marker for
the scenarios it turns green (strict means an unexpected pass also fails,
so the marker cannot silently go stale).

Run just this suite:   python -m pytest tests/scenarios -q
Tabulated:             python scripts/scenario_suite.py
"""
import time

import pytest
from rq.job import Job

from tests.scenarios.conftest import requires_redis, submit

pytestmark = requires_redis

TOKEN = "SECURE_GOVERNANCE_TOKEN_XYZ"


def outcome(redis_conn, job_id, ticket_id):
    """Find the ticket's final state however the system chose to store it."""
    import json
    for key in (f"state:{job_id}", f"state:{ticket_id}"):
        raw = redis_conn.get(key)
        if raw:
            return json.loads(raw)
    job = Job.fetch(job_id, connection=redis_conn)
    if job.is_failed:
        return {"status": "LOST", "error": (job.exc_info or "").strip().splitlines()[-1:]}
    return job.result or {"status": "UNKNOWN"}


def approve(client, job_id, action="APPROVE", token=TOKEN):
    return client.put(f"/api/v1/approve/{job_id}", params={"action": action, "supervisor_token": token})


# ---------------------------------------------------------------- L2 ----
def test_s01_state_reachable_by_returned_id(client, redis_conn, run_worker):
    """S01 (F02, L2): the id returned by POST /triage can be used to read the ticket's state, including a COMPLETED one."""
    r = submit(client, "TCK-S01", "User asks for a password reset on the finance app.")
    assert r.status_code == 202
    job_id = r.json()["job_id"]
    run_worker()
    state = client.get(f"/api/v1/state/{job_id}")
    assert state.status_code == 200, state.text
    assert state.json()["status"] == "COMPLETED"


def test_s01b_state_also_reachable_by_ticket_id_and_expires(client, redis_conn, run_worker):
    """S01b (F02, L2): the ticket_id still resolves to the same state, and stored state carries a retention TTL."""
    r = submit(client, "TCK-S01B", "Routine request")
    job_id = r.json()["job_id"]
    run_worker()
    by_ticket = client.get("/api/v1/state/TCK-S01B")
    assert by_ticket.status_code == 200 and by_ticket.json()["job_id"] == job_id
    assert 0 < redis_conn.ttl(f"state:{job_id}") <= 7 * 24 * 3600


# ---------------------------------------------------------------- L5 ----
def test_s02_llm_outage_does_not_auto_complete(client, redis_conn, run_worker, mock_llm):
    """S02 (F01, L5): when the model returns HTTP 500 the ticket is paused for a human, not completed."""
    mock_llm.mode = "fail"
    r = submit(client, "TCK-S02", "Ransomware note found on the file server, admin creds leaked")
    run_worker()
    out = outcome(redis_conn, r.json()["job_id"], "TCK-S02")
    assert out["status"] == "PAUSED_FOR_REVIEW", out


def test_s03_fenced_json_is_parsed(client, redis_conn, run_worker, mock_llm):
    """S03 (F01, L5): a reply wrapped in a ```json fence is parsed; a score of 9 pauses the ticket."""
    mock_llm.mode = "fenced:9"
    r = submit(client, "TCK-S03", "Domain admin account compromised, lateral movement observed")
    run_worker()
    out = outcome(redis_conn, r.json()["job_id"], "TCK-S03")
    assert out["status"] == "PAUSED_FOR_REVIEW" and out.get("risk_score") == 9, out


def test_s04_missing_score_is_not_lost(client, redis_conn, run_worker, mock_llm):
    """S04 (F06, L5): a reply without risk_score pauses the ticket instead of crashing the job."""
    mock_llm.mode = "noscore:9"
    r = submit(client, "TCK-S04", "Data exfiltration to unknown host")
    run_worker()
    out = outcome(redis_conn, r.json()["job_id"], "TCK-S04")
    assert out["status"] == "PAUSED_FOR_REVIEW", out


def test_s05_string_score_is_coerced(client, redis_conn, run_worker, mock_llm):
    """S05 (F06, L5): risk_score sent as the string "9" is coerced to 9 and the ticket is paused."""
    mock_llm.mode = "strscore:9"
    r = submit(client, "TCK-S05", "Insider threat flagged")
    run_worker()
    out = outcome(redis_conn, r.json()["job_id"], "TCK-S05")
    assert out["status"] == "PAUSED_FOR_REVIEW" and out.get("risk_score") == 9, out


def test_s06_out_of_range_score_is_rejected(client, redis_conn, run_worker, mock_llm):
    """S06 (F06, L5): a score of 42 is not stored as-is; the ticket is paused and flagged UNCLASSIFIED."""
    mock_llm.mode = "valid:42"
    r = submit(client, "TCK-S06", "Critical breach")
    run_worker()
    out = outcome(redis_conn, r.json()["job_id"], "TCK-S06")
    assert out["status"] == "PAUSED_FOR_REVIEW" and out.get("risk_score") != 42, out


def test_s07_critic_disagreement_pauses(client, redis_conn, run_worker, mock_llm):
    """S07 (F07, L5/L7): when the critic sets audit_passed=false on a low-scored ticket, the ticket is paused."""
    mock_llm.mode = "critic_disagree"
    r = submit(client, "TCK-S07", "Password reset request from CFO assistant")
    run_worker()
    out = outcome(redis_conn, r.json()["job_id"], "TCK-S07")
    assert out["status"] == "PAUSED_FOR_REVIEW", out


# ---------------------------------------------------------------- L7 ----
def _paused_ticket(client, run_worker, mock_llm, ticket_id):
    mock_llm.mode = "valid:9"
    r = submit(client, ticket_id, "Suspicious login and privilege escalation on finance app")
    run_worker()
    return r.json()["job_id"]


def test_s08_second_approval_is_rejected(client, redis_conn, run_worker, mock_llm):
    """S08 (F05, L7): approving an already-approved ticket returns 409 and does not rewrite the decision."""
    job_id = _paused_ticket(client, run_worker, mock_llm, "TCK-S08")
    first = approve(client, job_id)
    assert first.status_code == 200, first.text
    second = approve(client, job_id)
    assert second.status_code == 409, second.text


def test_s09_reject_after_approve_is_rejected(client, redis_conn, run_worker, mock_llm):
    """S09 (F05, L7): a decision cannot be flipped from APPROVE to REJECT through the gate."""
    job_id = _paused_ticket(client, run_worker, mock_llm, "TCK-S09")
    assert approve(client, job_id).status_code == 200
    flipped = approve(client, job_id, action="REJECT")
    assert flipped.status_code == 409, flipped.text
    assert client.get(f"/api/v1/state/{job_id}").json()["status"] == "COMPLETED"


def test_s10_cannot_approve_unpaused_ticket(client, redis_conn, run_worker, mock_llm):
    """S10 (F05, L7): the gate only acts on tickets that are PAUSED_FOR_REVIEW; a COMPLETED ticket gets 409."""
    r = submit(client, "TCK-S10", "Routine request")
    run_worker()
    resp = approve(client, r.json()["job_id"], action="REJECT")
    assert resp.status_code == 409, resp.text


def test_s11_resubmitting_decided_ticket_is_rejected(client, redis_conn, run_worker, mock_llm):
    """S11 (F03, L7): re-POSTing a ticket_id that already has a decision returns 409 and leaves the decision intact."""
    job_id = _paused_ticket(client, run_worker, mock_llm, "TCK-S11")
    assert approve(client, job_id, action="REJECT").status_code == 200
    again = submit(client, "TCK-S11", "Totally new content under an old ticket id")
    assert again.status_code == 409, again.text
    run_worker()
    assert client.get(f"/api/v1/state/{job_id}").json()["status"] == "REJECTED"


# ---------------------------------------------------------------- L8 ----
def test_s12_heartbeat_stores_timestamp_with_short_ttl(redis_conn):
    """S12 (F12, L8): one heartbeat write stores the current epoch second under worker:health_signal with a ~20 s TTL."""
    from worker import write_health_signal
    write_health_signal(redis_conn)
    value = int(redis_conn.get("worker:health_signal"))
    assert abs(value - time.time()) < 5, value
    assert 0 < redis_conn.ttl("worker:health_signal") <= 20


@pytest.mark.xfail(strict=True, reason="F22 unfixed: metrics double-counted")
def test_s13_cache_miss_counted_once_per_request(client, redis_conn):
    """S13 (F22, L8): one POST that misses the cache increments cache_misses_total by exactly 1."""
    from app.metrics import CACHE_MISSES_TOTAL
    before = CACHE_MISSES_TOTAL.labels(cache_type="redis")._value.get()
    submit(client, "TCK-S13", "Metric counting check")
    after = CACHE_MISSES_TOTAL.labels(cache_type="redis")._value.get()
    assert after - before == 1, (before, after)


# ---------------------------------------------------------------- L3 ----
@pytest.mark.xfail(strict=True, reason="F19 unfixed: permissive CORS")
def test_s14_cors_does_not_echo_unknown_origin(client):
    """S14 (F19, L3): a preflight from an origin not on the allow-list is not granted credentials."""
    resp = client.options("/api/v1/triage", headers={"Origin": "https://evil.example",
                                                     "Access-Control-Request-Method": "POST"})
    granted = resp.headers.get("access-control-allow-origin")
    assert granted not in ("https://evil.example", "*"), dict(resp.headers)


# ---------------------------------------------------------------- L4 ----
@pytest.mark.xfail(strict=True, reason="F08 unfixed: PII queued before scrub")
def test_s15_queued_payload_contains_no_pii(client, redis_conn):
    """S15 (F08, L4): the payload written to the RQ job is already scrubbed (no raw SSN in Redis)."""
    r = submit(client, "TCK-S15", "Customer SSN 123-45-6789 was emailed to bob@gmail.com")
    job = Job.fetch(r.json()["job_id"], connection=redis_conn)
    raw_text = job.args[0]["raw_text"]
    assert "123-45-6789" not in raw_text and "bob@gmail.com" not in raw_text, raw_text
