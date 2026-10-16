PY ?= python
VENV ?= .venv
VERSION ?= 0.1.0
VCS_REF ?= $(shell git rev-parse --short HEAD 2>/dev/null || echo dev)
IMAGE ?= backend-agnostic-broker:$(VERSION)

.PHONY: help venv install lint fmt typecheck test cov run openapi \
        docker-build docker-smoke compose-config compose-up compose-down clean

help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

venv: ## Create the virtualenv
	$(PY) -m venv $(VENV)

install: ## Install runtime + dev deps into the venv
	. $(VENV)/bin/activate && pip install -U pip && pip install -e ".[dev]"

lint: ## Ruff lint
	. $(VENV)/bin/activate && ruff check broker tests

fmt: ## Ruff autofix + format
	. $(VENV)/bin/activate && ruff check --fix broker tests && ruff format broker tests

typecheck: ## mypy
	. $(VENV)/bin/activate && mypy broker

test: ## Run the test suite
	. $(VENV)/bin/activate && pytest -q

cov: ## Tests with coverage
	. $(VENV)/bin/activate && pytest --cov=broker --cov-report=term-missing

run: ## Run the broker locally (expects registry.yaml + env)
	. $(VENV)/bin/activate && $(PY) -m broker

openapi: ## Regenerate docs/openapi.yaml
	. $(VENV)/bin/activate && PYTHONPATH=. $(PY) scripts/gen_openapi.py

docker-build: ## Build the Docker image
	docker build --build-arg VERSION=$(VERSION) --build-arg VCS_REF=$(VCS_REF) -t $(IMAGE) .

docker-smoke: ## Build + run container smoke test
	. $(VENV)/bin/activate && bash scripts/docker_smoke.sh $(IMAGE)

compose-config: ## Validate docker-compose files
	docker compose -f docker-compose.yml config >/dev/null && echo "base OK"
	docker compose -f docker-compose.yml -f docker-compose.gpu.yml config >/dev/null && echo "gpu OK"

compose-up: ## Start the stack
	docker compose up -d --build

compose-down: ## Stop the stack
	docker compose down -v

clean: ## Remove caches
	rm -rf .pytest_cache .ruff_cache .mypy_cache **/__pycache__ .coverage
