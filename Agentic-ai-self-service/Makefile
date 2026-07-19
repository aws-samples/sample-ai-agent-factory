# AgentCore Visual Workflow Platform — developer entry points.
# All targets are thin wrappers; the source of truth stays in each tool's config.

.PHONY: help install dev dev-backend dev-frontend test test-backend test-infra \
        test-frontend lint format typecheck build deploy clean

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*## ' $(MAKEFILE_LIST) | awk -F ':.*## ' '{printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

install: ## Install backend, infra, and frontend dependencies
	cd backend && pip install -e ".[dev]"
	cd infra && pip install -r requirements.txt pytest
	cd frontend && npm ci

dev-backend: ## Run the FastAPI backend locally (http://localhost:8000)
	cd backend && uvicorn app.main:app --app-dir src --reload --port 8000

dev-frontend: ## Run the Vite dev server (http://localhost:5173)
	cd frontend && npm run dev

dev: ## Both dev servers (backend in background; Ctrl-C stops frontend)
	$(MAKE) dev-backend & $(MAKE) dev-frontend

test-backend: ## Backend unit + property tests (no AWS)
	cd backend && python3 -m pytest -m "not integration" -q

test-infra: ## CDK assertion tests
	cd infra && python3 -m pytest tests/ -q

test-frontend: ## Frontend vitest suite
	cd frontend && npx vitest --run

test: test-backend test-infra test-frontend ## All local test suites

lint: ## Ruff + ESLint
	ruff check .
	ruff format --check .
	cd frontend && npx eslint .

format: ## Auto-format Python
	ruff check . --fix
	ruff format .

typecheck: ## Pyright (backend) + tsc (frontend)
	npx --yes pyright backend/src
	cd frontend && npx tsc -b --noEmit

build: ## Production frontend build
	cd frontend && npm run build

deploy: ## Deploy the full stack (requires COGNITO_USERS)
	./scripts/deploy.sh

clean: ## Tear down all AWS resources
	./scripts/cleanup.sh
