PYTHON ?= python3
NPM ?= npm
COMPOSE ?= docker compose
PYTEST_ARGS ?=

.PHONY: up down logs migrate seed test unit-test integration-test lint \
	frontend-install frontend-typecheck frontend-build frontend-check \
	check compose-config require-database-url

up:
	$(COMPOSE) up --build

down:
	$(COMPOSE) down

logs:
	$(COMPOSE) logs -f api

migrate:
	@set -eu; \
	$(COMPOSE) run --rm db-bootstrap; \
	trap '$(COMPOSE) run --rm --no-deps db-finalize' EXIT; \
	$(COMPOSE) run --rm --no-deps migrate; \
	$(COMPOSE) run --rm --no-deps db-grants; \
	$(COMPOSE) run --rm --no-deps db-finalize; \
	trap - EXIT

seed:
	@set -eu; \
	$(COMPOSE) run --rm db-bootstrap; \
	trap '$(COMPOSE) run --rm --no-deps db-finalize' EXIT; \
	$(COMPOSE) run --rm --no-deps migrate; \
	$(COMPOSE) run --rm --no-deps db-grants; \
	$(COMPOSE) run --rm --no-deps seed; \
	$(COMPOSE) run --rm --no-deps db-finalize; \
	trap - EXIT

require-database-url:
	@test -n "$${DATABASE_URL:-}" || { \
		echo "DATABASE_URL is required; point it at a dedicated disposable test database." >&2; \
		exit 2; \
	}

test: require-database-url
	$(PYTHON) -m alembic upgrade head
	$(PYTHON) -m pytest $(PYTEST_ARGS)

unit-test:
	$(PYTHON) -m pytest -m "not integration" $(PYTEST_ARGS)

integration-test: require-database-url
	$(PYTHON) -m alembic upgrade head
	$(PYTHON) -m pytest -m integration $(PYTEST_ARGS)

lint:
	$(PYTHON) -m ruff check .
	$(PYTHON) -m ruff format --check .

frontend-install:
	cd frontend && $(NPM) ci

frontend-typecheck:
	cd frontend && $(NPM) run typecheck

frontend-build:
	cd frontend && $(NPM) run build

frontend-check: frontend-typecheck frontend-build

check: lint unit-test frontend-check

compose-config:
	$(COMPOSE) config --quiet
