.PHONY: help install test lint index eval up down logs smoke loadtest

PYTHON ?= python3
PYTHONPATH := src

help:
	@printf '%s\n' \
	  'make install     install the package with dev extras' \
	  'make test        run the full pytest suite' \
	  'make lint        run ruff' \
	  'make index       build the local retrieval index' \
	  'make eval        run the golden-set evaluation for v1 and v2' \
	  'make up          build and start the Compose stack' \
	  'make down        stop the Compose stack (keeps volumes)' \
	  'make logs        follow api and worker logs' \
	  'make smoke       submit a synthetic request and poll it' \
	  'make loadtest    run the Compose load-test profile'

install:
	$(PYTHON) -m pip install -e '.[dev]'

test:
	PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m pytest -q

lint:
	$(PYTHON) -m ruff check src tests evals loadtest services scripts

index:
	PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m compliance.cli index build

eval:
	PYTHONPATH=$(PYTHONPATH) $(PYTHON) evals/run_evals.py --variant both

up:
	docker compose up -d --build
	@printf 'API: http://localhost:8000  Grafana: http://localhost:3000\n'

down:
	docker compose down

logs:
	docker compose logs -f api worker

smoke:
	@set -e; \
	RUN_ID=$$(curl -sS -X POST http://localhost:8000/v1/advisories \
	  -H 'content-type: application/json' \
	  -H 'X-Client-Id: make-smoke' \
	  -d '{"question":"An employee resigned after 3 years. Is any gratuity payable?","establishment":{"establishment_type":"it_services","employee_count":120},"employment":{"worker_type":"permanent","event":"resignation","completed_service_months":36,"monthly_wage_inr":45000},"requester":{"client_id":"make-smoke","role":"hr"},"consent":true}' \
	  | $(PYTHON) -c 'import json,sys; print(json.load(sys.stdin)["run_id"])'); \
	echo "run_id=$$RUN_ID"; \
	while :; do \
	  BODY=$$(curl -sS "http://localhost:8000/v1/advisories/$$RUN_ID"); \
	  STATUS=$$(printf '%s' "$$BODY" | $(PYTHON) -c 'import json,sys; print(json.load(sys.stdin)["status"])'); \
	  case "$$STATUS" in completed|escalated|failed) printf '%s\n' "$$BODY" | $(PYTHON) -m json.tool; break;; esac; \
	  sleep 0.5; \
	done

loadtest:
	docker compose --profile loadtest build locust
	docker compose --profile loadtest run --rm locust
