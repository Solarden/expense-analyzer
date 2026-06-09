.DEFAULT_GOAL := help
.PHONY: help install dev test lint format check migrate revision user up down logs deploy backup check-update audit css css-watch tailwind-cli

# Tailwind standalone CLI (no Node needed). The binary is a dev tool (gitignored,
# fetched on demand); the built CSS is committed and served offline via
# StaticFiles, exactly like the vendored chart.min.js. See tailwind.css.
TAILWIND_VERSION := v4.3.0
TAILWIND_BIN := tools/tailwindcss
TAILWIND_INPUT := tailwind.css
TAILWIND_OUTPUT := src/expense_analyzer/static/app.css

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

deploy: ## Deploy on the Pi: backup DB -> build -> migrate -> restart, with rollback. Pass a="--pull"
	scripts/deploy.sh $(a)

backup: ## Back up the SQLite database to data/backups (also the design §10 cron target)
	docker compose run --rm --no-deps -T app python -m expense_analyzer.backup

check-update: ## Check our repo for a newer release tag and notify HA (notify-only, never deploys)
	scripts/check_update.sh $(a)

audit: ## Scan dependencies for known CVEs (maintenance — run scheduled, not per-commit)
	uvx pip-audit

tailwind-cli: ## Download the Tailwind standalone CLI for this platform (once)
	@test -x $(TAILWIND_BIN) && echo "Tailwind CLI present" || ( \
		mkdir -p tools && \
		os=$$(uname -s) ; arch=$$(uname -m) ; \
		case "$$os-$$arch" in \
			Darwin-arm64) asset=tailwindcss-macos-arm64 ;; \
			Darwin-x86_64) asset=tailwindcss-macos-x64 ;; \
			Linux-aarch64|Linux-arm64) asset=tailwindcss-linux-arm64 ;; \
			Linux-x86_64) asset=tailwindcss-linux-x64 ;; \
			*) echo "unsupported platform: $$os-$$arch" ; exit 1 ;; \
		esac ; \
		echo "downloading $$asset ($(TAILWIND_VERSION))" ; \
		curl -sL -o $(TAILWIND_BIN) \
			https://github.com/tailwindlabs/tailwindcss/releases/download/$(TAILWIND_VERSION)/$$asset && \
		chmod +x $(TAILWIND_BIN) )

css: tailwind-cli ## Build the dashboard CSS (minified) into static/app.css
	$(TAILWIND_BIN) -i $(TAILWIND_INPUT) -o $(TAILWIND_OUTPUT) --minify
	@printf '\n' >> $(TAILWIND_OUTPUT)  # trailing newline so end-of-file-fixer leaves it alone

css-watch: tailwind-cli ## Rebuild the CSS on template/theme changes while developing
	$(TAILWIND_BIN) -i $(TAILWIND_INPUT) -o $(TAILWIND_OUTPUT) --watch
