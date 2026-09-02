# Orbit — 360° capture platform
.DEFAULT_GOAL := help
SHELL := /bin/bash

help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | \
	  awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

up: ## Start Postgres, MinIO and Redis
	docker compose up -d
	@echo "waiting for postgres..."
	@until docker exec orbit-postgres pg_isready -U orbit >/dev/null 2>&1; do sleep 1; done
	@$(MAKE) --no-print-directory migrate
	@echo "infra ready"

down: ## Stop the infra (keeps your data)
	docker compose down

reset: ## Stop the infra AND delete all stored photos and captures
	docker compose down -v

migrate: ## Apply database migrations
	@docker exec -i orbit-postgres psql -U orbit -d orbit -q < backend/migrations/001_init.sql

api: ## Run the Go API on :8080
	cd backend && go run ./cmd/api

worker: ## Run the Python CV worker
	cd cv-worker && .venv/bin/python worker.py

web: ## Serve the web client on :5173
	cd web && python3 serve.py

test: ## Run the Go, JavaScript and CV test suites
	cd backend && go test ./...
	node web/tests/sphere-math.test.js
	@if [ -x cv-worker/.venv/bin/python ]; then \
		cv-worker/.venv/bin/python cv-worker/tests/test_pose_stitch.py; \
		cv-worker/.venv/bin/python cv-worker/tests/test_xmp.py; \
		cv-worker/.venv/bin/python cv-worker/tests/test_finish.py; \
		cv-worker/.venv/bin/python cv-worker/tests/test_coverage.py; \
	else echo "skipping CV tests (no virtualenv)"; fi

build: ## Compile the Go binaries into backend/bin/
	cd backend && go build -o bin/api ./cmd/api && go build -o bin/storagectl ./cmd/storagectl

fmt: ## Format the Go code
	cd backend && gofmt -w . && go vet ./...

logs: ## Tail the infra logs
	docker compose logs -f

.PHONY: help up down reset migrate api worker web test build fmt logs
