.PHONY: up down logs evals idp idp-down idp-e2e policy test test-e2e test-live lint fmt reload shell install lock migrate key prices prune-usage restore-drill admin-key

up:
	docker compose up -d --build

down:
	docker compose down

logs:
	docker compose logs -f gateway

test:
	pytest -m "not live and not e2e" -q

test-e2e:
	pytest -m "e2e and not live" -q

test-live:
	pytest -q

lint:
	ruff check app tests tools evals && mypy app tools evals

# The dev stack plus an identity provider (authentik) on http://localhost:9000 (ADR 0029).
IDP = docker compose -f docker-compose.yml -f docker-compose.idp.yml
idp:
	$(IDP) up -d --build

idp-down:
	$(IDP) down

# Join, move, leave: provision a user in the identity provider, call the gateway as them,
# deprovision, get a 403. Builds its own throwaway stack.
idp-e2e:
	scripts/identity_e2e.sh

# Compare a candidate model with the current one on the golden tasks (ADR 0030):
#   make evals current=fast candidate=anthropic/claude-sonnet-5-5 [runs=3]
# Exit 1 on a regression. Calls real models: costs what the calls cost.
evals:
	python -m evals.run_evals --current $(current) --candidate $(candidate) --runs $(or $(runs),1)

# Validate config/policy.yaml against the rest of the config (ADR 0028).
policy:
	python -m app.check_policy

fmt:
	ruff format app tests tools evals && ruff check --fix app tests tools evals

reload:
	@sed -n 's/^GATEWAY_ADMIN_KEY=/Authorization: Bearer /p' .env | curl -s -X POST localhost:8000/admin/reload -H @-; echo

shell:
	docker compose exec gateway bash

install:
	pip install --require-hashes -r requirements-dev.txt

# Re-resolve pins after editing requirements*.in. Add --upgrade to pull newer versions.
PIP_COMPILE = pip-compile --quiet --strip-extras --generate-hashes --allow-unsafe --no-emit-index-url
lock:
	$(PIP_COMPILE) $(ARGS) -o requirements.txt requirements.in
	$(PIP_COMPILE) $(ARGS) -o requirements-dev.txt requirements-dev.in

migrate:
	docker compose exec gateway alembic upgrade head

# Create a gateway API key:  make key name=my-app tier=dev
key:
	@# The admin key goes to curl on stdin (-H @-), never on the command line where `ps` shows it.
	@sed -n 's/^GATEWAY_ADMIN_KEY=/Authorization: Bearer /p' .env | curl -s -X POST localhost:8000/admin/keys \
	  -H @- -H 'content-type: application/json' -d '{"name":"$(name)","tier":"$(or $(tier),dev)"}'; echo

# Check config/pricing.yaml + catalog.yaml against public catalogs (ADR 0011).
# Shows the diff; ARGS=--write applies changes both sources agree on. Review, then `make reload`.
prices:
	python -m tools.sync_prices $(ARGS)

# Delete usage_log rows older than DAYS (default 90), in batches. Export first if needed.
prune-usage:
	docker compose exec gateway python -m app.maintenance prune --days $(or $(DAYS),90)

# Production: restore the newest backup into a scratch database and compare (Runbooks.md).
PROD = docker compose -f docker-compose.prod.yml --env-file .env.prod
restore-drill:
	$(PROD) exec backup sh /drill.sh

# A key for a new operator, and its ADMIN_KEYS_FILE line (ADR 0025): make admin-key name=alice
admin-key:
	@python -m app.admin_auth $(name)
