.DEFAULT_GOAL := help
.PHONY: help install dev test lint format check migrate revision user up down logs

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

install: ## Create the venv and install all deps (incl. dev)
	uv sync --extra dev

dev: ## Run the app locally with autoreload (debug mode allows the default secret)
	EA_DEBUG=true uv run uvicorn expense_analyzer.main:app --reload

test: ## Run the test suite
	uv run pytest

lint: ## Lint with ruff
	uv run ruff check .

format: ## Format with ruff
	uv run ruff format .

check: ## Run all pre-commit hooks against the whole repo
	uv run pre-commit run --all-files

migrate: ## Apply migrations up to head
	uv run alembic upgrade head

revision: ## Autogenerate a migration: make revision m="add transaction"
	uv run alembic revision --autogenerate -m "$(m)"

user: ## Create a login user: make user u=pawel n="Paweł"
	uv run python -m expense_analyzer.create_user --username "$(u)" --name "$(n)"

up: ## Build and start the docker stack
	docker compose up --build

down: ## Stop the docker stack
	docker compose down

logs: ## Tail docker logs
	docker compose logs -f
