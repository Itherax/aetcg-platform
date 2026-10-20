# Single-command operations for AETCG.
.PHONY: up down logs test scenarios ci

up:            ## build and start API + worker + Redis
	docker compose up --build

down:          ## stop and remove containers (Redis data volume is kept)
	docker compose down

logs:
	docker compose logs -f --tail=100

test:          ## full suite: unit tests + scenario suite (needs Redis on REDIS_HOST:REDIS_PORT)
	python -m pytest -q

scenarios:     ## before/after scorecard
	python scripts/scenario_suite.py

ci: test
