"""
Deployment contract: one command (`docker compose up --build`) must start
the API, a *separately supervised* worker, and a persistent Redis.
Checked by parsing the compose file, so it runs without a Docker daemon.
"""
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
COMPOSE = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
SERVICES = COMPOSE["services"]


def test_worker_is_its_own_supervised_service():
    assert "worker" in SERVICES, "worker must not be a background shell job inside the API container"
    worker = SERVICES["worker"]
    assert "worker.py" in " ".join(worker["command"]) if isinstance(worker["command"], list) else "worker.py" in worker["command"]
    assert worker.get("restart") in ("always", "unless-stopped", "on-failure")


def test_api_container_does_not_also_run_the_worker():
    cmd = SERVICES["web_api"]["command"]
    cmd = " ".join(cmd) if isinstance(cmd, list) else cmd
    assert "worker.py" not in cmd and "&" not in cmd


def test_redis_is_healthchecked_and_persistent():
    redis = SERVICES["queue_broker"]
    assert "healthcheck" in redis
    assert redis.get("volumes"), "Redis data must survive a container recreate"
    assert "ports" not in redis, "Redis stays private to the compose network"


def test_api_and_worker_wait_for_healthy_redis():
    for name in ("web_api", "worker"):
        dep = SERVICES[name]["depends_on"]
        assert dep["queue_broker"]["condition"] == "service_healthy", name


def test_dockerignore_keeps_secrets_out_of_the_image():
    ignore = (ROOT / ".dockerignore").read_text().split()
    assert ".env" in ignore and ".git" in ignore
