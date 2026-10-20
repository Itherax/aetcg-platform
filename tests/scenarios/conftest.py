"""
Fixtures for the scenario suite: a real Redis, a mock LLM, the FastAPI app
and an in-process RQ worker.  Environment variables are set *before* the
application modules are imported because ``config.settings`` reads them at
import time.
"""
import os
import socket

import pytest

# --- environment must be in place before `main` / `worker` are imported -----
_llm_port = socket.socket()
_llm_port.bind(("127.0.0.1", 0))
LLM_PORT = _llm_port.getsockname()[1]
_llm_port.close()

os.environ.setdefault("REDIS_HOST", "127.0.0.1")
os.environ.setdefault("REDIS_PORT", "6379")
os.environ["LLM_ENDPOINT_URL"] = f"http://127.0.0.1:{LLM_PORT}/v1"
os.environ["LLM_ACCESS_TOKEN"] = "scenario-token"
os.environ["EXTERNAL_API_TIMEOUT_SEC"] = "2.0"
os.environ.setdefault("APP_ENV", "development")
os.environ.setdefault("RATE_LIMIT_REQUESTS_PER_MINUTE", "100000")

import redis as redis_lib  # noqa: E402
from rq import Queue, SimpleWorker  # noqa: E402

from tests.scenarios.mock_llm import MockLLM  # noqa: E402


def _redis_available():
    try:
        redis_lib.Redis(host=os.environ["REDIS_HOST"], port=int(os.environ["REDIS_PORT"]), socket_connect_timeout=1).ping()
        return True
    except Exception:
        return False


REDIS_UP = _redis_available()
requires_redis = pytest.mark.skipif(not REDIS_UP, reason="scenario suite needs a live Redis on REDIS_HOST:REDIS_PORT")


@pytest.fixture(scope="session")
def mock_llm():
    server = MockLLM(port=LLM_PORT).start()
    yield server
    server.stop()


@pytest.fixture
def redis_conn():
    if not REDIS_UP:
        pytest.skip("Redis not available")
    conn = redis_lib.Redis(host=os.environ["REDIS_HOST"], port=int(os.environ["REDIS_PORT"]), db=0)
    conn.flushdb()
    yield conn
    conn.flushdb()


@pytest.fixture
def client(redis_conn, mock_llm):
    from fastapi.testclient import TestClient
    import main
    mock_llm.mode = "valid:3"
    mock_llm.requests.clear()
    # The app's rate limiter is in-memory; reset it between scenarios.
    main._request_counts.clear()
    return TestClient(main.app)


@pytest.fixture
def run_worker(redis_conn):
    """Process every queued job in this process (no fork) and return."""
    def _run():
        import worker
        SimpleWorker([Queue("aetcg_jobs", connection=redis_conn)], connection=redis_conn).work(burst=True)
    return _run


def submit(client, ticket_id, text, reporter="analyst@enterprise.com"):
    return client.post("/api/v1/triage", json={"ticket_id": ticket_id, "reporter_email": reporter, "raw_text": text})
