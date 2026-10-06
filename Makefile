.PHONY: setup dev stop migrate seed backup restore backup-recover restore-cleanup workspace-up workspace-stop workspace-seed reset-preview reset lint typecheck test build
export WORKSPACE CONFIRM_FINGERPRINT

DRAIN_TIMEOUT ?= 600

setup:
	uv sync --frozen
	npm ci
	python scripts/ensure_env.py

dev:
	docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d --build

stop:
	docker compose -f docker-compose.yml -f docker-compose.dev.yml stop

migrate:
	docker compose -f docker-compose.yml -f docker-compose.dev.yml run --rm migrate

seed:
	docker compose -f docker-compose.yml run --rm --build api python -m modules.knowledge.documents.seed

workspace-up:
	python scripts/dev_workspace.py up --name "$$WORKSPACE"

workspace-stop:
	python scripts/dev_workspace.py stop --name "$$WORKSPACE"

workspace-seed:
	python scripts/dev_workspace.py seed --name "$$WORKSPACE"

reset-preview:
	python scripts/dev_workspace.py reset-preview --name "$$WORKSPACE"

reset:
	python scripts/dev_workspace.py reset --name "$$WORKSPACE" --confirm "$$CONFIRM_FINGERPRINT"

backup:
	uv run python scripts/backup.py --output "$(BACKUP)" --drain-timeout "$(DRAIN_TIMEOUT)"

restore:
	uv run python scripts/restore.py "$(BACKUP)" $(if $(KEEP_ISOLATED),--keep-isolated,)

backup-recover:
	uv run python scripts/restore.py --recover-operation "$(OPERATION_ID)" $(if $(BACKUP),"$(BACKUP)",)

restore-cleanup:
	uv run python scripts/restore.py --cleanup-project "$(PROJECT_ID)"

lint:
	uv run ruff check core apps modules tests infrastructure/postgres/migrations
	npm run lint

typecheck:
	uv run mypy core apps modules
	npm run typecheck

test:
	PYTEST_TARGET="$(PYTEST_TARGET)" E2E_TARGET="$(E2E_TARGET)" bash scripts/test.sh

build:
	npm run build
	docker compose -f docker-compose.yml build
